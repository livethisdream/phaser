/**
 * npz.js — NumPy .npz files in the browser, no libraries.
 *
 * An .npz is a zip of .npy files, one array each. writeNpz() builds one the
 * way np.savez does (stored, not deflated), so the in-browser simulator can
 * save captures in the course format (phaser_radar_dsp.capture_npz): numpy's
 * np.load reads the result, and so does the course's radar_lab.load_capture.
 *
 * Arrays are given as {dtype, shape, data}:
 *   dtype '<c8'   data {re, im} (Float32/Float64Array), row-major
 *   dtype '<f8'   data a number (shape []) or a Float64Array
 *   dtype '<U'    data a string (shape []); written as '<U<length>'
 */

// --- CRC-32, as zip wants it -------------------------------------------------
const CRC_TABLE = (() => {
    const t = new Uint32Array(256);
    for (let n = 0; n < 256; n++) {
        let c = n;
        for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
        t[n] = c >>> 0;
    }
    return t;
})();

function crc32(bytes) {
    let c = 0xffffffff;
    for (let i = 0; i < bytes.length; i++) c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
    return (c ^ 0xffffffff) >>> 0;
}

// --- .npy ------------------------------------------------------------------------

function npyHeader(descr, shape) {
    const shp = shape.length === 0 ? '()' : shape.length === 1 ? `(${shape[0]},)` : `(${shape.join(', ')})`;
    let dict = `{'descr': '${descr}', 'fortran_order': False, 'shape': ${shp}, }`;
    // Magic (6) + version (2) + length (2) + header, padded with spaces and a
    // closing newline to a multiple of 64, as numpy writes it.
    const total = Math.ceil((10 + dict.length + 1) / 64) * 64;
    dict = dict.padEnd(total - 10 - 1, ' ') + '\n';
    const out = new Uint8Array(total);
    out.set([0x93, 0x4e, 0x55, 0x4d, 0x50, 0x59, 1, 0], 0);   // \x93NUMPY v1.0
    out[8] = dict.length & 0xff;
    out[9] = dict.length >> 8;
    for (let i = 0; i < dict.length; i++) out[10 + i] = dict.charCodeAt(i);
    return out;
}

function npyBytes({ dtype, shape = [], data }) {
    if (dtype === '<c8') {
        const n = data.re.length;
        const body = new Float32Array(2 * n);
        for (let i = 0; i < n; i++) { body[2 * i] = data.re[i]; body[2 * i + 1] = data.im[i]; }
        return concat([npyHeader('<c8', shape), new Uint8Array(body.buffer)]);
    }
    if (dtype === '<f8') {
        const body = typeof data === 'number' ? Float64Array.of(data) : Float64Array.from(data);
        return concat([npyHeader('<f8', shape), new Uint8Array(body.buffer)]);
    }
    if (dtype === '<U') {
        const cps = Array.from(String(data), ch => ch.codePointAt(0));
        const body = Uint32Array.from(cps.length ? cps : [0]);
        return concat([npyHeader(`<U${Math.max(1, cps.length)}`, []), new Uint8Array(body.buffer)]);
    }
    throw new Error(`npz: unsupported dtype ${dtype}`);
}

function concat(parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let off = 0;
    for (const p of parts) { out.set(p, off); off += p.length; }
    return out;
}

// --- zip (stored) ----------------------------------------------------------------

/** {name: array} -> the .npz bytes. Entries are written as name + '.npy'. */
export function writeNpz(arrays) {
    const locals = [], centrals = [];
    let offset = 0;
    for (const [key, arr] of Object.entries(arrays)) {
        const name = new TextEncoder().encode(`${key}.npy`);
        const data = npyBytes(arr);
        const crc = crc32(data);
        const local = new DataView(new ArrayBuffer(30));
        local.setUint32(0, 0x04034b50, true);
        local.setUint16(4, 20, true);              // version needed
        local.setUint16(8, 0, true);               // stored
        local.setUint16(12, 0x21, true);           // date: 1980-01-01, as numpy's zipfile leaves it
        local.setUint32(14, crc, true);
        local.setUint32(18, data.length, true);
        local.setUint32(22, data.length, true);
        local.setUint16(26, name.length, true);
        locals.push(new Uint8Array(local.buffer), name, data);

        const central = new DataView(new ArrayBuffer(46));
        central.setUint32(0, 0x02014b50, true);
        central.setUint16(4, 20, true);            // version made by
        central.setUint16(6, 20, true);            // version needed
        central.setUint16(14, 0x21, true);
        central.setUint32(16, crc, true);
        central.setUint32(20, data.length, true);
        central.setUint32(24, data.length, true);
        central.setUint16(28, name.length, true);
        central.setUint32(42, offset, true);
        centrals.push(new Uint8Array(central.buffer), name);
        offset += 30 + name.length + data.length;
    }
    const cdSize = centrals.reduce((n, p) => n + p.length, 0);
    const end = new DataView(new ArrayBuffer(22));
    end.setUint32(0, 0x06054b50, true);
    end.setUint16(8, centrals.length / 2, true);
    end.setUint16(10, centrals.length / 2, true);
    end.setUint32(12, cdSize, true);
    end.setUint32(16, offset, true);
    return concat([...locals, ...centrals, new Uint8Array(end.buffer)]);
}

export function toBase64(bytes) {
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return typeof btoa === 'function' ? btoa(bin) : Buffer.from(bin, 'binary').toString('base64');
}

export function fromBase64(b64) {
    const bin = typeof atob === 'function' ? atob(b64) : Buffer.from(b64, 'base64').toString('binary');
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
}
