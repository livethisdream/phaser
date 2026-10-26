/**
 * engine.js — port of PhaserHeadless's sweep pipeline and state surface.
 *
 * Mirrors do_sweep(), _apply_gain_cal(), _apply_phase_cal(),
 * ConvertPhaseToSteerAngle(), _mvdr_weights(), _interferer_channels(),
 * _adaptive_pattern(), _measure_target_tone() and the get_state/set_state
 * command handlers from phaser_headless.py, driving the stubs in ./array.js and
 * ./sdr.js instead of pyadi-iio.
 *
 * Python is the source of truth. Constants come from ./constants.generated.js
 * (see tools/gen_sim_constants.py) and behavior is pinned by
 * tests/test_sim_parity.py. Change the physics in Python first.
 */

import {
    AVERAGES,
    B0_GAIN,
    B1_GAIN,
    BEAM0_PHASE_DEG,
    BEAM1_PHASE_DEG,
    BF_MODE,
    BUFFER_SIZE,
    BW_MHZ,
    CENTROID_DROP_DB,
    C_M_PER_S,
    DEFAULT_GAIN_CAL,
    DEFAULT_PHASE_CAL,
    ELEMENT_SPACING_M,
    FULL_SCALE,
    IGNORE_RES,
    INTERFERER_ANGLE_DEG,
    INTERFERER_OFFSET_HZ,
    INTERFERER_PHASE_DEG,
    INTERFERER_POWER_DBFS,
    MVDR_DIAG_LOAD,
    MVDR_K,
    NUM_ELEMENTS,
    PHASE_STEP_DEG,
    RX_FREQ_HZ,
    RX_GAIN_DB,
    SAMPLE_RATE_HZ,
    SIGNAL_FREQ_HZ,
    STEER_MAX_DEG,
    STEER_MIN_DEG,
    STEER_PI,
    STEER_RES_DEG,
    TX_FREQ_HZ,
    TX_GAIN_DB,
} from './constants.generated.js';
import { adarSetPhase, adarSetTaper, createStubArray, pyRound, rxGainCode } from './array.js';
import { blackman, fft, fftfreq, fftshift } from './fft.js';
import { createSimSdr } from './sdr.js';

const DEG = Math.PI / 180;

// Mirrors HOLD_MODES in phaser_headless.py: these hold the beam at
// hold_angle instead of sweeping it.
const HOLD_MODES = ['Static Phase', 'Signal vs Time'];
// Mirrors HOLD_FFT_INTERVAL_S: in a hold mode the spectrum rides along at most
// this often.
const HOLD_FFT_INTERVAL_MS = 200;

/**
 * Port of phaser_ctf.peak_angle_centroid(): the mainlobe peak as a
 * power-weighted centroid rather than an argmax, which wanders across a peak
 * clipped flat near full scale. Walks out from the (first) argmax while
 * samples stay within dropDb, so a sidelobe cannot pull it. null when there
 * is nothing to measure, as Python returns None.
 */
export function peakAngleCentroid(angles, gain, dropDb = CENTROID_DROP_DB) {
    if (!angles || !gain || !angles.length || !gain.length
        || angles.length !== gain.length) return null;

    let imax = 0;
    for (let k = 1; k < gain.length; k++) if (gain[k] > gain[imax]) imax = k;
    const gmax = gain[imax];

    let lo = imax;
    while (lo > 0 && gain[lo - 1] >= gmax - dropDb) lo--;
    let hi = imax;
    while (hi < gain.length - 1 && gain[hi + 1] >= gmax - dropDb) hi++;

    let total = 0;
    let moment = 0;
    for (let k = lo; k <= hi; k++) {
        const w = 10 ** (gain[k] / 10);
        total += w;
        moment += angles[k] * w;
    }
    if (total <= 0) return angles[imax];
    return moment / total;
}

