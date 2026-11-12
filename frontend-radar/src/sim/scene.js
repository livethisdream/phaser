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
    SCENE_PT_DBM, SCENE_GTGR_DB, SCENE_NF_DB, SCENE_NOISE_DBFS, SCENE_LEAKAGE_DBFS,
    SCENE_R_MIN, SCENE_FULL_SCALE, SCENE_WALKER_PHASES, SCENE_TURNTABLE_R0, SCENE_TURNTABLE_POINTS,
    SCENE_NOISE, FMCW_DEFAULT_SCENE_NOISE, SCENE_CLUTTER, SCENE_SKIRT, SCENE_FLUCT_HZ, SCENE_FLUCT_PHASE,
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

/** The two channels scaled, each with its own noise, kept apart (iq_ch). */
export function finishChannels(chans, { noiseSigma = NOISE_SIGMA, gauss = null } = {}) {
    const sigma = noiseSigma > 0 && gauss ? noiseSigma : 0;
    return chans.map(({ re: cr, im: ci }) => {
        const n = cr.length;
        const re = new Float64Array(n), im = new Float64Array(n);
        for (let i = 0; i < n; i++) {
            re[i] = cr[i] * AMP_SCALE + (sigma ? sigma * gauss() : 0);
            im[i] = ci[i] * AMP_SCALE + (sigma ? sigma * gauss() : 0);
        }
        return { re, im };
    });
}

// --- Live FMCW scenes (phaser_sim.scene_scatterers, after ece444 fmcw_sim.py) ---

/** phaser_sim.scene_echo_dbfs: the radar-equation echo level, dBFS. */
export function sceneEchoDbfs(r, rcs, fs, fc) {
    const rr = Math.max(r, SCENE_R_MIN);
    const lam = C / fc;
    const prDbm = SCENE_PT_DBM + SCENE_GTGR_DB + 10 * Math.log10(lam * lam * rcs)
        - 30 * Math.log10(4 * Math.PI) - 40 * Math.log10(rr);
    const noiseDbm = -174 + 10 * Math.log10(fs) + SCENE_NF_DB;
    return SCENE_NOISE_DBFS + prDbm - noiseDbm;
}

/** phaser_sim.scene_fluct_db: scatterer k's fading at time t, within +-depthDb. */
export function sceneFluctDb(t, k, depthDb) {
    if (!depthDb) return 0;
    let tone = 0;
    for (let i = 0; i < SCENE_FLUCT_HZ.length; i++) {
        tone += Math.sin(2 * Math.PI * SCENE_FLUCT_HZ[i] * t + SCENE_FLUCT_PHASE[i] * (k + 1));
    }
    return (depthDb * tone) / SCENE_FLUCT_HZ.length;
}

/**
 * A scene's scatterers as [{range(t), level(r, t), phase0, az}]: range in m at
 * absolute time t, s; level in dBFS for that range and time. The same list, in
 * the same order, as phaser_sim.scene_scatterers, `noise` (SCENE_NOISE) adding
 * the room's clutter and the leakage skirt and fading the moving targets.
 */
export function sceneScatterers(name, fs, fc, noise = FMCW_DEFAULT_SCENE_NOISE, turntableDeg = 0) {
    const out = sceneTargets(name, fs, fc, turntableDeg);
    if (name === 'static') return out;
    const level = SCENE_NOISE[noise];
    if (!level) throw new Error(`unknown scene noise '${noise}'`);
    const depth = level.fluct_db;
    let res = out;
    if (depth && name !== 'corners' && name !== 'turntable') {   // corner reflectors hold steady
        res = [out[0], ...out.slice(1).map((sc, k) => ({
            ...sc, level: (r, t) => sc.level(r, t) + sceneFluctDb(t, k, depth),
        }))];
    }
    if (level.clutter_db !== null) {
        const extra = level.clutter_db;
        for (const [r0, lv, p0] of SCENE_SKIRT) {
            res.push({ range: () => r0, level: () => lv + extra, phase0: p0, az: 0, still: true });
        }
        for (const [r0, rcs, p0, az] of SCENE_CLUTTER) {
            res.push({ range: () => r0, level: (r) => sceneEchoDbfs(r, rcs, fs, fc) + extra, phase0: p0, az, still: true });
        }
    }
    return res;
}

/** phaser_sim.turntable_point: platter point (x, y) turned by deg -> [range m, azimuth deg]. */
export function turntablePoint(x, y, deg, r0 = SCENE_TURNTABLE_R0) {
    const a = (deg * Math.PI) / 180;
    const xw = x * Math.cos(a) - y * Math.sin(a);
    const yw = r0 + x * Math.sin(a) + y * Math.cos(a);
    return [Math.hypot(xw, yw), (Math.atan2(xw, yw) * 180) / Math.PI];
}

