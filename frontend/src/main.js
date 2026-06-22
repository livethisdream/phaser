import './ipc-bridge-mock.js';
import { createTransport } from './transport.js';

// Application State Structure
const state = {
    Tx_mode: "Transmit Disabled",
    SignalFreq: 10.0 * 1e9,
    Rx_freq: 2.4e9, 
    Rx_gain: 0,
    Tx_gain: -40,
    gainList: [100, 100, 100, 100, 100, 100, 100, 100],
    phaseList: [0, 0, 0, 0, 0, 0, 0, 0],
    steer_res: 2.8125,
    bits: 7,
    ignore_res: true,
    PhaseValues: Array.from({length: 181}, (_, i) => i - 90),
    BW: 10,
    mode: "Beam Sweep",
    B0_Gain: 1.0,
    B1_Gain: 1.0,
    Beam0_Phase: 0,
    Beam1_Phase: 0,
    Averages: 1,
    d: 0.014
};

const calibrationState = {
    pollingTimer: null,
};
const backendProbeState = {
    ready: false,
    probing: false,
    retries: 0,
};
const BACKEND_PROBE_TIMEOUT_MS = 2000;
const BACKEND_PROBE_RETRY_MS = 2000;
const MAX_BACKEND_PROBE_RETRIES = 6;

// Internal tracking for history
let timeHistory = [];
let angleHistory = [];
let autoSweepInterval = null;
const THEME_KEY = 'phaser_theme';
const runtimeLogs = [];
const RUNTIME_LOG_LIMIT = 500;
const desktopChromeState = {
    enabled: false,
    wired: false,
    maximized: false,
    closing: false,
};
const calibrationLogState = {
    lastRunning: null,
    lastTask: null,
    lastReturnCode: null,
    lastLineCount: 0,
    lastReloadKey: null,
};

function getTheme() {
    return document.documentElement.getAttribute('data-theme') || 'dark';
}

function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function getPlotPalette() {
    const isLight = getTheme() === 'light';
    return {
        fontColor: cssVar('--text-muted') || '#94a3b8',
        gridColor: isLight ? 'rgba(15,23,42,0.15)' : 'rgba(255,255,255,0.08)',
        polarGridColor: isLight ? 'rgba(15,23,42,0.22)' : 'rgba(255,255,255,0.2)',
    };
}

function toPolarTheta(angleDeg) {
    // Map signed steering angles onto Plotly's upper semicircle:
    // -90 -> 180 (left), 0 -> 90 (up), +90 -> 0 (right).
    return 90 - angleDeg;
}

function escapeHtml(value) {
    return String(value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

function addRuntimeLog(level, source, message) {
    const now = new Date();
    const stamp = now.toLocaleTimeString();
    runtimeLogs.push({
        level: level || 'info',
        source: source || 'APP',
        message: message || '',
        stamp,
    });
    if (runtimeLogs.length > RUNTIME_LOG_LIMIT) {
        runtimeLogs.splice(0, runtimeLogs.length - RUNTIME_LOG_LIMIT);
    }
    renderRuntimeLogs();
}

function renderRuntimeLogs() {
    const consoleEl = document.getElementById('log-console');
    if (!consoleEl) return;

    consoleEl.innerHTML = runtimeLogs
        .map((entry) => {
            const line = `[${entry.stamp}] [${entry.source}] ${entry.message}`;
            return `<div class="log-line ${escapeHtml(entry.level)}">${escapeHtml(line)}</div>`;
        })
        .join('');
    consoleEl.scrollTop = consoleEl.scrollHeight;
}

function parseDesktopResponse(response) {
    if (typeof response === 'string') {
        try {
            return JSON.parse(response);
        } catch {
            return { status: 'error', message: `Bad desktop response: ${response}` };
        }
    }
    return response;
}

function isTauriHost() {
    return !!window.__TAURI_INTERNALS__;
}

function isElectronHost() {
    return !!window.__ELECTRON__;
}

function isDesktopHost() {
    return window.__PHASER_DESKTOP === true || !!window.pywebview?.api || isTauriHost() || isElectronHost();
}

function setDesktopChromeEnabled(enabled) {
    const active = Boolean(enabled);
    desktopChromeState.enabled = active;

    document.documentElement.classList.toggle('desktop-host', active);
    document.body.classList.toggle('desktop-host', active);
    document.querySelector('.app-container')?.classList.toggle('desktop-host', active);

    const titlebar = document.getElementById('desktop-titlebar');
    if (titlebar) {
        titlebar.hidden = !active;
        titlebar.setAttribute('aria-hidden', String(!active));
    }
}

function syncMaximizeButton() {
    const btn = document.getElementById('btn-window-maximize');
    if (!btn) return;

    const isMaximized = desktopChromeState.maximized;
    btn.innerText = isMaximized ? '❐' : '▢';
    const label = isMaximized ? 'Restore window' : 'Maximize window';
    btn.title = label;
    btn.setAttribute('aria-label', label);
}

function setDesktopControlsDisabled(disabled) {
    document.querySelectorAll('.desktop-titlebar__btn').forEach((btn) => {
        btn.disabled = Boolean(disabled);
    });
}

async function invokeWindowControl(action) {
    // Electron path
    if (isElectronHost()) {
        const { minimizeWindow, maximizeWindow, closeWindow, isMaximized } = await import('./transport-electron.js');
        switch (action) {
            case 'minimize':
                await minimizeWindow();
                break;
            case 'toggle_maximize':
                desktopChromeState.maximized = await maximizeWindow();
                syncMaximizeButton();
                break;
            case 'close':
                await closeWindow();
                break;
            default:
                throw new Error(`Unknown window action: ${action}`);
        }
        return { status: 'ok' };
    }

    // Tauri path
    if (isTauriHost()) {
        const { minimizeWindow, maximizeWindow, closeWindow, isMaximized } = await import('./transport-tauri.js');
        switch (action) {
            case 'minimize':
                await minimizeWindow();
                break;
            case 'toggle_maximize':
                await maximizeWindow();
                desktopChromeState.maximized = await isMaximized();
                syncMaximizeButton();
                break;
            case 'close':
                await closeWindow();
                break;
            default:
                throw new Error(`Unknown window action: ${action}`);
        }
        return { status: 'ok' };
    }

    // PyWebView path
    const api = window.pywebview?.api;
    if (!api || typeof api.window_control !== 'function') {
        throw new Error('Desktop window controls are unavailable');
    }

    const response = parseDesktopResponse(await api.window_control(action));
    if (!response || response.status !== 'ok') {
        throw new Error(response?.message || `Desktop window action failed: ${action}`);
    }

    if (response.data && typeof response.data.maximized === 'boolean') {
        desktopChromeState.maximized = response.data.maximized;
        syncMaximizeButton();
    }

    return response;
}

function wireDesktopChrome() {
    if (desktopChromeState.wired) return;
    desktopChromeState.wired = true;

    document.getElementById('btn-window-minimize')?.addEventListener('click', async () => {
        try {
            await invokeWindowControl('minimize');
        } catch (err) {
            addRuntimeLog('error', 'DESKTOP', String(err));
        }
    });

    document.getElementById('btn-window-maximize')?.addEventListener('click', async () => {
        try {
            await invokeWindowControl('toggle_maximize');
        } catch (err) {
            addRuntimeLog('error', 'DESKTOP', String(err));
        }
    });

    document.getElementById('btn-window-close')?.addEventListener('click', async () => {
        if (desktopChromeState.closing) {
            return;
        }

        try {
            desktopChromeState.closing = true;
            setDesktopControlsDisabled(true);
            await invokeWindowControl('close');
        } catch (err) {
            desktopChromeState.closing = false;
            setDesktopControlsDisabled(false);
            addRuntimeLog('error', 'DESKTOP', String(err));
        }
    });

    document.getElementById('desktop-titlebar-drag')?.addEventListener('dblclick', async (e) => {
        e.preventDefault();
        try {
            await invokeWindowControl('toggle_maximize');
        } catch (err) {
            addRuntimeLog('error', 'DESKTOP', String(err));
        }
    });

    // --- Window drag handling ---
    {
        const dragEl = document.getElementById('desktop-titlebar-drag');

        // Electron: use CSS -webkit-app-region: drag (handled in style.css)
        if (isElectronHost()) {
            // Electron handles drag natively via CSS, no JS needed
            // Just need to add the drag class
            dragEl?.classList.add('electron-drag');
        }
        // Tauri: use native OS drag (smooth, no jitter)
        else if (isTauriHost()) {
            dragEl?.addEventListener('mousedown', async (e) => {
                if (e.button !== 0) return;
                e.preventDefault();
                try {
                    const { startDrag } = await import('./transport-tauri.js');
                    await startDrag();
                } catch (err) {
                    console.error('Drag failed:', err);
                }
            });
        } else {
            // PyWebView: custom JS drag (rAF-throttled)
            let dragging = false;
            let lastX = 0, lastY = 0;
            let accumDx = 0, accumDy = 0;
            let rafPending = false;

            function flushMove() {
                rafPending = false;
                if (!dragging || (accumDx === 0 && accumDy === 0)) return;
                const dx = Math.round(accumDx);
                const dy = Math.round(accumDy);
                accumDx = 0;
                accumDy = 0;
                const api = window.pywebview?.api;
                if (api?.move_window) {
                    api.move_window(dx, dy).catch(() => {});
                }
            }

            dragEl?.addEventListener('mousedown', (e) => {
                if (e.button !== 0) return;
                dragging = true;
                lastX = e.screenX;
                lastY = e.screenY;
                accumDx = 0;
                accumDy = 0;
                e.preventDefault();
            });

            document.addEventListener('mousemove', (e) => {
                if (!dragging) return;
                accumDx += e.screenX - lastX;
                accumDy += e.screenY - lastY;
                lastX = e.screenX;
                lastY = e.screenY;
                if (!rafPending) {
                    rafPending = true;
                    requestAnimationFrame(flushMove);
                }
            });

            document.addEventListener('mouseup', (e) => {
                if (e.button !== 0) return;
                dragging = false;
                rafPending = false;
                accumDx = 0;
                accumDy = 0;
            });
        }
    }

    syncMaximizeButton();
}

function initDesktopChrome() {
    if (!isDesktopHost()) return;
    setDesktopChromeEnabled(true);
    wireDesktopChrome();
}

function applyTheme(theme, persist = true) {
    const nextTheme = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nextTheme);
    if (persist) localStorage.setItem(THEME_KEY, nextTheme);

    // Update expanded sidebar theme button
    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
        const iconEl = btn.querySelector('.theme-icon');
        const labelEl = btn.querySelector('.theme-label');
        if (iconEl) iconEl.textContent = nextTheme === 'light' ? '☀' : '☾';
        if (labelEl) labelEl.textContent = nextTheme === 'light' ? 'Light Mode' : 'Dark Mode';
        const label = nextTheme === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
        btn.title = label;
        btn.setAttribute('aria-label', label);
    }

    // Update icon sidebar theme button
    const iconBtn = document.getElementById('btn-theme-toggle-icon');
    if (iconBtn) {
        const moonIcon = iconBtn.querySelector('.icon-moon');
        const sunIcon = iconBtn.querySelector('.icon-sun');
        if (moonIcon) moonIcon.style.display = nextTheme === 'light' ? 'none' : 'block';
        if (sunIcon) sunIcon.style.display = nextTheme === 'light' ? 'block' : 'none';
        const label = nextTheme === 'light' ? 'Switch to dark theme' : 'Switch to light theme';
        iconBtn.title = label;
        iconBtn.setAttribute('aria-label', label);
    }

    if (window.Plotly) {
        try {
            applyPlotTheme();
        } catch (err) {
            // Plots may not be initialized yet.
        }
    }
}

