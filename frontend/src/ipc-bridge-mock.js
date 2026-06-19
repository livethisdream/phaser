/**
 * ipc-bridge-mock.js
 *
 * Dev-only bridge shim so IPC mode can be exercised in a browser without a
 * desktop host. Enable with `?mockIpc=1` or `window.__PHASER_IPC_MOCK = true`
 * before app startup.
 */

function shouldEnableMock() {
    if (window.__PHASER_IPC_MOCK === true) return true;
    const params = new URLSearchParams(window.location.search);
    return params.get('mockIpc') === '1';
}

function buildLabPreset(labIdx) {
    const base = {
        mode: 'Beam Sweep',
        gainList: [100, 100, 100, 100, 100, 100, 100, 100],
        phaseList: [0, 0, 0, 0, 0, 0, 0, 0],
        steer_res: 2.8125,
        bits: 7,
        ignore_res: true,
        BW: 10,
        B0_Gain: 1.0,
        B1_Gain: 1.0,
        Beam0_Phase: 0,
        Beam1_Phase: 0,
        ui_tab: 'tab-rect',
    };

    if (labIdx === 1) return { ...base, mode: 'Static Phase', ui_tab: 'tab-fft' };
    if (labIdx === 6) return { ...base, mode: 'Signal vs Time', steer_res: 1.0, ignore_res: false, gainList: [6, 27, 66, 100, 100, 66, 27, 6], ui_tab: 'tab-tracking' };
    if (labIdx === 8) return { ...base, mode: 'Tracking', gainList: [6, 27, 66, 100, 100, 66, 27, 6], ui_tab: 'tab-tracking' };
    return base;
}

function createMockBridge() {
    let sweepTick = 0;
    const calStatus = {
        running: false,
        task: null,
        pid: null,
        started_at: null,
        returncode: null,
        last_lines: [],
    };

    function runCalibration(taskName) {
        if (!['phaser_cal', 'find_hb100'].includes(taskName)) {
            return { status: 'error', message: `Unknown calibration task: ${taskName}` };
        }
        if (calStatus.running) {
            return { status: 'error', message: 'A calibration task is already running' };
        }

        calStatus.running = true;
        calStatus.task = taskName;
        calStatus.started_at = Date.now() / 1000;
        calStatus.returncode = null;
        calStatus.last_lines = [`Starting ${taskName}...`];

        setTimeout(() => {
            calStatus.last_lines.push(`${taskName} complete`);
            calStatus.running = false;
            calStatus.returncode = 0;
        }, 1800);

        return { status: 'ok', message: `Started ${taskName}` };
    }

    function processSweep(state = {}) {
        sweepTick += 1;
        const phaseValues = Array.isArray(state.PhaseValues) && state.PhaseValues.length
            ? state.PhaseValues
            : Array.from({ length: 181 }, (_, i) => i - 90);

        const target = 12 + 8 * Math.sin(sweepTick / 8);
        const gain = phaseValues.map((a) => {
            const diff = Math.abs(a - target);
            return -6 - 0.025 * diff * diff;
        });

        const delta = gain.map((g) => g - 14);
        const beamPhase = phaseValues.map((a) => Math.max(-1, Math.min(1, (a - target) / 90)));
        const err = beamPhase.map((v) => v * 0.5);

        const n = 1024;
        const xf = Array.from({ length: n }, (_, i) => (i - n / 2) * 3000);
        const maxGain = xf.map((x) => {
            const norm = (x - 150000) / 90000;
            return -78 + 58 * Math.exp(-(norm * norm));
        });

        return {
            status: 'ok',
            data: {
                ArrayGain: gain,
                ArrayAngle: phaseValues,
                ArrayDelta: delta,
                ArrayBeamPhase: beamPhase,
                ArrayError: err,
                max_gain: maxGain,
                xf,
            },
        };
    }

    return {
        invoke: async (cmd, data = {}) => {
            if (cmd === 'get_state') {
                return {
                    status: 'ok',
                    data: {
                        SignalFreq: 10.525e9,
                        Rx_freq: 2.4e9,
                        Rx_gain: 0,
                        Tx_gain: -40,
                        Averages: 1,
                        d: 0.014,
                        BW: 10,
                        sim_mode: true,
                        lab_presets_supported: true,
                    },
                };
            }

            if (cmd === 'get_lab') {
                const labIdx = Number(data.lab_idx);
                if (!Number.isInteger(labIdx) || labIdx < 1 || labIdx > 8) {
                    return { status: 'error', message: 'Lab index must be in range 1..8' };
                }
                return { status: 'ok', data: buildLabPreset(labIdx) };
            }

            if (cmd === 'run_calibration') {
                return runCalibration(data.task_name);
            }

            if (cmd === 'get_cal_status') {
                return { status: 'ok', data: { ...calStatus } };
            }

            if (cmd === 'sweep') {
                return processSweep(data.state || {});
            }

            return { status: 'error', message: `Unknown command ${cmd}` };
        },
    };
}

if (shouldEnableMock() && !window.__PHASER_IPC_BRIDGE) {
    window.__PHASER_TRANSPORT = 'ipc';
    window.__PHASER_IPC_BRIDGE = createMockBridge();
    // Keep noisy logs out of production; this only runs when explicitly enabled.
    console.info('[IPC MOCK] Enabled via ?mockIpc=1 or __PHASER_IPC_MOCK=true');
}

