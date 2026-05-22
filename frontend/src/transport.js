/**
 * transport.js — transport facade
 *
 * Public interface stays stable for main.js while transport implementation
 * can switch between web (REST+WebSocket) and desktop IPC.
 */

import { createWebTransport } from './transport-web.js';
import { createIpcTransport } from './transport-ipc.js';

function resolveTransportMode() {
    // Runtime override for desktop hosts.
    if (window.__PHASER_TRANSPORT === 'ipc') return 'ipc';
    // Desktop launcher loads frontend via file://; this must use IPC.
    if (window.location?.protocol === 'file:') return 'ipc';
    // Auto-detect PyWebView desktop host.
    if (window.pywebview?.api?.invoke) return 'ipc';
    // Build-time override (for future desktop builds).
    if (import.meta.env?.VITE_TRANSPORT === 'ipc') return 'ipc';
    return 'web';
}

export function createTransport(callbacks = {}) {
    const mode = resolveTransportMode();
    if (mode === 'ipc') {
        callbacks.onLog?.('info', 'IPC', 'Using IPC transport mode');
        return createIpcTransport(callbacks);
    }
    callbacks.onLog?.('info', 'WS', 'Using web transport mode');
    return createWebTransport(callbacks);
}
