// Stub - Tauri transport not used in browser mode
export const minimizeWindow = () => {};
export const maximizeWindow = () => {};
export const closeWindow = () => {};
export const isMaximized = () => false;
export const startDrag = () => {};

export function createTauriTransport() {
    return {
        connect: () => {},
        send: () => {},
        invoke: () => Promise.reject(new Error('Tauri not available')),
        onSweepData: null,
        onMessage: null,
        isConnected: false
    };
}
