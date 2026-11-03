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

// --- Reading ---------------------------------------------------------------------

async function inflateRaw(bytes) {
    // np.savez_compressed deflates each entry; np.savez (the course's files)
    // stores them, which needs nothing.
    if (typeof DecompressionStream === 'undefined') {
        throw new Error('npz: this browser cannot read a compressed .npz (np.savez_compressed)');
    }
    const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw'));
    return new Uint8Array(await new Response(stream).arrayBuffer());
}

function parseShape(text) {
    const m = /'shape':\s*\(([^)]*)\)/.exec(text);
    if (!m) throw new Error('npz: .npy header has no shape');
    return m[1].split(',').map(s => s.trim()).filter(Boolean).map(Number);
}

/** One .npy file -> {dtype, shape, data}. */
export function readNpy(bytes) {
    if (bytes[0] !== 0x93 || String.fromCharCode(...bytes.subarray(1, 6)) !== 'NUMPY') {
        throw new Error('npz: not a .npy file');
    }
    const major = bytes[6];
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const hlen = major === 1 ? view.getUint16(8, true) : view.getUint32(8, true);
    const hstart = major === 1 ? 10 : 12;
    const header = new TextDecoder('latin1').decode(bytes.subarray(hstart, hstart + hlen));
    const descr = /'descr':\s*'([^']+)'/.exec(header)?.[1];
    if (/'fortran_order':\s*True/.test(header)) throw new Error('npz: Fortran-ordered arrays are not supported');
    const shape = parseShape(header);
    const count = shape.reduce((a, b) => a * b, 1);
    // Copy the body to an aligned buffer: typed arrays need it. (A copy for
    // certain: Node's Buffer.slice is a view, not a copy.)
    const body = Uint8Array.prototype.slice.call(bytes, hstart + hlen);
    const buf = body.buffer;
    if (descr === '<c8' || descr === '<c16') {
        const src = descr === '<c8' ? new Float32Array(buf, 0, 2 * count) : new Float64Array(buf, 0, 2 * count);
        const re = new Float32Array(count), im = new Float32Array(count);
        for (let i = 0; i < count; i++) { re[i] = src[2 * i]; im[i] = src[2 * i + 1]; }
        return { dtype: descr, shape, data: { re, im } };
    }
    const real = { '<f8': Float64Array, '<f4': Float32Array, '<i8': BigInt64Array, '<i4': Int32Array, '<i2': Int16Array };
    if (descr in real) {
        let arr = new real[descr](buf, 0, count);
        if (descr === '<i8') arr = Float64Array.from(arr, Number);
        return { dtype: descr, shape, data: shape.length === 0 ? Number(arr[0]) : arr };
    }
    const u = /^<U(\d+)$/.exec(descr || '');
    if (u) {
        const n = Number(u[1]) * count;
        const cps = Array.from(new Uint32Array(buf, 0, n)).filter(c => c !== 0);
        return { dtype: descr, shape, data: String.fromCodePoint(...cps) };
    }
    throw new Error(`npz: unsupported dtype ${descr}`);
}

/** An .npz (ArrayBuffer or Uint8Array) -> {name: {dtype, shape, data}}. */
export async function readNpz(input) {
    const bytes = ArrayBuffer.isView(input)
        ? new Uint8Array(input.buffer, input.byteOffset, input.byteLength)
        : new Uint8Array(input);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    // The end-of-central-directory record is the last 22 bytes (plus any comment).
    let eocd = -1;
    for (let i = bytes.length - 22; i >= Math.max(0, bytes.length - 22 - 65535); i--) {
        if (view.getUint32(i, true) === 0x06054b50) { eocd = i; break; }
    }
    if (eocd < 0) throw new Error('npz: not a zip file');
    const entries = view.getUint16(eocd + 10, true);
    let p = view.getUint32(eocd + 16, true);
    const out = {};
    for (let e = 0; e < entries; e++) {
        if (view.getUint32(p, true) !== 0x02014b50) throw new Error('npz: bad central directory');
        const method = view.getUint16(p + 10, true);
        let csize = view.getUint32(p + 20, true);
        let usize = view.getUint32(p + 24, true);
        const nlen = view.getUint16(p + 28, true), xlen = view.getUint16(p + 30, true), clen = view.getUint16(p + 32, true);
        let local = view.getUint32(p + 42, true);
        const name = new TextDecoder().decode(bytes.subarray(p + 46, p + 46 + nlen));
        // Zip64 (numpy's savez may force it): any field saturated at
        // 0xFFFFFFFF is in the 0x0001 extra field instead, in this order.
        for (let x = p + 46 + nlen; x + 4 <= p + 46 + nlen + xlen;) {
            const id = view.getUint16(x, true), size = view.getUint16(x + 2, true);
            if (id === 0x0001) {
                let q = x + 4;
                const next = () => { const v = Number(view.getBigUint64(q, true)); q += 8; return v; };
                if (usize === 0xffffffff) usize = next();
                if (csize === 0xffffffff) csize = next();
                if (local === 0xffffffff) local = next();
            }
            x += 4 + size;
        }
        p += 46 + nlen + xlen + clen;
        const lnlen = view.getUint16(local + 26, true), lxlen = view.getUint16(local + 28, true);
        const start = local + 30 + lnlen + lxlen;
        let data = bytes.subarray(start, start + csize);
        if (method === 8) data = await inflateRaw(data);
        else if (method !== 0) throw new Error(`npz: unsupported zip method ${method}`);
        if (name.endsWith('.npy')) out[name.slice(0, -4)] = readNpy(data);
    }
    return out;
}
