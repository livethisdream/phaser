"""The FMCW capability probe (phaser_fmcw_radar.probe_fmcw).

Every missing piece it looks for fails silently on the kit -- the ramp never
starts or the capture is never triggered, and the plot just shows noise -- so
each one gets its own case here, asserting both the verdict and that the
reason names the actual culprit.

The fakes model libiio's shape (ctx.attrs, ctx.devices, find_device,
find_channel, attrs[...].value) and make every attribute read-only, so a probe
that ever writes to the hardware fails these tests rather than a lab.
"""

import contextlib
import io
import sys
import types

import pytest

import phaser_fmcw_radar as fm


class _Attr:
    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        return self._value
    # No setter: a write raises AttributeError.


class _Chan:
    def __init__(self, id, attrs, output=True):
        self.id = id
        self.output = output
        self.attrs = {k: _Attr(v) for k, v in attrs.items()}


class _Dev:
    def __init__(self, name, channels=(), attrs=None):
        self.name = name
        self.channels = list(channels)
        self.attrs = {k: _Attr(v) for k, v in (attrs or {}).items()}

    def find_channel(self, id, output=False):
        for c in self.channels:
            if c.id == id and c.output == output:
                return c
        return None


class _Ctx:
    def __init__(self, devices, attrs=None):
        self.devices = list(devices)
        self.attrs = {k: _Attr(v) for k, v in (attrs or {}).items()}

    def find_device(self, name):
        for d in self.devices:
            if d.name == name:
                return d
        return None


def _gpio_dev(labels, labelled_device=True):
    chans = [_Chan(f"voltage{i}", {"label": lab}) for i, lab in enumerate(labels)]
    attrs = {"label": fm.GPIO_DEVICE} if labelled_device else None
    name = "gpio-thing" if labelled_device else fm.GPIO_DEVICE
    return _Dev(name, chans, attrs)


RAMP_ATTRS = {
    "frequency": "3000000000",
    "frequency_deviation_range": "125000000",
    "frequency_deviation_step": "125000",
    "frequency_deviation_time": "1000",
    "ramp_mode": "disabled",
    "ramp_mode_available": ("disabled continuous_sawtooth continuous_triangular "
                            "single_sawtooth_burst single_ramp_burst"),
}


def _kit(fw="v0.39", tdd=True, pluto_gpios=("TDD_EXT_SYNC", "PHASER_ENABLE"),
         burst=True, adf4159=True, ramp_attrs=None, labelled_device=True):
    pluto = [_Dev("ad9361-phy"), _gpio_dev(pluto_gpios, labelled_device)]
    if tdd:
        pluto.append(_Dev(fm.TDD_DEVICE))
    sdr_ctx = _Ctx(pluto, {"fw_version": fw} if fw is not None else {})

    rpi_labels = ["vctrl_1", "vctrl_2", "tx_sw"] + (["BURST"] if burst else [])
    rpi = [_gpio_dev(rpi_labels, labelled_device)]
    if adf4159:
        attrs = RAMP_ATTRS if ramp_attrs is None else ramp_attrs
        rpi.append(_Dev(fm.ADF4159_DEVICE, [_Chan(fm.ADF4159_CHANNEL, attrs)]))
    return sdr_ctx, _Ctx(rpi)


ADI_NEW = types.SimpleNamespace(tddn=object)
ADI_OLD = types.SimpleNamespace()


def _probe(adi=ADI_NEW, **kit):
    sdr_ctx, rpi_ctx = _kit(**kit)
    return fm.probe_fmcw(adi, sdr_ctx, rpi_ctx)


def _check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


def test_healthy_kit_gets_the_synced_ramp():
    r = _probe()
    assert r["available"] is True
    assert r["ramp"] == fm.RAMP_SYNCED
    assert r["source"] == "hardware"
    assert r["reason"] == ""
    assert all(c["ok"] for c in r["checks"]), r["checks"]


def test_gpio_device_found_by_name_as_well_as_label():
    """pyadi-iio matches the GPIO device by label, then by name; so must we."""
    assert _probe(labelled_device=False)["ramp"] == fm.RAMP_SYNCED


@pytest.mark.parametrize("text, expected", [
    ("v0.39", (0, 39)),
    ("v0.39-dirty", (0, 39)),
    ("0.38", (0, 38)),
    ("v1.2-rc1", (1, 2)),
    ("", None),
    (None, None),
    ("unknown", None),
])
def test_parse_fw_version(text, expected):
    assert fm.parse_fw_version(text) == expected


def test_newer_firmware_passes():
    assert _probe(fw="v0.40")["ramp"] == fm.RAMP_SYNCED


