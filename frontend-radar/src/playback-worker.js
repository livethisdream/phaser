/**
 * playback-worker.js — replays a recorded capture (the course .npz) through
 * the GUI's FMCW processing, as if it were live.
 *
 * The recording's own fs, B, T_ramp, PRI, fc and f0 set the waveform; the
 * pane's processing controls (window, range crop, 0 m calibration, MTI,
 * velocity crop, CFAR) apply to it frame by frame, exactly as they do to a
 * kit's frames -- the processing is frontend-radar/src/sim/dsp.js's
 * fmcwPayload(), the backend's own, ported and parity-tested.
 *
 * How a recording becomes frames:
 *   bursts   a run of bursts (burst_len, burst_t): one burst a frame, at the
 *            recorded pace.
 *   burst    chirps a PRI of at most 5 ms apart (a range-Doppler burst, or a
 *            continuous record): range-Doppler frames of up to 256 chirps.
 *            A record longer than that is walked through in real time, a
 *            window sliding ~50 ms of chirps a frame; a single burst repeats.
 *   profile  chirps further apart (the L28 range captures, 50 ms): one range
 *            profile per frame, in real time, so the range waterfall builds
 *            as it did when it was recorded.
 * Either way the recording loops.
 */

import { fmcwPayload } from './sim/dsp.js';
import { CAPTURE_FULL_SCALE, CAPTURE_COUNTS_ABOVE, FMCW_PROC_DEFAULTS } from './sim/constants.generated.js';

const FRAME_MS = 50;          // the backend's 20 fps cap
const BURST_PRI_MAX = 5e-3;   // s; closer chirps than this are a burst
const RD_WINDOW = 256;        // chirps per range-Doppler frame
const BURST_REPEAT_MS = 250;  // a single burst, re-shown at this rate

let rec = null;               // {cols, fs, fc, B, T_ramp, PRI, f0, mode, window, hop}
let cfg = {};
let pos = 0;
let timer = null;
let paused = false;
let shown = 0;                // start chirp of the frame last posted

/** The playback plan and frame layout for a capture. A run of bursts (the
    file's burst_len, burst_t) plays a burst a frame, at the recorded pace. */
export function playbackLayout({ nChirps, PRI, burstLen = null, burstT = null }) {
    if (burstLen && burstLen >= 2 && nChirps >= burstLen) {
        const gaps = [];
        for (let i = 1; i < (burstT?.length || 0); i++) gaps.push(burstT[i] - burstT[i - 1]);
        gaps.sort((a, b) => a - b);
        const gap = gaps.length ? gaps[gaps.length >> 1] : BURST_REPEAT_MS / 1e3;
        return { mode: 'burst', window: burstLen, hop: burstLen, interval: Math.max(FRAME_MS, gap * 1e3),
                 bursts: Math.floor(nChirps / burstLen) };
    }
    const burst = nChirps >= 2 && PRI <= BURST_PRI_MAX;
    if (burst) {
        const window = Math.min(nChirps, RD_WINDOW);
        const repeat = nChirps <= window;
        const hop = repeat ? 0 : Math.max(1, Math.round(FRAME_MS / 1e3 / PRI));
        return { mode: 'burst', window, hop, interval: repeat ? BURST_REPEAT_MS : Math.max(FRAME_MS, hop * PRI * 1e3) };
    }
    const hop = Math.max(1, Math.round(FRAME_MS / 1e3 / Math.max(PRI, 1e-6)));
    return { mode: 'profile', window: 1, hop, interval: Math.max(FRAME_MS, hop * PRI * 1e3) };
}

/** The capture's frame starting at chirp `start`, as the GUI's frame payload. */
export function playbackFrame(r, settings, start) {
    const cols = r.cols.slice(start, start + r.window);
    const plan = {
        synced: true,
        ramp_mode: 'playback',
        num_chirps: cols.length,
        pri_ms: r.PRI * 1e3,
        sample_rate: r.fs,
        ramp_time: r.T_ramp,
        effective_ramp_time: r.T_ramp,
    };
    const c = {
        chirp_bw: r.B,
        output_freq: r.fc,
        signal_freq: r.f0,        // the recording's 0 m frequency...
        zero_range_freq: null,    // ...unless the pane's 0 m calibration overrides it
        range_max: 20,
        peak_exclude: 0.3,
        peak_snr_db: FMCW_PROC_DEFAULTS.peak_snr_db,
        clutter_margin_db: FMCW_PROC_DEFAULTS.clutter_margin_db,
        cfar_guard: 4, cfar_ref: 16, cfar_bias_db: 6,
        // An empty field (null) means "the default", not "off".
        ...Object.fromEntries(Object.entries(settings).filter(([, v]) => v != null)),
    };
    const out = fmcwPayload(cols, c, plan, 'playback');
    out.playback = { chirp: start, chirps: r.cols.length, t: start * r.PRI };
    return out;
}

