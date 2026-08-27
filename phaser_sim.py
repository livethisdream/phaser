"""Physics-based hardware stubs for --sim mode in phaser_headless.py.

The stubs mimic just enough of adi.one_bit_adc_dac / adi.adar1000_array /
adi.ad9361 for phaser_headless.py to run with no Phaser attached. The SDR
stub synthesizes element-level IQ from a boresight HB100 tone, applies the
element phases + taper the ADAR stub has captured, sums into two digital
sub-arrays (chan0 = elements 1-4, chan1 = elements 5-8), and returns the
same [chan0, chan1] shape SDR_getData produces on real hardware.
"""

import numpy as np


class _StubGPIOs:
    """Swallows attribute writes (gpio_vctrl_1 = 1, gpio_tx_sw = 0, ...)."""

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class _StubElement:
    """One element slot on the ADAR array.

    Models the two-stage write the real part has: `rx_gain` / `rx_phase` land
    in SPI shadow registers, and only a latch (RX_LOAD / `rx_load_spi`) moves
    them into the beam state the RF path actually uses. `latched_gain` and
    `latched_phase` are that beam state, and they are what SimSDR reads.

    Modelling this is the point, not pedantry: a caller that writes phases and
    forgets to latch gets a beam that never moves, on the real array and here
    alike -- which is the flat-line-instead-of-a-beam-pattern failure.
    """

    def __init__(self):
        # SPI shadow registers (what the driver writes).
        self.rx_gain = 100
        self.rx_phase = 0.0
        self.rx_attenuator = False
        # Live beam state (what the RF path uses). Starts matching the
        # shadow, so a correctly-latching caller behaves exactly as before.
        self.latched_gain = 100
        self.latched_phase = 0.0

    def latch(self):
        self.latched_gain = 0 if self.rx_attenuator else self.rx_gain
        self.latched_phase = self.rx_phase


class _StubChannel:
    """One of the four Rx channels on an ADAR1000 chip."""

    def __init__(self):
        self.rx_enable = False


class _StubDevice:
    """One ADAR1000 chip. reset() is a no-op; register writes are recorded."""

    def __init__(self):
        self.mode = "rx"
        self.channels = [_StubChannel() for _ in range(4)]

    def reset(self):
        pass


class _StubADARArray:
    """Stands in for adi.adar1000_array.

    Exposes:
      - elements: dict {1..8 -> _StubElement}, written by ADAR_set_Taper/Phase
      - devices:  dict {name -> _StubDevice}, iterated by ADAR_init/ADAR_set_mode
      - latch_rx_settings(), which commits the element writes to beam state

    The SDR stub reads back the *latched* gain/phase to build the per-element
    weighting used to synthesize IQ.
    """

    def __init__(self):
        self.elements = {i + 1: _StubElement() for i in range(8)}
        self.devices = {"BEAM0": _StubDevice(), "BEAM1": _StubDevice()}
        self.latch_count = 0
        # Intrinsic per-element phase error, degrees -- the thing the phase
        # calibration exists to cancel. Zero by default, so a plain sim run is
        # a perfectly matched array; a test sets it to give pcal something
        # real to correct.
        self.element_phase_error = [0.0] * 8

    def latch_rx_settings(self):
        """Commit every element's shadow registers to its beam state."""
        for element in self.elements.values():
            element.latch()
        self.latch_count += 1


