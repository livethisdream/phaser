/**
 * worker.js — runs the simulator off the main thread.
 *
 * A sweep is ~160 angles x 16k complex samples plus a 16k FFT. On the main
 * thread that visibly janks the UI; here the page stays responsive and plots
 * keep animating while a sweep is in flight.
 *
 * The message protocol deliberately mirrors the WebSocket JSON that
 * phaser_headless.py speaks, so transport-sim.js is a thin adapter rather than
 * a second protocol to keep in step.
 */

import { createEngine } from './engine.js';

const engine = createEngine();

let sweeping = false;
let sweepTimer = null;

function post(msg) {
    self.postMessage(msg);
}

/** Emit one sweep frame, then yield so a stop/set_state can be processed. */
function sweepLoop() {
    if (!sweeping) return;
    try {
        post({ type: 'sweep', timestamp: Date.now() / 1000, data: engine.doSweep() });
    } catch (err) {
        post({ type: 'error', message: String(err?.message || err) });
        sweeping = false;
        return;
    }
    // setTimeout(0) rather than a tight loop: the macrotask boundary is what
    // lets queued messages land between frames.
    sweepTimer = setTimeout(sweepLoop, 0);
}

function stopSweep() {
    sweeping = false;
    if (sweepTimer) { clearTimeout(sweepTimer); sweepTimer = null; }
}

// --- calibration -----------------------------------------------------------
//
// phaser_cal / find_hb100 shell out to Python on the Pi. There is no such thing
// here, so the run is scripted -- but in the exact shape get_calibration_status
// returns, so main.js's polling, log pane and modal need no special case.

const CAL_SCRIPTS = {
    find_hb100: [
        'Searching for HB100 source...',
        'Sweeping 10.0 - 11.0 GHz',
        'Peak found at 10.525000 GHz',
        'Saved signal frequency to calibration store',
    ],
    phaser_cal: [
        'Starting phase calibration...',
        'Calibrating element pairs 1-2, 3-4, 5-6, 7-8',
        'Gain calibration complete',
        'Phase calibration complete',
        'Saved calibration to store',
    ],
};

let cal = { running: false, task: null, lines: [], returncode: null, startedAt: null };
let calTimer = null;

function calibrationStatus() {
    return {
        status: 'ok',
        running: cal.running,
        task: cal.task,
        pid: cal.running ? 1 : null,
        started_at: cal.startedAt,
        returncode: cal.returncode,
        success: cal.returncode === 0,
        last_lines: [...cal.lines],
        simulated: true,
    };
}

function runCalibration(taskName) {
    const script = CAL_SCRIPTS[taskName];
    if (!script) {
        return { status: 'error', message: `Unknown calibration task: ${taskName}` };
    }
    if (cal.running) {
        return { status: 'error', message: 'A calibration task is already running' };
    }

    cal = {
        running: true,
        task: taskName,
        lines: ['[simulated run — no hardware is being calibrated]'],
        returncode: null,
        startedAt: Date.now() / 1000,
    };

    let step = 0;
    calTimer = setInterval(() => {
        if (step < script.length) {
            cal.lines.push(script[step++]);
            return;
        }
        clearInterval(calTimer);
        calTimer = null;
        cal.running = false;
        cal.returncode = 0;
    }, 600);

    return { status: 'ok', message: `Started ${taskName}` };
}

function cancelCalibration() {
    if (!cal.running) return { status: 'ok', message: 'No calibration running' };
    if (calTimer) { clearInterval(calTimer); calTimer = null; }
    cal.running = false;
    cal.returncode = -1;
    cal.lines.push('Cancelled');
    return { status: 'ok', message: 'Cancelled' };
}

// --- command dispatch ------------------------------------------------------

function handle(cmd, data) {
    switch (cmd) {
        case 'ping':
            return { status: 'ok', message: 'pong' };
        case 'get_state':
            return engine.getState();
        case 'set_state':
            return engine.setState(data.state || {});
        case 'start_sweep':
            if (!sweeping) { sweeping = true; sweepLoop(); }
            return { status: 'ok', mode: 'sweep' };
        case 'stop_sweep':
            stopSweep();
            return { status: 'ok', mode: 'idle' };
        case 'sweep':
            // The autosweep interval sends this; the frame loop is already
            // producing data, so it is a no-op, exactly as on the backend.
            return { status: 'ok' };
        case 'set_rx_gain':
            return engine.setState({ Rx_gain: data.gain });
        case 'set_tx_gain':
            return engine.setState({ Tx_gain: data.gain });
        case 'set_signal_freq':
            return engine.setState({ SignalFreq: data.freq });
        case 'set_taper':
            return engine.setState({ gainList: data.gainList });
        case 'set_tx_mode':
            return engine.setState({ Tx_mode: data.mode });
        case 'set_sweep_params':
            return engine.setSweepParams(data);
        case 'run_calibration':
            return runCalibration(data.task_name);
        case 'get_calibration_status':
            return calibrationStatus();
        case 'cancel_calibration':
            return cancelCalibration();

        // Things a static page genuinely cannot do. Say so plainly rather than
        // failing in a way that looks like a bug.
        case 'start_cw_radar':
        case 'stop_cw_radar':
        case 'set_cw_radar_params':
        case 'get_cw_radar_state':
            return { status: 'error', message: 'CW radar is not available in simulation' };
        case 'reboot_phaser':
            return { status: 'error', message: 'There is no hardware to reboot in simulation' };

        default:
            return { status: 'error', message: `Unknown command: ${cmd}` };
    }
}

self.onmessage = (event) => {
    const msg = event.data || {};
    const { cmd, id, ...rest } = msg;
    const data = { ...rest, ...(rest.data || {}) };

    let response;
    try {
        response = handle(cmd, data);
    } catch (err) {
        response = { status: 'error', message: String(err?.message || err) };
    }
    if (id) post({ ...response, id });
};

// Tell the transport the engine is up and hand over the initial state, which
// is what main.js's readiness probe is waiting for.
post({ type: 'ready', state: engine.getState() });
