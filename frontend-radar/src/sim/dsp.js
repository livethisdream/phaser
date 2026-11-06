/**
 * dsp.js — a port of the phaser_radar_dsp.py functions the radar UI's frames
 * are built from. Python is the source of truth; tests/test_radar_sim_parity.py
 * runs both on the same noiseless scene and fails when they part.
 *
 * Complex buffers are {re, im} Float64Array pairs. A chirp matrix is an array
 * of columns, one {re, im} per chirp (slow time), each samples long (fast time)
 * -- the transpose-free layout of phaser_radar_dsp's samples x chirps matrix.
 */

import { fft, fftshift, fftfreq, window } from './fft.js';
import {
    C, DEFAULTS, TAPER_PRESETS, TAPER_MAX, FMCW_DEFAULTS, FMCW_HW_DEFAULTS,
    FMCW_PRESETS, FMCW_PROC_DEFAULTS, MAINLOBE_HALF_BINS, MAP_SPAN_DB, DB_DECIMALS,
    AXIS_DECIMALS, CAPTURE_SINGLE_CHIRP_FRAMES, CAPTURE_MAX_FRAMES_S, CAPTURE_FRAME_S,
    BYTES_PER_SAMPLE, CAPTURE_BURST_INTERVAL_S, CAPTURE_MAX_BYTES,
    RAMP_SYNCED, RAMP_FREE_RUNNING, RANGE_TIME_SCALE, MAX_BURST_BUFFER,
    RAMP_SETTLE_FRACTION,
} from './constants.generated.js';

export { window };

/** Python's round() and np.rint: ties to even. */
export function roundHalfEven(x) {
    const r = Math.round(x);
    return (Math.abs(x % 1) === 0.5 && r % 2 !== 0) ? r - 1 : r;
}

/** np.round(v, decimals), near enough for a payload. */
export function rounded(values, decimals) {
    const s = 10 ** decimals;
    return Array.from(values, v => roundHalfEven(v * s) / s);
}

function argmax(arr) {
    let best = 0;
    for (let i = 1; i < arr.length; i++) if (arr[i] > arr[best]) best = i;
    return best;
}

function sum(arr) {
    let s = 0;
    for (let i = 0; i < arr.length; i++) s += arr[i];
    return s;
}

export function resolveTaper(taper) {
    if (taper == null) return [...TAPER_PRESETS.blackman];
    if (typeof taper === 'string') {
        return [...(TAPER_PRESETS[taper.toLowerCase()] || TAPER_PRESETS.blackman)];
    }
    if (!Array.isArray(taper) || !taper.length) return [...TAPER_PRESETS.blackman];
    const values = taper.map(v => roundHalfEven(Number(v)));
    if (values.some(v => !Number.isFinite(v))) return [...TAPER_PRESETS.blackman];
    return [...values, 0, 0, 0, 0, 0, 0, 0, 0].slice(0, 8)
        .map(v => Math.max(0, Math.min(TAPER_MAX, v)));
}

// --- CW ----------------------------------------------------------------------

