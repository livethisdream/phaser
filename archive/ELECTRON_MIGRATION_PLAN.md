# Electron Migration Plan

## Goal
Replace Tauri with Electron for faster iteration and reliable builds while keeping the polished frameless UI.

## Why Electron over Tauri
- Fast builds (seconds, not minutes)
- No Rust compilation / caching issues
- Hot reload during development
- Proven ecosystem (VS Code, Slack, Discord)
- Keep all our custom titlebar/drag work

## Architecture

```
PhaserApp/
├── electron/
│   ├── main.js          # Electron main process
│   ├── preload.js       # Bridge to Python backend
│   └── package.json
├── frontend/            # Existing Vite frontend (unchanged)
│   ├── src/
│   └── dist/
├── phaser_sidecar.py    # Existing Python backend
└── phaser_service.py    # Existing service layer
```

## Migration Steps

### Phase 1: Electron Shell
1. Create `electron/` directory with package.json
2. Set up main.js with frameless BrowserWindow
3. Load our existing frontend/dist
4. Test window controls (min/max/close)

### Phase 2: Python Backend Integration
1. Spawn Python sidecar from Electron (same as Tauri)
2. Use JSON-lines protocol over stdin/stdout (already built)
3. Update transport-tauri.js → transport-electron.js
4. Wire up IPC bridge

### Phase 3: Polish
1. App icon
2. Production build
3. PyInstaller bundle for Python
4. Single installer (NSIS or electron-builder)

### Phase 4: Cleanup
1. Remove src-tauri/ directory
2. Update documentation

## Backup Plan: Browser Mode
If Electron doesn't work out, fall back to:
- FastAPI server + auto-launch browser in Chrome app mode
- Single PyInstaller exe that starts server and opens browser
- Simpler but less "native" feel

## Files to Keep
- All frontend/ code (unchanged)
- phaser_sidecar.py (minor tweaks for Electron spawn)
- phaser_service.py (unchanged)

## Files to Create
- electron/main.js
- electron/preload.js  
- electron/package.json
- frontend/src/transport-electron.js

## Files to Remove (after migration)
- src-tauri/ (entire directory)