function initTheme() {
    const stored = localStorage.getItem(THEME_KEY);
    const systemPrefersDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    const initial = stored || (systemPrefersDark ? 'dark' : 'light');
    applyTheme(initial, false);

    document.getElementById('btn-theme-toggle')?.addEventListener('click', () => {
        applyTheme(getTheme() === 'dark' ? 'light' : 'dark');
    });

    // Icon sidebar theme toggle
    document.getElementById('btn-theme-toggle-icon')?.addEventListener('click', () => {
        applyTheme(getTheme() === 'dark' ? 'light' : 'dark');
    });
}

initTheme();
addRuntimeLog('info', 'UI', 'Dashboard initialized');
initDesktopChrome();
window.addEventListener('pywebviewready', initDesktopChrome);
window.addEventListener('phaserdesktopready', initDesktopChrome);
document.getElementById('btn-clear-logs')?.addEventListener('click', () => {
    runtimeLogs.length = 0;
    renderRuntimeLogs();
    addRuntimeLog('info', 'UI', 'Logs cleared');
});
window.addEventListener('error', (event) => {
    addRuntimeLog('error', 'JS', event.message || 'Unhandled error');
});
window.addEventListener('unhandledrejection', (event) => {
    const reason = event?.reason?.message || String(event?.reason || 'Unhandled promise rejection');
    addRuntimeLog('error', 'JS', reason);
});

/* --- UI Logic: Accordions --- */

// Icon definitions for accordion headers (matching sidebar icons)
const accordionIcons = [
    // 0. Configuration
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><circle cx="12" cy="12" r="3"></circle><path d="M12 3.5v2.2M12 18.3v2.2M20.5 12h-2.2M5.7 12H3.5M18.01 5.99l-1.56 1.56M7.55 16.45l-1.56 1.56M18.01 18.01l-1.56-1.56M7.55 7.55L5.99 5.99"></path></svg>',
    // 1. Element Gains
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M6 5v14M12 5v14M18 5v14"></path><circle cx="6" cy="9" r="2"></circle><circle cx="12" cy="14" r="2"></circle><circle cx="18" cy="8" r="2"></circle></svg>',
    // 2. Phase Control
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M12 4a8 8 0 1 1-7.4 4.9"></path><path d="M4.5 4.5v5h5"></path><path d="M12 8v4l2.6 2.6"></path></svg>',
    // 3. Quantization
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M5 7.5h14v9H5z"></path><path d="M8 7.5v3M11 7.5v2M14 7.5v3M17 7.5v2"></path></svg>',
    // 4. Digital Beam Forming
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><circle cx="12" cy="12" r="2"></circle><path d="M12 4v3M12 17v3M4 12h3M17 12h3"></path><path d="M6.7 6.7l2.1 2.1M15.2 15.2l2.1 2.1M17.3 6.7l-2.1 2.1M8.8 15.2l-2.1 2.1"></path></svg>',
    // 5. Plot Options
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M4.5 18.5h15"></path><path d="M6 16l4-5 3 2 5-6"></path><circle cx="6" cy="16" r="1"></circle><circle cx="10" cy="11" r="1"></circle><circle cx="13" cy="13" r="1"></circle><circle cx="18" cy="7" r="1"></circle></svg>',
    // 6. Lab Presets
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M9 3h6"></path><path d="M10 3v6.2L6 17a2 2 0 0 0 1.78 2.9h8.44A2 2 0 0 0 18 17l-4-7.8V3"></path><path d="M7.5 15h9"></path></svg>',
];

// Populate accordion icons
document.querySelectorAll('.accordion-icon[data-icon]').forEach(iconEl => {
    const iconIdx = parseInt(iconEl.getAttribute('data-icon'), 10);
    if (iconIdx >= 0 && iconIdx < accordionIcons.length) {
        iconEl.innerHTML = accordionIcons[iconIdx];
    }
});

document.querySelectorAll('.accordion-header').forEach(button => {
    button.addEventListener('dblclick', (e) => {
        e.preventDefault();
        e.stopPropagation();
    });

    button.addEventListener('click', () => {
        const item = button.parentElement;
        item.classList.toggle('active');

        const items = Array.from(document.querySelectorAll('.accordion-item'));
        const idx = items.indexOf(item);
        if (item.classList.contains('active') && idx >= 0) {
            currentExpandedSection = idx;
            document.querySelectorAll('.sidebar-icon-btn[data-section]').forEach((iconBtn) => {
                iconBtn.classList.toggle('active', parseInt(iconBtn.dataset.section, 10) === idx);
            });
            // Scroll the header to the top of the accordion container
            setTimeout(() => {
                const accordion = document.getElementById('accordionSettings');
                if (accordion && item) {
                    const itemRect = item.getBoundingClientRect();
                    const accordionRect = accordion.getBoundingClientRect();
                    const scrollOffset = itemRect.top - accordionRect.top + accordion.scrollTop;
                    accordion.scrollTop = scrollOffset;
                }
            }, 100);
        }
    });
});

/* --- UI Logic: Tabs --- */
document.querySelectorAll('.tab-btn').forEach(button => {
    button.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));

        button.classList.add('active');
        const targetId = button.getAttribute('data-target');
        document.getElementById(targetId).classList.add('active');

        // Defer resize until after the browser has painted the newly-visible tab,
        // preventing the one-frame layout jump that occurs when Plotly measures
        // a container that was display:none at initialization time.
        requestAnimationFrame(() => {
            requestAnimationFrame(() => {
                // Resize all charts in the newly-active tab (CW Radar tab has two charts)
                requestAnimationFrame(() => {
                    const activeContent = document.querySelector('.tab-content.active');
                    if (activeContent) {
                        activeContent.querySelectorAll('[id^="chart-"]').forEach(chartEl => {
                            Plotly.Plots.resize(chartEl);
                        });
                    }
                });
            });
        });
    });
});


/* --- Initialize Sliders --- */
const gainContainer = document.getElementById('elements-gain-container');
const phaseContainer = document.getElementById('elements-phase-container');

for(let i=0; i<8; i++) {
    // Gain Sliders (Text editable combo)
    gainContainer.innerHTML += `
        <div class="element-row">
            <div class="element-header">E${i+1}</div>
            <div class="slider-group" style="margin:0">
                <div class="combo-inputs">
                    <input type="range" class="modern-slider gain-sl" data-idx="${i}" min="0" max="100" value="100">
                    <input type="number" class="modern-input-small gain-in" data-idx="${i}" min="0" max="100" value="100">
                </div>
            </div>
        </div>
    `;
    
    // Phase Sliders (Text editable combo)
    phaseContainer.innerHTML += `
        <div class="element-row">
            <div class="element-header">E${i+1}</div>
            <div class="slider-group" style="margin:0">
                <div class="combo-inputs">
                    <input type="range" class="modern-slider phase-sl" data-idx="${i}" min="-180" max="180" value="0">
                    <input type="number" class="modern-input-small phase-in" data-idx="${i}" min="-180" max="180" value="0">
                </div>
            </div>
        </div>
    `;
}

