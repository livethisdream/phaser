/**
 * transport-ipc.js — IPC transport adapter
 *
 * Expected bridge contract injected by desktop shell:
 *   window.__PHASER_IPC_BRIDGE.invoke(cmd, data?) -> Promise<responseEnvelope>
 */

function getBridge() {
    if (window.__PHASER_IPC_BRIDGE && typeof window.__PHASER_IPC_BRIDGE.invoke === 'function') {
        return window.__PHASER_IPC_BRIDGE;
    }
    // PyWebView exposes window.pywebview.api.invoke(...) — adapt it on demand.
    if (window.pywebview?.api?.invoke) {
        return {
            invoke: async (cmd, data = {}) => {
                const envelope = JSON.stringify({ cmd, data, version: '1.0' });
                const raw = await window.pywebview.api.invoke(envelope);
                if (typeof raw === 'string') {
                    try {
                        return JSON.parse(raw);
                    } catch {
                        return { status: 'error', message: `Bad pywebview payload: ${raw}` };
                    }
                }
                return raw;
            },
        };
    }
    return null;
}

function toLegacyResponse(resp) {
    // Frontend currently expects { status, data, message }.
    if (!resp || typeof resp !== 'object') {
        return { status: 'error', message: 'Invalid IPC response' };
    }
    if (resp.status === 'ok' || resp.status === 'error') {
        return {
            status: resp.status,
            data: resp.data,
            message: resp.message,
        };
    }
    return resp;
}

export function createIpcTransport({ onMessage, onOpen, onClose, onLog } = {}) {
    let _connected = false;
    let _sweepInFlight = false;
    let _connectRetryTimer = null;

    function _scheduleReconnect() {
        if (_connectRetryTimer) return;
        _connectRetryTimer = setTimeout(() => {
            _connectRetryTimer = null;
            connect();
        }, 500);
    }

    async function _invoke(cmd, data = {}) {
        const bridge = getBridge();
        if (!bridge || typeof bridge.invoke !== 'function') {
            throw new Error('IPC bridge is unavailable');
        }
        const resp = await bridge.invoke(cmd, data);
        return toLegacyResponse(resp);
    }

    function connect() {
        const bridge = getBridge();
        if (!bridge || typeof bridge.invoke !== 'function') {
            // On desktop, pywebview bridge can appear shortly after page load.
            // Stay in "starting" state and retry instead of forcing offline.
            if (_connected) {
                _connected = false;
                onClose?.();
            }
            onLog?.('info', 'IPC', 'Waiting for IPC bridge...');
            _scheduleReconnect();
            return;
        }

        if (_connected) return;
        _connected = true;
        onOpen?.();
        onLog?.('info', 'IPC', 'Connected to IPC bridge');
    }

    // PyWebView raises this when window.pywebview becomes available.
    window.addEventListener('pywebviewready', () => {
        onLog?.('info', 'IPC', 'pywebviewready event received');
        connect();
    });

    function send(msg) {
        if (!_connected || !msg || msg.cmd !== 'sweep' || _sweepInFlight) {
            return;
        }
        _sweepInFlight = true;
        _invoke('sweep', { state: msg.state || {} })
            .then((resp) => onMessage?.(resp))
            .catch((err) => {
                onMessage?.({ status: 'error', message: String(err) });
                // If the bridge dropped out mid-run, reconnect in the background.
                _connected = false;
                _scheduleReconnect();
            })
            .finally(() => {
                _sweepInFlight = false;
            });
    }

    async function getState() {
        return _invoke('get_state', {});
    }

    async function getLabPreset(idx) {
        return _invoke('get_lab', { lab_idx: idx });
    }

    async function getCalibrationStatus() {
        return _invoke('get_cal_status', {});
    }

    async function runCalibration(taskName) {
        return _invoke('run_calibration', { task_name: taskName });
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
