// WebSocket transport for browser mode
export function createWebTransport(callbacks = {}) {
    let ws = null;
    let reconnectTimer = null;
    let pendingRequests = new Map();
    let requestId = 0;
    const host = window.location.hostname || 'localhost';

    function connect() {
        if (ws && ws.readyState === WebSocket.OPEN) return;

        ws = new WebSocket(`ws://${host}:8765`);

        ws.onopen = () => {
            callbacks.onLog?.('info', 'WS', 'Connected');
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
