/* --- Mini scene: a top-down thumbnail of the array and its sources --------
 *
 * Drawn in the plots' top-right corner while the interferer is on, so the
 * sources stay in view on the tabs that cannot show them (the Polar tab draws
 * them itself; see polarSources in main.js). The array sits at the bottom
 * centre, boresight straight up, +90 deg to the right -- the Polar tab's
 * convention. Over it: the live sum pattern on a fixed -50..0 dBFS radial
 * scale, the beam's peak (or held) direction, the simulated HB100 and the
 * synthetic interferer.
 *
 * Both sources are far-field plane waves in the model, so only their angle
 * means anything; they ride a ring near the edge. Dragging one calls back with
 * the new angle and the caller sends it to the backend -- this module owns no
 * state beyond what it last drew.
 */

const SVG_NS = 'http://www.w3.org/2000/svg';
const W = 440;
const H = 232;
const CX = W / 2;
const CY = 212;
const R = 186;
const DB_MIN = -50;
const RINGS_DB = [-40, -30, -20, -10];
const SPOKES_DEG = [-90, -60, -30, 0, 30, 60, 90];
const ANGLE_LIMIT = 90;
// Sources ride a ring just inside the grid's edge.
const R_SOURCE = R * 0.9;

function el(name, attrs = {}, parent = null) {
    const node = document.createElementNS(SVG_NS, name);
    for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
    if (parent) parent.appendChild(node);
    return node;
}

/** Angle (deg from boresight, + right) and radius -> SVG point. */
function pt(angleDeg, r) {
    const a = (angleDeg * Math.PI) / 180;
    return [CX + r * Math.sin(a), CY - r * Math.cos(a)];
}

function dbToR(db) {
    const clamped = Math.max(DB_MIN, Math.min(0, db));
    return (R * (clamped - DB_MIN)) / -DB_MIN;
}

export function clampAngle(deg) {
    return Math.max(-ANGLE_LIMIT, Math.min(ANGLE_LIMIT, deg));
}

/**
 * @param {SVGSVGElement} svg
 * @param {object} opts
 * @param {(kind: 'target'|'interferer', angleDeg: number) => void} opts.onDrag
 */
