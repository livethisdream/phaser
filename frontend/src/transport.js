/**
 * transport.js — transport facade
 *
 * One transport exists: web — REST + WebSocket against phaser_headless.py.
 *
 * There used to be electron/tauri/ipc entries here as well. Nothing in this
 * repo ever set the globals that selected them (`window.__ELECTRON__`,
 * `window.__TAURI_INTERNALS__`), so they could not be reached; they were
 * removed along with their transport modules rather than left to look load-
 * bearing. The PyWebView desktop chrome in main.js is untouched -- it keys off
 * `window.pywebview`, which is a different thing.
 */

import { createWebTransport } from './transport-web.js';

export function resolveTransportMode() {
    return 'web';
}

export function createTransport(callbacks = {}) {
    callbacks.onLog?.('info', 'WS', 'Using web transport mode');
    return createWebTransport(callbacks);
}
