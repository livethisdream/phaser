/**
 * rng.js — seeded pseudo-random numbers for the browser simulator.
 *
 * Deliberately NOT trying to reproduce NumPy's PCG64 stream. That is not
 * feasible in JS, and it is why tests/test_sim_parity.py compares the two sims
 * with noise switched off: only the deterministic physics can be checked
 * sample-for-sample. What matters here is that the noise has the right
 * distribution and that a given seed replays identically, so a sweep in the
 * browser is reproducible.
 */

/** xorshift128+ — fast, seedable, good enough for additive Gaussian noise. */
export function createRng(seed = 0) {
    // splitmix64-style seeding so a small integer seed still fills the state.
    let s0 = (seed >>> 0) || 0x9e3779b9;
    let s1 = ((seed * 0x85ebca6b) >>> 0) || 0x6a09e667;
    let s2 = 0x243f6a88;
    let s3 = 0x13198a2e;

    function next() {
        // xorshift128 on four 32-bit words.
        let t = s3;
        const s = s0;
        s3 = s2;
        s2 = s1;
        s1 = s;
        t ^= t << 11; t >>>= 0;
        t ^= t >>> 8;
        s0 = (t ^ s ^ (s >>> 19)) >>> 0;
        return s0;
    }

    let spare = null;

    return {
        /** Uniform in [0, 1). */
        uniform() {
            return next() / 4294967296;
        },
        /**
         * Standard normal via Box-Muller. Pairs are generated together and the
         * second is cached, so the cost amortizes to one transcendental per
         * two samples -- this is the hot path when filling a 16k IQ buffer.
         */
        normal() {
            if (spare !== null) {
                const v = spare;
                spare = null;
                return v;
            }
            let u = 0;
            let v = 0;
            let s = 0;
            do {
                u = this.uniform() * 2 - 1;
                v = this.uniform() * 2 - 1;
                s = u * u + v * v;
            } while (s >= 1 || s === 0);
            const scale = Math.sqrt((-2 * Math.log(s)) / s);
            spare = v * scale;
            return u * scale;
        },
    };
}
