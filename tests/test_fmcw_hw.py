"""The FMCW hardware path (Phase C1): plans, register writes, and the unwind.

Two halves:

  - The plans (phaser_radar_dsp): presets, ADF4159 registers, chirp timing
    and slicing. Held to the numbers the lab scripts compute, because those
    scripts are what the labs run and the only FMCW path proven on a kit.

  - The hardware sequence (phaser_fmcw_radar): enter, capture and exit,
    against recording fakes. What matters on a kit is mostly order and undo
    -- `enable` written last or the ramp never latches, the TDD left on and
    every later rx() hangs -- and neither shows up as an error, only as an
    empty plot. So the fakes log every write and the tests read the log.
"""

import sys
import types

import numpy as np
import pytest

import phaser_radar_dsp as dsp
import phaser_fmcw_radar as fm

# phaser_fmcw_radar imports the CW helpers lazily, and phaser_cw_radar imports
# SDR_functions, which imports adi. CI has no pyadi-iio; nothing here reaches
# it, so a stub is enough there. Use the real one where it exists.
try:
    import adi  # noqa: F401
except Exception:
    sys.modules.setdefault("adi", types.ModuleType("adi"))

import phaser_cw_radar  # noqa: E402

C = dsp.C
CENTER = 2.1e9


# --- recording fakes ------------------------------------------------------

class _Rec:
    """Holds attribute values and logs every write as (name, attr, value).

    `fail` is a shared set of (name, attr) to raise on, once each, so a test
    can break enter_fmcw_mode at any write and the unwind's own writes to the
    same attribute still go through.
    """

    def __init__(self, name, log, fail, **values):
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_log", log)
        object.__setattr__(self, "_fail", fail)
        object.__setattr__(self, "_vals", dict(values))

    def __getattr__(self, attr):
        vals = object.__getattribute__(self, "_vals")
        if attr in vals:
            return vals[attr]
        raise AttributeError(attr)

    def __setattr__(self, attr, value):
        self._maybe_fail(attr)
        self._log.append((self._name, attr, value))
        self._vals[attr] = value

    def _maybe_fail(self, attr):
        key = (self._name, attr)
        if key in self._fail:
            self._fail.discard(key)
            raise IOError(f"injected failure at {key}")


class _SDR(_Rec):
    def tx(self, payload):
        self._maybe_fail("tx()")
        self._log.append((self._name, "tx()", len(payload)))
        self._vals["tx_payload"] = payload

    def tx_destroy_buffer(self):
        self._log.append((self._name, "tx_destroy_buffer()", None))

    def rx_destroy_buffer(self):
        self._log.append((self._name, "rx_destroy_buffer()", None))

    def _rx_init_channels(self):
        self._log.append((self._name, "_rx_init_channels()", None))

    def rx(self):
        self._maybe_fail("rx()")
        self._log.append((self._name, "rx()", None))
        n = int(self.rx_buffer_size)
        return [np.arange(n, dtype=complex), np.zeros(n, dtype=complex)]


class _Synth(_Rec):
    """Rounds freq_dev_time the way the chip does, so readback differs."""

    ROUND_US = 4

    def __setattr__(self, attr, value):
        super().__setattr__(attr, value)
        if attr == "freq_dev_time":
            self._vals[attr] = int(value) + self.ROUND_US


SDR_INITIAL = {
    "sample_rate": 30_000_000,
    "rx_rf_bandwidth": 10_000_000,
    "rx_buffer_size": 4096,
    "rx_lo": 2_200_000_000,
    "tx_lo": 2_200_000_000,
    "rx_hardwaregain_chan0": 30,
    "rx_hardwaregain_chan1": 30,
    "tx_hardwaregain_chan0": -10,
    "tx_hardwaregain_chan1": -10,
    "tx_cyclic_buffer": False,
    "gain_control_mode_chan0": "manual",
    "gain_control_mode_chan1": "manual",
    "tx_enabled_channels": [0, 1],
}
BF_LO = 12.2e9  # what the beamforming path expects the LO back at


