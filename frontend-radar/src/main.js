import { createTransport, resolveTransportMode } from './transport.js';
import { fromBase64, readNpz } from './npz.js';
import { captureBytes } from './sim/dsp.js';

/* --- State --- */
const state = {
    running: false,
    waterfall_depth: 50,
    waterfall: null,         // 2D array of magnitudes (rows = time, cols = velocity bin)
    velocity_axis: null,     // m/s, length = fft_size after trimming
    last_frame_t: null,
    fps_alpha: 0.2,
    fps: 0,
    autoscale: false,
    db_floor: -100,
    db_ceil: 0,
    vel_max: 20,
    taper: 'blackman',
    // CW or FMCW: which controls, tabs and Start the page shows, and which
    // mode is running on the backend (null when idle).
    uiMode: 'cw',
    runningMode: null,
    playback: null,          // a recording playing through the FMCW views, or null
    fmcw: {
        available: null,     // from get_fmcw_radar_state's capability
        cfg: null,           // the running config, for the 0 m calibration
        plan: null,
        waterfall: null,     // rows = frames, cols = range bins
        last: null,          // the last frame, for the 0 m calibration
        span_db: 60,         // color scale: the top down this far
        scales: {},          // per chart: the fixed color scale {top, bottom} (colorScale)
        avg: null,           // averaged single-chirp profile power, for the readout
        readout_t: 0,        // when the peak readouts last changed
        preset_info: {},     // {preset: {num_chirps}}, from get_fmcw_radar_state
        // Range-Doppler: the waterfall and "moving" trace skip |v| below
        // this, as the CW peak readout does. The strongest cell per range bin
        // over every velocity is the stationary clutter at 0 m/s -- walls,
        // furniture, Tx->Rx leakage -- which a hand never outshines, so a
        // waterfall built from it showed fixed stripes (phaser-01, 2026-10-30).
        moving_min_mps: 0.3,
    },
};

// Every Plotly chart on the page, for the resize-after-layout-change passes.
const ALL_CHARTS = ['chart-waterfall', 'chart-spectrum', 'chart-time',
                    'chart-fmcw-range', 'chart-fmcw-wf', 'chart-fmcw-rd'];
const C_LIGHT = 299792458;

/* --- Theme --- */
const THEME_KEY = 'phaser_radar_theme';

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
    };
}

function applyTheme(theme, persist = true) {
    const next = theme === 'light' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', next);
    if (persist) localStorage.setItem(THEME_KEY, next);
    const btn = document.getElementById('btn-theme-toggle');
    if (btn) {
        const iconEl = btn.querySelector('.theme-icon');
        const labelEl = btn.querySelector('.theme-label');
        if (iconEl) iconEl.textContent = next === 'light' ? '☀' : '☾';
        if (labelEl) labelEl.textContent = next === 'light' ? 'Light Mode' : 'Dark Mode';
    }
    document.querySelectorAll('#btn-theme-toggle-icon .icon-moon, #btn-theme-toggle-icon .icon-sun').forEach(svg => {
        const isMoon = svg.classList.contains('icon-moon');
        svg.style.display = (next === 'light' ? !isMoon : isMoon) ? '' : 'none';
    });
    applyPlotTheme();
}

function initTheme() {
    const stored = localStorage.getItem(THEME_KEY);
    const sysDark = window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
    applyTheme(stored || (sysDark ? 'dark' : 'light'), false);
    document.getElementById('btn-theme-toggle')?.addEventListener('click', () => applyTheme(getTheme() === 'dark' ? 'light' : 'dark'));
    document.getElementById('btn-theme-toggle-icon')?.addEventListener('click', () => applyTheme(getTheme() === 'dark' ? 'light' : 'dark'));
}

/* --- Logging --- */
function log(level, source, message) {
    const console_el = document.getElementById('log-console');
    if (!console_el) return;
    const ts = new Date().toLocaleTimeString();
    const div = document.createElement('div');
    div.className = `log-line log-${level}`;
    div.textContent = `[${ts}] ${source}: ${message}`;
    console_el.appendChild(div);
    while (console_el.childElementCount > 500) console_el.removeChild(console_el.firstChild);
    console_el.scrollTop = console_el.scrollHeight;
}
document.getElementById('btn-clear-logs')?.addEventListener('click', () => {
    const c = document.getElementById('log-console');
    if (c) c.innerHTML = '';
});

/* --- Accordion header icons --- */
const accordionIcons = [
    // 0. Hardware (gear)
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><circle cx="12" cy="12" r="3"></circle><path d="M12 3.5v2.2M12 18.3v2.2M20.5 12h-2.2M5.7 12H3.5M18.01 5.99l-1.56 1.56M7.55 16.45l-1.56 1.56M18.01 18.01l-1.56-1.56M7.55 7.55L5.99 5.99"></path></svg>',
    // 1. CW Capture (sine wave)
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M3 12c2-4 4-4 6 0s4 4 6 0 4-4 6 0"></path></svg>',
    // 2. Processing (FFT bars)
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M5 19V13M9 19V8M13 19V11M17 19V6M21 19V14"></path></svg>',
    // 3. Display (monitor)
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M3.5 5h17v11h-17z"></path><path d="M9 19h6M12 16v3"></path></svg>',
    // 4. FMCW (sawtooth ramps)
    '<svg viewBox="0 0 24 24" style="width:100%; height:100%; stroke:currentColor; fill:none; stroke-width:1.8; stroke-linecap:round; stroke-linejoin:round;"><path d="M4 18L10 6M10 18l6-12M16 18l4-8"></path></svg>',
];

document.querySelectorAll('.accordion-icon[data-icon]').forEach(iconEl => {
    const iconIdx = parseInt(iconEl.getAttribute('data-icon'), 10);
    if (iconIdx >= 0 && iconIdx < accordionIcons.length) {
        iconEl.innerHTML = accordionIcons[iconIdx];
    }
});

/* --- Sidebar collapse + accordion --- */
const settingsPanel = document.getElementById('settings-panel');
const dashboard = document.querySelector('.dashboard');
const toggleSettingsBtn = document.getElementById('btn-toggle-settings');
const toggleSettingsIconBtn = document.getElementById('btn-toggle-settings-icon');
const sidebarIconsEl = document.getElementById('sidebar-icons');
const sidebarContentEl = document.getElementById('sidebar-content');
const sidebarSectionButtons = document.querySelectorAll('.sidebar-icon-btn[data-section]');
const accordionItems = document.querySelectorAll('.accordion-item');
let currentExpandedSection = 0;

function resizePlotsAfterSidebarTransition() {
    setTimeout(() => {
        ALL_CHARTS.forEach(id => {
            const el = document.getElementById(id);
            if (el) Plotly.Plots.resize(el);
        });
    }, 300);
}

function setSidebarCollapsed(collapsed) {
    if (!dashboard) return;
    settingsPanel?.classList.toggle('collapsed', collapsed);
    sidebarIconsEl?.style.removeProperty('display');
    sidebarContentEl?.style.removeProperty('display');
    dashboard.classList.toggle('settings-collapsed', collapsed);
    if (toggleSettingsBtn) {
        toggleSettingsBtn.innerText = collapsed ? '☰' : '−';
        const label = collapsed ? 'Expand settings' : 'Collapse settings';
        toggleSettingsBtn.title = label;
        toggleSettingsBtn.setAttribute('aria-label', label);
    }
    if (toggleSettingsIconBtn) {
        const label = collapsed ? 'Expand settings' : 'Collapse settings';
        toggleSettingsIconBtn.title = label;
        toggleSettingsIconBtn.setAttribute('aria-label', label);
    }
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
    setSidebarCollapsed(!dashboard?.classList.contains('settings-collapsed'));
});

toggleSettingsIconBtn?.addEventListener('click', () => {
    const collapsed = dashboard?.classList.contains('settings-collapsed');
    if (collapsed) {
        setSidebarCollapsed(false);
        activateSidebarSection(currentExpandedSection ?? 0);
    } else {
        setSidebarCollapsed(true);
    }
});

sidebarSectionButtons.forEach(btn => {
    btn.addEventListener('click', () => {
        const sectionIdx = parseInt(btn.dataset.section, 10);
        const collapsed = dashboard?.classList.contains('settings-collapsed');
        if (!collapsed && sectionIdx === currentExpandedSection) {
            setSidebarCollapsed(true);
            return;
        }
        if (collapsed) setSidebarCollapsed(false);
        activateSidebarSection(sectionIdx);
    });
});

document.querySelectorAll('.accordion-header').forEach(header => {
    header.addEventListener('click', () => {
        const item = header.parentElement;
        item.classList.toggle('active');
    });
});

/* --- Tabs --- */
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
        btn.classList.add('active');
        const targetId = btn.dataset.target;
        document.getElementById(targetId)?.classList.add('active');
        // resize plots after tab switch
        setTimeout(() => {
            ALL_CHARTS.forEach(id => {
                const el = document.getElementById(id);
                if (el) Plotly.Plots.resize(el);
            });
        }, 50);
    });
});

/* The FMCW plot a preset opens on: Range-Doppler for a multi-chirp preset
   (or a burst recording), Range Waterfall for a single-chirp one. The
   single-chirp Range profile is a tab to pick, not the first view: on its
   own it is the hardest of the three to read. */
function fmcwDefaultTab() {
    const multi = state.playback
        ? state.playback.info?.mode === 'burst'
        : (state.fmcw.preset_info?.[document.getElementById('fmcw-preset')?.value]?.num_chirps ?? 256) > 1;
    return multi ? 'tab-fmcw-rd' : 'tab-fmcw-wf';
}

