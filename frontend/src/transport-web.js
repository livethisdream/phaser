/**
 * transport-web.js — WebSocket transport adapter for browser-based Phaser UI
 *
 * Connects directly to phaser_headless.py WebSocket server.
 * All commands and data flow through the WebSocket connection (no REST API).
 */

function resolveWsUrl() {
    // Runtime injection: set window.__PHASER_WS_URL for custom Pi address
    if (window.__PHASER_WS_URL) return window.__PHASER_WS_URL;

    // Vite env variable
    const envUrl = import.meta.env?.VITE_WS_URL;
    if (envUrl) return envUrl;

    // Default: WebSocket on port 8765, same host as HTTP server
    // When served from Pi at :8080, connect to :8765 on same host
    const host = window.location.hostname || 'localhost';
    const wsPort = 8765;
    return `ws://${host}:${wsPort}`;
}

export function createWebTransport({ onMessage, onOpen, onClose, onLog, onSweepData, onConnectionStatus } = {}) {
    let _ws = null;
    let _connected = false;
    let _pendingRequests = new Map();
    let _requestId = 0;

    function connect() {
        const url = resolveWsUrl();
        onLog?.('info', 'WS', `Connecting to ${url}`);

        try {
            _ws = new WebSocket(url);
        } catch (err) {
            onLog?.('error', 'WS', `Failed to create WebSocket: ${err}`);
            setTimeout(connect, 3000);
            return;
        }

        _ws.onopen = () => {
            _connected = true;
            onLog?.('info', 'WS', 'Connected to Phaser backend');
            onOpen?.();
            onConnectionStatus?.({ connected: true });
        };

        _ws.onclose = () => {
            _connected = false;
            onLog?.('warn', 'WS', 'Disconnected from backend');
            onClose?.();
            onConnectionStatus?.({ connected: false });
            // Auto-reconnect after 3 seconds
            setTimeout(connect, 3000);
        };

        _ws.onerror = (err) => {
            onLog?.('error', 'WS', `WebSocket error`);
        };

        _ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (err) {
                onLog?.('error', 'WS', `Bad message payload: ${err}`);
            }
        };
    }

    function handleMessage(msg) {
        // Route message based on type
        if (msg.type === 'sweep') {
            // Sweep data from backend
            onSweepData?.(msg.data);
        } else if (msg.type === 'cw_radar') {
            // CW radar data (if implemented)
            onMessage?.({ type: 'cw-radar-data', data: msg.data });
        } else if (msg.type === 'state') {
            // Initial state on connect
            onMessage?.({ type: 'backend-ready', state: msg.data });
        } else if (msg.type === 'response') {
            // Response to a command
            onMessage?.(msg);
        } else if (msg.type === 'error') {
            onLog?.('error', 'Backend', msg.message);
        } else {
            // Generic message
            onMessage?.(msg);
        }
    }

    function send(msg) {
        if (_connected && _ws?.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify(msg));
            return true;
        }
        return false;
    }

    async function sendCommand(cmd, data = {}) {
        return new Promise((resolve, reject) => {
            if (!_connected || _ws?.readyState !== WebSocket.OPEN) {
                reject(new Error('Not connected'));
                return;
            }

            const msg = { cmd, data };
            _ws.send(JSON.stringify(msg));

            // For now, resolve immediately since backend sends response via onMessage
            // A more robust implementation would track request IDs
            resolve({ status: 'ok' });
        });
    }

    async function getState() {
        return sendCommand('get_state');
    }

    async function startSweep() {
        return sendCommand('start_sweep');
    }

    async function stopSweep() {
        return sendCommand('stop_sweep');
    }

    async function setState(state) {
        return sendCommand('set_state', { state });
    }

    async function getCalibrationStatus() {
        return sendCommand('get_calibration_status');
    }

    async function runCalibration(taskName) {
        return sendCommand('run_calibration', { task_name: taskName });
    }

    function disconnect() {
        if (_ws) {
            _ws.close();
            _ws = null;
        }
        _connected = false;
    }

    return {
        connect,
        disconnect,
        send,
        sendCommand,
        get connected() { return _connected; },
        getState,
        startSweep,
        stopSweep,
        setState,
        getCalibrationStatus,
        runCalibration,
    };
}

