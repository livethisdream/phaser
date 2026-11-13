// GENERATED FILE -- DO NOT EDIT BY HAND.
//
// Produced by tools/gen_sim_constants.py from phaser_sim.py,
// phaser_radar_dsp.py and phaser_headless.py, for the radar UI's in-browser
// simulator. Python is the source of truth; re-run after changing any of these:
//     python tools/gen_sim_constants.py
//
// CI regenerates this file and fails if the result differs from what is
// committed.

// --- Scenes (phaser_sim.SimSDR) --------------------------------------------
// CW: [velocity_mps, angle_deg, amplitude]; FMCW: [range_m, velocity_mps,
// angle_deg, amplitude]. Velocity is positive for closing targets.
export const CW_DEFAULT_TARGETS = [[0.0, 0.0, 1.0], [2.5, 0.0, 0.45], [-1.2, 15.0, 0.25]];
export const FMCW_DEFAULT_TARGETS = [[1.0, 0.0, 0.0, 1.0], [3.5, 1.8, 0.0, 0.6], [7.0, -2.4, 12.0, 0.35]];
export const AMP_SCALE = 60.0;
export const NOISE_SIGMA = 4.0;
export const ELEMENT_SPACING_M = 0.014;
// The kit IF a sim backend reports (get_state's Rx_freq).
export const RX_FREQ_HZ = 2200000000.0;
// ADAR1000 rx_gain register code -> voltage amplitude (ADAR_pyadi_functions).
export const RX_GAIN_AMPLITUDE = [0.0, 0.03710337961432636, 0.059444799531619394, 0.07831697833659848, 0.09523887656826795, 0.11084422537346067, 0.12547474449857607, 0.13934138949787458, 0.15258599038863105, 0.1653097146279082, 0.17758794010286924, 0.18947874330414666, 0.2010280764321269, 0.21227309364717636, 0.2232443796564605, 0.23396749534801023, 0.2444640812850239, 0.2547526652200807, 0.26484926572270756, 0.27476785184017977, 0.28452069887919723, 0.29411866779065393, 0.3035714274089764, 0.3128876332934543, 0.32207507315912215, 0.3311407862667718, 0.3400911622870279, 0.3489320238187747, 0.35766869576794386, 0.36630606407208977, 0.37484862571676203, 0.38330053158138605, 0.391665623340107, 0.39994746540196485, 0.4081493726869601, 0.41627443488705484, 0.4243255377443813, 0.4323053817858192, 0.4402164988783525, 0.4480612669092162, 0.4558419228457445, 0.46356057438968706, 0.47121921040776377, 0.47881971029296005, 0.48636385238843116, 0.49385332158699646, 0.501289716203388, 0.5086745542031064, 0.5160092788604932, 0.5232952639090989, 0.5305338182393082, 0.5377261901912604, 0.5448735714851636, 0.5519771008259986, 0.5590378672152099, 0.5660569129981734, 0.5730352366729331, 0.5799737954828285, 0.5868735078131304, 0.5937352554096208, 0.6005598854351293, 0.6073482123783626, 0.614101019827876, 0.6208190621227337, 0.6275030658902452, 0.6341537314801433, 0.6407717343036632, 0.6473577260851675, 0.6539123360332484, 0.6604361719375932, 0.6669298211973242, 0.6733938517860102, 0.6798288131580842, 0.6862352371009899, 0.692613638537002, 0.6989645162783322, 0.7052883537388277, 0.7115856196052951, 0.7178567684712337, 0.7241022414355369, 0.730322466668517, 0.7365178599474217, 0.742688825163443, 0.7488357548020643, 0.7549590303984514, 0.7610590229694656, 0.7671360934237597, 0.7731905929513091, 0.7792228633936351, 0.7852332375958843, 0.791222039741847, 0.797189585672923, 0.8031361831919689, 0.8090621323529025, 0.8149677257368759, 0.8208532487157774, 0.8267189797037688, 0.832565190397524, 0.8383921460057816, 0.8442001054687979, 0.8499893216682349, 0.8557600416279978, 0.8615125067064936, 0.8672469527807596, 0.8729636104228803, 0.878662705069088, 0.8843444571819143, 0.8900090824057454, 0.8956567917161032, 0.9012877915629652, 0.9069022840084122, 0.912500466858877, 0.9180825337922534, 0.9236486744801097, 0.9291990747052358, 0.9347339164747416, 0.9402533781289131, 0.9457576344460195, 0.9512468567432559, 0.956721212973995, 0.962180867821513, 0.9676259827893454, 0.9730567162884214, 0.9784732237211167, 0.9838756575623575, 0.9892641674379028, 0.9946389001999247, 1.0];