class Kit:
    def __init__(self, ext_sync=False, synced=True):
        self.log = []
        self.fail = set()
        self.sdr = _SDR("sdr", self.log, self.fail, **SDR_INITIAL)
        self.synth = _Synth("synth", self.log, self.fail, freq_dev_time=1000,
                            frequency=int(BF_LO / 4), ramp_mode="disabled", enable=0)
        pin_vals = {"gpio_phaser_enable": False}
        labels = {"phaser_enable", "muxout"}
        if ext_sync:
            pin_vals["gpio_tdd_ext_sync"] = False
            labels.add("tdd_ext_sync")
        self.pins = _Rec("pins", self.log, self.fail, **pin_vals)
        self.tdd = _Rec("tdd", self.log, self.fail, enable=False,
                        channel=[_Rec(f"tdd.ch{i}", self.log, self.fail)
                                 for i in range(3)])
        self.gpios = _Rec("gpios", self.log, self.fail, gpio_burst=0)
        if synced:
            self.hw = fm.FmcwHardware(self.synth, self.tdd, self.pins, self.gpios, labels)
        else:
            self.hw = fm.FmcwHardware(self.synth, phaser_gpios=self.gpios)

    def writes(self, name):
        return [(a, v) for n, a, v in self.log if n == name]

    def enter(self, preset="range_doppler", **params):
        mode = fm.RAMP_SYNCED if self.hw.tdd is not None else fm.RAMP_FREE_RUNNING
        cfg = dsp.fmcw_config({"center_freq": CENTER, **params}, preset=preset)
        self.saved = {}
        return fm.enter_fmcw_mode(self.sdr, cfg, self.saved, self.hw, ramp_mode=mode)

    def exit(self):
        fm.exit_fmcw_mode(self.sdr, self.saved, self.hw, restore_lo_freq=BF_LO)


def _cfg(preset, **params):
    return dsp.fmcw_config({"center_freq": CENTER, **params}, preset=preset)


# --- presets and config ---------------------------------------------------

def test_every_preset_plans_cleanly_in_both_ramp_modes():
    for name in dsp.FMCW_PRESETS:
        cfg = _cfg(name)
        for mode in dsp.RAMP_MODES:
            plan = dsp.chirp_plan(cfg, mode, dsp.ramp_time_us(cfg))
            assert plan["rx_buffer_size"] <= dsp.MAX_BURST_BUFFER, name


def test_no_preset_carries_a_center_freq():
    """center_freq is the kit's characterized IF. A preset value would put a
    kit with a lower IF ceiling (phaser-00, 2.18 GHz) out of its passband."""
    for name, preset in dsp.FMCW_PRESETS.items():
        assert "center_freq" not in preset, name


def test_range_preset_follows_the_lab_not_the_script():
    """RADAR_FFT_Waterfall.py ramps for 1.2 ms; the lab says T_s = 1 ms and
    derives its 3.3 kHz/m from it. The lab wins."""
    assert dsp.FMCW_PRESETS["range"]["ramp_time"] == pytest.approx(1e-3)


def test_unknown_preset_is_rejected():
    with pytest.raises(ValueError, match="unknown FMCW preset"):
        dsp.fmcw_config(preset="nope")


def test_params_override_the_preset():
    cfg = dsp.fmcw_config({"num_chirps": 32}, preset="range_doppler")
    assert cfg["num_chirps"] == 32 and cfg["sample_rate"] == 4_000_000


# --- ADF4159 registers ----------------------------------------------------

@pytest.mark.parametrize("mode", dsp.RAMP_MODES)
def test_enable_is_the_last_register_written(mode):
    regs = dsp.ramp_registers(_cfg("range_doppler"), mode)
    assert regs[-1] == ("enable", 0)
    assert [a for a, _ in regs].count("enable") == 1


def test_frequencies_are_divided_by_four():
    cfg = _cfg("range_doppler")
    regs = dict(dsp.ramp_registers(cfg, dsp.RAMP_SYNCED))
    assert regs["frequency"] == int((9.9e9 + 100e3 + CENTER) / 4)
    assert regs["freq_dev_range"] == int(500e6 / 4)


def test_steps_cover_the_deviation_range_at_one_per_microsecond():
    regs = dict(dsp.ramp_registers(_cfg("range_doppler"), dsp.RAMP_SYNCED))
    assert regs["freq_dev_time"] == 300
    steps = regs["freq_dev_time"]
    assert abs(regs["freq_dev_step"] * steps - regs["freq_dev_range"]) < steps