/* --- Sync Array Inputs & Sliders --- */
document.querySelectorAll('.gain-sl').forEach(el => {
    el.addEventListener('input', (e) => {
        const idx = parseInt(e.target.dataset.idx);
        const val = parseInt(e.target.value);
        document.querySelector(`.gain-in[data-idx="${idx}"]`).value = val;
        state.gainList[idx] = val;

        if (document.getElementById('opt-symmetric-taper')?.checked) {
            const symIdx = 7 - idx;
            document.querySelector(`.gain-sl[data-idx="${symIdx}"]`).value = val;
            document.querySelector(`.gain-in[data-idx="${symIdx}"]`).value = val;
            state.gainList[symIdx] = val;
        }
        syncStateToBackend();
    })
});
document.querySelectorAll('.gain-in').forEach(el => {
    el.addEventListener('input', (e) => {
        const idx = parseInt(e.target.dataset.idx);
        const val = parseInt(e.target.value) || 0;
        document.querySelector(`.gain-sl[data-idx="${idx}"]`).value = val;
        state.gainList[idx] = val;

        if (document.getElementById('opt-symmetric-taper')?.checked) {
            const symIdx = 7 - idx;
            document.querySelector(`.gain-sl[data-idx="${symIdx}"]`).value = val;
            document.querySelector(`.gain-in[data-idx="${symIdx}"]`).value = val;
            state.gainList[symIdx] = val;
        }
        syncStateToBackend();
    })
});

document.getElementById('opt-symmetric-taper')?.addEventListener('change', (e) => {
    if (e.target.checked) {
        for (let i = 0; i < 4; i++) {
            const symIdx = 7 - i;
            const val = state.gainList[i];
            document.querySelector(`.gain-sl[data-idx="${symIdx}"]`).value = val;
            document.querySelector(`.gain-in[data-idx="${symIdx}"]`).value = val;
            state.gainList[symIdx] = val;
        }
    }
});

document.querySelectorAll('.phase-sl').forEach(el => {
    el.addEventListener('input', (e) => {
        const idx = e.target.dataset.idx;
        const val = parseInt(e.target.value);
        document.querySelector(`.phase-in[data-idx="${idx}"]`).value = val;
        state.phaseList[idx] = val;
        syncStateToBackend();
    })
});
document.querySelectorAll('.phase-in').forEach(el => {
    el.addEventListener('input', (e) => {
        const idx = e.target.dataset.idx;
        const val = parseInt(e.target.value) || 0;
        document.querySelector(`.phase-sl[data-idx="${idx}"]`).value = val;
        state.phaseList[idx] = val;
        syncStateToBackend();
    })
});

document.getElementById('btn-zero-phase').addEventListener('click', () => {
    state.phaseList = [0,0,0,0,0,0,0,0];
    document.querySelectorAll('.phase-sl').forEach((el) => el.value = 0);
    document.querySelectorAll('.phase-in').forEach((el) => el.value = 0);
    syncStateToBackend();
});

/* --- Base Config Events --- */
const freqInput = document.getElementById('freq');
const updateSignalFreqFromInput = (rawVal) => {
    const ghz = parseFloat(rawVal);
    if (Number.isFinite(ghz)) {
        state.SignalFreq = ghz * 1e9;
    }
};
freqInput.addEventListener('input', (e) => updateSignalFreqFromInput(e.target.value));
freqInput.addEventListener('change', (e) => updateSignalFreqFromInput(e.target.value));

function syncStateToBackend() {
    if (!isConnected) return;
    // Send state update to backend
    transport.send({ cmd: 'set_state', data: { state } });
}

const linkComboSlider = (sliderId, inputId, stateKey, parseFunc = parseFloat, multiplier = 1) => {
    const slider = document.getElementById(sliderId);
    const input = document.getElementById(inputId);
    if (slider && input) {
        slider.addEventListener('input', (e) => {
            input.value = e.target.value;
            state[stateKey] = parseFunc(e.target.value) * multiplier;
            syncStateToBackend();
        });
        input.addEventListener('input', (e) => {
            slider.value = e.target.value;
            state[stateKey] = parseFunc(e.target.value) * multiplier;
            syncStateToBackend();
        });
    }
};

// For sliders with span display (not combo inputs)
const linkSlider = (id, stateKey, displayId, parseFunc = parseFloat, multiplier = 1) => {
    const el = document.getElementById(id);
    if(el) {
        el.addEventListener('input', (e) => {
            if(displayId) document.getElementById(displayId).innerText = e.target.value;
            state[stateKey] = parseFunc(e.target.value) * multiplier;
            syncStateToBackend();
        });
    }
};

linkComboSlider('rxgain', 'val-rx-gain', 'Rx_gain', parseInt);
linkComboSlider('txgain', 'val-tx-gain', 'Tx_gain', parseInt);
linkComboSlider('res', 'val-res', 'steer_res', parseFloat);
linkComboSlider('bits', 'val-bits', 'bits', parseInt);
linkComboSlider('b0_gain', 'val-b0g', 'B0_Gain', parseFloat);
linkComboSlider('b0_phase', 'val-b0p', 'Beam0_Phase', parseFloat);
linkComboSlider('b1_gain', 'val-b1g', 'B1_Gain', parseFloat);
linkComboSlider('b1_phase', 'val-b1p', 'Beam1_Phase', parseFloat);
linkComboSlider('bw', 'val-bw', 'BW', parseInt);

// Force-sync all combo sliders on page load (some browsers don't render initial value correctly)
document.querySelectorAll('.combo-inputs').forEach(combo => {
    const slider = combo.querySelector('input[type="range"]');
    const input = combo.querySelector('input[type="number"]');
    if (slider && input) {
        slider.value = input.value;
    }
});

// Beam Squint: show/hide based on toggle and BW > 0
function updateBeamSquintDisplay() {
    const bwMHz = state.BW;
    const measFreqGHz = state.SignalFreq / 1e9;
    const calcFreqGHz = measFreqGHz - (bwMHz / 1000);

    const infoDiv = document.getElementById('beam-squint-info');
    const infoDiv2 = document.getElementById('beam-squint-info2');
    const calcSpan = document.getElementById('beam-calc-freq');
    const measSpan = document.getElementById('beam-meas-freq');
    const showSquint = document.getElementById('opt-show-squint')?.checked;

    const show = showSquint && bwMHz > 0;
    if (infoDiv) infoDiv.style.display = show ? 'flex' : 'none';
    if (infoDiv2) infoDiv2.style.display = show ? 'flex' : 'none';
    if (calcSpan) calcSpan.textContent = calcFreqGHz.toFixed(3);
    if (measSpan) measSpan.textContent = measFreqGHz.toFixed(3);
}

document.getElementById('opt-show-squint')?.addEventListener('change', updateBeamSquintDisplay);

const bwSlider = document.getElementById('bw');
const bwInput = document.getElementById('val-bw');
if (bwSlider && bwInput) {
    bwSlider.addEventListener('input', (e) => {
        bwInput.value = e.target.value;
        state.BW = parseFloat(e.target.value);
        updateBeamSquintDisplay();
        syncStateToBackend();
    });
    bwInput.addEventListener('input', (e) => {
        bwSlider.value = e.target.value;
        state.BW = parseFloat(e.target.value);
        updateBeamSquintDisplay();
        syncStateToBackend();
    });
}

// Also update beam squint when frequency changes
const origFreqHandler = freqInput.oninput;
freqInput.addEventListener('input', () => updateBeamSquintDisplay());
freqInput.addEventListener('change', () => updateBeamSquintDisplay());

document.getElementById('ignore-res')?.addEventListener('change', (e) => {
    state.ignore_res = e.target.checked;
});

// Monopulse delta/error toggles - trigger immediate plot update
document.getElementById('opt-show-delta')?.addEventListener('change', (e) => {
    const rectEl = document.getElementById('chart-rect');
    if (rectEl && rectEl.data && rectEl.data[1]) {
        Plotly.restyle('chart-rect', { visible: e.target.checked }, [1]);
    }
});

document.getElementById('opt-show-error')?.addEventListener('change', (e) => {
    // Error function visibility is handled in updateCharts
    // Just trigger a redraw if we have data
    const rectEl = document.getElementById('chart-rect');
    if (rectEl && rectEl.data && rectEl.data[2]) {
        Plotly.restyle('chart-rect', { visible: e.target.checked }, [2]);
    }
    // Show/hide the right y-axis
    Plotly.relayout('chart-rect', {
        'yaxis2.visible': e.target.checked,
        'yaxis2.showticklabels': e.target.checked
    });
});

