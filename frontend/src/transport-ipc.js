// Stub - IPC transport not used in browser mode
export function createIpcTransport() {
    return {
        connect: () => {},
        send: () => {},
        invoke: () => Promise.reject(new Error('IPC not available')),
        onSweepData: null,
        onMessage: null,
        isConnected: false
    };
}
