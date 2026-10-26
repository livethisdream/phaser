"""
FMCW radar helpers for the Phaser headless backend.

Phase C0 is the capability probe. Phase C1 adds the hardware path: program
the ADF4159 ramp, sync the Pluto capture to it, capture and slice a frame, and
put everything back. The mode that drives it is C2 (docs/fmcw-phase-c-plan.md).

Nothing here imports pyadi-iio. The probe and FmcwHardware.open take the `adi`
module as an argument, and the CW helpers (which do import it, through
SDR_functions) are imported inside the functions that use them. That keeps the
probe and its tests running on CI, which has no pyadi-iio.

FMCW needs four things a stock kit may not have, and every one of them fails
silently when missing -- the ramp never starts, the capture is never
triggered, and the plot shows noise:

  - pyadi-iio with `adi.tddn` (the Pluto's new TDD engine driver)
  - Pluto firmware v0.39 or later (the TDD engine itself)
  - the Pluto's `phaser_enable` GPIO and the Phaser's `burst` GPIO, which
    together trigger one capture per chirp
  - an ADF4159 driver that exposes the frequency-ramp attributes

So the backend asks once, at startup, and reports the answer with a reason,
rather than letting a lab discover it as an empty plot.

The probe only READS, from the iio contexts the backend already holds: no
writes, and no new contexts (open ones are already a leak on the todo list).
It never raises -- a probe that crashes the backend would be worse than no
probe.
"""

import re

# Firmware that carries the TDD engine the chirp sync depends on.
MIN_PLUTO_FW = (0, 39)

TDD_DEVICE = "iio-axi-tdd-0"
GPIO_DEVICE = "one-bit-adc-dac"
PLUTO_SYNC_GPIOS = ("phaser_enable",)
# The lab scripts also set gpio_tdd_ext_sync, but Pluto v0.39 has no such
# channel (only phaser_enable and muxout), and chirp sync works without it:
# Range_Doppler_Plot.py was run on a v0.39 kit on 2026-10-26 and produced a
# live range-Doppler map. On that firmware the scripts' write is a silent
# no-op -- pyadi-iio only makes gpio_<label> properties for channels that
# exist, so the assignment just sets a plain Python attribute. Reported when
# present, never required; the mode must only write it when it exists.
PLUTO_OPTIONAL_GPIOS = ("tdd_ext_sync",)
PHASER_BURST_GPIO = "burst"
ADF4159_DEVICE = "adf4159"
ADF4159_CHANNEL = "altvoltage0"
ADF4159_RAMP_ATTRS = (
    "ramp_mode",
    "frequency_deviation_range",
    "frequency_deviation_step",
    "frequency_deviation_time",
)

# The two ramps the labs use. Synced sawtooth drives range-Doppler, MTI and
# CFAR (Range_Doppler_Plot.py and the *_ChirpSync.py scripts); the free-running
# triangle is what RADAR_FFT_Waterfall.py runs, and needs no TDD at all, so it
# is the fallback when the sync chain is missing. Defined beside the plans that
# depend on them, in phaser_radar_dsp.
from phaser_radar_dsp import (  # noqa: E402
    RAMP_FREE_RUNNING,
    RAMP_SYNCED,
    TDD_OFF_RAW,
    TDD_ON_RAW,
    build_iq_tone,
    chirp_plan,
    fmcw_config,
    ramp_registers,
    slice_chirps,
    to_adf4159_hz,
)


