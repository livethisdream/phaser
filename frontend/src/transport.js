/**
 * transport.js — transport facade
 *
 * Public interface stays stable for main.js while transport implementation
 * can switch between web (REST+WebSocket), desktop IPC (PyWebView), Tauri, or Electron.
 */

import { createWebTransport } from './transport-web.js';
import { createIpcTransport } from './transport-ipc.js';
import { createTauriTransport } from './transport-tauri.js';
import { createElectronTransport } from './transport-electron.js';

function resolveTransportMode() {
    // Electron detection — __ELECTRON__ is set by preload.js
    if (window.__ELECTRON__) return 'electron';
    // Tauri detection — __TAURI_INTERNALS__ is set by Tauri runtime
    if (window.__TAURI_INTERNALS__) return 'tauri';
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
    if (mode === 'electron') {
        callbacks.onLog?.('info', 'Electron', 'Using Electron transport mode');
        return createElectronTransport(callbacks);
    }
    if (mode === 'tauri') {
        callbacks.onLog?.('info', 'Tauri', 'Using Tauri transport mode');
        return createTauriTransport(callbacks);
    }
    if (mode === 'ipc') {
        callbacks.onLog?.('info', 'IPC', 'Using IPC transport mode');
        return createIpcTransport(callbacks);
    }
    callbacks.onLog?.('info', 'WS', 'Using web transport mode');
    return createWebTransport(callbacks);
}

export function isTauri() {
    return !!window.__TAURI_INTERNALS__;
}

export function isElectron() {
    return !!window.__ELECTRON__;
}