def test_only_the_synced_ramp_waits_for_tx_data():
    cfg = _cfg("range")
    assert dict(dsp.ramp_registers(cfg, dsp.RAMP_SYNCED))["tx_trig_en"] == 1
    assert dict(dsp.ramp_registers(cfg, dsp.RAMP_FREE_RUNNING))["tx_trig_en"] == 0


def test_bad_ramp_mode_and_ramp_time_are_rejected():
    with pytest.raises(ValueError, match="ramp mode"):
        dsp.ramp_registers(_cfg("range"), "continuous_sawtooth")
    with pytest.raises(ValueError, match="under 1 us"):
        dsp.ramp_registers(_cfg("range", ramp_time=1e-8), dsp.RAMP_SYNCED)


# --- chirp plan -----------------------------------------------------------

def _script_plan(sample_rate, ramp_time, guard_ms, num_chirps):
    """Range_Doppler_Plot.py's arithmetic, transcribed line for line.

    `ramp_time` is the int(freq_dev_time) the script reads back, in us.
    """
    PRI_ms = ramp_time / 1e3 + guard_ms
    ramp_time_s = ramp_time / 1e6
    begin_offset_time = 0.1 * ramp_time_s
    good_ramp_samples = int((ramp_time_s - begin_offset_time) * sample_rate)
    start_offset_samples = int((0 / 1e3 + begin_offset_time) * sample_rate)
    total_time = PRI_ms * num_chirps
    buffer_time = 0
    power = 12
    while total_time > buffer_time:
        power = power + 1
        buffer_size = int(2 ** power)
        buffer_time = buffer_size / sample_rate * 1000
    N_frame = int(PRI_ms / 1e3 * float(sample_rate))
    return {
        "pri_ms": PRI_ms,
        "frame_samples": N_frame,
        "good_ramp_samples": good_ramp_samples,
        "start_offset_samples": start_offset_samples,
        "rx_buffer_size": buffer_size,
    }


def test_range_doppler_plan_matches_the_script_and_the_lab():
    """The lab (p. 36) gives 4 MHz, 300 us, 256 chirps, PRI 0.5 ms.

    1079 and 119, not the 1080 and 120 hand arithmetic gives: the script's
    int() truncates 1079.9999..., and matching the script is the point.
    """
    plan = dsp.chirp_plan(_cfg("range_doppler"), dsp.RAMP_SYNCED, 300)
    assert plan["pri_ms"] == pytest.approx(0.5)
    assert plan["frame_samples"] == 2000
    assert plan["good_ramp_samples"] == 1079
    assert plan["start_offset_samples"] == 119
    assert plan["rx_buffer_size"] == 2 ** 19
    assert plan["num_chirps"] == 256


@pytest.mark.parametrize("name", sorted(dsp.FMCW_PRESETS))
def test_every_synced_plan_matches_the_script_arithmetic(name):
    cfg = _cfg(name)
    ramp_us = dsp.ramp_time_us(cfg)
    plan = dsp.chirp_plan(cfg, dsp.RAMP_SYNCED, ramp_us)
    want = _script_plan(cfg["sample_rate"], ramp_us, cfg["pri_guard_ms"], cfg["num_chirps"])
    for key, value in want.items():
        assert plan[key] == pytest.approx(value), key


def test_plan_uses_the_ramp_time_it_is_given():
    """The read-back time drives PRI and range, not the requested one."""
    cfg = _cfg("range_doppler")
    plan = dsp.chirp_plan(cfg, dsp.RAMP_SYNCED, 304)
    assert plan["ramp_time"] == pytest.approx(304e-6)
    assert plan["pri_ms"] == pytest.approx(0.504)


def test_triangle_ranges_on_half_its_ramp_time():
    cfg = _cfg("range")
    saw = dsp.chirp_plan(cfg, dsp.RAMP_SYNCED, 1000)
    tri = dsp.chirp_plan(cfg, dsp.RAMP_FREE_RUNNING, 1000)
    assert saw["effective_ramp_time"] == pytest.approx(1e-3)
    assert tri["effective_ramp_time"] == pytest.approx(0.5e-3)
    # ...which is RADAR_FFT_Waterfall.py's c/(4*S) on the full ramp time.
    f_b = 6.67e3
    s = 500e6 / 1e-3
    assert dsp.beat_freq_to_range(f_b, 500e6, tri["effective_ramp_time"]) == \
        pytest.approx(C * f_b / (4 * s))


