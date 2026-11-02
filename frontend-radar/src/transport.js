/**
 * transport.js — which backend the radar UI talks to.
 *
 *   web — the WebSocket to phaser_headless.py on the Pi (or a --sim backend).
 *   sim — the in-browser simulator in ./sim/, no backend at all.
 *
 * Same rules as the beamforming UI's transport.js: ?sim=1 / ?sim=0 win, then
 * window.__PHASER_SIM, then the build default (VITE_TRANSPORT=sim for the
 * GitHub Pages demo, which has no backend to talk to).
 */

import { createWebTransport } from './transport-web.js';
import { createSimTransport } from './transport-sim.js';

export function resolveTransportMode() {
    const params = new URLSearchParams(window.location?.search || '');
    if (params.get('sim') === '1') return 'sim';
    if (params.get('sim') === '0') return 'web';
    if (window.__PHASER_SIM === true) return 'sim';
    if (import.meta.env?.VITE_TRANSPORT === 'sim') return 'sim';
    return 'web';
}

export function createTransport(callbacks = {}) {
    if (resolveTransportMode() === 'sim') {
        callbacks.onLog?.('info', 'SIM', 'Using the in-browser radar simulator (no hardware)');
        return createSimTransport(callbacks);
    }
    return createWebTransport(callbacks);
}