/* Go to fmcwDefaultTab() -- on entering FMCW, a new preset or a loaded
   recording -- unless the user is reading the Logs. */
function showFmcwDefaultTab({ force = false } = {}) {
    const active = document.querySelector('.tab-btn.active')?.dataset.target;
    if (active === 'tab-logs' && !force) return;
    const want = fmcwDefaultTab();
    if (active !== want) document.querySelector(`.tab-btn[data-target="${want}"]`)?.click();
}

/* --- Slider/Input combo linking --- */
function linkComboSlider(rangeId, numId, onChange) {
    const r = document.getElementById(rangeId);
    const n = document.getElementById(numId);
    if (!r || !n) return;
    const sync = (src, dst) => () => {
        dst.value = src.value;
        onChange?.(parseFloat(src.value));
    };
    r.addEventListener('input', sync(r, n));
    n.addEventListener('input', sync(n, r));
}

linkComboSlider('rx-gain', 'val-rx-gain', (v) => maybePushParams({ rx_gain: v }));
linkComboSlider('tx-gain', 'val-tx-gain', (v) => maybePushParams({ tx_gain: v }));
linkComboSlider('db-floor', 'val-db-floor', (v) => { state.db_floor = v; refreshWaterfallScale(); });
linkComboSlider('db-ceil', 'val-db-ceil', (v) => { state.db_ceil = v; refreshWaterfallScale(); });
linkComboSlider('vel-max', 'val-vel-max', (v) => {
    state.vel_max = v;
    refreshAxisRanges();
    // The backend crops the Doppler window before sending it, so widening the
    // axis without telling it just adds empty margin to the plot.
    maybePushParams({ vel_max: v });
});
linkComboSlider('waterfall-depth', 'val-waterfall-depth', (v) => {
    state.waterfall_depth = parseInt(v, 10);
    state.waterfall = null;  // reset; will reinit on next frame
    state.fmcw.waterfall = null;
});

document.getElementById('opt-autoscale')?.addEventListener('change', (e) => {
    state.autoscale = e.target.checked;
    refreshWaterfallScale();
});

['output-freq', 'center-freq', 'sample-rate', 'fft-size', 'signal-freq', 'fft-window'].forEach(id => {
    document.getElementById(id)?.addEventListener('change', () => {
        // FMCW applies these while running (the window on the next frame,
        // the frequencies by re-entering); CW takes them on the next Start.
        if (state.runningMode === 'fmcw') {
            const p = readFmcwParams();
            const key = { 'output-freq': 'output_freq', 'center-freq': 'center_freq',
                          'fft-window': 'fft_window' }[id];
            if (key) pushFmcwParams({ [key]: p[key] });
            return;
        }
        log('info', 'UI', `${id} changed — applies on next Start`);
    });
});

/* Taper preset buttons. The backend latches these into the ADAR1000: live
   while running, and via readParams() on the next Start when it is not. */
document.querySelectorAll('#taper-rect, #taper-hann, #taper-black').forEach(b => {
    b.addEventListener('click', () => {
        document.querySelectorAll('#taper-rect, #taper-hann, #taper-black').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        const map = { 'taper-rect': 'rect', 'taper-hann': 'hann', 'taper-black': 'blackman' };
        state.taper = map[b.id];
        maybePushParams({ taper: state.taper });
    });
});

/* --- Plotly setup --- */
function getLayoutBase() {
    const palette = getPlotPalette();
    return {
        paper_bgcolor: 'transparent',
        plot_bgcolor: 'transparent',
        font: { color: palette.fontColor, family: "'Outfit', sans-serif" },
        margin: { t: 20, r: 20, l: 60, b: 40 },
        showlegend: false,
        hovermode: 'closest'
    };
}

function applyPlotTheme() {
    const palette = getPlotPalette();
    const common = {
        'font.color': palette.fontColor,
        'paper_bgcolor': 'transparent',
        'plot_bgcolor': 'transparent',
    };
    ALL_CHARTS.forEach(id => {
        if (!document.getElementById(id)) return;
        try {
            Plotly.relayout(id, {
                ...common,
                'xaxis.gridcolor': palette.gridColor,
                'yaxis.gridcolor': palette.gridColor,
            });
        } catch (e) { /* plot not yet initialized */ }
    });
}

// Heatmap: x = velocity (m/s), y = time index (0 = newest at top), z = magnitude dB
Plotly.newPlot('chart-waterfall', [{
    z: [[]],
    x: [],
    y: [],
    type: 'heatmap',
    colorscale: 'Viridis',
    zmin: -100,
    zmax: 0,
    colorbar: { title: 'dB', thickness: 12 },
    hovertemplate: 'v=%{x:.2f} m/s<br>t=-%{y}<br>%{z:.1f} dB<extra></extra>',
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Velocity (m/s)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', range: [-20, 20] },
    yaxis: { title: 'Time (frames ago)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', autorange: 'reversed' },
}), { displayModeBar: false, responsive: true });

Plotly.newPlot('chart-spectrum', [{
    x: [], y: [], type: 'scatter', mode: 'lines',
    line: { color: '#10b981', width: 2 },
    fill: 'tozeroy', fillcolor: 'rgba(16,185,129,0.1)',
    name: 'Doppler Spectrum'
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Velocity (m/s)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', range: [-20, 20] },
    yaxis: { title: 'Magnitude (dB)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', range: [-100, 0] },
}), { displayModeBar: false, responsive: true });

Plotly.newPlot('chart-time', [{
    x: [], y: [], type: 'scatter', mode: 'lines',
    line: { color: '#8b5cf6', width: 1 },
    name: '|IQ|'
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Sample', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: '|IQ| (counts)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
}), { displayModeBar: false, responsive: true });

// --- FMCW plots (C4) ---
// Range: the profile (or, for range-Doppler, the strongest cell per range
// bin), the CFAR threshold and the detections.
Plotly.newPlot('chart-fmcw-range', [
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Range profile',
      line: { color: '#10b981', width: 2 } },
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'Moving only',
      line: { color: '#8b5cf6', width: 2 } },
    { x: [], y: [], type: 'scatter', mode: 'lines', name: 'CFAR threshold',
      line: { color: '#ef4444', width: 1.5, dash: 'dash' } },
    { x: [], y: [], type: 'scatter', mode: 'markers', name: 'Detections',
      marker: { color: '#f59e0b', size: 8, symbol: 'triangle-down' } },
], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Range (m)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: 'Magnitude (dB)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    showlegend: true,
    legend: { orientation: 'h', x: 0, y: 1.08 },
}), { displayModeBar: false, responsive: true });

// Range waterfall: x = range, y = frames ago (lab "Range Waterfall Plot").
Plotly.newPlot('chart-fmcw-wf', [{
    z: [[]], x: [], y: [], type: 'heatmap', colorscale: 'Viridis',
    colorbar: { title: 'dB', thickness: 12 },
    hovertemplate: 'R=%{x:.2f} m<br>t=-%{y}<br>%{z:.1f} dB<extra></extra>',
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Range (m)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: 'Time (frames ago)', gridcolor: getPlotPalette().gridColor, griddash: 'dash', autorange: 'reversed' },
}), { displayModeBar: false, responsive: true });

// Range-Doppler: x = velocity, y = range (lab "Range Doppler Plotting").
Plotly.newPlot('chart-fmcw-rd', [{
    z: [[]], x: [], y: [], type: 'heatmap', colorscale: 'Electric',
    colorbar: { title: 'dB', thickness: 12 },
    hovertemplate: 'v=%{x:.2f} m/s<br>R=%{y:.2f} m<br>%{z:.1f} dB<extra></extra>',
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Velocity (m/s)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
    yaxis: { title: 'Range (m)', gridcolor: getPlotPalette().gridColor, griddash: 'dash' },
}), { displayModeBar: false, responsive: true });

enableColorbarDrag('chart-fmcw-wf', 'wf');
enableColorbarDrag('chart-fmcw-rd', 'rd');

function refreshWaterfallScale() {
    if (state.autoscale) {
        Plotly.relayout('chart-waterfall', { 'zauto': true });
    } else {
        Plotly.relayout('chart-waterfall', { 'zmin': state.db_floor, 'zmax': state.db_ceil, 'zauto': false });
    }
    Plotly.relayout('chart-spectrum', { 'yaxis.range': [state.db_floor, state.db_ceil] });
    // FMCW plots keep their own scale (fmcw.span_db, from each frame's peak):
    // its levels sit tens of dB above CW's, so the CW floor/ceiling clipped
    // every target to white.
}
function refreshAxisRanges() {
    Plotly.relayout('chart-waterfall', { 'xaxis.range': [-state.vel_max, state.vel_max] });
    Plotly.relayout('chart-spectrum', { 'xaxis.range': [-state.vel_max, state.vel_max] });
}

/* --- Connection / status UI --- */
function setConnectionStatus(connected) {
    const dot = document.getElementById('connection-dot');
    const text = document.getElementById('connection-text');
    if (dot) {
        dot.classList.toggle('disconnected', !connected);
        dot.classList.toggle('connected', connected);
    }
    // Simulated data never reads as a live kit: amber, "Simulated".
    const sim = connected && resolveTransportMode() === 'sim';
    if (dot) dot.classList.toggle('warn', sim);
    if (text) {
        text.textContent = shuttingDown ? 'Shutting down...'
            : sim ? 'Simulated' : connected ? 'Connected' : 'Disconnected';
    }
    // An armed pill keeps its own tooltip: how to shut down.
    if (state.playback && text) {
        text.textContent = 'Playback';
        dot?.classList.add('warn');
    }
    const pill = document.getElementById('connection-pill');
    if (pill && state.playback) {
        pill.title = `Playing ${state.playback.name}: recorded data, not live.\n${state.playback.note || ''}`.trim();
    } else if (pill && !shutdownArmed) {
        pill.title = sim ? 'Simulated data from the in-browser simulator. No Phaser hardware is connected.' : '';
    }
}

