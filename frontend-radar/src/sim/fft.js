/**
 * fft.js — the numpy.fft pieces the radar processing uses, at any length.
 *
 * Radar FFT lengths are not powers of two: a range_doppler chirp is 540
 * samples, padded to a 5-smooth length (phaser_radar_dsp._fast_len), and MTI
 * leaves 255 or 254 chirps. Powers of two take the radix-2 path; anything else
 * goes through Bluestein's algorithm on a power-of-two convolution, which
 * gives numpy's exact DFT (to rounding) at any n.
 *
 * Buffers are split real/imaginary Float64Array pairs.
 */

function isPow2(n) { return (n & (n - 1)) === 0; }

/** In-place radix-2 FFT. `sign` -1 forward, +1 inverse (unscaled). */
function radix2(re, im, sign = -1) {
    const n = re.length;
    for (let i = 1, j = 0; i < n; i++) {
        let bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) {
            let t = re[i]; re[i] = re[j]; re[j] = t;
            t = im[i]; im[i] = im[j]; im[j] = t;
        }
    }
    for (let len = 2; len <= n; len <<= 1) {
        const half = len >> 1;
        const ang = (sign * 2 * Math.PI) / len;
        // Twiddles by direct cos/sin, not by recurrence: a recurrence drifts
        // over 2^18 points far more than numpy does.
        const wr = new Float64Array(half);
        const wi = new Float64Array(half);
        for (let k = 0; k < half; k++) { wr[k] = Math.cos(ang * k); wi[k] = Math.sin(ang * k); }
        for (let i = 0; i < n; i += len) {
            for (let k = 0; k < half; k++) {
                const a = i + k, b = a + half;
                const br = re[b] * wr[k] - im[b] * wi[k];
                const bi = re[b] * wi[k] + im[b] * wr[k];
                re[b] = re[a] - br; im[b] = im[a] - bi;
                re[a] += br; im[a] += bi;
            }
        }
    }
}

// Bluestein chirps, per length: a range-Doppler frame runs hundreds of FFTs
// of the same two lengths.
const bluesteinCache = new Map();

function bluesteinPlan(n) {
    let plan = bluesteinCache.get(n);
    if (plan) return plan;
    let m = 1;
    while (m < 2 * n - 1) m <<= 1;
    const cr = new Float64Array(n), ci = new Float64Array(n);
    for (let k = 0; k < n; k++) {
        // k^2 mod 2n keeps the angle small, so the phase stays exact.
        const a = (Math.PI * ((k * k) % (2 * n))) / n;
        cr[k] = Math.cos(a); ci[k] = -Math.sin(a);
    }
    const br = new Float64Array(m), bi = new Float64Array(m);
    br[0] = cr[0]; bi[0] = -ci[0];
    for (let k = 1; k < n; k++) {
        br[k] = br[m - k] = cr[k];
        bi[k] = bi[m - k] = -ci[k];
    }
    radix2(br, bi, -1);
    plan = { m, cr, ci, br, bi };
    bluesteinCache.set(n, plan);
    return plan;
}

function bluestein(re, im) {
    const n = re.length;
    const { m, cr, ci, br, bi } = bluesteinPlan(n);
    const ar = new Float64Array(m), ai = new Float64Array(m);
    for (let k = 0; k < n; k++) {
        ar[k] = re[k] * cr[k] - im[k] * ci[k];
        ai[k] = re[k] * ci[k] + im[k] * cr[k];
    }
    radix2(ar, ai, -1);
    for (let k = 0; k < m; k++) {
        const r = ar[k] * br[k] - ai[k] * bi[k];
        ai[k] = ar[k] * bi[k] + ai[k] * br[k];
        ar[k] = r;
    }
    radix2(ar, ai, 1);
    for (let k = 0; k < n; k++) {
        const r = ar[k] / m, i = ai[k] / m;
        re[k] = r * cr[k] - i * ci[k];
        im[k] = r * ci[k] + i * cr[k];
    }
}

/**
 * np.fft.fft(x, n): a new {re, im} of length n (default the input's), the
 * input zero-padded or truncated to n first.
 */
export function fft(re, im, n = re.length) {
    const outRe = new Float64Array(n), outIm = new Float64Array(n);
    const k = Math.min(n, re.length);
    outRe.set(re.subarray(0, k));
    if (im) outIm.set(im.subarray(0, k));
    if (n <= 1) return { re: outRe, im: outIm };
    if (isPow2(n)) radix2(outRe, outIm, -1);
    else bluestein(outRe, outIm);
    return { re: outRe, im: outIm };
}

/** np.fft.fftshift for a 1-D array. */
export function fftshift(arr) {
    const n = arr.length;
    const half = Math.floor(n / 2);
    const out = new Float64Array(n);
    // numpy rolls by n // 2: the second half (from index ceil(n/2)) comes first.
    const start = n - half;
    out.set(arr.subarray(start), 0);
    out.set(arr.subarray(0, start), half);
    return out;
}

/** np.fft.fftfreq(n, d). */
export function fftfreq(n, d) {
    const out = new Float64Array(n);
    const half = Math.floor((n - 1) / 2) + 1;
    for (let i = 0; i < half; i++) out[i] = i / (n * d);
    for (let i = half; i < n; i++) out[i] = (i - n) / (n * d);
    return out;
}

/** phaser_radar_dsp.window: np.hanning / np.hamming / np.blackman / ones. */
export function window(name, n) {
    const w = new Float64Array(n);
    const kind = String(name || '').toLowerCase();
    if (n === 1 || kind === 'rect' || kind === 'none') { w.fill(1); return w; }
    for (let i = 0; i < n; i++) {
        const x = (2 * Math.PI * i) / (n - 1);
        if (kind === 'hann' || kind === 'hanning') w[i] = 0.5 - 0.5 * Math.cos(x);
        else if (kind === 'hamming') w[i] = 0.54 - 0.46 * Math.cos(x);
        else w[i] = 0.42 - 0.5 * Math.cos(x) + 0.08 * Math.cos(2 * x);
    }
    return w;
}