function applyInitialStateToControls() {
    document.getElementById('freq').value = (state.SignalFreq / 1e9).toFixed(3);

    const rx = document.getElementById('rxgain');
    const tx = document.getElementById('txgain');
    if (rx) {
        rx.value = String(state.Rx_gain);
        document.getElementById('val-rx-gain').value = String(state.Rx_gain);
    }
    if (tx) {
        tx.value = String(state.Tx_gain);
        document.getElementById('val-tx-gain').value = String(state.Tx_gain);
    }
    const bw = document.getElementById('bw');
    if (bw) {
        bw.value = String(state.BW);
        document.getElementById('val-bw').value = String(state.BW);
    }

    const res = document.getElementById('res');
    const bits = document.getElementById('bits');
    const ignoreRes = document.getElementById('ignore-res');
    if (res) {
        res.value = String(state.steer_res);
        document.getElementById('val-res').value = String(state.steer_res);
    }
    if (bits) {
        bits.value = String(state.bits);
        document.getElementById('val-bits').value = String(state.bits);
    }
    if (ignoreRes) {
        ignoreRes.checked = Boolean(state.ignore_res);
    }

    // Update beam squint display based on current BW
    updateBeamSquintDisplay();
}

function updateHardwareConnectionStatus(connected) {
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    console.log('[HW] updateHardwareConnectionStatus:', connected);
    addRuntimeLog('info', 'HW', `Hardware connected: ${connected}`);
    if (connected) {
        dot?.classList.remove('disconnected');
        dot?.classList.add('connected');
        if (text) text.innerText = 'Connected';
    } else {
        dot?.classList.remove('connected');
        dot?.classList.add('disconnected');
        if (text) text.innerText = 'Hardware Offline';
    }
}

async function loadStateFromServer() {
    try {
        const msg = await transport.getState();
        if (msg.status !== 'ok' || !msg.data) return;

        if (Number.isFinite(msg.data.SignalFreq)) state.SignalFreq = msg.data.SignalFreq;
        if (Number.isFinite(msg.data.Rx_freq)) state.Rx_freq = msg.data.Rx_freq;
        if (Number.isFinite(msg.data.Rx_gain)) state.Rx_gain = msg.data.Rx_gain;
        if (Number.isFinite(msg.data.Tx_gain)) state.Tx_gain = msg.data.Tx_gain;
        if (Number.isFinite(msg.data.Averages)) state.Averages = msg.data.Averages;
        if (Number.isFinite(msg.data.d)) state.d = msg.data.d;
        if (Number.isFinite(msg.data.BW)) state.BW = msg.data.BW;

        // Update hardware connection indicator
        updateHardwareConnectionStatus(msg.data.hardware_connected ?? false);

        applyInitialStateToControls();
    } catch (err) {
        console.warn('Failed to load initial state from server:', err);
    }
}

applyInitialStateToControls();
setBackendStatus('starting', 'Backend: Starting...');
updateSweepAvailability();
// Initial load now happens through readiness probe once connected.

document.getElementById('txMode')?.addEventListener('change', e => state.Tx_mode = e.target.value);

/* --- Plot Options Combo Inputs --- */
const setupCombo = (sliderId, inputId) => {
    const sl = document.getElementById(sliderId);
    const inpt = document.getElementById(inputId);
    sl.addEventListener('input', e => { inpt.value = e.target.value; updatePlotLimits(); });
    inpt.addEventListener('input', e => { sl.value = e.target.value; updatePlotLimits(); });
}
setupCombo('xmin', 'val-xmin');
setupCombo('xmax', 'val-xmax');
setupCombo('ymin', 'val-ymin');
setupCombo('ymax', 'val-ymax');


// Tapers
const applyTaper = (gains) => {
    state.gainList = gains;
    gains.forEach((val, idx) => {
        document.querySelectorAll('.gain-sl')[idx].value = val;
        document.querySelectorAll('.gain-in')[idx].value = val;
    });
    syncStateToBackend();
};
document.getElementById('taper-rect').addEventListener('click', () => applyTaper([100,100,100,100,100,100,100,100]));
document.getElementById('taper-cheb').addEventListener('click', () => applyTaper([4,23,62,100,100,62,23,4]));
document.getElementById('taper-hann').addEventListener('click', () => applyTaper([12,43,77,100,100,77,43,12]));
document.getElementById('taper-black').addEventListener('click', () => applyTaper([6,27,66,100,100,66,27,6]));

/* --- Plotly Setup --- */
function getLayoutBase() {
     const palette = getPlotPalette();
     return {
         paper_bgcolor: 'transparent',
         plot_bgcolor: 'transparent',
         font: { color: palette.fontColor, family: "'Outfit', sans-serif" },
         margin: { t: 20, r: 15, l: 45, b: 35 },
         showlegend: false,
         hovermode: "closest"
     };
}

function applyPlotTheme() {
    const palette = getPlotPalette();
    const common = {
        'font.color': palette.fontColor,
        'paper_bgcolor': 'transparent',
        'plot_bgcolor': 'transparent',
    };

    Plotly.relayout('chart-rect', {
        ...common,
        'xaxis.gridcolor': palette.gridColor,
        'yaxis.gridcolor': palette.gridColor,
    });

    Plotly.relayout('chart-polar', {
        ...common,
        'polar.bgcolor': 'transparent',
        'polar.radialaxis.color': palette.fontColor,
        'polar.angularaxis.color': palette.fontColor,
        'polar.radialaxis.gridcolor': palette.polarGridColor,
        'polar.angularaxis.gridcolor': palette.polarGridColor,
    });

    Plotly.relayout('chart-fft', {
        ...common,
        'xaxis.gridcolor': palette.gridColor,
        'yaxis.gridcolor': palette.gridColor,
    });

    Plotly.relayout('chart-tracking', {
        ...common,
        'xaxis.gridcolor': palette.gridColor,
        'yaxis.gridcolor': palette.gridColor,
    });
}

Plotly.newPlot('chart-rect', [
    // Trace 0: Sum beam
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Sum', line: { color: '#6366f1', width: 3, shape: 'spline' }, fill: 'tozeroy', fillcolor: 'rgba(99, 102, 241, 0.1)' },
    // Trace 1: Delta beam (hidden by default)
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Delta', line: { color: '#f59e0b', width: 2, dash: 'dash', shape: 'spline' }, visible: false },
    // Trace 2: Error function (hidden by default, secondary y-axis)
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Error', yaxis: 'y2', line: { color: '#ef4444', width: 2, shape: 'spline' }, visible: false }
], Object.assign({}, getLayoutBase(), {
    xaxis: {
        title: 'Steering Angle (°)',
        gridcolor: getPlotPalette().gridColor,
        griddash: 'dash',
        range: [-90, 90]
    },
    yaxis: {
        title: 'Magnitude (dBFS)',
        gridcolor: getPlotPalette().gridColor,
        griddash: 'dash',
        range: [-50, 0]
    },
    yaxis2: {
        title: 'Error Function',
        overlaying: 'y',
        side: 'right',
        range: [-1, 1],
        gridcolor: 'rgba(239, 68, 68, 0.2)',
        griddash: 'dot',
        showgrid: false,
        visible: false,
        showticklabels: false
    },
    showlegend: true,
    legend: { x: 1, xanchor: 'right', y: 1, bgcolor: 'rgba(0,0,0,0)' }
}), {displayModeBar: false, responsive: true});

Plotly.newPlot('chart-polar', [{
    r: [], theta: [], type: 'scatterpolar', mode: 'lines', line: { color: '#10b981', width: 3, shape: 'spline' }, fill: 'toself', fillcolor: 'rgba(16, 185, 129, 0.1)'
}], Object.assign({}, getLayoutBase(), {
    polar: {
        sector: [0, 180],
        bgcolor: 'transparent',
        radialaxis: {
            visible: true,
            range: [-50, 0],
            dtick: 10,
            angle: 90,
            tickangle: 90,
            color: getPlotPalette().fontColor,
            gridcolor: getPlotPalette().polarGridColor,
            griddash: 'solid',
            showgrid: true,
            showline: false
        },
        angularaxis: {
            visible: true,
            thetaunit: 'degrees',
            direction: "counterclockwise",
            rotation: 0,
            tickmode: 'array',
            tickvals: [180, 150, 120, 90, 60, 30, 0],
            ticktext: ['-90', '-60', '-30', '0', '30', '60', '90'],
            color: getPlotPalette().fontColor,
            gridcolor: getPlotPalette().polarGridColor,
            griddash: 'solid',
            showgrid: true,
            showline: false
        }
    }
}), {displayModeBar: false, responsive: true});

Plotly.newPlot('chart-fft', [{
    x: [], y: [], type: 'scatter', mode: 'lines', line: { color: '#8b5cf6', width: 2 }
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Frequency (MHz)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: 'Amplitude (dBFS)', range: [-100, 0], gridcolor: getPlotPalette().gridColor, griddash: 'dash' }
}), {displayModeBar: false, responsive: true});

Plotly.newPlot('chart-tracking', [{
    x: [], y: [], type: 'scatter', mode: 'lines', line: { color: '#ef4444', width: 3 }
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Sweep Count', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: 'Steering Angle (°)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', range: [-90, 90] }
}), {displayModeBar: false, responsive: true});


function updatePlotLimits() {
    const xMin = parseFloat(document.getElementById('val-xmin').value);
    const xMax = parseFloat(document.getElementById('val-xmax').value);
    const yMin = parseFloat(document.getElementById('val-ymin').value);
    const yMax = parseFloat(document.getElementById('val-ymax').value);
    
    // Update Cartesian Axes
    Plotly.relayout('chart-rect', {
        'xaxis.range': [xMin, xMax],
        'yaxis.range': [yMin, yMax]
    });
    
    // Auto-calculate appropriate radial range from Y-Mins for Polar
    Plotly.relayout('chart-polar', {
        'polar.radialaxis.range': [yMin, yMax]
    });
}
updatePlotLimits();