/* --- Shutdown: hold the connection pill ---
   As in the beamforming UI (frontend/src/main.js): where the backend reports
   shutdown_available, a 2 s hold on the pill powers the Pi off. Releasing
   early aborts, so the gesture is its own confirmation. Where it is not
   available (a Pi installed before the grant, a dev box, the simulator) the
   pill stays a plain readout: no cursor, no tooltip, no listeners. Armed once:
   the grant is a file on the Pi and cannot change without a restart. */
const SHUTDOWN_HOLD_MS = 2000;  // keep in step with --hold-ms in style.css
let shutdownArmed = false;
let shuttingDown = false;

function wireHoldToCommit(el, holdMs, onCommit) {
    let holdTimer = null;
    let pointerActive = false;
    const fire = () => {
        el.classList.remove('holding');
        Promise.resolve(onCommit()).then((ok) => {
            if (ok === false) return;
            el.classList.add('flash');
            setTimeout(() => el.classList.remove('flash'), 260);
        });
    };
    const cancel = () => {
        if (holdTimer) { clearTimeout(holdTimer); holdTimer = null; }
        el.classList.remove('holding');
    };
    el.addEventListener('pointerdown', (e) => {
        if (!el.classList.contains('btn-hold')) return;
        if (e.button !== undefined && e.button !== 0) return;
        pointerActive = true;
        if (holdTimer) clearTimeout(holdTimer);
        el.classList.add('holding');
        holdTimer = setTimeout(() => { holdTimer = null; fire(); }, holdMs);
    });
    el.addEventListener('pointerup', () => { pointerActive = false; cancel(); });
    el.addEventListener('pointercancel', () => { pointerActive = false; cancel(); });
    el.addEventListener('pointerleave', () => { if (pointerActive) cancel(); pointerActive = false; });
    // A held key repeats rather than reporting a duration; reaching for the
    // keyboard is deliberate enough, so Enter/Space commits directly.
    el.addEventListener('keydown', (e) => {
        if ((e.key === 'Enter' || e.key === ' ') && el.classList.contains('btn-hold')) {
            e.preventDefault();
            fire();
        }
    });
}

function armShutdownIfPermitted(available) {
    if (!available || shutdownArmed) return;
    const pill = document.getElementById('connection-pill');
    if (!pill) return;
    shutdownArmed = true;
    pill.classList.add('btn-hold');
    pill.setAttribute('role', 'button');
    pill.setAttribute('tabindex', '0');
    pill.title = 'Hold to shut the Phaser down cleanly.';
    wireHoldToCommit(pill, SHUTDOWN_HOLD_MS, async () => {
        try {
            const resp = await transport.invoke('power_off', {});
            if (resp?.status === 'ok') {
                log('warn', 'POWER', 'Shutting down — the Pi is going away.');
                shuttingDown = true;
                // Nothing will answer after this: no more hold, and the
                // reconnect loop must not paint Disconnected over the message.
                pill.classList.remove('btn-hold');
                pill.title = '';
                setConnectionStatus(false);
                return true;
            }
            log('error', 'POWER', resp?.message || 'Shutdown refused');
        } catch (err) {
            log('error', 'POWER', `Could not shut down: ${err.message || err}`);
        }
        return false;
    });
}

function setRunningUi(running, mode = state.uiMode) {
    state.running = running;
    state.runningMode = running ? mode : null;
    updateFmcwCalModal();
    updateFmcwSaveButton();
    const btn = document.getElementById('btn-radar');
    if (btn) {
        btn.textContent = running ? 'Stop' : 'Start';
        btn.classList.toggle('btn-primary', !running);
        btn.classList.toggle('btn-danger', running);
        // FMCW on a kit that cannot run it: say why instead of failing.
        btn.disabled = !running && state.uiMode === 'fmcw' && state.fmcw.available === false;
    }
    // One mode at a time on the backend: switch only while idle.
    document.querySelectorAll('#mode-cw, #mode-fmcw').forEach(b => { b.disabled = running; });
}

/* --- CW / FMCW mode switch --- */
/* The Hardware pane follows the mode. Each FMCW preset is a lab with its own
   output frequency and gains, so in FMCW mode the pane shows the preset's
   and an edit overrides it; CW keeps its own values. Each mode's values are
   stashed on the way out and restored on the way back. */
const hwStash = { cw: null, fmcw: null };

function readHw() {
    return {
        output: document.getElementById('output-freq').value,
        rx: document.getElementById('val-rx-gain').value,
        tx: document.getElementById('val-tx-gain').value,
    };
}

function writeHw(v) {
    if (!v) return;
    document.getElementById('output-freq').value = v.output;
    for (const [k, id] of [['rx', 'rx-gain'], ['tx', 'tx-gain']]) {
        document.getElementById(id).value = v[k];
        document.getElementById('val-' + id).value = v[k];
    }
}

function hwParams() {
    const v = readHw();
    const p = {};
    const out = parseFloat(v.output), rx = parseFloat(v.rx), tx = parseFloat(v.tx);
    if (Number.isFinite(out)) p.output_freq = out * 1e9;
    if (Number.isFinite(rx)) p.rx_gain = rx;
    if (Number.isFinite(tx)) p.tx_gain = tx;
    return p;
}

// What the selected preset runs at -- or, while FMCW runs and `live` is set,
// what it is actually running, edits included.
function presetHw(live = true) {
    const cfg = live && state.runningMode === 'fmcw' ? state.fmcw.cfg : null;
    const src = cfg || state.fmcw.preset_info?.[fmcwEl('fmcw-preset')?.value];
    if (!src || src.output_freq == null) return null;
    return { output: String(Number((src.output_freq / 1e9).toFixed(6))),
             rx: String(src.rx_gain), tx: String(src.tx_gain) };
}

// Controls only CW reads: greyed in FMCW mode, with the reason on hover.
const CW_ONLY_ROWS = [
    ['sample-rate', 'CW only: the FMCW preset sets the sample rate.'],
    ['fft-size', 'CW only: the FMCW preset sets the capture size.'],
    ['signal-freq', 'CW only: the FMCW preset sets the IF tone.'],
    ['val-db-floor', 'CW only: FMCW uses Color Span.'],
    ['val-db-ceil', 'CW only: FMCW uses Color Span.'],
    ['opt-autoscale', 'CW only: FMCW uses Color Span.'],
];
function updateModeGating() {
    const fmcw = state.uiMode === 'fmcw';
    CW_ONLY_ROWS.forEach(([id, why]) =>
        gateRow(document.getElementById(id)?.closest('.set-row'), fmcw, why));
}

function setUiMode(mode) {
    const prev = state.uiMode;
    state.uiMode = mode === 'fmcw' ? 'fmcw' : 'cw';
    if (prev !== state.uiMode) {
        hwStash[prev] = readHw();
        writeHw(state.uiMode === 'fmcw' ? (hwStash.fmcw || presetHw()) : hwStash.cw);
    }
    updateModeGating();
    document.getElementById('mode-cw')?.classList.toggle('active', state.uiMode === 'cw');
    document.getElementById('mode-fmcw')?.classList.toggle('active', state.uiMode === 'fmcw');
    const modeTabs = document.querySelector('.mode-tabs');
    if (modeTabs) modeTabs.dataset.mode = state.uiMode;
    // Show the mode's tabs (and Logs), and land on its first one -- in FMCW,
    // the plot that suits the preset (fmcwDefaultTab).
    let first = null;
    document.querySelectorAll('.tab-btn').forEach(b => {
        const m = b.dataset.mode;
        b.hidden = Boolean(m) && m !== state.uiMode;
        if (!first && m === state.uiMode) first = b;
    });
    if (state.uiMode === 'fmcw') showFmcwDefaultTab({ force: true });
    else first?.click();
    // Always shown, greyed in CW, so the stats row does not shift.
    const rangeBox = document.getElementById('box-peak-range');
    rangeBox.classList.toggle('set-disabled', state.uiMode !== 'fmcw');
    rangeBox.title = state.uiMode === 'fmcw' ? '' : 'FMCW only: CW measures velocity, not range.';
    if (state.uiMode !== 'fmcw') document.getElementById('stat-peak-range').textContent = '-- m';
    // The FMCW settings open with FMCW; CW keeps the Hardware section.
    activateSidebarSection(state.uiMode === 'fmcw' ? 4 : 0);
    setRunningUi(state.running, state.runningMode || state.uiMode);
}
document.getElementById('mode-cw')?.addEventListener('click', () => setUiMode('cw'));
document.getElementById('mode-fmcw')?.addEventListener('click', () => setUiMode('fmcw'));

/* --- Frame rate, shared by both modes --- */
// Frames counted over the last second. An average of 1/dt between messages
// read high: WebSocket frames arrive in bunches, and each bunch's near-zero
// gaps counted as hundreds of fps (a 20 fps stream showed 30, phaser-01).
const frameTimes = [];
function noteFrameRate() {
    const now = performance.now();
    frameTimes.push(now);
    while (frameTimes.length && now - frameTimes[0] > 1000) frameTimes.shift();
    state.last_frame_t = now;
    if (frameTimes.length < 2) return;
    const span = (frameTimes[frameTimes.length - 1] - frameTimes[0]) / 1000;
    state.fps = span > 0 ? (frameTimes.length - 1) / span : 0;
    const el = document.getElementById('stat-rate');
    if (el) el.textContent = `${state.fps.toFixed(1)} fps`;
}

