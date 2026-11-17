/**
 * engine.js — phaser_headless.py's radar handlers, in the browser.
 *
 * Answers the same commands with the same shapes: get_state, the CW and FMCW
 * start/stop/set/get_state calls, get_radar_capabilities. frame() is the main
 * loop's _radar_frame_step: one CW or FMCW frame from the simulated scene,
 * processed exactly as do_cw_radar_frame / do_fmcw_radar_frame do.
 *
 * As under --sim, the kit reports a chirp-synced ramp, the stub ADF4159 runs
 * the ramp time it was given, and the simulated array is a matched one (no
 * element phase error, boresight phases).
 */

import {
    CW_DEFAULT_TARGETS, DEFAULTS, FMCW_DEFAULT_PRESET, FMCW_DEFAULT_TARGETS,
    FMCW_PRESETS, FMCW_PRESET_UI, NOISE_SIGMA,
    RAMP_SYNCED, RX_FREQ_HZ, CAPTURE_SINGLE_CHIRP_FRAMES, FMCW_SCENES, FMCW_DEFAULT_SCENE, SCENE_NOISE_LEVELS, FMCW_DEFAULT_SCENE_NOISE,
    CAPTURE_FULL_SCALE,
} from './constants.generated.js';
import {
    captureBursts, captureLimits, chirpPlan, fmcwConfig, fmcwPayload, processCwFrame, rampTimeUs,
    resolveTaper, sliceChirps,
} from './dsp.js';
import {
    createGauss, finishCapture, finishChannels, synthesizeCw, synthesizeFmcw,
    synthesizeFmcwScene, sceneNoiseSigma,
} from './scene.js';
import { toBase64, writeNpz } from '../npz.js';

// Params that only change processing (next frame), and those written straight
// to the hardware; anything else re-enters FMCW. phaser_headless's sets.
const FMCW_LIVE_KEYS = new Set([
    'mti', 'fft_window', 'range_max', 'vel_max', 'zero_range_freq', 'peak_exclude', 'peak_min', 'peak_snr_db', 'clutter_margin_db', 'spectrogram_range',
    'cfar', 'cfar_guard', 'cfar_ref', 'cfar_bias_db', 'scene', 'scene_noise',
]);
const FMCW_WRITE_KEYS = new Set(['taper', 'rx_gain', 'tx_gain']);

const CAPABILITY = Object.freeze({
    available: true, ramp: RAMP_SYNCED, source: 'sim', reason: '', checks: [],
});

const roundHalfEvenNum = (x) => { const r = Math.round(x); return (Math.abs(x % 1) === 0.5 && r % 2 !== 0) ? r - 1 : r; };
const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

