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
    FMCW_PRESETS, FMCW_PRESET_UI, AXIS_DECIMALS, DB_DECIMALS, NOISE_SIGMA,
    RAMP_SYNCED, RX_FREQ_HZ,
} from './constants.generated.js';
import {
    caCfar, cfarDetections, chirpPlan, encodeDbU8, fmcwConfig, mtiFilter,
    processCwFrame, rampTimeUs, rangeDopplerMap, rangeProfile, resolveTaper,
    rounded, sliceChirps,
} from './dsp.js';
import { createGauss, finishCapture, synthesizeCw, synthesizeFmcw } from './scene.js';

// Params that only change processing (next frame), and those written straight
// to the hardware; anything else re-enters FMCW. phaser_headless's sets.
const FMCW_LIVE_KEYS = new Set([
    'mti', 'fft_window', 'range_max', 'vel_max', 'zero_range_freq', 'peak_exclude',
    'cfar', 'cfar_guard', 'cfar_ref', 'cfar_bias_db',
]);
const FMCW_WRITE_KEYS = new Set(['taper', 'rx_gain', 'tx_gain']);

const CAPABILITY = Object.freeze({
    available: true, ramp: RAMP_SYNCED, source: 'sim', reason: '', checks: [],
});

const now = () => (typeof performance !== 'undefined' ? performance.now() : Date.now());

export function createRadarEngine({ noiseSigma = NOISE_SIGMA, seed = 0 } = {}) {
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

    function fmcwFrame() {
        const cfg = s.fmcwCfg, plan = s.fmcwPlan;
        const t0 = now();
        const chans = synthesizeFmcw({
            n: s.bufferSize, fs: s.sampleRate,
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
        const data = sliceChirps(finishCapture(chans, { noiseSigma, gauss }), plan);
        const t1 = now();

        const fs = plan.sample_rate;
        const ramp = plan.effective_ramp_time;
        const zero = cfg.zero_range_freq ?? cfg.signal_freq;
        const fftWindow = cfg.fft_window ?? 'blackman';
        const out = {
            preset: s.fmcwPreset,
            ramp_mode: plan.ramp_mode,
            num_chirps: plan.num_chirps,
            pri_ms: plan.pri_ms,
            ramp_time: plan.ramp_time,
        };
        if (plan.synced && plan.num_chirps > 1) {
            const matrix = mtiFilter(data, cfg.mti ?? 'none');
            const rd = rangeDopplerMap(matrix, fs, cfg.chirp_bw, ramp, plan.pri_ms / 1e3,
                cfg.output_freq, {
                    zeroRangeFreq: zero, fftWindow, rangeMax: cfg.range_max,
                    velMax: cfg.vel_max, peakExclude: cfg.peak_exclude ?? 0,
                });
            Object.assign(out, {
                kind: 'range_doppler',
                peak_range: rd.peak_range,
                cal_peak_range: rd.cal_peak_range,
                peak_velocity: rd.peak_velocity,
                peak_magnitude_db: rd.peak_magnitude_db,
                range_resolution: rd.range_resolution,
                velocity_resolution: rd.velocity_resolution,
                max_velocity: rd.max_velocity,
                map: encodeDbU8(rd.map_db),
                range_axis: rounded(rd.range_axis, AXIS_DECIMALS),
                velocity_axis: rounded(rd.velocity_axis, AXIS_DECIMALS),
            });
        } else {
            const prof = rangeProfile(data[0], fs, cfg.chirp_bw, ramp, {
                zeroRangeFreq: zero, fftWindow, rangeMax: cfg.range_max,
            });
            Object.assign(out, {
                kind: 'range_profile',
                peak_range: prof.peak_range,
                cal_peak_range: prof.peak_range,
                peak_magnitude_db: prof.peak_magnitude_db,
                range_resolution: prof.range_resolution,
                max_range: prof.max_range,
                range_axis: rounded(prof.range_axis, AXIS_DECIMALS),
                profile_db: rounded(prof.profile_db, DB_DECIMALS),
            });
            if (cfg.cfar) {
                const spectrum = prof.profile_db;
                const threshold = caCfar(spectrum, cfg.cfar_guard, cfg.cfar_ref, cfg.cfar_bias_db);
                const detections = cfarDetections(spectrum, threshold);
                out.detections = detections.length;
                out.cfar_threshold_db = rounded(threshold, DB_DECIMALS);
                out.detection_indices = detections;
            }
        }
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
