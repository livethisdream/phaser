// WebSocket transport for browser mode

const BACKEND_URL_KEY = 'phaser.backendUrl';

/**
 * An explicitly configured backend, if there is one.
 *
 * `?backend=wss://host/ws` wins (shareable, and a way out of a bad saved
 * value); otherwise whatever the Configuration pane last saved.
 *
 * This exists because the derivation below is same-origin only, which is
 * right when the Pi serves the page and wrong when it does not. The hosted
 * demo on GitHub Pages is the case in point: without an override it resolves
 * to wss://<user>.github.io/ws, which is GitHub's server, not a Phaser.
 */
export function getBackendUrlOverride() {
    const fromQuery = new URLSearchParams(window.location.search).get('backend');
    if (fromQuery) return fromQuery.trim();
    try {
        return (localStorage.getItem(BACKEND_URL_KEY) || '').trim();
    } catch {
        // Private mode / blocked storage. Not worth failing over.
        return '';
    }
}

export function setBackendUrlOverride(url) {
    try {
        const trimmed = (url || '').trim();
        if (trimmed) localStorage.setItem(BACKEND_URL_KEY, trimmed);
        else localStorage.removeItem(BACKEND_URL_KEY);
        return true;
    } catch {
        return false;
    }
}

// Where the backend's WebSocket lives, given how this page was served.
//
// Plain http on the LAN: the backend serves the frontend on 8080 and the
// WebSocket on 8765, so point straight at 8765.
//
// https: the page is behind a TLS reverse proxy -- a Tailscale Funnel, say.
// Two things change. The browser refuses a plain ws:// from an https:// page
// as mixed content, and 8765 is not exposed through that proxy anyway (a
// Funnel only publishes 443/8443/10000). So go same-origin over wss:// and
// let the proxy route /ws to 8765. The backend's handler ignores the request
// path, so it does not care whether the proxy strips the prefix.
export function autoBackendWsUrl() {
    if (window.location.protocol === 'https:') {
        return `wss://${window.location.host}/ws`;
    }
    const host = window.location.hostname || 'localhost';
    return `ws://${host}:8765`;
}

function backendWsUrl() {
    return getBackendUrlOverride() || autoBackendWsUrl();
}

export function createWebTransport(callbacks = {}) {
    let ws = null;
    let reconnectTimer = null;
    let pendingRequests = new Map();
    let requestId = 0;

    function connect() {
        if (ws && ws.readyState === WebSocket.OPEN) return;

        ws = new WebSocket(backendWsUrl());

        ws.onopen = () => {
            callbacks.onLog?.('info', 'WS', `Connected to ${ws.url}`);
            callbacks.onOpen?.();
        };

        ws.onclose = () => {
            callbacks.onLog?.('info', 'WS', 'Disconnected');
            callbacks.onClose?.();
            // Reconnect after 2 seconds
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = (err) => {
            callbacks.onLog?.('error', 'WS', `Connection error`);
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                // Check if this is a response to a pending request
                if (data.id && pendingRequests.has(data.id)) {
                    const { resolve } = pendingRequests.get(data.id);
                    pendingRequests.delete(data.id);
                    resolve(data);
                    return;
                }

                // Handle sweep data
                if (data.type === 'sweep_data' || data.type === 'sweep') {
                    // Extract nested data if present
                    callbacks.onSweepData?.(data.data || data);
                    return;
                }

                // Handle other messages
                callbacks.onMessage?.(data);
            } catch (e) {
                callbacks.onLog?.('error', 'WS', `Parse error: ${e}`);
            }
        };
    }

    function send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(msg));
        }
    }

    function invoke(cmd, args = {}) {
        return new Promise((resolve, reject) => {
            if (!ws || ws.readyState !== WebSocket.OPEN) {
                reject(new Error('Not connected'));
                return;
            }
            const id = `req_${++requestId}`;
            pendingRequests.set(id, { resolve, reject });
            ws.send(JSON.stringify({ cmd, ...args, id }));

            // Timeout after 30s
            setTimeout(() => {
                if (pendingRequests.has(id)) {
                    pendingRequests.delete(id);
                    reject(new Error('Timeout'));
                }
            }, 30000);
        });
    }

    // High-level API methods
    function getState() {
        return invoke('get_state');
    }

    function getCalibrationStatus() {
        return invoke('get_calibration_status');
    }

    function runCalibration(taskName) {
        return invoke('run_calibration', { task_name: taskName });
    }

    function cancelCalibration() {
        return invoke('cancel_calibration');
    }

    function getLabPreset(labIdx) {
        return invoke('get_lab_preset', { lab: labIdx });
    }

    return {
        connect,
        send,
        invoke,
        getState,
        getCalibrationStatus,
        runCalibration,
        cancelCalibration,
        getLabPreset,
        get isConnected() { return ws && ws.readyState === WebSocket.OPEN; }
    };
}