class SimSDR:
    """Stub adi.ad9361. Records LO/gain/etc.; rx() synthesizes IQ per call.

    The array stub is passed in so rx() can read the currently-commanded
    per-element rx_gain and rx_phase and produce a physically-consistent
    beam response.
    """

    # Target sits at boresight (0°) — matches an HB100 pointed straight at
    # the array, per user preference.
    TARGET_ANGLE_DEG = 0.0

    # IF tone the sim places in the baseband spectrum (Hz). Real target
    # appears at some frequency after downconversion; we just pick a value
    # inside the passband so the FFT plot shows a peak.
    TARGET_IF_HZ = 1.0e6

    # Interferer defaults. All off by default so plain sim mode remains a
    # clean single-target scene. Enabled and configured from phaser_headless
    # via set_interferer(); the frontend "Simulator Interferer" panel is
    # hidden unless the app is loaded with ?instructor=1.
    #
    # NOTE: Interferer runs at the SAME IF as the target on purpose. This
    # is the classic textbook MVDR setup — two co-frequency sources at
    # different arrival angles, where the covariance matrix has clean
    # rank-2 structure and MVDR can place a hard null on the interferer.
    # A different-IF interferer is more realistic but shows much weaker
    # nulling because the sources are then temporally uncorrelated.
    INTERFERER_IF_HZ = 1.0e6  # same as TARGET_IF_HZ (see comment above)

    def __init__(self, array, signal_freq, element_spacing, sample_rate=3e6,
                 buffer_size=1024 * 16):
        self._array = array
        self._SignalFreq = signal_freq
        self._d = element_spacing
        self.sample_rate = int(sample_rate)
        self.rx_buffer_size = int(buffer_size)

        # Attributes touched by SDR_init / SDR_setRx / SDR_setTx / CW radar
        self.rx_lo = 0
        self.tx_lo = 0
        self.rx_rf_bandwidth = int(sample_rate)
        self.tx_rf_bandwidth = int(sample_rate)
        self.rx_enabled_channels = [0, 1]
        self.tx_enabled_channels = [0, 1]
        self.tx_cyclic_buffer = True
        self.gain_control_mode_chan0 = "manual"
        self.gain_control_mode_chan1 = "manual"
        self.rx_hardwaregain_chan0 = 30
        self.rx_hardwaregain_chan1 = 30
        self.tx_hardwaregain_chan0 = -30
        self.tx_hardwaregain_chan1 = -30

        # Interferer state (default: disabled)
        self._interferer_enable = False
        self._interferer_angle_deg = 30.0
        self._interferer_power_db = 0.0   # relative to target amplitude

        self._rng = np.random.default_rng(0)

    def set_signal_freq(self, freq_hz):
        """Kept in sync from phaser_headless.set_signal_freq so the spatial
        phase progression uses the current SignalFreq."""
        self._SignalFreq = float(freq_hz)

    def set_interferer(self, enable=None, angle_deg=None, power_db=None):
        """Configure the simulator interferer. Called from phaser_headless
        when the frontend (instructor mode) updates interferer state."""
        if enable is not None:
            self._interferer_enable = bool(enable)
        if angle_deg is not None:
            self._interferer_angle_deg = float(angle_deg)
        if power_db is not None:
            self._interferer_power_db = float(power_db)

    def _superpose_source(self, chan_sub, t, if_hz, arrival_angle_deg,
                          amplitude, wavelength):
        """Add one narrowband plane-wave source at (arrival_angle_deg, if_hz,
        amplitude) into the two sub-array accumulators.

        Physical model per element k (k=0..7):
          phi_incident_k = 2*pi * (k * d / lambda) * sin(theta)
          elem_signal   = (gain_k/127) * exp(j*if*t)
                          * exp(j*(phi_incident_k - rx_phase_k))
        Sub-array 0 = elements 0..3, sub-array 1 = elements 4..7 (matches
        the ADAR device_element_map wiring in _do_init_hardware).
        """
        wave = np.exp(1j * 2 * np.pi * if_hz * t).astype(np.complex64)
        theta_rad = np.radians(arrival_angle_deg)
        for k in range(8):
            el = self._array.elements[k + 1]
            # Latched beam state, not the shadow registers -- an unlatched
            # write must not steer the simulated array either.
            gain = max(0.0, min(127.0, float(el.latched_gain))) / 127.0
            rx_phase_rad = np.radians(float(el.latched_phase))
            # Intrinsic element phase error, which the commanded rx_phase has
            # to absorb via the phase calibration.
            err_rad = np.radians(float(self._array.element_phase_error[k]))
            phi_incident = (
                2 * np.pi * (k * self._d / wavelength) * np.sin(theta_rad) + err_rad
            )
            elem_signal = amplitude * gain * wave * np.exp(
                1j * (phi_incident - rx_phase_rad)
            )
            sub = 0 if k < 4 else 1
            chan_sub[sub] = chan_sub[sub] + elem_signal

    def _synthesize_channels(self):
        """Return (chan0, chan1) as 1D complex arrays, shape (buffer_size,).

        Sums:
          - Target: plane wave at TARGET_ANGLE_DEG, IF = TARGET_IF_HZ, unit amp
          - Interferer (if enabled): plane wave at interferer_angle_deg,
            IF = INTERFERER_IF_HZ, amp = 10^(power_db/20) relative to target
          - Independent complex noise per sub-array
        """
        N = self.rx_buffer_size
        fs = float(self.sample_rate)
        t = np.arange(N) / fs

        c = 299_792_458.0
        wavelength = c / self._SignalFreq

        chan_sub = [
            np.zeros(N, dtype=np.complex64),
            np.zeros(N, dtype=np.complex64),
        ]

        # Target (always present)
        self._superpose_source(
            chan_sub, t,
            if_hz=self.TARGET_IF_HZ,
            arrival_angle_deg=self.TARGET_ANGLE_DEG,
            amplitude=1.0,
            wavelength=wavelength,
        )

        # Interferer (only when instructor mode has enabled it). Uses the
        # same IF as the target — see class comment for rationale.
        if self._interferer_enable:
            interf_amp = 10.0 ** (self._interferer_power_db / 20.0)
            self._superpose_source(
                chan_sub, t,
                if_hz=self.INTERFERER_IF_HZ,
                arrival_angle_deg=self._interferer_angle_deg,
                amplitude=interf_amp,
                wavelength=wavelength,
            )

        # Scale so a full-taper on-boresight beam peaks around -10 dBFS in
        # the sum channel, matching typical HB100 signal levels on real
        # hardware (leaves headroom before the 2^11 fixed-point full-scale).
        amp = 60.0
        chan0 = chan_sub[0] * amp
        chan1 = chan_sub[1] * amp

        # Additive complex noise (per sub-array, independent). Sigma chosen
        # to keep per-element SNR ~28 dB, so the beam pattern shows a clean
        # main lobe with realistic sidelobe-vs-noise ratio.
        noise_sigma = 4.0
        chan0 = chan0 + (self._rng.normal(0, noise_sigma, N)
                         + 1j * self._rng.normal(0, noise_sigma, N)).astype(np.complex64)
        chan1 = chan1 + (self._rng.normal(0, noise_sigma, N)
                         + 1j * self._rng.normal(0, noise_sigma, N)).astype(np.complex64)

        return chan0, chan1

    def rx(self):
        c0, c1 = self._synthesize_channels()
        return [c0, c1]


def make_stub_gpios():
    return _StubGPIOs()


def make_stub_array():
    return _StubADARArray()