// --- Live FMCW scenes (phaser_sim, after ece444 fmcw_sim.py) ----------------
export const FMCW_SCENES = {"static": "Static targets: 1 m, 3.5 m walking in place, 7 m", "corners": "Corner reflectors at 1, 2 and 3 m", "walker": "A person walking out from 1 m to 4 m and back", "hand": "A hand waving at 0.6 m, 1 Hz", "turntable": "Three corner reflectors on a turntable 1.5 m out (ISAR)"};
export const FMCW_DEFAULT_SCENE = "static";
export const SCENE_PT_DBM = -16.0;
export const SCENE_GTGR_DB = 21.0;
export const SCENE_NF_DB = 2.0;
export const SCENE_NOISE_DBFS = -60.0;
export const SCENE_LEAKAGE_DBFS = -20.0;
export const SCENE_R_MIN = 0.3;
export const SCENE_FULL_SCALE = 2048;
export const SCENE_WALKER_PHASES = [3.2142, 5.9689, 0.9053, 5.9575, 1.9583];
export const SCENE_TURNTABLE_R0 = 1.5;
export const SCENE_TURNTABLE_POINTS = [[0.03, 0.0, 0.1], [-0.02, 0.04, 0.1], [-0.04, -0.03, 0.1]];
export const SCENE_NOISE_LEVELS = {"clean": "Clean: the course's simulator, targets and noise floor only", "typical": "Typical: a room's clutter, leakage skirt, ~3 dB target fading, 10 dB more noise", "noisy": "Noisy: stronger clutter, ~6 dB fading, 18 dB more noise"};
export const FMCW_DEFAULT_SCENE_NOISE = "clean";
export const SCENE_NOISE = {"clean": {"floor_dbfs": -60.0, "clutter_db": null, "fluct_db": 0.0}, "typical": {"floor_dbfs": -50.0, "clutter_db": 0.0, "fluct_db": 3.0}, "noisy": {"floor_dbfs": -42.0, "clutter_db": 6.0, "fluct_db": 6.0}};
export const SCENE_CLUTTER = [[1.6, 0.3, 0.4, -25.0], [3.3, 0.8, 2.1, 30.0], [5.0, 4.0, 4.4, 0.0], [6.5, 2.0, 1.7, -15.0]];
export const SCENE_SKIRT = [[0.15, -34.0, 2.9], [0.3, -42.0, 5.1], [0.5, -48.0, 0.3]];
export const SCENE_FLUCT_HZ = [1.7, 2.9, 4.3];
export const SCENE_FLUCT_PHASE = [1.1, 2.3, 0.7];

