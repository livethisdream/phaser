// WebSocket transport for the radar browser app.
// Connects to the same backend WebSocket the beamforming GUI uses.
// All radar-specific commands invoke()'d here have to be implemented
// as new branches in phaser_headless.py's handle_command().

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
function backendWsUrl() {
    if (window.location.protocol === 'https:') {
        return `wss://${window.location.host}/ws`;
    }
    const host = window.location.hostname || 'localhost';
    return `ws://${host}:8765`;
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
            callbacks.onLog?.('info', 'WS', 'Connected');
            callbacks.onOpen?.();
        };

        ws.onclose = () => {
            callbacks.onLog?.('info', 'WS', 'Disconnected');
            callbacks.onClose?.();
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(connect, 2000);
        };

        ws.onerror = () => {
            callbacks.onLog?.('error', 'WS', 'Connection error');
        };

        ws.onmessage = (event) => {
            try {
                const data = JSON.parse(event.data);

                if (data.id && pendingRequests.has(data.id)) {
                    const { resolve } = pendingRequests.get(data.id);
                    pendingRequests.delete(data.id);
                    resolve(data);
                    return;
                }

                if (data.type === 'cw_radar_frame') {
                    callbacks.onRadarFrame?.(data.data || data);
                    return;
                }

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

            setTimeout(() => {
                if (pendingRequests.has(id)) {
                    pendingRequests.delete(id);
                    reject(new Error('Timeout'));
                }
            }, 30000);
        });
    }

    function getState() { return invoke('get_state'); }
    function getCwRadarState() { return invoke('get_cw_radar_state'); }
    function startCwRadar(params = {}) { return invoke('start_cw_radar', params); }
    function stopCwRadar() { return invoke('stop_cw_radar'); }
    function setCwRadarParams(params) { return invoke('set_cw_radar_params', params); }

    return {
        connect,
        send,
        invoke,
        getState,
        getCwRadarState,
        startCwRadar,
        stopCwRadar,
        setCwRadarParams,
        get isConnected() { return ws && ws.readyState === WebSocket.OPEN; }
    };
}
