/**
 * transport.js — transport facade
 *
 * Two transports exist:
 *   web — REST + WebSocket against phaser_headless.py on the Pi.
 *   sim — the browser-local simulator in ./sim/, no backend at all.
 *
 * There used to be electron/tauri/ipc entries here as well. Nothing in this
 * repo ever set the globals that selected them (`window.__ELECTRON__`,
 * `window.__TAURI_INTERNALS__`), so they could not be reached; they were
 * removed along with their transport modules rather than left to look load-
 * bearing. The PyWebView desktop chrome in main.js is untouched -- it keys off
 * `window.pywebview`, which is a different thing.
 */

import { createWebTransport } from './transport-web.js';
import { createSimTransport } from './transport-sim.js';

export function resolveTransportMode() {
    // An explicit request wins over everything, so ?sim=1 works on the Pi too --
    // useful when the hardware is absent or broken mid-lab.
    const params = new URLSearchParams(window.location?.search || '');
    if (params.get('sim') === '1') return 'sim';
    if (params.get('sim') === '0') return 'web';
    if (window.__PHASER_SIM === true) return 'sim';
    // Build-time default. The GitHub Pages build sets this: a static site has
    // no backend to talk to, so sim is the only thing that can work there.
    if (import.meta.env?.VITE_TRANSPORT === 'sim') return 'sim';
    return 'web';
}

export function createTransport(callbacks = {}) {
    if (resolveTransportMode() === 'sim') {
        callbacks.onLog?.('info', 'SIM', 'Using in-browser simulator (no hardware)');
        return createSimTransport(callbacks);
    }
    callbacks.onLog?.('info', 'WS', 'Using web transport mode');
    return createWebTransport(callbacks);
}
