/**
 * fft.js — the numpy.fft pieces do_sweep() uses, and nothing more.
 *
 * Hand-rolled rather than pulled from a library: the built page must stay
 * self-contained (CI fails the build on any external <script src>), and this is
 * a few dozen lines against a dependency that would need vendoring.
 *
 * Buffers are split real/imaginary Float64Array pairs throughout, which is what
 * the synthesis path in sdr.js produces.
 */

/** np.blackman(N). */
export function blackman(n) {
    const w = new Float64Array(n);
    if (n === 1) { w[0] = 1; return w; }
    for (let i = 0; i < n; i++) {
        const x = (2 * Math.PI * i) / (n - 1);
        w[i] = 0.42 - 0.5 * Math.cos(x) + 0.08 * Math.cos(2 * x);
    }
    return w;
}

/**
 * In-place iterative radix-2 complex FFT.
 *
 * Requires a power-of-two length -- the sim's buffer_size is 1024*16, and the
 * caller trims to a power of two if that ever stops being true.
 */
export function fft(re, im) {
    const n = re.length;
    if (n <= 1) return;
    if ((n & (n - 1)) !== 0) {
        throw new Error(`fft: length ${n} is not a power of two`);
    }

    // Bit-reversal permutation.
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
        const ang = (-2 * Math.PI) / len;
        const wRe = Math.cos(ang);
        const wIm = Math.sin(ang);
        for (let i = 0; i < n; i += len) {
            let curRe = 1;
            let curIm = 0;
            const half = len >> 1;
            for (let k = 0; k < half; k++) {
                const aRe = re[i + k];
                const aIm = im[i + k];
                const bRe = re[i + k + half] * curRe - im[i + k + half] * curIm;
                const bIm = re[i + k + half] * curIm + im[i + k + half] * curRe;
                re[i + k] = aRe + bRe;
                im[i + k] = aIm + bIm;
                re[i + k + half] = aRe - bRe;
                im[i + k + half] = aIm - bIm;
                const nextRe = curRe * wRe - curIm * wIm;
                curIm = curRe * wIm + curIm * wRe;
                curRe = nextRe;
            }
        }
    }
}

/** np.fft.fftshift for a 1-D array (moves the zero-frequency bin to centre). */
export function fftshift(arr) {
    const n = arr.length;
    const half = Math.ceil(n / 2);
    const out = new Float64Array(n);
    out.set(arr.subarray(half), 0);
    out.set(arr.subarray(0, half), n - half);
    return out;
}

/** np.fft.fftfreq(n, ts). */
export function fftfreq(n, ts) {
    const out = new Float64Array(n);
    const denom = n * ts;
    const half = Math.floor((n - 1) / 2) + 1;
    for (let i = 0; i < half; i++) out[i] = i / denom;
    for (let i = half; i < n; i++) out[i] = (i - n) / denom;
    return out;
}
