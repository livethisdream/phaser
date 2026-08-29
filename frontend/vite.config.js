import path from 'node:path';
import { defineConfig } from 'vite';

// Which backend this build defaults to, resolved once so the stamped marker and
// the code in transport.js cannot disagree: both come from VITE_TRANSPORT.
//   web — REST + WebSocket against phaser_headless.py on the Pi. The default,
//         and what install.sh ships.
//   sim — the in-browser simulator, for the GitHub Pages demo, which is static
//         and has no backend to talk to.
const TRANSPORT = process.env.VITE_TRANSPORT === 'sim' ? 'sim' : 'web';

/**
 * Make the build say which one it is, and refuse the one dangerous mistake.
 *
 * Vite constant-folds `import.meta.env.VITE_TRANSPORT` at build time, so the
 * flag leaves no readable trace: a sim build and a hardware build have
 * byte-identical index.html and differ only inside minified JS, where the
 * check collapses to `(window.__PHASER_SIM, "sim")` versus a live ternary
 * ending in "web". You cannot look at a dist/ -- or at a Pi -- and tell which
 * you have. The only runtime tell is the orange SIMULATION pill.
 *
 * That matters because the two are one forgotten flag apart. deploy-pages.yml
 * builds the demo with `npm run build -- --outDir dist-pages`; Vite's default
 * outDir is `dist`, which is the tree install.sh ships to hardware. Anyone
 * reproducing the Pages build locally and dropping the --outDir overwrites the
 * real frontend with a simulator, and a lab then runs on synthesized IQ that
 * looks entirely plausible.
 *
 * So: stamp the mode into index.html where CI and install.sh can read it, and
 * hard-fail a sim build aimed at dist/ rather than letting it land.
 */
function transportMarker() {
    return {
        name: 'phaser-transport-marker',

        configResolved(config) {
            if (TRANSPORT !== 'sim' || config.command !== 'build') return;
            const out = path.resolve(config.root, config.build.outDir);
            if (out !== path.resolve(config.root, 'dist')) return;
            throw new Error(
                'Refusing to write a simulator build into frontend/dist/.\n' +
                'That directory is committed and install.sh ships it to real hardware.\n' +
                'For the Pages demo build, pass an explicit output directory:\n' +
                '  VITE_TRANSPORT=sim npm run build -- --outDir dist-pages'
            );
        },

        // Read by build-frontends.yml, deploy-pages.yml and install.sh. Also
        // handy in devtools for answering "which build is this Pi serving?".
        transformIndexHtml() {
            return [{
                tag: 'meta',
                attrs: { name: 'phaser-transport', content: TRANSPORT },
                injectTo: 'head',
            }];
        },
    };
}

export default defineConfig({
    // Relative asset URLs. Load-bearing for GitHub Pages, which serves this at
    // /phaser/ rather than at a domain root; an absolute base would 404 every
    // asset there.
    base: './',
    plugins: [transportMarker()],
});