/* --- Transport Setup --- */
let isConnected = false;
let sweepCounter = 0;

const transport = createTransport({
    onMessage: (msg) => {
        if (msg.type === 'backend-ready' && msg.state) {
            // Initial state from backend - mark as ready and load state
            if (msg.state.status === 'ok' && msg.state.data) {
                backendProbeState.ready = true;
                backendProbeState.probing = false;
                const data = msg.state.data;
                if (Number.isFinite(data.SignalFreq)) state.SignalFreq = data.SignalFreq;
                if (Number.isFinite(data.Rx_freq)) state.Rx_freq = data.Rx_freq;
                if (Number.isFinite(data.Rx_gain)) state.Rx_gain = data.Rx_gain;
                if (Number.isFinite(data.Tx_gain)) state.Tx_gain = data.Tx_gain;
                if (Number.isFinite(data.Averages)) state.Averages = data.Averages;
                if (Number.isFinite(data.d)) state.d = data.d;
                if (Number.isFinite(data.BW)) state.BW = data.BW;
                updateHardwareConnectionStatus(data.hardware_connected ?? false);
                applyInitialStateToControls();
                setBackendStatus('ready', 'Backend: Ready');
                updateSweepAvailability();
                addRuntimeLog('info', 'WS', 'Backend ready, hardware connected');
            }
        } else if (msg.type === 'response' && msg.data) {
            // Command response - check if it's a state response
            if (msg.data.status === 'ok' && msg.data.data) {
                // Could be get_state response during probe
            }
        } else if (msg.status === 'ok' && msg.data) {
            updateCharts(msg.data);
        } else if (msg.status === 'error') {
            addRuntimeLog('error', 'WS', msg.message || 'Backend reported an error');
        }
    },
    onSweepData: (data) => {
        // Direct sweep data from WebSocket
        if (data) updateCharts(data);
    },
    onConnectionStatus: (status) => {
        // Connection status update
        if (status.connected) {
            document.getElementById('connection-dot')?.classList.replace('disconnected', 'connected');
            document.getElementById('connection-text').innerText = 'Connected';
        } else {
            document.getElementById('connection-dot')?.classList.replace('connected', 'disconnected');
            document.getElementById('connection-text').innerText = 'Disconnected';
        }
    },
    onOpen: () => {
        isConnected = true;
        // Don't set "Connected" here - let loadStateFromServer check hardware status
        document.getElementById('connection-dot')?.classList.replace('connected', 'disconnected');
        document.getElementById('connection-text').innerText = 'Checking...';
        addRuntimeLog('info', 'WS', 'Backend connected, checking hardware...');
        setBackendStatus('starting', 'Backend: Probing...');
        updateSweepAvailability();
        probeBackendReadiness();
    },
    onClose: () => {
        isConnected = false;
        backendProbeState.ready = false;
        backendProbeState.probing = false;
        document.getElementById('connection-dot').classList.replace('connected', 'disconnected');
        document.getElementById('connection-text').innerText = 'Disconnected - Retrying...';
        setBackendStatus('error', 'Backend: Offline');
        updateSweepAvailability();
        addRuntimeLog('warn', 'WS', 'Disconnected, retrying in 2s');
        if (autoSweepInterval) {
            clearInterval(autoSweepInterval);
            autoSweepInterval = null;
            document.getElementById('btn-sweep').innerText = 'Start';
            addRuntimeLog('warn', 'SWEEP', 'Stopped because websocket disconnected');
        }
    },
    onLog: addRuntimeLog,
});
transport.connect();

/* --- Transport Self-Check & Badge --- */
(function initTransportBadge() {
    const mode = (window.__PHASER_TRANSPORT === 'ipc' || window.pywebview?.api?.invoke) ? 'ipc' : 'web';
    const bridgePresent = !!(
        (window.__PHASER_IPC_BRIDGE && typeof window.__PHASER_IPC_BRIDGE.invoke === 'function') ||
        window.pywebview?.api?.invoke
    );
    const isMockBridge = bridgePresent && (window.__PHASER_IPC_MOCK === true || new URLSearchParams(window.location.search).get('mockIpc') === '1');

    // Log a one-liner to the runtime console
    if (mode === 'ipc') {
        const bridgeLabel = isMockBridge ? 'mock-bridge' : (bridgePresent ? 'bridge-ok' : 'NO-BRIDGE');
        addRuntimeLog('info', 'TRANSPORT', `mode=ipc  bridge=${bridgeLabel}`);
    } else {
        addRuntimeLog('info', 'TRANSPORT', `mode=web  (WebSocket + REST)`);
    }

    // Show badge only for non-default transports or mock mode
    const badgeEl = document.getElementById('transport-badge');
    if (!badgeEl) return;
    if (mode === 'ipc') {
        badgeEl.textContent = isMockBridge ? 'IPC · mock' : 'IPC';
        badgeEl.style.display = 'inline-flex';
        badgeEl.title = bridgePresent
            ? (isMockBridge ? 'IPC transport — mock bridge (dev mode)' : 'IPC transport — bridge connected')
            : 'IPC transport — bridge NOT found';
        if (!bridgePresent) {
            badgeEl.style.setProperty('--transport-badge-bg', 'rgba(239,68,68,0.15)');
            badgeEl.style.setProperty('--transport-badge-color', '#ef4444');
            badgeEl.style.setProperty('--transport-badge-border', 'rgba(239,68,68,0.4)');
            addRuntimeLog('warn', 'TRANSPORT', 'IPC mode selected but no bridge found — set window.__PHASER_IPC_BRIDGE');
        }
    }
    // In plain web mode, badge stays hidden (display:none from HTML)
})();

function formatCalibrationStatus(data) {
    if (!data) return 'Idle';
    const lines = [];
    lines.push(`Task: ${data.task || 'none'}`);
    lines.push(`Running: ${data.running ? 'yes' : 'no'}`);
    if (data.outcome) lines.push(`Outcome: ${data.outcome}`);
    if (data.pid) lines.push(`PID: ${data.pid}`);
    if (data.returncode !== null && data.returncode !== undefined) lines.push(`Return code: ${data.returncode}`);
    if (data.log_path) lines.push(`Log file: ${data.log_path}`);
    if (data.evidence && Array.isArray(data.evidence.network) && data.evidence.network.length) {
        lines.push('--- network evidence ---');
        lines.push(data.evidence.network.join('\n'));
    }
    if (data.evidence && Array.isArray(data.evidence.errors) && data.evidence.errors.length) {
        lines.push('--- error evidence ---');
        lines.push(data.evidence.errors.join('\n'));
    }
    if (data.evidence && Number.isFinite(data.evidence.hb100_peak_ghz)) {
        lines.push(`HB100 peak (GHz): ${data.evidence.hb100_peak_ghz.toFixed(6)}`);
    }
    if (data.evidence && typeof data.evidence.saved === 'boolean') {
        lines.push(`Saved calibration: ${data.evidence.saved ? 'yes' : 'no'}`);
    }
    if (Array.isArray(data.last_lines) && data.last_lines.length > 0) {
        lines.push('--- log ---');
        lines.push(data.last_lines.slice(-8).join('\n'));
    }
    return lines.join('\n');
}

function formatCalibrationSummary(data) {
    if (!data) return 'Cal: Idle';
    if (data.running) {
        const task = data.task ? data.task.replace('_', ' ') : 'calibration';
        return `Cal: Running (${task})`;
    }
    if (data.returncode === 0) return 'Cal: Done';
    if (data.returncode !== null && data.returncode !== undefined) return `Cal: Error (${data.returncode})`;
    return 'Cal: Idle';
}

function setCalibrationButtonsBusy(running, taskName = null) {
    const btnCal = document.getElementById('btn-calibrate-phaser');
    const btnHb100 = document.getElementById('btn-find-hb100');
    console.log('[CAL] setCalibrationButtonsBusy:', { running, taskName, btnCal: !!btnCal, btnHb100: !!btnHb100 });
    if (!btnCal || !btnHb100) {
        console.warn('[CAL] Buttons not found!');
        return;
    }

    const isRunning = Boolean(running);
    const task = String(taskName || '');

    btnCal.disabled = isRunning;
    btnHb100.disabled = isRunning;

    if (isRunning && task === 'phaser_cal') {
        btnCal.innerText = 'Calibrating...';
        btnHb100.innerText = 'Find HB100';
    } else if (isRunning && task === 'find_hb100') {
        btnCal.innerText = 'Calibrate Phaser';
        btnHb100.innerText = 'Scanning...';
    } else {
        btnCal.innerText = 'Calibrate Phaser';
        btnHb100.innerText = 'Find HB100';
    }
    console.log('[CAL] Button states after update:', { calText: btnCal.innerText, hb100Text: btnHb100.innerText });
}