/* --- Frame handling --- */
function handleRadarFrame(frame) {
    // frame: { spectrum_db: [...], velocity_axis: [...], peak_velocity, peak_magnitude_db, ts }
    if (!frame || !frame.spectrum_db || !frame.velocity_axis) return;

    state.velocity_axis = frame.velocity_axis;

    // Update spectrum
    Plotly.restyle('chart-spectrum', { x: [frame.velocity_axis], y: [frame.spectrum_db] });

    // Update waterfall (push newest row at index 0)
    const depth = state.waterfall_depth;
    if (!state.waterfall || state.waterfall.length !== depth || (state.waterfall[0] && state.waterfall[0].length !== frame.spectrum_db.length)) {
        state.waterfall = [];
        for (let i = 0; i < depth; i++) state.waterfall.push(new Array(frame.spectrum_db.length).fill(state.db_floor));
    }
    state.waterfall.pop();
    state.waterfall.unshift(frame.spectrum_db);

    Plotly.restyle('chart-waterfall', {
        z: [state.waterfall],
        x: [frame.velocity_axis],
        y: [Array.from({ length: depth }, (_, i) => i)],
    });

    // Stats
    if (typeof frame.peak_velocity === 'number') {
        document.getElementById('stat-peak-vel').textContent = `${frame.peak_velocity.toFixed(2)} m/s`;
    }
    if (typeof frame.peak_magnitude_db === 'number') {
        document.getElementById('stat-peak-mag').textContent = `${frame.peak_magnitude_db.toFixed(1)} dB`;
    }

    noteFrameRate();

    // Time-domain (optional — only if backend includes a downsampled iq trace)
    if (frame.iq_mag && frame.iq_mag.length) {
        Plotly.restyle('chart-time', {
            x: [Array.from({ length: frame.iq_mag.length }, (_, i) => i)],
            y: [frame.iq_mag]
        });
    }
}

/* --- FMCW frames (C4) --- */

// The backend's u8-base64 map (phaser_radar_dsp.encode_db_u8) back to dB.
// Rows are range, columns velocity; dB = db_min + byte * (db_max - db_min) / 255.
function decodeDbU8(enc) {
    const bin = atob(enc.data);
    const scale = (enc.db_max - enc.db_min) / 255;
    const z = new Array(enc.rows);
    for (let r = 0; r < enc.rows; r++) {
        const row = new Array(enc.cols);
        const off = r * enc.cols;
        for (let c = 0; c < enc.cols; c++) row[c] = enc.db_min + bin.charCodeAt(off + c) * scale;
        z[r] = row;
    }
    return z;
}

/* A chart's color scale, {top, bottom} in dB: fixed, not rescaled to every
   frame (phaser-01, 2026-11-06: "the scale moves around like crazy"). Set
   once from the first frame after a Start, a new preset or a loaded
   recording -- that frame's peak rounded up to 5 dB, plus 5 dB of headroom,
   down Color Span -- then held. Drag the color bar's ends to move it; a
   double-click on the bar sets it afresh from the next frame. */
const SCALE_STEP_DB = 5;
function colorScale(key, peak) {
    let sc = state.fmcw.scales[key];
    if (!sc && Number.isFinite(peak)) {
        const top = Math.ceil(peak / SCALE_STEP_DB) * SCALE_STEP_DB + SCALE_STEP_DB;
        sc = state.fmcw.scales[key] = { top, bottom: top - state.fmcw.span_db };
    }
    return sc || { top: 0, bottom: -state.fmcw.span_db };
}

/* Drag a heatmap's color bar: the upper half moves the top, the lower half
   the bottom, 1 dB steps, at least 5 dB apart. Double-click: re-fit. */
function enableColorbarDrag(chartId, key) {
    const gd = document.getElementById(chartId);
    if (!gd) return;
    const barRect = () => (gd.querySelector('.colorbar .cbfills') || gd.querySelector('.colorbar'))?.getBoundingClientRect();
    const onBar = (e) => {
        const r = barRect();
        return r && e.clientX >= r.left - 12 && e.clientX <= r.right + 40 && e.clientY >= r.top && e.clientY <= r.bottom ? r : null;
    };
    gd.addEventListener('pointermove', (e) => {
        if (!gd._cbDrag) gd.style.cursor = onBar(e) ? 'ns-resize' : '';
    });
    gd.addEventListener('pointerdown', (e) => {
        const r = onBar(e);
        const sc = state.fmcw.scales[key];
        if (!r || !sc) return;
        e.preventDefault();
        e.stopPropagation();
        const end = e.clientY < r.top + r.height / 2 ? 'top' : 'bottom';
        const perPx = (sc.top - sc.bottom) / Math.max(r.height, 1);
        const y0 = e.clientY, v0 = sc[end];
        gd._cbDrag = true;
        const move = (ev) => {
            let v = Math.round(v0 - (ev.clientY - y0) * perPx);
            v = end === 'top' ? Math.max(v, sc.bottom + 5) : Math.min(v, sc.top - 5);
            if (v === sc[end]) return;
            sc[end] = v;
            Plotly.restyle(chartId, { zmin: [sc.bottom], zmax: [sc.top] });
        };
        const up = () => {
            gd._cbDrag = false;
            window.removeEventListener('pointermove', move);
            window.removeEventListener('pointerup', up);
        };
        window.addEventListener('pointermove', move);
        window.addEventListener('pointerup', up);
    }, true);
    gd.addEventListener('dblclick', (e) => {
        if (onBar(e)) delete state.fmcw.scales[key];
    }, true);
}

/* A new view: every chart sets its scale afresh from its next frame. */
function resetColorScales() { state.fmcw.scales = {}; }

function pushRangeWaterfall(rangeAxis, trace) {
    const depth = state.waterfall_depth;
    let wf = state.fmcw.waterfall;
    if (!wf || wf.length !== depth || (wf[0] && wf[0].length !== trace.length)) {
        const floor = Math.min(...trace);
        wf = [];
        for (let i = 0; i < depth; i++) wf.push(new Array(trace.length).fill(floor));
    }
    wf.pop();
    wf.unshift(trace);
    state.fmcw.waterfall = wf;
    const sc = colorScale('wf', Math.max(...trace));
    Plotly.restyle('chart-fmcw-wf', {
        z: [wf], x: [rangeAxis], y: [Array.from({ length: depth }, (_, i) => i)],
        zauto: false, zmin: sc.bottom, zmax: sc.top,
    });
}

// Frames are drawn at most once per display refresh, newest first: Plotly
// cannot redraw three charts at the rates a single-chirp preset can deliver,
// and a backlog of redraws delays every click behind it.
let fmcwPending = null;
function handleFmcwFrame(frame) {
    if (!frame || !frame.range_axis) return;
    state.fmcw.last = frame;
    if (frame.kind === 'range_profile') averageFmcwProfile(frame);
    else state.fmcw.avg = null;
    noteFrameRate();
    updateFmcwCalModal();
    const scheduled = fmcwPending !== null;
    fmcwPending = frame;
    if (!scheduled) {
        requestAnimationFrame(() => {
            const f = fmcwPending;
            fmcwPending = null;
            renderFmcwFrame(f);
        });
    }
}

function renderFmcwFrame(frame) {
    const range = frame.range_axis;
    let trace;

    if (frame.kind === 'range_doppler' && frame.map) {
        const z = decodeDbU8(frame.map);
        const sc = colorScale('rd', frame.map.db_max);
        Plotly.restyle('chart-fmcw-rd', {
            z: [z], x: [frame.velocity_axis], y: [range],
            zauto: false, zmin: sc.bottom, zmax: sc.top,
        });
        // The range plot shows the strongest return per range bin twice: over
        // every velocity, and over the moving ones only. The waterfall gets
        // the moving one, so a hand shows against the static clutter.
        // Skip the clutter's whole main lobe too, as the backend's peak
        // search does: at least 3 bins (Blackman) either side of 0 m/s.
        const vel = frame.velocity_axis || [];
        const dv = vel.length > 1 ? Math.abs(vel[1] - vel[0]) : 0;
        const vmin = Math.max(state.fmcw.moving_min_mps, 3 * dv) - 1e-9;
        const moving = [];
        vel.forEach((v, i) => { if (Math.abs(v) >= vmin) moving.push(i); });
        const all = z.map(row => Math.max(...row));
        trace = moving.length ? z.map(row => Math.max(...moving.map(i => row[i]))) : all;
        Plotly.restyle('chart-fmcw-range', {
            x: [range, range, [], []], y: [all, trace, [], []],
        });
    } else {
        trace = frame.profile_db || [];
        const thr = frame.cfar_threshold_db || [];
        const idx = frame.detection_indices || [];
        // "Apply CFAR Threshold" (lab step 4): everything at or below the
        // threshold goes to the trace's floor, in the plot and the waterfall.
        if (thr.length === trace.length && fmcwEl('fmcw-cfar-mode')?.value === 'detections') {
            const floor = Math.min(...trace);
            trace = trace.map((v, i) => (v > thr[i] ? v : floor));
        }
        Plotly.restyle('chart-fmcw-range', {
            x: [range, [], thr.length ? range : [], idx.map(i => range[i])],
            y: [trace, [], thr, idx.map(i => trace[i])],
        });
    }
    pushRangeWaterfall(range, trace);

    // At most 4 readout updates a second: a single-chirp preset runs at 20
    // fps, and a number that changes 20 times a second cannot be read.
    const now = performance.now();
    if (now - state.fmcw.readout_t < FMCW_READOUT_MS) return;
    state.fmcw.readout_t = now;
    const peak = fmcwPeak(frame);
    // null: nothing stood clear of the noise (the backend's peak_snr_db).
    document.getElementById('stat-peak-range').textContent =
        typeof peak.range === 'number' ? `${peak.range.toFixed(2)} m` : '-- m';
    document.getElementById('stat-peak-vel').textContent =
        typeof frame.peak_velocity === 'number' ? `${frame.peak_velocity.toFixed(2)} m/s` : '-- m/s';
    if (typeof peak.db === 'number') {
        document.getElementById('stat-peak-mag').textContent = `${peak.db.toFixed(1)} dB`;
    }
}

