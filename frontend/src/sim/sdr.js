/**
 * sdr.js — port of phaser_sim.SimSDR.
 *
 * Synthesizes element-level IQ from one or more narrowband plane-wave sources,
 * applies the latched per-element phase and taper the ADAR stub holds, and sums
 * into the two digital sub-arrays (chan0 = elements 1-4, chan1 = elements 5-8),
 * matching the [chan0, chan1] shape SDR_getData returns on real hardware.
 *
 * ---------------------------------------------------------------------------
 * One departure from the Python, algebraically exact.
 *
 * phaser_sim._superpose_source builds a full length-N complex vector per
 * element and sums eight of them. But every element multiplies the SAME carrier
 * `wave`, so the element sum factorizes:
 *
 *     chan_sub[s] = wave * sum_{k in s} amplitude * gain_k
 *                                       * exp(j*(phi_incident_k - psi_k))
 *
 * The inner sum is eight complex scalars. That turns each read from eight
 * length-N vector passes into one, and it is the same number to the last bit --
 * tests/test_sim_parity.py asserts exactly that against the Python.
 *
 * Sources are therefore grouped by IF: sources sharing an IF share a carrier
 * and collapse into a single pass. The target and the interferer deliberately
 * run at the same IF (see phaser_sim's class comment on MVDR), so the common
 * case is one pass per sub-array.
 * ---------------------------------------------------------------------------
 */

import {
    AMP_SCALE,
    C_M_PER_S,
    INTERFERER_IF_HZ,
    NOISE_SIGMA,
    NUM_ELEMENTS,
    SIM_PI,
    TARGET_ANGLE_DEG,
    TARGET_IF_HZ,
} from './constants.generated.js';
import { createRng } from './rng.js';

export function createSimSdr({
    array,
    signalFreq,
    elementSpacing,
    sampleRate,
    bufferSize,
    seed = 0,
    noiseSigma = NOISE_SIGMA,
}) {
    const rng = createRng(seed);

    const state = {
        array,
        signalFreq,
        d: elementSpacing,
        sampleRate,
        bufferSize,
        noiseSigma,
        interfererEnable: false,
        interfererAngleDeg: 30,
        interfererPowerDb: 0,
    };

    /**
     * Per-sub-array complex weight for one source: the factored inner sum.
     * Returns [re0, im0, re1, im1].
     */
    function sourceWeights(arrivalAngleDeg, amplitude, wavelength) {
        const thetaRad = (arrivalAngleDeg * Math.PI) / 180;
        const sinTheta = Math.sin(thetaRad);
        const w = [0, 0, 0, 0];

        for (let k = 0; k < NUM_ELEMENTS; k++) {
            const el = state.array.elements[k];
            // Latched beam state, not the shadow registers -- an unlatched
            // write must not steer the simulated array either.
            const gain = Math.max(0, Math.min(127, el.latchedGain)) / 127;
            const rxPhaseRad = (el.latchedPhase * Math.PI) / 180;
            const errRad = (state.array.elementPhaseError[k] * Math.PI) / 180;
            const phiIncident =
                2 * SIM_PI * ((k * state.d) / wavelength) * sinTheta + errRad;

            const phase = phiIncident - rxPhaseRad;
            const mag = amplitude * gain;
            const sub = k < 4 ? 0 : 1;
            w[sub * 2] += mag * Math.cos(phase);
            w[sub * 2 + 1] += mag * Math.sin(phase);
        }
        return w;
    }

    /** Returns [{re0,im0,re1,im1}] keyed by IF, one entry per distinct IF. */
    function collectSources(wavelength) {
        const byIf = new Map();

        function add(ifHz, angleDeg, amplitude) {
            const w = sourceWeights(angleDeg, amplitude, wavelength);
            const cur = byIf.get(ifHz) || [0, 0, 0, 0];
            for (let i = 0; i < 4; i++) cur[i] += w[i];
            byIf.set(ifHz, cur);
        }

        // Target is always present.
        add(TARGET_IF_HZ, TARGET_ANGLE_DEG, 1);

        if (state.interfererEnable) {
            add(
                INTERFERER_IF_HZ,
                state.interfererAngleDeg,
                Math.pow(10, state.interfererPowerDb / 20),
            );
        }
        return byIf;
    }

    function synthesizeChannels() {
        const n = state.bufferSize;
        const fs = state.sampleRate;
        const wavelength = C_M_PER_S / state.signalFreq;

        const re0 = new Float64Array(n);
        const im0 = new Float64Array(n);
        const re1 = new Float64Array(n);
        const im1 = new Float64Array(n);

        for (const [ifHz, w] of collectSources(wavelength)) {
            const [wr0, wi0, wr1, wi1] = w;
            // Advance the carrier by repeated complex multiply rather than
            // calling cos/sin per sample: 16k transcendental pairs per read,
            // times ~162 angles per sweep, is the difference between a sweep
            // that feels instant and one that visibly stutters.
            const dAng = (2 * Math.PI * ifHz) / fs;
            const stepRe = Math.cos(dAng);
            const stepIm = Math.sin(dAng);
            let cRe = 1;
            let cIm = 0;

            for (let i = 0; i < n; i++) {
                re0[i] += wr0 * cRe - wi0 * cIm;
                im0[i] += wr0 * cIm + wi0 * cRe;
                re1[i] += wr1 * cRe - wi1 * cIm;
                im1[i] += wr1 * cIm + wi1 * cRe;

                const nRe = cRe * stepRe - cIm * stepIm;
                cIm = cRe * stepIm + cIm * stepRe;
                cRe = nRe;

                // Renormalize periodically: repeated multiplication drifts off
                // the unit circle, and over 16k samples that is visible.
                if ((i & 1023) === 1023) {
                    const m = Math.hypot(cRe, cIm) || 1;
                    cRe /= m;
                    cIm /= m;
                }
            }
        }

        for (let i = 0; i < n; i++) {
            re0[i] *= AMP_SCALE; im0[i] *= AMP_SCALE;
            re1[i] *= AMP_SCALE; im1[i] *= AMP_SCALE;
        }

        if (state.noiseSigma > 0) {
            const s = state.noiseSigma;
            for (let i = 0; i < n; i++) {
                re0[i] += rng.normal() * s;
                im0[i] += rng.normal() * s;
                re1[i] += rng.normal() * s;
                im1[i] += rng.normal() * s;
            }
        }

        return [{ re: re0, im: im0 }, { re: re1, im: im1 }];
    }

    return {
        get state() { return state; },

        setSignalFreq(hz) { state.signalFreq = hz; },

        setInterferer({ enable, angleDeg, powerDb } = {}) {
            if (enable !== undefined) state.interfererEnable = !!enable;
            if (angleDeg !== undefined) {
                state.interfererAngleDeg = Math.max(-90, Math.min(90, angleDeg));
            }
            if (powerDb !== undefined) state.interfererPowerDb = powerDb;
        },

        setNoiseSigma(sigma) { state.noiseSigma = sigma; },

        rx() { return synthesizeChannels(); },
    };
}