export function createScene(svg, { onDrag } = {}) {
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('preserveAspectRatio', 'xMidYMax meet');
    svg.classList.add('scene-svg');
    svg.replaceChildren();

    // Grid: half-disc rings and angle spokes.
    const grid = el('g', { class: 'scene-grid' }, svg);
    el('path', {
        d: `M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY} Z`,
        class: 'scene-field',
    }, grid);
    for (const db of RINGS_DB) {
        const r = dbToR(db);
        el('path', { d: `M ${CX - r} ${CY} A ${r} ${r} 0 0 1 ${CX + r} ${CY}` }, grid);
    }
    for (const deg of SPOKES_DEG) {
        const [x, y] = pt(deg, R);
        el('line', { x1: CX, y1: CY, x2: x, y2: y }, grid);
    }

    const pattern = el('path', { class: 'scene-pattern', d: '' }, svg);
    const beamRay = el('line', { class: 'scene-beam-ray', x1: CX, y1: CY, x2: CX, y2: CY }, svg);
    const targetRay = el('line', { class: 'scene-source-ray scene-target', x1: CX, y1: CY }, svg);
    const intfRay = el('line', { class: 'scene-source-ray scene-interferer', x1: CX, y1: CY }, svg);

    // The array: eight elements along the baseline, two sub-arrays of four.
    const arr = el('g', { class: 'scene-array' }, svg);
    const pitch = 7;
    for (let k = 0; k < 8; k++) {
        const x = CX + (k - 3.5) * pitch;
        el('rect', {
            x: x - pitch * 0.35, y: CY - 3, width: pitch * 0.7, height: 6, rx: 1,
            class: k < 4 ? 'scene-sub0' : 'scene-sub1',
        }, arr);
    }

    function makeHandle(kind, label) {
        const g = el('g', {
            class: `scene-handle scene-${kind}`,
            tabindex: '0',
            role: 'slider',
            'aria-label': `${label} angle`,
            'aria-valuemin': String(-ANGLE_LIMIT),
            'aria-valuemax': String(ANGLE_LIMIT),
        }, svg);
        // A generous invisible hit area, so the handle is easy to grab on touch.
        el('circle', { r: 22, class: 'scene-hit' }, g);
        if (kind === 'target') {
            // HB100: a small horn-ish box.
            el('rect', { x: -7, y: -5, width: 14, height: 10, rx: 2, class: 'scene-icon' }, g);
            el('rect', { x: -3, y: -2, width: 6, height: 4, rx: 1, class: 'scene-icon-face' }, g);
        } else {
            // Interferer: a diamond.
            el('path', { d: 'M 0 -8 L 8 0 L 0 8 L -8 0 Z', class: 'scene-icon' }, g);
        }
        const value = el('text', { y: 30, 'text-anchor': 'middle', class: 'scene-handle-value' }, g);
        return { g, value, kind, angle: 0, draggable: false };
    }

    const target = makeHandle('target', 'HB100');
    const intf = makeHandle('interferer', 'Interferer');

    function placeHandle(h, angle) {
        h.angle = angle;
        const [x, y] = pt(angle, R_SOURCE);
        h.g.setAttribute('transform', `translate(${x} ${y})`);
        h.value.textContent = `${Math.round(angle)}°`;
        h.g.setAttribute('aria-valuenow', String(Math.round(angle)));
        const ray = h.kind === 'target' ? targetRay : intfRay;
        ray.setAttribute('x2', x);
        ray.setAttribute('y2', y);
    }

    function setVisible(node, on) {
        node.style.display = on ? '' : 'none';
    }

    // --- dragging --------------------------------------------------------

    function pointerAngle(evt) {
        const ctm = svg.getScreenCTM();
        if (!ctm) return null;
        const p = new DOMPoint(evt.clientX, evt.clientY).matrixTransform(ctm.inverse());
        // Below the baseline, keep the side the pointer is on.
        const dy = Math.max(CY - p.y, 0);
        const dx = p.x - CX;
        if (dx === 0 && dy === 0) return null;
        return clampAngle(Math.round((Math.atan2(dx, dy) * 180) / Math.PI));
    }

    function commit(h, angle) {
        if (angle === null || angle === h.angle) return;
        placeHandle(h, angle);
        onDrag?.(h.kind, angle);
    }

    for (const h of [target, intf]) {
        h.g.addEventListener('pointerdown', (evt) => {
            if (!h.draggable) return;
            evt.preventDefault();
            h.g.setPointerCapture(evt.pointerId);
            h.g.classList.add('dragging');
        });
        h.g.addEventListener('pointermove', (evt) => {
            if (!h.g.hasPointerCapture(evt.pointerId)) return;
            commit(h, pointerAngle(evt));
        });
        const end = (evt) => {
            if (h.g.hasPointerCapture(evt.pointerId)) h.g.releasePointerCapture(evt.pointerId);
            h.g.classList.remove('dragging');
        };
        h.g.addEventListener('pointerup', end);
        h.g.addEventListener('pointercancel', end);
        h.g.addEventListener('keydown', (evt) => {
            if (!h.draggable) return;
            const step = evt.shiftKey ? 5 : 1;
            let next = null;
            if (evt.key === 'ArrowLeft' || evt.key === 'ArrowDown') next = h.angle - step;
            else if (evt.key === 'ArrowRight' || evt.key === 'ArrowUp') next = h.angle + step;
            else if (evt.key === 'Home') next = 0;
            if (next === null) return;
            evt.preventDefault();
            commit(h, clampAngle(next));
        });
    }

    // --- update ----------------------------------------------------------

    /**
     * @param {object} s
     * @param {number[]|null} s.angles   sweep angles (deg), or null in a hold mode
     * @param {number[]|null} s.gain     sum pattern, dBFS
     * @param {number|null} s.beamAngle  peak of the sweep, or the held angle
     * @param {number|null} s.targetAngle  simulated HB100; null when unknown (hardware)
     * @param {boolean} s.interfererOn
     * @param {number} s.interfererAngle
     */
    function update(s) {
        if (s.angles && s.gain && s.angles.length === s.gain.length && s.angles.length > 1) {
            let d = '';
            for (let i = 0; i < s.angles.length; i++) {
                const [x, y] = pt(s.angles[i], dbToR(s.gain[i]));
                d += `${i ? 'L' : 'M'} ${x.toFixed(1)} ${y.toFixed(1)} `;
            }
            pattern.setAttribute('d', `${d}L ${CX} ${CY} Z`);
        } else {
            pattern.setAttribute('d', '');
        }

        const hasBeam = Number.isFinite(s.beamAngle);
        setVisible(beamRay, hasBeam);
        if (hasBeam) {
            const [x, y] = pt(s.beamAngle, R);
            beamRay.setAttribute('x2', x);
            beamRay.setAttribute('y2', y);
        }

        const hasTarget = Number.isFinite(s.targetAngle);
        target.draggable = hasTarget;
        setVisible(target.g, hasTarget);
        setVisible(targetRay, hasTarget);
        if (hasTarget && !target.g.classList.contains('dragging')) placeHandle(target, s.targetAngle);

        intf.draggable = !!s.interfererOn;
        setVisible(intf.g, !!s.interfererOn);
        setVisible(intfRay, !!s.interfererOn);
        if (s.interfererOn && !intf.g.classList.contains('dragging')) {
            placeHandle(intf, s.interfererAngle);
        }
    }

    return { update };
}