export function processCwFrame(iq, fs, signalFreq, outputFreq, {
    fftWindow = 'blackman', downsampleIq = 512, velMax = null, peakExclude = null,
} = {}) {
    const n = iq.re.length;
    if (n === 0) throw new Error('Empty IQ buffer');
    const win = window(fftWindow, n);
    const xr = new Float64Array(n), xi = new Float64Array(n);
    for (let i = 0; i < n; i++) { xr[i] = iq.re[i] * win[i]; xi[i] = iq.im[i] * win[i]; }
    const X = fft(xr, xi);
    const sp = new Float64Array(n);
    for (let i = 0; i < n; i++) sp[i] = Math.hypot(X.re[i], X.im[i]);
    const shifted = fftshift(sp);
    const ws = sum(win);
    const freq = fftshift(fftfreq(n, 1 / fs));
    const fc = outputFreq ? Number(outputFreq) : 1;

    const vmax = Math.abs(Number(velMax ?? DEFAULTS.vel_max));
    const velocity = new Float64Array(n), spectrum = new Float64Array(n);
    const keep = [];
    for (let i = 0; i < n; i++) {
        velocity[i] = (C * (freq[i] - signalFreq)) / (2 * fc);
        spectrum[i] = 20 * Math.log10(Math.max(shifted[i] / ws, 1e-15) / 2 ** 11);
        if (Math.abs(velocity[i]) <= vmax) keep.push(i);
    }
    if (!keep.length) {
        const center = Math.floor(n / 2), half = Math.min(512, center);
        for (let i = center - half; i < center + half; i++) keep.push(i);
    }
    const velOut = keep.map(i => velocity[i]);
    const specOut = keep.map(i => spectrum[i]);

    const pe = Math.abs(Number(peakExclude ?? DEFAULTS.peak_exclude));
    let search = velOut.map(v => Math.abs(v) > pe);
    if (!search.some(Boolean)) search = search.map(() => true);
    let peak = -1;
    for (let i = 0; i < specOut.length; i++) {
        if (search[i] && (peak < 0 || specOut[i] > specOut[peak])) peak = i;
    }

    const out = {
        velocity_axis: velOut,
        spectrum_db: specOut,
        peak_velocity: velOut[peak],
        peak_magnitude_db: specOut[peak],
        peak_exclude: pe,
        n_samples: n,
        sample_rate: Number(fs),
        signal_freq: Number(signalFreq),
        output_freq: Number(outputFreq),
    };
    if (downsampleIq && n > downsampleIq) {
        const step = Math.max(1, Math.floor(n / downsampleIq));
        const mag = [];
        for (let i = 0; i < n && mag.length < downsampleIq; i += step) {
            mag.push(Math.hypot(iq.re[i], iq.im[i]));
        }
        out.iq_mag = mag;
    }
    return out;
}

// --- FMCW relations ------------------------------------------------------------

export function fmcwSlope(chirpBw, rampTime) {
    if (!(rampTime > 0)) throw new Error('ramp_time must be positive');
    return Number(chirpBw) / Number(rampTime);
}

export function rangeResolution(chirpBw) { return C / (2 * Number(chirpBw)); }

export function maxUnambiguousRange(fs, chirpBw, rampTime) {
    return (C * (fs / 2)) / (2 * fmcwSlope(chirpBw, rampTime));
}

export function velocityResolution(numChirps, pri, outputFreq) {
    const dwell = Number(numChirps) * Number(pri);
    if (!(dwell > 0)) throw new Error('num_chirps and pri must be positive');
    return C / (2 * Number(outputFreq) * dwell);
}

export function maxUnambiguousVelocity(pri, outputFreq) {
    return C / (4 * Number(outputFreq) * Number(pri));
}

/** Positive half of an n-point FFT, as range. */
function rangeBins(n, fs, chirpBw, rampTime, zeroRangeFreq) {
    const slope = fmcwSlope(chirpBw, rampTime);
    const half = Math.floor(n / 2);
    const out = new Float64Array(half);
    for (let k = 0; k < half; k++) {
        out[k] = (C * ((k * fs) / n - Number(zeroRangeFreq))) / (2 * slope);
    }
    return out;
}

function toDb(mag, winSum) {
    return 20 * Math.log10(Math.max(mag / winSum, 1e-15) / 2 ** 11);
}

/** phaser_radar_dsp._fast_len: the next length with no prime factor above 5. */
export function fastLen(n) {
    let k = Math.max(Math.trunc(n), 1);
    for (;;) {
        let m = k;
        for (const p of [2, 3, 5]) while (m % p === 0) m /= p;
        if (m === 1) return k;
        k += 1;
    }
}

