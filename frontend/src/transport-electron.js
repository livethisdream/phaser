/**
 * transport-electron.js — Electron IPC transport adapter for ZMQ backend
 *
 * This transport connects to a remote Phaser ZMQ server via Electron's main process.
 * Data streams in via ZMQ PUB/SUB (onSweepData), commands go via ZMQ REQ/REP.
 */

function toLegacyResponse(resp) {
    if (!resp || typeof resp !== 'object') {
        return { status: 'error', message: 'Invalid response' };
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

export function createElectronTransport({ onMessage, onOpen, onClose, onLog, onCwRadarData } = {}) {
    let _connected = false;
    let _sweeping = false;
    let _cwRadarActive = false;

    async function _invoke(cmd, data = {}) {
        const resp = await window.electronAPI.invokeBackend(cmd, data);
        return toLegacyResponse(resp);
    }

    async function connect() {
        try {
            // Listen for backend ready event (ZMQ connected)
            window.electronAPI.onBackendReady(() => {
                _connected = true;
                onLog?.('info', 'Electron', 'ZMQ backend connected');
                onOpen?.();
            });

            // Listen for connection status changes
            window.electronAPI.onConnectionStatus((status) => {
                _connected = status.connected;
                onLog?.('info', 'Electron', `Connection status: ${status.connected ? 'connected' : 'disconnected'}`);
                if (status.connected) {
                    onOpen?.();
                } else {
                    _sweeping = false;
                    onClose?.();
                }
            });

            // Listen for streaming sweep data from ZMQ PUB
            window.electronAPI.onSweepData((data) => {
                if (data && _sweeping) {
                    onMessage?.({ status: 'ok', data });
                }
            });

            // Listen for CW radar data from ZMQ PUB
            window.electronAPI.onCwRadarData?.((data) => {
                if (data && _cwRadarActive) {
                    onCwRadarData?.(data);
                }
            });

            // Check initial connection status
            const status = await window.electronAPI.getConnectionStatus();
            _connected = status?.connected || false;
            if (_connected) {
                onOpen?.();
            }
            onLog?.('info', 'Electron', `Initial connection: ${_connected ? 'connected' : 'waiting'}`);

        } catch (e) {
            onLog?.('error', 'Electron', `Connection failed: ${e}`);
            _connected = false;
            onClose?.();
        }
    }

    function send(msg) {
        if (!_connected || !msg || msg.cmd !== 'sweep') {
            return;
        }

        // For ZMQ architecture, start/stop sweep controls the data stream
        if (!_sweeping) {
            _sweeping = true;
            window.electronAPI.startSweep(msg.state || {})
                .then((resp) => {
                    if (resp.status !== 'ok') {
                        _sweeping = false;
                        onMessage?.({ status: 'error', message: resp.message || 'Failed to start sweep' });
                    }
                })
                .catch((err) => {
                    _sweeping = false;
                    onMessage?.({ status: 'error', message: String(err) });
                });
        } else {
            // Update state while sweeping
            _invoke('set_state', { state: msg.state }).catch(() => {});
        }
    }

    function stopSweep() {
        if (_sweeping) {
            _sweeping = false;
            window.electronAPI.stopSweep().catch(() => {});
        }
    }

    async function startCwRadar() {
        if (!_connected) return { status: 'error', message: 'Not connected' };
        if (_sweeping) stopSweep();
        _cwRadarActive = true;
        const resp = await window.electronAPI.startCwRadar();
        if (resp?.status !== 'ok') _cwRadarActive = false;
        return resp;
    }

    async function stopCwRadar() {
        _cwRadarActive = false;
        return window.electronAPI.stopCwRadar();
    }

    async function getState() {
        return _invoke('get_state', {});
    }

    async function getLabPreset(idx) {
        return _invoke('get_lab', { lab_idx: idx });
    }

    async function getCalibrationStatus() {
        return _invoke('get_calibration_status', {});
    }

    async function runCalibration(taskName) {
        return _invoke('run_calibration', { task_name: taskName });
    }

    async function cancelCalibration() {
        return _invoke('cancel_calibration', {});
    }

    async function invoke(cmd, data) {
        return _invoke(cmd, data);
    }

    async function reconnect() {
        return window.electronAPI.reconnect();
    }

    return {
        connect,
        send,
        stopSweep,
        startCwRadar,
        stopCwRadar,
        get connected() { return _connected; },
        get sweeping() { return _sweeping; },
        get cwRadarActive() { return _cwRadarActive; },
        getState,
        getLabPreset,
        getCalibrationStatus,
        runCalibration,
        cancelCalibration,
        invoke,
        reconnect,
    };
}

// Window control functions for the custom titlebar
export async function minimizeWindow() {
    return window.electronAPI.minimizeWindow();
}

export async function maximizeWindow() {
    return window.electronAPI.maximizeWindow();
}

export async function closeWindow() {
    return window.electronAPI.closeWindow();
}

export async function isMaximized() {
    return window.electronAPI.isMaximized();
}

export async function startDrag() {
    // Electron uses CSS for drag (-webkit-app-region: drag)
    return window.electronAPI.startDrag();
}
