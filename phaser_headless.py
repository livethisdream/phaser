#!/usr/bin/env python3
"""
Headless Phaser backend with ZMQ and WebSocket interfaces.
Runs on the Raspberry Pi, publishes data and accepts commands.

Sockets:
  - PUB on port 5555: Streams sweep data (FFT, beam pattern) as msgpack
  - REP on port 5556: Receives commands (JSON) and returns responses
  - WebSocket on port 8765: Browser clients (JSON messages)
  - HTTP on port 8080: Serves static frontend files

Usage:
    python phaser_headless.py [--pub-port 5555] [--rep-port 5556] [--ws-port 8765] [--http-port 8080]
"""

import os
import socket
import subprocess
import sys
import threading
import time
import signal
import json
import asyncio
import numpy as np
from pathlib import Path
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from functools import partial

try:
    import zmq
    import msgpack
except ImportError:
    print("Install: pip install pyzmq msgpack")
    sys.exit(1)

try:
    import websockets
    HAS_WEBSOCKETS = True
except ImportError:
    HAS_WEBSOCKETS = False
    print("WebSocket support disabled. Install: pip install websockets")

import adi
from SDR_functions import SDR_init, SDR_LO_init, SDR_getData, SDR_setRx, SDR_setTx
from ADAR_pyadi_functions import (
    ADAR_init, ADAR_set_mode, ADAR_set_Taper, ADAR_set_Phase,
    load_phase_cal, load_gain_cal, rx_gain_code
)

try:
    import config
except ImportError:
    print("config.py not found")
    sys.exit(1)

from phaser_functions import load_hb100_cal, rx_freq_source
from SDR_functions import load_channel_cal

import phaser_cw_radar  # CW Doppler radar helpers (additive; sweep path unchanged)
import phaser_radar_dsp  # pure radar DSP/config; importable without pyadi-iio
import phaser_fmcw_radar  # FMCW radar helpers (C0: capability probe only)
from phaser_ctf import CtfMode, peak_angle_centroid  # GRCon26 CTF mode (additive)


# GUI shutdown. The physical button on the Phaser is a gpio-shutdown overlay
# (GPIO21) that makes the kernel emit KEY_POWER, which logind turns into exactly
# this command -- so triggering it from the browser is the same clean poweroff
# rather than a second, parallel mechanism.
#
# The backend runs unprivileged and cannot do it alone: polkit refuses a process
# with no active session, and there is no blanket NOPASSWD. install.sh therefore
# installs a sudoers drop-in for this one command on every install -- shutting
# the kit down from the page it is serving is a standard feature, not an opt-in.
#
# Note what that means: the backend is unauthenticated, so anyone who can reach
# it can power the machine off. On a shared or public network that is a power
# switch for the room. Revoking is deleting /etc/sudoers.d/phaser-shutdown, but
# the next install.sh writes it back.
SHUTDOWN_CMD = ["/usr/bin/systemctl", "poweroff"]

# Legacy Mode menu values that hold the beam at one steering angle instead of
# sweeping it. Any other mode ("Beam Sweep", or a lab label such as
# "Tracking") sweeps.
HOLD_MODES = ("Static Phase", "Signal vs Time")

# A hold mode returns one point per SDR read, so updates arrive as fast as the
# receiver reads. The spectrum is ~600 KB of JSON per frame (16k bins of xf and
# max_gain) and would set the update rate -- and back up the WebSocket, which
# has no backpressure -- so there it rides along at most this often.
HOLD_FFT_INTERVAL_S = 0.2


