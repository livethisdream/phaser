/**
 * transport-sim.js — the in-browser radar simulator, behind the transport API.
 *
 * Exposes exactly what transport-web.js does, so main.js cannot tell the
 * difference: same calls, same callbacks, same message shapes. The work runs
 * in ./sim/worker.js.
 */

export function createSimTransport(callbacks = {}) {
    let worker = null;
    let connected = false;
    let requestId = 0;
    const pending = new Map();

    function connect() {
        if (worker) return;
        // `new URL(..., import.meta.url)` is the form Vite bundles as a worker.
        worker = new Worker(new URL('./sim/worker.js', import.meta.url), { type: 'module' });

        worker.onmessage = (event) => {
            const data = event.data || {};
            if (data.id && pending.has(data.id)) {
                const { resolve } = pending.get(data.id);
                pending.delete(data.id);
                resolve(data);
                return;
            }
            if (data.type === 'ready') {
                connected = true;
                callbacks.onLog?.('info', 'SIM', 'Radar simulator ready — no hardware required');
                callbacks.onOpen?.();
                // The backend's first message to a new client.
                callbacks.onMessage?.({ type: 'state', data: data.state });
                return;
            }
            if (data.type === 'cw_radar_frame') { callbacks.onRadarFrame?.(data.data || data); return; }
            if (data.type === 'fmcw_radar_frame') { callbacks.onFmcwFrame?.(data.data || data); return; }
            if (data.type === 'error') {
                callbacks.onLog?.('error', 'SIM', data.message || 'Simulator error');
                return;
            }
            callbacks.onMessage?.(data);
        };

        worker.onerror = (err) => {
            callbacks.onLog?.('error', 'SIM', `Simulator failed: ${err.message || err}`);
            connected = false;
            callbacks.onClose?.();
        };
    }

    function send(msg) {
        if (worker) worker.postMessage(msg);
    }

    function invoke(cmd, args = {}) {
        return new Promise((resolve, reject) => {
            if (!worker) { reject(new Error('Simulator not started')); return; }
            const id = `sim_${++requestId}`;
            pending.set(id, { resolve, reject });
            worker.postMessage({ cmd, ...args, id });
            setTimeout(() => {
                if (pending.has(id)) {
                    pending.delete(id);
                    reject(new Error('Timeout'));
                }
            }, 30000);
        });
    }

    return {
        connect,
        send,
        invoke,
        simulated: true,
        getState: () => invoke('get_state'),
        getCwRadarState: () => invoke('get_cw_radar_state'),
        startCwRadar: (params = {}) => invoke('start_cw_radar', params),
        stopCwRadar: () => invoke('stop_cw_radar'),
        setCwRadarParams: (params) => invoke('set_cw_radar_params', params),
        getFmcwRadarState: () => invoke('get_fmcw_radar_state'),
        startFmcwRadar: (params = {}) => invoke('start_fmcw_radar', params),
        stopFmcwRadar: () => invoke('stop_fmcw_radar'),
        setFmcwRadarParams: (params) => invoke('set_fmcw_radar_params', params),
        get isConnected() { return connected; },
    };
}