const FMCW_READOUT_MS = 250;
// Frames the single-chirp profile is averaged over for the readout, roughly:
// one chirp is noisy enough that its strongest bin wanders between the
// reflector, the leakage and the clutter from frame to frame (phaser-01,
// 2026-11-02: "peak range moving all over the place"). A range-Doppler frame
// already integrates 256 chirps and is read as it comes.
const FMCW_PROFILE_AVG_FRAMES = 10;

/* Average a range_profile frame's power into state.fmcw.avg. Restarts when
   the axis changes -- a new preset, range crop or 0 m calibration. */
function averageFmcwProfile(frame) {
    const axis = frame.range_axis || [], prof = frame.profile_db || [];
    const key = `${axis.length}:${axis[0]}:${axis[axis.length - 1]}`;
    const avg = state.fmcw.avg;
    const power = prof.map(db => 10 ** (db / 10));
    if (!avg || avg.key !== key) {
        state.fmcw.avg = { key, power, n: 1 };
        return;
    }
    avg.n = Math.min(avg.n + 1, FMCW_PROFILE_AVG_FRAMES);
    const a = 1 / avg.n;
    for (let i = 0; i < power.length; i++) avg.power[i] += a * (power[i] - avg.power[i]);
}

/* The peak the readouts show: the averaged profile's on single-chirp
   presets, the frame's own on range-Doppler. `cal` asks for the calibration
   pop-up's strongest return, which on range-Doppler includes 0 m/s. */
function fmcwPeak(frame, { cal = false } = {}) {
    if (!frame) return {};
    // Calibrate: the backend's strongest return over the whole band, which
    // the displayed (and averaged) profile may not even contain -- below
    // 0 m, or past Range Max, when the current 0 m beat is off.
    if (cal && typeof frame.cal_peak_range === 'number') {
        return { range: frame.cal_peak_range, db: frame.peak_magnitude_db };
    }
    const avg = state.fmcw.avg;
    if (frame.kind === 'range_profile' && avg && avg.power.length === frame.range_axis.length) {
        // As the backend's readout: skip the leakage closer than Peak Min.
        // Half a bin of slack, as range_profile: a target at the cutoff counts.
        const axis = frame.range_axis;
        const min = (Number(frame.peak_min) || 0) - (axis.length > 1 ? 0.5 * Math.abs(axis[1] - axis[0]) : 0);
        let best = -1;
        for (let i = 0; i < avg.power.length; i++) {
            if (axis[i] >= min && (best < 0 || avg.power[i] > avg.power[best])) best = i;
        }
        if (best < 0) best = avg.power.indexOf(Math.max(...avg.power));
        return { range: axis[best], db: 10 * Math.log10(avg.power[best]) };
    }
    return { range: frame.peak_range, db: frame.peak_magnitude_db };
}

/* --- FMCW controls --- */
const fmcwEl = (id) => document.getElementById(id);

function readFmcwParams() {
    const params = {
        preset: fmcwEl('fmcw-preset').value || undefined,
        // Simulation only; a kit has no scene to pick.
        scene: state.fmcw.scenes ? (fmcwEl('fmcw-scene')?.value || undefined) : undefined,
        scene_noise: state.fmcw.sceneNoise ? (fmcwEl('fmcw-scene-noise')?.value || undefined) : undefined,
        // A switch plus a mode in the UI; one value on the backend.
        mti: fmcwEl('fmcw-mti-on').checked ? fmcwEl('fmcw-mti-mode').value : 'none',
        cfar: fmcwEl('fmcw-cfar').checked,
        cfar_guard: parseInt(fmcwEl('fmcw-cfar-guard').value, 10),
        cfar_ref: parseInt(fmcwEl('fmcw-cfar-ref').value, 10),
        cfar_bias_db: parseFloat(fmcwEl('fmcw-cfar-bias').value),
        taper: state.taper,
    };
    // Empty fields mean "the preset's own" / "the IF tone": null, so a value
    // sent on an earlier Start does not linger on the backend.
    const rmax = parseFloat(fmcwEl('fmcw-range-max').value);
    params.range_max = Number.isFinite(rmax) && rmax > 0 ? rmax : null;
    const pmin = parseFloat(fmcwEl('fmcw-peak-min')?.value);
    params.peak_min = Number.isFinite(pmin) && pmin >= 0 ? pmin : null;
    const zero = parseFloat(fmcwEl('fmcw-zero-khz').value);
    params.zero_range_freq = Number.isFinite(zero) ? zero * 1e3 : null;
    const center = parseFloat(document.getElementById('center-freq').value);
    params.center_freq = Number.isFinite(center) ? center * 1e9 : null;
    // The Hardware pane follows the preset in FMCW mode, so this is the lab's
    // own frequency and gains unless they were edited; FFT Window is shared.
    Object.assign(params, hwParams());
    params.fft_window = document.getElementById('fft-window').value;
    return params;
}

async function refreshFmcwState() {
    try {
        const resp = await transport.getFmcwRadarState();
        const d = resp.data || {};
        state.fmcw.cfg = d.cfg || null;
        state.fmcw.plan = d.plan || null;
        const cap = d.capability || {};
        state.fmcw.available = Boolean(cap.available);
        const capEl = fmcwEl('fmcw-capability');
        if (capEl) {
            capEl.classList.toggle('ok', state.fmcw.available && !cap.reason);
            capEl.classList.toggle('unavailable', !state.fmcw.available);
            // One short line; the detail is in the tooltip.
            capEl.textContent = !state.fmcw.available ? 'FMCW unavailable'
                : cap.reason ? 'FMCW limited' : 'FMCW ready';
            capEl.title = !state.fmcw.available ? (cap.reason || 'unknown reason')
                : cap.reason ? cap.reason
                : `${cap.ramp}${cap.source === 'sim' ? ', simulated' : ''}`;
        }
        state.fmcw.preset_info = d.preset_info || {};
        state.fmcw.capture = d.capture || null;
        updateFmcwSaveButton();
        // Scenes: only a simulator reports them. A live, moving scene is the
        // default pick here; the backend's own default is the static one.
        state.fmcw.scenes = d.scenes || null;
        const sceneRow = fmcwEl('fmcw-scene-row'), sceneSel = fmcwEl('fmcw-scene');
        if (sceneRow && sceneSel) {
            sceneRow.hidden = !state.fmcw.scenes;
            if (state.fmcw.scenes && sceneSel.options.length !== Object.keys(state.fmcw.scenes).length) {
                sceneSel.innerHTML = '';
                for (const [name, label] of Object.entries(state.fmcw.scenes)) {
                    const short = { static: 'Static', corners: 'Corners', walker: 'Walker', hand: 'Hand Wave' }[name] || name;
                    const opt = new Option(short, name);
                    opt.title = label;
                    sceneSel.add(opt);
                }
                sceneSel.value = d.running && d.scene ? d.scene
                    : (FMCW_UI_DEFAULT_SCENE in state.fmcw.scenes ? FMCW_UI_DEFAULT_SCENE : d.scene);
            }
            sceneSel.title = state.fmcw.scenes?.[sceneSel.value] || '';
        }
        state.fmcw.sceneNoise = d.scene_noise_levels || null;
        const noiseRow = fmcwEl('fmcw-scene-noise-row'), noiseSel = fmcwEl('fmcw-scene-noise');
        if (noiseRow && noiseSel) {
            const levels = state.fmcw.sceneNoise;
            noiseRow.hidden = !levels;
            if (levels && noiseSel.options.length !== Object.keys(levels).length) {
                noiseSel.innerHTML = '';
                for (const [name, label] of Object.entries(levels)) {
                    const opt = new Option(name[0].toUpperCase() + name.slice(1), name);
                    opt.title = label;
                    noiseSel.add(opt);
                }
                noiseSel.value = d.running && d.scene_noise ? d.scene_noise
                    : (FMCW_UI_DEFAULT_SCENE_NOISE in levels ? FMCW_UI_DEFAULT_SCENE_NOISE : d.scene_noise);
            }
        }
        const sel = fmcwEl('fmcw-preset');
        if (sel && Array.isArray(d.presets) && sel.options.length !== d.presets.length) {
            const keep = sel.value || d.preset;
            sel.innerHTML = '';
            // Short label in the box; the internal name stays the value.
            d.presets.forEach(p => sel.add(new Option(d.preset_info?.[p]?.label || p, p)));
            sel.value = d.presets.includes(keep) ? keep : d.presets[0];
            // The preset is known only now: land on its plot.
            if (state.uiMode === 'fmcw') showFmcwDefaultTab();
        }
        updateFmcwControlGating();
        setRunningUi(state.running, state.runningMode || state.uiMode);
        return d;
    } catch (e) {
        log('error', 'FMCW', `get_fmcw_radar_state failed: ${e.message || e}`);
        return null;
    }
}