def test_free_running_plan_is_one_unsliced_buffer():
    plan = dsp.chirp_plan(_cfg("range"), dsp.RAMP_FREE_RUNNING, 1000)
    assert plan["synced"] is False
    assert plan["rx_buffer_size"] == 4096 and plan["num_chirps"] == 1


def test_a_burst_too_big_for_the_buffer_raises():
    """The script stops growing the buffer at its cap and slices past the end.
    That is a silent garbage frame; this is an error that names the fix."""
    with pytest.raises(ValueError, match="fewer chirps"):
        dsp.chirp_plan(_cfg("range_doppler", num_chirps=8192), dsp.RAMP_SYNCED, 300)


def test_a_chirp_window_past_its_pri_slot_raises():
    with pytest.raises(ValueError, match="pri_guard_ms"):
        dsp.chirp_plan(_cfg("range_doppler", pri_guard_ms=0.0), dsp.RAMP_SYNCED, 300,
                       on_ms=0.1)
    with pytest.raises(ValueError, match="negative"):
        dsp.chirp_plan(_cfg("range_doppler", pri_guard_ms=-1), dsp.RAMP_SYNCED, 300)


# --- slicing --------------------------------------------------------------

def test_slice_takes_each_chirps_settled_window_in_order():
    plan = dsp.chirp_plan(_cfg("range_doppler"), dsp.RAMP_SYNCED, 300)
    iq = np.arange(plan["rx_buffer_size"])
    m = dsp.slice_chirps(iq, plan)
    assert m.shape == (plan["good_ramp_samples"], plan["num_chirps"])
    for k in (0, 1, 255):
        start = plan["start_offset_samples"] + k * plan["frame_samples"]
        np.testing.assert_array_equal(m[:, k], np.arange(start, start + plan["good_ramp_samples"]))


def test_slice_rejects_a_short_capture():
    plan = dsp.chirp_plan(_cfg("range_doppler"), dsp.RAMP_SYNCED, 300)
    with pytest.raises(ValueError, match="shorter"):
        dsp.slice_chirps(np.zeros(plan["frame_samples"] * 10), plan)


def test_slice_passes_a_free_running_capture_through():
    plan = dsp.chirp_plan(_cfg("range"), dsp.RAMP_FREE_RUNNING, 1000)
    iq = np.arange(4096)
    assert dsp.slice_chirps(iq, plan) is not None
    np.testing.assert_array_equal(dsp.slice_chirps(iq, plan), iq)


@pytest.mark.parametrize("target_range", [3.0, 8.0])
def test_sliced_synced_capture_ranges_a_target(target_range):
    """Beat tones laid out in PRI slots, the way the hardware delivers them,
    come back at the right range once sliced -- through the same
    range_profile the mode will call."""
    cfg = _cfg("range_sync")
    plan = dsp.chirp_plan(cfg, dsp.RAMP_SYNCED, dsp.ramp_time_us(cfg))
    fs = plan["sample_rate"]
    f = cfg["signal_freq"] + dsp.range_to_beat_freq(target_range, cfg["chirp_bw"],
                                                    plan["effective_ramp_time"])
    iq = np.zeros(plan["rx_buffer_size"], dtype=complex)
    ramp_n = int(plan["ramp_time"] * fs)
    t = np.arange(ramp_n) / fs
    for k in range(plan["num_chirps"]):
        s = k * plan["frame_samples"]
        iq[s:s + ramp_n] = np.exp(2j * np.pi * f * t) * 2 ** 11
    chirps = dsp.slice_chirps(iq, plan)
    prof = dsp.range_profile(chirps[:, 0], fs, cfg["chirp_bw"], plan["effective_ramp_time"],
                             zero_range_freq=cfg["signal_freq"], range_max=20)
    assert prof["peak_range"] == pytest.approx(target_range, abs=0.5)


# --- enter: write order ---------------------------------------------------

def test_enter_writes_the_ramp_with_enable_last():
    kit = Kit()
    kit.enter()
    synth = kit.writes("synth")
    assert synth[-1] == ("enable", 0)
    assert [a for a, _ in synth] == [a for a, _ in dsp.ramp_registers(
        _cfg("range_doppler"), dsp.RAMP_SYNCED)]