def parse_fw_version(text):
    """'v0.39', 'v0.39-dirty' or '0.38' -> (0, 39). None if unparseable."""
    m = re.search(r"(\d+)\.(\d+)", str(text or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _attr_value(attrs, name):
    """Read one libiio attribute, tolerating both the libiio Attr objects and
    plain strings (which is what the test fakes use)."""
    attr = attrs[name]
    return getattr(attr, "value", attr)


def _find_gpio_device(ctx):
    """The one-bit-adc-dac device, matched the way pyadi-iio matches it: by
    label first, then by name."""
    for dev in ctx.devices:
        attrs = getattr(dev, "attrs", {}) or {}
        if "label" in attrs and _attr_value(attrs, "label") == GPIO_DEVICE:
            return dev
        if getattr(dev, "name", None) == GPIO_DEVICE:
            return dev
    return None


def _gpio_labels(ctx):
    """Lower-cased channel labels of a context's GPIO device -- the same names
    pyadi-iio turns into `gpio_<label>` properties. None if no such device."""
    dev = _find_gpio_device(ctx)
    if dev is None:
        return None
    labels = set()
    for chan in dev.channels:
        attrs = getattr(chan, "attrs", {}) or {}
        if "label" in attrs:
            labels.add(str(_attr_value(attrs, "label")).lower())
    return labels


def _check(name, fn):
    """Run one probe step; any exception becomes a failed check, not a crash."""
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001 -- a probe must never raise
        ok, detail = False, f"could not check: {e}"
    return {"name": name, "ok": bool(ok), "detail": detail}


def probe_fmcw(adi_module, sdr_ctx, rpi_ctx, sim=False):
    """Report whether this kit can run FMCW, and if not, why.

    Args:
        adi_module: the imported `adi` package (injected so tests can fake it)
        sdr_ctx: the Pluto's libiio context (the backend's `sdr._ctx`)
        rpi_ctx: the Phaser's libiio context (the backend's `gpios._ctx`)
        sim: True under --sim, where the simulator stands in for all of it

    Returns:
        {"available": bool, "ramp": RAMP_SYNCED | RAMP_FREE_RUNNING | None,
         "source": "hardware" | "sim", "reason": str, "checks": [...]}
        `reason` is empty when the synced ramp is available, and otherwise
        names the first thing standing in its way.
    """
    if sim:
        return {
            "available": True,
            "ramp": RAMP_SYNCED,
            "source": "sim",
            "reason": "",
            "checks": [],
        }

    def pyadi_tddn():
        if hasattr(adi_module, "tddn"):
            return True, "adi.tddn present"
        return False, "pyadi-iio on the Pi has no adi.tddn; upgrade pyadi-iio"

    def pluto_fw():
        raw = _attr_value(sdr_ctx.attrs, "fw_version")
        version = parse_fw_version(raw)
        if version is None:
            return False, f"unrecognized Pluto firmware version {raw!r}"
        need = "v%d.%d" % MIN_PLUTO_FW
        if version < MIN_PLUTO_FW:
            return False, f"Pluto firmware {raw} is older than {need}"
        return True, f"Pluto firmware {raw}"

    def pluto_tdd():
        if sdr_ctx.find_device(TDD_DEVICE) is not None:
            return True, f"{TDD_DEVICE} present"
        return False, f"Pluto has no {TDD_DEVICE} (TDD engine)"

    def pluto_gpios():
        labels = _gpio_labels(sdr_ctx)
        if labels is None:
            return False, f"Pluto has no {GPIO_DEVICE} device"
        missing = [g for g in PLUTO_SYNC_GPIOS if g not in labels]
        if missing:
            return False, "Pluto GPIOs missing: " + ", ".join(missing)
        optional = [g for g in PLUTO_OPTIONAL_GPIOS if g in labels]
        return True, "Pluto sync GPIOs present" + (
            " (+ " + ", ".join(optional) + ")" if optional else "")

    def phaser_burst():
        labels = _gpio_labels(rpi_ctx)
        if labels is None:
            return False, f"Phaser has no {GPIO_DEVICE} device"
        if PHASER_BURST_GPIO not in labels:
            return False, f"Phaser GPIO '{PHASER_BURST_GPIO}' missing"
        return True, "Phaser burst GPIO present"

    ramp_modes = []

    def adf4159_ramp():
        dev = rpi_ctx.find_device(ADF4159_DEVICE)
        if dev is None:
            return False, "Phaser has no adf4159 device"
        chan = dev.find_channel(ADF4159_CHANNEL, True)
        if chan is None:
            return False, f"adf4159 has no {ADF4159_CHANNEL} output channel"
        missing = [a for a in ADF4159_RAMP_ATTRS if a not in chan.attrs]
        if missing:
            return False, "adf4159 ramp attributes missing: " + ", ".join(missing)
        ramp_modes.extend(
            str(_attr_value(chan.attrs, "ramp_mode_available")).split())
        return True, "ramp modes: " + " ".join(ramp_modes)

    checks = [
        _check("pyadi_tddn", pyadi_tddn),
        _check("pluto_firmware", pluto_fw),
        _check("pluto_tdd", pluto_tdd),
        _check("pluto_sync_gpios", pluto_gpios),
        _check("phaser_burst_gpio", phaser_burst),
        _check("adf4159_ramp", adf4159_ramp),
    ]
    by_name = {c["name"]: c for c in checks}

    synced = all(c["ok"] for c in checks) and RAMP_SYNCED in ramp_modes
    free_running = (by_name["adf4159_ramp"]["ok"]
                    and RAMP_FREE_RUNNING in ramp_modes)

    reason = ""
    if not synced:
        failed = [c for c in checks if not c["ok"]]
        reason = (failed[0]["detail"] if failed
                  else f"adf4159 does not offer {RAMP_SYNCED}")
        if free_running:
            reason += "; range lab only (free-running ramp, no range-Doppler)"

    return {
        "available": synced or free_running,
        "ramp": RAMP_SYNCED if synced else (RAMP_FREE_RUNNING if free_running else None),
        "source": "hardware",
        "reason": reason,
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# Phase C1: the hardware path.
#
# Sequence and values follow Range_Doppler_Plot.py, which is the script proven
# on the bench (2026-10-26). Every value written is computed in
# phaser_radar_dsp, so this half only decides order and undo.
# ---------------------------------------------------------------------------

# Tx tone length. The scripts load 2**18 samples; build_iq_tone snaps the
# tone to a whole number of cycles in it, so the cyclic buffer wraps cleanly.
TX_TONE_SAMPLES = 2 ** 18
TX_TONE_SCALE = 0.9 * 2 ** 14
TX_OFF_DB = -88
TDD_CHANNELS = 3

# Steps enter_fmcw_mode records in saved["landed"], each one BEFORE it starts,
# so a step that failed halfway is still undone.
STEP_SDR = "sdr"
STEP_TAPER = "taper"
STEP_RAMP = "ramp"
STEP_PLUTO_GPIOS = "pluto_gpios"
STEP_TDD = "tdd"
STEP_TX = "tx"


class FmcwHardware:
    """The handles FMCW needs beyond the ones the backend already holds.

    Opened once when the mode is entered and dropped when it is left, rather
    than reopened per write: SDR_LO_init opens a fresh libiio context on every
    call, and those already leak. The Phaser's own one_bit_adc_dac (for the
    `burst` GPIO) is the backend's, passed in, not reopened.
    """

    def __init__(self, synth, tdd=None, pluto_pins=None, phaser_gpios=None,
                 pluto_labels=None):
        self.synth = synth
        self.tdd = tdd
        self.pluto_pins = pluto_pins
        self.phaser_gpios = phaser_gpios
        self.pluto_labels = set(pluto_labels or ())

    @classmethod
    def open(cls, adi_module, rpi_ip, sdr_ip, phaser_gpios, synced=True):
        """Open the ADF4159 and, for the synced ramp, the Pluto TDD and GPIOs."""
        synth = adi_module.adf4159(rpi_ip)
        tdd = pins = None
        labels = set()
        if synced:
            pins = adi_module.one_bit_adc_dac(sdr_ip)
            tdd = adi_module.tddn(sdr_ip)
            ctx = getattr(pins, "_ctx", None)
            labels = (_gpio_labels(ctx) if ctx is not None else None) or set()
        return cls(synth, tdd, pins, phaser_gpios, labels)

    def has_pluto_gpio(self, name):
        return name in self.pluto_labels

    def close(self):
        """Drop the handles; their libiio contexts close when collected."""
        self.synth = None
        self.tdd = None
        self.pluto_pins = None


def _land(saved, step):
    saved.setdefault("landed", []).append(step)


def _tx_payload(sdr, iq):
    # Lazy: phaser_cw_radar imports SDR_functions, which imports adi.
    from phaser_cw_radar import _tx_channel_count
    return [iq, iq] if _tx_channel_count(sdr) > 1 else iq


def enter_fmcw_mode(sdr, params, saved, hw, array=None, ramp_mode=RAMP_SYNCED):
    """Program the ramp, sync the capture to it, and start the Tx tone.

    Args:
        sdr: the backend's adi.ad9361
        params: FMCW config (fmcw_config() is applied, so partial is fine).
            `center_freq` should be the kit's characterized IF.
        saved: dict to fill with what to undo; pass it to exit_fmcw_mode
            whether or not this returns
        hw: an FmcwHardware
        array: ADAR1000 array, for the taper. Optional.
        ramp_mode: RAMP_SYNCED, or RAMP_FREE_RUNNING when the probe found no TDD

    Returns:
        (cfg, plan). `plan` is built from the ramp time the ADF4159 read back.

    Raises on any failure. The caller unwinds with exit_fmcw_mode, as the
    dispatcher already does for CW.
    """
    from phaser_cw_radar import apply_taper, snapshot_sdr

    cfg = fmcw_config(params)
    synced = ramp_mode == RAMP_SYNCED
    if synced and (hw.tdd is None or hw.pluto_pins is None):
        raise RuntimeError("synced FMCW needs the Pluto TDD and GPIO handles")
    if hw.phaser_gpios is None and synced:
        raise RuntimeError("synced FMCW needs the Phaser GPIOs for the burst trigger")

    fs = int(cfg["sample_rate"])
    center = int(cfg["center_freq"])
    rx_gain = int(cfg["rx_gain"])

    # 1. Snapshot, then 2. taper.
    saved["sdr"] = snapshot_sdr(sdr, {}, tag="FMCW")
    if array is not None:
        _land(saved, STEP_TAPER)
        apply_taper(array, cfg.get("taper"))

    # 3. SDR rates, LOs and gains, as Range_Doppler_Plot.py sets them.
    _land(saved, STEP_SDR)
    sdr.sample_rate = fs
    sdr.rx_lo = center
    sdr.gain_control_mode_chan0 = "manual"
    sdr.rx_hardwaregain_chan0 = rx_gain
    if hasattr(sdr, "rx_hardwaregain_chan1"):
        sdr.gain_control_mode_chan1 = "manual"
        sdr.rx_hardwaregain_chan1 = rx_gain
    sdr.tx_lo = center
    # Required for TDD burst mode; without it Tx turns on and off at random.
    sdr.tx_cyclic_buffer = True
    sdr.tx_hardwaregain_chan0 = TX_OFF_DB
    if hasattr(sdr, "tx_hardwaregain_chan1"):
        sdr.tx_hardwaregain_chan1 = int(cfg["tx_gain"])

    # 4. The ramp. `enable = 0` comes last in the list and latches the rest.
    _land(saved, STEP_RAMP)
    for attr, value in ramp_registers(cfg, ramp_mode):
        setattr(hw.synth, attr, value)
    actual_us = int(hw.synth.freq_dev_time)
    plan = chirp_plan(cfg, ramp_mode, actual_us, on_ms=0.0)

    # 5. Chirp sync. tdd_ext_sync only where the channel exists: Pluto v0.39
    # has none, and writing it there only sets a plain Python attribute.
    if synced:
        _land(saved, STEP_PLUTO_GPIOS)
        prior = {"gpio_phaser_enable": bool(hw.pluto_pins.gpio_phaser_enable)}
        if hw.has_pluto_gpio("tdd_ext_sync"):
            prior["gpio_tdd_ext_sync"] = bool(hw.pluto_pins.gpio_tdd_ext_sync)
        saved["pluto_gpios"] = prior
        if "gpio_tdd_ext_sync" in prior:
            hw.pluto_pins.gpio_tdd_ext_sync = True
        hw.pluto_pins.gpio_phaser_enable = True

        _land(saved, STEP_TDD)
        tdd = hw.tdd
        tdd.enable = False
        tdd.sync_external = True
        tdd.startup_delay_ms = 0
        tdd.frame_length_ms = plan["pri_ms"]
        tdd.burst_count = plan["num_chirps"]
        for i in range(TDD_CHANNELS):
            ch = tdd.channel[i]
            ch.enable = True
            ch.polarity = False
            ch.on_raw = TDD_ON_RAW
            ch.off_raw = TDD_OFF_RAW
        tdd.enable = True

        hw.phaser_gpios.gpio_burst = 0

    sdr.rx_buffer_size = int(plan["rx_buffer_size"])

    # 6. The Tx tone.
    _land(saved, STEP_TX)
    try:
        sdr.tx_destroy_buffer()
    except Exception:
        pass
    iq = build_iq_tone(fs, TX_TONE_SAMPLES, float(cfg["signal_freq"]), scale=TX_TONE_SCALE)
    sdr.tx(_tx_payload(sdr, iq))

    # 7. Throw away the first frame. A buffer filled before the ramp and the
    # sync were in place is the stale-buffer failure from calibration bug 3.
    capture_fmcw_frame(sdr, hw, plan)

    return cfg, plan


def capture_fmcw_frame(sdr, hw, plan):
    """One frame: trigger the burst (synced only), read, sum, slice.

    Returns slice_chirps' samples x chirps matrix for the synced ramp, or the
    raw summed buffer for the free-running one.
    """
    if plan["synced"]:
        gpios = hw.phaser_gpios
        gpios.gpio_burst = 0
        gpios.gpio_burst = 1
        gpios.gpio_burst = 0
    data = sdr.rx()
    summed = data[0] + data[1]
    return slice_chirps(summed, plan)


def exit_fmcw_mode(sdr, saved, hw, array=None, restore_taper=None,
                   restore_lo_freq=None):
    """Undo whatever enter_fmcw_mode got as far as doing, then drop the handles.

    Every step is attempted independently, so a failure in one does not leave
    the rest undone, and a half-entered mode is as leavable as a full one.
    Calling it twice is harmless: the first call clears `saved["landed"]`.

    The TDD goes off first. Left on with sync_external set, every plain rx()
    the beamforming sweep makes waits for a trigger that never comes.
    """
    from phaser_cw_radar import apply_taper, restore_sdr

    landed = set(saved.get("landed", ()))

    def attempt(what, fn):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 -- keep unwinding
            print(f"[FMCW] Warning: could not {what}: {e}")

    synth = hw.synth if hw is not None else None
    tdd = hw.tdd if hw is not None else None
    pins = hw.pluto_pins if hw is not None else None

    if STEP_TDD in landed and tdd is not None:
        attempt("disable the TDD engine", lambda: setattr(tdd, "enable", False))

    if STEP_PLUTO_GPIOS in landed and pins is not None:
        for attr, value in (saved.get("pluto_gpios") or {}).items():
            attempt(f"restore {attr}", lambda a=attr, v=value: setattr(pins, a, v))

    if STEP_RAMP in landed and synth is not None:
        def stop_ramp():
            synth.ramp_mode = "disabled"
            synth.tx_trig_en = 0
            if restore_lo_freq:
                synth.frequency = to_adf4159_hz(restore_lo_freq)
            synth.enable = 0  # last: latches the writes above
        attempt("stop the ramp and restore the LO", stop_ramp)

    if STEP_TX in landed:
        attempt("destroy the Tx buffer", sdr.tx_destroy_buffer)

    if STEP_SDR in landed and saved.get("sdr"):
        attempt("restore the SDR", lambda: restore_sdr(sdr, saved["sdr"], tag="FMCW"))

    if STEP_TAPER in landed and array is not None and restore_taper is not None:
        attempt("restore the taper", lambda: apply_taper(array, restore_taper))

    saved["landed"] = []
    if hw is not None:
        hw.close()