class PhaserHeadless:
    def __init__(self, pub_port=5555, rep_port=5556, ws_port=8765, http_port=8080,
                 radar_http_port=8081, sim_mode=False):
        self.pub_port = pub_port
        self.rep_port = rep_port
        self.ws_port = ws_port
        self.http_port = http_port
        self.radar_http_port = radar_http_port
        self.sim_mode = bool(sim_mode)
        self.running = False

        # Mode dispatcher: "idle" | "sweep" | "cw_radar" | "fmcw_radar".
        # `self.sweeping` is kept as a literal attribute for backwards compatibility
        # with existing handlers and any code that assigns to it directly.
        # _set_mode() keeps the two in sync.
        self.mode = "idle"
        self.sweeping = False
        self._shutdown_permitted = None   # probed lazily, then cached

        # CW radar runtime state
        self.cw_params = {}                # effective config (after defaults)
        self.cw_saved_sdr = {}              # snapshot for restoration

        # FMCW radar runtime state
        self.fmcw_params = {}               # what the client asked for
        self.fmcw_cfg = None                # effective config while running
        self.fmcw_plan = None               # chirp_plan from the read-back ramp
        self.fmcw_saved = {}                # what exit_fmcw_mode has to undo
        self.fmcw_hw = None                 # FmcwHardware while running

        # Serializes mode transitions, and is held for each radar frame so a
        # transition cannot tear the hardware down under a capture. Reentrant
        # so that a frame or handler on the same thread cannot deadlock on a
        # nested _set_mode. cw_lock is the historical name.
        self.mode_lock = threading.RLock()
        self.cw_lock = self.mode_lock

        # WebSocket clients
        self.ws_clients = set()
        self.ws_lock = threading.Lock()

        # Physical constants
        self.c = 299792458

        # Calibration process tracking
        self.cal_process = None
        self.cal_task = None
        self.cal_log = []
        # The outcome of the last finished run. get_calibration_status used to
        # report a finished run exactly once and then forget it, so a client
        # that was reconnecting during that single 2s poll never learned the
        # run had failed -- the modal simply sat there. Retained until the next
        # run starts, so the answer is the same however often you ask.
        self.cal_last_result = None
        self.cal_started_at = None
        # Whether a sweep was running when the task took the hardware, so it
        # can be handed back afterwards. See _resume_sweep_after_calibration.
        self.cal_resume_sweep = False

        # Initialize hardware
        self._init_hardware()

        # ZMQ setup
        self.ctx = zmq.Context()
        self.pub_socket = self.ctx.socket(zmq.PUB)
        self.pub_socket.bind(f"tcp://*:{pub_port}")

        self.rep_socket = self.ctx.socket(zmq.REP)
        self.rep_socket.bind(f"tcp://*:{rep_port}")

        print(f"ZMQ PUB on port {pub_port}, REP on port {rep_port}")

    def _init_hardware(self, max_retries=3, retry_delay=2.0):
        """Initialize hardware exactly like phaser_gui.py, with retry on broken pipe"""

        for attempt in range(max_retries):
            try:
                self._do_init_hardware()
                return  # Success
            except BrokenPipeError as e:
                print(f"Broken pipe error (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    print(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    print("Max retries reached. Hardware init failed.")
                    raise
            except Exception as e:
                print(f"Hardware init error: {type(e).__name__}: {e}")
                if attempt < max_retries - 1:
                    print(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                else:
                    raise

    def _do_init_hardware(self):
        """Actual hardware initialization (called by _init_hardware with retry)"""

        # Load signal frequency from calibration or config
        try:
            self.SignalFreq = load_hb100_cal()
            print(f"Loaded HB100 freq: {self.SignalFreq}")
        except Exception:
            self.SignalFreq = config.SignalFreq
            print(f"Using config SignalFreq: {self.SignalFreq}")

        # Detect hostname for URI selection
        if socket.gethostname().find(".") >= 0:
            my_hostname = socket.gethostname()
        else:
            my_hostname = socket.gethostbyaddr(socket.gethostname())[0]

        if self.sim_mode:
            self.rpi_ip = "sim"
            self.sdr_ip = "sim"
        elif "phaser" in my_hostname:
            self.rpi_ip = "ip:localhost"
            self.sdr_ip = "ip:192.168.2.1"
        else:
            self.rpi_ip = "ip:phaser.local"
            self.sdr_ip = "ip:phaser.local:50901"

        print(f"Hostname: {my_hostname}, rpi: {self.rpi_ip}, sdr: {self.sdr_ip}")

        # Load configuration.
        #
        # The IF comes from rx_freq_source, not straight from config.py. It is
        # a per-unit measurement (phaser_characterize_headless.py): the usable
        # IF differs by ~200 MHz between kits, config.py is site-owned and
        # never deployed over, and the value it ships is one a kit in this
        # fleet cannot hear. Running at an unusable IF is silent -- the writes
        # are accepted and the patterns are noise -- so the source is printed.
        _if = rx_freq_source(config)
        self.Rx_freq = _if["rx_freq_hz"]
        self.Rx_freq_source = _if["source"]
        self.Rx_freq_warnings = list(_if["warnings"])
        print("IF: %.3f GHz, from %s" % (self.Rx_freq / 1e9, _if["source"]))
        for _line in _if["warnings"]:
            print("  WARNING: %s" % _line)
        self.Tx_freq = config.Tx_freq
        self.LO_freq = self.SignalFreq + self.Rx_freq
        self.SampleRate = config.SampleRate
        self.Rx_gain = config.Rx_gain
        self.Tx_gain = config.Tx_gain
        self.d = config.d
        self.Averages = getattr(config, 'Averages', 1)

        # Load calibration data (works in both real and sim — the .pkl files
        # in the repo root are loaded whether or not hardware is attached)
        self.phase_cal = load_phase_cal()
        self.gain_cal = load_gain_cal()
        self.channel_cal = load_channel_cal()

        print(f"SignalFreq: {self.SignalFreq/1e9:.3f} GHz, LO: {self.LO_freq/1e9:.3f} GHz")
        print(f"Rx_gain: {self.Rx_gain}, Tx_gain: {self.Tx_gain}")
        print(f"Phase cal: {self.phase_cal}")
        print(f"Gain cal: {self.gain_cal}")
        print(f"Channel cal: {self.channel_cal}")

        if self.sim_mode:
            print("[SIM] Using physics-based hardware stubs (no Phaser required)")
            import phaser_sim
            self.gpios = phaser_sim.make_stub_gpios()
            self.array = phaser_sim.make_stub_array()
            self.sdr = phaser_sim.SimSDR(
                self.array,
                signal_freq=self.SignalFreq,
                element_spacing=self.d,
                sample_rate=self.SampleRate,
                buffer_size=config.buffer_size,
            )
            for device in self.array.devices.values():
                ADAR_init(device)
                ADAR_set_mode(device, "rx")
        else:
            # GPIO setup (exactly like phaser_gui.py at module level)
            print("Setting up GPIOs...")
            self.gpios = adi.one_bit_adc_dac(self.rpi_ip)
            self.gpios.gpio_vctrl_1 = 1
            self.gpios.gpio_vctrl_2 = 1
            self.gpios.gpio_div_mr = 1
            self.gpios.gpio_div_s0 = 0
            self.gpios.gpio_div_s1 = 0
            self.gpios.gpio_div_s2 = 0
            self.gpios.gpio_tx_sw = 0
            time.sleep(0.5)

            # SDR init
            print("Initializing SDR...")
            self.sdr = SDR_init(
                self.sdr_ip,
                self.SampleRate,
                self.Tx_freq,
                self.Rx_freq,
                self.Rx_gain,
                self.Tx_gain,
                config.buffer_size,
            )

            print("Initializing LO...")
            SDR_LO_init(self.rpi_ip, self.LO_freq)

            # ADAR init
            print("Initializing ADAR1000...")
            time.sleep(0.5)
            self.array = adi.adar1000_array(
                uri=self.rpi_ip,
                chip_ids=["BEAM0", "BEAM1"],
                device_map=[[1], [2]],
                element_map=[[1, 2, 3, 4, 5, 6, 7, 8]],
                device_element_map={
                    1: [7, 8, 5, 6],
                    2: [3, 4, 1, 2],
                },
            )
            for device in self.array.devices.values():
                ADAR_init(device)
                ADAR_set_mode(device, "rx")

        # Channel calibration trims the two SDR Rx channels against each
        # other. It was loaded, printed and then never used -- so the sum and
        # delta beams were built from two channels with an uncorrected gain
        # mismatch. Legacy applies it inside SDR_functions (ccal[0]/ccal[1] on
        # the two rx_hardwaregain attributes); phaser_service applies it here,
        # at the caller, and this now matches.
        SDR_setRx(
            self.sdr,
            self.Rx_gain + self.channel_cal[0],
            self.Rx_gain + self.channel_cal[1],
        )

        # Free the Rx and Tx DMA, the phaser_enable pin and the ADF4159 from
        # whatever an FMCW session left behind. They outlive this process: a
        # session that crashed or was killed, or a lab script, leaves the TDD
        # channels idling low, and then no Rx buffer fills and no Tx tone
        # leaves. Before the first capture, so the sweep's buffer is not the
        # first casualty.
        self._reset_fmcw_leftovers()

        # Ask once whether this kit can run FMCW, so the radar UI can say why
        # not instead of showing an empty plot. Read-only, and never raises.
        self.fmcw_capability = self._probe_fmcw()
        if self.fmcw_capability["available"]:
            print(f"[FMCW] available ({self.fmcw_capability['ramp']}, "
                  f"{self.fmcw_capability['source']})")
        if self.fmcw_capability["reason"]:
            print(f"[FMCW] {self.fmcw_capability['reason']}")

        # Default taper (all elements equal). Through _apply_gain_cal, or the
        # array runs on uncalibrated element gains until someone happens to
        # touch a taper slider.
        self.gainList = [100, 100, 100, 100, 100, 100, 100, 100]
        ADAR_set_Taper(self.array, self._apply_gain_cal(self.gainList))

        # Per-element user phase offsets (degrees). Added to each element's
        # steering phase inside ADAR_set_Phase. Zeros by default; the
        # frontend Phase Control sliders write here via set_state.
        self.phaseList = [0.0] * 8

        # Boresight, latched now. Nothing else writes the phases until the
        # first sweep, so a radar mode -- or anything else -- started before
        # one ran found the array deaf (docs/calibration-bugs.md, bug 5).
        phaser_cw_radar.apply_boresight(self.array, self.phase_cal)

        # Sweep settings. phase_step and steer_res are DIFFERENT quantities
        # and used to be the same attribute, which is why the Phase Shift
        # Bits slider silently coarsened the sweep itself:
        #   phase_step -- the ADAR1000 phase-shifter LSB, 360/2**bits. Only
        #                 ever a quantization step handed to ADAR_set_Phase.
        #   steer_res  -- how finely the sweep steps across the scan, in
        #                 degrees of steering angle.
        # ignore_res mirrors the legacy "ignore steering resolution" switch:
        # when set, the sweep walks phase one LSB at a time instead of
        # walking angle in steer_res steps.
        self.phase_step = 2.8125  # 7 bits = 360/128
        self.steer_res = 2.8125   # degrees of steering angle per sweep point
        self.ignore_res = True
        self.steer_min = -90
        self.steer_max = 90

        # Sweep mode, as legacy's Mode menu. In HOLD_MODES each update is ONE
        # steering point at hold_angle (degrees, +theta toward the E8 end, the
        # same side the sweep plots +theta), whatever ignore_res says: legacy
        # tests the mode after res_bits, so the mode wins.
        self.sweep_mode = "Beam Sweep"
        self.hold_angle = 0.0

        # GRCon26 CTF sector-sequence mode. Passive: it only watches the
        # commanded phaseList and answers ctf_status / ctf_reset, so it has no
        # effect on the workshop app unless a browser asks for it. Flag and
        # target sequence come from the environment or gitignored sidecar
        # files — see phaser_ctf.py.
        self.ctf = CtfMode()

        # Tx mode
        self.Tx_mode = "Transmit Disabled"

        # Beam squint: BW offset (MHz) for phase calculation
        self.BW = 0  # When > 0, phases calculated for (SignalFreq - BW*1e6)

        # Digital beam weights — complex w_k = gain * exp(j*phase) applied
        # to each digital channel before summing. The two analog sub-arrays
        # (chan0, chan1) become a 2-element digital array.
        self.B0_Gain = 1.0
        self.B1_Gain = 1.0
        self.Beam0_Phase = 0.0  # degrees
        self.Beam1_Phase = 0.0  # degrees

        # Digital beamforming mode: "manual" (use B0/B1 weights above) or
        # "mvdr" (adaptive, wired in a later step). Wired here so the
        # frontend toggle plumbs through today; algorithm added with todo #4.
        self.bf_mode = "manual"
        self.mvdr_K = 128         # snapshots for covariance estimate
        self.mvdr_diag_load = 1e-3  # diagonal loading factor

        # Synthetic interferer, injected into the received IQ inside
        # do_sweep() -- on real hardware and in --sim alike. One HB100 is a
        # target; a second one as a jammer is not something a lab can
        # reliably set up, so the jammer is computed instead. See
        # _interferer_channels() for the model.
        #   power_dbfs -- level the sum beam reads when pointed straight at
        #                 it with a uniform full taper. Absolute, because the
        #                 real target's level is not known in advance.
        #   offset_hz  -- from the tracked target tone. 0 makes it coherent
        #                 with the target over the MVDR snapshot window,
        #                 which is the case MVDR handles badly.
        #   phase_deg  -- start phase. Only meaningful against the simulated
        #                 target; a real HB100's phase is not ours to set.
        #   angle_deg  -- default clear of +/-30 deg. The two sub-arrays sit
        #                 ~2 wavelengths apart, so where sin(angle) differs
        #                 from the look's by ~0.5 (+/-30 deg at boresight) a
        #                 jammer looks almost like the look direction to the
        #                 digital stage, and MVDR can only null it with huge
        #                 weights.
        self.interferer_enable = False
        self.interferer_angle_deg = -20.0
        self.interferer_power_dbfs = -10.0
        self.interferer_offset_hz = 100e3
        self.interferer_phase_deg = 0.0
        # Baseband frequency of the real target tone, re-measured every sweep
        # from the pre-injection IQ. None until the first sweep has run.
        self.target_tone_hz = None

        print("Hardware Initialization Complete.")

    def _apply_gain_cal(self, taper_values):
        """Apply gain calibration to taper values.

        Taper values arrive on the frontend's 0-100 scale and mean percent of
        full-scale VOLTAGE, which is what the labs teach. The ADAR1000 rx_gain
        register is 0-127 and not linear in voltage, so a linear % -> code map
        (legacy's `int(gainList[i] * 127 / 100 * gcal[i])`) made every taper
        milder than the slider said: one element at 50% read -3.8 dB, not -6.

        gcal is an amplitude ratio (measured at code 127), so it scales the
        target amplitude; the chip's curve is inverted after it, by
        rx_gain_code().
        """
        calibrated = []
        for idx, value in enumerate((list(taper_values) + [100] * 8)[:8]):
            gain_mult = self.gain_cal[idx] if idx < len(self.gain_cal) else 1.0
            calibrated.append(rx_gain_code(value / 100.0 * gain_mult))
        return calibrated

    def _apply_phase_cal(self, phase_values):
        """Fold the per-element phase calibration into the user's offsets.

        self.phase_cal was loaded at init, printed, and then never applied to
        anything -- the single reason a swept array here would not form a
        clean main lobe. pcal is what makes the eight elements add coherently;
        without it each element sits at its own uncorrected phase error and
        the pattern smears out into something with no recognizable beam.

        Legacy adds pcal[i] inside ADAR_set_Phase itself. This repo moved cal
        application out to the caller (phaser_service does the same thing with
        its own _apply_phase_cal), so it belongs here -- adding it in the
        helper as well would double-apply it for that caller.
        """
        base = (list(phase_values) + [0.0] * 8)[:8]
        return [
            float(base[idx] + (self.phase_cal[idx] if idx < len(self.phase_cal) else 0.0))
            for idx in range(8)
        ]

    def ConvertPhaseToSteerAngle(self, PhDelta, freq=None):
        """Convert phase delta to steering angle.

        `freq` is the frequency the phases were computed at. It defaults to
        SignalFreq, but a beam-squint sweep computes phases at
        (SignalFreq - BW) and has to invert them at the same frequency,
        otherwise the angle axis disagrees with the phases actually loaded.
        """
        if freq is None:
            freq = self.SignalFreq
        value1 = (self.c * np.radians(np.abs(PhDelta))) / (2 * 3.14159 * freq * self.d)
        clamped = max(min(1, value1), -1)
        theta = np.degrees(np.arcsin(clamped))
        return theta if PhDelta >= 0 else -theta

    def ConvertSteerAngleToPhase(self, steer_angle):
        """Convert steering angle to phase delta (inverse of ConvertPhaseToSteerAngle)"""
        # Phase delta = 2*Pi*d*sin(theta)*f/c (in degrees)
        phase_rad = 2 * 3.14159 * self.d * np.sin(np.radians(steer_angle)) * self.SignalFreq / self.c
        return np.degrees(phase_rad)

    def _mvdr_weights(self, snapshots):
        """Compute MVDR (Capon) weights for the 2-element digital sub-array.

        snapshots: complex ndarray of shape (2, K) — K IQ snapshots stacked
                   across the two sub-array outputs.

        Returns a 2-element complex weight vector w such that y = w^H · x
        is the MVDR beamformer output for the *analog-steered look
        direction*.

        Why the steering vector is [1, 1]:
        The analog ADAR stage has already phase-compensated all 8 elements
        for the current sweep angle. Consequently, a signal arriving from
        that angle produces identical (in-phase) outputs at sub-array 0
        (elements 1..4) and sub-array 1 (elements 5..8). Signals from any
        other direction produce a phase differential between the sub-arrays
        (proportional to sin(θ_src) - sin(θ_steer)) which shows up in the
        sample covariance R̂ — that's what MVDR minimizes against.

        Math (Capon 1969):
            R̂ = (1/K) X Xᴴ                          # sample covariance
            R̂ ← R̂ + δ · tr(R̂)/Nr · I               # diagonal loading
            s = [1, 1]ᵀ                              # on-target steering vector
            w = R̂⁻¹ s / (sᴴ R̂⁻¹ s)                  # MVDR weights
        """
        X = np.asarray(snapshots, dtype=np.complex128)
        K = X.shape[1]

        R = (X @ X.conj().T) / K

        # Diagonal loading — robustifies against near-singular R and steering
        # mismatch. Scaled by tr(R)/Nr so the load stays proportional to the
        # signal power (consistent units).
        Nr = R.shape[0]
        load = self.mvdr_diag_load * (np.trace(R).real / Nr)
        R = R + load * np.eye(Nr, dtype=np.complex128)

        s = np.ones((Nr, 1), dtype=np.complex128)

        Rinv = np.linalg.inv(R)
        numer = Rinv @ s
        denom = (s.conj().T @ Rinv @ s)[0, 0]
        w = numer / denom
        return w.ravel()

    def _interferer_channels(self, PhDelta, n):
        """The synthetic interferer as the two sub-array outputs would see it.

        Returns (chan0, chan1) to add to SDR_getData()'s two channels, or None
        when the interferer is off.

        The real target has already been through the ADAR phase shifters and
        the analog sub-array sums before we get its IQ, so an interferer added
        after the fact has to go through the same analog stage or MVDR would
        be nulling something no array could receive. Per element k (k=0..7):

            a_k = A * g_k * exp(j*(2*pi*k*d/lambda*sin(theta) - psi_k))

        summed over k=0..3 into chan0 and k=4..7 into chan1, times the tone.
        g_k is the commanded taper (0..1) and psi_k the commanded steering
        ramp plus the user's Phase Control offset -- the same ramp
        ADAR_set_Phase loads, but WITHOUT the phase/gain calibration. The cal
        exists to cancel each real element's own error, so a calibrated array
        behaves like an ideal one, and the ideal one is what is modeled here.
        Working from commanded values also avoids reading eight registers back
        over IIO at every sweep point.

        A = 2**11 * 10**(power_dbfs/20) / 8, so a uniform full-taper beam
        steered onto the interferer reads power_dbfs in the sum channel.

        PORTED TO JAVASCRIPT in frontend/src/sim/engine.js (interfererWeights,
        interfererTone).
        """
        if not self.interferer_enable:
            return None
        amp = (2**11) * 10.0 ** (self.interferer_power_dbfs / 20.0) / 8.0
        wavelength = self.c / self.SignalFreq
        sin_theta = np.sin(np.radians(self.interferer_angle_deg))

        w = [0j, 0j]
        for k in range(8):
            g = max(0.0, min(100.0, float(self.gainList[k]))) / 100.0
            ramp = round(k * PhDelta / self.phase_step) * self.phase_step
            psi = np.radians(ramp + float(self.phaseList[k]))
            phi = 2 * np.pi * (k * self.d / wavelength) * sin_theta
            w[0 if k < 4 else 1] += amp * g * np.exp(1j * (phi - psi))

        f = (self.target_tone_hz or 0.0) + self.interferer_offset_hz
        t = np.arange(n) / float(self.SampleRate)
        tone = np.exp(1j * (2 * np.pi * f * t + np.radians(self.interferer_phase_deg)))
        return w[0] * tone, w[1] * tone

    def _adaptive_pattern(self, PhDelta, w):
        """Response of the whole array -- analog stage plus digital weights --
        against arrival angle, for the beam held at PhDelta.

        The MVDR weights only exist as two complex numbers, which say nothing
        to a student until they are turned back into a pattern: this is where
        the null on the interferer becomes visible. Same element model as
        _interferer_channels(): commanded taper and ramp, no calibration.

        Returns angles (deg), adaptive_db for weights `w` and conventional_db
        for equal weights [0.5, 0.5] -- what MVDR's distortionless constraint
        reduces to with nothing to null. Both are divided by the mean
        sub-array gain sum(g)/2, so each reads ~0 dB at the look direction,
        and floored at -80 dB.

        PORTED TO JAVASCRIPT in frontend/src/sim/engine.js (adaptivePattern).
        """
        angles = np.arange(-90.0, 90.5, 0.5)
        wavelength = self.c / self.SignalFreq
        sin_t = np.sin(np.radians(angles))
        a = np.zeros((2, len(angles)), dtype=np.complex128)
        g_sum = 0.0
        for k in range(8):
            g = max(0.0, min(100.0, float(self.gainList[k]))) / 100.0
            g_sum += g
            ramp = round(k * PhDelta / self.phase_step) * self.phase_step
            psi = np.radians(ramp + float(self.phaseList[k]))
            phi = 2 * np.pi * (k * self.d / wavelength) * sin_t
            a[0 if k < 4 else 1] += g * np.exp(1j * (phi - psi))
        norm = max(g_sum / 2, 1e-12)

        def db(y):
            return (20 * np.log10(np.maximum(np.abs(y) / norm, 1e-4))).tolist()

        return {
            "angles": angles.tolist(),
            "adaptive_db": db(np.conj(w[0]) * a[0] + np.conj(w[1]) * a[1]),
            "conventional_db": db(0.5 * (a[0] + a[1])),
        }

    def _measure_target_tone(self, raw):
        """Baseband frequency of the strongest tone in `raw`, in Hz.

        `raw` is the pre-injection sum (chan0 + chan1) from the loudest point
        of the sweep, so the interferer can never measure itself -- which
        would walk it off by offset_hz every sweep.

        Bin resolution (fs/N, ~183 Hz) is plenty: at offset 0 a half-bin
        error turns the interferer through 0.004 cycles over a 128-snapshot
        MVDR window, which is still coherent for every purpose here.
        """
        n = len(raw)
        sp = np.abs(np.fft.fft(raw * np.blackman(n)))
        return float(np.fft.fftfreq(n, 1 / float(self.SampleRate))[int(np.argmax(sp))])

    def do_sweep(self):
        """Perform one beam sweep and return data including monopulse delta/error

        PORTED TO JAVASCRIPT, along with the helpers above it
        (_apply_gain_cal, _apply_phase_cal, ConvertPhaseToSteerAngle,
        _mvdr_weights): frontend/src/sim/engine.js runs this pipeline in the
        browser for --sim-without-a-Pi and the GitHub Pages demo. This is the
        source of truth. tests/test_sim_parity.py compares the two and fails on
        drift, so a change here needs the same change there.
        """
        max_signal = -1000
        data_fft = None
        gain = []          # Sum beam (chan1 + chan2)
        delta_gain = []    # Delta beam (chan1 - chan2)
        phase_diff = []    # Phase difference between sum and delta
        error_func = []    # Monopulse error function
        angles = []

        # Beam squint: calculate phases for (SignalFreq - BW) but measure at
        # SignalFreq.
        calc_freq = self.SignalFreq - self.BW * 1e6

        if self.sweep_mode in HOLD_MODES:
            # Hold the beam: one point, the same +i*PhDelta ramp the sweep
            # uses at that angle, so a held 30 deg reads what the sweep reads
            # at 30 deg. Phase Control offsets still add on top, as in legacy.
            SteerValues = np.array([float(self.hold_angle)])
            PhaseValues = np.degrees(
                2 * 3.14159 * self.d * np.sin(np.radians(SteerValues)) * calc_freq / self.c
            )
        elif self.ignore_res:
            # Legacy "ignore steering resolution": step the phase delta one
            # ADAR LSB at a time and let the angle axis fall out of it.
            phase_limit = (
                int(225 / self.phase_step) * self.phase_step + self.phase_step
            )
            PhaseValues = np.arange(-phase_limit, phase_limit, self.phase_step)
            SteerValues = np.array(
                [self.ConvertPhaseToSteerAngle(ph, calc_freq) for ph in PhaseValues]
            )
        else:
            # Step the steering angle, and derive the phase each angle needs.
            steer_res = max(self.steer_res, 0.1)
            SteerValues = np.arange(
                self.steer_min, self.steer_max + steer_res, steer_res
            )
            PhaseValues = np.degrees(
                2 * 3.14159 * self.d * np.sin(np.radians(SteerValues)) * calc_freq / self.c
            )

        # Per-element phase offsets: the user's Phase Control sliders PLUS the
        # phase calibration. ADAR_set_Phase adds these to the i*PhDelta
        # steering ramp per element.
        phaseList = self._apply_phase_cal(self.phaseList)

        # Loudest pre-injection buffer of this sweep; feeds target_tone_hz.
        best_raw = None
        best_raw_pow = -1.0
        # Last MVDR weights computed; a held beam has one point, so these are
        # the weights behind the adaptive pattern.
        last_w = None

        for i, PhDelta in enumerate(PhaseValues):
            ADAR_set_Phase(self.array, PhDelta, self.phase_step, phaseList)

            # Average multiple reads if configured
            total_sum = 0
            total_delta = 0
            total_phase = 0

            for _ in range(self.Averages):
                data = SDR_getData(self.sdr)
                chan1 = np.asarray(data[0])
                chan2 = np.asarray(data[1])

                raw = chan1 + chan2
                raw_pow = float(np.vdot(raw, raw).real)
                if raw_pow > best_raw_pow:
                    best_raw_pow = raw_pow
                    best_raw = raw

                # Inject before any digital weights, so MVDR sees the
                # interferer in its covariance like any received signal.
                interf = self._interferer_channels(PhDelta, len(chan1))
                if interf is not None:
                    chan1 = chan1 + interf[0]
                    chan2 = chan2 + interf[1]

                # Compute digital beamforming weights (2 complex scalars,
                # one per digital sub-array). Manual: user-set sliders.
                # MVDR: Capon-optimal weights derived from covariance of
                # the current samples with the current sweep angle as
                # the desired look direction.
                if self.bf_mode == "mvdr":
                    # Build snapshot matrix X (2 x K) from the current SDR
                    # read. One SDR read gives buffer_size samples per
                    # channel; use the first mvdr_K of them as our K IQ
                    # snapshots — much cheaper than K separate SDR reads
                    # and mathematically equivalent.
                    K = int(min(self.mvdr_K, len(chan1)))
                    X = np.vstack([chan1[:K], chan2[:K]]).astype(np.complex128)
                    w = self._mvdr_weights(X)
                    last_w = w
                    # Apply weights: y = w^H · x, per-sample
                    sum_chan = np.conj(w[0]) * chan1 + np.conj(w[1]) * chan2
                    # MVDR produces one optimal beam; no natural delta output.
                    delta_chan = np.zeros_like(sum_chan)
                else:
                    # Conventional (manual) digital beamformer: two complex
                    # scalars applied to the two digital channels before summing.
                    w0 = self.B0_Gain * np.exp(1j * np.deg2rad(self.Beam0_Phase))
                    w1 = self.B1_Gain * np.exp(1j * np.deg2rad(self.Beam1_Phase))
                    chan1 = chan1 * w0
                    chan2 = chan2 * w1
                    sum_chan = chan1 + chan2
                    delta_chan = chan1 - chan2

                if self.interferer_enable:
                    # Two tones beat, so the sum channel's envelope repeats
                    # and "the peak sample" becomes a tie decided by
                    # rounding. Use in-band RMS instead: the same level as
                    # the peak sample for a lone constant-envelope tone, and
                    # it counts the interferer, which is the point -- the
                    # jammer has to show in the beam pattern for MVDR to
                    # visibly remove it. (Reading only the target's FFT bin
                    # would filter an off-frequency jammer out entirely.)
                    # The monopulse phase becomes that of the sum/delta
                    # cross-correlation, which a lone tone reads identically.
                    n_s = len(sum_chan)
                    cross = np.vdot(delta_chan, sum_chan) / n_s
                    s_val = np.sqrt(np.vdot(sum_chan, sum_chan).real / n_s)
                    d_val = np.sqrt(np.vdot(delta_chan, delta_chan).real / n_s)
                    beam_phase_src = cross
                else:
                    # Find peak in sum channel
                    max_index = np.argmax(np.abs(sum_chan))
                    s_val = sum_chan[max_index]
                    d_val = delta_chan[max_index]
                    beam_phase_src = s_val * np.conj(d_val)

                # Sum beam magnitude
                s_mag_sum = max(np.abs(s_val), 1e-15)
                s_dbfs_sum = 20 * np.log10(s_mag_sum / (2**11))

                # Delta beam magnitude
                s_mag_delta = max(np.abs(d_val), 1e-15)
                s_dbfs_delta = 20 * np.log10(s_mag_delta / (2**11))

                # Phase difference between sum and delta.
                #
                # Taken as the angle of sum * conj(delta), NOT as
                # angle(sum) - angle(delta). The subtraction spans (-2pi, 2pi)
                # while the quantity is only meaningful mod 2pi, so the same
                # physical angle came out as +pi/2 or -3pi/2 depending on which
                # side of the +/-pi branch cut the two angles landed. The next
                # line takes sign() of this, and those two read oppositely, so
                # the monopulse error curve inverted at random.
                #
                # It was not a rare edge case: sign(A-B) disagrees with the
                # physical phase whenever |A-B| > pi, which is 1 in 4 for
                # uniformly distributed phases -- and they are uniform here,
                # because max_index is an argmax over a flat-envelope CW tone,
                # so which sample wins (and the absolute carrier phase there)
                # is set by noise.
                #
                # The product form also removes the dependence on that sample
                # choice: sum and delta share the carrier, so it cancels in the
                # product and only the sub-array geometry survives. Exactly so
                # with no noise; with noise the value still jitters by ~0.03 rad
                # sample to sample, but continuously, with no 2pi steps.
                beam_phase = np.angle(beam_phase_src)

                total_sum += s_dbfs_sum
                total_delta += s_dbfs_delta
                total_phase += beam_phase

            # Average over all samples
            avg_sum = total_sum / self.Averages
            avg_delta = total_delta / self.Averages
            avg_phase = total_phase / self.Averages

            gain.append(avg_sum)
            delta_gain.append(avg_delta)
            phase_diff.append(float(avg_phase))

            # Compute error function for monopulse tracking
            # error = sign(phase) * (sum - delta) / (sum + delta)
            denom = avg_sum + avg_delta
            if abs(denom) > 0.001:
                err = np.sign(avg_phase) * (avg_sum - avg_delta) / denom
                # Clamp to reasonable range
                err = max(-1.0, min(1.0, err))
            else:
                err = 0.0
            error_func.append(float(err))

            angles.append(float(SteerValues[i]))

            if avg_sum > max_signal:
                max_signal = avg_sum
                data_fft = sum_chan

        if best_raw is not None:
            self.target_tone_hz = self._measure_target_tone(best_raw)

        result = {
            "ArrayGain": [float(x) for x in gain],
            "ArrayDelta": [float(x) for x in delta_gain],
            "PhaseDiff": phase_diff,
            "ErrorFunc": error_func,
            "ArrayAngle": [float(x) for x in angles],
            "peak_signal": float(max_signal),
            # Where the SOURCE is, for CTF tracking mode. Computed here rather
            # than in the browser because the flag is scored backend-side, and
            # a client-computed sector would be trivially spoofable.
            "peak_angle_deg": peak_angle_centroid(angles, gain),
            # Echoed so the frontend can tell a held beam from a sweep, and
            # spot a backend too old to know the mode.
            "sweep_mode": self.sweep_mode,
            "hold_angle_deg": float(self.hold_angle),
            # Measured from pre-injection IQ, so it is the real target even
            # with the interferer on. The FFT plot marks both tones from these.
            "target_tone_hz": self.target_tone_hz,
            "interferer_freq_hz": (
                (self.target_tone_hz or 0.0) + self.interferer_offset_hz
                if self.interferer_enable else None
            ),
        }

        if self.sweep_mode in HOLD_MODES and last_w is not None:
            # Every held update, not throttled with the spectrum: it is 361
            # points, and it should move as the weights do.
            result["adaptive_pattern"] = self._adaptive_pattern(PhaseValues[0], last_w)

        now = time.monotonic()
        last_fft = getattr(self, "_last_fft_t", None)
        if (self.sweep_mode not in HOLD_MODES or last_fft is None
                or now - last_fft >= HOLD_FFT_INTERVAL_S):
            self._last_fft_t = now
            # FFT at peak angle
            NumSamples = len(data_fft)
            win = np.blackman(NumSamples)
            y = data_fft * win
            sp = np.absolute(np.fft.fft(y))
            sp = np.fft.fftshift(sp)
            s_mag = np.abs(sp) / np.sum(win)
            s_mag = np.maximum(s_mag, 10**-15)
            max_gain = 20 * np.log10(s_mag / (2**11))

            ts = 1 / float(self.SampleRate)
            xf = np.fft.fftfreq(NumSamples, ts)
            xf = np.fft.fftshift(xf)

            result["max_gain"] = max_gain.tolist()
            result["xf"] = xf.tolist()
        return result

    # --- Mode dispatcher --------------------------------------------------

    # Modes that reconfigure the hardware, as {mode: (enter, exit)} method
    # names. idle and sweep need neither: the sweep path is the baseline every
    # radar mode's exit restores.
    _MODE_HANDLERS = {
        "cw_radar": ("_enter_cw", "_exit_cw"),
        "fmcw_radar": ("_enter_fmcw", "_exit_fmcw"),
    }

    def _set_mode(self, new_mode):
        """Single source of truth for mode transitions.

        Always leaves the old mode before entering the new one, so CW -> FMCW
        is a full restore and then a full enter, never a half-swap. A failed
        enter is unwound by that mode's own exit, which tolerates a half-entered
        mode, and lands in idle. Keeps `self.sweeping` in sync for legacy code.

        Runs under mode_lock, which the main loop also holds for each radar
        frame, so a transition never tears the hardware down mid-capture.
        """
        if new_mode == self.mode:
            return {"status": "ok", "mode": self.mode}

        with self._get_mode_lock():
            # Leave current mode
            handlers = self._MODE_HANDLERS.get(self.mode)
            if handlers:
                try:
                    getattr(self, handlers[1])()
                except Exception as e:
                    print(f"[MODE] leaving {self.mode} failed: {e}")
            self.mode = "idle"
            self.sweeping = False

            # Enter new mode
            handlers = self._MODE_HANDLERS.get(new_mode)
            if handlers:
                try:
                    getattr(self, handlers[0])()
                except Exception as e:
                    print(f"[MODE] entering {new_mode} failed: {e}")
                    # Unwind whatever did land. enter_cw_mode moves the LO and
                    # the taper before it touches the SDR, so a failure partway
                    # through used to leave the array illuminated for radar and
                    # the LO parked on the CW mixing frequency -- and the next
                    # sweep then saw nothing, with no error to explain it.
                    try:
                        getattr(self, handlers[1])()
                    except Exception as unwind_err:
                        print(f"[MODE] unwind after failed enter failed: {unwind_err}")
                    return {"status": "error",
                            "message": f"entering {new_mode} failed: {e}"}

            self.mode = new_mode
            self.sweeping = (new_mode == "sweep")
            print(f"[MODE] -> {new_mode}")
            return {"status": "ok", "mode": new_mode}

    # --- CW Radar handlers ------------------------------------------------

    def _cw_params_with_kit_if(self):
        """cw_params with center_freq defaulted to this kit's characterized IF.

        As FMCW does (_fmcw_effective_cfg): the DEFAULTS value is one kit's IF,
        and kits differ by ~200 MHz, so it leaves some deaf. One the client
        sent wins; None (the radar UI's empty field) means "the kit's IF".
        """
        params = dict(self.cw_params or {})
        if params.get("center_freq") is None:
            params["center_freq"] = self.Rx_freq
        return params

    def _enter_cw(self):
        self.cw_saved_sdr = {}
        self.cw_params = phaser_cw_radar.enter_cw_mode(
            self.sdr,
            self._cw_params_with_kit_if(),
            self.cw_saved_sdr,
            array=self.array,
            rpi_ip=self._cw_rpi_ip(),
            phase_cal=getattr(self, "phase_cal", None),
        )
        self._sim_set_cw(True)

    def _exit_cw(self):
        try:
            self._restore_from_cw()
        finally:
            self.cw_saved_sdr = {}
            self._sim_set_cw(False)

    def _get_mode_lock(self):
        """mode_lock, created on first use for an object built without
        __init__ (the tests build PhaserHeadless with __new__ and set only the
        historical cw_lock)."""
        lock = getattr(self, "mode_lock", None)
        if lock is None:
            lock = getattr(self, "cw_lock", None) or threading.RLock()
            self.mode_lock = lock
        return lock

    # --- FMCW Radar handlers ----------------------------------------------

    # Params that only change processing; they apply on the next frame.
    _FMCW_LIVE_KEYS = frozenset({
        "mti", "fft_window", "range_max", "vel_max", "zero_range_freq", "peak_exclude",
        "cfar", "cfar_guard", "cfar_ref", "cfar_bias_db",
    })
    # Params written straight to the hardware while running, as CW does.
    _FMCW_WRITE_KEYS = frozenset({"taper", "rx_gain", "tx_gain"})

    def _fmcw_effective_cfg(self):
        """The config the next enter would use, and its preset name.

        center_freq defaults to this kit's characterized IF, never a preset's:
        kits differ by ~200 MHz, and a fixed value is out of some kits'
        passband (docs/calibration-bugs.md). One the client sent wins.
        """
        params = dict(getattr(self, "fmcw_params", None) or {})
        preset = params.pop("preset", None) or self._default_fmcw_preset()
        # None means "this kit's IF" too: the radar UI sends it for an empty
        # Center Freq field, as it does for CW, so a value typed earlier and
        # then cleared does not linger in fmcw_params.
        if params.get("center_freq") is None:
            params["center_freq"] = self.Rx_freq
        # And None range_max means the preset's own (range_doppler_far shows
        # 100 m), not "no crop": the UI sends it for an empty field.
        if "range_max" in params and params["range_max"] is None:
            del params["range_max"]
        return phaser_radar_dsp.fmcw_config(params, preset=preset), preset

    def _default_fmcw_preset(self):
        """range_doppler, or the single-chirp range lab on a kit whose probe
        found no chirp sync (range_doppler needs it and would refuse)."""
        cap = getattr(self, "fmcw_capability", None) or {}
        if cap.get("available") and cap.get("ramp") != phaser_radar_dsp.RAMP_SYNCED:
            return phaser_radar_dsp.FMCW_FALLBACK_PRESET
        return phaser_radar_dsp.FMCW_DEFAULT_PRESET

    def _enter_fmcw(self):
        cap = getattr(self, "fmcw_capability", None) or {}
        if not cap.get("available"):
            raise RuntimeError(cap.get("reason") or "FMCW is not available on this kit")
        ramp_mode = cap["ramp"]
        synced = ramp_mode == phaser_radar_dsp.RAMP_SYNCED

        cfg, preset = self._fmcw_effective_cfg()
        if not synced and int(cfg["num_chirps"]) > 1:
            raise RuntimeError(
                f"preset {preset!r} uses {int(cfg['num_chirps'])} chirps; range-Doppler "
                f"needs chirp sync, which this kit lacks ({cap.get('reason')})")

        self.fmcw_saved = {}
        if self.sim_mode:
            import phaser_sim
            self.fmcw_hw = phaser_sim.make_stub_fmcw_hardware(self.gpios)
        else:
            self.fmcw_hw = phaser_fmcw_radar.FmcwHardware.open(
                adi, self.rpi_ip, self.sdr_ip, self.gpios, synced=synced)

        self.fmcw_cfg, self.fmcw_plan = phaser_fmcw_radar.enter_fmcw_mode(
            self.sdr, cfg, self.fmcw_saved, self.fmcw_hw,
            array=self.array, ramp_mode=ramp_mode,
            phase_cal=getattr(self, "phase_cal", None))
        self.fmcw_preset = preset
        self._sim_set_fmcw(True)
        plan = self.fmcw_plan
        print(f"[FMCW] {preset}: {plan['ramp_mode']}, {plan['num_chirps']} chirp(s), "
              f"ramp {plan['ramp_time'] * 1e6:.0f} us, buffer {plan['rx_buffer_size']}")

    def _exit_fmcw(self):
        """Put the SDR, ramp, LO, TDD and array back for the sweep path.

        The taper goes back through _apply_gain_cal for the same reason as
        _restore_from_cw. The LO goes back through the held ADF4159 handle.
        """
        try:
            if getattr(self, "fmcw_hw", None) is not None or getattr(self, "fmcw_saved", None):
                phaser_fmcw_radar.exit_fmcw_mode(
                    self.sdr,
                    self.fmcw_saved,
                    self.fmcw_hw,
                    array=self.array,
                    restore_taper=self._apply_gain_cal(self.gainList),
                    restore_lo_freq=self.LO_freq,
                )
        finally:
            self.fmcw_hw = None
            self.fmcw_saved = {}
            self.fmcw_cfg = None
            self.fmcw_plan = None
            self._sim_set_fmcw(False)

    def _sim_set_fmcw(self, enable):
        """Point the simulator at the FMCW scene, laid out by the plan."""
        if not self.sim_mode or not hasattr(self.sdr, "set_fmcw_mode"):
            return
        try:
            if enable:
                cfg = self.fmcw_cfg
                self.sdr.set_fmcw_mode(
                    True,
                    plan=self.fmcw_plan,
                    chirp_bw=cfg["chirp_bw"],
                    signal_freq=cfg["signal_freq"],
                    output_freq=cfg["output_freq"],
                )
            else:
                self.sdr.set_fmcw_mode(False)
        except Exception as e:
            print(f"[FMCW] sim set_fmcw_mode failed: {e}")

    def start_fmcw_radar(self, params):
        """Switch to FMCW radar. `params` may name a `preset` (a lab).

        Already running, it applies `params` the way set_fmcw_radar_params
        does. It used to store them and return ok without touching the
        running config, so a Start from a reloaded page or a second tab
        silently kept the old preset.
        """
        if self.mode == "fmcw_radar" and params:
            return self.set_fmcw_radar_params(params)
        if params:
            self.fmcw_params = {**(getattr(self, "fmcw_params", None) or {}), **params}
        return self._set_mode("fmcw_radar")

    def stop_fmcw_radar(self):
        """Leave FMCW radar mode (back to idle)."""
        if self.mode != "fmcw_radar":
            return {"status": "ok", "mode": self.mode}
        return self._set_mode("idle")

    def set_fmcw_radar_params(self, params):
        """Update FMCW parameters.

        Processing params apply on the next frame, and the taper and gains are
        written straight away, as in CW. Anything else -- preset, ramp, timing,
        frequencies, chirp count -- changes the plan, so while running the mode
        is left and re-entered here rather than leaving that to the client:
        FMCW has too many such knobs for a stop/start in the UI to be pleasant.
        """
        if not params:
            return {"status": "ok"}
        self.fmcw_params = {**(getattr(self, "fmcw_params", None) or {}), **params}
        if self.mode != "fmcw_radar":
            return {"status": "ok", "params": self.fmcw_params, "reentered": False}

        keys = set(params)
        if keys - self._FMCW_LIVE_KEYS - self._FMCW_WRITE_KEYS:
            self._set_mode("idle")
            resp = self._set_mode("fmcw_radar")
            return {**resp, "params": self.fmcw_params, "reentered": True}

        with self._get_mode_lock():
            if self.mode != "fmcw_radar" or self.fmcw_cfg is None:
                return {"status": "ok", "params": self.fmcw_params, "reentered": False}
            live = dict(params)
            if "range_max" in live and live["range_max"] is None:
                live["range_max"] = phaser_radar_dsp.fmcw_config(
                    preset=getattr(self, "fmcw_preset", None))["range_max"]
            self.fmcw_cfg = {**self.fmcw_cfg, **live}
            try:
                if "taper" in params:
                    phaser_cw_radar.apply_taper(self.array, params["taper"])
                if "rx_gain" in params:
                    self.sdr.rx_hardwaregain_chan0 = int(params["rx_gain"])
                    if hasattr(self.sdr, "rx_hardwaregain_chan1"):
                        self.sdr.rx_hardwaregain_chan1 = int(params["rx_gain"])
                if "tx_gain" in params and hasattr(self.sdr, "tx_hardwaregain_chan1"):
                    self.sdr.tx_hardwaregain_chan1 = int(params["tx_gain"])
            except Exception as e:
                print(f"[FMCW] live param update failed: {e}")
                return {"status": "error", "message": str(e)}
        return {"status": "ok", "params": self.fmcw_params, "reentered": False}

    def get_fmcw_radar_state(self):
        """FMCW config, plan and capability, running or not."""
        running = self.mode == "fmcw_radar"
        cfg, preset, error = getattr(self, "fmcw_cfg", None), getattr(self, "fmcw_preset", None), None
        if not running or cfg is None:
            try:
                cfg, preset = self._fmcw_effective_cfg()
            except ValueError as e:
                cfg, error = None, str(e)
        data = {
            "mode": self.mode,
            "running": running,
            "preset": preset,
            # In the UI's order (FMCW_PRESET_UI), then any preset it lacks.
            "presets": [p for p in phaser_radar_dsp.FMCW_PRESET_UI
                        if p in phaser_radar_dsp.FMCW_PRESETS]
                       + sorted(set(phaser_radar_dsp.FMCW_PRESETS)
                                - set(phaser_radar_dsp.FMCW_PRESET_UI)),
            # Per preset, what the UI needs to know before Start: chirp count
            # decides whether MTI (multi-chirp) or CFAR (single-chirp) applies.
            "preset_info": {
                name: {
                    "num_chirps": int(phaser_radar_dsp.fmcw_config(preset=name)["num_chirps"]),
                    "label": phaser_radar_dsp.FMCW_PRESET_UI.get(name, (name, ""))[0],
                    "hint": phaser_radar_dsp.FMCW_PRESET_UI.get(name, (name, ""))[1],
                }
                for name in phaser_radar_dsp.FMCW_PRESETS
            },
            "params": dict(getattr(self, "fmcw_params", None) or {}),
            "cfg": cfg,
            "plan": getattr(self, "fmcw_plan", None) if running else None,
            "capability": self.get_radar_capabilities()["data"]["fmcw"],
        }
        if error:
            data["error"] = error
        return {"status": "ok", "data": data}

    def do_fmcw_radar_frame(self):
        """Capture and process one FMCW frame. Returns the broadcast payload.

        The peaks and timings, and the arrays the plots need: the encoded
        range-Doppler map or the range profile (and CFAR). The format is in
        docs/fmcw-phase-c-plan.md, C3.
        """
        cfg, plan = self.fmcw_cfg, self.fmcw_plan
        t0 = time.perf_counter()
        data = phaser_fmcw_radar.capture_fmcw_frame(self.sdr, self.fmcw_hw, plan)
        t1 = time.perf_counter()

        fs = plan["sample_rate"]
        ramp = plan["effective_ramp_time"]
        zero = cfg.get("zero_range_freq")
        if zero is None:
            zero = cfg["signal_freq"]
        window = cfg.get("fft_window", "blackman")
        out = {
            "preset": getattr(self, "fmcw_preset", None),
            "ramp_mode": plan["ramp_mode"],
            "num_chirps": plan["num_chirps"],
            "pri_ms": plan["pri_ms"],
            "ramp_time": plan["ramp_time"],
        }

        if plan["synced"] and plan["num_chirps"] > 1:
            matrix = phaser_radar_dsp.mti_filter(data, cfg.get("mti", "none"))
            rd = phaser_radar_dsp.range_doppler_map(
                matrix, fs, cfg["chirp_bw"], ramp, plan["pri_ms"] / 1e3,
                cfg["output_freq"], zero_range_freq=zero, fft_window=window,
                range_max=cfg.get("range_max"), vel_max=cfg.get("vel_max"),
                peak_exclude=cfg.get("peak_exclude", 0.0))
            out.update({
                "kind": "range_doppler",
                "peak_range": rd["peak_range"],
                "peak_velocity": rd["peak_velocity"],
                "peak_magnitude_db": rd["peak_magnitude_db"],
                "range_resolution": rd["range_resolution"],
                "velocity_resolution": rd["velocity_resolution"],
                "max_velocity": rd["max_velocity"],
                "map": phaser_radar_dsp.encode_db_u8(rd["map_db"]),
                "range_axis": phaser_radar_dsp.rounded(
                    rd["range_axis"], phaser_radar_dsp.AXIS_DECIMALS),
                "velocity_axis": phaser_radar_dsp.rounded(
                    rd["velocity_axis"], phaser_radar_dsp.AXIS_DECIMALS),
            })
        else:
            iq = data[:, 0] if np.ndim(data) == 2 else data
            prof = phaser_radar_dsp.range_profile(
                iq, fs, cfg["chirp_bw"], ramp, zero_range_freq=zero,
                fft_window=window, range_max=cfg.get("range_max"))
            out.update({
                "kind": "range_profile",
                "peak_range": prof["peak_range"],
                "peak_magnitude_db": prof["peak_magnitude_db"],
                "range_resolution": prof["range_resolution"],
                "max_range": prof["max_range"],
                "range_axis": phaser_radar_dsp.rounded(
                    prof["range_axis"], phaser_radar_dsp.AXIS_DECIMALS),
                "profile_db": phaser_radar_dsp.rounded(
                    prof["profile_db"], phaser_radar_dsp.DB_DECIMALS),
            })
            if cfg.get("cfar"):
                spectrum = np.asarray(prof["profile_db"], dtype=float)
                threshold = phaser_radar_dsp.ca_cfar(
                    spectrum, num_guard=int(cfg["cfar_guard"]),
                    num_ref=int(cfg["cfar_ref"]), bias_db=float(cfg["cfar_bias_db"]))
                _, detections = phaser_radar_dsp.apply_cfar(spectrum, threshold)
                out["detections"] = int(len(detections))
                # The lab's two checkboxes: draw the threshold, then blank
                # below it. The browser does the blanking from these.
                out["cfar_threshold_db"] = phaser_radar_dsp.rounded(
                    threshold, phaser_radar_dsp.DB_DECIMALS)
                out["detection_indices"] = [int(i) for i in detections]

        out["capture_ms"] = (t1 - t0) * 1e3
        out["process_ms"] = (time.perf_counter() - t1) * 1e3
        return out

    def _cw_rpi_ip(self):
        """The Phaser IP to program the ADF4159 through, or None in sim mode.

        Sim mode has no libiio to reach, so the LO write is skipped there; the
        simulator is told the CW carrier directly via _sim_set_cw instead.
        """
        return None if self.sim_mode else self.rpi_ip

    def _restore_from_cw(self):
        """Put the SDR, LO and array back the way the sweep path expects them.

        The taper goes back through _apply_gain_cal, not raw: self.gainList is
        on the frontend's 0-100 scale and the ADAR1000 register is 0-127, so
        restoring it raw would leave the array at 79% of full scale with the
        gain calibration dropped. apply_taper writes register codes, which is
        the right scale for the CW presets and the wrong one for gainList.
        """
        phaser_cw_radar.exit_cw_mode(
            self.sdr,
            self.cw_saved_sdr,
            array=self.array,
            rpi_ip=self._cw_rpi_ip(),
            restore_taper=self._apply_gain_cal(self.gainList),
            restore_lo_freq=self.LO_freq,
        )

    def _sim_set_cw(self, enable):
        """Point the simulator at the CW scene (no-op on real hardware)."""
        if not self.sim_mode or not hasattr(self.sdr, "set_cw_mode"):
            return
        cfg = {**phaser_radar_dsp.DEFAULTS, **(self.cw_params or {})}
        try:
            self.sdr.set_cw_mode(
                enable,
                signal_freq=cfg["signal_freq"],
                output_freq=cfg["output_freq"],
            )
        except Exception as e:
            print(f"[CW] sim set_cw_mode failed: {e}")

    def start_cw_radar(self, params):
        """Switch to CW radar mode. If a sweep is running, it is stopped first."""
        # Merge requested params on top of stored ones (with defaults applied later
        # by phaser_cw_radar.enter_cw_mode).
        if params:
            self.cw_params = {**(self.cw_params or {}), **params}
        return self._set_mode("cw_radar")

    def stop_cw_radar(self):
        """Leave CW radar mode (back to idle)."""
        if self.mode != "cw_radar":
            return {"status": "ok", "mode": self.mode}
        return self._set_mode("idle")

    def set_cw_radar_params(self, params):
        """Live-update CW radar parameters. If running, applies on the next frame
        for processing-only changes (window, etc.); hardware changes (sample rate,
        FFT size, gains, freqs) require a stop/start cycle."""
        if not params:
            return {"status": "ok"}
        self.cw_params = {**(self.cw_params or {}), **params}
        # Apply gain changes live if running
        if self.mode == "cw_radar":
            try:
                # The taper is an array write, not an SDR one, and it is live:
                # the frontend's Rect/Hann/Black buttons pushed it here and it
                # went no further than this dict until now.
                if "taper" in params:
                    gains = phaser_cw_radar.apply_taper(self.array, params["taper"])
                    if gains is not None:
                        print(f"[CW] Taper applied: {gains}")
                if "signal_freq" in params or "output_freq" in params:
                    self._sim_set_cw(True)
                if "rx_gain" in params:
                    self.sdr.rx_hardwaregain_chan0 = int(params["rx_gain"])
                    if hasattr(self.sdr, "rx_hardwaregain_chan1"):
                        try:
                            self.sdr.rx_hardwaregain_chan1 = int(params["rx_gain"])
                        except Exception:
                            pass
                if "tx_gain" in params and hasattr(self.sdr, "tx_hardwaregain_chan1"):
                    try:
                        self.sdr.tx_hardwaregain_chan1 = int(params["tx_gain"])
                    except Exception:
                        pass
            except Exception as e:
                print(f"[CW] live param update failed: {e}")
                return {"status": "error", "message": str(e)}
        return {"status": "ok", "params": self.cw_params}

    def _reset_fmcw_leftovers(self):
        """Undo what an FMCW session that never exited cleanly left behind.

        Three pieces of state outlive the process, and FMCW exit resets each:
        the Pluto TDD (release_tdd), the Pluto's phaser_enable pin, and the
        ADF4159 ramp. Each is reset on its own, and none raises: a Pluto
        without the TDD core or the pin (older firmware) has nothing to reset,
        and must still come up for beamforming.
        """
        if self.sim_mode:
            return
        if hasattr(adi, "tddn"):
            try:
                phaser_fmcw_radar.release_tdd(adi.tddn(self.sdr_ip))
                print("[TDD] Pluto TDD released (non-TDD mode)")
            except Exception as e:
                print(f"[TDD] No Pluto TDD to release: {e}")
        try:
            pins = adi.one_bit_adc_dac(self.sdr_ip)
            if hasattr(pins, "gpio_phaser_enable"):
                pins.gpio_phaser_enable = False
                print("[TDD] Pluto phaser_enable cleared")
        except Exception as e:
            print(f"[TDD] No Pluto phaser_enable to clear: {e}")
        try:
            phaser_fmcw_radar.stop_ramp(adi.adf4159(self.rpi_ip))
            print("[LO] ADF4159 ramp disabled")
        except Exception as e:
            print(f"[LO] Could not disable the ADF4159 ramp: {e}")

    def _probe_fmcw(self):
        """Run the FMCW capability probe against the contexts already open."""
        return phaser_fmcw_radar.probe_fmcw(
            adi,
            sdr_ctx=getattr(self.sdr, "_ctx", None),
            rpi_ctx=getattr(self.gpios, "_ctx", None),
            sim=self.sim_mode,
        )

    def get_radar_capabilities(self):
        """Which radar modes this kit can run. CW needs nothing beyond the
        stock kit; FMCW is whatever the startup probe found."""
        fmcw = getattr(self, "fmcw_capability", None) or {
            "available": False, "ramp": None, "source": "hardware",
            "reason": "not probed yet", "checks": [],
        }
        return {"status": "ok", "data": {"fmcw": fmcw}}

    def get_cw_radar_state(self):
        """Return current CW radar config and mode."""
        cfg = {**phaser_radar_dsp.DEFAULTS, **(self.cw_params or {})}
        return {
            "status": "ok",
            "data": {
                "mode": self.mode,
                "running": self.mode == "cw_radar",
                "params": cfg,
            },
        }

    def do_cw_radar_frame(self):
        """Capture and process a single CW radar frame. Returns the broadcast payload."""
        cfg = {**phaser_radar_dsp.DEFAULTS, **(self.cw_params or {})}
        iq = phaser_cw_radar.capture_cw_frame(self.sdr)
        return phaser_radar_dsp.process_cw_frame(
            iq,
            fs=cfg["sample_rate"],
            signal_freq=cfg["signal_freq"],
            output_freq=cfg["output_freq"],
            fft_window=cfg.get("fft_window", "blackman"),
            vel_max=cfg.get("vel_max"),
            peak_exclude=cfg.get("peak_exclude"),
        )

    def shutdown_permitted(self):
        """Whether sudo will run the poweroff command without a password.

        `sudo -l <cmd>` answers that without running anything. Cached: the
        answer only changes when the sudoers drop-in does, and install.sh
        restarts the service whenever it writes one.
        """
        if self._shutdown_permitted is None:
            try:
                probe = subprocess.run(
                    ["sudo", "-n", "-l"] + SHUTDOWN_CMD,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                self._shutdown_permitted = probe.returncode == 0
            except Exception:
                self._shutdown_permitted = False
        return self._shutdown_permitted

    def get_state(self):
        """Return current configuration state"""
        return {
            "status": "ok",
            "data": {
                "SignalFreq": self.SignalFreq,
                "Rx_freq": self.Rx_freq,
                # Where the IF came from, so the sidebar can say whether this
                # kit has actually been measured. An unverified IF is the one
                # fault that looks identical to a working kit on screen.
                "Rx_freq_source": getattr(self, "Rx_freq_source", "unknown"),
                "Rx_freq_warnings": list(getattr(self, "Rx_freq_warnings", [])),
                "Tx_freq": self.Tx_freq,
                "Rx_gain": self.Rx_gain,
                "Tx_gain": self.Tx_gain,
                "Tx_mode": self.Tx_mode,
                "gainList": self.gainList,
                "phaseList": self.phaseList,
                "phase_step": self.phase_step,
                "steer_res": self.steer_res,
                "ignore_res": self.ignore_res,
                "sweep_mode": self.sweep_mode,
                "hold_angle": self.hold_angle,
                "steer_min": self.steer_min,
                "steer_max": self.steer_max,
                "Averages": self.Averages,
                "d": self.d,
                "BW": self.BW,
                "B0_Gain": self.B0_Gain,
                "B1_Gain": self.B1_Gain,
                "Beam0_Phase": self.Beam0_Phase,
                "Beam1_Phase": self.Beam1_Phase,
                "bfMode": self.bf_mode,
                "mvdrK": self.mvdr_K,
                "mvdrDiagLoad": self.mvdr_diag_load,
                "sim_mode": self.sim_mode,
                "fmcw": self.get_radar_capabilities()["data"]["fmcw"],
                "interferer_enable": self.interferer_enable,
                "interferer_angle_deg": self.interferer_angle_deg,
                "interferer_power_dbfs": self.interferer_power_dbfs,
                "interferer_offset_hz": self.interferer_offset_hz,
                "interferer_phase_deg": self.interferer_phase_deg,
                "sweeping": self.sweeping,
                "hardware_connected": True,  # If we got here, hardware is connected
                # Lets the UI hide the affordance where it would only fail.
                "shutdown_available": self.shutdown_permitted(),
            }
        }

    def set_rx_gain(self, gain):
        """Set Rx gain and apply to hardware"""
        self.Rx_gain = int(gain)
        # Keep the per-channel trim -- setting both channels to the same raw
        # gain here silently threw away the channel calibration.
        SDR_setRx(
            self.sdr,
            self.Rx_gain + self.channel_cal[0],
            self.Rx_gain + self.channel_cal[1],
        )
        print(f"Rx gain set to {self.Rx_gain} dB")
        return {"status": "ok"}

    def set_tx_gain(self, gain):
        """Set Tx gain and apply to hardware"""
        self.Tx_gain = int(gain)
        SDR_setTx(self.sdr, self.Tx_gain)
        print(f"Tx gain set to {self.Tx_gain} dB")
        return {"status": "ok"}

    def set_signal_freq(self, freq):
        """Set signal frequency and retune LO"""
        self.SignalFreq = float(freq)
        self.LO_freq = self.SignalFreq + self.Rx_freq
        if self.sim_mode:
            self.sdr.set_signal_freq(self.SignalFreq)
        else:
            SDR_LO_init(self.rpi_ip, self.LO_freq)
        print(f"Signal freq set to {self.SignalFreq/1e9:.6f} GHz, LO: {self.LO_freq/1e9:.6f} GHz")
        return {"status": "ok"}

    def set_taper(self, gain_list):
        """Set element gains (taper)"""
        self.gainList = list(gain_list)[:8]
        ADAR_set_Taper(self.array, self._apply_gain_cal(self.gainList))
        print(f"Taper set to {self.gainList}")
        return {"status": "ok"}

    def set_tx_mode(self, mode):
        """Set Tx mode"""
        self.Tx_mode = mode
        if mode == "Transmit on OUT1":
            self.gpios.gpio_tx_sw = 0
            self.gpios.gpio_vctrl_2 = 1
        elif mode == "Transmit on OUT2":
            self.gpios.gpio_tx_sw = 1
            self.gpios.gpio_vctrl_2 = 1
        else:
            self.gpios.gpio_vctrl_2 = 1
        print(f"Tx mode set to {mode}")
        return {"status": "ok"}

    def set_sweep_params(self, steer_min=None, steer_max=None, phase_step=None, averages=None):
        """Set sweep parameters"""
        if steer_min is not None:
            self.steer_min = float(steer_min)
        if steer_max is not None:
            self.steer_max = float(steer_max)
        if phase_step is not None:
            self.phase_step = float(phase_step)
        if averages is not None:
            self.Averages = int(averages)
        print(f"Sweep params: {self.steer_min} to {self.steer_max}, step={self.phase_step}, avg={self.Averages}")
        return {"status": "ok"}

    def _read_calibration_output(self):
        """Background thread to read calibration subprocess output"""
        try:
            for line in iter(self.cal_process.stdout.readline, ''):
                if line:
                    text = line.rstrip('\n')
                    self.cal_log.append(text)
                    # Also to the journal. Without this the subprocess's output
                    # -- including any traceback -- existed only in cal_log,
                    # which a browser has to be connected at the right moment to
                    # see. An ImportError that killed the HB100 search in 50ms
                    # was invisible on the Pi for exactly this reason.
                    print(f"[CAL:{self.cal_task}] {text}", flush=True)
                if self.cal_process.poll() is not None:
                    break
            # Read any remaining output
            remaining = self.cal_process.stdout.read()
            if remaining:
                for line in remaining.strip().split('\n'):
                    if line:
                        self.cal_log.append(line)
                        print(f"[CAL:{self.cal_task}] {line}", flush=True)
        except Exception as e:
            self.cal_log.append(f"[Read error: {e}]")

    def run_calibration(self, task_name):
        """Run a calibration script as subprocess"""
        if self.cal_process is not None and self.cal_process.poll() is None:
            return {"status": "error", "message": "Calibration already running"}

        script_dir = os.path.dirname(os.path.abspath(__file__))

        if task_name == "find_hb100":
            script = os.path.join(script_dir, "phaser_find_hb100_headless.py")
        elif task_name == "phaser_cal":
            script = os.path.join(script_dir, "phaser_cal_headless.py")
        elif task_name == "characterize":
            script = os.path.join(script_dir, "phaser_characterize_headless.py")
        else:
            return {"status": "error", "message": f"Unknown calibration task: {task_name}"}

        if not os.path.exists(script):
            return {"status": "error", "message": f"Script not found: {script}"}

        print(f"Starting calibration: {task_name}")
        self.cal_task = task_name
        # Reported in the status payload. The UI keys its post-run state reload
        # on it; without it the key was constant across runs, so the reload
        # happened once ever and later runs left stale values on screen.
        self.cal_started_at = time.time()
        self.cal_log = []
        self.cal_last_result = None

        # Need to stop any active mode during calibration. _set_mode handles
        # CW teardown (restoring SDR state) before the calibration subprocess
        # tries to grab the hardware.
        was_sweeping = self.sweeping
        self.cal_resume_sweep = was_sweeping
        if self.mode != "idle":
            self._set_mode("idle")

        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            self.cal_process = subprocess.Popen(
                [sys.executable, "-u", script],
                cwd=script_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                env=env,
            )
            # Start background thread to read output
            import threading
            self.cal_reader_thread = threading.Thread(target=self._read_calibration_output, daemon=True)
            self.cal_reader_thread.start()
            return {"status": "ok", "message": f"Started {task_name}"}
        except Exception as e:
            self.cal_resume_sweep = False
            if was_sweeping:
                self._set_mode("sweep")
            return {"status": "error", "message": str(e)}

    def get_calibration_status(self):
        """Get calibration process status"""
        if self.cal_process is None:
            # Replay the last outcome rather than reporting a bare idle, so a
            # client that reconnects after a run finished still sees how it went.
            if self.cal_last_result is not None:
                return dict(self.cal_last_result)
            return {"status": "ok", "running": False, "task": None}

        if self.cal_process.poll() is None:
            # Still running - output is being read by background thread
            return {
                "status": "ok",
                "running": True,
                "task": self.cal_task,
                "started_at": getattr(self, "cal_started_at", None),
                "last_lines": self.cal_log[-20:],
            }
        else:
            # Finished - wait for reader thread to complete
            if hasattr(self, 'cal_reader_thread') and self.cal_reader_thread.is_alive():
                self.cal_reader_thread.join(timeout=1.0)

            returncode = self.cal_process.returncode
            result = {
                "status": "ok",
                "running": False,
                "task": self.cal_task,
                "started_at": getattr(self, "cal_started_at", None),
                "returncode": returncode,
                "success": returncode == 0,
                "last_lines": self.cal_log,
            }

            # Reload calibration if successful
            if returncode == 0:
                self._reload_calibration(self.cal_task)
                self._resume_sweep_after_calibration()
            else:
                print(f"[CAL:{self.cal_task}] FAILED, exit {returncode}", flush=True)
                self.cal_resume_sweep = False

            self.cal_last_result = dict(result)
            self.cal_process = None
            self.cal_task = None
            return result

    def cancel_calibration(self):
        """Cancel running calibration process"""
        if self.cal_process is not None and self.cal_process.poll() is not None:
            # It exited on its own before the cancel arrived -- which is what a
            # crash looks like from here. Harvest the outcome so the caller
            # learns why, instead of being told nothing was running.
            return self.get_calibration_status()
        if self.cal_process is None:
            if self.cal_last_result is not None:
                return dict(self.cal_last_result)
            return {"status": "error", "message": "No calibration running"}

        try:
            self.cal_process.terminate()
            self.cal_process.wait(timeout=5)
        except Exception:
            self.cal_process.kill()

        task = self.cal_task
        self.cal_process = None
        self.cal_task = None
        self.cal_log.append("Calibration canceled by user")
        print(f"Calibration {task} canceled")
        return {"status": "ok", "message": f"Canceled {task}"}

    def _resume_sweep_after_calibration(self):
        """Hand the sweep back if a calibration task interrupted one.

        run_calibration drops to idle so the subprocess can own the hardware,
        and nothing ever switched back: after Find HB100 the browser still
        showed Stop while the backend sat idle, so the plots froze until the
        user pressed Stop and Start. Only after a successful run, which is the
        one that re-initializes the hardware first.
        """
        resume = getattr(self, "cal_resume_sweep", False)
        self.cal_resume_sweep = False
        if resume and self.mode == "idle":
            self._set_mode("sweep")

    def _reload_calibration(self, task_name):
        """Re-initialize hardware after calibration script completes.

        Calibration scripts take over the hardware completely, so we need
        to re-initialize everything, not just reload cal values.
        """
        print(f"Re-initializing hardware after {task_name} calibration...")

        try:
            # Full hardware re-init to restore our configuration
            self._init_hardware()
            print("Hardware re-initialized successfully")
        except Exception as e:
            print(f"Failed to re-initialize hardware: {e}")
            print("You may need to restart phaser_headless.py")

    def handle_command(self, msg):
        """Handle a command message"""
        cmd = msg.get("cmd", "")
        data = msg.get("data", {})
        # Support flat {args} format from invoke() - merge top-level keys into data
        for k, v in msg.items():
            if k not in ("cmd", "id", "type", "data"):
                data[k] = v

        print(f"[CMD] {cmd}")

        if cmd == "ping":
            return {"status": "ok", "message": "pong"}

        elif cmd == "get_state":
            return self.get_state()

        elif cmd == "start_sweep":
            return self._set_mode("sweep")

        elif cmd == "stop_sweep":
            if self.mode == "sweep":
                return self._set_mode("idle")
            # Even if we're idle or in cw_radar, ensure sweeping is False.
            self.sweeping = False
            return {"status": "ok", "mode": self.mode}

        elif cmd == "set_rx_gain":
            return self.set_rx_gain(data.get("gain", self.Rx_gain))

        elif cmd == "set_tx_gain":
            return self.set_tx_gain(data.get("gain", self.Tx_gain))

        elif cmd == "set_signal_freq":
            return self.set_signal_freq(data.get("freq", self.SignalFreq))

        elif cmd == "set_taper":
            return self.set_taper(data.get("gainList", self.gainList))

        elif cmd == "set_tx_mode":
            return self.set_tx_mode(data.get("mode", self.Tx_mode))

        elif cmd == "set_sweep_params":
            return self.set_sweep_params(
                steer_min=data.get("steer_min"),
                steer_max=data.get("steer_max"),
                phase_step=data.get("phase_step"),
                averages=data.get("averages"),
            )

        elif cmd == "set_state":
            # Bulk state update from frontend - apply relevant settings
            state = data.get("state", {})
            if "Rx_gain" in state:
                self.set_rx_gain(state["Rx_gain"])
            if "Tx_gain" in state:
                self.set_tx_gain(state["Tx_gain"])
            # Every set_state carries SignalFreq, so retuning unconditionally
            # re-ran SDR_LO_init (a fresh ADF4159 context) on each slider drag.
            if "SignalFreq" in state and float(state["SignalFreq"]) != self.SignalFreq:
                self.set_signal_freq(state["SignalFreq"])
            if "gainList" in state:
                self.set_taper(state["gainList"])
            if "phaseList" in state:
                incoming = list(state["phaseList"])[:8]
                self.phaseList = [float(v) for v in (incoming + [0.0] * 8)[:8]]
                # Where the operator deliberately pointed the beam. Hooked
                # here rather than in do_sweep, which walks every steer angle
                # in the range by design and would swamp the state machine.
                self.ctf.observe(self.phaseList, self.ConvertPhaseToSteerAngle)
            if "Tx_mode" in state:
                self.set_tx_mode(state["Tx_mode"])
            if "Averages" in state:
                self.Averages = int(state["Averages"])
            if "d" in state:
                self.d = float(state["d"])
            if "BW" in state:
                self.BW = float(state["BW"])
            if "B0_Gain" in state:
                self.B0_Gain = float(state["B0_Gain"])
            if "B1_Gain" in state:
                self.B1_Gain = float(state["B1_Gain"])
            if "Beam0_Phase" in state:
                self.Beam0_Phase = float(state["Beam0_Phase"])
            if "Beam1_Phase" in state:
                self.Beam1_Phase = float(state["Beam1_Phase"])
            if "bfMode" in state:
                mode = str(state["bfMode"])
                if mode in ("manual", "mvdr") and mode != self.bf_mode:
                    self.bf_mode = mode
                    if mode == "mvdr":
                        print("[BF] MVDR mode selected — algorithm not yet wired; using manual weights until todo #4 lands.")
                    else:
                        print("[BF] Manual mode selected.")
            if "mvdrK" in state:
                self.mvdr_K = max(8, int(state["mvdrK"]))
            if "mvdrDiagLoad" in state:
                self.mvdr_diag_load = max(0.0, float(state["mvdrDiagLoad"]))
            # Synthetic interferer. Read by do_sweep() at the next SDR read,
            # on hardware and in sim alike; nothing to push anywhere.
            if "interferer_enable" in state:
                self.interferer_enable = bool(state["interferer_enable"])
            if "interferer_angle_deg" in state:
                v = float(state["interferer_angle_deg"])
                self.interferer_angle_deg = max(-90.0, min(90.0, v))
            if "interferer_power_dbfs" in state:
                v = float(state["interferer_power_dbfs"])
                self.interferer_power_dbfs = max(-80.0, min(0.0, v))
            if "interferer_offset_hz" in state:
                # Inside the +/- fs/2 baseband, with room for the target tone.
                lim = float(self.SampleRate) / 4
                v = float(state["interferer_offset_hz"])
                self.interferer_offset_hz = max(-lim, min(lim, v))
            if "interferer_phase_deg" in state:
                self.interferer_phase_deg = float(state["interferer_phase_deg"]) % 360.0
            # Phase LSB and steering resolution are independent knobs, so
            # take both whenever the frontend sends them. ignore_res only
            # decides which one drives the sweep -- it must not make the
            # Bits slider overwrite the steering resolution, which is what
            # used to collapse the pattern to a handful of points whenever
            # a lab dropped the phase shifter to 3 or 4 bits.
            if "bits" in state:
                bits = max(int(state["bits"]), 1)
                self.phase_step = 360.0 / (2 ** bits)
                print(f"Phase shift LSB set to {self.phase_step}° ({bits} bits)")
            if "steer_res" in state:
                self.steer_res = max(float(state["steer_res"]), 0.1)
                print(f"Steering resolution set to {self.steer_res}°")
            if "ignore_res" in state:
                self.ignore_res = bool(state["ignore_res"])
                print(f"Ignore steering resolution: {self.ignore_res}")
            # The frontend's `mode` is the legacy sweep mode. It is NOT
            # self.mode, which is the run state (idle/sweep/cw_radar).
            if "mode" in state:
                sweep_mode = str(state["mode"])
                if sweep_mode != self.sweep_mode:
                    self.sweep_mode = sweep_mode
                    print(f"Sweep mode: {self.sweep_mode}")
            if "hold_angle" in state:
                self.hold_angle = max(-90.0, min(90.0, float(state["hold_angle"])))
            return {"status": "ok"}

        elif cmd == "power_off":
            if not self.shutdown_permitted():
                return {"status": "error",
                        "message": "Shutdown is not permitted on this host. "
                                   "Re-run install.sh to install the sudoers "
                                   "drop-in that grants it."}
            # Popen, not run: the reply has to reach the browser before systemd
            # starts tearing the machine down.
            subprocess.Popen(["sudo", "-n"] + SHUTDOWN_CMD)
            return {"status": "ok", "message": "Shutting down."}

        elif cmd == "ctf_status":
            return self.ctf.status(sim_mode=self.sim_mode, sweeping=self.sweeping)

        elif cmd == "ctf_reset":
            self.ctf.reset()
            return self.ctf.status(sim_mode=self.sim_mode, sweeping=self.sweeping)

        elif cmd == "run_calibration":
            return self.run_calibration(data.get("task_name", "find_hb100"))

        elif cmd == "get_calibration_status":
            return self.get_calibration_status()

        elif cmd == "cancel_calibration":
            return self.cancel_calibration()

        elif cmd == "start_cw_radar":
            return self.start_cw_radar(data)

        elif cmd == "stop_cw_radar":
            return self.stop_cw_radar()

        elif cmd == "set_cw_radar_params":
            return self.set_cw_radar_params(data)

        elif cmd == "get_cw_radar_state":
            return self.get_cw_radar_state()
        elif cmd == "get_radar_capabilities":
            return self.get_radar_capabilities()

        elif cmd == "start_fmcw_radar":
            return self.start_fmcw_radar(data)

        elif cmd == "stop_fmcw_radar":
            return self.stop_fmcw_radar()

        elif cmd == "set_fmcw_radar_params":
            return self.set_fmcw_radar_params(data)

        elif cmd == "get_fmcw_radar_state":
            return self.get_fmcw_radar_state()

        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}

    def broadcast_to_ws(self, message):
        """Send message to all connected WebSocket clients"""
        if not self.ws_clients:
            return

        # Serialize once, not once per client: an FMCW frame carries its map.
        text = json.dumps(message)
        with self.ws_lock:
            dead_clients = set()
            for client in self.ws_clients:
                try:
                    asyncio.run_coroutine_threadsafe(
                        client.send(text),
                        self.ws_loop
                    )
                except Exception:
                    dead_clients.add(client)

            self.ws_clients -= dead_clients

    # Radar frames are capped at this rate. A single-chirp FMCW capture
    # refills in ~5 ms on a kit and in well under that in --sim (range_sync
    # ran at ~475 fps there): every frame is a WebSocket message and a
    # browser redraw, and at those rates the page fell minutes behind its
    # own clicks. CW (~9 fps) and range_doppler (~5 fps) never reach it.
    RADAR_MAX_FPS = 20.0

    # Radar modes the main loop drives: {mode: (frame method, frame type)}.
    _RADAR_FRAMES = {
        "cw_radar": ("do_cw_radar_frame", "cw_radar_frame"),
        "fmcw_radar": ("do_fmcw_radar_frame", "fmcw_radar_frame"),
    }

    def _pace_radar_frames(self):
        """Sleep off whatever is left of this frame's 1/RADAR_MAX_FPS slot."""
        now = time.perf_counter()
        last = getattr(self, "_last_radar_frame_t", None)
        if last is not None:
            wait = 1.0 / self.RADAR_MAX_FPS - (now - last)
            if wait > 0:
                time.sleep(wait)
                now = time.perf_counter()
        self._last_radar_frame_t = now

    def _radar_frame_step(self):
        """One radar frame, under mode_lock.

        Holding the lock means a stop from the WebSocket thread waits for the
        frame instead of restoring the SDR -- or, in FMCW, switching the TDD
        off -- underneath a capture in progress. A transition waits at most one
        frame. The mode is re-read inside the lock because it may have changed
        while this thread waited for it.
        """
        with self._get_mode_lock():
            mode = self.mode
            entry = self._RADAR_FRAMES.get(mode)
            if entry is None:
                return
            method, frame_type = entry
            try:
                frame = {
                    "type": frame_type,
                    "timestamp": time.time(),
                    "data": getattr(self, method)(),
                }
            except Exception as e:
                frame = None
                print(f"{mode} error: {e}")
                error = {
                    "type": "error",
                    "source": mode,
                    "timestamp": time.time(),
                    "message": str(e),
                }
        # Broadcast outside the lock (skip ZMQ -- desktop clients don't use radar).
        if frame is not None:
            self.broadcast_to_ws(frame)
        else:
            self.broadcast_to_ws(error)
            # Brief pause on error so we don't tight-loop
            time.sleep(0.1)

    def run(self):
        """Main run loop"""
        self.running = True

        # Start command handler thread (ZMQ)
        # Kept so stop() can wait for it before closing its socket.
        self._cmd_thread = threading.Thread(target=self._command_loop, daemon=True)
        self._cmd_thread.start()

        # Start WebSocket server if available
        if HAS_WEBSOCKETS:
            ws_thread = threading.Thread(target=self._websocket_server, daemon=True)
            ws_thread.start()

        # Start HTTP server for static files
        http_thread = threading.Thread(target=self._http_server, daemon=True)
        http_thread.start()

        # Start radar HTTP server (separate port, independent of main HTTP server)
        radar_http_thread = threading.Thread(target=self._radar_http_server, daemon=True)
        radar_http_thread.start()

        print("Starting main loop... (Ctrl+C to stop)")

        while self.running:
            if self.mode == "sweep":
                try:
                    sweep_data = self.do_sweep()

                    # CTF tracking mode watches where the source is. Note this
                    # is NOT what phaser_ctf's docstring warns against: that
                    # warning is about hooking the sweep's commanded phases,
                    # which step through every sector on every pass. One peak
                    # angle per sweep is a single measurement.
                    self.ctf.observe_tracked(
                        sweep_data.get("peak_angle_deg"),
                        sweep_data.get("peak_signal"),
                    )

                    frame = {
                        "type": "sweep",
                        "timestamp": time.time(),
                        "data": sweep_data,
                    }

                    # Send via ZMQ
                    self.pub_socket.send(msgpack.packb(frame, use_bin_type=True))

                    # Broadcast to WebSocket clients
                    self.broadcast_to_ws(frame)

                except Exception as e:
                    print(f"Sweep error: {e}")
                    frame = {
                        "type": "error",
                        "timestamp": time.time(),
                        "message": str(e),
                    }
                    self.pub_socket.send(msgpack.packb(frame, use_bin_type=True))
                    self.broadcast_to_ws(frame)

            elif self.mode in self._RADAR_FRAMES:
                self._radar_frame_step()
                self._pace_radar_frames()

            else:
                time.sleep(0.1)

    def _command_loop(self):
        """Handle incoming commands on REP socket"""
        poller = zmq.Poller()
        poller.register(self.rep_socket, zmq.POLLIN)

        while self.running:
            try:
                socks = dict(poller.poll(100))
            except zmq.ZMQError:
                # stop() closed the socket under us. Only an error while
                # shutting down is expected; anything else is real.
                if not self.running:
                    return
                raise
            if self.rep_socket in socks:
                try:
                    msg = self.rep_socket.recv_json()
                    response = self.handle_command(msg)
                    self.rep_socket.send_json(response)
                except Exception as e:
                    print(f"Command error: {e}")
                    self.rep_socket.send_json({"status": "error", "message": str(e)})

    def _websocket_server(self):
        """Run WebSocket server for browser clients"""
        async def handler(websocket):
            # New websockets API (v11+) only passes websocket, not path
            print(f"[WS] Client connected from {websocket.remote_address}")
            with self.ws_lock:
                self.ws_clients.add(websocket)

            # Send current state on connect
            try:
                state = self.get_state()
                await websocket.send(json.dumps({"type": "state", "data": state}))
            except Exception as e:
                print(f"[WS] Error sending state: {e}")

            try:
                async for message in websocket:
                    try:
                        msg = json.loads(message)
                        request_id = msg.get("id")
                        response = self.handle_command(msg)
                        reply = {"type": "response", **response}
                        if request_id:
                            reply["id"] = request_id
                        await websocket.send(json.dumps(reply))
                    except json.JSONDecodeError:
                        await websocket.send(json.dumps({"type": "error", "message": "Invalid JSON"}))
                    except Exception as e:
                        await websocket.send(json.dumps({"type": "error", "message": str(e)}))
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                with self.ws_lock:
                    self.ws_clients.discard(websocket)
                print(f"[WS] Client disconnected")

        async def serve():
            self.ws_loop = asyncio.get_event_loop()
            async with websockets.serve(handler, "0.0.0.0", self.ws_port):
                print(f"WebSocket server on port {self.ws_port}")
                while self.running:
                    await asyncio.sleep(0.1)

        asyncio.run(serve())

    def _http_server(self):
        """Serve static frontend files"""
        # Look for frontend in common locations
        script_dir = Path(__file__).parent
        possible_paths = [
            script_dir / "frontend" / "dist",
            script_dir / "frontend",
            script_dir / "www",
            Path("/var/www/phaser"),
        ]

        www_dir = None
        for p in possible_paths:
            if p.exists() and (p / "index.html").exists():
                www_dir = p
                break

        if www_dir is None:
            print(f"[HTTP] No frontend found, serving from {script_dir}")
            www_dir = script_dir

        print(f"[HTTP] Serving static files from {www_dir}")

        class Handler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(www_dir), **kwargs)

            def log_message(self, format, *args):
                pass  # Suppress access logs

            def end_headers(self):
                # CORS headers for development
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                super().end_headers()

        # ThreadingHTTPServer, not HTTPServer. The synchronous server handles
        # one connection to completion in this thread, so a client that opens
        # a TCP connection and does not send a request blocks it forever --
        # HTTPServer sets no socket timeout, so that read never returns and
        # the whole server is dead from that point on. Mobile Safari and
        # Chrome both open speculative connections they may never use, which
        # is exactly that case: ssh to the Pi keeps working, the page never
        # loads, and nothing appears in any log.
        #
        # Handler.timeout closes a connection that goes idle mid-request, so a
        # stalled client costs one thread for 30s rather than leaking it. The
        # server timeout makes the accept loop notice self.running going false
        # instead of blocking in accept() until the next connection.
        Handler.timeout = 30
        server = ThreadingHTTPServer(("0.0.0.0", self.http_port), Handler)
        server.daemon_threads = True
        server.timeout = 1.0
        print(f"HTTP server on port {self.http_port}")

        while self.running:
            server.handle_request()

        server.server_close()

    def _radar_http_server(self):
        """Serve the radar app static frontend on a separate port.

        Independent of the main HTTP server so a misconfiguration here can't
        affect the existing beamforming UI's serving path.
        """
        if not self.radar_http_port:
            return

        script_dir = Path(__file__).parent
        possible_paths = [
            script_dir / "frontend-radar" / "dist",
            script_dir / "frontend-radar",
        ]

        www_dir = None
        for p in possible_paths:
            if p.exists() and (p / "index.html").exists():
                www_dir = p
                break

        if www_dir is None:
            print(f"[RADAR-HTTP] No radar frontend found; skipping radar HTTP server")
            return

        print(f"[RADAR-HTTP] Serving radar static files from {www_dir}")

        class RadarHandler(SimpleHTTPRequestHandler):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, directory=str(www_dir), **kwargs)

            def log_message(self, format, *args):
                pass

            def end_headers(self):
                self.send_header('Access-Control-Allow-Origin', '*')
                self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
                self.send_header('Access-Control-Allow-Headers', 'Content-Type')
                super().end_headers()

        try:
            RadarHandler.timeout = 30
            server = ThreadingHTTPServer(("0.0.0.0", self.radar_http_port), RadarHandler)
            server.daemon_threads = True
            server.timeout = 1.0
        except OSError as e:
            print(f"[RADAR-HTTP] Failed to bind port {self.radar_http_port}: {e}")
            return

        print(f"Radar HTTP server on port {self.radar_http_port}")

        while self.running:
            server.handle_request()

        server.server_close()

    def stop(self):
        """Stop the server"""
        print("Shutting down...")
        # Cleanly leave any active mode so SDR state is restored.
        try:
            if getattr(self, "mode", "idle") in self._MODE_HANDLERS:
                self._set_mode("idle")
        except Exception as e:
            print(f"[MODE] cleanup on stop failed: {e}")
        self.running = False
        self.sweeping = False
        self.mode = "idle"

        # Kill any running calibration
        if self.cal_process and self.cal_process.poll() is None:
            self.cal_process.terminate()

        # Let the command thread see running=False and leave its 100 ms poll
        # before its socket goes. Closing a ZMQ socket another thread is
        # polling is not thread-safe: it raised in that thread on most stops,
        # and the process exited mid-traceback, leaving a bare
        # "Exception in thread Thread-1" in the journal on every restart.
        cmd_thread = getattr(self, "_cmd_thread", None)
        if cmd_thread is not None and cmd_thread is not threading.current_thread():
            cmd_thread.join(timeout=1.0)

        self.pub_socket.close()
        self.rep_socket.close()
        self.ctx.term()