def test_old_firmware_falls_back_to_the_range_lab():
    r = _probe(fw="v0.38")
    assert r["available"] is True
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert "v0.38" in r["reason"] and "v0.39" in r["reason"]
    assert "range lab only" in r["reason"]


def test_unreadable_firmware_version_is_a_failed_check_not_a_crash():
    r = _probe(fw=None)
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert _check(r, "pluto_firmware")["ok"] is False
    assert "could not check" in _check(r, "pluto_firmware")["detail"]


def test_old_pyadi_is_named():
    r = _probe(adi=ADI_OLD)
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert "adi.tddn" in r["reason"]


def test_missing_tdd_core_is_named():
    r = _probe(tdd=False)
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert fm.TDD_DEVICE in r["reason"]


def test_missing_pluto_sync_gpio_is_named():
    r = _probe(pluto_gpios=("TDD_EXT_SYNC",))
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert "phaser_enable" in r["reason"]


def test_tdd_ext_sync_is_optional():
    """What a real v0.39 Pluto lists: phaser_enable and muxout, no
    tdd_ext_sync -- and chirp sync works on it. It must not cost the kit
    range-Doppler."""
    r = _probe(pluto_gpios=("muxout", "phaser_enable"))
    assert r["ramp"] == fm.RAMP_SYNCED
    assert r["reason"] == ""
    assert "tdd_ext_sync" not in _check(r, "pluto_sync_gpios")["detail"]


def test_tdd_ext_sync_is_reported_when_present():
    r = _probe()
    assert "tdd_ext_sync" in _check(r, "pluto_sync_gpios")["detail"]


def test_missing_phaser_burst_gpio_is_named():
    r = _probe(burst=False)
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert "burst" in r["reason"]


def test_no_adf4159_means_no_fmcw_at_all():
    r = _probe(adf4159=False)
    assert r["available"] is False
    assert r["ramp"] is None
    assert "adf4159" in r["reason"]
    assert "range lab only" not in r["reason"]


def test_adf4159_without_ramp_attributes_means_no_fmcw():
    attrs = {k: v for k, v in RAMP_ATTRS.items()
             if not k.startswith("frequency_deviation")}
    r = _probe(ramp_attrs=attrs)
    assert r["available"] is False
    assert "frequency_deviation_range" in r["reason"]


def test_driver_without_the_burst_ramp_falls_back():
    attrs = dict(RAMP_ATTRS, ramp_mode_available="disabled continuous_triangular")
    r = _probe(ramp_attrs=attrs)
    assert r["ramp"] == fm.RAMP_FREE_RUNNING
    assert fm.RAMP_SYNCED in r["reason"]


def test_failure_reason_is_the_first_failing_check():
    r = _probe(adi=ADI_OLD, fw="v0.30")
    assert "adi.tddn" in r["reason"]
    assert "v0.30" in _check(r, "pluto_firmware")["detail"]


def test_no_contexts_never_raises():
    """A backend whose SDR or GPIO object has no _ctx must still start."""
    r = fm.probe_fmcw(ADI_NEW, None, None)
    assert r["available"] is False
    assert r["ramp"] is None
    assert len(r["checks"]) == 6


def test_sim_is_always_available():
    r = fm.probe_fmcw(ADI_OLD, None, None, sim=True)
    assert r == {"available": True, "ramp": fm.RAMP_SYNCED, "source": "sim",
                 "reason": "", "checks": []}


# --- backend wiring ----------------------------------------------------------

def _headless():
    sys.modules.setdefault("adi", types.ModuleType("adi"))
    try:
        import phaser_headless
    except Exception as exc:  # noqa: BLE001 - zmq/msgpack absent
        pytest.skip(f"phaser_headless unavailable: {exc}")
    with contextlib.redirect_stdout(io.StringIO()):
        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
    return phaser_headless, h


def test_backend_probes_at_startup_and_reports_it():
    _, h = _headless()
    caps = h.handle_command({"cmd": "get_radar_capabilities"})
    assert caps["status"] == "ok"
    assert caps["data"]["fmcw"]["available"] is True
    assert caps["data"]["fmcw"]["source"] == "sim"
    # get_state reads run state that __init__ sets and this partial build skips.
    h.sweeping = False
    h.shutdown_permitted = lambda: False
    assert h.get_state()["data"]["fmcw"] == caps["data"]["fmcw"]


def test_capabilities_before_the_probe_say_so():
    phaser_headless, _ = _headless()
    bare = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
    fmcw = bare.get_radar_capabilities()["data"]["fmcw"]
    assert fmcw["available"] is False
    assert fmcw["reason"] == "not probed yet"