// On (re)connect, show what the backend is actually doing: a page reloaded,
// or opened in a second tab, while a mode runs used to offer "Start".
async function syncRunningFromBackend() {
    if (state.playback) return;
    const fmcw = await refreshFmcwState();
    if (fmcw?.running) {
        // Running first, so the Hardware pane picks up the running config.
        setRunningUi(true, 'fmcw');
        setUiMode('fmcw');
        return;
    }
    try {
        const cw = await transport.getCwRadarState();
        if (cw?.data?.running) {
            setUiMode('cw');
            setRunningUi(true, 'cw');
        }
    } catch (e) {
        log('error', 'CW', `get_cw_radar_state failed: ${e.message || e}`);
    }
}

// Processing params apply on the next frame; preset changes make the backend
// leave and re-enter the mode itself, after which the plan is refetched.
async function pushFmcwParams(partial) {
    if (state.runningMode !== 'fmcw') return;
    if (state.playback) {
        // A recording's waveform is fixed; its processing follows the pane.
        state.playback.worker.postMessage({ cmd: 'settings', settings: partial });
        if (state.fmcw.cfg) Object.assign(state.fmcw.cfg, partial);
        return;
    }
    try {
        const resp = await transport.setFmcwRadarParams(partial);
        if (resp.status !== 'ok') {
            log('error', 'FMCW', `set_fmcw_radar_params: ${resp.message || JSON.stringify(resp)}`);
            return;
        }
        if (resp.reentered) {
            state.fmcw.waterfall = null;
            await refreshFmcwState();
        } else if (state.fmcw.cfg) {
            Object.assign(state.fmcw.cfg, partial);
        }
    } catch (e) {
        log('error', 'FMCW', `set_fmcw_radar_params failed: ${e.message || e}`);
    }
}

// MTI cancels between chirps, so it needs a multi-chirp preset; CFAR runs on
// the single-chirp range profile, as the lab's CFAR script does. Each used to
// sit there doing nothing silently on the other kind of preset.
// Grey a settings row out with its reason as the tooltip; restore its own
// tooltip (kept in data-tip) when it applies again.
function gateRow(row, off, why) {
    if (!row) return;
    if (row.dataset.tip === undefined) row.dataset.tip = row.title || '';
    row.classList.toggle('set-disabled', off);
    row.title = off ? why : row.dataset.tip;
    row.querySelectorAll('input, select').forEach(el => { el.disabled = off; });
}

function updateFmcwControlGating() {
    const preset = fmcwEl('fmcw-preset')?.value;
    // A recording plays as range-Doppler frames or single-chirp profiles.
    const n = state.playback ? state.playback.info.window : state.fmcw.preset_info?.[preset]?.num_chirps;
    const presetRow = fmcwEl('fmcw-preset')?.closest('.set-row');
    gateRow(presetRow, Boolean(state.playback), 'Playback: the recording sets the waveform.');
    gateRow(fmcwEl('fmcw-scene-row'), Boolean(state.playback), 'Playback: the recording is the scene.');
    const staticScene = fmcwEl('fmcw-scene')?.value === 'static';
    gateRow(fmcwEl('fmcw-scene-noise-row'), Boolean(state.playback) || staticScene,
            state.playback ? 'Playback: the recording has its own noise.'
                : 'The static scene has its own fixed noise: pick a live scene.');
    const noiseSel = fmcwEl('fmcw-scene-noise');
    if (noiseSel && !noiseSel.disabled) noiseSel.title = state.fmcw.sceneNoise?.[noiseSel.value] || '';
    if (!n) return;  // not known yet
    const multi = n > 1;
    const gate = gateRow;
    // Each section is a switch and a mode: the preset decides whether the
    // section applies at all, and the switch whether its mode can be chosen.
    const noMti = 'MTI cancels between chirps: it needs a multi-chirp preset (Range + Velocity).';
    const mtiOn = fmcwEl('fmcw-mti-on')?.checked;
    gate(fmcwEl('fmcw-mti-on')?.closest('.set-row'), !multi, noMti);
    gate(fmcwEl('fmcw-mti-mode')?.closest('.set-row'), !multi || !mtiOn,
         !multi ? noMti : 'Turn MTI on to choose its mode.');
    gate(fmcwEl('fmcw-peak-min-row'), multi,
         'Range-Doppler skips the 0 m leakage by velocity (it sits at 0 m/s): Peak Min is for the Range presets.');
    const noCfar = 'CFAR runs on the single-chirp range profile: pick a Range preset.';
    const cfarOn = fmcwEl('fmcw-cfar')?.checked;
    ['fmcw-cfar', 'fmcw-cfar-guard', 'fmcw-cfar-ref', 'fmcw-cfar-bias']
        .forEach(id => gate(fmcwEl(id)?.closest('.set-row'), multi, noCfar));
    gate(fmcwEl('fmcw-cfar-mode')?.closest('.set-row'), multi || !cfarOn,
         multi ? noCfar : 'Turn CFAR on to choose its mode.');
    // What the preset is for, on hover rather than under the box.
    const sel = fmcwEl('fmcw-preset');
    if (sel) sel.title = state.fmcw.preset_info?.[preset]?.hint || '';
}

// The scene a simulator starts in: someone walking, so a first look shows
// motion rather than a frozen picture.
const FMCW_UI_DEFAULT_SCENE = 'walker';
// And a room that looks like a room: clutter, fading and a kit-like floor.
const FMCW_UI_DEFAULT_SCENE_NOISE = 'typical';

fmcwEl('fmcw-scene')?.addEventListener('change', (e) => {
    e.target.title = state.fmcw.scenes?.[e.target.value] || '';
    state.fmcw.waterfall = null;
    state.fmcw.avg = null;
    pushFmcwParams({ scene: e.target.value });
    updateFmcwControlGating();
});

fmcwEl('fmcw-scene-noise')?.addEventListener('change', (e) => {
    e.target.title = state.fmcw.sceneNoise?.[e.target.value] || '';
    state.fmcw.waterfall = null;
    state.fmcw.avg = null;
    pushFmcwParams({ scene_noise: e.target.value });
});

fmcwEl('fmcw-preset')?.addEventListener('change', (e) => {
    resetColorScales();
    updateFmcwControlGating();
    showFmcwDefaultTab();
    // The lab's own frequency and gains, sent along: an override from an
    // earlier preset would otherwise outlive the switch in the stored params.
    const hw = presetHw(false);
    if (hw && state.uiMode === 'fmcw') writeHw(hw);
    else hwStash.fmcw = null;
    pushFmcwParams({ preset: e.target.value, ...(hw ? hwParams() : {}) });
});
['fmcw-mti-on', 'fmcw-mti-mode'].forEach(id => fmcwEl(id)?.addEventListener('change', () => {
    updateFmcwControlGating();
    pushFmcwParams({ mti: readFmcwParams().mti });
}));
// Show Threshold / Detections Only is drawn in the browser: nothing to send.
fmcwEl('fmcw-cfar-mode')?.addEventListener('change', () => { state.fmcw.waterfall = null; });
fmcwEl('fmcw-range-max')?.addEventListener('change', () => {
    state.fmcw.waterfall = null;
    pushFmcwParams({ range_max: readFmcwParams().range_max });
});
fmcwEl('fmcw-peak-min')?.addEventListener('change', () => {
    pushFmcwParams({ peak_min: readFmcwParams().peak_min });
});
['fmcw-cfar', 'fmcw-cfar-guard', 'fmcw-cfar-ref', 'fmcw-cfar-bias'].forEach(id => {
    fmcwEl(id)?.addEventListener('change', () => {
        if (id === 'fmcw-cfar') updateFmcwControlGating();
        const p = readFmcwParams();
        pushFmcwParams({ cfar: p.cfar, cfar_guard: p.cfar_guard, cfar_ref: p.cfar_ref,
                         cfar_bias_db: p.cfar_bias_db });
    });
});
fmcwEl('fmcw-span')?.addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (!(Number.isFinite(v) && v > 0)) return;
    state.fmcw.span_db = v;
    // Keep each chart's top; move its bottom to the new span.
    for (const sc of Object.values(state.fmcw.scales)) sc.bottom = sc.top - v;
});
fmcwEl('fmcw-zero-khz')?.addEventListener('change', () => {
    pushFmcwParams({ zero_range_freq: readFmcwParams().zero_range_freq });
});

// Lab step 21: hold the reflector at the array, and its return becomes 0 m.
// The frame's cal_peak_range -- the strongest cell over every velocity, so a
// still reflector counts on range-Doppler presets, where the peak readout
// skips 0 m/s -- was computed against the current 0 m beat, so the new one is
// that plus the beat the range stands for: f = 2 S R / c, with S the slope
// over the ramp time the plan actually uses.
const fmcwCalModal = document.getElementById('fmcw-cal-modal');

// The pop-up's main button always does the next step: Start FMCW, then Turn
// MTI Off, then Calibrate -- so it can be opened at any point and never has to
// be closed to fix something first. MTI only matters on a multi-chirp
// preset: it cancels a still reflector there.
let fmcwCalBusy = false;

function fmcwMtiInTheWay() {
    if (!fmcwEl('fmcw-mti-on')?.checked) return false;
    const f = state.fmcw.last;
    if (state.runningMode === 'fmcw' && f) return f.kind === 'range_doppler';
    const info = state.fmcw.preset_info?.[fmcwEl('fmcw-preset')?.value];
    return (info?.num_chirps ?? 1) > 1;
}