export function createEngine(options = {}) {
    const array = createStubArray();

    const state = {
        SignalFreq: SIGNAL_FREQ_HZ,
        Rx_freq: RX_FREQ_HZ,
        // There is no kit to measure; says so rather than read as unverified.
        Rx_freq_source: 'simulator',
        Tx_freq: TX_FREQ_HZ,
        Rx_gain: RX_GAIN_DB,
        Tx_gain: TX_GAIN_DB,
        Tx_mode: 'Transmit off',
        gainList: new Array(NUM_ELEMENTS).fill(100),
        phaseList: new Array(NUM_ELEMENTS).fill(0),
        phase_step: PHASE_STEP_DEG,
        steer_res: STEER_RES_DEG,
        ignore_res: IGNORE_RES,
        sweep_mode: 'Beam Sweep',
        hold_angle: 0,
        steer_min: STEER_MIN_DEG,
        steer_max: STEER_MAX_DEG,
        Averages: AVERAGES,
        d: ELEMENT_SPACING_M,
        BW: BW_MHZ,
        B0_Gain: B0_GAIN,
        B1_Gain: B1_GAIN,
        Beam0_Phase: BEAM0_PHASE_DEG,
        Beam1_Phase: BEAM1_PHASE_DEG,
        bfMode: BF_MODE,
        mvdrK: MVDR_K,
        mvdrDiagLoad: MVDR_DIAG_LOAD,
        interferer_enable: false,
        interferer_angle_deg: INTERFERER_ANGLE_DEG,
        interferer_power_dbfs: INTERFERER_POWER_DBFS,
        interferer_offset_hz: INTERFERER_OFFSET_HZ,
        interferer_phase_deg: INTERFERER_PHASE_DEG,
        sweeping: false,
    };

    // When doSweep() last attached the spectrum; see HOLD_FFT_INTERVAL_MS.
    let lastFftMs = null;
    // target_tone_hz: re-measured every sweep from pre-injection IQ.
    let targetToneHz = null;

    const phaseCal = [...DEFAULT_PHASE_CAL];
    const gainCal = [...DEFAULT_GAIN_CAL];

    const sdr = createSimSdr({
        array,
        signalFreq: state.SignalFreq,
        elementSpacing: state.d,
        sampleRate: SAMPLE_RATE_HZ,
        bufferSize: options.bufferSize ?? BUFFER_SIZE,
        seed: options.seed ?? 0,
        ...(options.noiseSigma !== undefined ? { noiseSigma: options.noiseSigma } : {}),
    });

    // --- calibration application ------------------------------------------

    /**
     * _apply_gain_cal. Taper arrives on the frontend's 0-100 scale as percent
     * of full-scale voltage; gcal scales that amplitude, then rxGainCode
     * inverts the ADAR rx_gain register's (non-linear) curve.
     */
    function applyGainCal(taper) {
        const out = [];
        for (let i = 0; i < NUM_ELEMENTS; i++) {
            const value = taper[i] ?? 100;
            out.push(rxGainCode(value / 100 * (gainCal[i] ?? 1)));
        }
        return out;
    }

    /** _apply_phase_cal — folds the per-element cal into the user's offsets. */
    function applyPhaseCal(phases) {
        const out = [];
        for (let i = 0; i < NUM_ELEMENTS; i++) {
            out.push((phases[i] ?? 0) + (phaseCal[i] ?? 0));
        }
        return out;
    }

    function pushTaper() {
        adarSetTaper(array, applyGainCal(state.gainList));
    }

    /** ConvertPhaseToSteerAngle. `freq` defaults to SignalFreq. */
    function convertPhaseToSteerAngle(phDelta, freq = state.SignalFreq) {
        const value1 =
            (C_M_PER_S * (Math.abs(phDelta) * DEG)) / (2 * STEER_PI * freq * state.d);
        const clamped = Math.max(Math.min(1, value1), -1);
        const theta = Math.asin(clamped) / DEG;
        return phDelta >= 0 ? theta : -theta;
    }

    // --- MVDR --------------------------------------------------------------

    /**
     * _mvdr_weights for the 2-element digital sub-array, closed form.
     *
     * R is 2x2 Hermitian, so the inverse is by hand rather than via a linear
     * algebra library:
     *     R = (1/K) X X^H,  R += load*I,  s = [1,1]^T,  w = R^-1 s / (s^H R^-1 s)
     *
     * The steering vector is [1,1] because the analog ADAR stage has already
     * phase-compensated all 8 elements for the current sweep angle, so an
     * on-target signal arrives in phase at both sub-arrays.
     */
    function mvdrWeights(c0, c1, k) {
        // R entries. r01 = conj(r10); r00 and r11 are real.
        let r00 = 0, r11 = 0, r01re = 0, r01im = 0;
        for (let i = 0; i < k; i++) {
            r00 += c0.re[i] * c0.re[i] + c0.im[i] * c0.im[i];
            r11 += c1.re[i] * c1.re[i] + c1.im[i] * c1.im[i];
            // x0 * conj(x1)
            r01re += c0.re[i] * c1.re[i] + c0.im[i] * c1.im[i];
            r01im += c0.im[i] * c1.re[i] - c0.re[i] * c1.im[i];
        }
        r00 /= k; r11 /= k; r01re /= k; r01im /= k;

        // Diagonal loading, scaled by tr(R)/Nr so it stays proportional to
        // signal power.
        const load = state.mvdrDiagLoad * ((r00 + r11) / 2);
        r00 += load;
        r11 += load;

        // inv(R) = 1/det * [[r11, -r01], [-conj(r01), r00]], det real.
        const det = r00 * r11 - (r01re * r01re + r01im * r01im);
        const d = Math.abs(det) < 1e-30 ? 1e-30 : det;

        // u = R^-1 s, s = [1,1]
        const u0re = (r11 - r01re) / d;
        const u0im = (0 - r01im) / d;
        const u1re = (r00 - r01re) / d;
        const u1im = (0 + r01im) / d;

        // denom = s^H u = u0 + u1 (real part carries it; imag cancels)
        const dre = u0re + u1re;
        const dim = u0im + u1im;
        const dmag = dre * dre + dim * dim || 1e-30;

        // w = u / denom
        return [
            { re: (u0re * dre + u0im * dim) / dmag, im: (u0im * dre - u0re * dim) / dmag },
            { re: (u1re * dre + u1im * dim) / dmag, im: (u1im * dre - u1re * dim) / dmag },
        ];
    }

    // --- synthetic interferer ---------------------------------------------

    /**
     * _interferer_channels' per-sub-array weights: the interferer's plane wave
     * through the commanded (uncalibrated, i.e. ideal) analog stage. See the
     * Python docstring for the model. Returns null when the interferer is off.
     */
    function interfererWeights(phDelta) {
        if (!state.interferer_enable) return null;
        const amp = (FULL_SCALE * Math.pow(10, state.interferer_power_dbfs / 20)) / 8;
        const wavelength = C_M_PER_S / state.SignalFreq;
        const sinTheta = Math.sin(state.interferer_angle_deg * DEG);
        const w = [0, 0, 0, 0];
        for (let k = 0; k < NUM_ELEMENTS; k++) {
            const g = Math.max(0, Math.min(100, state.gainList[k])) / 100;
            const ramp = pyRound((k * phDelta) / state.phase_step) * state.phase_step;
            const psi = (ramp + state.phaseList[k]) * DEG;
            const phi = 2 * Math.PI * ((k * state.d) / wavelength) * sinTheta;
            const sub = k < 4 ? 0 : 1;
            w[sub * 2] += amp * g * Math.cos(phi - psi);
            w[sub * 2 + 1] += amp * g * Math.sin(phi - psi);
        }
        return w;
    }

    /**
     * The interferer's tone, exp(j*(2*pi*f*t + phase)). Identical for every
     * read in a sweep, so doSweep() builds it once rather than per read.
     */
    function interfererTone(n) {
        const f = (targetToneHz ?? 0) + state.interferer_offset_hz;
        const ph = state.interferer_phase_deg * DEG;
        const re = new Float64Array(n);
        const im = new Float64Array(n);
        for (let i = 0; i < n; i++) {
            const a = (2 * Math.PI * f * i) / SAMPLE_RATE_HZ + ph;
            re[i] = Math.cos(a);
            im[i] = Math.sin(a);
        }
        return { re, im };
    }

    /**
     * _adaptive_pattern: the whole array's response (analog stage + digital
     * weights w) against angle, for the beam held at phDelta, beside equal
     * weights [0.5, 0.5]. Both / (sum(g)/2), floored at -80 dB.
     */
    function adaptivePattern(phDelta, w) {
        const angles = [];
        for (let i = 0; i <= 360; i++) angles.push(-90 + i * 0.5);
        const wavelength = C_M_PER_S / state.SignalFreq;

        // Per element: taper and commanded phase, as interfererWeights.
        const g = [];
        const psi = [];
        let gSum = 0;
        for (let k = 0; k < NUM_ELEMENTS; k++) {
            g.push(Math.max(0, Math.min(100, state.gainList[k])) / 100);
            gSum += g[k];
            const ramp = pyRound((k * phDelta) / state.phase_step) * state.phase_step;
            psi.push((ramp + state.phaseList[k]) * DEG);
        }
        const norm = Math.max(gSum / 2, 1e-12);
        const db = (re, im) => 20 * Math.log10(Math.max(Math.hypot(re, im) / norm, 1e-4));

        const adaptive = [];
        const conventional = [];
        for (const ang of angles) {
            const sinT = Math.sin(ang * DEG);
            let a0re = 0, a0im = 0, a1re = 0, a1im = 0;
            for (let k = 0; k < NUM_ELEMENTS; k++) {
                const ph = 2 * Math.PI * ((k * state.d) / wavelength) * sinT - psi[k];
                if (k < 4) { a0re += g[k] * Math.cos(ph); a0im += g[k] * Math.sin(ph); }
                else { a1re += g[k] * Math.cos(ph); a1im += g[k] * Math.sin(ph); }
            }
            // conj(w0)*a0 + conj(w1)*a1
            const yRe = w[0].re * a0re + w[0].im * a0im + w[1].re * a1re + w[1].im * a1im;
            const yIm = w[0].re * a0im - w[0].im * a0re + w[1].re * a1im - w[1].im * a1re;
            adaptive.push(db(yRe, yIm));
            conventional.push(db(0.5 * (a0re + a1re), 0.5 * (a0im + a1im)));
        }
        return { angles, adaptive_db: adaptive, conventional_db: conventional };
    }

    /** _measure_target_tone: strongest bin of the pre-injection sum, in Hz. */
    function measureTargetTone(raw) {
        const n = raw.re.length;
        const win = blackman(n);
        const re = new Float64Array(n);
        const im = new Float64Array(n);
        for (let j = 0; j < n; j++) {
            re[j] = raw.re[j] * win[j];
            im[j] = raw.im[j] * win[j];
        }
        fft(re, im);
        let best = 0;
        let bestMag = -1;
        for (let j = 0; j < n; j++) {
            const m = Math.hypot(re[j], im[j]);
            if (m > bestMag) { bestMag = m; best = j; }
        }
        return fftfreq(n, 1 / SAMPLE_RATE_HZ)[best];
    }

    // --- the sweep ---------------------------------------------------------

    /** do_sweep(). Returns the same payload shape the WebSocket broadcasts. */
    function doSweep() {
        let maxSignal = -1000;
        let dataFft = null;

        const gain = [];
        const deltaGain = [];
        const phaseDiff = [];
        const errorFunc = [];
        const angles = [];

        // Beam squint: phases are calculated for (SignalFreq - BW) but measured
        // at SignalFreq.
        const calcFreq = state.SignalFreq - state.BW * 1e6;

        let phaseValues;
        let steerValues;
        if (HOLD_MODES.includes(state.sweep_mode)) {
            // Hold the beam: one point, the same ramp the sweep uses there.
            steerValues = [state.hold_angle];
            phaseValues = steerValues.map(
                (sv) =>
                    ((2 * STEER_PI * state.d * Math.sin(sv * DEG) * calcFreq) / C_M_PER_S) /
                    DEG,
            );
        } else if (state.ignore_res) {
            // Legacy "ignore steering resolution": step the phase delta one
            // ADAR LSB at a time and let the angle axis fall out of it.
            const phaseLimit =
                Math.trunc(225 / state.phase_step) * state.phase_step + state.phase_step;
            phaseValues = arange(-phaseLimit, phaseLimit, state.phase_step);
            steerValues = phaseValues.map((ph) => convertPhaseToSteerAngle(ph, calcFreq));
        } else {
            const steerRes = Math.max(state.steer_res, 0.1);
            steerValues = arange(state.steer_min, state.steer_max + steerRes, steerRes);
            phaseValues = steerValues.map(
                (sv) =>
                    ((2 * STEER_PI * state.d * Math.sin(sv * DEG) * calcFreq) / C_M_PER_S) /
                    DEG,
            );
        }

        const phaseList = applyPhaseCal(state.phaseList);
        const averages = Math.max(1, state.Averages | 0);

        // The interferer sits off the tone measured on the LAST sweep, so
        // report that one; see do_sweep() for why it matters on a kit.
        const injectedToneHz = targetToneHz;
        // Loudest pre-injection buffer of this sweep; feeds targetToneHz.
        let bestRaw = null;
        let bestRawPow = -1;
        let tone = null;
        // Last MVDR weights; behind the adaptive pattern of a held beam.
        let lastW = null;

        for (let i = 0; i < phaseValues.length; i++) {
            adarSetPhase(array, phaseValues[i], state.phase_step, phaseList);

            let totalSum = 0;
            let totalDelta = 0;
            let totalPhase = 0;
            let lastSum = null;

            for (let a = 0; a < averages; a++) {
                let [c0, c1] = sdr.rx();
                const n = c0.re.length;

                let rawPow = 0;
                for (let j = 0; j < n; j++) {
                    const r = c0.re[j] + c1.re[j];
                    const m = c0.im[j] + c1.im[j];
                    rawPow += r * r + m * m;
                }
                if (rawPow > bestRawPow) {
                    bestRawPow = rawPow;
                    bestRaw = [c0, c1];
                }

                // Inject before any digital weights, so MVDR sees the
                // interferer in its covariance like any received signal.
                const iw = interfererWeights(phaseValues[i]);
                if (iw) {
                    if (!tone || tone.re.length !== n) tone = interfererTone(n);
                    c0 = addTone(c0, tone, iw[0], iw[1]);
                    c1 = addTone(c1, tone, iw[2], iw[3]);
                }

                let sumRe, sumIm, deltaRe, deltaIm;

                if (state.bfMode === 'mvdr') {
                    // One SDR read gives buffer_size samples per channel; use
                    // the first mvdrK of them as the K IQ snapshots -- much
                    // cheaper than K separate reads and mathematically the same.
                    const k = Math.min(state.mvdrK | 0, n);
                    const w = mvdrWeights(c0, c1, k);
                    lastW = w;
                    sumRe = new Float64Array(n);
                    sumIm = new Float64Array(n);
                    // y = Nr w^H x. The Nr matches Manual's scale: the
                    // distortionless weights sum to 1 where Manual's [1, 1]
                    // sum to 2, so unscaled MVDR read 6 dB low.
                    const nr = w.length;
                    for (let j = 0; j < n; j++) {
                        sumRe[j] = nr * (
                            w[0].re * c0.re[j] + w[0].im * c0.im[j] +
                            w[1].re * c1.re[j] + w[1].im * c1.im[j]);
                        sumIm[j] = nr * (
                            w[0].re * c0.im[j] - w[0].im * c0.re[j] +
                            w[1].re * c1.im[j] - w[1].im * c1.re[j]);
                    }
                    // MVDR produces one optimal beam; no natural delta output.
                    deltaRe = new Float64Array(n);
                    deltaIm = new Float64Array(n);
                } else {
                    // Conventional beamformer: two complex scalars applied to
                    // the two digital channels before summing.
                    const w0re = state.B0_Gain * Math.cos(state.Beam0_Phase * DEG);
                    const w0im = state.B0_Gain * Math.sin(state.Beam0_Phase * DEG);
                    const w1re = state.B1_Gain * Math.cos(state.Beam1_Phase * DEG);
                    const w1im = state.B1_Gain * Math.sin(state.Beam1_Phase * DEG);

                    sumRe = new Float64Array(n);
                    sumIm = new Float64Array(n);
                    deltaRe = new Float64Array(n);
                    deltaIm = new Float64Array(n);
                    for (let j = 0; j < n; j++) {
                        const a0re = c0.re[j] * w0re - c0.im[j] * w0im;
                        const a0im = c0.re[j] * w0im + c0.im[j] * w0re;
                        const a1re = c1.re[j] * w1re - c1.im[j] * w1im;
                        const a1im = c1.re[j] * w1im + c1.im[j] * w1re;
                        sumRe[j] = a0re + a1re;
                        sumIm[j] = a0im + a1im;
                        deltaRe[j] = a0re - a1re;
                        deltaIm[j] = a0im - a1im;
                    }
                }

                let sv;
                let dv;
                let bpRe;
                let bpIm;
                if (state.interferer_enable) {
                    // Two tones beat, so "the peak sample" is a tie decided
                    // by rounding. In-band RMS instead, and the phase of the
                    // sum/delta cross-correlation -- see do_sweep().
                    let ps = 0, pd = 0;
                    bpRe = 0;
                    bpIm = 0;
                    for (let j = 0; j < n; j++) {
                        ps += sumRe[j] * sumRe[j] + sumIm[j] * sumIm[j];
                        pd += deltaRe[j] * deltaRe[j] + deltaIm[j] * deltaIm[j];
                        // sum * conj(delta)
                        bpRe += sumRe[j] * deltaRe[j] + sumIm[j] * deltaIm[j];
                        bpIm += sumIm[j] * deltaRe[j] - sumRe[j] * deltaIm[j];
                    }
                    sv = { re: Math.sqrt(ps / n), im: 0 };
                    dv = { re: Math.sqrt(pd / n), im: 0 };
                } else {
                    // Peak in the sum channel.
                    let maxIndex = 0;
                    let maxAbs = -1;
                    for (let j = 0; j < n; j++) {
                        const m = sumRe[j] * sumRe[j] + sumIm[j] * sumIm[j];
                        if (m > maxAbs) { maxAbs = m; maxIndex = j; }
                    }
                    sv = { re: sumRe[maxIndex], im: sumIm[maxIndex] };
                    dv = { re: deltaRe[maxIndex], im: deltaIm[maxIndex] };
                    bpRe = sv.re * dv.re + sv.im * dv.im;
                    bpIm = sv.im * dv.re - sv.re * dv.im;
                }

                const sMagSum = Math.max(Math.hypot(sv.re, sv.im), 1e-15);
                const sDbfsSum = 20 * Math.log10(sMagSum / FULL_SCALE);

                const sMagDelta = Math.max(Math.hypot(dv.re, dv.im), 1e-15);
                const sDbfsDelta = 20 * Math.log10(sMagDelta / FULL_SCALE);

                // angle(sum * conj(delta)) -- see the long note in
                // phaser_headless.py's do_sweep(). Subtracting the two angles
                // instead spans (-2pi, 2pi) and makes sign() depend on which
                // side of the branch cut they land, which is decided by noise.
                const beamPhase = Math.atan2(bpIm, bpRe);

                totalSum += sDbfsSum;
                totalDelta += sDbfsDelta;
                totalPhase += beamPhase;
                lastSum = { re: sumRe, im: sumIm };
            }

            const avgSum = totalSum / averages;
            const avgDelta = totalDelta / averages;
            const avgPhase = totalPhase / averages;

            gain.push(avgSum);
            deltaGain.push(avgDelta);
            phaseDiff.push(avgPhase);

            // error = sign(phase) * (sum - delta) / (sum + delta)
            const denom = avgSum + avgDelta;
            let err = 0;
            if (Math.abs(denom) > 0.001) {
                err = Math.sign(avgPhase) * ((avgSum - avgDelta) / denom);
                err = Math.max(-1, Math.min(1, err));
            }
            errorFunc.push(err);
            angles.push(steerValues[i]);

            if (avgSum > maxSignal) {
                maxSignal = avgSum;
                dataFft = lastSum;
            }
        }

        if (bestRaw) {
            const [r0, r1] = bestRaw;
            const n = r0.re.length;
            const re = new Float64Array(n);
            const im = new Float64Array(n);
            for (let j = 0; j < n; j++) {
                re[j] = r0.re[j] + r1.re[j];
                im[j] = r0.im[j] + r1.im[j];
            }
            targetToneHz = measureTargetTone({ re, im });
        }

        const result = {
            ArrayGain: gain,
            ArrayDelta: deltaGain,
            PhaseDiff: phaseDiff,
            ErrorFunc: errorFunc,
            ArrayAngle: angles,
            peak_signal: maxSignal,
            // Same field, same function as do_sweep() -- see phaser_ctf.
            peak_angle_deg: peakAngleCentroid(angles, gain),
            sweep_mode: state.sweep_mode,
            hold_angle_deg: state.hold_angle,
            target_tone_hz: targetToneHz,
            interferer_freq_hz: state.interferer_enable
                ? (injectedToneHz ?? 0) + state.interferer_offset_hz
                : null,
        };

        if (HOLD_MODES.includes(state.sweep_mode) && lastW) {
            // Every held update, not throttled with the spectrum.
            result.adaptive_pattern = adaptivePattern(phaseValues[0], lastW);
        }

        const now = Date.now();
        if (!HOLD_MODES.includes(state.sweep_mode) || lastFftMs === null
            || now - lastFftMs >= HOLD_FFT_INTERVAL_MS) {
            lastFftMs = now;
            // FFT at the peak angle.
            const n = dataFft.re.length;
            const win = blackman(n);
            const re = new Float64Array(n);
            const im = new Float64Array(n);
            let winSum = 0;
            for (let j = 0; j < n; j++) {
                re[j] = dataFft.re[j] * win[j];
                im[j] = dataFft.im[j] * win[j];
                winSum += win[j];
            }
            fft(re, im);

            const mag = new Float64Array(n);
            for (let j = 0; j < n; j++) mag[j] = Math.hypot(re[j], im[j]);
            const shifted = fftshift(mag);

            const maxGain = new Array(n);
            for (let j = 0; j < n; j++) {
                const sMag = Math.max(shifted[j] / winSum, 1e-15);
                maxGain[j] = 20 * Math.log10(sMag / FULL_SCALE);
            }

            const xf = Array.from(fftshift(fftfreq(n, 1 / SAMPLE_RATE_HZ)));

            result.max_gain = maxGain;
            result.xf = xf;
        }
        return result;
    }

    // --- state surface -----------------------------------------------------

    function getState() {
        return {
            status: 'ok',
            data: {
                ...state,
                gainList: [...state.gainList],
                phaseList: [...state.phaseList],
                sim_mode: true,
                // Simulated hardware is, from the UI's point of view, present
                // and working -- there is nothing to reconnect to.
                hardware_connected: true,
            },
        };
    }

    function setState(patch = {}) {
        const num = (k) => { if (Number.isFinite(patch[k])) state[k] = patch[k]; };

        num('Rx_gain'); num('Tx_gain'); num('Averages'); num('d'); num('BW');
        num('B0_Gain'); num('B1_Gain'); num('Beam0_Phase'); num('Beam1_Phase');
        // Deliberately NOT steer_min/steer_max: phaser_headless's set_state
        // does not accept them either -- they arrive via set_sweep_params.

        if (Number.isFinite(patch.SignalFreq)) {
            state.SignalFreq = patch.SignalFreq;
            sdr.setSignalFreq(state.SignalFreq);
        }
        if (Array.isArray(patch.gainList)) {
            state.gainList = padTo(patch.gainList, NUM_ELEMENTS, 100);
            pushTaper();
        }
        if (Array.isArray(patch.phaseList)) {
            state.phaseList = padTo(patch.phaseList, NUM_ELEMENTS, 0);
        }
        if (typeof patch.Tx_mode === 'string') state.Tx_mode = patch.Tx_mode;
        if (patch.bfMode === 'manual' || patch.bfMode === 'mvdr') {
            state.bfMode = patch.bfMode;
        }
        if (Number.isFinite(patch.mvdrK)) state.mvdrK = Math.max(8, patch.mvdrK | 0);
        if (Number.isFinite(patch.mvdrDiagLoad)) {
            state.mvdrDiagLoad = Math.max(0, patch.mvdrDiagLoad);
        }

        // Synthetic interferer; doSweep() reads it at the next read.
        if ('interferer_enable' in patch) state.interferer_enable = !!patch.interferer_enable;
        if (Number.isFinite(patch.interferer_angle_deg)) {
            state.interferer_angle_deg = Math.max(-90, Math.min(90, patch.interferer_angle_deg));
        }
        if (Number.isFinite(patch.interferer_power_dbfs)) {
            state.interferer_power_dbfs = Math.max(-80, Math.min(0, patch.interferer_power_dbfs));
        }
        if (Number.isFinite(patch.interferer_offset_hz)) {
            const lim = SAMPLE_RATE_HZ / 4;
            state.interferer_offset_hz = Math.max(-lim, Math.min(lim, patch.interferer_offset_hz));
        }
        if (Number.isFinite(patch.interferer_phase_deg)) {
            // Python's % takes the sign of the divisor; JS's of the dividend.
            state.interferer_phase_deg = ((patch.interferer_phase_deg % 360) + 360) % 360;
        }

        // Phase LSB and steering resolution are independent knobs. ignore_res
        // decides which one drives the sweep; it must not let the Bits slider
        // overwrite the steering resolution.
        if (Number.isFinite(patch.bits)) {
            state.phase_step = 360 / Math.pow(2, Math.max(1, patch.bits | 0));
        }
        if (Number.isFinite(patch.steer_res)) {
            state.steer_res = Math.max(0.1, patch.steer_res);
        }
        if ('ignore_res' in patch) state.ignore_res = !!patch.ignore_res;
        // The frontend's `mode` is the legacy sweep mode, as on the backend.
        if (typeof patch.mode === 'string') state.sweep_mode = patch.mode;
        if (Number.isFinite(patch.hold_angle)) {
            state.hold_angle = Math.max(-90, Math.min(90, patch.hold_angle));
        }

        return { status: 'ok' };
    }

    // Taper has to reach the array before the first sweep, exactly as
    // _do_init_hardware does on the real path.
    pushTaper();

    /** set_sweep_params — the only route to steer_min/steer_max, as on the backend. */
    function setSweepParams({ steer_min, steer_max, phase_step, averages } = {}) {
        if (Number.isFinite(steer_min)) state.steer_min = steer_min;
        if (Number.isFinite(steer_max)) state.steer_max = steer_max;
        if (Number.isFinite(phase_step)) state.phase_step = phase_step;
        if (Number.isFinite(averages)) state.Averages = averages | 0;
        return { status: 'ok' };
    }

    return {
        getState,
        setState,
        setSweepParams,
        doSweep,
        // Exposed for tests/test_sim_parity.py.
        _internal: { array, sdr, convertPhaseToSteerAngle, applyGainCal, applyPhaseCal },
    };
}

/** chan + (wRe + j*wIm) * tone, as a new channel; never mutates chan. */
function addTone(chan, tone, wRe, wIm) {
    const n = chan.re.length;
    const re = new Float64Array(n);
    const im = new Float64Array(n);
    for (let j = 0; j < n; j++) {
        re[j] = chan.re[j] + wRe * tone.re[j] - wIm * tone.im[j];
        im[j] = chan.im[j] + wRe * tone.im[j] + wIm * tone.re[j];
    }
    return { re, im };
}

/** np.arange — half-open, and tolerant of float step accumulation. */
function arange(start, stop, step) {
    const out = [];
    const n = Math.max(0, Math.ceil((stop - start) / step - 1e-9));
    for (let i = 0; i < n; i++) out.push(start + i * step);
    return out;
}

function padTo(arr, n, fill) {
    const out = arr.slice(0, n).map(Number);
    while (out.length < n) out.push(fill);
    return out;
}