def test_enter_brackets_the_tdd_config_with_enable():
    kit = Kit()
    kit.enter()
    tdd = kit.writes("tdd")
    assert tdd[0] == ("enable", False)
    assert tdd[-1] == ("enable", True)
    assert ("sync_external", True) in tdd
    for i in range(3):
        assert set(kit.writes(f"tdd.ch{i}")) == {
            ("enable", True), ("polarity", False), ("on_raw", 0), ("off_raw", 10)}


def test_tdd_is_programmed_from_the_read_back_ramp():
    kit = Kit()
    _, plan = kit.enter()
    assert plan["ramp_time"] == pytest.approx((300 + _Synth.ROUND_US) / 1e6)
    assert ("frame_length_ms", pytest.approx(plan["pri_ms"])) in kit.writes("tdd")
    assert plan["pri_ms"] == pytest.approx(0.504)
    assert ("burst_count", 256) in kit.writes("tdd")


def test_tdd_ext_sync_is_never_written_when_absent():
    """Pluto v0.39 has no such channel. Writing it there sets a plain Python
    attribute and does nothing; the mode must not pretend otherwise."""
    kit = Kit(ext_sync=False)
    kit.enter()
    assert all(a != "gpio_tdd_ext_sync" for a, _ in kit.writes("pins"))
    assert ("gpio_phaser_enable", True) in kit.writes("pins")


def test_tdd_ext_sync_is_set_and_restored_when_present():
    kit = Kit(ext_sync=True)
    kit.enter()
    assert ("gpio_tdd_ext_sync", True) in kit.writes("pins")
    kit.exit()
    assert kit.pins.gpio_tdd_ext_sync is False


def test_enter_sets_the_buffer_and_starts_a_cyclic_tone():
    kit = Kit()
    _, plan = kit.enter()
    assert kit.sdr.rx_buffer_size == plan["rx_buffer_size"]
    assert kit.sdr.tx_cyclic_buffer is True
    assert kit.sdr.tx_hardwaregain_chan0 == -88
    assert kit.sdr.rx_lo == int(CENTER) and kit.sdr.tx_lo == int(CENTER)
    assert ("tx()", 2) in kit.writes("sdr")


def test_enter_discards_one_triggered_frame():
    kit = Kit()
    kit.enter()
    sdr_ops = [a for a, _ in kit.writes("sdr")]
    assert sdr_ops.count("rx()") == 1
    assert sdr_ops.index("tx()") < sdr_ops.index("rx()")
    assert kit.writes("gpios")[-3:] == [("gpio_burst", 0), ("gpio_burst", 1), ("gpio_burst", 0)]


def test_capture_triggers_and_returns_sliced_chirps():
    kit = Kit()
    _, plan = kit.enter()
    kit.log.clear()
    m = fm.capture_fmcw_frame(kit.sdr, kit.hw, plan)
    assert [v for _, v in kit.writes("gpios")] == [0, 1, 0]
    assert m.shape == (plan["good_ramp_samples"], plan["num_chirps"])
    assert m[0, 0] == plan["start_offset_samples"]


def test_free_running_enter_touches_no_tdd_and_does_not_slice():
    kit = Kit(synced=False)
    cfg, plan = kit.enter(preset="range")
    assert kit.writes("tdd") == [] and kit.writes("pins") == []
    assert dict(kit.writes("synth"))["ramp_mode"] == dsp.RAMP_FREE_RUNNING
    out = fm.capture_fmcw_frame(kit.sdr, kit.hw, plan)
    assert out.shape == (4096,)


def test_synced_enter_refuses_without_tdd_handles():
    kit = Kit(synced=False)
    cfg = _cfg("range_doppler")
    with pytest.raises(RuntimeError, match="TDD"):
        fm.enter_fmcw_mode(kit.sdr, cfg, {}, kit.hw, ramp_mode=fm.RAMP_SYNCED)
    assert kit.log == []


