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
};

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
        ['chart-waterfall', 'chart-spectrum', 'chart-time'].forEach(id => {
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
            ['chart-waterfall', 'chart-spectrum', 'chart-time'].forEach(id => {
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
    ['chart-waterfall', 'chart-spectrum', 'chart-time'].forEach(id => {
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

function refreshWaterfallScale() {
    if (state.autoscale) {
        Plotly.relayout('chart-waterfall', { 'zauto': true });
    } else {
        Plotly.relayout('chart-waterfall', { 'zmin': state.db_floor, 'zmax': state.db_ceil, 'zauto': false });
    }
    Plotly.relayout('chart-spectrum', { 'yaxis.range': [state.db_floor, state.db_ceil] });
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

function setRunningUi(running) {
    state.running = running;
    const btn = document.getElementById('btn-radar');
    if (btn) {
        btn.textContent = running ? 'Stop' : 'Start CW';
        btn.classList.toggle('btn-primary', !running);
        btn.classList.toggle('btn-danger', running);
    }
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

    // FPS — log periodically (every ~3s) instead of cluttering the stat row
    const now = performance.now();
    if (state.last_frame_t) {
        const dt = (now - state.last_frame_t) / 1000;
        const inst = dt > 0 ? 1 / dt : 0;
        state.fps = state.fps_alpha * inst + (1 - state.fps_alpha) * state.fps;
        if (!state.last_fps_log_t || (now - state.last_fps_log_t) > 3000) {
            log('info', 'Radar', `Frame rate: ${state.fps.toFixed(1)} Hz`);
            state.last_fps_log_t = now;
        }
    }
    state.last_frame_t = now;

    // Time-domain (optional — only if backend includes a downsampled iq trace)
    if (frame.iq_mag && frame.iq_mag.length) {
        Plotly.restyle('chart-time', {
            x: [Array.from({ length: frame.iq_mag.length }, (_, i) => i)],
            y: [frame.iq_mag]
        });
    }
}

/* --- Param plumbing --- */
function readParams() {
    return {
        sample_rate: parseFloat(document.getElementById('sample-rate').value) * 1e3,
        fft_size: parseInt(document.getElementById('fft-size').value, 10),
        signal_freq: parseFloat(document.getElementById('signal-freq').value) * 1e3,
        output_freq: parseFloat(document.getElementById('output-freq').value) * 1e9,
        center_freq: parseFloat(document.getElementById('center-freq').value) * 1e9,
        rx_gain: parseFloat(document.getElementById('rx-gain').value),
        tx_gain: parseFloat(document.getElementById('tx-gain').value),
        fft_window: document.getElementById('fft-window').value,
        taper: state.taper,
        vel_max: state.vel_max,
    };
}

let pushTimer = null;
function maybePushParams(partial) {
    if (!state.running) return;
    // Debounce live updates
    if (pushTimer) clearTimeout(pushTimer);
    pushTimer = setTimeout(() => {
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
    },
    onClose: () => {
        setConnectionStatus(false);
        setRunningUi(false);
    },
    onMessage: (msg) => {
        if (msg.type === 'state') {
            // Initial state push from backend; not radar-specific.
            return;
        }
        if (msg.type === 'response') return;  // handled by invoke()
    },
    onRadarFrame: handleRadarFrame,
});

document.getElementById('btn-radar').addEventListener('click', async () => {
    if (state.running) {
        try {
            await transport.stopCwRadar();
            setRunningUi(false);
            log('info', 'Radar', 'Stopped');
        } catch (e) {
            log('error', 'Radar', `Stop failed: ${e.message || e}`);
        }
    } else {
        const params = readParams();
        try {
            const resp = await transport.startCwRadar(params);
            if (resp.status === 'ok') {
                setRunningUi(true);
                log('info', 'Radar', 'Started');
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
    ['chart-waterfall', 'chart-spectrum', 'chart-time'].forEach(id => {
        const el = document.getElementById(id);
        if (el) Plotly.Plots.resize(el);
    });
}, 100);