function rangeMask(ranges, rangeMin, rangeMax) {
    let idx = [];
    for (let i = 0; i < ranges.length; i++) {
        if (ranges[i] >= rangeMin && (rangeMax == null || ranges[i] <= rangeMax)) idx.push(i);
    }
    if (!idx.length) idx = Array.from(ranges, (_, i) => i);
    return idx;
}

export function rangeProfile(iq, fs, chirpBw, rampTime, {
    zeroRangeFreq = 0, fftWindow = 'blackman', rangeMax = null, rangeMin = 0, peakMin = 0,
} = {}) {
    const n = iq.re.length;
    if (n < 2) throw new Error('need at least 2 samples for a range profile');
    const win = window(fftWindow, n);
    const xr = new Float64Array(n), xi = new Float64Array(n);
    for (let i = 0; i < n; i++) { xr[i] = iq.re[i] * win[i]; xi[i] = iq.im[i] * win[i]; }
    const X = fft(xr, xi);
    const ws = sum(win);
    const ranges = rangeBins(n, fs, chirpBw, rampTime, zeroRangeFreq);
    const idx = rangeMask(ranges, Number(rangeMin), rangeMax == null ? null : Number(rangeMax));
    const rangeOut = idx.map(i => ranges[i]);
    const profile = idx.map(i => toDb(Math.hypot(X.re[i], X.im[i]), ws));
    // The readout skips the leakage closer than peakMin; Calibrate reads the
    // whole band, below 0 m and past the crop (phaser_radar_dsp.range_profile).
    // Half a bin of slack: a target at the cutoff itself still counts.
    const pm = Number(peakMin || 0) - (ranges.length > 1 ? 0.5 * Math.abs(ranges[1] - ranges[0]) : 0);
    let peak = -1;
    for (let k = 0; k < profile.length; k++) {
        if (rangeOut[k] >= pm && (peak < 0 || profile[k] > profile[peak])) peak = k;
    }
    if (peak < 0) peak = argmax(profile);
    let cal = 0, calMag = -1;
    for (let i = 0; i < ranges.length; i++) {
        const m = Math.hypot(X.re[i], X.im[i]);
        if (m > calMag) { calMag = m; cal = i; }
    }
    return {
        range_axis: rangeOut,
        profile_db: profile,
        peak_range: rangeOut[peak],
        cal_peak_range: ranges[cal],
        peak_magnitude_db: profile[peak],
        range_resolution: rangeResolution(chirpBw),
        max_range: maxUnambiguousRange(fs, chirpBw, rampTime),
        n_samples: n,
    };
}

/** Pulse-cancel along slow time. Each stage costs one chirp. */
export function mtiFilter(cols, mode = 'none') {
    const m = String(mode || 'none').toLowerCase();
    if (m === 'none' || m === 'off' || m === '') return cols;
    const sub = (fn, drop) => {
        if (cols.length < drop + 1) throw new Error(`${m} MTI needs at least ${drop + 1} chirps`);
        const out = [];
        for (let c = 0; c + drop < cols.length; c++) {
            const n = cols[c].re.length;
            const re = new Float64Array(n), im = new Float64Array(n);
            for (let i = 0; i < n; i++) {
                const [r, j] = fn(c, i);
                re[i] = r; im[i] = j;
            }
            out.push({ re, im });
        }
        return out;
    };
    if (m === '2pulse') {
        return sub((c, i) => [cols[c + 1].re[i] - cols[c].re[i],
                              cols[c + 1].im[i] - cols[c].im[i]], 1);
    }
    if (m === '3pulse') {
        return sub((c, i) => [
            cols[c + 2].re[i] - 2 * cols[c + 1].re[i] + cols[c].re[i],
            cols[c + 2].im[i] - 2 * cols[c + 1].im[i] + cols[c].im[i]], 2);
    }
    throw new Error(`unknown MTI mode '${mode}'`);
}

