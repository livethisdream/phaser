import { createWebTransport } from './transport-web.js';

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
    fmcw: {
        available: null,     // from get_fmcw_radar_state's capability
        cfg: null,           // the running config, for the 0 m calibration
        plan: null,
        waterfall: null,     // rows = frames, cols = range bins
        last: null,          // the last frame, for "Set 0 m Here"
        span_db: 60,         // colour scale: the frame's peak down this far
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
        // These take effect on next start; surface a hint to user.
        log('info', 'UI', `${id} changed — applies on next Start CW`);
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
    xaxis: { title: 'Velocity (m/s)', range: [-20, 20] },
    yaxis: { title: 'Time (frames ago)', autorange: 'reversed' },
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
    xaxis: { title: 'Range (m)' },
    yaxis: { title: 'Time (frames ago)', autorange: 'reversed' },
}), { displayModeBar: false, responsive: true });

// Range-Doppler: x = velocity, y = range (lab "Range Doppler Plotting").
Plotly.newPlot('chart-fmcw-rd', [{
    z: [[]], x: [], y: [], type: 'heatmap', colorscale: 'Electric',
    colorbar: { title: 'dB', thickness: 12 },
    hovertemplate: 'v=%{x:.2f} m/s<br>R=%{y:.2f} m<br>%{z:.1f} dB<extra></extra>',
}], Object.assign({}, getLayoutBase(), {
    xaxis: { title: 'Velocity (m/s)' },
    yaxis: { title: 'Range (m)' },
}), { displayModeBar: false, responsive: true });

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
    if (text) text.textContent = connected ? 'Connected' : 'Disconnected';
}

function setRunningUi(running, mode = state.uiMode) {
    state.running = running;
    state.runningMode = running ? mode : null;
    const btn = document.getElementById('btn-radar');
    if (btn) {
        btn.textContent = running ? 'Stop' : (state.uiMode === 'fmcw' ? 'Start FMCW' : 'Start CW');
        btn.classList.toggle('btn-primary', !running);
        btn.classList.toggle('btn-danger', running);
        // FMCW on a kit that cannot run it: say why instead of failing.
        btn.disabled = !running && state.uiMode === 'fmcw' && state.fmcw.available === false;
    }
    // One mode at a time on the backend: switch only while idle.
    document.querySelectorAll('#mode-cw, #mode-fmcw').forEach(b => { b.disabled = running; });
}

/* --- CW / FMCW mode switch --- */
function setUiMode(mode) {
    state.uiMode = mode === 'fmcw' ? 'fmcw' : 'cw';
    document.getElementById('mode-cw')?.classList.toggle('active', state.uiMode === 'cw');
    document.getElementById('mode-fmcw')?.classList.toggle('active', state.uiMode === 'fmcw');
    // Show the mode's tabs (and Logs), and land on its first one.
    let first = null;
    document.querySelectorAll('.tab-btn').forEach(b => {
        const m = b.dataset.mode;
        b.hidden = Boolean(m) && m !== state.uiMode;
        if (!first && m === state.uiMode) first = b;
    });
    first?.click();
    document.getElementById('box-peak-range').hidden = state.uiMode !== 'fmcw';
    // The FMCW settings open with FMCW; CW keeps the Hardware section.
    activateSidebarSection(state.uiMode === 'fmcw' ? 4 : 0);
    setRunningUi(state.running, state.runningMode || state.uiMode);
}
document.getElementById('mode-cw')?.addEventListener('click', () => setUiMode('cw'));
document.getElementById('mode-fmcw')?.addEventListener('click', () => setUiMode('fmcw'));

/* --- Frame rate, shared by both modes --- */
function noteFrameRate() {
    const now = performance.now();
    if (state.last_frame_t) {
        const dt = (now - state.last_frame_t) / 1000;
        const inst = dt > 0 ? 1 / dt : 0;
        state.fps = state.fps_alpha * inst + (1 - state.fps_alpha) * state.fps;
        const el = document.getElementById('stat-rate');
        if (el) el.textContent = `${state.fps.toFixed(1)} fps`;
    }
    state.last_frame_t = now;
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
    const zmax = Math.max(...trace);
    Plotly.restyle('chart-fmcw-wf', {
        z: [wf], x: [rangeAxis], y: [Array.from({ length: depth }, (_, i) => i)],
        zauto: false, zmin: zmax - state.fmcw.span_db, zmax,
    });
}

