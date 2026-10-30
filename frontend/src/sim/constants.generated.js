// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Produced by tools/gen_sim_constants.py from phaser_sim.py, config.py,
// ADAR_pyadi_functions.py and phaser_headless.py. Python is the source of
// truth for the simulator's physics; this file exists so the JS port cannot
// drift from it.
//
// To change a value here, change it in Python and re-run:
//     python tools/gen_sim_constants.py
//
// CI regenerates this file and fails if the result differs from what is
// committed.

// --- Scene -----------------------------------------------------------------
export const TARGET_ANGLE_DEG = 0.0;
export const TARGET_RANGE_M = 1.0;
export const TARGET_RANGE_MIN_M = 0.5;
export const TARGET_RANGE_MAX_M = 20.0;
export const TARGET_IF_HZ = 1000000.0;

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

// --- Synthetic interferer (injected in do_sweep) ---------------------------
export const INTERFERER_ANGLE_DEG = -20.0;
export const INTERFERER_POWER_DBFS = -10.0;
export const INTERFERER_OFFSET_HZ = 100000.0;
export const INTERFERER_PHASE_DEG = 0.0;
export const B0_GAIN = 1.0;
export const B1_GAIN = 1.0;
export const BEAM0_PHASE_DEG = 0.0;
export const BEAM1_PHASE_DEG = 0.0;

// --- Peak angle ------------------------------------------------------------
// peak_angle_deg is phaser_ctf.peak_angle_centroid() over the sweep: the
// mainlobe is walked out from the argmax while samples stay within this many
// dB of it, then power-weighted.
export const CENTROID_DROP_DB = 3.0;

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

// --- ADAR1000 rx_gain register ---------------------------------------------
// Voltage amplitude at each register code 0-127, relative to code 127. The
// register is not linear in voltage; applyGainCal inverts this table and the
// SDR stub applies it, so the sim stays linear in slider %. Emitted as numbers
// rather than recomputed with Math.pow, which need not round like Python's **.
export const RX_GAIN_AMPLITUDE = [0.0, 0.03710337961432636, 0.059444799531619394, 0.07831697833659848, 0.09523887656826795, 0.11084422537346067, 0.12547474449857607, 0.13934138949787458, 0.15258599038863105, 0.1653097146279082, 0.17758794010286924, 0.18947874330414666, 0.2010280764321269, 0.21227309364717636, 0.2232443796564605, 0.23396749534801023, 0.2444640812850239, 0.2547526652200807, 0.26484926572270756, 0.27476785184017977, 0.28452069887919723, 0.29411866779065393, 0.3035714274089764, 0.3128876332934543, 0.32207507315912215, 0.3311407862667718, 0.3400911622870279, 0.3489320238187747, 0.35766869576794386, 0.36630606407208977, 0.37484862571676203, 0.38330053158138605, 0.391665623340107, 0.39994746540196485, 0.4081493726869601, 0.41627443488705484, 0.4243255377443813, 0.4323053817858192, 0.4402164988783525, 0.4480612669092162, 0.4558419228457445, 0.46356057438968706, 0.47121921040776377, 0.47881971029296005, 0.48636385238843116, 0.49385332158699646, 0.501289716203388, 0.5086745542031064, 0.5160092788604932, 0.5232952639090989, 0.5305338182393082, 0.5377261901912604, 0.5448735714851636, 0.5519771008259986, 0.5590378672152099, 0.5660569129981734, 0.5730352366729331, 0.5799737954828285, 0.5868735078131304, 0.5937352554096208, 0.6005598854351293, 0.6073482123783626, 0.614101019827876, 0.6208190621227337, 0.6275030658902452, 0.6341537314801433, 0.6407717343036632, 0.6473577260851675, 0.6539123360332484, 0.6604361719375932, 0.6669298211973242, 0.6733938517860102, 0.6798288131580842, 0.6862352371009899, 0.692613638537002, 0.6989645162783322, 0.7052883537388277, 0.7115856196052951, 0.7178567684712337, 0.7241022414355369, 0.730322466668517, 0.7365178599474217, 0.742688825163443, 0.7488357548020643, 0.7549590303984514, 0.7610590229694656, 0.7671360934237597, 0.7731905929513091, 0.7792228633936351, 0.7852332375958843, 0.791222039741847, 0.797189585672923, 0.8031361831919689, 0.8090621323529025, 0.8149677257368759, 0.8208532487157774, 0.8267189797037688, 0.832565190397524, 0.8383921460057816, 0.8442001054687979, 0.8499893216682349, 0.8557600416279978, 0.8615125067064936, 0.8672469527807596, 0.8729636104228803, 0.878662705069088, 0.8843444571819143, 0.8900090824057454, 0.8956567917161032, 0.9012877915629652, 0.9069022840084122, 0.912500466858877, 0.9180825337922534, 0.9236486744801097, 0.9291990747052358, 0.9347339164747416, 0.9402533781289131, 0.9457576344460195, 0.9512468567432559, 0.956721212973995, 0.962180867821513, 0.9676259827893454, 0.9730567162884214, 0.9784732237211167, 0.9838756575623575, 0.9892641674379028, 0.9946389001999247, 1.0];

// --- Calibration -----------------------------------------------------------
// A static build has no calibration store, so it gets whatever a fresh clone
// gets from load_phase_cal()/load_gain_cal().
export const DEFAULT_PHASE_CAL = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0];
export const DEFAULT_GAIN_CAL = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0];