/**
 * The 2D FFT of a chirp matrix -> range vs velocity, as range_doppler_map():
 * crop to the displayed range bins before the slow-time FFT, pad the fast-time
 * FFT to a 5-smooth length, and keep the peak readout off the zero-Doppler
 * cells (peakExclude) while cal_peak_range reads the strongest cell anywhere.
 */
export function rangeDopplerMap(cols, fs, chirpBw, rampTime, pri, outputFreq, {
    zeroRangeFreq = 0, fftWindow = 'blackman', rangeMax = null, rangeMin = 0,
    velMax = null, peakExclude = 0,
} = {}) {
    const nSlow = cols.length;
    const nFast = nSlow ? cols[0].re.length : 0;
    if (nFast < 2 || nSlow < 2) throw new Error(`matrix too small for a 2D FFT: ${nFast}x${nSlow}`);

    const nFft = fastLen(nFast);
    const wFast = window(fftWindow, nFast);
    const wSlow = window(fftWindow, nSlow);
    const winSum = (sum(wFast) * sum(wSlow)) / nSlow;

    const ranges = rangeBins(nFft, fs, chirpBw, rampTime, zeroRangeFreq);
    const dopplerHz = fftshift(fftfreq(nSlow, Number(pri)));
    const velocity = Array.from(dopplerHz, f => (C * f) / (2 * Number(outputFreq)));

    const ridx = rangeMask(ranges, Number(rangeMin), rangeMax == null ? null : Number(rangeMax));
    let vidx = velocity.map((_, i) => i);
    if (velMax != null) {
        const vm = Math.abs(Number(velMax));
        const kept = vidx.filter(i => Math.abs(velocity[i]) <= vm);
        if (kept.length) vidx = kept;
    }
    const rangeOut = ridx.map(i => ranges[i]);
    const velOut = vidx.map(i => velocity[i]);

    // Fast time, chirp by chirp, keeping only the displayed range bins.
    const nr = ridx.length;
    const specRe = Array.from({ length: nr }, () => new Float64Array(nSlow));
    const specIm = Array.from({ length: nr }, () => new Float64Array(nSlow));
    const xr = new Float64Array(nFast), xi = new Float64Array(nFast);
    // Calibrate: power summed over the chirps, every range bin in the band.
    const calPow = new Float64Array(ranges.length);
    for (let c = 0; c < nSlow; c++) {
        const col = cols[c];
        for (let i = 0; i < nFast; i++) { xr[i] = col.re[i] * wFast[i]; xi[i] = col.im[i] * wFast[i]; }
        const X = fft(xr, xi, nFft);
        for (let k = 0; k < calPow.length; k++) calPow[k] += X.re[k] * X.re[k] + X.im[k] * X.im[k];
        for (let r = 0; r < nr; r++) {
            specRe[r][c] = X.re[ridx[r]] * wSlow[c];
            specIm[r][c] = X.im[ridx[r]] * wSlow[c];
        }
    }

    // Slow time, range bin by range bin.
    const mapDb = [];
    for (let r = 0; r < nr; r++) {
        const Y = fft(specRe[r], specIm[r]);
        const mag = new Float64Array(nSlow);
        for (let i = 0; i < nSlow; i++) mag[i] = Math.hypot(Y.re[i], Y.im[i]);
        const shifted = fftshift(mag);
        mapDb.push(vidx.map(i => toDb(shifted[i], winSum)));
    }

    let search = velOut.map(() => true);
    if (peakExclude) {
        const dv = velocityResolution(nSlow, pri, outputFreq);
        const lobe = MAINLOBE_HALF_BINS[String(fftWindow).toLowerCase()] ?? 3;
        const half = Math.max(lobe, Math.ceil(Math.abs(Number(peakExclude)) / dv - 1e-9));
        search = velOut.map(v => Math.abs(roundHalfEven(v / dv)) >= half);
    }
    if (!search.some(Boolean)) search = search.map(() => true);

    let pr = 0, pv = -1;
    for (let r = 0; r < nr; r++) {
        const row = mapDb[r];
        for (let v = 0; v < row.length; v++) {
            if (search[v] && (pv < 0 || row[v] > mapDb[pr][pv])) { pr = r; pv = v; }
        }
    }
    const cr = argmax(calPow);
    return {
        map_db: mapDb,
        range_axis: rangeOut,
        velocity_axis: velOut,
        peak_range: rangeOut[pr],
        cal_peak_range: ranges[cr],
        peak_velocity: velOut[pv],
        peak_magnitude_db: mapDb[pr][pv],
        range_resolution: rangeResolution(chirpBw),
        velocity_resolution: velocityResolution(nSlow, pri, outputFreq),
        max_velocity: maxUnambiguousVelocity(pri, outputFreq),
        num_chirps: nSlow,
    };
}