// Frames are drawn at most once per display refresh, newest first: Plotly
// cannot redraw three charts at the rates a single-chirp preset can deliver,
// and a backlog of redraws delays every click behind it.
let fmcwPending = null;
function handleFmcwFrame(frame) {
    if (!frame || !frame.range_axis) return;
    state.fmcw.last = frame;
    noteFrameRate();
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
        const zmax = frame.map.db_max;
        Plotly.restyle('chart-fmcw-rd', {
            z: [z], x: [frame.velocity_axis], y: [range],
            zauto: false, zmin: zmax - state.fmcw.span_db, zmax,
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
        if (thr.length === trace.length && fmcwEl('fmcw-cfar-apply')?.checked) {
            const floor = Math.min(...trace);
            trace = trace.map((v, i) => (v > thr[i] ? v : floor));
        }
        Plotly.restyle('chart-fmcw-range', {
            x: [range, [], thr.length ? range : [], idx.map(i => range[i])],
            y: [trace, [], thr, idx.map(i => trace[i])],
        });
    }
    pushRangeWaterfall(range, trace);

    if (typeof frame.peak_range === 'number') {
        document.getElementById('stat-peak-range').textContent = `${frame.peak_range.toFixed(2)} m`;
    }
    document.getElementById('stat-peak-vel').textContent =
        typeof frame.peak_velocity === 'number' ? `${frame.peak_velocity.toFixed(2)} m/s` : '-- m/s';
    if (typeof frame.peak_magnitude_db === 'number') {
        document.getElementById('stat-peak-mag').textContent = `${frame.peak_magnitude_db.toFixed(1)} dB`;
    }
}

/* --- FMCW controls --- */
const fmcwEl = (id) => document.getElementById(id);

function readFmcwParams() {
    const params = {
        preset: fmcwEl('fmcw-preset').value || undefined,
        mti: fmcwEl('fmcw-mti').value,
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
    const zero = parseFloat(fmcwEl('fmcw-zero-khz').value);
    params.zero_range_freq = Number.isFinite(zero) ? zero * 1e3 : null;
    const center = parseFloat(document.getElementById('center-freq').value);
    params.center_freq = Number.isFinite(center) ? center * 1e9 : null;
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
        const sel = fmcwEl('fmcw-preset');
        if (sel && Array.isArray(d.presets) && sel.options.length !== d.presets.length) {
            const keep = sel.value || d.preset;
            sel.innerHTML = '';
            // Short label in the box; the internal name stays the value.
            d.presets.forEach(p => sel.add(new Option(d.preset_info?.[p]?.label || p, p)));
            sel.value = d.presets.includes(keep) ? keep : d.presets[0];
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
    const fmcw = await refreshFmcwState();
    if (fmcw?.running) {
        setUiMode('fmcw');
        setRunningUi(true, 'fmcw');
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
function updateFmcwControlGating() {
    const preset = fmcwEl('fmcw-preset')?.value;
    const n = state.fmcw.preset_info?.[preset]?.num_chirps;
    if (!n) return;  // not known yet
    const multi = n > 1;
    // Grey a row out with its reason as the tooltip; restore its own tooltip
    // (kept in data-tip) when it applies again.
    const gate = (row, off, why) => {
        if (!row) return;
        if (row.dataset.tip === undefined) row.dataset.tip = row.title || '';
        row.classList.toggle('fmcw-disabled', off);
        row.title = off ? why : row.dataset.tip;
        row.querySelectorAll('input, select').forEach(el => { el.disabled = off; });
    };
    gate(fmcwEl('fmcw-mti')?.closest('.fmcw-row'), !multi,
         'MTI cancels between chirps: it needs a multi-chirp preset (Range + Velocity).');
    const noCfar = 'CFAR runs on the single-chirp range profile: pick a Range preset.';
    ['fmcw-cfar', 'fmcw-cfar-guard', 'fmcw-cfar-ref', 'fmcw-cfar-bias', 'fmcw-cfar-apply']
        .forEach(id => gate(fmcwEl(id)?.closest('.fmcw-row'), multi, noCfar));
    // What the preset is for, on hover rather than under the box.
    const sel = fmcwEl('fmcw-preset');
    if (sel) sel.title = state.fmcw.preset_info?.[preset]?.hint || '';
}

fmcwEl('fmcw-preset')?.addEventListener('change', (e) => {
    updateFmcwControlGating();
    pushFmcwParams({ preset: e.target.value });
});
fmcwEl('fmcw-mti')?.addEventListener('change', (e) => pushFmcwParams({ mti: e.target.value }));
fmcwEl('fmcw-range-max')?.addEventListener('change', () => {
    state.fmcw.waterfall = null;
    pushFmcwParams({ range_max: readFmcwParams().range_max });
});
['fmcw-cfar', 'fmcw-cfar-guard', 'fmcw-cfar-ref', 'fmcw-cfar-bias'].forEach(id => {
    fmcwEl(id)?.addEventListener('change', () => {
        const p = readFmcwParams();
        pushFmcwParams({ cfar: p.cfar, cfar_guard: p.cfar_guard, cfar_ref: p.cfar_ref,
                         cfar_bias_db: p.cfar_bias_db });
    });
});
fmcwEl('fmcw-span')?.addEventListener('change', (e) => {
    const v = parseFloat(e.target.value);
    if (Number.isFinite(v) && v > 0) state.fmcw.span_db = v;
});
fmcwEl('fmcw-zero-khz')?.addEventListener('change', () => {
    pushFmcwParams({ zero_range_freq: readFmcwParams().zero_range_freq });
});

// Lab step 21: hold the reflector at the array, and its peak becomes 0 m.
// The frame's peak range was computed against the current 0 m beat, so the
// new one is that plus the beat the peak range stands for: f = 2 S R / c,
// with S the slope over the ramp time the plan actually uses.
fmcwEl('fmcw-zero-set')?.addEventListener('click', () => {
    const f = state.fmcw.last, cfg = state.fmcw.cfg, plan = state.fmcw.plan;
    if (state.runningMode !== 'fmcw' || !f || !cfg || !plan) {
        log('error', 'FMCW', 'Start FMCW with a reflector at 0 m first');
        return;
    }
    const current = cfg.zero_range_freq ?? cfg.signal_freq;
    const slope = cfg.chirp_bw / plan.effective_ramp_time;
    const zero = current + 2 * slope * f.peak_range / C_LIGHT;
    fmcwEl('fmcw-zero-khz').value = (zero / 1e3).toFixed(2);
    pushFmcwParams({ zero_range_freq: zero });
    log('info', 'FMCW', `0 m set to ${(zero / 1e3).toFixed(2)} kHz (peak was at ${f.peak_range.toFixed(2)} m)`);
});
fmcwEl('fmcw-zero-reset')?.addEventListener('click', () => {
    fmcwEl('fmcw-zero-khz').value = '';
    pushFmcwParams({ zero_range_freq: null });
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
const transport = createWebTransport({
    onLog: log,
    onOpen: () => {
        setConnectionStatus(true);
        log('info', 'WS', 'Connected to backend');
        syncRunningFromBackend();
    },
    onClose: () => {
        setConnectionStatus(false);
        setRunningUi(false);
    },
    onMessage: (msg) => {
        if (msg.type === 'state') {
            // Initial state push from backend. Only the kit's IF is used here.
            showKitIf(msg.data);
            return;
        }
        if (msg.type === 'response') return;  // handled by invoke()
    },
    onRadarFrame: handleRadarFrame,
    onFmcwFrame: handleFmcwFrame,
});

document.getElementById('btn-radar').addEventListener('click', async () => {
    if (state.running) {
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
    } else {
        const mode = state.uiMode;
        try {
            const resp = mode === 'fmcw'
                ? await transport.startFmcwRadar(readFmcwParams())
                : await transport.startCwRadar(readParams());
            if (resp.status === 'ok') {
                state.last_frame_t = null;
                state.fmcw.waterfall = null;
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
});

initTheme();
refreshWaterfallScale();
refreshAxisRanges();
transport.connect();

setTimeout(() => {
    ALL_CHARTS.forEach(id => {
        const el = document.getElementById(id);
        if (el) Plotly.Plots.resize(el);
    });
}, 100);
