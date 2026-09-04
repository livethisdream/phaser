import { createTransport, resolveTransportMode } from './transport.js';
import { autoBackendWsUrl, getBackendUrlOverride, setBackendUrlOverride } from './transport-web.js';

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
    d: 0.014,
    bfMode: 'manual',       // 'manual' | 'mvdr'
    mvdrK: 128,             // MVDR snapshot count
    mvdrDiagLoad: 0.001,    // MVDR diagonal loading factor

    // Simulator interferer (instructor-only, sim mode only; hidden by default)
    sim_interferer_enable: false,
    sim_interferer_angle_deg: 30,
    sim_interferer_power_db: 0,
};

// Instructor mode: adds ?instructor=1 to the URL to reveal the
// Simulator Interferer accordion. Students never see it.
const instructorMode = new URLSearchParams(window.location.search).get('instructor') === '1';

// CTF mode: ?ctf=1 reveals the GRCon26 sector-sequence panel. Unlike
// instructor mode this parameter is UI convenience only, NOT a secret —
// the sequence check and the flag live in the backend, because this bundle
// is served to every browser that connects and a CTF player's whole job is
// to go looking. Everything below only displays what the backend reports.
const ctfMode = new URLSearchParams(window.location.search).get('ctf') === '1';

// Sector geometry as last reported by the backend, and whether to draw it on
// the beam pattern. Both live here rather than in the CTF block because
// updateCharts() reads them on every sweep.
state.ctfSectors = [];
state.ctfSectorLines = true;

// When switching Manual -> MVDR, we zero B0/B1 so residual manual weights
// don't fight the adaptive algorithm. These snapshots restore them on the
// way back.
const bfManualSnapshot = { B0_Gain: 1.0, B1_Gain: 1.0, Beam0_Phase: 0, Beam1_Phase: 0 };

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

