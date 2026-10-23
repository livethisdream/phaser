import os
import time

from phaser_functions import load_cal_values

def load_phase_cal(default=None, filename=None):
    """Per-element phase offsets, from calibration.json.

    Falls back to the legacy phase_cal_val.pkl inside load_cal_values, so a Pi
    calibrated before the JSON store existed keeps working until its next run.
    `filename` is accepted and ignored for backwards compatibility.
    """
    if default is None:
        default = [0.0] * 8
    return load_cal_values("phase_cal", default, 8)


def load_gain_cal(default=None, filename=None):
    """Per-element gain trims. Same JSON-then-legacy path as load_phase_cal."""
    if default is None:
        default = [1.0] * 8
    return load_cal_values("gain_cal", default, 8)


def ADAR_init(device):
    """Bring one ADAR1000 up into a usable Rx state.

    This used to be `device.reset()` and nothing else, which is worse than
    doing nothing: reset returns the part to its power-on defaults, and in
    those defaults the whole Rx chain -- LNA, vector modulator, VGA -- is
    powered down, and the beam state is driven from on-chip RAM rather than
    from the SPI registers that `rx_phase`/`rx_gain` write.

    A part left in that state still passes some signal, so the spectrum keeps
    showing the HB100 tone, but the vector modulator is off and the beam
    state does not come from SPI -- so every steering phase we write lands on
    a register nothing is reading. The sweep then measures the same array
    response at every angle, which plots as a flat line where the beam
    pattern should be.

    The register writes below are the ones pyadi-iio's own CN0566.configure()
    and adar1000.initialize() perform, and they match ADI's reference
    examples/phaser/ADAR_pyadi_functions.py.
    """
    device.reset()
    time.sleep(0.1)

    # Beam and bias state from SPI, not from the on-chip RAM sequencer.
    # Without this the rx_phase/rx_gain writes below have no path to the
    # beam state at all.
    device.sequencer_enable = False
    device.beam_mem_enable = False
    device.bias_mem_enable = False

    # T/R control comes from SPI, held in Rx.
    device.pol_state = False
    device.pol_switch_enable = False
    device.tr_source = "spi"
    device.tr_spi = "rx"
    device.tr_switch_enable = True
    device.external_tr_polarity = True

    # Power up the Rx chain. rx_vm_enable is the one that actually makes
    # phase shifting happen -- with the vector modulator off, the array
    # cannot steer no matter what phase we command.
    device.rx_vga_enable = True
    device.rx_vm_enable = True
    device.rx_lna_enable = True
    device.rx_lna_bias_current = 8    # middle of range
    device.rx_vga_vm_bias_current = 22


def ADAR_set_mode(device, mode):
    """ Set rx/tx mode """
    device.mode = mode
    if mode == "rx":
        # The external LNAs on the CN0566 self-bias; driving the bias output
        # fights them.
        device.lna_bias_out_enable = False
        # Enable the Rx path on all four channels of this chip. A channel
        # left disabled contributes nothing to its sub-array.
        for channel in device.channels:
            channel.rx_enable = True


# Voltage amplitude of an element at each rx_gain register code, relative to
# code 127. The register is not linear in voltage: the ECE 444 Module 3 dry run
# (2026-10-23, phaser-00) fitted amplitude = (code/127)**0.68 to three taper
# peak drops (Hann, Blackman, Chebyshev) within 0.1 dB, and that fit predicted
# the beamwidths within ~0.5 deg without being fitted to them. Mapping slider %
# linearly onto the code made every taper milder than the slider said.
#
# This is the one place the chip's curve lives. rx_gain_code() inverts it for
# the backend, and phaser_sim reads it to model the chip, so the simulator stays
# linear in slider %. A table measured with tools/measure_rx_gain_curve.py
# replaces it by replacing this list (128 entries, 0.0 at code 0, 1.0 at 127);
# then re-run tools/gen_sim_constants.py.
RX_GAIN_EXPONENT = 0.68
RX_GAIN_AMPLITUDE = [(code / 127.0) ** RX_GAIN_EXPONENT for code in range(128)]


def rx_gain_code(amplitude):
    """Register code whose voltage amplitude is nearest `amplitude`.

    `amplitude` is a fraction of full scale (code 127). Ties go to the lower
    code. Zero maps to code 0, which ADAR_set_Taper routes through the
    attenuator.
    """
    target = max(0.0, min(1.0, float(amplitude)))
    best = 0
    for code in range(1, 128):
        if abs(RX_GAIN_AMPLITUDE[code] - target) < abs(RX_GAIN_AMPLITUDE[best] - target):
            best = code
    return best


def ADAR_set_Taper(array, taper_list):
    """ Set array taper gains """
    # Map elements 1..8 to their gains
    for i in range(8):
        element_id = i + 1
        array.elements[element_id].rx_gain = int(taper_list[i])
        # Route an element commanded to zero through the attenuator, so a
        # nulled element is actually off rather than just turned down.
        array.elements[element_id].rx_attenuator = not bool(taper_list[i])
    # Transfer the SPI registers we just wrote into the live beam state.
    # Nothing above takes effect until this runs.
    array.latch_rx_settings()

def ADAR_set_Phase(array, PhDelta, phase_step_size, phaseList):
    """Set array phases for a given steering delta.

    `phase_step_size` quantizes the STEERING RAMP only, which is what legacy
    does:

        (rint(PhDelta * i / step) * step + phaseList[i] + pcal[i]) % 360

    The distinction matters. phase_step_size is the "phase shift bits" knob,
    and a lab that drops it to 3 bits is asking what a 45-degree phase shifter
    does to the beam -- not asking to round the phase calibration off to the
    nearest 45 degrees as well. Quantizing the sum did the latter, which threw
    away the per-element correction that makes the elements add coherently,
    exactly when the pattern was already at its most fragile.

    `phaseList` carries the user's per-element offsets with the phase
    calibration already folded in by the caller.
    """
    for i in range(8):
        element_id = i + 1
        # Quantize the steering ramp; leave the offsets at full resolution.
        ramp = round(i * PhDelta / phase_step_size) * phase_step_size
        q_phase = (ramp + phaseList[i]) % 360
        if q_phase < 0:
            q_phase += 360

        array.elements[element_id].rx_phase = q_phase
    # Same as the taper: the rx_phase writes above sit in SPI shadow
    # registers until they are latched. Sweeping without this leaves the
    # array pointing wherever it was last latched, for every angle.
    array.latch_rx_settings()
