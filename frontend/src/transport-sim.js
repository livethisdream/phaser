/**
 * transport-sim.js — the browser-local simulator, behind the transport API.
 *
 * Exposes exactly what transport-web.js does, so main.js cannot tell the
 * difference: same invoke/send surface, same callbacks, same message shapes.
 * The work happens in ./sim/worker.js; this file is the adapter.
 *
 * Reached by ?sim=1, window.__PHASER_SIM, or a build with VITE_TRANSPORT=sim
 * (which is how the GitHub Pages demo ships, since a static site has no backend
 * to talk to).
 */

export function createSimTransport(callbacks = {}) {
    let worker = null;
    let connected = false;
    let requestId = 0;
    const pending = new Map();

    function connect() {
        if (worker) return;

        // `new URL(..., import.meta.url)` is the form Vite understands for
        // bundling a worker; a bare string path would not survive the build.
        worker = new Worker(new URL('./sim/worker.js', import.meta.url), {
            type: 'module',
        });

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
                callbacks.onLog?.('info', 'SIM', 'Simulator ready — no hardware required');
                callbacks.onOpen?.();
                callbacks.onConnectionStatus?.({ connected: true });
                // main.js listens for this to skip straight to a ready state.
                callbacks.onMessage?.({ type: 'backend-ready', state: data.state });
                return;
            }

            if (data.type === 'sweep' || data.type === 'sweep_data') {
                callbacks.onSweepData?.(data.data || data);
                return;
            }

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
            callbacks.onConnectionStatus?.({ connected: false });
        };
    }

    function send(msg) {
        if (worker) worker.postMessage(msg);
    }

    function invoke(cmd, args = {}) {
        return new Promise((resolve, reject) => {
            if (!worker) {
                reject(new Error('Simulator not started'));
                return;
            }
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
        getState: () => invoke('get_state'),
        getCalibrationStatus: () => invoke('get_calibration_status'),
        runCalibration: (taskName) => invoke('run_calibration', { task_name: taskName }),
        cancelCalibration: () => invoke('cancel_calibration'),
        // No lab-preset endpoint: main.js's localLabPreset() fallback already
        // serves these client-side, and duplicating them here would be a second
        // copy to keep in step.
        getLabPreset: () => Promise.reject(new Error('Lab presets are served locally in simulation')),
        get isConnected() { return connected; },
    };
}
