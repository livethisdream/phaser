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

const FRAME_MS = 50;          // the backend's 20 fps cap
const BURST_PRI_MAX = 5e-3;   // s; closer chirps than this are a burst
const RD_WINDOW = 256;        // chirps per range-Doppler frame
const BURST_REPEAT_MS = 250;  // a single burst, re-shown at this rate

let rec = null;               // {cols, fs, fc, B, T_ramp, PRI, f0, mode, window, hop}
let cfg = {};
let pos = 0;
let timer = null;

/** The playback plan and frame layout for a capture. */
export function playbackLayout({ nChirps, PRI }) {
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
        cfar_guard: 4, cfar_ref: 16, cfar_bias_db: 6,
        // An empty field (null) means "the default", not "off".
        ...Object.fromEntries(Object.entries(settings).filter(([, v]) => v != null)),
    };
    const out = fmcwPayload(cols, c, plan, 'playback');
    out.playback = { chirp: start, chirps: r.cols.length, t: start * r.PRI };
    return out;
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
    const cols = [];
    for (let c = 0; c < nChirps; c++) {
        cols.push({
            re: Float64Array.from(iq.data.re.subarray(c * nSamples, (c + 1) * nSamples)),
            im: Float64Array.from(iq.data.im.subarray(c * nSamples, (c + 1) * nSamples)),
        });
    }
    const v = Object.fromEntries(need.map(k => [k, arrays[k].data]));
    return { cols, nSamples, ...v, ...playbackLayout({ nChirps, PRI: v.PRI }) };
}

function step() {
    timer = null;
    if (!rec) return;
    try {
        self.postMessage({ type: 'fmcw_radar_frame', data: playbackFrame(rec, cfg, pos) });
    } catch (err) {
        self.postMessage({ type: 'error', message: String(err?.message || err) });
    }
    if (rec.hop) {
        pos += rec.hop;
        if (pos + rec.window > rec.cols.length) pos = 0;   // loop
    }
    timer = setTimeout(step, rec.interval);
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
            self.postMessage({
                type: 'loaded',
                info: {
                    mode: rec.mode, chirps: rec.cols.length, samples: rec.nSamples,
                    window: rec.window, interval: rec.interval,
                    fs: rec.fs, fc: rec.fc, B: rec.B, T_ramp: rec.T_ramp, PRI: rec.PRI, f0: rec.f0,
                },
            });
            step();
        } else if (msg.cmd === 'settings') {
            Object.assign(cfg, msg.settings || {});
        } else if (msg.cmd === 'stop') {
            if (timer) clearTimeout(timer);
            timer = null;
            rec = null;
        }
    };
}