def test_enter_applies_the_taper_and_boresight_when_given_an_array(monkeypatch):
    seen = []
    monkeypatch.setattr(phaser_cw_radar, "apply_taper", lambda arr, t: seen.append(t))
    monkeypatch.setattr(phaser_cw_radar, "apply_boresight",
                        lambda arr, pcal: seen.append(("boresight", pcal)))
    kit = Kit()
    cfg = dsp.fmcw_config({"center_freq": CENTER, "taper": "hann"}, preset="range_doppler")
    saved = {}
    fm.enter_fmcw_mode(kit.sdr, cfg, saved, kit.hw, array=object(), phase_cal=[5.0] * 8)
    fm.exit_fmcw_mode(kit.sdr, saved, kit.hw, array=object(), restore_taper=[1] * 8)
    # Boresight is not restored on exit: the sweep rewrites every phase on
    # every angle, and boresight is a sane state for anything else.
    assert seen == ["hann", ("boresight", [5.0] * 8), [1] * 8]


# --- exit and the unwind ---------------------------------------------------

def _assert_restored(kit):
    for key in ("sample_rate", "rx_rf_bandwidth", "rx_buffer_size", "rx_lo", "tx_lo",
                "rx_hardwaregain_chan0", "rx_hardwaregain_chan1",
                "tx_hardwaregain_chan0", "tx_hardwaregain_chan1", "tx_cyclic_buffer"):
        assert getattr(kit.sdr, key) == SDR_INITIAL[key], key
    assert kit.tdd.enable is False
    assert kit.pins.gpio_phaser_enable is False
    assert kit.synth.ramp_mode in ("disabled",)
    assert kit.synth.frequency == int(BF_LO / 4)
    assert kit.hw.synth is None and kit.hw.tdd is None and kit.hw.pluto_pins is None


def test_enter_then_exit_puts_everything_back():
    kit = Kit()
    kit.enter()
    kit.exit()
    _assert_restored(kit)


def test_exit_turns_the_tdd_off_before_anything_else():
    kit = Kit()
    kit.enter()
    kit.log.clear()
    kit.exit()
    assert kit.log[0] == ("tdd", "enable", False)


def test_exit_latches_the_ramp_off_with_enable_last():
    kit = Kit()
    kit.enter()
    kit.log.clear()
    kit.exit()
    synth = kit.writes("synth")
    assert ("ramp_mode", "disabled") in synth and ("tx_trig_en", 0) in synth
    assert synth[-1] == ("enable", 0)


def test_exit_twice_is_harmless():
    kit = Kit()
    kit.enter()
    kit.exit()
    kit.log.clear()
    fm.exit_fmcw_mode(kit.sdr, kit.saved, kit.hw, restore_lo_freq=BF_LO)
    assert kit.log == []


FAIL_POINTS = [
    ("sdr", "sample_rate"),
    ("sdr", "tx_cyclic_buffer"),
    ("synth", "frequency"),
    ("synth", "enable"),
    ("pins", "gpio_phaser_enable"),
    ("tdd", "sync_external"),
    ("tdd", "burst_count"),
    ("sdr", "rx_buffer_size"),
    ("sdr", "tx()"),
    ("sdr", "rx()"),
]


@pytest.mark.parametrize("point", FAIL_POINTS, ids=lambda p: f"{p[0]}.{p[1]}")
def test_a_failure_anywhere_in_enter_unwinds_completely(point):
    kit = Kit()
    kit.fail.add(point)
    with pytest.raises(IOError, match="injected"):
        kit.enter()
    kit.exit()
    _assert_restored(kit)


def test_exit_survives_a_failing_step_and_does_the_rest():
    kit = Kit()
    kit.enter()
    kit.fail.add(("tdd", "enable"))
    kit.exit()
    # The TDD write failed; the ramp, the GPIOs and the SDR were still undone.
    assert kit.synth.ramp_mode == "disabled"
    assert kit.pins.gpio_phaser_enable is False
    assert kit.sdr.sample_rate == SDR_INITIAL["sample_rate"]


def test_open_uses_the_injected_adi_and_reads_pluto_labels():
    opened = []

    class _Pins:
        _ctx = object()

    fake_adi = types.SimpleNamespace(
        adf4159=lambda uri: opened.append(("adf4159", uri)) or "synth",
        tddn=lambda uri: opened.append(("tddn", uri)) or "tdd",
        one_bit_adc_dac=lambda uri: opened.append(("gpio", uri)) or _Pins(),
    )
    orig = fm._gpio_labels
    fm._gpio_labels = lambda ctx: {"phaser_enable"}
    try:
        hw = fm.FmcwHardware.open(fake_adi, "ip:rpi", "ip:pluto", "gpios")
    finally:
        fm._gpio_labels = orig
    assert opened == [("adf4159", "ip:rpi"), ("gpio", "ip:pluto"), ("tddn", "ip:pluto")]
    assert hw.has_pluto_gpio("phaser_enable") and not hw.has_pluto_gpio("tdd_ext_sync")

    opened.clear()
    hw = fm.FmcwHardware.open(fake_adi, "ip:rpi", "ip:pluto", "gpios", synced=False)
    assert opened == [("adf4159", "ip:rpi")] and hw.tdd is None