// --- Payload -----------------------------------------------------------------

function base64(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return typeof btoa === 'function' ? btoa(bin) : Buffer.from(bin, 'binary').toString('base64');
}

/** A 2D dB array as one byte a cell, base64, with its own scale. */
export function encodeDbU8(mapDb, spanDb = MAP_SPAN_DB) {
    const rows = mapDb.length, cols = rows ? mapDb[0].length : 0;
    let hi = -Infinity, lo = Infinity;
    for (const row of mapDb) for (const v of row) {
        const f = Math.fround(v);
        if (f > hi) hi = f;
        if (f < lo) lo = f;
    }
    if (!rows || !cols) { hi = 0; lo = 0; }
    lo = Math.max(lo, hi - spanDb);
    if (hi <= lo) lo = hi - 1;
    const q = new Uint8Array(rows * cols);
    const scale = 255 / (hi - lo);
    let k = 0;
    for (const row of mapDb) for (const v of row) {
        q[k++] = Math.max(0, Math.min(255, roundHalfEven((Math.fround(v) - lo) * scale)));
    }
    const r2 = (x) => roundHalfEven(x * 10 ** DB_DECIMALS) / 10 ** DB_DECIMALS;
    return {
        encoding: 'u8-base64', rows, cols,
        db_min: r2(lo), db_max: r2(hi), data: base64(q),
    };
}

export function caCfar(spectrumDb, numGuard = 4, numRef = 16, biasDb = 6) {
    const n = spectrumDb.length;
    const guard = Math.max(0, Math.trunc(numGuard));
    const ref = Math.trunc(numRef);
    if (ref < 1) throw new Error('num_ref must be >= 1');
    if (!n) return [];
    const power = Array.from(spectrumDb, v => 10 ** (v / 10));
    const csum = new Float64Array(n + 1);
    for (let i = 0; i < n; i++) csum[i + 1] = csum[i] + power[i];
    const maxPower = Math.max(...power);
    const half = guard + ref;
    const out = new Array(n);
    for (let i = 0; i < n; i++) {
        const loT = Math.max(i - half, 0), hiT = Math.min(i + half + 1, n);
        const loG = Math.max(i - guard, 0), hiG = Math.min(i + guard + 1, n);
        const sumRef = (csum[hiT] - csum[loT]) - (csum[hiG] - csum[loG]);
        const cnt = (hiT - loT) - (hiG - loG);
        const noise = cnt > 0 ? sumRef / Math.max(cnt, 1) : maxPower;
        out[i] = 10 * Math.log10(Math.max(noise, 1e-30)) + Number(biasDb);
    }
    return out;
}

export function cfarDetections(spectrumDb, thresholdDb) {
    const idx = [];
    for (let i = 0; i < spectrumDb.length; i++) if (spectrumDb[i] > thresholdDb[i]) idx.push(i);
    return idx;
}

// --- Config and the chirp plan -------------------------------------------------

