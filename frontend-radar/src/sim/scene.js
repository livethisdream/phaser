/**
 * scene.js — the radar half of phaser_sim.SimSDR: what the Pluto would
 * receive in CW and FMCW radar mode, through the simulated array.
 *
 * Python is the source of truth (phaser_sim._synthesize_cw / _synthesize_fmcw
 * / _superpose_wave). One shortcut, exact up to rounding: phaser_sim adds each
 * element's copy of the waveform separately, and with the waveform shared by
 * every element that is the waveform times one complex coefficient per
 * sub-array, the sum of the elements' taper and arrival phase. Computing that
 * coefficient first makes a 2^18-sample range-Doppler buffer one pass per
 * target instead of eight.
 */

import {
    AMP_SCALE, C, ELEMENT_SPACING_M, NOISE_SIGMA, RX_GAIN_AMPLITUDE,
} from './constants.generated.js';
import { roundHalfEven } from './dsp.js';

/** xorshift128 with Box-Muller: seedable Gaussian noise (not NumPy's stream). */
export function createGauss(seed = 0) {
    let s0 = (seed >>> 0) || 0x9e3779b9, s1 = 0x6a09e667, s2 = 0x243f6a88, s3 = 0x13198a2e;
    const next = () => {
        let t = s3;
        const s = s0;
        s3 = s2; s2 = s1; s1 = s;
        t ^= t << 11; t >>>= 0;
        t ^= t >>> 8;
        s0 = (t ^ s ^ (s >>> 19)) >>> 0;
        return (s0 + 1) / 4294967297;
    };
    let spare = null;
    return () => {
        if (spare !== null) { const v = spare; spare = null; return v; }
        const u = next(), v = next();
        const r = Math.sqrt(-2 * Math.log(u));
        spare = r * Math.sin(2 * Math.PI * v);
        return r * Math.cos(2 * Math.PI * v);
    };
}

/**
 * Per sub-array coefficient for a plane wave from `angleDeg`: the sum over its
 * four elements of amplitude * A(gain) * exp(j*(phi_incident - rx_phase)).
 * `elements` are {gain, phase} -- the latched ADAR state, gain a 0-127 code.
 */
function subarrayCoefficients(elements, phaseError, angleDeg, amplitude, wavelength) {
    const theta = (angleDeg * Math.PI) / 180;
    const coef = [{ re: 0, im: 0 }, { re: 0, im: 0 }];
    for (let k = 0; k < 8; k++) {
        const el = elements[k];
        const gain = RX_GAIN_AMPLITUDE[Math.max(0, Math.min(127, Math.trunc(el.gain)))];
        const phi = 2 * Math.PI * ((k * ELEMENT_SPACING_M) / wavelength) * Math.sin(theta)
            + (phaseError[k] * Math.PI) / 180 - (el.phase * Math.PI) / 180;
        const a = amplitude * gain;
        const sub = coef[k < 4 ? 0 : 1];
        sub.re += a * Math.cos(phi);
        sub.im += a * Math.sin(phi);
    }
    return coef;
}

function addScaled(chan, coef, wr, wi, offset = 0) {
    for (let i = 0; i < wr.length; i++) {
        chan.re[offset + i] += coef.re * wr[i] - coef.im * wi[i];
        chan.im[offset + i] += coef.re * wi[i] + coef.im * wr[i];
    }
}

function emptyChannels(n) {
    return [
        { re: new Float64Array(n), im: new Float64Array(n) },
        { re: new Float64Array(n), im: new Float64Array(n) },
    ];
}

/** CW: the IF tone plus one Doppler-shifted line per target. */
export function synthesizeCw({ n, fs, signalFreq, outputFreq, targets, elements, phaseError }) {
    const chans = emptyChannels(n);
    const wavelength = C / Math.max(outputFreq, 1);
    const wr = new Float64Array(n), wi = new Float64Array(n);
    for (const [velocity, angle, amplitude] of targets) {
        const f = signalFreq + (2 * velocity * outputFreq) / C;
        for (let i = 0; i < n; i++) {
            const ph = 2 * Math.PI * f * (i / fs);
            wr[i] = Math.cos(ph); wi[i] = Math.sin(ph);
        }
        const coef = subarrayCoefficients(elements, phaseError, angle, amplitude, wavelength);
        addScaled(chans[0], coef[0], wr, wi);
        addScaled(chans[1], coef[1], wr, wi);
    }
    return chans;
}

/**
 * FMCW: one beat tone per target with slow-time Doppler phase, laid out in PRI
 * slots the way a chirp-synced capture arrives (plan.synced), else back to back.
 */
export function synthesizeFmcw({ n, fs, fmcw, plan, targets, elements, phaseError }) {
    const chans = emptyChannels(n);
    const numChirps = Math.max(1, Math.trunc(fmcw.num_chirps));
    let slot, rampN;
    if (plan && plan.synced) {
        slot = Math.max(1, Math.trunc(plan.frame_samples));
        rampN = Math.max(1, Math.min(slot, roundHalfEven(plan.ramp_time * fs)));
    } else {
        slot = Math.max(1, Math.floor(n / numChirps));
        rampN = slot;
    }
    const slope = fmcw.chirp_bw / fmcw.ramp_time;
    const fc = fmcw.output_freq;
    const wavelength = C / Math.max(fc, 1);
    const wr = new Float64Array(rampN), wi = new Float64Array(rampN);

    for (const [range, velocity, angle, amplitude] of targets) {
        const fBeat = (2 * slope * range) / C;
        const fDop = (2 * velocity * fc) / C;
        const f = fmcw.signal_freq + fBeat + fDop;
        const coef = subarrayCoefficients(elements, phaseError, angle, amplitude, wavelength);
        for (let m = 0; m < numChirps; m++) {
            const offset = m * slot;
            if (offset >= n) break;
            const slowPhase = 2 * Math.PI * fDop * m * fmcw.pri;
            const len = Math.min(rampN, n - offset);
            for (let i = 0; i < len; i++) {
                const ph = 2 * Math.PI * f * (i / fs) + slowPhase;
                wr[i] = Math.cos(ph); wi[i] = Math.sin(ph);
            }
            const wrS = len < rampN ? wr.subarray(0, len) : wr;
            const wiS = len < rampN ? wi.subarray(0, len) : wi;
            addScaled(chans[0], coef[0], wrS, wiS, offset);
            addScaled(chans[1], coef[1], wrS, wiS, offset);
        }
    }
    return chans;
}

/** Scale, add noise and sum the two channels, as read_summed / capture_cw_frame do. */
export function finishCapture(chans, { noiseSigma = NOISE_SIGMA, gauss = null } = {}) {
    const n = chans[0].re.length;
    const re = new Float64Array(n), im = new Float64Array(n);
    const sigma = noiseSigma > 0 && gauss ? noiseSigma : 0;
    for (let i = 0; i < n; i++) {
        re[i] = (chans[0].re[i] + chans[1].re[i]) * AMP_SCALE;
        im[i] = (chans[0].im[i] + chans[1].im[i]) * AMP_SCALE;
        if (sigma) {
            // Independent noise on each channel, then summed.
            re[i] += sigma * (gauss() + gauss());
            im[i] += sigma * (gauss() + gauss());
        }
    }
    return { re, im };
}
