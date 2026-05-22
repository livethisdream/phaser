/**
 * nanoid.js — minimal vendored ID generator (no external dep)
 * Generates a URL-safe random string of length n.
 */
export function nanoid(n = 21) {
    const arr = new Uint8Array(n);
    crypto.getRandomValues(arr);
    const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-';
    return Array.from(arr, (b) => chars[b % 64]).join('');
}