# The Pluto's iiod, reached over the USB network gadget the Pluto presents
# (SDR_functions and _do_init_hardware use "ip:192.168.2.1"). A TCP connect is
# enough to tell "cable unplugged" from "plugged in but something else broke",
# and it needs no libiio.
PLUTO_IIOD = ("192.168.2.1", 30431)


def pluto_reachable(timeout=1.0):
    try:
        with socket.create_connection(PLUTO_IIOD, timeout=timeout):
            return True
    except OSError:
        return False


class HardwareMissingServer:
    """Serves the UI, and says what is wrong, when the hardware will not start.

    Without this a failed hardware init killed the process before the HTTP
    server existed, so the browser said "refused to connect" -- which points
    at the network, not at the cause. On two bring-ups in a row the cause was
    the Pluto's USB cable to the Pi, left unplugged. Now the page loads and
    shows a card naming the cable.

    It watches for the Pluto and, once it appears, exits nonzero so systemd
    (Restart=on-failure) starts the full backend; the page's 2 s WebSocket
    reconnect then picks that up with no reload. A failure with the Pluto
    present gets no watcher -- restarting would fail the same way -- only the
    card's Retry button.
    """

    EXIT_RETRY = 3
    # Plugged in is not ready: the Pluto's iiod answers a few seconds before
    # the radio does, and an init in that window fails all over again.
    SETTLE_S = 5.0
    POLL_S = 2.0

    # Borrowed rather than copied: the same static-file server, the same
    # passwordless-poweroff probe. Neither touches hardware state.
    _http_server = PhaserHeadless._http_server
    shutdown_permitted = PhaserHeadless.shutdown_permitted

    def __init__(self, error, ws_port=8765, http_port=8080, pluto_check=pluto_reachable):
        self.error = f"{type(error).__name__}: {error}"
        self.ws_port = ws_port
        self.http_port = http_port
        self.pluto_check = pluto_check
        self.pluto_found = pluto_check()
        self._shutdown_permitted = None
        self.running = False
        self.exit_code = 0

    def status(self):
        if self.pluto_found:
            message = ("The Phaser hardware did not start. Check the Pluto's USB "
                       "cable to the Pi and the PHASER's USB-C power, then press Retry.")
        else:
            message = ("The Pluto's USB cable to the Pi is unplugged, the usual "
                       "cause, or the Pluto has no power. Plug it in and this page "
                       "reconnects on its own.")
        return {
            "status": "error",
            "hardware_missing": True,
            "pluto_found": self.pluto_found,
            "message": message,
            "detail": self.error,
            "shutdown_available": self.shutdown_permitted(),
        }

    def handle_command(self, msg):
        cmd = msg.get("cmd")
        if cmd == "retry_hardware":
            self.request_restart("Retry pressed")
            return {"status": "ok", "message": "Restarting the backend."}
        if cmd == "power_off":
            if not self.shutdown_permitted():
                return {"status": "error",
                        "message": "Shutdown is not permitted on this host."}
            subprocess.Popen(["sudo", "-n"] + SHUTDOWN_CMD)
            return {"status": "ok", "message": "Shutting down."}
        return self.status()

    def request_restart(self, why):
        print(f"[HW] {why}; exiting for systemd to restart the backend")
        self.exit_code = self.EXIT_RETRY
        self.running = False

    def watch_once(self):
        """One poll. True when the Pluto has just appeared."""
        if self.pluto_found:
            return False
        if self.pluto_check():
            self.pluto_found = True
            return True
        return False

    def _watch(self):
        while self.running:
            if self.watch_once():
                print(f"[HW] Pluto appeared; waiting {self.SETTLE_S:.0f} s for it to settle")
                time.sleep(self.SETTLE_S)
                self.request_restart("Pluto found")
                return
            time.sleep(self.POLL_S)

    def _websocket_server(self):
        async def handler(websocket):
            try:
                await websocket.send(json.dumps({"type": "hardware_missing", "data": self.status()}))
                async for message in websocket:
                    try:
                        msg = json.loads(message)
                    except json.JSONDecodeError:
                        continue
                    reply = {"type": "response", **self.handle_command(msg)}
                    if msg.get("id"):
                        reply["id"] = msg["id"]
                    await websocket.send(json.dumps(reply))
            except websockets.exceptions.ConnectionClosed:
                pass

        async def serve():
            async with websockets.serve(handler, "0.0.0.0", self.ws_port):
                while self.running:
                    await asyncio.sleep(0.2)

        asyncio.run(serve())

    def run(self):
        """Serve until the hardware should be retried; returns the exit code."""
        self.running = True
        print(f"[HW] Hardware init failed: {self.error}")
        print("[HW] Pluto " + ("reachable" if self.pluto_found else
                               "NOT reachable at %s:%d -- check its USB cable to the Pi" % PLUTO_IIOD))
        threads = [threading.Thread(target=self._http_server, daemon=True)]
        if HAS_WEBSOCKETS:
            threads.append(threading.Thread(target=self._websocket_server, daemon=True))
        threads.append(threading.Thread(target=self._watch, daemon=True))
        for t in threads:
            t.start()
        while self.running:
            time.sleep(0.2)
        time.sleep(0.5)   # let the Retry reply reach the browser
        return self.exit_code


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phaser Headless Backend")
    parser.add_argument("--pub-port", type=int, default=5555, help="ZMQ PUB port")
    parser.add_argument("--rep-port", type=int, default=5556, help="ZMQ REP port")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port for static files")
    parser.add_argument("--radar-http-port", type=int, default=8081,
                        help="HTTP port for radar app static files (0 to disable)")
    parser.add_argument("--sim", action="store_true",
                        help="Run with simulated hardware (no Phaser required)")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              ADI Phaser Beamforming Backend                  ║
