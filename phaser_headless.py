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
from http.server import HTTPServer, SimpleHTTPRequestHandler
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
    load_phase_cal, load_gain_cal
)

try:
    import config
except ImportError:
    print("config.py not found")
    sys.exit(1)

from phaser_functions import load_hb100_cal
from SDR_functions import load_channel_cal


class PhaserHeadless:
    def __init__(self, pub_port=5555, rep_port=5556, ws_port=8765, http_port=8080):
        self.pub_port = pub_port
        self.rep_port = rep_port
        self.ws_port = ws_port
        self.http_port = http_port
        self.running = False
        self.sweeping = False

        # WebSocket clients
        self.ws_clients = set()
        self.ws_lock = threading.Lock()

        # Physical constants
        self.c = 299792458

        # Calibration process tracking
        self.cal_process = None
        self.cal_task = None
        self.cal_log = []

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

        if "phaser" in my_hostname:
            self.rpi_ip = "ip:localhost"
            self.sdr_ip = "ip:192.168.2.1"
        else:
            self.rpi_ip = "ip:phaser.local"
            self.sdr_ip = "ip:phaser.local:50901"

        print(f"Hostname: {my_hostname}, rpi: {self.rpi_ip}, sdr: {self.sdr_ip}")

        # Load configuration
        self.Rx_freq = config.Rx_freq
        self.Tx_freq = config.Tx_freq
        self.LO_freq = self.SignalFreq + self.Rx_freq
        self.SampleRate = config.SampleRate
        self.Rx_gain = config.Rx_gain
        self.Tx_gain = config.Tx_gain
        self.d = config.d
        self.Averages = getattr(config, 'Averages', 1)

        # Load calibration data
        self.phase_cal = load_phase_cal()
        self.gain_cal = load_gain_cal()
        self.channel_cal = load_channel_cal()

        print(f"SignalFreq: {self.SignalFreq/1e9:.3f} GHz, LO: {self.LO_freq/1e9:.3f} GHz")
        print(f"Rx_gain: {self.Rx_gain}, Tx_gain: {self.Tx_gain}")
        print(f"Phase cal: {self.phase_cal}")
        print(f"Gain cal: {self.gain_cal}")
        print(f"Channel cal: {self.channel_cal}")

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

        # Default taper (all elements equal)
        self.gainList = [100, 100, 100, 100, 100, 100, 100, 100]
        ADAR_set_Taper(self.array, self.gainList)

        # Sweep settings
        self.phase_step = 2.8125  # 7 bits = 360/128
        self.steer_min = -90
        self.steer_max = 90

        # Tx mode
        self.Tx_mode = "Transmit Disabled"

        # Beam squint: BW offset (MHz) for phase calculation
        self.BW = 0  # When > 0, phases calculated for (SignalFreq - BW*1e6)

        # Digital beam gains (for hybrid beamforming / monopulse)
        self.B0_Gain = 1.0  # Beam 0 (elements 5-8) digital gain
        self.B1_Gain = 1.0  # Beam 1 (elements 1-4) digital gain

        print("Hardware Initialization Complete.")

    def _apply_gain_cal(self, taper_values):
        """Apply gain calibration to taper values"""
        calibrated = []
        for idx, value in enumerate((list(taper_values) + [100] * 8)[:8]):
            gain_mult = self.gain_cal[idx] if idx < len(self.gain_cal) else 1.0
            calibrated.append(int(max(0, min(127, round(value * gain_mult)))))
        return calibrated

    def ConvertPhaseToSteerAngle(self, PhDelta):
        """Convert phase delta to steering angle"""
        value1 = (self.c * np.radians(np.abs(PhDelta))) / (2 * 3.14159 * self.SignalFreq * self.d)
        clamped = max(min(1, value1), -1)
        theta = np.degrees(np.arcsin(clamped))
        return theta if PhDelta >= 0 else -theta

    def ConvertSteerAngleToPhase(self, steer_angle):
        """Convert steering angle to phase delta (inverse of ConvertPhaseToSteerAngle)"""
        # Phase delta = 2*Pi*d*sin(theta)*f/c (in degrees)
        phase_rad = 2 * 3.14159 * self.d * np.sin(np.radians(steer_angle)) * self.SignalFreq / self.c
        return np.degrees(phase_rad)

    def do_sweep(self):
        """Perform one beam sweep and return data including monopulse delta/error"""
        max_signal = -1000
        data_fft = None
        gain = []          # Sum beam (chan1 + chan2)
        delta_gain = []    # Delta beam (chan1 - chan2)
        phase_diff = []    # Phase difference between sum and delta
        error_func = []    # Monopulse error function
        angles = []

        # Convert steering angle range to phase values (like phaser_gui.py)
        steer_res = self.phase_step  # Use phase_step as steering resolution
        SteerValues = np.arange(self.steer_min, self.steer_max + steer_res, steer_res)

        # Beam squint: calculate phases for (SignalFreq - BW) but measure at SignalFreq
        calc_freq = self.SignalFreq - self.BW * 1e6
        PhaseValues = np.degrees(
            2 * 3.14159 * self.d * np.sin(np.radians(SteerValues)) * calc_freq / self.c
        )

        for i, PhDelta in enumerate(PhaseValues):
            ADAR_set_Phase(self.array, PhDelta, self.phase_step, self.phase_cal)

            # Average multiple reads if configured
            total_sum = 0
            total_delta = 0
            total_phase = 0

            for _ in range(self.Averages):
                data = SDR_getData(self.sdr)
                chan1 = data[0]
                chan2 = data[1]

                # Apply digital beam gains (B0_Gain, B1_Gain)
                chan1 = chan1 * self.B0_Gain
                chan2 = chan2 * self.B1_Gain

                # Sum and delta beams
                sum_chan = chan1 + chan2
                delta_chan = chan1 - chan2

                # Find peak in sum channel
                max_index = np.argmax(np.abs(sum_chan))

                # Sum beam magnitude
                s_mag_sum = max(np.abs(sum_chan[max_index]), 1e-15)
                s_dbfs_sum = 20 * np.log10(s_mag_sum / (2**11))

                # Delta beam magnitude
                s_mag_delta = max(np.abs(delta_chan[max_index]), 1e-15)
                s_dbfs_delta = 20 * np.log10(s_mag_delta / (2**11))

                # Phase difference between sum and delta
                beam_phase = np.angle(sum_chan[max_index]) - np.angle(delta_chan[max_index])

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

        return {
            "ArrayGain": [float(x) for x in gain],
            "ArrayDelta": [float(x) for x in delta_gain],
            "PhaseDiff": phase_diff,
            "ErrorFunc": error_func,
            "ArrayAngle": [float(x) for x in angles],
            "max_gain": max_gain.tolist(),
            "xf": xf.tolist(),
            "peak_signal": float(max_signal),
        }

    def get_state(self):
        """Return current configuration state"""
        return {
            "status": "ok",
            "data": {
                "SignalFreq": self.SignalFreq,
                "Rx_freq": self.Rx_freq,
                "Tx_freq": self.Tx_freq,
                "Rx_gain": self.Rx_gain,
                "Tx_gain": self.Tx_gain,
                "Tx_mode": self.Tx_mode,
                "gainList": self.gainList,
                "phase_step": self.phase_step,
                "steer_min": self.steer_min,
                "steer_max": self.steer_max,
                "Averages": self.Averages,
                "d": self.d,
                "BW": self.BW,
                "B0_Gain": self.B0_Gain,
                "B1_Gain": self.B1_Gain,
                "sweeping": self.sweeping,
                "hardware_connected": True,  # If we got here, hardware is connected
            }
        }

    def set_rx_gain(self, gain):
        """Set Rx gain and apply to hardware"""
        self.Rx_gain = int(gain)
        SDR_setRx(self.sdr, self.Rx_gain, self.Rx_gain)
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

    def run_calibration(self, task_name):
        """Run a calibration script as subprocess"""
        if self.cal_process is not None and self.cal_process.poll() is None:
            return {"status": "error", "message": "Calibration already running"}

        script_dir = os.path.dirname(os.path.abspath(__file__))

        if task_name == "find_hb100":
            script = os.path.join(script_dir, "phaser_find_hb100_headless.py")
        elif task_name == "phaser_cal":
            script = os.path.join(script_dir, "phaser_cal_headless.py")
        else:
            return {"status": "error", "message": f"Unknown calibration task: {task_name}"}

        if not os.path.exists(script):
            return {"status": "error", "message": f"Script not found: {script}"}

        print(f"Starting calibration: {task_name}")
        self.cal_task = task_name
        self.cal_log = []

        # Need to stop sweeping during calibration
        was_sweeping = self.sweeping
        self.sweeping = False

        try:
            self.cal_process = subprocess.Popen(
                [sys.executable, script],
                cwd=script_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            return {"status": "ok", "message": f"Started {task_name}"}
        except Exception as e:
            self.sweeping = was_sweeping
            return {"status": "error", "message": str(e)}

    def get_calibration_status(self):
        """Get calibration process status"""
        if self.cal_process is None:
            return {"status": "ok", "running": False, "task": None}

        # Read any available output
        if self.cal_process.poll() is None:
            # Still running - non-blocking read
            import select
            while True:
                # Check if there's data to read (Unix only, but works on Pi)
                try:
                    import fcntl
                    fd = self.cal_process.stdout.fileno()
                    fl = fcntl.fcntl(fd, fcntl.F_GETFL)
                    fcntl.fcntl(fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)
                    line = self.cal_process.stdout.readline()
                    if line:
                        self.cal_log.append(line.strip())
                    else:
                        break
                except Exception:
                    break

            return {
                "status": "ok",
                "running": True,
                "task": self.cal_task,
                "log": self.cal_log[-20:],  # Last 20 lines
            }
        else:
            # Finished - read remaining output
            remaining = self.cal_process.stdout.read()
            if remaining:
                self.cal_log.extend(remaining.strip().split('\n'))

            returncode = self.cal_process.returncode
            result = {
                "status": "ok",
                "running": False,
                "task": self.cal_task,
                "returncode": returncode,
                "success": returncode == 0,
                "log": self.cal_log,
            }

            # Reload calibration if successful
            if returncode == 0:
                self._reload_calibration(self.cal_task)

            self.cal_process = None
            self.cal_task = None
            return result

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

        print(f"[CMD] {cmd}")

        if cmd == "ping":
            return {"status": "ok", "message": "pong"}

        elif cmd == "get_state":
            return self.get_state()

        elif cmd == "start_sweep":
            self.sweeping = True
            return {"status": "ok"}

        elif cmd == "stop_sweep":
            self.sweeping = False
            return {"status": "ok"}

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
            if "SignalFreq" in state:
                self.set_signal_freq(state["SignalFreq"])
            if "gainList" in state:
                self.set_taper(state["gainList"])
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
            # Handle phase_step: ignore_res=true uses bits, ignore_res=false uses steer_res
            ignore_res = state.get("ignore_res", True)
            if ignore_res:
                if "bits" in state:
                    bits = int(state["bits"])
                    self.phase_step = 360.0 / (2 ** bits)
                    print(f"Phase step set to {self.phase_step}° ({bits} bits)")
            else:
                if "steer_res" in state:
                    self.phase_step = float(state["steer_res"])
                    print(f"Steering resolution set to {self.phase_step}°")
            return {"status": "ok"}

        elif cmd == "run_calibration":
            return self.run_calibration(data.get("task_name", "find_hb100"))

        elif cmd == "get_calibration_status":
            return self.get_calibration_status()

        else:
            return {"status": "error", "message": f"Unknown command: {cmd}"}

    def broadcast_to_ws(self, message):
        """Send message to all connected WebSocket clients"""
        if not self.ws_clients:
            return

        with self.ws_lock:
            dead_clients = set()
            for client in self.ws_clients:
                try:
                    asyncio.run_coroutine_threadsafe(
                        client.send(json.dumps(message)),
                        self.ws_loop
                    )
                except Exception:
                    dead_clients.add(client)

            self.ws_clients -= dead_clients

    def run(self):
        """Main run loop"""
        self.running = True

        # Start command handler thread (ZMQ)
        cmd_thread = threading.Thread(target=self._command_loop, daemon=True)
        cmd_thread.start()

        # Start WebSocket server if available
        if HAS_WEBSOCKETS:
            ws_thread = threading.Thread(target=self._websocket_server, daemon=True)
            ws_thread.start()

        # Start HTTP server for static files
        http_thread = threading.Thread(target=self._http_server, daemon=True)
        http_thread.start()

        print("Starting sweep loop... (Ctrl+C to stop)")

        while self.running:
            if self.sweeping:
                try:
                    sweep_data = self.do_sweep()

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
            else:
                time.sleep(0.1)

    def _command_loop(self):
        """Handle incoming commands on REP socket"""
        poller = zmq.Poller()
        poller.register(self.rep_socket, zmq.POLLIN)

        while self.running:
            socks = dict(poller.poll(100))
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

        server = HTTPServer(("0.0.0.0", self.http_port), Handler)
        print(f"HTTP server on port {self.http_port}")

        while self.running:
            server.handle_request()

        server.server_close()

    def stop(self):
        """Stop the server"""
        print("Shutting down...")
        self.running = False
        self.sweeping = False

        # Kill any running calibration
        if self.cal_process and self.cal_process.poll() is None:
            self.cal_process.terminate()

        self.pub_socket.close()
        self.rep_socket.close()
        self.ctx.term()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Phaser Headless Backend")
    parser.add_argument("--pub-port", type=int, default=5555, help="ZMQ PUB port")
    parser.add_argument("--rep-port", type=int, default=5556, help="ZMQ REP port")
    parser.add_argument("--ws-port", type=int, default=8765, help="WebSocket port")
    parser.add_argument("--http-port", type=int, default=8080, help="HTTP port for static files")
    args = parser.parse_args()

    print(f"""
╔══════════════════════════════════════════════════════════════╗
║              ADI Phaser Beamforming Backend                  ║
╠══════════════════════════════════════════════════════════════╣
║  ZMQ PUB:    tcp://*:{args.pub_port:<5}  (sweep data stream)          ║
║  ZMQ REP:    tcp://*:{args.rep_port:<5}  (commands)                   ║
║  WebSocket:  ws://*:{args.ws_port:<5}   (browser clients)            ║
║  HTTP:       http://*:{args.http_port:<5} (web UI)                     ║
╚══════════════════════════════════════════════════════════════╝
    """)

    server = PhaserHeadless(
        pub_port=args.pub_port,
        rep_port=args.rep_port,
        ws_port=args.ws_port,
        http_port=args.http_port
    )

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