/** Defaults <- preset <- params, in that order. */
export function fmcwConfig(params = {}, preset = null) {
    const cfg = { ...FMCW_DEFAULTS, ...FMCW_HW_DEFAULTS, ...FMCW_PROC_DEFAULTS };
    if (preset != null) {
        if (!(preset in FMCW_PRESETS)) {
            throw new Error(`unknown FMCW preset '${preset}'; expected one of ${Object.keys(FMCW_PRESETS).sort()}`);
        }
        Object.assign(cfg, FMCW_PRESETS[preset]);
    }
    return Object.assign(cfg, params || {});
}

/** The whole microseconds freq_dev_time takes; the stub ADF4159 reads it back as written. */
export function rampTimeUs(cfg) {
    const us = roundHalfEven(Number(cfg.ramp_time) * 1e6);
    if (us < 1) throw new Error(`ramp_time ${cfg.ramp_time} s is under 1 us`);
    return us;
}

export function chirpPlan(cfg, rampMode, actualRampUs, onMs = 0) {
    const fs = Number(cfg.sample_rate);
    const rampUs = Number(actualRampUs);
    if (!(fs > 0)) throw new Error('sample_rate must be positive');
    if (!(rampUs > 0)) throw new Error('the ADF4159 reported a ramp time of 0');
    const rampS = rampUs / 1e6;
    const plan = {
        ramp_mode: rampMode,
        synced: rampMode === RAMP_SYNCED,
        sample_rate: fs,
        ramp_time: rampS,
        effective_ramp_time: rampS * RANGE_TIME_SCALE[rampMode],
    };
    if (rampMode === RAMP_FREE_RUNNING) {
        const n = Math.trunc(cfg.fft_size);
        return Object.assign(plan, {
            num_chirps: 1, pri_ms: null, rx_buffer_size: n, frame_samples: n,
            start_offset_samples: 0, good_ramp_samples: n,
        });
    }
    const numChirps = Math.trunc(cfg.num_chirps);
    if (numChirps < 1) throw new Error('num_chirps must be >= 1');
    const guardMs = Number(cfg.pri_guard_ms);
    const priMs = rampUs / 1e3 + guardMs;
    const priS = priMs / 1e3;
    const frameSamples = Math.trunc(priS * fs);
    const beginOffset = RAMP_SETTLE_FRACTION * rampS;
    const good = Math.trunc((rampS - beginOffset) * fs);
    const start = Math.trunc((Number(onMs) / 1e3 + beginOffset) * fs);
    if (good < 2) throw new Error(`${rampUs} us at ${fs} S/s leaves under 2 samples per chirp`);
    if (start + good > frameSamples) {
        throw new Error('the chirp window runs past its PRI slot; raise pri_guard_ms');
    }
    const cycles = Number(cfg.signal_freq) * priS;
    const totalMs = priMs * numChirps;
    let power = 13, buffer = 2 ** power;
    while ((buffer / fs) * 1000 < totalMs) {
        power += 1;
        buffer = 2 ** power;
        if (buffer > MAX_BURST_BUFFER) {
            throw new Error(`${numChirps} chirps x ${priMs} ms at ${fs} S/s needs more than ${MAX_BURST_BUFFER} samples`);
        }
    }
    return Object.assign(plan, {
        num_chirps: numChirps,
        pri_ms: priMs,
        rx_buffer_size: buffer,
        frame_samples: frameSamples,
        start_offset_samples: start,
        good_ramp_samples: good,
        tone_cycles_per_pri: cycles,
        chirp_phase_step_deg: (cycles - roundHalfEven(cycles)) * 360,
    });
}

/** The settled part of each chirp, as columns. A free-running capture is one column. */
export function sliceChirps(iq, plan) {
    if (!plan.synced) return [iq];
    const m = plan.num_chirps, frame = plan.frame_samples;
    const start = plan.start_offset_samples, good = plan.good_ramp_samples;
    const end = start + (m - 1) * frame + good;
    if (end > iq.re.length) {
        throw new Error(`capture of ${iq.re.length} samples is shorter than the ${end} the plan needs`);
    }
    const cols = [];
    for (let c = 0; c < m; c++) {
        const s = start + c * frame;
        cols.push({ re: iq.re.slice(s, s + good), im: iq.im.slice(s, s + good) });
    }
    return cols;
}