╠══════════════════════════════════════════════════════════════╣
║  ZMQ PUB:    tcp://*:{args.pub_port:<5}  (sweep data stream)          ║
║  ZMQ REP:    tcp://*:{args.rep_port:<5}  (commands)                   ║
║  WebSocket:  ws://*:{args.ws_port:<5}   (browser clients)            ║
║  HTTP:       http://*:{args.http_port:<5} (web UI)                     ║
║  Radar HTTP: http://*:{args.radar_http_port:<5} (radar UI)                   ║
╚══════════════════════════════════════════════════════════════╝
    """)

    if args.sim:
        print("*** RUNNING IN SIMULATION MODE — no Phaser hardware required ***")

    try:
        server = PhaserHeadless(
            pub_port=args.pub_port,
            rep_port=args.rep_port,
            ws_port=args.ws_port,
            http_port=args.http_port,
            radar_http_port=args.radar_http_port,
            sim_mode=args.sim,
        )
    except Exception as e:
        if args.sim:
            raise
        fallback = HardwareMissingServer(e, ws_port=args.ws_port, http_port=args.http_port)
        sys.exit(fallback.run())

    def signal_handler(sig, frame):
        server.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        server.run()
    except KeyboardInterrupt:
        server.stop()


if __name__ == "__main__":
    main()