/** 'full_scale' (the course's units) or 'counts' (an older raw save), by the peak |iq|. */
export function playbackUnits({ re, im }) {
    let peak2 = 0;
    for (let i = 0; i < re.length; i++) peak2 = Math.max(peak2, re[i] * re[i] + im[i] * im[i]);
    return Math.sqrt(peak2) > CAPTURE_COUNTS_ABOVE ? 'counts' : 'full_scale';
}

/** {iq: {shape, data: {re, im}}, fs, ...} (readNpz's output, scalars as numbers) -> rec. */
export function playbackRecording(arrays) {
    const iq = arrays.iq;
    if (!iq || !iq.dtype?.startsWith('<c') || iq.shape.length !== 2) {
        throw new Error('not a course capture: iq must be a 2-D complex array (chirps x samples)');
    }
    const need = ['fs', 'fc', 'B', 'T_ramp', 'PRI', 'f0'];
    const missing = need.filter(k => typeof arrays[k]?.data !== 'number');
    if (missing.length) throw new Error(`not a course capture: missing ${missing.join(', ')}`);
    const [nChirps, nSamples] = iq.shape;
    // A course file is in full-scale units (|iq| = 1 is 0 dBFS); the DSP
    // reads ADC counts. A peak above CAPTURE_COUNTS_ABOVE cannot be full
    // scale -- it is a save from before captures were -- so it plays as is.
    const units = playbackUnits(iq.data);
    const k = units === 'full_scale' ? CAPTURE_FULL_SCALE : 1;
    const cols = [];
    for (let c = 0; c < nChirps; c++) {
        const re = new Float64Array(nSamples), im = new Float64Array(nSamples);
        for (let i = 0; i < nSamples; i++) {
            re[i] = iq.data.re[c * nSamples + i] * k;
            im[i] = iq.data.im[c * nSamples + i] * k;
        }
        cols.push({ re, im });
    }
    const v = Object.fromEntries(need.map(k => [k, arrays[k].data]));
    const burstLen = typeof arrays.burst_len?.data === 'number' ? Math.trunc(arrays.burst_len.data) : null;
    const burstT = arrays.burst_t?.data ? Array.from(arrays.burst_t.data) : null;
    return { cols, nSamples, units, ...v, ...playbackLayout({ nChirps, PRI: v.PRI, burstLen, burstT }) };
}

/* The frame on screen, again: a setting changed while paused. */
function redraw() {
    try {
        self.postMessage({ type: 'fmcw_radar_frame', data: playbackFrame(rec, cfg, shown) });
    } catch (err) {
        self.postMessage({ type: 'error', message: String(err?.message || err) });
    }
}

function step() {
    timer = null;
    if (!rec) return;
    try {
        shown = pos;
        self.postMessage({ type: 'fmcw_radar_frame', data: playbackFrame(rec, cfg, pos) });
    } catch (err) {
        self.postMessage({ type: 'error', message: String(err?.message || err) });
    }
    if (rec.hop) {
        pos += rec.hop;
        if (pos + rec.window > rec.cols.length) pos = 0;   // loop
    }
    if (!paused) timer = setTimeout(step, rec.interval);
}

if (typeof self !== 'undefined' && typeof self.postMessage === 'function' && typeof window === 'undefined') {
    self.onmessage = (event) => {
        const msg = event.data || {};
        if (msg.cmd === 'load') {
            if (timer) clearTimeout(timer);
            try {
                rec = playbackRecording(msg.arrays);
            } catch (err) {
                self.postMessage({ type: 'error', message: String(err?.message || err), fatal: true });
                return;
            }
            cfg = { ...(msg.settings || {}) };
            pos = 0;
            paused = false;
            self.postMessage({
                type: 'loaded',
                info: {
                    mode: rec.mode, chirps: rec.cols.length, samples: rec.nSamples,
                    window: rec.window, interval: rec.interval, units: rec.units, bursts: rec.bursts || 0,
                    fs: rec.fs, fc: rec.fc, B: rec.B, T_ramp: rec.T_ramp, PRI: rec.PRI, f0: rec.f0,
                },
            });
            step();
        } else if (msg.cmd === 'settings') {
            Object.assign(cfg, msg.settings || {});
            // Paused, the frame on screen is redrawn with the new settings.
            if (paused && rec) redraw();
        } else if (msg.cmd === 'pause') {
            paused = true;
            if (timer) clearTimeout(timer);
            timer = null;
        } else if (msg.cmd === 'resume') {
            if (paused && rec) { paused = false; if (!timer) step(); }
        } else if (msg.cmd === 'stop') {
            if (timer) clearTimeout(timer);
            timer = null;
            rec = null;
        }
    };
}
