/**
 * worker.js — runs the radar simulator off the main thread.
 *
 * A range-Doppler frame is a 2^18-sample synthesis and a few hundred FFTs,
 * a couple of hundred milliseconds of work; on the main thread that would
 * freeze the page for each frame. The message protocol mirrors the WebSocket
 * JSON phaser_headless.py speaks, so transport-sim.js is a thin adapter.
 *
 * Frames are paced the way the backend's main loop paces them: at most
 * RADAR_MAX_FPS, one at a time, yielding between frames so commands land.
 */

import { RADAR_MAX_FPS } from './constants.generated.js';
import { createRadarEngine } from './engine.js';

const engine = createRadarEngine();
let timer = null;
let lastStart = 0;

function schedule() {
    if (timer !== null || engine.mode === 'idle') return;
    const wait = Math.max(0, 1000 / RADAR_MAX_FPS - (performance.now() - lastStart));
    timer = setTimeout(step, wait);
}

function step() {
    timer = null;
    lastStart = performance.now();
    try {
        const frame = engine.frame();
        if (frame) self.postMessage(frame);
    } catch (err) {
        self.postMessage({ type: 'error', source: engine.mode, message: String(err?.message || err) });
    }
    schedule();
}

self.onmessage = (event) => {
    const { cmd, id, ...rest } = event.data || {};
    const data = { ...rest, ...(rest.data || {}) };
    delete data.data;
    let response;
    try {
        response = engine.handle(cmd, data);
    } catch (err) {
        response = { status: 'error', message: String(err?.message || err) };
    }
    if (id) self.postMessage({ ...response, id });
    schedule();
};

// What the backend sends a new WebSocket client first: its state.
self.postMessage({ type: 'ready', state: engine.handle('get_state') });
