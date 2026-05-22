/**
 * transport-web.js — WebSocket + HTTP transport adapter
 */

function resolveWsUrl() {
    // Runtime injection point: set window.__PHASER_WS_URL in a <script> tag
    // for environments that need an explicit override (e.g. desktop shell).
    if (window.__PHASER_WS_URL) return window.__PHASER_WS_URL;
    const envUrl = import.meta.env?.VITE_WS_URL;
    if (envUrl) return envUrl;
    const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
    return `${scheme}://${window.location.host}/ws`;
}

function resolveBaseUrl() {
    // Runtime injection point: set window.__PHASER_BASE_URL for desktop shells
    // where REST calls may not share the same origin.
    return window.__PHASER_BASE_URL || '';
}

export function createWebTransport({ onMessage, onOpen, onClose, onLog } = {}) {
    let _ws = null;
    let _connected = false;

    function connect() {
        const url = resolveWsUrl();
        onLog?.('info', 'WS', `Connecting to ${url}`);
        _ws = new WebSocket(url);

        _ws.onopen = () => {
            _connected = true;
            onOpen?.();
        };

        _ws.onclose = () => {
            _connected = false;
            onClose?.();
            setTimeout(connect, 2000);
        };

        _ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                onMessage?.(msg);
            } catch (err) {
                onLog?.('error', 'WS', `Bad message payload: ${err}`);
            }
        };
    }

    function send(msg) {
        if (_connected && _ws?.readyState === WebSocket.OPEN) {
            _ws.send(JSON.stringify(msg));
        }
    }

    async function _get(path) {
        const resp = await fetch(resolveBaseUrl() + path);
        return resp.json();
    }

    async function _post(path) {
        const resp = await fetch(resolveBaseUrl() + path, { method: 'POST' });
        return resp.json();
    }

    async function getState() {
        return _get('/api/state');
    }

    async function getLabPreset(idx) {
        return _get(`/api/lab/${idx}`);
    }

    async function getCalibrationStatus() {
        return _get('/api/calibration/status');
    }

    async function runCalibration(taskName) {
        return _post(`/api/calibration/${taskName}`);
    }

    return {
        connect,
        send,
        get connected() { return _connected; },
        getState,
        getLabPreset,
        getCalibrationStatus,
        runCalibration,
    };
}

