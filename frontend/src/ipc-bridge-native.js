/**
 * ipc-bridge-native.js — real desktop IPC bridge
 *
 * Wires window.__PHASER_IPC_BRIDGE to the Python IPC worker via the desktop
 * shell's sidecar channel.  Loaded only by the desktop build; the browser
 * build uses ipc-bridge-mock.js (activated by ?mockIpc=1) or no bridge at all.
 *
 * Two sidecar back-ends are supported, selected by build env / runtime env:
 *
 *   1. Tauri (window.__TAURI__) — uses Tauri's `invoke()` to call a Rust
 *      command that proxies NDJSON messages to/from the Python sidecar.
 *      Rust command name: `phaser_ipc` (must be registered in src-tauri/).
 *
 *   2. Generic stdin/stdout pipe (window.__PHASER_PIPE) — used by any other
 *      desktop host (Electron, CEF, PyWebView, etc.) that injects a
 *      channel object: { send(json: string): void, onMessage(cb): void }.
 *
 * Usage (desktop host injects before main.js):
 *   window.__PHASER_TRANSPORT = 'ipc';
 *   await import('./ipc-bridge-native.js');   // installs bridge
 */

// Use the browser's built-in crypto for request IDs (no extra dep).
const nanoid = (n = 12) => crypto.randomUUID().replace(/-/g, '').slice(0, n);

// ---------------------------------------------------------------------------
// Pending request registry
// ---------------------------------------------------------------------------
const _pending = new Map(); // id -> { resolve, reject, timer }

const IPC_TIMEOUT_MS = {
    sweep:           1500,
    get_state:       2000,
    get_lab:         2000,
    get_cal_status:  2000,
    run_calibration: 5000,
    _default:        3000,
};

function _timeout(cmd) {
    return IPC_TIMEOUT_MS[cmd] ?? IPC_TIMEOUT_MS._default;
}

function _resolve(id, response) {
    const entry = _pending.get(id);
    if (!entry) return;
    clearTimeout(entry.timer);
    _pending.delete(id);
    entry.resolve(response);
}

function _reject(id, err) {
    const entry = _pending.get(id);
    if (!entry) return;
    clearTimeout(entry.timer);
    _pending.delete(id);
    entry.reject(err);
}

// ---------------------------------------------------------------------------
// Message dispatch — handles incoming NDJSON lines from the worker
// ---------------------------------------------------------------------------
function _onIncomingMessage(line) {
    let msg;
    try {
        msg = JSON.parse(line);
    } catch {
        console.warn('[IPC BRIDGE] Invalid JSON from worker:', line);
        return;
    }
    if (!msg || !msg.id) return;   // events have no id — ignored for now
    _resolve(msg.id, msg);
}

// ---------------------------------------------------------------------------
// Tauri back-end
// ---------------------------------------------------------------------------
function _createTauriBridge() {
    const { invoke } = window.__TAURI__.core;

    async function send(cmd, data) {
        // Tauri Rust command receives the full NDJSON-spec envelope as a string
        // and returns the response envelope string.
        const id = nanoid(12);
        const envelope = JSON.stringify({ id, type: 'request', cmd, data, version: '1.0' });
        const responseStr = await invoke('phaser_ipc', { envelope });
        return JSON.parse(responseStr);
    }

    return { invoke: send };
}

// ---------------------------------------------------------------------------
// Generic pipe back-end (Electron, CEF, PyWebView …)
// The host must inject: window.__PHASER_PIPE = { send(str), onMessage(cb) }
// ---------------------------------------------------------------------------
function _createPipeBridge() {
    const pipe = window.__PHASER_PIPE;

    // Register the incoming-message handler once.
    pipe.onMessage((line) => _onIncomingMessage(line));

    function send(cmd, data) {
        return new Promise((resolve, reject) => {
            const id = nanoid(12);
            const envelope = JSON.stringify({ id, type: 'request', cmd, data, version: '1.0' });

            const timer = setTimeout(() => {
                _reject(id, new Error(`IPC timeout: ${cmd}`));
            }, _timeout(cmd));

            _pending.set(id, { resolve, reject, timer });
            pipe.send(envelope + '\n');
        });
    }

    return { invoke: send };
}

// ---------------------------------------------------------------------------
// Bridge installation
// ---------------------------------------------------------------------------
function installBridge() {
    if (window.__PHASER_IPC_BRIDGE) return;   // already installed (mock or re-import)

    if (window.__TAURI__?.core?.invoke) {
        window.__PHASER_IPC_BRIDGE = _createTauriBridge();
        console.info('[IPC BRIDGE] Tauri sidecar bridge installed');
        return;
    }

    if (window.__PHASER_PIPE) {
        window.__PHASER_IPC_BRIDGE = _createPipeBridge();
        console.info('[IPC BRIDGE] Generic pipe bridge installed');
        return;
    }

    console.warn('[IPC BRIDGE] No sidecar found (no __TAURI__ and no __PHASER_PIPE)');
}

installBridge();

/*
 * nanoid replaced with crypto.randomUUID() — no vendor file needed.
 */

