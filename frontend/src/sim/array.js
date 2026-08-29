/**
 * array.js — port of phaser_sim._StubADARArray plus the two ADAR_pyadi_functions
 * writers the sweep uses.
 *
 * The shadow-register/latch split is carried over deliberately, not as
 * pedantry. On the real ADAR1000, `rx_gain`/`rx_phase` land in SPI shadow
 * registers and only a latch (RX_LOAD) moves them into the beam state the RF
 * path uses. A caller that writes phases and forgets to latch gets a beam that
 * never moves -- the flat-line-instead-of-a-beam-pattern failure. Modelling it
 * here means the browser sim reproduces that bug too, which is the whole point
 * of a simulator used for teaching.
 */

import { NUM_ELEMENTS } from './constants.generated.js';

class StubElement {
    constructor() {
        // SPI shadow registers (what the driver writes).
        this.rxGain = 100;
        this.rxPhase = 0;
        this.rxAttenuator = false;
        // Live beam state (what the RF path uses).
        this.latchedGain = 100;
        this.latchedPhase = 0;
    }

    latch() {
        this.latchedGain = this.rxAttenuator ? 0 : this.rxGain;
        this.latchedPhase = this.rxPhase;
    }
}

export function createStubArray() {
    const elements = [];
    for (let i = 0; i < NUM_ELEMENTS; i++) elements.push(new StubElement());

    return {
        elements,
        latchCount: 0,
        // Intrinsic per-element phase error, degrees -- the thing the phase
        // calibration exists to cancel. Zero by default, so a plain sim run is
        // a perfectly matched array.
        elementPhaseError: new Array(NUM_ELEMENTS).fill(0),

        latchRxSettings() {
            for (const el of elements) el.latch();
            this.latchCount += 1;
        },
    };
}

/**
 * ADAR_set_Taper. Taper arrives on the ADAR's 0-127 register scale (the caller
 * has already applied the 0-100 -> 0-127 conversion and the gain calibration).
 */
export function adarSetTaper(array, taperList) {
    for (let i = 0; i < NUM_ELEMENTS; i++) {
        const value = taperList[i] | 0;
        array.elements[i].rxGain = value;
        // An element commanded to zero is routed through the attenuator, so a
        // nulled element is actually off rather than just turned down.
        array.elements[i].rxAttenuator = !value;
    }
    // Nothing above takes effect until this runs.
    array.latchRxSettings();
}

/**
 * ADAR_set_Phase.
 *
 * `phaseStepSize` quantizes the STEERING RAMP only:
 *
 *     (round(PhDelta * i / step) * step + phaseList[i]) % 360
 *
 * The distinction matters. phaseStepSize is the "phase shift bits" knob, and a
 * lab that drops it to 3 bits is asking what a 45-degree phase shifter does to
 * the beam -- not asking to round the phase calibration off to 45 degrees as
 * well. Quantizing the sum would throw away the per-element correction that
 * makes the elements add coherently, exactly when the pattern is already at its
 * most fragile.
 *
 * `phaseList` carries the user's per-element offsets with the phase calibration
 * already folded in by the caller.
 */
export function adarSetPhase(array, phDelta, phaseStepSize, phaseList) {
    for (let i = 0; i < NUM_ELEMENTS; i++) {
        // Quantize the ramp; leave the offsets at full resolution.
        const ramp = pyRound((i * phDelta) / phaseStepSize) * phaseStepSize;
        let qPhase = (ramp + phaseList[i]) % 360;
        if (qPhase < 0) qPhase += 360;
        array.elements[i].rxPhase = qPhase;
    }
    // The rx_phase writes sit in shadow registers until this runs. Sweeping
    // without it leaves the array pointing wherever it was last latched, for
    // every angle.
    array.latchRxSettings();
}

/**
 * Python's round() — banker's rounding, ties to even.
 *
 * JS Math.round() breaks ties upward (Math.round(0.5) === 1, Math.round(-0.5)
 * === -0), so it disagrees with the Python at exactly the half-LSB steering
 * values a coarse `bits` setting lands on constantly. Getting this wrong shows
 * up as a beam that is one phase LSB off at scattered angles.
 */
export function pyRound(x) {
    const floor = Math.floor(x);
    const diff = x - floor;
    if (diff > 0.5) return floor + 1;
    if (diff < 0.5) return floor;
    return floor % 2 === 0 ? floor : floor + 1;
}
