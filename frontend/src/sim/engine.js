/**
 * engine.js — port of PhaserHeadless's sweep pipeline and state surface.
 *
 * Mirrors do_sweep(), _apply_gain_cal(), _apply_phase_cal(),
 * ConvertPhaseToSteerAngle(), _mvdr_weights() and the get_state/set_state
 * command handlers from phaser_headless.py, driving the stubs in ./array.js and
 * ./sdr.js instead of pyadi-iio.
 *
 * Python is the source of truth. Constants come from ./constants.generated.js
 * (see tools/gen_sim_constants.py) and behaviour is pinned by
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
    C_M_PER_S,
    DEFAULT_GAIN_CAL,
    DEFAULT_PHASE_CAL,
    ELEMENT_SPACING_M,
    FULL_SCALE,
    IGNORE_RES,
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
import { adarSetPhase, adarSetTaper, createStubArray, pyRound } from './array.js';
import { blackman, fft, fftfreq, fftshift } from './fft.js';
import { createSimSdr } from './sdr.js';

const DEG = Math.PI / 180;

export function createEngine(options = {}) {
    const array = createStubArray();

    const state = {
        SignalFreq: SIGNAL_FREQ_HZ,
        Rx_freq: RX_FREQ_HZ,
        Tx_freq: TX_FREQ_HZ,
        Rx_gain: RX_GAIN_DB,
        Tx_gain: TX_GAIN_DB,
        Tx_mode: 'Transmit off',
        gainList: new Array(NUM_ELEMENTS).fill(100),
        phaseList: new Array(NUM_ELEMENTS).fill(0),
        phase_step: PHASE_STEP_DEG,
        steer_res: STEER_RES_DEG,
        ignore_res: IGNORE_RES,
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
        sim_interferer_enable: false,
        sim_interferer_angle_deg: 30,
        sim_interferer_power_db: 0,
        sweeping: false,
    };

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
     * _apply_gain_cal. Taper arrives on the frontend's 0-100 scale; the ADAR
     * rx_gain register is 0-127, so the two are not interchangeable -- a taper
     * commanded to 100 must reach full scale, not 79% of it.
     */
    function applyGainCal(taper) {
        const out = [];
        for (let i = 0; i < NUM_ELEMENTS; i++) {
            const value = taper[i] ?? 100;
            const scaled = (value * 127) / 100 * (gainCal[i] ?? 1);
            out.push(Math.max(0, Math.min(127, pyRound(scaled))));
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
        if (state.ignore_res) {
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

        for (let i = 0; i < phaseValues.length; i++) {
            adarSetPhase(array, phaseValues[i], state.phase_step, phaseList);

            let totalSum = 0;
            let totalDelta = 0;
            let totalPhase = 0;
            let lastSum = null;

            for (let a = 0; a < averages; a++) {
                const [c0, c1] = sdr.rx();
                const n = c0.re.length;

                let sumRe, sumIm, deltaRe, deltaIm;

                if (state.bfMode === 'mvdr') {
                    // One SDR read gives buffer_size samples per channel; use
                    // the first mvdrK of them as the K IQ snapshots -- much
                    // cheaper than K separate reads and mathematically the same.
                    const k = Math.min(state.mvdrK | 0, n);
                    const w = mvdrWeights(c0, c1, k);
                    sumRe = new Float64Array(n);
                    sumIm = new Float64Array(n);
                    // y = w^H x
                    for (let j = 0; j < n; j++) {
                        sumRe[j] =
                            w[0].re * c0.re[j] + w[0].im * c0.im[j] +
                            w[1].re * c1.re[j] + w[1].im * c1.im[j];
                        sumIm[j] =
                            w[0].re * c0.im[j] - w[0].im * c0.re[j] +
                            w[1].re * c1.im[j] - w[1].im * c1.re[j];
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

                // Peak in the sum channel.
                let maxIndex = 0;
                let maxAbs = -1;
                for (let j = 0; j < n; j++) {
                    const m = sumRe[j] * sumRe[j] + sumIm[j] * sumIm[j];
                    if (m > maxAbs) { maxAbs = m; maxIndex = j; }
                }

                const sMagSum = Math.max(Math.hypot(sumRe[maxIndex], sumIm[maxIndex]), 1e-15);
                const sDbfsSum = 20 * Math.log10(sMagSum / FULL_SCALE);

                const sMagDelta = Math.max(
                    Math.hypot(deltaRe[maxIndex], deltaIm[maxIndex]), 1e-15);
                const sDbfsDelta = 20 * Math.log10(sMagDelta / FULL_SCALE);

                // angle(sum * conj(delta)) -- see the long note in
                // phaser_headless.py's do_sweep(). Subtracting the two angles
                // instead spans (-2pi, 2pi) and makes sign() depend on which
                // side of the branch cut they land, which is decided by noise.
                const bpRe = sumRe[maxIndex] * deltaRe[maxIndex]
                           + sumIm[maxIndex] * deltaIm[maxIndex];
                const bpIm = sumIm[maxIndex] * deltaRe[maxIndex]
                           - sumRe[maxIndex] * deltaIm[maxIndex];
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

        return {
            ArrayGain: gain,
            ArrayDelta: deltaGain,
            PhaseDiff: phaseDiff,
            ErrorFunc: errorFunc,
            ArrayAngle: angles,
            max_gain: maxGain,
            xf,
            peak_signal: maxSignal,
        };
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

        let interfChanged = false;
        if ('sim_interferer_enable' in patch) {
            state.sim_interferer_enable = !!patch.sim_interferer_enable;
            interfChanged = true;
        }
        if (Number.isFinite(patch.sim_interferer_angle_deg)) {
            state.sim_interferer_angle_deg =
                Math.max(-90, Math.min(90, patch.sim_interferer_angle_deg));
            interfChanged = true;
        }
        if (Number.isFinite(patch.sim_interferer_power_db)) {
            state.sim_interferer_power_db = patch.sim_interferer_power_db;
            interfChanged = true;
        }
        if (interfChanged) {
            sdr.setInterferer({
                enable: state.sim_interferer_enable,
                angleDeg: state.sim_interferer_angle_deg,
                powerDb: state.sim_interferer_power_db,
            });
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