def test_sdr_snapshot_records_tx_cyclic_buffer_for_both_modes():
    kit = Kit()
    snap = phaser_cw_radar.snapshot_sdr(kit.sdr, {})
    assert snap["tx_cyclic_buffer"] is False
    assert snap["sample_rate"] == SDR_INITIAL["sample_rate"]


# --- chirp-to-chirp coherence -------------------------------------------------
#
# The Tx tone has to advance a whole number of cycles per PRI, or every
# stationary return walks in phase chirp to chirp: a constant Doppler that
# moved clutter two bins up and through the MTI on phaser-01 (+1.9 deg a
# chirp, 55% of the leakage at zero Doppler against upstream's 99%).

@pytest.mark.parametrize("name", sorted(dsp.FMCW_PRESETS))
def test_every_preset_puts_a_whole_number_of_tone_cycles_in_a_pri(name):
    cfg = _cfg(name)
    plan = dsp.chirp_plan(cfg, fm.RAMP_SYNCED, dsp.ramp_time_us(cfg))
    assert plan["tone_cycles_per_pri"] == pytest.approx(round(plan["tone_cycles_per_pri"]))
    assert abs(plan["chirp_phase_step_deg"]) < 1e-6


def test_the_plan_reports_the_step_a_bin_snapped_tone_had():
    cfg = _cfg("range_doppler", signal_freq=round(1e5 / 4e6 * 2 ** 18) * 4e6 / 2 ** 18)
    plan = dsp.chirp_plan(cfg, fm.RAMP_SYNCED, dsp.ramp_time_us(cfg))
    assert plan["chirp_phase_step_deg"] > 1.0


@pytest.mark.parametrize("fs,f,period", [(4e6, 1e5, 40), (5e6, 1e5, 200), (2e6, 1e5, 40),
                                         (600e3, 1e5, 24)])
def test_coherent_tone_length_is_whole_periods_near_the_target(fs, f, period):
    n = dsp.coherent_tone_length(fs, f, 2 ** 18)
    assert n % period == 0 and n % 8 == 0
    assert abs(n - 2 ** 18) <= period / 2
    # So build_iq_tone's snap is a no-op: the bin is the tone.
    assert round(f / fs * n) * fs / n == f


def test_coherent_tone_length_gives_up_on_a_tone_with_no_short_period():
    assert dsp.coherent_tone_length(4e6, 100_000.123457, 2 ** 18) is None


def test_enter_transmits_the_exact_tone_and_it_wraps_without_a_step():
    kit = Kit()
    cfg, plan = kit.enter()
    iq = np.asarray(kit.sdr.tx_payload[0])
    fs, f = float(cfg["sample_rate"]), float(cfg["signal_freq"])
    assert len(iq) == dsp.coherent_tone_length(fs, f, fm.TX_TONE_SAMPLES)
    peak = np.argmax(np.abs(np.fft.fft(iq)))
    assert peak * fs / len(iq) == f
    # The sample after the last one is the first one again.
    nxt = iq[-1] * np.exp(2j * np.pi * f / fs)
    assert abs(nxt - iq[0]) < 1e-6 * abs(iq[0])


# --- the Rx buffer is rebuilt at the new size ----------------------------------
#
# pyadi only reads rx_buffer_size when it creates the buffer. Without a
# destroy, range_sync went on reading range_doppler's 2**19 samples, and the
# sweep after a radar mode read the radar's.

def _destroy_then_resize(log):
    sdr = [(a, v) for n, a, v in log if n == "sdr"]
    sizes = [i for i, (a, _) in enumerate(sdr) if a == "rx_buffer_size"]
    assert sizes
    for i in sizes:
        assert sdr[i - 1][0] == "rx_destroy_buffer()", sdr[max(0, i - 3): i + 1]