function updateCalibrationPill(data, fallbackText) {
    const pillEl = document.getElementById('cal-status-pill');
    if (!pillEl) return;

    const text = fallbackText || formatCalibrationSummary(data);
    pillEl.innerText = text;
    pillEl.classList.remove('idle', 'running', 'ok', 'error');

    if (!data) {
        pillEl.classList.add('idle');
        setCalibrationButtonsBusy(false);
        return;
    }
    if (data.running) {
        pillEl.classList.add('running');
        setCalibrationButtonsBusy(true, data.task);
    } else if (data.returncode === 0) {
        pillEl.classList.add('ok');
        setCalibrationButtonsBusy(false);
    } else if (data.returncode !== null && data.returncode !== undefined) {
        pillEl.classList.add('error');
        setCalibrationButtonsBusy(false);
    } else {
        pillEl.classList.add('idle');
        setCalibrationButtonsBusy(false);
    }
}

function trackCalibrationLogUpdates(data) {
    if (!data) return;

    if (
        data.running !== calibrationLogState.lastRunning ||
        data.task !== calibrationLogState.lastTask ||
        data.returncode !== calibrationLogState.lastReturnCode
    ) {
        addRuntimeLog('info', 'CAL', formatCalibrationSummary(data));
        calibrationLogState.lastRunning = data.running;
        calibrationLogState.lastTask = data.task;
        calibrationLogState.lastReturnCode = data.returncode;
    }

    const lines = Array.isArray(data.last_lines) ? data.last_lines : [];
    if (lines.length < calibrationLogState.lastLineCount) {
        calibrationLogState.lastLineCount = 0;
    }
    const newLines = lines.slice(calibrationLogState.lastLineCount);
    for (const line of newLines) {
        addRuntimeLog('info', 'CAL', line);
    }
    calibrationLogState.lastLineCount = lines.length;
}

async function refreshCalibrationStatus() {
    try {
        const msg = await transport.getCalibrationStatus();
        if (msg.status === 'ok') {
            updateCalibrationPill(msg.data);
            updateCalibrationModal(msg.data);
            trackCalibrationLogUpdates(msg.data);
            if (msg.data && !msg.data.running && msg.data.returncode === 0) {
                const reloadableTasks = new Set(['find_hb100', 'phaser_cal']);
                if (reloadableTasks.has(msg.data.task)) {
                    const reloadKey = `${msg.data.task}:${msg.data.started_at}:${msg.data.returncode}`;
                    if (reloadKey !== calibrationLogState.lastReloadKey) {
                        calibrationLogState.lastReloadKey = reloadKey;
                        await loadStateFromServer();
                        addRuntimeLog('info', 'CAL', `Reloaded UI state after ${msg.data.task}`);
                    }
                }
            }
        }
    } catch (err) {
        updateCalibrationPill(null, 'Cal: Status Error');
        addRuntimeLog('error', 'CAL', `Status polling failed: ${err}`);
    }
}

function startCalibrationPolling() {
    refreshCalibrationStatus();
    if (calibrationState.pollingTimer) return;
    calibrationState.pollingTimer = setInterval(refreshCalibrationStatus, 2000);
}

function showCalibrationModal(taskName) {
    const modal = document.getElementById('calibration-modal');
    const titleEl = document.getElementById('cal-modal-title');
    const statusEl = document.getElementById('cal-modal-status');
    const outputEl = document.getElementById('cal-modal-output');
    const actionBtn = document.getElementById('btn-cal-action');
    const spinnerEl = document.getElementById('cal-spinner');

    const taskLabels = {
        'find_hb100': 'Find HB100',
        'phaser_cal': 'Calibrate Phaser',
    };
    if (titleEl) titleEl.innerText = taskLabels[taskName] || 'Calibration';
    if (statusEl) statusEl.innerText = 'Starting...';
    if (outputEl) outputEl.innerText = '';
    if (actionBtn) {
        actionBtn.innerText = 'Cancel';
        actionBtn.classList.remove('btn-outline');
        actionBtn.classList.add('btn-danger');
        actionBtn.dataset.mode = 'cancel';
    }
    if (spinnerEl) spinnerEl.classList.add('spinning');
    if (modal) modal.hidden = false;
}

function updateCalibrationModal(data) {
    const modal = document.getElementById('calibration-modal');
    if (!modal || modal.hidden) return;

    const statusEl = document.getElementById('cal-modal-status');
    const outputEl = document.getElementById('cal-modal-output');
    const actionBtn = document.getElementById('btn-cal-action');
    const spinnerEl = document.getElementById('cal-spinner');

    if (!data) return;

    if (data.running) {
        if (statusEl) statusEl.innerText = 'Running...';
        if (spinnerEl) spinnerEl.classList.add('spinning');
        if (actionBtn) {
            actionBtn.innerText = 'Cancel';
            actionBtn.classList.remove('btn-outline');
            actionBtn.classList.add('btn-danger');
            actionBtn.dataset.mode = 'cancel';
        }
    } else {
        if (spinnerEl) spinnerEl.classList.remove('spinning');
        if (actionBtn) {
            actionBtn.innerText = 'Close';
            actionBtn.classList.remove('btn-danger');
            actionBtn.classList.add('btn-outline');
            actionBtn.dataset.mode = 'close';
        }
        // Reset sidebar buttons when calibration completes
        setCalibrationButtonsBusy(false);
        if (data.returncode === 0) {
            if (statusEl) statusEl.innerText = 'Completed successfully';
        } else if (data.returncode !== null && data.returncode !== undefined) {
            if (statusEl) statusEl.innerText = `Failed (code ${data.returncode})`;
        } else {
            if (statusEl) statusEl.innerText = 'Idle';
        }
    }

    if (outputEl && Array.isArray(data.last_lines)) {
        outputEl.innerText = data.last_lines.slice(-12).join('\n');
        outputEl.scrollTop = outputEl.scrollHeight;
    }
}

function hideCalibrationModal() {
    const modal = document.getElementById('calibration-modal');
    if (modal) modal.hidden = true;
    // Directly reset buttons when closing modal
    setCalibrationButtonsBusy(false);
}

async function cancelCalibrationTask() {
    const cancelBtn = document.getElementById('btn-cal-cancel');
    if (cancelBtn) cancelBtn.disabled = true;
    addRuntimeLog('info', 'CAL', 'Cancellation requested');
    try {
        const msg = await transport.cancelCalibration();
        if (msg.status === 'ok') {
            addRuntimeLog('info', 'CAL', 'Calibration cancelled');
            setCalibrationButtonsBusy(false);
            hideCalibrationModal();
        } else {
            addRuntimeLog('warn', 'CAL', msg.message || 'Cancel failed');
        }
    } catch (err) {
        addRuntimeLog('error', 'CAL', `Cancel request failed: ${err}`);
    }
}

async function runCalibrationTask(taskName) {
    addRuntimeLog('info', 'CAL', `Requested task: ${taskName}`);
    showCalibrationModal(taskName);
    setCalibrationButtonsBusy(true, taskName);
    try {
        const msg = await transport.runCalibration(taskName);
        if (msg.status !== 'ok') {
            updateCalibrationModal({ running: false, returncode: 1, last_lines: [msg.message || 'Calibration start failed'] });
            setCalibrationButtonsBusy(false);
            addRuntimeLog('error', 'CAL', msg.message || 'Calibration start failed');
            return;
        }
        addRuntimeLog('info', 'CAL', `Task started: ${taskName}`);
        startCalibrationPolling();
    } catch (err) {
        updateCalibrationModal({ running: false, returncode: 1, last_lines: [`Error: ${err}`] });
        setCalibrationButtonsBusy(false);
        addRuntimeLog('error', 'CAL', `Task request failed: ${err}`);
    }
}

document.getElementById('btn-calibrate-phaser')?.addEventListener('click', () => runCalibrationTask('phaser_cal'));
document.getElementById('btn-find-hb100')?.addEventListener('click', () => runCalibrationTask('find_hb100'));

document.getElementById('btn-reboot-phaser')?.addEventListener('click', async () => {
    const btn = document.getElementById('btn-reboot-phaser');
    if (!confirm('Reboot the Phaser hardware? This will take about 30 seconds.')) return;
    btn.disabled = true;
    btn.textContent = 'Rebooting...';
    addRuntimeLog('info', 'SYS', 'Sending reboot command to Phaser...');
    try {
        const resp = await transport.invoke('reboot_phaser', {});
        if (resp?.status === 'ok') {
            addRuntimeLog('info', 'SYS', resp.message || 'Phaser is rebooting');
            // Wait for reboot and then try to reconnect
            setTimeout(() => {
                btn.textContent = 'Reboot';
                btn.disabled = false;
                addRuntimeLog('info', 'SYS', 'Phaser should be back online. Reconnecting...');
                loadStateFromServer();
            }, 35000);
        } else {
            addRuntimeLog('error', 'SYS', resp?.message || 'Reboot failed');
            btn.textContent = 'Reboot';
            btn.disabled = false;
        }
    } catch (err) {
        addRuntimeLog('error', 'SYS', `Reboot failed: ${err}`);
        btn.textContent = 'Reboot';
        btn.disabled = false;
    }
});
document.getElementById('btn-cal-action')?.addEventListener('click', (e) => {
    const mode = e.target.dataset.mode;
    if (mode === 'cancel') {
        cancelCalibrationTask();
    } else {
        hideCalibrationModal();
    }
});
startCalibrationPolling();