/**
 * One FMCW frame's payload from its chirps (columns), as
 * phaser_headless.do_fmcw_radar_frame builds it: a range-Doppler map for a
 * multi-chirp synced plan, else a range profile (and CFAR). Shared by the
 * in-browser simulator and recording playback, so both are the backend's
 * processing exactly.
 */
export function fmcwPayload(data, cfg, plan, preset = null) {
    const fs = plan.sample_rate;
    const ramp = plan.effective_ramp_time;
    const zero = cfg.zero_range_freq ?? cfg.signal_freq;
    const fftWindow = cfg.fft_window ?? 'blackman';
    const out = {
        preset,
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
        const peakMin = cfg.peak_min == null ? FMCW_PROC_DEFAULTS.peak_min : Number(cfg.peak_min);
        const prof = rangeProfile(data[0], fs, cfg.chirp_bw, ramp, {
            zeroRangeFreq: zero, fftWindow, rangeMax: cfg.range_max, peakMin,
        });
        Object.assign(out, {
            kind: 'range_profile',
            peak_range: prof.peak_range,
            cal_peak_range: prof.cal_peak_range,
            peak_min: peakMin,
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
    return out;
}

/** phaser_radar_dsp.capture_limits: what Save Capture can do for this plan. */
export function captureLimits(plan, continuous = false) {
    const good = Math.trunc(plan.good_ramp_samples);
    if (plan.synced && plan.num_chirps > 1) {
        const pri = plan.pri_ms / 1e3;
        const n = Math.trunc(plan.num_chirps);
        const perS = (good * BYTES_PER_SAMPLE) / pri;
        const out = { default_s: n * pri, chirp_s: pri, burst_s: n * pri,
                      bytes_per_s: perS, bytes_per_s_iq_ch: 3 * perS };
        if (continuous) {
            const maxChirps = Math.floor(MAX_BURST_BUFFER / (plan.sample_rate * pri));
            return { kind: 'burst', max_s: maxChirps * pri, ...out };
        }
        const runS = Math.max(CAPTURE_BURST_INTERVAL_S, n * pri);
        const perBurst = n * good * BYTES_PER_SAMPLE;
        const maxBursts = Math.max(2, Math.floor(CAPTURE_MAX_BYTES / perBurst));
        return { kind: 'bursts', max_s: Math.min(CAPTURE_MAX_FRAMES_S, maxBursts * runS),
                 run_s: runS, bytes_per_burst: perBurst, ...out };
    }
    const perS = (good * BYTES_PER_SAMPLE) / CAPTURE_FRAME_S;
    return { kind: 'frames', default_s: CAPTURE_SINGLE_CHIRP_FRAMES * CAPTURE_FRAME_S,
             max_s: CAPTURE_MAX_FRAMES_S, chirp_s: CAPTURE_FRAME_S,
             bytes_per_s: perS, bytes_per_s_iq_ch: 3 * perS };
}

/** phaser_radar_dsp.capture_bursts: whole bursts in a "bursts" save; 0 for one trimmed burst. */
export function captureBursts(limits, duration) {
    if (limits.kind !== 'bursts' || duration <= limits.burst_s * (1 + 1e-9)) return 0;
    return Math.max(2, roundHalfEven(duration / limits.run_s));
}

/** phaser_radar_dsp.capture_bytes: the iq bytes a save of `duration` s writes. */
export function captureBytes(limits, duration, iqCh = false) {
    const k = iqCh ? 3 : 1;
    const n = captureBursts(limits, duration);
    return n ? k * n * limits.bytes_per_burst : k * limits.bytes_per_s * duration;
}