function isDesktopHost() {
    return window.__PHASER_DESKTOP === true || !!window.pywebview?.api;
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

        updateMemoryButtons();

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

/* --- Memory Snapshots (Rectangular + Polar) -------------------------------
 * Capture up to 3 snapshots of the live Sum trace as faded overlays for
 * before/after comparisons when changing beam parameters. FIFO eviction —
 * a 4th press drops the oldest. "Clear" removes all at once.
 */
const MEMORY_MAX = 3;
const MEMORY_COLORS = ['#f59e0b', '#06b6d4', '#ec4899'];  // amber / cyan / magenta
const memorySlots = [];  // each: { label, x: [...], y: [...], color }
let lastLiveTrace = { x: null, y: null };  // most recently drawn Sum data

function memoryActiveOnTab() {
    const active = document.querySelector('.tab-btn.active');
    const target = active?.getAttribute('data-target');
    return target === 'tab-rect' || target === 'tab-polar';
}

function updateMemoryButtons() {
    const memBtn = document.getElementById('btn-memory');
    const onMemTab = memoryActiveOnTab();
    // Allow click-to-freeze whenever there is live data on the right tab.
    // Long-press still clears whenever snapshots exist, even with no live data.
    if (memBtn) {
        const canFreeze = onMemTab && lastLiveTrace.x && lastLiveTrace.x.length;
        const canClear = onMemTab && memorySlots.length > 0;
        memBtn.disabled = !(canFreeze || canClear);
    }
}

function indexOfMax(arr) {
    let best = 0;
    for (let i = 1; i < arr.length; i++) if (arr[i] > arr[best]) best = i;
    return best;
}

function rebuildMemoryTraces() {
    // Drop any existing memory traces (everything past the live ones), then
    // re-add from `memorySlots`. Labels are rendered separately as absolutely
    // positioned HTML bubbles (see positionFreezeBubbles).
    const rectEl = document.getElementById('chart-rect');
    const polarEl = document.getElementById('chart-polar');

    if (rectEl && rectEl.data) {
        const liveCount = 3;  // Sum, Delta, Error
        const extras = rectEl.data.length - liveCount;
        if (extras > 0) {
            const indices = [];
            for (let i = 0; i < extras; i++) indices.push(liveCount + i);
            try { Plotly.deleteTraces('chart-rect', indices); } catch (e) { /* ignore */ }
        }
        if (memorySlots.length) {
            const newRectTraces = memorySlots.map((slot) => ({
                x: slot.x,
                y: slot.y,
                type: 'scatter',
                mode: 'lines',
                name: slot.label,
                line: { color: slot.color, width: 1.5, dash: 'dot' },
                opacity: 0.7,
                hoverinfo: 'skip',
                showlegend: false,
            }));
            try { Plotly.addTraces('chart-rect', newRectTraces); } catch (e) { /* ignore */ }
        }
    }

    if (polarEl && polarEl.data) {
        const liveCount = 3;  // Sum + peak-angle marker + peak-gain marker
        const extras = polarEl.data.length - liveCount;
        if (extras > 0) {
            const indices = [];
            for (let i = 0; i < extras; i++) indices.push(liveCount + i);
            try { Plotly.deleteTraces('chart-polar', indices); } catch (e) { /* ignore */ }
        }
        if (memorySlots.length) {
            const newPolarTraces = memorySlots.map((slot) => ({
                r: slot.y,
                theta: slot.x.map(toPolarTheta),
                type: 'scatterpolar',
                mode: 'lines',
                name: slot.label,
                line: { color: slot.color, width: 1.5, dash: 'dot' },
                opacity: 0.7,
                hoverinfo: 'skip',
                showlegend: false,
            }));
            try { Plotly.addTraces('chart-polar', newPolarTraces); } catch (e) { /* ignore */ }
        }
    }

    positionFreezeBubbles();
}

/* HTML-overlay freeze labels — small rounded pills positioned absolutely
   within each chart wrapper at the peak of each frozen trace. Updated:
     - when memorySlots changes (capture/clear/eviction)
     - on Plotly relayout (axis range changes, theme toggles)
     - on window resize and sidebar collapse (chart resizes)
*/
function ensureFreezeBubbles(wrapperId) {
    const wrapper = document.getElementById(wrapperId);
    if (!wrapper) return null;
    let layer = wrapper.querySelector('.freeze-bubble-layer');
    if (!layer) {
        layer = document.createElement('div');
        layer.className = 'freeze-bubble-layer';
        wrapper.appendChild(layer);
    }
    return layer;
}

function renderFreezeStack(wrapperId) {
    // Render the freeze pills as a fixed top-right legend stack on the chart
    // wrapper, just below Plotly's existing legend. Order is ascending by
    // capture index (oldest "1" on top, newest at the bottom).
    const layer = ensureFreezeBubbles(wrapperId);
    if (!layer) return;
    layer.innerHTML = '';
    memorySlots.forEach((slot) => {
        const bubble = document.createElement('div');
        bubble.className = 'freeze-bubble';
        bubble.style.background = slot.color;
        bubble.textContent = slot.label;
        layer.appendChild(bubble);
    });
}

function positionFreezeBubbles() {
    renderFreezeStack('tab-rect');
    renderFreezeStack('tab-polar');
}

// Re-position bubbles whenever Plotly relayouts (zoom/pan/theme/resize).
['chart-rect', 'chart-polar'].forEach((id) => {
    const el = document.getElementById(id);
    if (el && el.on) {
        el.on('plotly_relayout', positionFreezeBubbles);
        el.on('plotly_afterplot', positionFreezeBubbles);
    }
});
window.addEventListener('resize', positionFreezeBubbles);

function captureFreezeSnapshot() {
    if (!lastLiveTrace.x || !lastLiveTrace.x.length) return false;
    const x = lastLiveTrace.x.slice();
    const y = lastLiveTrace.y.slice();
    if (memorySlots.length >= MEMORY_MAX) memorySlots.shift();  // FIFO evict
    memorySlots.push({ x, y, color: '_pending', label: '_pending' });
    memorySlots.forEach((slot, i) => {
        slot.label = `${i + 1}`;
        slot.color = MEMORY_COLORS[i % MEMORY_COLORS.length];
    });
    rebuildMemoryTraces();
    updateMemoryButtons();
    return true;
}

function clearFreezeSnapshots() {
    if (memorySlots.length === 0) return false;
    memorySlots.length = 0;
    rebuildMemoryTraces();
    updateMemoryButtons();
    return true;
}

/* Freeze button: short click captures a snapshot, long-press (~700 ms) clears
   all snapshots. A CSS overlay fills the button during the hold to telegraph
   the imminent clear. Releasing before the timer fires aborts. */
const FREEZE_HOLD_MS = 700;
(() => {
    const btn = document.getElementById('btn-memory');
    if (!btn) return;
    let holdTimer = null;
    let suppressClick = false;
    let pointerActive = false;

    const startHold = () => {
        if (memorySlots.length === 0) return;  // nothing to clear → don't arm
        if (holdTimer) clearTimeout(holdTimer);
        btn.classList.add('holding');
        holdTimer = setTimeout(() => {
            holdTimer = null;
            btn.classList.remove('holding');
            if (clearFreezeSnapshots()) {
                btn.classList.add('flash');
                setTimeout(() => btn.classList.remove('flash'), 260);
            }
            suppressClick = true;  // swallow the click that follows pointerup
        }, FREEZE_HOLD_MS);
    };

    const cancelHold = () => {
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
        btn.classList.remove('holding');
    };

    btn.addEventListener('pointerdown', (e) => {
        if (btn.disabled) return;
        if (e.button !== undefined && e.button !== 0) return;  // left/primary only
        pointerActive = true;
        suppressClick = false;
        startHold();
    });
    btn.addEventListener('pointerup', () => { pointerActive = false; cancelHold(); });
    btn.addEventListener('pointercancel', () => { pointerActive = false; cancelHold(); });
    btn.addEventListener('pointerleave', () => { if (pointerActive) cancelHold(); pointerActive = false; });

    btn.addEventListener('click', (e) => {
        if (suppressClick) { suppressClick = false; return; }
        captureFreezeSnapshot();
    });

    // Keyboard accessibility: Enter/Space = capture; long-press is mouse/touch only.
    btn.addEventListener('keydown', (e) => {
        if ((e.key === 'Enter' || e.key === ' ') && !btn.disabled) {
            e.preventDefault();
            captureFreezeSnapshot();
        }
    });
})();

// Initial state: nothing captured yet, on-tab gating only.
updateMemoryButtons();


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

/* --- Beam Steering: compute per-element phase ramp for a chosen angle --- */
const steerAngleSlider = document.getElementById('steer-angle');
const steerAngleInput = document.getElementById('val-steer-angle');
if (steerAngleSlider && steerAngleInput) {
    steerAngleSlider.addEventListener('input', (e) => {
        steerAngleInput.value = e.target.value;
    });
    steerAngleInput.addEventListener('input', (e) => {
        steerAngleSlider.value = e.target.value;
    });
}

const STEER_TAPERS = {
    uniform:   [100, 100, 100, 100, 100, 100, 100, 100],
    chebyshev: [4, 23, 62, 100, 100, 62, 23, 4],
    hann:      [12, 43, 77, 100, 100, 77, 43, 12],
    blackman:  [6, 27, 66, 100, 100, 66, 27, 6],
};

document.getElementById('btn-apply-steer')?.addEventListener('click', () => {
    const theta_deg = parseFloat(steerAngleInput.value);
    if (!Number.isFinite(theta_deg)) return;

    // Sign convention matches ADAR_set_Phase: element k gets phase k * PhDelta.
    // For a plane wave arriving at angle theta, the required per-element
    // compensation phase per step is 2*pi*d*sin(theta)*f/c (in radians).
    // We negate so positive steer angles produce a peak at +theta on the plot.
    const c = 299792458;
    const f = state.SignalFreq;
    const d = state.d;
    const phDeltaDeg = -(360 * d * Math.sin(theta_deg * Math.PI / 180) * f / c);
    const ramp = Array.from({ length: 8 }, (_, k) => Math.round(k * phDeltaDeg));

    const taperKey = document.getElementById('steer-taper')?.value || 'uniform';
    const gains = STEER_TAPERS[taperKey] || STEER_TAPERS.uniform;

    applyTaper(gains.slice());  // syncs backend
    applyPhaseList(ramp);
    syncStateToBackend();
});

/* --- Digital Beam Forming mode toggle --- */
function setBeamformerMode(mode) {
    const prevMode = state.bfMode;
    if (mode === prevMode) return;

    if (mode === 'mvdr') {
        // Snapshot current manual weights before zeroing (option b:
        // adaptive weights get a clean slate)
        bfManualSnapshot.B0_Gain = state.B0_Gain;
        bfManualSnapshot.B1_Gain = state.B1_Gain;
        bfManualSnapshot.Beam0_Phase = state.Beam0_Phase;
        bfManualSnapshot.Beam1_Phase = state.Beam1_Phase;
        // Zero them
        state.B0_Gain = 1.0; state.B1_Gain = 1.0;
        state.Beam0_Phase = 0; state.Beam1_Phase = 0;
    } else if (prevMode === 'mvdr') {
        // Restore manual weights
        state.B0_Gain = bfManualSnapshot.B0_Gain;
        state.B1_Gain = bfManualSnapshot.B1_Gain;
        state.Beam0_Phase = bfManualSnapshot.Beam0_Phase;
        state.Beam1_Phase = bfManualSnapshot.Beam1_Phase;
    }
    // Reflect restored/reset values in the manual sliders
    const setPair = (slId, inId, val) => {
        const sl = document.getElementById(slId), inp = document.getElementById(inId);
        if (sl) sl.value = val;
        if (inp) inp.value = val;
    };
    setPair('b0_gain', 'val-b0g', state.B0_Gain);
    setPair('b1_gain', 'val-b1g', state.B1_Gain);
    setPair('b0_phase', 'val-b0p', state.Beam0_Phase);
    setPair('b1_phase', 'val-b1p', state.Beam1_Phase);

    state.bfMode = mode;
    document.getElementById('bf-panel-manual').hidden = (mode !== 'manual');
    document.getElementById('bf-panel-mvdr').hidden = (mode !== 'mvdr');
    syncStateToBackend();
}

document.querySelectorAll('input[name="bf-mode"]').forEach(el => {
    el.addEventListener('change', (e) => {
        if (e.target.checked) setBeamformerMode(e.target.value);
    });
});

document.getElementById('btn-reset-dbf')?.addEventListener('click', () => {
    state.B0_Gain = 1.0; state.B1_Gain = 1.0;
    state.Beam0_Phase = 0; state.Beam1_Phase = 0;
    // Keep the snapshot in sync so a subsequent Manual->MVDR->Manual round
    // trip doesn't restore stale weights.
    bfManualSnapshot.B0_Gain = 1.0; bfManualSnapshot.B1_Gain = 1.0;
    bfManualSnapshot.Beam0_Phase = 0; bfManualSnapshot.Beam1_Phase = 0;
    const setPair = (slId, inId, val) => {
        const sl = document.getElementById(slId), inp = document.getElementById(inId);
        if (sl) sl.value = val;
        if (inp) inp.value = val;
    };
    setPair('b0_gain', 'val-b0g', 1.0);
    setPair('b1_gain', 'val-b1g', 1.0);
    setPair('b0_phase', 'val-b0p', 0);
    setPair('b1_phase', 'val-b1p', 0);
    syncStateToBackend();
});

const mvdrKInput = document.getElementById('mvdr-k');
if (mvdrKInput) {
    mvdrKInput.addEventListener('input', (e) => {
        const v = parseInt(e.target.value);
        if (Number.isFinite(v) && v >= 8) {
            state.mvdrK = v;
            syncStateToBackend();
        }
    });
}
const mvdrDiagInput = document.getElementById('mvdr-diag-load');
if (mvdrDiagInput) {
    mvdrDiagInput.addEventListener('input', (e) => {
        const v = parseFloat(e.target.value);
        if (Number.isFinite(v) && v >= 0) {
            state.mvdrDiagLoad = v;
            syncStateToBackend();
        }
    });
}

/* --- Simulator Interferer (instructor mode only) ---
   The accordion stays hidden unless BOTH conditions are met:
     1. URL contains ?instructor=1 (see instructorMode above)
     2. Backend reports sim_mode: true (revealSimInterfererIfEligible())
   Students loading the app normally never see or reach this panel. */
function revealSimInterfererIfEligible(serverState) {
    const el = document.getElementById('accordion-sim-interferer');
    if (!el) return;
    const showIt = instructorMode && !!serverState?.sim_mode;
    el.hidden = !showIt;
}

/* --- CTF Mode (GRCon26) ---
   A display for backend state, nothing more. It polls ctf_status rather than
   computing anything: the backend needs a poll to advance its dwell clock
   anyway, since a player who steers and then holds still sends no further
   commands. */
let ctfPollTimer = null;

function renderCtfStatus(data) {
    if (!data) return;

    // Sector geometry comes from the backend, never from a constant here: the
    // matcher in phaser_ctf.py owns it, so the bands drawn on the plot cannot
    // drift away from the windows actually being scored.
    const sectorsEl = document.getElementById('ctf-sectors');
    if (sectorsEl && Array.isArray(data.sectors) && !sectorsEl.dataset.filled) {
        // Numbers on top, angles underneath: the sequence is written in sector
        // numbers but the player has to act on degrees, and the table is what
        // saves them doing that translation in their head at the table.
        const nums = data.sectors
            .map(s => `<th>${Number(s.sector)}</th>`).join('');
        const degs = data.sectors.map(s => {
            const deg = Number(s.centre_deg);
            return `<td>${Number.isFinite(deg) ? deg : '?'}°</td>`;
        }).join('');
        sectorsEl.innerHTML = '<table class="ctf-sectors"><tbody>'
            + `<tr>${nums}</tr><tr>${degs}</tr>`
            + '</tbody></table>';
        sectorsEl.dataset.filled = '1';
    }
    if (Array.isArray(data.sectors) && !state.ctfSectors.length) {
        state.ctfSectors = data.sectors;
        drawCtfSectorBands();
    }

    // Progress as a pill in the stat row, beside Est. Angle. It lives there
    // rather than in the panel because that is where the player is already
    // looking, and because the panel is meant to stay quiet.
    //
    // Showing the count at all is a deliberate difficulty decision, not a
    // convenience: it collapses the search from 5^5 orderings to roughly 25
    // guesses, which makes the sector sequence an accelerator for
    // thats_random solvers rather than a hard gate in front of everyone else.
    // Removing it would re-gate this challenge behind another one.
    const pillBox = document.getElementById('ctf-progress-box');
    const pillEl = document.getElementById('ctf-progress-pill');
    if (pillBox && pillEl) {
        pillBox.style.display = 'flex';
        const total = data.sequence_length ?? '—';
        if (data.armed === false) {
            pillEl.textContent = `— / ${total}`;
        } else {
            const done = data.matched ? total : (data.progress ?? 0);
            pillEl.textContent = `${done} / ${total}`;
        }
    }

    // One status line, and it stays quiet during normal play — the bands and
    // the pill are the display. It speaks up only for the two things neither
    // of them can show.
    const statusEl = document.getElementById('ctf-status');
    if (statusEl) {
        if (data.armed === false) {
            // Without this the table reads as broken: the beam moves, the
            // bands are drawn, and nothing ever scores.
            statusEl.textContent = 'Hold the CTF Sequence button to begin'
                + '  ·  where the source is right now does not count';
        } else if (data.matched) {
            statusEl.textContent = 'Sequence complete.';
        } else if (data.source === 'tracked') {
            // The tracked machine confirms on consecutive sweeps, not on a
            // wall clock, so quoting dwell_s here would state the wrong rule.
            statusEl.textContent =
                `Hold the source still in each sector for ${data.dwell_sweeps} sweeps.`;
        } else {
            statusEl.textContent = `Hold each sector ${data.dwell_s}s.`;
        }
    }

    const flagEl = document.getElementById('ctf-flag');
    if (flagEl) {
        if (data.flag) {
            flagEl.textContent = data.flag;
        } else if (data.flag_withheld_in_sim) {
            flagEl.textContent = 'Sequence complete — but this backend is in sim mode, so no flag. Come find the array.';
        } else if (data.matched && !data.configured) {
            flagEl.textContent = 'Sequence complete — no flag configured on this backend.';
        } else {
            flagEl.textContent = '';
        }
    }
}

/* Sector bands on the beam pattern.

   chart-rect's x-axis is already Steering Angle in degrees over [-90, 90], so
   these need no coordinate mapping — but they cannot be relayout'd on their
   own. updateCharts() rebuilds layout.shapes from scratch every frame for the
   peak markers and applies the whole array, so anything set independently is
   erased on the next sweep. ctfSectorShapes() is therefore called from inside
   that rebuild, and this function only handles the case where no sweep is
   running and updateCharts() is not being called at all. */
function ctfSectorShapes() {
    if (!ctfMode || !state.ctfSectorLines || !state.ctfSectors.length) return [];

    const shapes = [];
    for (const s of state.ctfSectors) {
        const lo = s.centre_deg - s.tolerance_deg;
        const hi = s.centre_deg + s.tolerance_deg;
        // A filled band, not a pair of lines: the scoreable region is an
        // interval, and drawing only its edges invites aiming AT an edge.
        shapes.push({
            // Named so drawCtfSectorBands can strip only its own bands if a
            // future change adds other rects to this plot (Plotly 2.30 keeps
            // shape.name). Filtering on type alone would take them too.
            name: 'ctf-sector', type: 'rect', x0: lo, x1: hi, y0: 0, y1: 1, yref: 'paper',
            fillcolor: 'rgba(99, 102, 241, 0.12)',
            line: { color: 'rgba(99, 102, 241, 0.45)', width: 1, dash: 'dot' },
            layer: 'below',
        });
    }
    return shapes;
}

function drawCtfSectorBands() {
    const el = document.getElementById('chart-rect');
    if (!el || !window.Plotly) return;
    const existing = (el.layout?.shapes || []).filter(sh => sh.name !== 'ctf-sector');
    try {
        Plotly.relayout('chart-rect', { shapes: existing.concat(ctfSectorShapes()) });
    } catch (e) { /* chart not ready yet; the next sweep will draw them */ }
}

document.getElementById('ctf-show-sectors')?.addEventListener('change', (e) => {
    state.ctfSectorLines = e.target.checked;
    drawCtfSectorBands();
});

async function pollCtfStatus() {
    try {
        const resp = await transport.invoke('ctf_status', {});
        if (resp?.status === 'ok') renderCtfStatus(resp.data);
    } catch (err) {
        // A dropped poll is not worth logging every 700 ms; the next one retries.
    }
}

/* The browser simulator deliberately has no ctf_status handler: the sequence
   check and the flag live in the backend precisely so they are not in a
   downloadable bundle. Under ?sim=1 the panel says so rather than polling a
   command that will only ever answer "Unknown command". */
function renderCtfNeedsBackend() {
    const sectorsEl = document.getElementById('ctf-sectors');
    if (sectorsEl) {
        sectorsEl.textContent = '';
        // Drop the guard too, or a later reconnect finds a filled flag over an
        // empty table and never rebuilds it.
        delete sectorsEl.dataset.filled;
    }
    const statusEl = document.getElementById('ctf-status');
    if (statusEl) {
        statusEl.textContent = 'CTF mode needs the Python backend. The sector'
            + ' sequence and the flag are checked server-side, so they are not'
            + ' part of this page. Connect to a phaser_headless.py backend to play.';
    }
    const flagEl = document.getElementById('ctf-flag');
    if (flagEl) flagEl.textContent = '';
    const resetBtn = document.getElementById('btn-ctf-reset');
    if (resetBtn) resetBtn.disabled = true;
    // No backend means no sector geometry to draw; the toggle would control nothing.
    const bandsToggle = document.getElementById('ctf-show-sectors');
    if (bandsToggle) bandsToggle.disabled = true;
    state.ctfSectorLines = false;
    // No backend, no progress to report -- leave the stat row alone rather
    // than parking a dead "— / —" pill next to the live readouts.
    const pillBox = document.getElementById('ctf-progress-box');
    if (pillBox) pillBox.style.display = 'none';
}

function revealCtfIfEligible() {
    const el = document.getElementById('accordion-ctf');
    if (!el) return;
    el.hidden = !ctfMode;
    if (!ctfMode) return;
    if (resolveTransportMode() === 'sim') {
        renderCtfNeedsBackend();
        return;
    }
    if (!ctfPollTimer) {
        pollCtfStatus();
        ctfPollTimer = setInterval(pollCtfStatus, 700);
    }
}

async function ctfReset() {
    try {
        const resp = await transport.invoke('ctf_reset', {});
        if (resp?.status === 'ok') renderCtfStatus(resp.data);
        return true;
    } catch (err) {
        addRuntimeLog('warn', 'CTF', 'Could not reset: ' + err);
        return false;
    }
}

/* CTF Sequence button: hold to start a run, and only hold. A short click does
   nothing on purpose.

   ctf_reset clears the trail, so a stray click mid-run silently discards
   everything the player has walked. Requiring a deliberate hold, with the fill
   telegraphing what is about to happen, makes that impossible to do by
   accident while leaving it one gesture away on purpose. */
const CTF_HOLD_MS = 1200;
(() => {
    const btn = document.getElementById('btn-ctf-reset');
    if (!btn) return;
    let holdTimer = null;
    let pointerActive = false;

    const fire = () => {
        btn.classList.remove('holding');
        ctfReset().then((ok) => {
            if (!ok) return;
            btn.classList.add('flash');
            setTimeout(() => btn.classList.remove('flash'), 260);
        });
    };

    const startHold = () => {
        if (btn.disabled) return;
        if (holdTimer) clearTimeout(holdTimer);
        btn.classList.add('holding');
        holdTimer = setTimeout(() => { holdTimer = null; fire(); }, CTF_HOLD_MS);
    };

    const cancelHold = () => {
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
        btn.classList.remove('holding');
    };

    btn.addEventListener('pointerdown', (e) => {
        if (btn.disabled) return;
        if (e.button !== undefined && e.button !== 0) return;  // primary only
        pointerActive = true;
        startHold();
    });
    btn.addEventListener('pointerup', () => { pointerActive = false; cancelHold(); });
    btn.addEventListener('pointercancel', () => { pointerActive = false; cancelHold(); });
    btn.addEventListener('pointerleave', () => {
        if (pointerActive) cancelHold();
        pointerActive = false;
    });

    // Keyboard: a held key repeats rather than reporting a duration, so there
    // is no honest hold gesture here. Enter/Space commits directly -- reaching
    // for the keyboard is already deliberate in a way a stray tap is not.
    btn.addEventListener('keydown', (e) => {
        if ((e.key === 'Enter' || e.key === ' ') && !btn.disabled) {
            e.preventDefault();
            fire();
        }
    });
})();

const simInterfererEnable = document.getElementById('sim-interferer-enable');
if (simInterfererEnable) {
    simInterfererEnable.addEventListener('change', (e) => {
        state.sim_interferer_enable = !!e.target.checked;
        syncStateToBackend();
    });
}

// Angle: slider <-> number input, both write into state
function linkInterfererPair(slId, inId, stateKey) {
    const sl = document.getElementById(slId);
    const inp = document.getElementById(inId);
    if (!sl || !inp) return;
    const onChange = (raw) => {
        const v = parseFloat(raw);
        if (Number.isFinite(v)) {
            state[stateKey] = v;
            syncStateToBackend();
        }
    };
    sl.addEventListener('input', (e) => { inp.value = e.target.value; onChange(e.target.value); });
    inp.addEventListener('input', (e) => { sl.value = e.target.value; onChange(e.target.value); });
}
linkInterfererPair('sim-interferer-angle', 'val-sim-interferer-angle', 'sim_interferer_angle_deg');
linkInterfererPair('sim-interferer-power', 'val-sim-interferer-power', 'sim_interferer_power_db');

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

/* --- Show Logs Tab toggle --- */
const LOGS_TAB_KEY = 'phaser_show_logs_tab';
function applyLogsTabVisibility(show) {
    const tabBtn = document.getElementById('tab-btn-logs');
    if (tabBtn) tabBtn.hidden = !show;
    // If the user hides the Logs tab while it's the active one, switch to Rect.
    if (!show && tabBtn?.classList.contains('active')) {
        document.querySelector('.tab-btn[data-target="tab-rect"]')?.click();
    }
}
const showLogsToggle = document.getElementById('opt-show-logs');
if (showLogsToggle) {
    const stored = localStorage.getItem(LOGS_TAB_KEY) === '1';
    showLogsToggle.checked = stored;
    applyLogsTabVisibility(stored);
    showLogsToggle.addEventListener('change', (e) => {
        const on = !!e.target.checked;
        localStorage.setItem(LOGS_TAB_KEY, on ? '1' : '0');
        applyLogsTabVisibility(on);
    });
}

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

        // Reveal the instructor-only interferer panel iff (a) URL has
        // ?instructor=1 AND (b) backend is running in sim mode. Also
        // hydrate the panel's controls from the current server state.
        revealSimInterfererIfEligible(msg.data);
        revealCtfIfEligible();
        if (typeof msg.data.sim_interferer_enable === 'boolean') {
            state.sim_interferer_enable = msg.data.sim_interferer_enable;
            const el = document.getElementById('sim-interferer-enable');
            if (el) el.checked = msg.data.sim_interferer_enable;
        }
        if (Number.isFinite(msg.data.sim_interferer_angle_deg)) {
            state.sim_interferer_angle_deg = msg.data.sim_interferer_angle_deg;
            const s = document.getElementById('sim-interferer-angle');
            const n = document.getElementById('val-sim-interferer-angle');
            if (s) s.value = msg.data.sim_interferer_angle_deg;
            if (n) n.value = msg.data.sim_interferer_angle_deg;
        }
        if (Number.isFinite(msg.data.sim_interferer_power_db)) {
            state.sim_interferer_power_db = msg.data.sim_interferer_power_db;
            const s = document.getElementById('sim-interferer-power');
            const n = document.getElementById('val-sim-interferer-power');
            if (s) s.value = msg.data.sim_interferer_power_db;
            if (n) n.value = msg.data.sim_interferer_power_db;
        }

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
// Aperture presets: sparse element patterns for teaching array topology
document.getElementById('taper-2elem')?.addEventListener('click', () => applyTaper([0,0,0,127,127,0,0,0]));
document.getElementById('taper-sparse-lambda')?.addEventListener('click', () => applyTaper([127,0,127,0,127,0,127,0]));

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
        range: [-90, 90],
        autorange: false
    },
    yaxis: {
        title: 'Magnitude (dBFS)',
        gridcolor: getPlotPalette().gridColor,
        griddash: 'dash',
        range: [-50, 0],
        autorange: false
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

Plotly.newPlot('chart-polar', [
    // Trace 0: main sum beam
    { r: [], theta: [], type: 'scatterpolar', mode: 'lines', line: { color: '#10b981', width: 3, shape: 'spline' }, fill: 'toself', fillcolor: 'rgba(16, 185, 129, 0.1)' },
    // Trace 1: peak-angle marker (radial line at peak theta)
    { r: [], theta: [], type: 'scatterpolar', mode: 'lines', line: { color: '#ef4444', width: 1.5, dash: 'dash' }, hoverinfo: 'skip', showlegend: false, visible: false },
    // Trace 2: peak-gain marker (circle at peak dBFS)
    { r: [], theta: [], type: 'scatterpolar', mode: 'lines', line: { color: '#10b981', width: 1.5, dash: 'dash' }, hoverinfo: 'skip', showlegend: false, visible: false },
], Object.assign({}, getLayoutBase(), {
    // The shared margin is sized for a cartesian plot, whose y-axis labels sit
    // well inside the right edge. A [0, 180] sector puts its outermost angular
    // ticks -- "-90" and "90" -- hard against both sides, and r:15 was not
    // enough for the right-hand one: it was clipped by ~8px at every viewport
    // width, desktop included.
    margin: { t: 20, r: 40, l: 48, b: 30 },
    polar: {
        sector: [0, 180],
        bgcolor: 'transparent',
        radialaxis: {
            visible: true,
            range: [-50, 0],
            autorange: false,
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
    
    // Update Cartesian Axes (force autorange off so traces don't re-trigger
    // autoscaling on every redraw or addTraces call).
    Plotly.relayout('chart-rect', {
        'xaxis.range': [xMin, xMax],
        'xaxis.autorange': false,
        'yaxis.range': [yMin, yMax],
        'yaxis.autorange': false
    });

    Plotly.relayout('chart-polar', {
        'polar.radialaxis.range': [yMin, yMax],
        'polar.radialaxis.autorange': false
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
    // Ask the transport layer which mode it picked rather than re-deriving it
    // here. The old copy of this logic tested for an IPC bridge that no longer
    // exists, so it could only ever report "web".
    const mode = resolveTransportMode();
    addRuntimeLog('info', 'TRANSPORT', mode === 'sim'
        ? 'mode=sim  (in-browser simulator, no hardware)'
        : 'mode=web  (WebSocket + REST)');

    // The badge is for non-default transports; plain web mode leaves it hidden.
    const badgeEl = document.getElementById('transport-badge');
    if (!badgeEl) return;
    if (mode === 'sim') {
        badgeEl.textContent = 'SIMULATION';
        badgeEl.style.display = 'inline-flex';
        badgeEl.title = 'Simulated data — no Phaser hardware is connected';
        badgeEl.style.setProperty('--transport-badge-bg', 'rgba(245,158,11,0.15)');
        badgeEl.style.setProperty('--transport-badge-color', '#f59e0b');
        badgeEl.style.setProperty('--transport-badge-border', 'rgba(245,158,11,0.45)');
    }
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
            // The websocket transport resolves the raw backend reply, and
            // get_calibration_status answers FLAT -- {status, running, task,
            // returncode, ...} with no `data` envelope. Reading msg.data gave
            // undefined on every poll, and updateCalibrationModal() starts with
            // `if (!data) return`, so the modal sat on "Running..." forever
            // even though the run had finished successfully. That is the
            // "find HB100 hangs" symptom. Accept either shape.
            const cal = msg.data ?? msg;
            updateCalibrationPill(cal);
            updateCalibrationModal(cal);
            trackCalibrationLogUpdates(cal);
            if (cal && !cal.running && cal.returncode === 0) {
                const reloadableTasks = new Set(['find_hb100', 'phaser_cal']);
                if (reloadableTasks.has(cal.task)) {
                    const reloadKey = `${cal.task}:${cal.started_at}:${cal.returncode}`;
                    if (reloadKey !== calibrationLogState.lastReloadKey) {
                        calibrationLogState.lastReloadKey = reloadKey;
                        await loadStateFromServer();
                        addRuntimeLog('info', 'CAL', `Reloaded UI state after ${cal.task}`);
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

let calibrationAutoCloseTimer = null;

function updateCalibrationModal(data) {
    const modal = document.getElementById('calibration-modal');
    if (!modal || modal.hidden) return;

    const statusEl = document.getElementById('cal-modal-status');
    const outputEl = document.getElementById('cal-modal-output');
    const actionBtn = document.getElementById('btn-cal-action');
    const spinnerEl = document.getElementById('cal-spinner');

    if (!data) return;

    if (data.running) {
        // Clear any pending auto-close when a new run starts
        if (calibrationAutoCloseTimer) {
            clearTimeout(calibrationAutoCloseTimer);
            calibrationAutoCloseTimer = null;
        }
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
            if (statusEl) statusEl.innerText = 'Completed successfully!';
            // Auto-close after 2 seconds on success
            if (!calibrationAutoCloseTimer) {
                calibrationAutoCloseTimer = setTimeout(() => {
                    hideCalibrationModal();
                    calibrationAutoCloseTimer = null;
                }, 2000);
            }
        } else if (data.returncode !== null && data.returncode !== undefined) {
            if (statusEl) statusEl.innerText = `Failed (code ${data.returncode})`;
            // Auto-close after 5 seconds on failure (give more time to read error)
            if (!calibrationAutoCloseTimer) {
                calibrationAutoCloseTimer = setTimeout(() => {
                    hideCalibrationModal();
                    calibrationAutoCloseTimer = null;
                }, 5000);
            }
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

/* --- Connection: simulator toggle + backend URL ------------------------------
 *
 * Both live in the Configuration pane rather than the header. Each reloads the
 * page, because the transport is chosen once at startup: reusing the whole
 * init path (readiness probe, state load, control population) is far less
 * fragile than tearing a live transport out from under a running sweep, and it
 * keeps the mode in the URL so it stays shareable.
 */
function reloadWith(params) {
    const url = new URL(window.location.href);
    for (const [k, v] of Object.entries(params)) {
        if (v === null) url.searchParams.delete(k);
        else url.searchParams.set(k, v);
    }
    window.location.href = url.toString();
}

const simToggle = document.getElementById('opt-sim-mode');
if (simToggle) {
    const simActive = resolveTransportMode() === 'sim';
    simToggle.checked = simActive;
    simToggle.addEventListener('change', (e) => {
        // Explicit 0 rather than deleting the parameter: a build that defaults
        // to sim (GitHub Pages) needs to be told to leave it.
        reloadWith({ sim: e.target.checked ? '1' : '0' });
    });
}

const backendInput = document.getElementById('backend-url');
if (backendInput) {
    const simActive = resolveTransportMode() === 'sim';
    const override = getBackendUrlOverride();
    backendInput.value = override;
    backendInput.placeholder = autoBackendWsUrl();

    // A tooltip rather than standing text: it explains a field most sessions
    // never touch, so it should be there when reached for and invisible
    // otherwise.
    function describeBackend() {
        if (simActive) {
            backendInput.title =
                'Used when Simulator Mode is off. Set it to reach a Phaser that '
                + 'is not serving this page — a Tailscale hostname, say.';
        } else if (override) {
            backendInput.title = `Connecting to ${override}`;
        } else {
            backendInput.title =
                `Auto: ${autoBackendWsUrl()} — the origin serving this page. `
                + 'Set a ws:// or wss:// URL to reach a different Phaser.';
        }
    }
    describeBackend();

    function applyBackendUrl() {
        const next = backendInput.value.trim();
        if (next === override) return;
        if (next && !/^wss?:\/\//i.test(next)) {
            addRuntimeLog('error', 'WS', 'Backend URL must start with ws:// or wss://');
            backendInput.focus();
            return;
        }
        // An https page cannot open an insecure socket; say so here rather than
        // letting the browser fail it silently as mixed content.
        if (next.startsWith('ws://') && window.location.protocol === 'https:') {
            addRuntimeLog('error', 'WS',
                'This page is served over https, so it can only use a wss:// backend.');
            backendInput.focus();
            return;
        }
        if (!setBackendUrlOverride(next)) {
            addRuntimeLog('warn', 'WS', 'Could not save the backend URL (storage blocked).');
            return;
        }
        // Leaving simulator mode is implied by pointing at a backend.
        reloadWith({ sim: '0', backend: null });
    }

    backendInput.addEventListener('change', applyBackendUrl);
    backendInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); applyBackendUrl(); }
    });
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

    // Apply Enforce Symmetric Taper toggle (used by Lab 3 to keep student
    // taper exploration mirror-symmetric across the array center)
    const symmetricTaperEl = document.getElementById('opt-symmetric-taper');
    if (symmetricTaperEl && typeof preset.symmetricTaper === 'boolean') {
        symmetricTaperEl.checked = preset.symmetricTaper;
    }

    const tabName = preset.ui_tab || 'tab-rect';
    document.querySelector(`[data-target="${tabName}"]`)?.click();
    applyInitialStateToControls();

    // Send preset state to backend
    syncStateToBackend();
}

function localLabPreset(labIdx) {
    // Base state matches the "clean slate" the labs expect: uniform 8-element
    // array, zero per-element phase, 7-bit phase (default hardware), 10 MHz
    // signal BW (so beam-squint is negligible until Lab 5 raises it).
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
    // Preset entries are aligned with docs/2025_Phaser_labs_Python.pdf
    // (PHASER Phased Array Radar Workshop, Python edition, 2025).
    switch (labIdx) {
        // Lab 1 — STEERING ANGLE (PDF p.9): student moves the Steering Angle
        // slider while watching the FFT to observe the peak, then switches
        // to the Rect plot. Start on FFT tab in Beam Sweep so they see live
        // spectrum immediately.
        case 1: return { ...base, mode: 'Beam Sweep', ui_tab: 'tab-fft' };

        // Lab 2 — ARRAY FACTOR AND BEAMWIDTH (PDF p.11): uniform 8-element
        // array on Rect plot. PDF has student later disable Rx1/2/7/8 by
        // hand to reduce to N=4, then N=2 — we start at the full array.
        case 2: return { ...base, ui_tab: 'tab-rect' };

        // Lab 3 — SIDELOBES AND TAPERING (PDF p.16): PDF instructs student
        // to enable Symmetric Taper and then try different taper profiles.
        // Preset enforces symmetric taper so exploration stays symmetric.
        case 3: return { ...base, ui_tab: 'tab-rect', symmetricTaper: true };

        // Lab 4 — GRATING LOBES (PDF p.17): PDF instructs student to set
        // Rx2/3/5/6/8 = 0, leaving Rx1/4/7 active with spacing d_eff = 3d
        // = 42mm. At 10.3 GHz (λ ≈ 29mm), grating lobes appear at
        // sin⁻¹(m·λ/d_eff) ≈ ±44°. Preset applies that taper directly so
        // students see the grating lobes on first sweep.
        case 4: return { ...base, ui_tab: 'tab-rect',
                         gainList: [100, 0, 0, 100, 0, 0, 100, 0] };

        // Lab 5 — BEAM SQUINT (PDF p.19): PDF sets signal BW to 500 MHz and
        // steers to +45° so the student sees the ~3° squint predicted by
        // arcsin(10.5/10 * sin(45°)) - 45°.
        case 5: return { ...base, BW: 500, ui_tab: 'tab-rect' };

        // Lab 6 — QUANTIZATION SIDELOBES (PDF p.21): PDF explicitly says
        // "Blackman is the pre-programmed default for this lab" and sets
        // steering angle to 15°. Uses Phase Shift Bits (not steer_res) as
        // the quantization knob — set ignore_res=true so student's changes
        // to the Bits slider drive the phase step.
        case 6: return { ...base, ui_tab: 'tab-rect',
                         gainList: [6, 27, 66, 100, 100, 66, 27, 6],
                         ignore_res: true };

        // Lab 7 — MEASURING THE ACTUAL ANTENNA PATTERN (PDF p.14):
        // (Renamed from "Hybrid Control" — PDF has no lab by that name.)
        // PDF selects Signal vs Time mode so the student physically rotates
        // the HB100 by hand and traces the pattern amplitude vs time.
        case 7: return { ...base, mode: 'Signal vs Time', ui_tab: 'tab-tracking' };

        // Lab 8 — MONOPULSE TRACKING (PDF p.28): PDF selects Blackman taper
        // and enables monopulse delta/error display. Tracking mode drives
        // the closed-loop steering.
        case 8: return { ...base, mode: 'Tracking', ui_tab: 'tab-rect',
                         gainList: [6, 27, 66, 100, 100, 66, 27, 6],
                         showDelta: true, showError: true };

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
        
        // Sector bands first, so the peak markers draw over them.
        const shapes = ctfSectorShapes();

        // Render Peak Markers (applied via Plotly.relayout below)
        if(document.getElementById('opt-peak-angle').checked) {
            shapes.push({ type: 'line', x0: xData[peakIndex], y0: 0, x1: xData[peakIndex], y1: 1, yref: 'paper', line: { color: '#ef4444', dash: 'dash'} });
        }
        if(document.getElementById('opt-peak-gain').checked) {
            shapes.push({ type: 'line', x0: 0, y0: peakValue, x1: 1, y1: peakValue, xref: 'paper', line: { color: '#10b981', dash: 'dash'} });
        }


        // Snapshot the live Sum trace for the Memory feature.
        lastLiveTrace = { x: xData.slice(), y: yData.slice() };
        updateMemoryButtons();

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
            // Apply peak-marker shapes (or clear them if both toggles are off).
            // These live in layout, not in trace data, so a redraw alone
            // won't touch them — we need an explicit relayout.
            Plotly.relayout('chart-rect', { shapes });
        }

        // Update polar plot
        const polarEl = document.getElementById('chart-polar');
        if (polarEl && polarEl.data && polarEl.data[0]) {
            polarEl.data[0].r = yData;
            polarEl.data[0].theta = xData.map(toPolarTheta);

            // Peak-angle marker: radial spoke at the peak theta.
            // Runs from the current radial-axis min to the peak's dB value
            // so it visually tracks the peak on both axes.
            if (polarEl.data[1]) {
                const showPeakAngle = document.getElementById('opt-peak-angle')?.checked ?? false;
                if (showPeakAngle) {
                    const peakTheta = toPolarTheta(xData[peakIndex]);
                    const rMin = (polarEl.layout?.polar?.radialaxis?.range?.[0]) ?? -50;
                    polarEl.data[1].r = [rMin, peakValue];
                    polarEl.data[1].theta = [peakTheta, peakTheta];
                    polarEl.data[1].visible = true;
                } else {
                    polarEl.data[1].visible = false;
                }
            }

            // Peak-gain marker: constant-r arc across the visible sector.
            if (polarEl.data[2]) {
                const showPeakGain = document.getElementById('opt-peak-gain')?.checked ?? false;
                if (showPeakGain) {
                    const arcN = 61;
                    const arcTheta = Array.from({ length: arcN }, (_, i) => 180 * i / (arcN - 1));
                    polarEl.data[2].r = arcTheta.map(() => peakValue);
                    polarEl.data[2].theta = arcTheta;
                    polarEl.data[2].visible = true;
                } else {
                    polarEl.data[2].visible = false;
                }
            }

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