// Simulation mode toggle (Electron only)
let simModeActive = false;
const simBtn = document.getElementById('btn-sim-mode');
if (simBtn && window.electronAPI?.startSim) {
    // Listen for sim status changes
    window.electronAPI.onSimStatus?.((status) => {
        simModeActive = status.running;
        updateSimButton();
    });

    // Check initial status
    window.electronAPI.getSimStatus?.().then((status) => {
        simModeActive = status?.running || false;
        updateSimButton();
    });

    simBtn.addEventListener('click', async () => {
        if (simModeActive) {
            // Stop sim
            simBtn.disabled = true;
            simBtn.textContent = 'Stopping...';
            addRuntimeLog('info', 'SIM', 'Stopping simulator...');
            await window.electronAPI.stopSim();
        } else {
            // Start sim with default data file
            simBtn.disabled = true;
            simBtn.textContent = 'Starting...';
            addRuntimeLog('info', 'SIM', 'Starting simulator...');
            const resp = await window.electronAPI.startSim(null);
            if (resp.status !== 'ok') {
                addRuntimeLog('error', 'SIM', resp.message || 'Failed to start simulator');
                alert('Failed to start simulator:\n' + (resp.message || 'Unknown error'));
            }
        }
        updateSimButton();
    });

    function updateSimButton() {
        simBtn.disabled = false;
        if (simModeActive) {
            simBtn.textContent = 'Stop Sim';
            simBtn.style.background = '#f59e0b';
            simBtn.style.borderColor = '#f59e0b';
            simBtn.style.color = '#000';
        } else {
            simBtn.textContent = 'Sim';
            simBtn.style.background = '';
            simBtn.style.borderColor = '';
            simBtn.style.color = '';
        }
    }
} else if (simBtn) {
    // Hide button if not in Electron
    simBtn.style.display = 'none';
}


const settingsPanel = document.getElementById('settings-panel');
const dashboard = document.querySelector('.dashboard');
const toggleSettingsBtn = document.getElementById('btn-toggle-settings');
const toggleSettingsIconBtn = document.getElementById('btn-toggle-settings-icon');
const sidebarIcons = document.getElementById('sidebar-icons');
const sidebarContent = document.getElementById('sidebar-content');
const sidebarSectionButtons = document.querySelectorAll('.sidebar-icon-btn[data-section]');
const accordionItems = document.querySelectorAll('.accordion-item');
let currentExpandedSection = null;

function resizePlotsAfterSidebarTransition() {
  setTimeout(() => {
      Plotly.Plots.resize(document.getElementById('chart-rect'));
      Plotly.Plots.resize(document.getElementById('chart-polar'));
      Plotly.Plots.resize(document.getElementById('chart-fft'));
      Plotly.Plots.resize(document.getElementById('chart-tracking'));
  }, 300);
}

function updateSidebarMode(isCollapsed) {
  sidebarIcons?.style.removeProperty('display');
  sidebarContent?.style.removeProperty('display');
  dashboard.classList.toggle('settings-collapsed', isCollapsed);
}

function setSidebarCollapsed(isCollapsed) {
  settingsPanel?.classList.toggle('collapsed', isCollapsed);
  updateSidebarMode(isCollapsed);
  toggleSettingsBtn.innerText = isCollapsed ? '☰' : '−';
  const label = isCollapsed ? 'Expand settings' : 'Collapse settings';
  toggleSettingsBtn.title = label;
  toggleSettingsBtn.setAttribute('aria-label', label);
  toggleSettingsIconBtn.title = label;
  toggleSettingsIconBtn.setAttribute('aria-label', label);
  resizePlotsAfterSidebarTransition();
}

function activateSidebarSection(sectionIdx) {
  const safeIdx = Number.isInteger(sectionIdx) ? sectionIdx : parseInt(sectionIdx, 10);
  if (!Number.isInteger(safeIdx) || safeIdx < 0 || safeIdx >= accordionItems.length) return;

  sidebarSectionButtons.forEach((btn) => {
    btn.classList.toggle('active', parseInt(btn.dataset.section, 10) === safeIdx);
  });
  accordionItems.forEach((item, idx) => {
    item.classList.toggle('active', idx === safeIdx);
  });
  currentExpandedSection = safeIdx;
}

toggleSettingsBtn?.addEventListener('click', () => {
     const isCollapsed = !dashboard?.classList.contains('settings-collapsed');
     setSidebarCollapsed(isCollapsed);
});

// Icon sidebar functionality
sidebarSectionButtons.forEach(btn => {
  btn.addEventListener('dblclick', (e) => {
    e.preventDefault();
    e.stopPropagation();
  });

  btn.addEventListener('click', () => {
    const sectionIdx = parseInt(btn.dataset.section, 10);
    const isCollapsed = dashboard?.classList.contains('settings-collapsed');

    // PyCharm-like behavior: clicking the active icon toggles collapse.
    if (!isCollapsed && sectionIdx === currentExpandedSection) {
      setSidebarCollapsed(true);
      return;
    }

    if (isCollapsed) {
      setSidebarCollapsed(false);
    }
    activateSidebarSection(sectionIdx);
  });
});

toggleSettingsIconBtn?.addEventListener('click', () => {
  // Rail toggle button mirrors IDE sidebars: click to expand/collapse quickly.
  const isCollapsed = dashboard?.classList.contains('settings-collapsed');
  if (isCollapsed) {
    setSidebarCollapsed(false);
    activateSidebarSection(currentExpandedSection ?? 0);
  } else {
    setSidebarCollapsed(true);
  }
});

const initialActiveSection = Array.from(accordionItems).findIndex((item) => item.classList.contains('active'));
activateSidebarSection(initialActiveSection >= 0 ? initialActiveSection : 0);


function requestSweep() {
    if (isConnected) transport.send({ cmd: 'sweep', state: state });
}

function applyPhaseList(phases) {
    state.phaseList = phases.slice(0, 8);
    document.querySelectorAll('.phase-sl').forEach((el, idx) => {
        el.value = state.phaseList[idx] ?? 0;
    });
    document.querySelectorAll('.phase-in').forEach((el, idx) => {
        el.value = state.phaseList[idx] ?? 0;
    });
}

function applyLabPreset(preset) {
    if (!preset) return;
    state.mode = preset.mode ?? state.mode;
    state.BW = Number.isFinite(preset.BW) ? preset.BW : state.BW;
    state.steer_res = Number.isFinite(preset.steer_res) ? preset.steer_res : state.steer_res;
    state.bits = Number.isFinite(preset.bits) ? preset.bits : state.bits;
    state.ignore_res = typeof preset.ignore_res === 'boolean' ? preset.ignore_res : state.ignore_res;
    state.B0_Gain = Number.isFinite(preset.B0_Gain) ? preset.B0_Gain : state.B0_Gain;
    state.B1_Gain = Number.isFinite(preset.B1_Gain) ? preset.B1_Gain : state.B1_Gain;
    state.Beam0_Phase = Number.isFinite(preset.Beam0_Phase) ? preset.Beam0_Phase : state.Beam0_Phase;
    state.Beam1_Phase = Number.isFinite(preset.Beam1_Phase) ? preset.Beam1_Phase : state.Beam1_Phase;

    if (Array.isArray(preset.gainList) && preset.gainList.length >= 8) {
        applyTaper(preset.gainList.slice(0, 8));
    }
    if (Array.isArray(preset.phaseList) && preset.phaseList.length >= 8) {
        applyPhaseList(preset.phaseList);
    }

    // Apply monopulse display options
    const showDeltaEl = document.getElementById('opt-show-delta');
    const showErrorEl = document.getElementById('opt-show-error');
    if (showDeltaEl && typeof preset.showDelta === 'boolean') {
        showDeltaEl.checked = preset.showDelta;
        showDeltaEl.dispatchEvent(new Event('change'));
    }
    if (showErrorEl && typeof preset.showError === 'boolean') {
        showErrorEl.checked = preset.showError;
        showErrorEl.dispatchEvent(new Event('change'));
    }

    const tabName = preset.ui_tab || 'tab-rect';
    document.querySelector(`[data-target="${tabName}"]`)?.click();
    applyInitialStateToControls();

    // Send preset state to backend
    syncStateToBackend();
}

function localLabPreset(labIdx) {
    const base = {
        mode: 'Beam Sweep',
        gainList: [100,100,100,100,100,100,100,100],
        phaseList: [0,0,0,0,0,0,0,0],
        steer_res: 2.8125,
        bits: 7,
        ignore_res: true,
        BW: 10,
        ui_tab: 'tab-rect',
    };
    switch (labIdx) {
        case 1: return { ...base, mode: 'Static Phase', ui_tab: 'tab-fft' };
        case 5: return { ...base, mode: 'Beam Sweep', BW: 500, ui_tab: 'tab-rect' };  // Beam Squint: 500 MHz offset shows ~3° shift at 45°
        case 6: return { ...base, mode: 'Beam Sweep', ui_tab: 'tab-rect' };  // Quantization: change bits slider to see effect on beam pattern
        case 8: return { ...base, mode: 'Tracking', gainList: [6,27,66,100,100,66,27,6], ui_tab: 'tab-rect', showDelta: true, showError: true };
        default: return base;
    }
}

