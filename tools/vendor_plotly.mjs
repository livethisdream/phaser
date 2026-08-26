// Copy Plotly out of node_modules into public/vendor/ so the built app can
// load it from a relative path instead of cdn.plot.ly.
//
// Deliberately NOT a bundled `import Plotly from 'plotly.js-dist-min'`:
// Plotly is ~4.8 MB, and a content-hashed chunk would land a fresh 4.8 MB
// blob in git history on every rebuild. A stable filename means git stores
// exactly one blob until the pinned version changes.
//
// Run automatically as npm `prebuild`; cwd is the frontend package root.

import { createRequire } from "node:module";
import { copyFileSync, mkdirSync, statSync } from "node:fs";
import { dirname, join } from "node:path";

const require = createRequire(import.meta.url + "/../");
let src;
try {
  src = require.resolve("plotly.js-dist-min/plotly.min.js", {
    paths: [process.cwd()],
  });
} catch {
  console.error(
    "  ERROR: plotly.js-dist-min not installed. Run `npm install` first."
  );
  process.exit(1);
}

const destDir = join(process.cwd(), "public", "vendor");
mkdirSync(destDir, { recursive: true });
const dest = join(destDir, "plotly.min.js");
copyFileSync(src, dest);

const version = require(join(dirname(src), "package.json")).version;
const kb = Math.round(statSync(dest).size / 1024);
console.log(`  vendored plotly.js-dist-min@${version} -> public/vendor/plotly.min.js (${kb} KB)`);