/** A scene's own scatterers, as fmcw_sim places them: leakage first. */
function sceneTargets(name, fs, fc, turntableDeg = 0) {
    const echo = (rcs) => (r) => sceneEchoDbfs(r, rcs, fs, fc);
    // `still`: range and level never change, so every chirp is the same one.
    const out = [{ range: () => 0, level: () => SCENE_LEAKAGE_DBFS, phase0: 0.7, az: 0, still: true }];
    if (name === 'turntable') {
        SCENE_TURNTABLE_POINTS.forEach(([x, y, rcs], k) => {
            const [r0, az] = turntablePoint(x, y, turntableDeg);
            out.push({ range: () => r0, level: echo(rcs), phase0: 0.4 + 0.9 * k, az, still: true });
        });
        return out;
    }
    if (name === 'corners') {
        for (const [r0, ph] of [[1.0, 0.0], [2.0, 1.1], [3.0, 2.3]]) {
            out.push({ range: () => r0, level: echo(0.47), phase0: ph, az: 0, still: true });
        }
    } else if (name === 'hand') {
        out.push({ range: (t) => 0.6 + 0.15 * Math.sin(2 * Math.PI * 1.0 * t), level: echo(0.02), phase0: 1.3, az: 0 });
    } else if (name === 'walker') {
        const rStart = 1.0, rTurn = 4.0, speed = 1.0, strideHz = 0.9, rcsBody = 0.5;
        const leg = (2 * (rTurn - rStart)) / speed;
        const torso = (t) => {
            const s = (((t % leg) + leg) % leg) / leg;
            return rStart + (rTurn - rStart) * (1 - Math.abs(2 * s - 1));
        };
        const ph = SCENE_WALKER_PHASES;
        out.push({ range: torso, level: echo(rcsBody), phase0: ph[0], az: 0 });
        const limbs = [[0.25, 0, 0.08], [0.25, Math.PI, 0.08], [0.15, Math.PI, 0.04], [0.15, 0, 0.04]];
        limbs.forEach(([swing, p, share], i) => {
            out.push({
                range: (t) => torso(t) + swing * Math.sin(2 * Math.PI * strideHz * t + p),
                level: echo(share * rcsBody), phase0: ph[i + 1], az: 0,
            });
        });
    } else if (name !== 'static') {
        throw new Error(`unknown FMCW scene '${name}'`);
    }
    return out;
}

/**
 * A live scene, chirp by chirp at absolute time t0 + m PRI + t_fast:
 * phaser_sim._synthesize_fmcw_scene. Pre-AMP_SCALE units, like the others.
 */
export function synthesizeFmcwScene({ n, fs, fmcw, plan, scene, noise, t0, elements, phaseError, turntableDeg = 0 }) {
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
    const pri = fmcw.pri, fIf = fmcw.signal_freq;
    const wr = new Float64Array(rampN), wi = new Float64Array(rampN);
    const still = emptyChannels(rampN);
    for (const sc of sceneScatterers(scene, fs, fc, noise, turntableDeg)) {
        const coef = subarrayCoefficients(elements, phaseError, sc.az, 1.0, wavelength);
        if (sc.still) {
            // Range and level never change: the same chirp in every slot, so
            // the still scatterers sum into one template, added once a slot.
            const r = sc.range(t0);
            const amp = (SCENE_FULL_SCALE * 10 ** (sc.level(r, t0) / 20)) / AMP_SCALE / 8;
            for (let i = 0; i < rampN; i++) {
                const ph = 2 * Math.PI * (fIf + (2 * slope * r) / C) * (i / fs) - (4 * Math.PI * r * fc) / C + sc.phase0;
                wr[i] = amp * Math.cos(ph); wi[i] = amp * Math.sin(ph);
            }
            addScaled(still[0], coef[0], wr, wi);
            addScaled(still[1], coef[1], wr, wi);
            continue;
        }
        for (let m = 0; m < numChirps; m++) {
            const offset = m * slot;
            if (offset >= n) break;
            const len = Math.min(rampN, n - offset);
            for (let i = 0; i < len; i++) {
                const tf = i / fs;
                const t = t0 + m * pri + tf;
                const r = sc.range(t);
                const amp = (SCENE_FULL_SCALE * 10 ** (sc.level(r, t) / 20)) / AMP_SCALE / 8;
                const ph = 2 * Math.PI * (fIf + (2 * slope * r) / C) * tf - (4 * Math.PI * r * fc) / C + sc.phase0;
                wr[i] = amp * Math.cos(ph); wi[i] = amp * Math.sin(ph);
            }
            const wrS = len < rampN ? wr.subarray(0, len) : wr;
            const wiS = len < rampN ? wi.subarray(0, len) : wi;
            addScaled(chans[0], coef[0], wrS, wiS, offset);
            addScaled(chans[1], coef[1], wrS, wiS, offset);
        }
    }
    for (let m = 0; m < numChirps; m++) {
        const offset = m * slot;
        if (offset >= n) break;
        const len = Math.min(rampN, n - offset);
        for (let c = 0; c < 2; c++) {
            const dst = chans[c], src = still[c];
            for (let k = 0; k < len; k++) {
                dst.re[offset + k] += src.re[k];
                dst.im[offset + k] += src.im[k];
            }
        }
    }
    return chans;
}

/** A live scene's noise floor, as phaser_sim applies it: sigma per channel, per part. */
export function sceneNoiseSigma(noise = FMCW_DEFAULT_SCENE_NOISE) {
    const level = SCENE_NOISE[noise];
    if (!level) throw new Error(`unknown scene noise '${noise}'`);
    return (SCENE_FULL_SCALE * 10 ** (level.floor_dbfs / 20)) / 2;
}