async function fetchLabPreset(labIdx) {
    try {
        const msg = await transport.getLabPreset(labIdx);
        if (msg.status === 'ok' && msg.data) {
            return msg.data;
        }
    } catch (err) {
        console.warn('Lab preset endpoint unavailable, using local fallback:', err);
    }
    return localLabPreset(labIdx);
}

function updateCharts(data) {
    let peakIndex = 0;
    let peakValue = -1000;
    let xData = data.ArrayAngle || state.PhaseValues;
    let yData = data.ArrayGain;

    // Debug: check if monopulse data is present
    if (data.ArrayDelta) {
        console.log('[Monopulse] Delta data present, length:', data.ArrayDelta.length);
    }
    if (data.ErrorFunc) {
        console.log('[Monopulse] Error data present, length:', data.ErrorFunc.length, 'range:', Math.min(...data.ErrorFunc).toFixed(2), 'to', Math.max(...data.ErrorFunc).toFixed(2));
    }

    // Process Arrays
    if(yData && yData.length > 0) {
        peakValue = Math.max(...yData);
        peakIndex = yData.indexOf(peakValue);
        
        let axisUpdate = {};
        let shapes = [];
        
        // Render Peak Markers
        if(document.getElementById('opt-peak-angle').checked) {
            shapes.push({ type: 'line', x0: xData[peakIndex], y0: 0, x1: xData[peakIndex], y1: 1, yref: 'paper', line: { color: '#ef4444', dash: 'dash'} });
        }
        if(document.getElementById('opt-peak-gain').checked) {
            shapes.push({ type: 'line', x0: 0, y0: peakValue, x1: 1, y1: peakValue, xref: 'paper', line: { color: '#10b981', dash: 'dash'} });
        }
        axisUpdate.shapes = shapes;
        
        // Update rectangular plot (Sum beam)
        const rectEl = document.getElementById('chart-rect');
        if (rectEl && rectEl.data && rectEl.data[0]) {
            rectEl.data[0].x = xData;
            rectEl.data[0].y = yData;

            // Update Delta beam (trace 1) if data available
            if (rectEl.data[1] && data.ArrayDelta) {
                rectEl.data[1].x = xData;
                rectEl.data[1].y = data.ArrayDelta;
                const showDelta = document.getElementById('opt-show-delta')?.checked ?? false;
                rectEl.data[1].visible = showDelta;
            }

            // Update Error function (trace 2) if data available
            if (rectEl.data[2] && data.ErrorFunc) {
                rectEl.data[2].x = xData;
                rectEl.data[2].y = data.ErrorFunc;
                const showError = document.getElementById('opt-show-error')?.checked ?? false;
                rectEl.data[2].visible = showError;
            }

            Plotly.redraw('chart-rect');
        }

        // Update polar plot
        const polarEl = document.getElementById('chart-polar');
        if (polarEl && polarEl.data && polarEl.data[0]) {
            polarEl.data[0].r = yData;
            polarEl.data[0].theta = xData.map(toPolarTheta);
            Plotly.redraw('chart-polar');
        }

        // Time tracking - show steering angle vs sweep count
        sweepCounter++;
        const peakAngle = xData[peakIndex];
        timeHistory.push(sweepCounter);
        angleHistory.push(peakAngle);
        if(timeHistory.length > 100) { timeHistory.shift(); angleHistory.shift(); }
        Plotly.update('chart-tracking', {x: [timeHistory], y: [angleHistory]}, {});
        
        // Update Stats displays
        document.getElementById('stat-peak').innerText = peakValue.toFixed(2) + " dB";
        document.getElementById('stat-angle').innerText = xData[peakIndex].toFixed(1) + " °";
    }
    
    // FFT data - apply same transforms as original Tkinter GUI (phaser_gui.py:2423)
    if(data.xf && data.max_gain) {
        // Debug: log FFT data range
        const maxGainVal = Math.max(...data.max_gain);
        const minGainVal = Math.min(...data.max_gain);
        const xfMin = Math.min(...data.xf);
        const xfMax = Math.max(...data.xf);
        console.log(`FFT: gain range [${minGainVal.toFixed(1)}, ${maxGainVal.toFixed(1)}] dB, freq range [${(xfMin/1e6).toFixed(2)}, ${(xfMax/1e6).toFixed(2)}] MHz, len=${data.xf.length}`);

        // Negate and convert Hz to MHz to match original GUI behavior
        const xfMHz = data.xf.map(f => -f / 1e6);
        Plotly.update('chart-fft', {x: [xfMHz], y: [data.max_gain]}, {});
    }
}

// Global UI interaction
const sweepBtn = document.getElementById('btn-sweep');
let isSweeping = false;

sweepBtn.addEventListener('click', async () => {
    if (sweepBtn.disabled) {
        addRuntimeLog('warn', 'SWEEP', 'Start blocked until backend is ready');
        return;
    }

    if (!isSweeping) {
        // Start sweeping - send current state first, then start
        transport.send({ cmd: 'set_state', data: { state } });
        transport.send({ cmd: 'start_sweep' });
        isSweeping = true;
        sweepBtn.innerText = "Stop";
        sweepBtn.style.background = "#ef4444";
        sweepBtn.style.boxShadow = "0 4px 15px rgba(239, 68, 68, 0.4)";
        addRuntimeLog('info', 'SWEEP', 'Started sweep stream');
    } else {
        // Stop sweeping
        transport.send({ cmd: 'stop_sweep' });
        isSweeping = false;
        sweepBtn.innerText = "Start";
        sweepBtn.style.background = "";
        sweepBtn.style.boxShadow = "";
        addRuntimeLog('info', 'SWEEP', 'Stopped sweep stream');
    }
});


/* --- Lab Pre-sets --- */
document.querySelectorAll('.lab-btn').forEach(btn => {
    btn.addEventListener('click', async (e) => {
        const labIdx = parseInt(e.target.dataset.lab);
        addRuntimeLog('info', 'LAB', `Loading lab preset ${labIdx}`);
        const preset = await fetchLabPreset(labIdx);
        applyLabPreset(preset);
        addRuntimeLog('info', 'LAB', `Applied lab preset ${labIdx}`);
    });
});


function setBackendStatus(state, text) {
    const pill = document.getElementById('backend-status-pill');
    if (!pill) return;
    pill.classList.remove('state-starting', 'state-ready', 'state-error');
    if (state === 'ready') pill.classList.add('state-ready');
    else if (state === 'error') pill.classList.add('state-error');
    else pill.classList.add('state-starting');
    pill.innerText = text;
}

function updateSweepAvailability() {
    const sweepBtn = document.getElementById('btn-sweep');
    if (!sweepBtn) return;

    const enabled = isConnected && backendProbeState.ready;
    if (!enabled && autoSweepInterval) {
        clearInterval(autoSweepInterval);
        autoSweepInterval = null;
        sweepBtn.innerText = 'Start';
        sweepBtn.style.background = '';
        sweepBtn.style.boxShadow = '';
        addRuntimeLog('warn', 'SWEEP', 'Stopped due to backend readiness loss');
    }

    sweepBtn.disabled = !enabled;
    sweepBtn.style.opacity = enabled ? '1' : '0.65';
    sweepBtn.style.cursor = enabled ? 'pointer' : 'not-allowed';
    if (!enabled) {
        sweepBtn.title = !isConnected ? 'Waiting for transport connection' : 'Waiting for backend readiness';
    } else {
        sweepBtn.title = 'Start/Stop sweep';
    }
}

async function probeBackendReadiness() {
    if (!isConnected || backendProbeState.probing || backendProbeState.ready) return;
    backendProbeState.probing = true;
    setBackendStatus('starting', 'Backend: Starting...');

    try {
        const msg = await Promise.race([
            transport.getState(),
            new Promise((_, reject) => setTimeout(() => reject(new Error('Backend probe timeout')), BACKEND_PROBE_TIMEOUT_MS)),
        ]);

        if (msg.status === 'ok' && msg.data) {
            backendProbeState.ready = true;
            backendProbeState.retries = 0;
            setBackendStatus('ready', 'Backend: Ready');
            await loadStateFromServer();
            addRuntimeLog('info', 'BACKEND', 'Readiness probe succeeded');
        } else {
            throw new Error(msg.message || 'Backend probe failed');
        }
    } catch (err) {
        backendProbeState.ready = false;
        backendProbeState.retries += 1;
        setBackendStatus('error', 'Backend: Init Error');
        addRuntimeLog('error', 'BACKEND', `Readiness probe failed: ${err}`);

        if (isConnected && backendProbeState.retries < MAX_BACKEND_PROBE_RETRIES) {
            setTimeout(() => {
                backendProbeState.probing = false;
                probeBackendReadiness();
            }, BACKEND_PROBE_RETRY_MS);
            updateSweepAvailability();
            return;
        }
    }

    backendProbeState.probing = false;
    updateSweepAvailability();
}

// Force resize all plots after initial render to prevent "jump" on first tab click
// This ensures hidden plots have correct dimensions when first shown
setTimeout(() => {
    const chartIds = ['chart-rect', 'chart-polar', 'chart-fft', 'chart-tracking'];
    chartIds.forEach(id => {
        const el = document.getElementById(id);
        if (el && window.Plotly) {
            Plotly.Plots.resize(el);
        }
    });
}, 100);
