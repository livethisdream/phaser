import path from 'node:path';
import { defineConfig } from 'vite';

// Which backend this build defaults to, resolved once so the stamped marker and
// src/transport.js cannot disagree: both come from VITE_TRANSPORT.
//   web — the WebSocket to phaser_headless.py on the Pi. The default, and what
//         install.sh ships.
//   sim — the in-browser radar simulator, for the GitHub Pages demo.
const TRANSPORT = process.env.VITE_TRANSPORT === 'sim' ? 'sim' : 'web';

/**
 * The same guard as frontend/vite.config.js, for the same reason: Vite
 * constant-folds VITE_TRANSPORT, so a sim build and a hardware build differ
 * only inside minified JS. Stamp the mode into index.html where CI and
 * install.sh can read it, and refuse a sim build aimed at dist/, the committed
 * tree install.sh ships to real hardware.
 */
function transportMarker() {
    return {
        name: 'phaser-transport-marker',
        configResolved(config) {
            if (TRANSPORT !== 'sim' || config.command !== 'build') return;
            const out = path.resolve(config.root, config.build.outDir);
            if (out !== path.resolve(config.root, 'dist')) return;
            throw new Error(
                'Refusing to write a simulator build into frontend-radar/dist/.\n' +
                'That directory is committed and install.sh ships it to real hardware.\n' +
                'For the Pages demo build, pass an explicit output directory:\n' +
                '  VITE_TRANSPORT=sim npm run build -- --outDir dist-pages'
            );
        },
        transformIndexHtml() {
            return [{
                tag: 'meta',
                attrs: { name: 'phaser-transport', content: TRANSPORT },
                injectTo: 'head',
            }];
        },
    };
}

// Relative asset URLs, so any static host -- the Pi's :8081, or GitHub Pages
// under /phaser/radar/ -- can serve the build.
export default defineConfig({
    base: './',
    plugins: [transportMarker()],
    // The simulator's worker imports ES modules.
    worker: { format: 'es' },
});