function fmcwCalProposal() {
    if (state.runningMode !== 'fmcw') {
        if (state.fmcw.available === false) {
            return { action: 'start', disabled: true, why: 'FMCW is not available on this kit.' };
        }
        return { action: 'start',
                 why: fmcwMtiInTheWay() ? 'Starts FMCW with MTI off.' : 'FMCW is stopped.' };
    }
    const f = state.fmcw.last, cfg = state.fmcw.cfg, plan = state.fmcw.plan;
    const current = cfg ? (cfg.zero_range_freq ?? cfg.signal_freq) : undefined;
    if (fmcwMtiInTheWay()) {
        return { action: 'mti', current, why: 'MTI cancels a still reflector.' };
    }
    const r = f && fmcwPeak(f, { cal: true }).range;
    if (!cfg || !plan || typeof r !== 'number') {
        return { action: 'set', disabled: true, current, why: 'Waiting for a frame...' };
    }
    const slope = cfg.chirp_bw / plan.effective_ramp_time;
    return { action: 'set', current, range: r, zero: current + 2 * slope * r / C_LIGHT };
}

const FMCW_CAL_ACTION = { start: 'Start FMCW', mti: 'Turn MTI Off', set: 'Calibrate' };

function updateFmcwCalModal() {
    const fmcwCalModal = document.getElementById('fmcw-cal-modal');
    if (!fmcwCalModal || fmcwCalModal.hidden) return;
    const p = fmcwCalProposal();
    const khz = (hz) => (typeof hz === 'number' ? `${(hz / 1e3).toFixed(2)} kHz` : '--');
    document.getElementById('fmcw-cal-peak').textContent =
        typeof p.range === 'number' ? `${p.range.toFixed(2)} m` : '--';
    document.getElementById('fmcw-cal-current').textContent =
        khz(p.current) + (state.fmcw.cfg && state.fmcw.cfg.zero_range_freq == null && p.current != null
            ? (state.playback ? ' (recording)' : ' (IF tone)') : '');
    document.getElementById('fmcw-cal-new').textContent = khz(p.zero);
    document.getElementById('fmcw-cal-hint').textContent = p.why || '';
    const btn = document.getElementById('fmcw-cal-set');
    btn.textContent = FMCW_CAL_ACTION[p.action];
    btn.disabled = fmcwCalBusy || !!p.disabled;
    btn.title = p.action === 'set' && !p.disabled ? 'Make the strongest return 0 m' : (p.why || '');
}

function openFmcwCal() {
    fmcwCalModal.hidden = false;
    updateFmcwCalModal();
    document.getElementById('fmcw-cal-cancel').focus();
}
function closeFmcwCal() { fmcwCalModal.hidden = true; }

fmcwEl('fmcw-cal-open')?.addEventListener('click', openFmcwCal);
document.getElementById('fmcw-cal-cancel')?.addEventListener('click', closeFmcwCal);
fmcwCalModal?.addEventListener('click', (e) => { if (e.target === fmcwCalModal) closeFmcwCal(); });
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && fmcwCalModal && !fmcwCalModal.hidden) closeFmcwCal();
});
document.getElementById('fmcw-cal-set')?.addEventListener('click', async () => {
    const p = fmcwCalProposal();
    if (p.disabled || fmcwCalBusy) return;
    if (p.action === 'start' || p.action === 'mti') {
        if (fmcwMtiInTheWay()) {
            fmcwEl('fmcw-mti-on').checked = false;
            fmcwEl('fmcw-mti-on').dispatchEvent(new Event('change'));
            log('info', 'FMCW', 'MTI turned off for the 0 m calibration');
        }
        if (p.action === 'start') {
            fmcwCalBusy = true;
            updateFmcwCalModal();
            try { await startRadar(); } finally { fmcwCalBusy = false; }
        }
        updateFmcwCalModal();
        return;
    }
    fmcwEl('fmcw-zero-khz').value = (p.zero / 1e3).toFixed(2);
    pushFmcwParams({ zero_range_freq: p.zero });
    log('info', 'FMCW', `0 m set to ${(p.zero / 1e3).toFixed(2)} kHz (return was at ${p.range.toFixed(2)} m)`);
    closeFmcwCal();
});
document.getElementById('fmcw-cal-reset')?.addEventListener('click', () => {
    fmcwEl('fmcw-zero-khz').value = '';
    pushFmcwParams({ zero_range_freq: null });
    log('info', 'FMCW', '0 m reset to the IF tone');
    closeFmcwCal();
});

/* --- Save Capture: the course .npz, downloaded to this laptop ---
   The backend (or the in-browser simulator) captures raw chirps and packs
   them in the ECE 444 capture format -- iq, optionally iq_ch, fs, fc, B,
   T_ramp, PRI, f0 -- that the labs' radar_lab.load_capture reads. One burst on
   a range-Doppler preset, 32 single chirps on the others. */
let fmcwSaving = false;

function formatBytes(n) {
    if (n >= 1e6) return `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} MB`;
    return `${Math.max(1, Math.round(n / 1e3))} kB`;
}

/* The Duration box: what this preset can save (get_fmcw_radar_state's
   capture limits), and the file size it will make -- on hover, and checked
   before anything is sent. Empty means the preset's default. */
function captureDuration() {
    const cap = state.fmcw.capture;
    const raw = document.getElementById('fmcw-save-dur')?.value;
    const d = raw === '' || raw == null ? cap?.default_s : parseFloat(raw);
    return { cap, d, ok: Boolean(cap) && d > 0 && d <= cap.max_s * (1 + 1e-9) };
}

function updateCaptureEstimate() {
    const row = document.getElementById('fmcw-save-dur-row');
    const input = document.getElementById('fmcw-save-dur');
    const { cap, d, ok } = captureDuration();
    if (!row || !input) return '';
    if (!cap) {
        row.title = 'Start FMCW to see what this preset can save.';
        return '';
    }
    input.placeholder = String(+cap.default_s.toPrecision(3));
    input.max = String(cap.max_s);
    input.classList.toggle('set-invalid', !ok);
    const ms = (t) => +(t * 1e3).toPrecision(3);
    const what = cap.kind === 'burst'
        ? `One continuous burst, chirps ${ms(cap.chirp_s)} ms apart, up to ${+cap.max_s.toPrecision(3)} s on this preset (the Pluto's burst buffer).`
        : cap.kind === 'bursts'
            ? `Up to ${ms(cap.burst_s)} ms: one continuous burst. Longer: a run of bursts, one every ${ms(cap.run_s)} ms, `
              + `each played back as a Range-Doppler frame; up to ${+cap.max_s.toPrecision(3)} s on this preset.`
            : `Single chirps every ${ms(cap.chirp_s)} ms, up to ${cap.max_s} s.`;
    const withCh = document.getElementById('fmcw-save-ch')?.checked;
    const size = ok ? `About ${formatBytes(captureBytes(cap, d))}, or ${formatBytes(captureBytes(cap, d, true))} with Subarray Channels${withCh ? ' (on)' : ''}.`
        : `${+d.toPrecision(3)} s is too long: at most ${+cap.max_s.toPrecision(3)} s here.`;
    row.title = `${what} Empty: ${+cap.default_s.toPrecision(3)} s. ${size}`;
    return ok ? `${+d.toPrecision(3)} s, about ${formatBytes(captureBytes(cap, d, withCh))}` : size;
}

function updateFmcwSaveButton() {
    const btn = document.getElementById('fmcw-save');
    if (!btn) return;
    const ready = state.runningMode === 'fmcw' && !state.playback;
    const est = updateCaptureEstimate();
    const { ok } = captureDuration();
    btn.disabled = !ready || fmcwSaving || (ready && !ok);
    btn.title = state.playback ? 'Saving is for live captures; this is a recording already'
        : !ready ? 'Start FMCW to save a capture'
        : !ok ? est
        : `Save raw chirps as a course .npz (iq, fs, fc, B, T_ramp, PRI, f0), downloaded to this computer: ${est}`;
}
document.getElementById('fmcw-save-dur')?.addEventListener('input', updateFmcwSaveButton);
document.getElementById('fmcw-save-ch')?.addEventListener('change', updateFmcwSaveButton);

function captureFilename(preset) {
    const d = new Date();
    const p2 = (n) => String(n).padStart(2, '0');
    const stamp = `${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}${p2(d.getSeconds())}`;
    return `phaser_${preset || 'fmcw'}_${stamp}.npz`;
}

document.getElementById('fmcw-save')?.addEventListener('click', async () => {
    const btn = document.getElementById('fmcw-save');
    fmcwSaving = true;
    updateFmcwSaveButton();
    btn.textContent = 'Saving...';
    try {
        const { d } = captureDuration();
        const resp = await transport.invoke('save_fmcw_capture', {
            iq_ch: Boolean(document.getElementById('fmcw-save-ch')?.checked),
            duration_s: d,
        }, 60000 + 2000 * (d || 0));
        if (resp.status !== 'ok') throw new Error(resp.message || 'capture failed');
        const name = captureFilename(resp.preset);
        const url = URL.createObjectURL(new Blob([fromBase64(resp.data)], { type: 'application/octet-stream' }));
        const a = document.createElement('a');
        a.href = url;
        a.download = name;
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 10000);
        log('info', 'FMCW', `Saved ${name}: ${resp.n_chirps} x ${resp.n_samples}${resp.iq_ch ? ' with iq_ch' : ''}, ${(resp.size / 1e6).toFixed(2)} MB`);
    } catch (e) {
        log('error', 'FMCW', `Save failed: ${e.message || e}`);
    } finally {
        fmcwSaving = false;
        btn.textContent = 'Save Capture';
        updateFmcwSaveButton();
    }
});

/* --- Load Recording: play a course .npz through the FMCW views ---
   The recording's own fs, B, T_ramp, PRI, fc and f0 set the waveform; the
   pane's processing controls apply to it as they do live (playback-worker.js
   runs the backend's processing, ported). Everything happens in the browser,
   so it works with no kit and offline. Stop ends it. */
