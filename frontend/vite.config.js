import { defineConfig } from 'vite';

// Use relative asset URLs so the desktop file:// host (PyWebView) can load
// built JS/CSS from frontend/dist without an HTTP server.
export default defineConfig({
  base: './',
});