export function createRadarEngine({ noiseSigma = NOISE_SIGMA, seed = 0, clock = null } = {}) {
    // The live scenes' clock: seconds since the engine started, or a fixed
    // one for the parity tests (phaser_sim.SimSDR.scene_clock).
    const tStart = now();
    const sceneClock = clock || (() => (now() - tStart) / 1000);
    const gauss = createGauss(seed);
    // The latched ADAR state the scene reads: gain codes and phases.
    const elements = Array.from({ length: 8 }, () => ({ gain: 100, phase: 0 }));
    const phaseError = new Array(8).fill(0);

    const s = {
        mode: 'idle',
        cwParams: {},
        fmcwParams: {},
        fmcwCfg: null,
        fmcwPlan: null,
        fmcwPreset: null,
        sampleRate: 0,
        bufferSize: 0,
    };

    function applyTaper(taper) {
        const gains = resolveTaper(taper);
        gains.forEach((g, i) => { elements[i].gain = g; });
        return gains;
    }

    function applyBoresight() {
        for (const el of elements) el.phase = 0;
    }

    // --- CW --------------------------------------------------------------

    function cwCfg() {
        const cfg = { ...DEFAULTS, ...s.cwParams };
        if (cfg.center_freq == null) cfg.center_freq = RX_FREQ_HZ;
        return cfg;
    }

    function enterCw() {
        const cfg = cwCfg();
        applyTaper(cfg.taper);
        applyBoresight();
        s.sampleRate = Math.trunc(cfg.sample_rate);
        s.bufferSize = Math.trunc(cfg.fft_size);
    }

    function cwFrame() {
        const cfg = cwCfg();
        const chans = synthesizeCw({
            n: s.bufferSize, fs: s.sampleRate,
            signalFreq: Number(cfg.signal_freq), outputFreq: Number(cfg.output_freq),
            targets: CW_DEFAULT_TARGETS, elements, phaseError,
        });
        const iq = finishCapture(chans, { noiseSigma, gauss });
        return processCwFrame(iq, cfg.sample_rate, cfg.signal_freq, cfg.output_freq, {
            fftWindow: cfg.fft_window ?? 'blackman',
            velMax: cfg.vel_max, peakExclude: cfg.peak_exclude,
        });
    }

    // --- FMCW ------------------------------------------------------------

    function effectiveFmcwCfg() {
        const params = { ...s.fmcwParams };
        const preset = params.preset || FMCW_DEFAULT_PRESET;
        delete params.preset;
        if (params.center_freq == null) params.center_freq = RX_FREQ_HZ;
        if ('range_max' in params && params.range_max == null) delete params.range_max;
        return { cfg: fmcwConfig(params, preset), preset };
    }

    function enterFmcw() {
        const { cfg, preset } = effectiveFmcwCfg();
        applyTaper(cfg.taper);
        applyBoresight();
        const plan = chirpPlan(cfg, RAMP_SYNCED, rampTimeUs(cfg), 0);
        plan.raw_read = false;
        plan.prefetch = Boolean(cfg.prefetch ?? true);
        s.sampleRate = Math.trunc(cfg.sample_rate);
        s.bufferSize = plan.rx_buffer_size;
        s.fmcwCfg = cfg;
        s.fmcwPlan = plan;
        s.fmcwPreset = preset;
    }

    function exitFmcw() {
        s.fmcwCfg = null;
        s.fmcwPlan = null;
    }

    function currentScene() {
        const name = s.fmcwCfg?.scene ?? FMCW_DEFAULT_SCENE;
        if (!(name in FMCW_SCENES)) throw new Error(`unknown FMCW scene '${name}'`);
        return name;
    }

    function currentSceneNoise() {
        const name = s.fmcwCfg?.scene_noise ?? FMCW_DEFAULT_SCENE_NOISE;
        if (!(name in SCENE_NOISE_LEVELS)) throw new Error(`unknown scene noise '${name}'`);
        return name;
    }

    // A live scene has the course's noise floor; the static one this
    // simulator's own. Off when the caller turned noise off (parity tests).
    function fmcwNoiseSigma() {
        return noiseSigma > 0 && currentScene() !== 'static' ? sceneNoiseSigma(currentSceneNoise()) : noiseSigma;
    }

    // `plan` and `t0` default to the running ones: a long Save Capture
    // synthesizes one burst of its own plan, and single-chirp saves step the
    // scene clock a frame interval at a time.
    function synthesizeFmcwChannels(plan = s.fmcwPlan, t0 = null) {
        const cfg = s.fmcwCfg;
        const n = plan === s.fmcwPlan ? s.bufferSize : plan.rx_buffer_size;
        const scene = currentScene();
        if (scene !== 'static') {
            return synthesizeFmcwScene({
                n, fs: s.sampleRate,
                fmcw: {
                    chirp_bw: Number(cfg.chirp_bw), ramp_time: plan.effective_ramp_time,
                    num_chirps: plan.num_chirps, pri: plan.pri_ms / 1e3,
                    signal_freq: Number(cfg.signal_freq), output_freq: Number(cfg.output_freq),
                },
                plan, scene, noise: currentSceneNoise(), t0: t0 ?? sceneClock(), elements, phaseError,
            });
        }
        return synthesizeFmcw({
            n, fs: s.sampleRate,
            fmcw: {
                chirp_bw: Number(cfg.chirp_bw),
                ramp_time: plan.effective_ramp_time,
                num_chirps: plan.num_chirps,
                pri: plan.pri_ms / 1e3,
                signal_freq: Number(cfg.signal_freq),
                output_freq: Number(cfg.output_freq),
            },
            plan, targets: FMCW_DEFAULT_TARGETS, elements, phaseError,
        });
    }

    /** save_fmcw_capture: the course .npz, as phaser_headless writes it. */
    function saveFmcwCapture(params = {}) {
        if (s.mode !== 'fmcw_radar' || !s.fmcwCfg) {
            return { status: 'error', message: 'Start FMCW to save a capture' };
        }
        const cfg = s.fmcwCfg;
        let plan = s.fmcwPlan;
        const wantCh = Boolean(params.iq_ch);
        // Only Micro-Doppler saves one continuous long burst; the browser is a simulator.
        const limits = captureLimits(plan, s.fmcwPreset === 'micro_doppler');
        const duration = params.duration_s == null || params.duration_s === '' ? limits.default_s : Number(params.duration_s);
        if (!(duration > 0) || duration > limits.max_s * (1 + 1e-9)) {
            return { status: 'error', message: `Duration must be more than 0 and at most ${+limits.max_s.toPrecision(3)} s on this preset` };
        }
        // As phaser_headless.save_fmcw_capture: a burst of duration / PRI
        // chirps (a longer plan if the preset's is too short), or frames at
        // the live frame interval -- here stepped on the scene clock, since
        // the simulator does not wait.
        let frames = 1, keep = Infinity;
        const nBursts = captureBursts(limits, duration);
        if (nBursts) {
            // A run of whole bursts, run_s apart on the scene clock.
            frames = nBursts;
        } else if (limits.kind === 'burst' || limits.kind === 'bursts') {
            const chirps = Math.max(2, roundHalfEvenNum(duration / limits.chirp_s));
            if (chirps > plan.num_chirps) {
                plan = chirpPlan({ ...cfg, num_chirps: chirps }, RAMP_SYNCED, rampTimeUs(cfg), 0);
            }
            keep = chirps;
        } else {
            frames = Math.max(1, roundHalfEvenNum(duration / limits.chirp_s));
        }
        const rows = [[], [], []];   // summed, chan0, chan1: one {re, im} per chirp
        const stamps = [];
        const tBase = sceneClock();
        for (let f = 0; f < frames; f++) {
            const step = nBursts ? limits.run_s : limits.chirp_s;
            stamps.push(f * step * 1e3);
            const t0 = limits.kind === 'frames' || nBursts ? tBase + f * step : null;
            const [c0, c1] = finishChannels(synthesizeFmcwChannels(plan, t0), { noiseSigma: fmcwNoiseSigma(), gauss });
            const s0 = sliceChirps(c0, plan).slice(0, keep), s1 = sliceChirps(c1, plan).slice(0, keep);
            s0.forEach((col, k) => {
                const re = col.re.map((v, i) => v + s1[k].re[i]);
                const im = col.im.map((v, i) => v + s1[k].im[i]);
                rows[0].push({ re, im });
                rows[1].push(col);
                rows[2].push(s1[k]);
            });
        }
        const nChirps = rows[0].length, nSamples = rows[0][0].re.length;
        // Counts in, full-scale units out (|iq| = 1 is 0 dBFS), as capture_npz.
        const flat = (list) => {
            const re = new Float32Array(list.length * nSamples), im = new Float32Array(list.length * nSamples);
            list.forEach((r, k) => {
                for (let i = 0; i < nSamples; i++) {
                    re[k * nSamples + i] = r.re[i] / CAPTURE_FULL_SCALE;
                    im[k * nSamples + i] = r.im[i] / CAPTURE_FULL_SCALE;
                }
            });
            return { re, im };
        };
        // One single-chirp frame has no interval to measure: the next would
        // have come a frame interval later, not the plan's PRI.
        const pri = nBursts ? limits.chirp_s
            : frames > 1 ? (stamps[frames - 1] - stamps[0]) / 1e3 / (frames - 1)
            : limits.kind === 'frames' ? limits.chirp_s
                : (plan.pri_ms ? plan.pri_ms / 1e3 : plan.ramp_time);
        const calibrated = cfg.zero_range_freq != null;
        const f0 = Number(calibrated ? cfg.zero_range_freq : cfg.signal_freq);
        const fs = plan.sample_rate, tRamp = plan.effective_ramp_time;
        const run = nBursts ? `${nBursts} bursts of ${plan.num_chirps} chirps, one every `
            + `${((stamps[nBursts - 1] - stamps[0]) / (nBursts - 1)).toFixed(0)} ms (burst_len, burst_t: `
            + 'take Doppler within a burst); ' : '';
        const note = `Phaser radar GUI capture: preset ${s.fmcwPreset}, ${run}${nChirps} chirps of `
            + `${+(tRamp * 1e6).toPrecision(6)} us every ${+(pri * 1e3).toPrecision(4)} ms, `
            + `${fs / 1e6} MSPS, B ${Number(cfg.chirp_bw) / 1e6} MHz, fc ${Number(cfg.output_freq) / 1e9} GHz, `
            + `f0 ${f0.toFixed(0)} Hz (${calibrated ? '0 m calibration' : 'IF tone, not calibrated'}); `
            + `in-browser simulator, ${new Date().toISOString().slice(0, 19).replace('T', ' ')}`;
        const arrays = {
            iq: { dtype: '<c8', shape: [nChirps, nSamples], data: flat(rows[0]) },
            fs: { dtype: '<f8', data: fs },
            fc: { dtype: '<f8', data: Number(cfg.output_freq) },
            B: { dtype: '<f8', data: Number(cfg.chirp_bw) },
            T_ramp: { dtype: '<f8', data: tRamp },
            PRI: { dtype: '<f8', data: pri },
            f0: { dtype: '<f8', data: f0 },
            note: { dtype: '<U', data: note },
        };
        if (nBursts) {
            arrays.burst_len = { dtype: '<f8', data: Number(plan.num_chirps) };
            arrays.burst_t = { dtype: '<f8', shape: [nBursts],
                               data: Float64Array.from(stamps, (t) => (t - stamps[0]) / 1e3) };
        }
        if (wantCh) {
            const a = flat(rows[1]), b = flat(rows[2]);
            const re = new Float32Array(2 * a.re.length), im = new Float32Array(2 * a.im.length);
            re.set(a.re); re.set(b.re, a.re.length); im.set(a.im); im.set(b.im, a.im.length);
            arrays.iq_ch = { dtype: '<c8', shape: [2, nChirps, nSamples], data: { re, im } };
        }
        const blob = writeNpz(arrays);
        return {
            status: 'ok', preset: s.fmcwPreset, n_chirps: nChirps, n_samples: nSamples,
            duration_s: nChirps * pri, iq_ch: wantCh, size: blob.length, note, data: toBase64(blob),
        };
    }

    function fmcwFrame() {
        const cfg = s.fmcwCfg, plan = s.fmcwPlan;
        const t0 = now();
        const chans = synthesizeFmcwChannels();
        const data = sliceChirps(finishCapture(chans, { noiseSigma: fmcwNoiseSigma(), gauss }), plan);
        const t1 = now();

        const out = fmcwPayload(data, cfg, plan, s.fmcwPreset);
        out.capture_ms = t1 - t0;
        out.process_ms = now() - t1;
        return out;
    }

    // --- Mode dispatcher -------------------------------------------------

    const HANDLERS = {
        cw_radar: [enterCw, () => {}],
        fmcw_radar: [enterFmcw, exitFmcw],
    };

    function setMode(newMode) {
        if (newMode === s.mode) return { status: 'ok', mode: s.mode };
        HANDLERS[s.mode]?.[1]();
        s.mode = 'idle';
        const enter = HANDLERS[newMode]?.[0];
        if (enter) {
            try {
                enter();
            } catch (e) {
                HANDLERS[newMode][1]();
                return { status: 'error', message: `entering ${newMode} failed: ${e.message || e}` };
            }
        }
        s.mode = newMode;
        return { status: 'ok', mode: newMode };
    }

    function presetInfo(name) {
        const cfg = fmcwConfig({}, name);
        const [label, hint] = FMCW_PRESET_UI[name] || [name, ''];
        return {
            num_chirps: Math.trunc(cfg.num_chirps), label, hint,
            output_freq: Number(cfg.output_freq),
            rx_gain: Number(cfg.rx_gain), tx_gain: Number(cfg.tx_gain),
        };
    }

    function getFmcwRadarState() {
        const running = s.mode === 'fmcw_radar';
        let cfg = s.fmcwCfg, preset = s.fmcwPreset, error = null;
        if (!running || cfg == null) {
            try {
                ({ cfg, preset } = effectiveFmcwCfg());
            } catch (e) {
                cfg = null;
                error = String(e.message || e);
            }
        }
        const ui = Object.keys(FMCW_PRESET_UI).filter(p => p in FMCW_PRESETS);
        const rest = Object.keys(FMCW_PRESETS).filter(p => !(p in FMCW_PRESET_UI)).sort();
        const data = {
            mode: s.mode,
            running,
            preset,
            presets: [...ui, ...rest],
            preset_info: Object.fromEntries(Object.keys(FMCW_PRESETS).map(p => [p, presetInfo(p)])),
            params: { ...s.fmcwParams },
            cfg,
            plan: running ? s.fmcwPlan : null,
            capability: { ...CAPABILITY },
            capture: (() => {
                try {
                    return captureLimits(running ? s.fmcwPlan
                        : chirpPlan(cfg, RAMP_SYNCED, rampTimeUs(cfg), 0), preset === 'micro_doppler');
                } catch (e) { return null; }
            })(),
            // Simulation only: the live scenes the UI can pick from.
            scenes: { ...FMCW_SCENES },
            scene: cfg?.scene ?? FMCW_DEFAULT_SCENE,
            scene_noise_levels: { ...SCENE_NOISE_LEVELS },
            scene_noise: cfg?.scene_noise ?? FMCW_DEFAULT_SCENE_NOISE,
        };
        if (error) data.error = error;
        return { status: 'ok', data };
    }

    function setFmcwRadarParams(params) {
        if (!params || !Object.keys(params).length) return { status: 'ok' };
        s.fmcwParams = { ...s.fmcwParams, ...params };
        if (s.mode !== 'fmcw_radar') {
            return { status: 'ok', params: s.fmcwParams, reentered: false };
        }
        const replan = Object.keys(params).some(k => !FMCW_LIVE_KEYS.has(k) && !FMCW_WRITE_KEYS.has(k));
        if (replan) {
            setMode('idle');
            const resp = setMode('fmcw_radar');
            return { ...resp, params: s.fmcwParams, reentered: true };
        }
        const live = { ...params };
        if ('range_max' in live && live.range_max == null) {
            live.range_max = fmcwConfig({}, s.fmcwPreset).range_max;
        }
        s.fmcwCfg = { ...s.fmcwCfg, ...live };
        if ('taper' in params) applyTaper(params.taper);
        return { status: 'ok', params: s.fmcwParams, reentered: false };
    }

    function handle(cmd, data = {}) {
        switch (cmd) {
            case 'ping':
                return { status: 'ok', message: 'pong' };
            case 'get_state':
                return { status: 'ok', data: { Rx_freq: RX_FREQ_HZ, mode: s.mode, simulated: true } };
            case 'get_radar_capabilities':
                return { status: 'ok', data: { fmcw: { ...CAPABILITY } } };

            case 'get_cw_radar_state':
                return {
                    status: 'ok',
                    data: { mode: s.mode, running: s.mode === 'cw_radar', params: { ...DEFAULTS, ...s.cwParams } },
                };
            case 'start_cw_radar':
                if (Object.keys(data).length) s.cwParams = { ...s.cwParams, ...data };
                return setMode('cw_radar');
            case 'stop_cw_radar':
                return s.mode === 'cw_radar' ? setMode('idle') : { status: 'ok', mode: s.mode };
            case 'set_cw_radar_params':
                if (!Object.keys(data).length) return { status: 'ok' };
                s.cwParams = { ...s.cwParams, ...data };
                if (s.mode === 'cw_radar' && 'taper' in data) applyTaper(data.taper);
                return { status: 'ok', params: s.cwParams };

            case 'get_fmcw_radar_state':
                return getFmcwRadarState();
            case 'start_fmcw_radar':
                if (s.mode === 'fmcw_radar' && Object.keys(data).length) return setFmcwRadarParams(data);
                if (Object.keys(data).length) s.fmcwParams = { ...s.fmcwParams, ...data };
                return setMode('fmcw_radar');
            case 'stop_fmcw_radar':
                return s.mode === 'fmcw_radar' ? setMode('idle') : { status: 'ok', mode: s.mode };
            case 'set_fmcw_radar_params':
                return setFmcwRadarParams(data);
            case 'save_fmcw_capture':
                return saveFmcwCapture(data);

            default:
                return { status: 'error', message: `Unknown command: ${cmd}` };
        }
    }

    /** One radar frame as the main loop broadcasts it, or null when idle. */
    function frame() {
        if (s.mode === 'cw_radar') {
            return { type: 'cw_radar_frame', timestamp: Date.now() / 1000, data: cwFrame() };
        }
        if (s.mode === 'fmcw_radar') {
            return { type: 'fmcw_radar_frame', timestamp: Date.now() / 1000, data: fmcwFrame() };
        }
        return null;
    }

    return { handle, frame, get mode() { return s.mode; }, _internal: { s, elements } };
}
