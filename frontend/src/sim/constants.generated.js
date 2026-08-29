// GENERATED FILE -- DO NOT EDIT BY HAND.
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
export const TARGET_ANGLE_DEG = 0.0;
export const TARGET_IF_HZ = 1000000.0;
export const INTERFERER_IF_HZ = 1000000.0;

// --- Synthesis -------------------------------------------------------------
export const AMP_SCALE = 60.0;
export const NOISE_SIGMA = 4.0;

// --- Radio -----------------------------------------------------------------
export const SIGNAL_FREQ_HZ = 10525000000.0;
export const RX_FREQ_HZ = 2200000000.0;
export const TX_FREQ_HZ = 2200000000.0;
export const SAMPLE_RATE_HZ = 3000000.0;
export const BUFFER_SIZE = 16384;
export const RX_GAIN_DB = 30;
export const TX_GAIN_DB = -10;
export const AVERAGES = 1;

// --- Array geometry --------------------------------------------------------
export const ELEMENT_SPACING_M = 0.014;
export const NUM_ELEMENTS = 8;
export const C_M_PER_S = 299792458;

// --- Sweep defaults --------------------------------------------------------
export const PHASE_STEP_DEG = 2.8125;
export const STEER_RES_DEG = 2.8125;
export const IGNORE_RES = true;
export const STEER_MIN_DEG = -90;
export const STEER_MAX_DEG = 90;
export const BW_MHZ = 0;

// --- Digital beamformer ----------------------------------------------------
export const BF_MODE = 'manual';
export const MVDR_K = 128;
export const MVDR_DIAG_LOAD = 0.001;
export const B0_GAIN = 1.0;
export const B1_GAIN = 1.0;
export const BEAM0_PHASE_DEG = 0.0;
export const BEAM1_PHASE_DEG = 0.0;

// --- Fixed point -----------------------------------------------------------
// dBFS reference: the ADC's 2^11 full scale, as used by do_sweep().
export const FULL_SCALE = 2048;

// --- The two pis -----------------------------------------------------------
// phaser_sim's wave synthesis uses real pi...
export const SIM_PI = Math.PI;
// ...while do_sweep()'s steering math uses this truncated literal, carried over
// from the legacy phaser_gui.py. Do not "fix" this to Math.PI: it would move
// every steering angle by ~0.03% relative to the hardware code path.
export const STEER_PI = 3.14159;

// --- Calibration -----------------------------------------------------------
// A static build has no calibration store, so it gets whatever a fresh clone
// gets from load_phase_cal()/load_gain_cal().
export const DEFAULT_PHASE_CAL = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
export const DEFAULT_GAIN_CAL = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0];
