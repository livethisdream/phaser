#!/usr/bin/env python3
"""Generate frontend/src/sim/constants.generated.js from the Python sim.

The browser simulator (frontend/src/sim/) is a JavaScript port of phaser_sim.py
and PhaserHeadless.do_sweep(). Python is the source of truth. Every number both
sides must agree on is emitted from here rather than typed twice, so the whole
class of "someone changed a constant in one language" cannot happen.

Run after changing any sim constant:

    python tools/gen_sim_constants.py

CI regenerates and fails on `git diff --exit-code`, so a Python-side change that
is not reflected in the committed JS breaks the build.

Behavioural drift -- an algorithm change rather than a constant change -- is not
caught here. That is what tests/test_sim_parity.py is for.
"""

import contextlib
import io
import pathlib
import re
import sys
import types

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import config                      # noqa: E402
import phaser_sim                  # noqa: E402

OUT = REPO / "frontend" / "src" / "sim" / "constants.generated.js"


def steering_pi():
    """Extract the truncated pi that the steering math uses.

    do_sweep() and ConvertPhaseToSteerAngle()/ConvertSteerAngleToPhase() spell
    pi as the literal 3.14159 rather than math.pi, deliberately matching the
    legacy phaser_gui.py they were ported from. A JS port reaching for Math.PI
    instead would shift every steering angle by ~0.03% and produce a parity
    failure that looks like a physics bug rather than a typo.

    Note this is NOT the pi used by phaser_sim's own wave synthesis, which uses
    real np.pi. The two coexist on purpose; see SIM_PI below.

    Parsing the literal rather than hardcoding it here makes this a tripwire: if
    someone changes one of the three occurrences and not the others, this raises
    instead of silently emitting a value that matches only part of the Python.
    """
    src = (REPO / "phaser_headless.py").read_text()
    found = re.findall(r"2 \* (3\.14159\d*)", src)
    if not found:
        raise SystemExit(
            "phaser_headless.py: no `2 * 3.14159...` steering literal found. "
            "If the steering math moved to math.pi, update SIM/STEER_PI here "
            "and in frontend/src/sim/ together."
        )
    if len(set(found)) != 1:
        raise SystemExit(
            f"phaser_headless.py: inconsistent steering pi literals {sorted(set(found))}. "
            "All occurrences must agree, or the sweep's angle axis disagrees "
            "with the phases actually loaded."
        )
    if len(found) != 3:
        raise SystemExit(
            f"phaser_headless.py: expected 3 steering pi literals, found {len(found)}. "
            "Check whether a new call site needs porting to frontend/src/sim/."
        )
    return found[0]


def headless_defaults():
    """Read the sweep defaults straight off a PhaserHeadless instance.

    Transcribing `self.mvdr_K = 128` and friends into this file by hand would
    just move the drift problem one level up. Instead build the object the same
    way tests/test_beam_pattern.py does -- __new__ to skip __init__'s socket
    binds, then _do_init_hardware() in sim mode -- and read the attributes. The
    generated defaults are then the Python defaults by construction.
    """
    if "adi" not in sys.modules:
        # pyadi-iio binds libiio at import; sim mode never touches it.
        sys.modules["adi"] = types.ModuleType("adi")

    import phaser_headless

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        h = phaser_headless.PhaserHeadless.__new__(phaser_headless.PhaserHeadless)
        h.sim_mode = True
        h.c = 299792458
        h._do_init_hardware()
    return h


def main():
    sdr = phaser_sim.SimSDR
    steer_pi = steering_pi()
    h = headless_defaults()

    body = f"""// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Produced by tools/gen_sim_constants.py from phaser_sim.py, config.py and
// phaser_headless.py. Python is the source of truth for the simulator's
// physics; this file exists so the JS port cannot drift from it.
//
// To change a value here, change it in Python and re-run:
//     python tools/gen_sim_constants.py
//
// CI regenerates this file and fails if the result differs from what is
// committed.

// --- Scene -----------------------------------------------------------------
export const TARGET_ANGLE_DEG = {sdr.TARGET_ANGLE_DEG!r};
export const TARGET_IF_HZ = {sdr.TARGET_IF_HZ!r};
export const INTERFERER_IF_HZ = {sdr.INTERFERER_IF_HZ!r};

// --- Synthesis -------------------------------------------------------------
export const AMP_SCALE = {sdr.AMP_SCALE!r};
export const NOISE_SIGMA = {sdr.NOISE_SIGMA!r};

// --- Radio -----------------------------------------------------------------
export const SIGNAL_FREQ_HZ = {h.SignalFreq!r};
export const RX_FREQ_HZ = {h.Rx_freq!r};
export const TX_FREQ_HZ = {h.Tx_freq!r};
export const SAMPLE_RATE_HZ = {h.SampleRate!r};
export const BUFFER_SIZE = {config.buffer_size!r};
export const RX_GAIN_DB = {h.Rx_gain!r};
export const TX_GAIN_DB = {h.Tx_gain!r};
export const AVERAGES = {h.Averages!r};

// --- Array geometry --------------------------------------------------------
export const ELEMENT_SPACING_M = {h.d!r};
export const NUM_ELEMENTS = 8;
export const C_M_PER_S = {h.c!r};

// --- Sweep defaults --------------------------------------------------------
export const PHASE_STEP_DEG = {h.phase_step!r};
export const STEER_RES_DEG = {h.steer_res!r};
export const IGNORE_RES = {str(bool(h.ignore_res)).lower()};
export const STEER_MIN_DEG = {h.steer_min!r};
export const STEER_MAX_DEG = {h.steer_max!r};
export const BW_MHZ = {h.BW!r};

// --- Digital beamformer ----------------------------------------------------
export const BF_MODE = {h.bf_mode!r};
export const MVDR_K = {h.mvdr_K!r};
export const MVDR_DIAG_LOAD = {h.mvdr_diag_load!r};
export const B0_GAIN = {h.B0_Gain!r};
export const B1_GAIN = {h.B1_Gain!r};
export const BEAM0_PHASE_DEG = {h.Beam0_Phase!r};
export const BEAM1_PHASE_DEG = {h.Beam1_Phase!r};

// --- Fixed point -----------------------------------------------------------
// dBFS reference: the ADC's 2^11 full scale, as used by do_sweep().
export const FULL_SCALE = 2048;

// --- The two pis -----------------------------------------------------------
// phaser_sim's wave synthesis uses real pi...
export const SIM_PI = Math.PI;
// ...while do_sweep()'s steering math uses this truncated literal, carried over
// from the legacy phaser_gui.py. Do not "fix" this to Math.PI: it would move
// every steering angle by ~0.03% relative to the hardware code path.
export const STEER_PI = {steer_pi};

// --- Calibration -----------------------------------------------------------
// A static build has no calibration store, so it gets whatever a fresh clone
// gets from load_phase_cal()/load_gain_cal().
export const DEFAULT_PHASE_CAL = {list(h.phase_cal)!r};
export const DEFAULT_GAIN_CAL = {list(h.gain_cal)!r};
"""
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(body)
    print(f"wrote {OUT.relative_to(REPO)}")


if __name__ == "__main__":
    main()