def test_enter_destroys_the_rx_buffer_before_resizing_it():
    kit = Kit()
    kit.enter()
    _destroy_then_resize(kit.log)


def test_exit_destroys_the_rx_buffer_before_restoring_its_size():
    kit = Kit()
    kit.enter()
    kit.log.clear()
    kit.exit()
    _destroy_then_resize(kit.log)


def test_resize_works_on_an_sdr_with_no_destroy():
    sdr = _Rec("sdr", [], set(), rx_buffer_size=1)
    phaser_cw_radar.set_rx_buffer_size(sdr, 4096)
    assert sdr.rx_buffer_size == 4096


def test_resize_survives_a_failing_destroy():
    class Sdr(_Rec):
        def rx_destroy_buffer(self):
            raise OSError("no buffer yet")

    sdr = Sdr("sdr", [], set(), rx_buffer_size=1)
    phaser_cw_radar.set_rx_buffer_size(sdr, 4096)
    assert sdr.rx_buffer_size == 4096


# --- the Rx buffer is armed before the first burst ------------------------------

def test_synced_enter_arms_the_rx_buffer_before_the_first_burst():
    kit = Kit()
    kit.enter()
    sdr = [a for n, a, _ in kit.log if n == "sdr"]
    burst = next(i for i, (n, a, v) in enumerate(kit.log)
                 if n == "gpios" and a == "gpio_burst" and v == 1)
    armed = next(i for i, (n, a, _) in enumerate(kit.log) if a == "_rx_init_channels()")
    resized = max(i for i, (n, a, _) in enumerate(kit.log) if n == "sdr" and a == "rx_buffer_size")
    assert resized < armed < burst
    assert sdr.count("_rx_init_channels()") == 1


def test_free_running_enter_does_not_arm():
    kit = Kit(synced=False)
    kit.enter()
    assert all(a != "_rx_init_channels()" for _, a, _ in kit.log)


# --- the TDD is released on exit (calibration-bugs.md, bug 6) -------------------
#
# With the engine off, each TDD channel idles at its polarity, and channel 1
# low holds the Rx DMA. Disabling the engine alone left every new Rx buffer
# unable to fill, on the Pluto, across restarts.

def _tdd_writes(log):
    return [(n, a, v) for n, a, v in log if n.startswith("tdd")]


def test_release_tdd_idles_channel_1_high_and_latches_with_a_toggle():
    log = []
    tdd = _Rec("tdd", log, set(), enable=True,
               channel=[_Rec(f"tdd.ch{i}", log, set()) for i in range(3)])
    fm.release_tdd(tdd)
    assert log == [
        ("tdd", "enable", False),
        ("tdd.ch1", "polarity", True),
        ("tdd.ch2", "polarity", False),
        ("tdd", "enable", True),
        ("tdd", "enable", False),
    ]


def test_exit_releases_the_tdd_after_enter_set_every_polarity_low():
    kit = Kit()
    kit.enter()
    assert all(c.polarity is False for c in kit.tdd.channel)
    kit.log.clear()
    kit.exit()
    assert kit.tdd.channel[1].polarity is True
    assert kit.tdd.channel[2].polarity is False
    tdd = _tdd_writes(kit.log)
    assert tdd[0] == ("tdd", "enable", False)
    assert tdd[-1] == ("tdd", "enable", False) and tdd[-2] == ("tdd", "enable", True)


def test_free_running_exit_does_not_touch_a_tdd():
    kit = Kit(synced=False)
    kit.enter()
    kit.exit()
    assert _tdd_writes(kit.log) == []


# --- CW never waits forever -----------------------------------------------------

class _Ctx:
    def __init__(self):
        self.timeouts = []

    def set_timeout(self, ms):
        self.timeouts.append(ms)


def test_cw_sets_a_finite_libiio_timeout():
    log = []
    sdr = _SDR("sdr", log, set(), **SDR_INITIAL)
    sdr._vals["_ctx"] = ctx = _Ctx()
    phaser_cw_radar.enter_cw_mode(sdr, {}, saved_state={})
    assert ctx.timeouts and all(t > 0 for t in ctx.timeouts)
    assert ctx.timeouts[-1] == phaser_cw_radar.RX_TIMEOUT_MS