// --- phaser_radar_dsp --------------------------------------------------------
export const C = 299792458.0;
export const DEFAULTS = {"sample_rate": 600000, "fft_size": 65536, "signal_freq": 100000, "output_freq": 12200000000.0, "center_freq": 2200000000.0, "rx_gain": 30, "tx_gain": 0, "fft_window": "blackman", "taper": "blackman", "vel_max": 30.0, "peak_exclude": 0.3};
export const TAPER_PRESETS = {"rect": [127, 127, 127, 127, 127, 127, 127, 127], "hann": [12, 43, 77, 100, 100, 77, 43, 12], "blackman": [8, 34, 84, 127, 127, 84, 34, 8]};
export const TAPER_MAX = 127;
export const FMCW_DEFAULTS = {"sample_rate": 600000, "chirp_bw": 500000000.0, "ramp_time": 0.001, "num_chirps": 1, "pri": 0.001, "signal_freq": 100000, "output_freq": 12200000000.0, "center_freq": 2200000000.0, "rx_gain": 30, "tx_gain": 0, "fft_window": "blackman", "taper": "blackman", "range_max": 20.0, "mti": "none", "zero_range_freq": null};
export const FMCW_HW_DEFAULTS = {"pri_guard_ms": 1.0, "fft_size": 4096, "prefetch": true};
export const FMCW_PRESETS = {"range": {"sample_rate": 600000, "output_freq": 10000000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.001, "num_chirps": 1, "pri_guard_ms": 1.0, "fft_size": 4096, "rx_gain": 30, "tx_gain": 0}, "range_sync": {"sample_rate": 5000000, "output_freq": 10000000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.0005, "num_chirps": 1, "pri_guard_ms": 1.0, "rx_gain": 20, "tx_gain": 0}, "cfar": {"sample_rate": 2000000, "output_freq": 10000000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.0005, "num_chirps": 1, "pri_guard_ms": 1.0, "rx_gain": 20, "tx_gain": 0}, "range_doppler": {"sample_rate": 2000000, "output_freq": 9900000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.0003, "num_chirps": 256, "pri_guard_ms": 0.2, "rx_gain": 60, "tx_gain": 0}, "range_doppler_far": {"sample_rate": 4000000, "output_freq": 9900000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.0003, "num_chirps": 256, "pri_guard_ms": 0.2, "rx_gain": 60, "tx_gain": 0, "range_max": 100.0}, "micro_doppler": {"sample_rate": 600000, "output_freq": 10000000000.0, "chirp_bw": 500000000.0, "ramp_time": 0.0003, "num_chirps": 256, "pri_guard_ms": 0.2, "rx_gain": 60, "tx_gain": 0, "range_max": 4.0}};
export const FMCW_PROC_DEFAULTS = {"vel_max": null, "cfar": false, "cfar_guard": 4, "cfar_ref": 16, "cfar_bias_db": 6.0, "peak_exclude": 0.3, "peak_min": 1.0, "peak_snr_db": 15.0, "clutter_margin_db": 30.0, "spectrogram_range": null};
export const FMCW_DEFAULT_PRESET = "range_doppler";
export const FMCW_FALLBACK_PRESET = "range";
// [label, hint] per preset, in the dropdown's order.
export const FMCW_PRESET_UI = {"range_doppler": ["Range + Velocity", "Indoor, ~5 fps. Hand wave, fidget spinner, MTI."], "range_doppler_far": ["Range + Vel (Far)", "Outdoor, out to ~180 m, ~2.4 fps."], "range": ["Range (Basic)", "FMCW lab: the 0 m calibration and kHz per meter."], "range_sync": ["Range (Fast)", "A steeper chirp, out to ~375 m."], "cfar": ["Range (CFAR)", "CFAR lab: tune the threshold, then Apply."], "micro_doppler": ["Micro-Doppler", "Long continuous records of moving targets for micro-Doppler: 0.6 MS/s, up to ~7 s, shown out to 4 m."]};
export const MTI_MODES = ["none", "2pulse", "3pulse"];
export const MAINLOBE_HALF_BINS = {"blackman": 3, "hann": 2, "hamming": 2, "rect": 1};
export const MAP_SPAN_DB = 100.0;
export const AXIS_DECIMALS = 4;
export const DB_DECIMALS = 2;
export const RAMP_SYNCED = "single_sawtooth_burst";
export const RAMP_FREE_RUNNING = "continuous_triangular";
export const RANGE_TIME_SCALE = {"single_sawtooth_burst": 1.0, "continuous_triangular": 0.5};
export const MAX_BURST_BUFFER = 4194304;
export const RAMP_SETTLE_FRACTION = 0.1;
// Single-chirp frames per saved capture (the course .npz).
export const CAPTURE_SINGLE_CHIRP_FRAMES = 32;
export const CAPTURE_MAX_FRAMES_S = 60.0;
export const CAPTURE_FRAME_S = 0.05;
export const BYTES_PER_SAMPLE = 8;
export const CAPTURE_FULL_SCALE = 2048;
export const SPEC_WINDOW = 64;
export const ISAR_PAD = 8;
export const ISAR_EXTENT_M = 0.16;
export const ISAR_PIXELS = 81;
export const ISAR_R0_M = 1.5;
export const ISAR_LEAK_SEARCH_M = 0.6;
export const ISAR_FIND_SPAN_M = 0.5;
export const SPEC_HOP = 16;
export const CAPTURE_BURST_INTERVAL_S = 0.25;
export const CAPTURE_MAX_BYTES = 64000000.0;
export const CAPTURE_COUNTS_ABOVE = 4.0;

// --- phaser_headless ---------------------------------------------------------
// The frame-rate cap the main loop applies to radar frames.
export const RADAR_MAX_FPS = 20.0;