function playbackSettings() {
    const p = readFmcwParams();
    return {
        mti: p.mti, cfar: p.cfar, cfar_guard: p.cfar_guard, cfar_ref: p.cfar_ref,
        cfar_bias_db: p.cfar_bias_db, range_max: p.range_max, peak_min: p.peak_min,
        zero_range_freq: p.zero_range_freq,
        fft_window: document.getElementById('fft-window').value,
    };
}

async function startPlayback(file) {
    resetColorScales();
    let arrays;
    try {
        arrays = await readNpz(await file.arrayBuffer());
    } catch (e) {
        log('error', 'PLAYBACK', `${file.name}: ${e.message || e}`);
        return;
    }
    // Leave whatever was live: the views show one source at a time.
    if (state.playback) stopPlayback({ quiet: true });
    if (state.running) {
        try {
            if (state.runningMode === 'fmcw') await transport.stopFmcwRadar();
            else await transport.stopCwRadar();
        } catch (e) { /* the backend may be gone; playback needs none */ }
        setRunningUi(false);
    }
    if (state.uiMode !== 'fmcw') setUiMode('fmcw');

    const worker = new Worker(new URL('./playback-worker.js', import.meta.url), { type: 'module' });
    const note = typeof arrays.note?.data === 'string' ? arrays.note.data : '';
    const pb = { worker, name: file.name, note, info: null };
    worker.onmessage = (event) => {
        const msg = event.data || {};
        if (state.playback !== pb) return;
        if (msg.type === 'fmcw_radar_frame') { handleFmcwFrame(msg.data); return; }
        if (msg.type === 'loaded') {
            pb.info = msg.info;
            if (state.uiMode === 'fmcw') showFmcwDefaultTab();
            const i = msg.info;
            state.fmcw.cfg = {
                chirp_bw: i.B, signal_freq: i.f0, output_freq: i.fc,
                zero_range_freq: playbackSettings().zero_range_freq,
            };
            state.fmcw.plan = { effective_ramp_time: i.T_ramp, ramp_time: i.T_ramp, num_chirps: i.window };
            updateFmcwControlGating();
            log('info', 'PLAYBACK', `${file.name}: ${i.chirps} chirps x ${i.samples} samples, `
                + `${i.bursts ? `${i.bursts} bursts, one every ${Math.round(i.interval)} ms` : i.mode === 'burst' ? 'range-Doppler' : 'range profiles'}, `
                + `fs ${i.fs / 1e6} MHz, B ${i.B / 1e6} MHz, T_ramp ${+(i.T_ramp * 1e6).toFixed(1)} us, PRI ${+(i.PRI * 1e3).toPrecision(4)} ms, f0 ${i.f0} Hz`);
            if (note) log('info', 'PLAYBACK', note);
            if (i.units === 'counts') {
                log('warn', 'PLAYBACK', `${file.name}: peak |iq| is above full scale, so it holds raw ADC counts `
                    + '(a save from before captures were stored in full-scale units); played as counts.');
            }
            return;
        }
        if (msg.type === 'error') {
            log('error', 'PLAYBACK', `${file.name}: ${msg.message}`);
            if (msg.fatal) stopPlayback({ quiet: true });
        }
    };
    state.playback = pb;
    state.fmcw.waterfall = null;
    state.fmcw.last = null;
    state.fmcw.avg = null;
    setRunningUi(true, 'fmcw');
    setConnectionStatus(true);
    setPlaybackPaused(false);
    worker.postMessage({ cmd: 'load', arrays, settings: playbackSettings() });
}

/* Play/Pause beside Stop, shown only while a recording plays. Paused, the
   frame stays on screen and the processing controls still redraw it. */
function setPlaybackPaused(paused) {
    const btn = document.getElementById('btn-playback-pause');
    if (state.playback) state.playback.paused = paused;
    if (!btn) return;
    btn.hidden = !state.playback;
    btn.textContent = paused ? 'Play' : 'Pause';
    // No frames arrive while paused: say so rather than show the last rate.
    if (paused) {
        frameTimes.length = 0;
        const rate = document.getElementById('stat-rate');
        if (rate) rate.textContent = 'paused';
    }
    btn.title = paused ? 'Play the recording on from this frame' : 'Pause the recording on this frame';
}
document.getElementById('btn-playback-pause')?.addEventListener('click', () => {
    const pb = state.playback;
    if (!pb) return;
    const paused = !pb.paused;
    pb.worker.postMessage({ cmd: paused ? 'pause' : 'resume' });
    setPlaybackPaused(paused);
});

function stopPlayback({ quiet = false } = {}) {
    const pb = state.playback;
    if (!pb) return;
    pb.worker.postMessage({ cmd: 'stop' });
    pb.worker.terminate();
    state.playback = null;
    setPlaybackPaused(false);
    setRunningUi(false);
    setConnectionStatus(transport.isConnected);
    updateFmcwControlGating();
    refreshFmcwState();
    if (!quiet) log('info', 'PLAYBACK', `Stopped ${pb.name}`);
}

document.getElementById('fmcw-load')?.addEventListener('click', () => {
    document.getElementById('fmcw-load-file')?.click();
});
document.getElementById('fmcw-load-file')?.addEventListener('change', (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';           // the same file can be loaded again
    if (file) startPlayback(file);
});

/* --- Param plumbing --- */
function readParams() {
    const params = {
        sample_rate: parseFloat(document.getElementById('sample-rate').value) * 1e3,
        fft_size: parseInt(document.getElementById('fft-size').value, 10),
        signal_freq: parseFloat(document.getElementById('signal-freq').value) * 1e3,
        output_freq: parseFloat(document.getElementById('output-freq').value) * 1e9,
        rx_gain: parseFloat(document.getElementById('rx-gain').value),
        tx_gain: parseFloat(document.getElementById('tx-gain').value),
        fft_window: document.getElementById('fft-window').value,
        taper: state.taper,
        vel_max: state.vel_max,
    };
    // Empty means "this kit's IF": null, not omitted, so the backend drops a
    // value an earlier Start left in its stored params and uses its own.
    const center = parseFloat(document.getElementById('center-freq').value);
    params.center_freq = Number.isFinite(center) ? center * 1e9 : null;
    return params;
}

/* Show this kit's measured IF in the Center Freq field. Kits differ by ~200
   MHz, so a fixed default leaves some deaf. A value the user typed stays. */
function showKitIf(backendState) {
    const field = document.getElementById('center-freq');
    // The state push wraps get_state(), which nests its fields under `data`.
    const ifHz = Number(backendState?.data?.Rx_freq ?? backendState?.Rx_freq);
    if (field && field.value === '' && Number.isFinite(ifHz) && ifHz > 0) {
        field.value = (ifHz / 1e9).toFixed(3);
    }
}

let pushTimer = null;
function maybePushParams(partial) {
    if (!state.running) return;
    // Debounce live updates
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(() => {
        // Gains, taper and vel_max apply live in whichever mode is running.
        if (state.runningMode === 'fmcw') {
            pushFmcwParams(partial);
            return;
        }
        transport.setCwRadarParams(partial)
            .catch(e => log('error', 'WS', `set_cw_radar_params failed: ${e.message || e}`));
    }, 100);
}

/* --- Transport / wiring --- */
const transport = createTransport({
    onLog: log,
    onOpen: () => {
        setConnectionStatus(true);
        log('info', resolveTransportMode() === 'sim' ? 'SIM' : 'WS',
            resolveTransportMode() === 'sim' ? 'Connected to the radar simulator' : 'Connected to backend');
        syncRunningFromBackend();
    },
    onClose: () => {
        setConnectionStatus(false);
        if (!state.playback) setRunningUi(false);
    },
    onMessage: (msg) => {
        if (msg.type === 'state') {
            // Initial state push from backend. Only the kit's IF is used here.
            showKitIf(msg.data);
            // get_state() nests its fields under `data`.
            armShutdownIfPermitted((msg.data?.data ?? msg.data)?.shutdown_available === true);
            return;
        }
        if (msg.type === 'response') return;  // handled by invoke()
    },
    onRadarFrame: handleRadarFrame,
    // While a recording plays, its frames are the only ones drawn.
    onFmcwFrame: (frame) => { if (!state.playback) handleFmcwFrame(frame); },
});

async function startRadar() {
    resetColorScales();
    const mode = state.uiMode;
    try {
        const resp = mode === 'fmcw'
            ? await transport.startFmcwRadar(readFmcwParams())
            : await transport.startCwRadar(readParams());
        if (resp.status === 'ok') {
            state.last_frame_t = null;
            state.fmcw.waterfall = null;
            state.fmcw.last = null;
            setRunningUi(true, mode);
            log('info', 'Radar', `Started ${mode.toUpperCase()}`);
            if (mode === 'fmcw') await refreshFmcwState();
        } else {
            log('error', 'Radar', `Start failed: ${resp.message || JSON.stringify(resp)}`);
        }
    } catch (e) {
        log('error', 'Radar', `Start failed: ${e.message || e}`);
    }
}

document.getElementById('btn-radar').addEventListener('click', async () => {
    if (state.playback) {
        stopPlayback();
        return;
    }
    if (!state.running) {
        await startRadar();
        return;
    }
    try {
        if (state.runningMode === 'fmcw') {
            await transport.stopFmcwRadar();
        } else {
            await transport.stopCwRadar();
        }
        setRunningUi(false);
        log('info', 'Radar', 'Stopped');
    } catch (e) {
        log('error', 'Radar', `Stop failed: ${e.message || e}`);
    }
});

initTheme();
refreshWaterfallScale();
refreshAxisRanges();
setUiMode(state.uiMode);
transport.connect();

setTimeout(() => {
    ALL_CHARTS.forEach(id => {
        const el = document.getElementById(id);
        if (el) Plotly.Plots.resize(el);
    });
}, 100);
