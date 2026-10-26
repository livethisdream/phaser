"""
FMCW radar helpers for the Phaser headless backend.

Phase C0: the capability probe only. The ramp, the chirp-synced capture and
the mode itself follow (docs/fmcw-phase-c-plan.md).

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
# is the fallback when the sync chain is missing.
RAMP_SYNCED = "single_sawtooth_burst"
RAMP_FREE_RUNNING = "continuous_triangular"


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
