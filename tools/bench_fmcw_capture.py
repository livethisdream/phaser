#!/usr/bin/env python3
"""Time an FMCW capture on the kit, and try a faster way to read it.

Item 3 of "Next" in docs/fmcw-phase-c-plan.md: a range_doppler frame takes
~554 ms, mostly the USB transfer, against a ~5 fps target. Measure before
changing anything. This runs ON the Pi, against the installed backend, with
the service stopped (it needs the hardware to itself):

    sudo systemctl stop phaser-headless
    curl -fsSLO https://raw.githubusercontent.com/livethisdream/phaser/radar-dev/tools/bench_fmcw_capture.py
    python3 bench_fmcw_capture.py
    sudo systemctl start phaser-headless

It enters FMCW through PhaserHeadless itself -- the same probe, ramp, TDD and
buffer sizing the service uses -- and reports, per frame (median, ms):

  end to end   do_fmcw_radar_frame(): capture + DSP, as the service runs it
  refill       burst + buffer refill: the USB transfer, which no read
               strategy can shorten
  stock read   pyadi's conversion: four per-channel chan.read() passes,
               then complex math in float64, then the channel sum
  raw read     one buffer.read() and a numpy de-interleave, summed in place

and whether the raw read gives bit-identical samples to the stock one on
the same buffer. It changes nothing on the kit and leaves FMCW mode on exit.
"""

import argparse
import os
import statistics
import sys
import time

import numpy as np

INSTALL_DIR = "/home/analog/pyadi-iio/examples/phaser"


def trigger(hw, plan):
    """Fire one burst, as capture_fmcw_frame does."""
    if plan["synced"]:
        g = hw.phaser_gpios
        g.gpio_burst = 0
        g.gpio_burst = 1
        g.gpio_burst = 0


def stock_read(sdr, buf):
    """pyadi's libiio 0.x read path (compat_libiio_v0_rx._rx_buffered_data,
    minus the refill), then rx()'s complex combine and capture_fmcw_frame's
    channel sum."""
    x = []
    for m in sdr.rx_enabled_channels:
        for name in (sdr._rx_channel_names[m * 2], sdr._rx_channel_names[m * 2 + 1]):
            chan = sdr._rxadc.find_channel(name)
            df = chan.data_format
            fmt = ("i" if df.is_signed is True else "u") + str(df.length // 8)
            x.append(np.frombuffer(chan.read(buf), dtype=fmt))
    data = [x[i] + 1j * x[i + 1] for i in range(0, len(x), 2)]
    return data[0] + data[1]


def raw_read(buf, nchan):
    """One copy of the whole buffer, de-interleaved by numpy, summed in place.

    Assumes the buffer interleaves I0 Q0 I1 Q1 as int16, already
    sign-extended. That is exactly what the equality check tests.
    """
    raw = np.frombuffer(buf.read(), dtype=np.int16).reshape(-1, nchan)
    out = np.empty(raw.shape[0], dtype=np.complex64)
    out.real = raw[:, 0]
    out.real += raw[:, 2]
    out.imag = raw[:, 1]
    out.imag += raw[:, 3]
    return out


def ms(xs):
    return f"{statistics.median(xs) * 1e3:7.1f} ms  (min {min(xs) * 1e3:.1f}, max {max(xs) * 1e3:.1f})"


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", default=INSTALL_DIR, help="installed backend (default: %(default)s)")
    ap.add_argument("--preset", default="range_doppler")
    ap.add_argument("--frames", type=int, default=20)
    args = ap.parse_args()

    os.chdir(args.dir)
    sys.path.insert(0, args.dir)
    import phaser_headless  # noqa: E402 -- needs the install dir on the path

    h = phaser_headless.PhaserHeadless(pub_port=15555, rep_port=15556)
    try:
        resp = h.start_fmcw_radar({"preset": args.preset})
        if resp.get("status") != "ok":
            sys.exit(f"could not enter FMCW: {resp.get('message')}")
        sdr, hw, plan = h.sdr, h.fmcw_hw, h.fmcw_plan
        print(f"\n{args.preset}: {plan['num_chirps']} chirp(s), PRI {plan['pri_ms']} ms, "
              f"buffer {plan['rx_buffer_size']} samples at {plan['sample_rate'] / 1e6:g} MS/s, "
              f"{args.frames} frames\n")

        for _ in range(2):  # warm-up
            h.do_fmcw_radar_frame()

        e2e, cap, proc = [], [], []
        for _ in range(args.frames):
            t = time.perf_counter()
            out = h.do_fmcw_radar_frame()
            e2e.append(time.perf_counter() - t)
            cap.append(out["capture_ms"] / 1e3)
            proc.append(out["process_ms"] / 1e3)
        print(f"end to end   {ms(e2e)}   -> {1 / statistics.median(e2e):.1f} fps")
        print(f"  capture    {ms(cap)}")
        print(f"  DSP        {ms(proc)}")

        buf = getattr(sdr, "_rxbuf", None)
        if buf is None:
            print("\nNo sdr._rxbuf: this pyadi uses the libiio 1.x stream path, "
                  "which this script does not split. Only the totals above apply.")
            return
        nchan = 2 * len(sdr.rx_enabled_channels)
        if nchan != 4:
            sys.exit(f"expected 2 Rx channels enabled, found {sdr.rx_enabled_channels}")

        refill, stock, fast, equal, maxdiff = [], [], [], 0, 0.0
        for _ in range(args.frames):
            trigger(hw, plan)
            t0 = time.perf_counter()
            buf.refill()
            t1 = time.perf_counter()
            a = stock_read(sdr, buf)
            t2 = time.perf_counter()
            b = raw_read(buf, nchan)
            t3 = time.perf_counter()
            refill.append(t1 - t0)
            stock.append(t2 - t1)
            fast.append(t3 - t2)
            if np.array_equal(a, b.astype(np.complex128)):
                equal += 1
            else:
                maxdiff = max(maxdiff, float(np.max(np.abs(a - b))))

        print(f"\nrefill       {ms(refill)}")
        print(f"stock read   {ms(stock)}")
        print(f"raw read     {ms(fast)}   saves {(statistics.median(stock) - statistics.median(fast)) * 1e3:.1f} ms")
        print(f"identical    {equal}/{args.frames} frames"
              + ("" if equal == args.frames else f"  (max |diff| {maxdiff:g}: raw read NOT safe as written)"))
        df = sdr._rxadc.find_channel(sdr._rx_channel_names[0]).data_format
        print(f"\ndata format  {df.bits} bits in {df.length}, shift {df.shift}, "
              f"signed {df.is_signed}, big-endian {df.is_be}")
    finally:
        try:
            h.stop_fmcw_radar()
        finally:
            h.stop()


if __name__ == "__main__":
    main()
