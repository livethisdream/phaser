#!/usr/bin/env python3
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import adi
import numpy as np

from ADAR_pyadi_functions import *
from phaser_functions import load_channel_cal, load_gain_cal, load_hb100_cal, load_phase_cal
from SDR_functions import *

# Try local config first, then parent directory
try:
    import config as config
except ImportError:
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        import config as config
    except ImportError:
        print("Make sure config.py is in this directory or parent directory")
        sys.exit(0)


CW_SAMPLE_RATE = 600_000
CW_BUFFER_SIZE = 1024 * 64
CW_IF_HZ = 100_000
CW_DISPLAY_BW_HZ = 300


def _detect_hostname():
    if socket.gethostname().find(".") >= 0:
        return socket.gethostname()
    return socket.gethostbyaddr(socket.gethostname())[0]


def _auto_uris(hostname):
    if "phaser" in hostname:
        return "ip:localhost", "ip:192.168.2.1"
    # Laptop or other non-board host with context-forwarded SDR access.
    return "ip:phaser.local", "ip:phaser.local:50901"


def resolve_hardware_uris():
    hostname = _detect_hostname()
    auto_rpi_uri, auto_sdr_uri = _auto_uris(hostname)

    env_rpi_uri = os.environ.get("PHASER_RPI_URI")
    env_sdr_uri = os.environ.get("PHASER_SDR_URI")

    cfg_mode = str(getattr(config, "uri_mode", "auto")).strip().lower()
    cfg_rpi_uri = getattr(config, "rpi_uri", None)
    cfg_sdr_uri = getattr(config, "sdr_uri", None)

    # Environment vars are the highest-precedence deployment override.
    if env_rpi_uri and env_sdr_uri:
        return hostname, env_rpi_uri, env_sdr_uri

    if cfg_mode == "custom":
        if not cfg_rpi_uri or not cfg_sdr_uri:
            raise ValueError("uri_mode='custom' requires both config.rpi_uri and config.sdr_uri")
        return hostname, cfg_rpi_uri, cfg_sdr_uri

    if cfg_mode == "prefer_config":
        return hostname, cfg_rpi_uri or auto_rpi_uri, cfg_sdr_uri or auto_sdr_uri

    return hostname, auto_rpi_uri, auto_sdr_uri


class PhaserServer:
    def __init__(self):
        self.c = 299792458
        self.hostname, self.rpi_uri, self.sdr_uri = resolve_hardware_uris()
        print(
            f"Server Hostname is {self.hostname}, connecting to rpi: {self.rpi_uri} and sdr: {self.sdr_uri}"
        )

        # Load Signal Frequency from config or cal
        self.SignalFreq = config.SignalFreq
        try:
            self.SignalFreq = load_hb100_cal()
            print("Found signal freq file, ", self.SignalFreq)
        except Exception:
            print("No signal freq found, keeping at ", self.SignalFreq)

        self.Tx_freq = config.Tx_freq
        self.Rx_freq = config.Rx_freq
        self.LO_freq = self.SignalFreq + self.Rx_freq
        self.SampleRate = config.SampleRate
        self.Rx_gain = config.Rx_gain
        self.Tx_gain = config.Tx_gain
        self.Averages = config.Averages
        self.d = config.d
        self.bandwidth = 10
        self.phase_cal = load_phase_cal(
            [
                getattr(config, "Rx1_cal", 0.0),
                getattr(config, "Rx2_cal", 0.0),
                getattr(config, "Rx3_cal", 0.0),
                getattr(config, "Rx4_cal", 0.0),
                getattr(config, "Rx5_cal", 0.0),
                getattr(config, "Rx6_cal", 0.0),
                getattr(config, "Rx7_cal", 0.0),
                getattr(config, "Rx8_cal", 0.0),
            ]
        )
        self.channel_cal = load_channel_cal()
        self.gain_cal = load_gain_cal()
        print(f"Loaded phase calibration: {self.phase_cal}")
        print(f"Loaded channel calibration: {self.channel_cal}")
        print(f"Loaded gain calibration: {self.gain_cal}")

        # System state
        self.current_Taper = [100] * 8
        self.Tx_mode = "Transmit Disabled"

        # GPIO control (one-bit ADC/DAC bridge on the RPi side).
        self.gpios = adi.one_bit_adc_dac(self.rpi_uri)
        self.gpios.gpio_vctrl_1 = 1
        self.gpios.gpio_vctrl_2 = 1
        self.gpios.gpio_div_mr = 1
        self.gpios.gpio_div_s0 = 0
        self.gpios.gpio_div_s1 = 0
        self.gpios.gpio_div_s2 = 0
        self.gpios.gpio_tx_sw = 0
        time.sleep(0.5)

        # SDR Init
        self.sdr = SDR_init(
            self.sdr_uri,
            self.SampleRate,
            self.Tx_freq,
            self.Rx_freq,
            self.Rx_gain,
            self.Tx_gain,
            config.buffer_size,
        )
        SDR_setRx(
            self.sdr,
            self.Rx_gain + self.channel_cal[0],
            self.Rx_gain + self.channel_cal[1],
        )
        self.lo = SDR_LO_init(self.rpi_uri, self.LO_freq)
        time.sleep(0.5)

        # ADAR Array Init
        self.array = adi.adar1000_array(
            uri=self.rpi_uri,
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

        ADAR_set_Taper(self.array, self._apply_gain_cal(self.current_Taper))
        print("Hardware Initialization Complete.")

    def reload_calibration(self, task_name=None):
        """Reload calibration values from disk and re-apply to live hardware."""
        if task_name in (None, "find_hb100"):
            try:
                self.SignalFreq = load_hb100_cal()
                self.LO_freq = self.SignalFreq + self.Rx_freq
                self.lo = SDR_LO_init(self.rpi_uri, self.LO_freq)
                print(f"Reloaded HB100 signal frequency: {self.SignalFreq}")
            except Exception as exc:
                print(f"HB100 reload skipped: {exc}")

        if task_name in (None, "phaser_cal"):
            self.phase_cal = load_phase_cal(
                [
                    getattr(config, "Rx1_cal", 0.0),
                    getattr(config, "Rx2_cal", 0.0),
                    getattr(config, "Rx3_cal", 0.0),
                    getattr(config, "Rx4_cal", 0.0),
                    getattr(config, "Rx5_cal", 0.0),
                    getattr(config, "Rx6_cal", 0.0),
                    getattr(config, "Rx7_cal", 0.0),
                    getattr(config, "Rx8_cal", 0.0),
                ]
            )
            self.channel_cal = load_channel_cal()
            self.gain_cal = load_gain_cal()

            # Re-apply cal-dependent settings immediately.
            SDR_setRx(
                self.sdr,
                self.Rx_gain + self.channel_cal[0],
                self.Rx_gain + self.channel_cal[1],
            )
            ADAR_set_Taper(self.array, self._apply_gain_cal(self.current_Taper))
            print("Reloaded phase/channel/gain calibration values")

    def _apply_gain_cal(self, taper_values):
        calibrated = []
        for idx, value in enumerate((list(taper_values) + [100] * 8)[:8]):
            gain_mult = self.gain_cal[idx] if idx < len(self.gain_cal) else 1.0
            calibrated.append(int(max(0, min(127, round(value * gain_mult)))))
        return calibrated

    def _apply_phase_cal(self, phase_values):
        base = (list(phase_values) + [0.0] * 8)[:8]
        return [float(base[idx] + self.phase_cal[idx]) for idx in range(8)]

    def _compute_phase_sweep(self, state):
        mode = state.get("mode", "Beam Sweep")
        bandwidth = state.get("BW", 0)
        steer_res = max(float(state.get("steer_res", state.get("SteerRes", 1.0))), 0.1)
        bits = max(int(state.get("bits", 7)), 1)
        quant_step = 360 / (2**bits)
        ignore_res = bool(state.get("ignore_res", False))

        if ignore_res:
            phase_limit = int(225 / quant_step) * quant_step + quant_step
            phase_values = np.arange(-phase_limit, phase_limit, quant_step)
            angle_values = [self.ConvertPhaseToSteerAngle(ph, self.SignalFreq, bandwidth) for ph in phase_values]
            return phase_values.tolist(), [float(x) for x in angle_values], quant_step

        steer_values = state.get("PhaseValues")
        if not steer_values:
            steer_values = np.arange(-90, 90 + steer_res, steer_res).tolist()

        if mode in ("Static Phase", "Signal vs Time") and len(steer_values) == 0:
            steer_values = [0.0]

        phase_values = np.degrees(
            2 * np.pi * self.d * np.sin(np.radians(steer_values)) * self.SignalFreq / self.c
        )
        return phase_values.tolist(), [float(x) for x in steer_values], quant_step

    def update_hardware_state(self, state):
        """Update Tx mode, LO, Rx/Tx gains, and taper when values change."""
        tx_mode = state.get("Tx_mode", "Transmit Disabled")
        if self.Tx_mode != tx_mode:
            self.Tx_mode = tx_mode
            if tx_mode == "Transmit on OUT1":
                self.gpios.gpio_tx_sw = 0
                self.gpios.gpio_vctrl_2 = 1
            elif tx_mode == "Transmit on OUT2":
                self.gpios.gpio_tx_sw = 1
                self.gpios.gpio_vctrl_2 = 1
            else:
                self.gpios.gpio_vctrl_2 = 1

        signal_freq = state.get("SignalFreq", self.SignalFreq)
        rx_freq = state.get("Rx_freq", self.Rx_freq)
        lo_freq = signal_freq + rx_freq

        if self.SignalFreq != signal_freq or self.Rx_freq != rx_freq:
            self.SignalFreq = signal_freq
            self.Rx_freq = rx_freq
            self.LO_freq = lo_freq
            self.lo = SDR_LO_init(self.rpi_uri, self.LO_freq)

        rx_gain = state.get("Rx_gain", self.Rx_gain)
        if self.Rx_gain != rx_gain:
            self.Rx_gain = rx_gain
            SDR_setRx(
                self.sdr,
                self.Rx_gain + self.channel_cal[0],
                self.Rx_gain + self.channel_cal[1],
            )

        tx_gain = state.get("Tx_gain", self.Tx_gain)
        if self.Tx_gain != tx_gain:
            self.Tx_gain = tx_gain
            SDR_setTx(self.sdr, self.Tx_gain)

        new_taper = state.get("gainList", self.current_Taper)
        if new_taper != self.current_Taper:
            self.current_Taper = new_taper
            ADAR_set_Taper(self.array, self._apply_gain_cal(self.current_Taper))

        self.Averages = state.get("Averages", self.Averages)
        self.d = state.get("d", self.d)
        self.bandwidth = state.get("BW", self.bandwidth)

    def ConvertPhaseToSteerAngle(self, PhDelta, signal_freq, bw):
        value1 = (self.c * np.radians(np.abs(PhDelta))) / (2 * 3.14159 * (signal_freq - bw * 1000000) * self.d)
        clamped_value1 = max(min(1, value1), -1)
        theta = np.degrees(np.arcsin(clamped_value1))
        return theta if PhDelta >= 0 else -theta

    def getData(self, averages, b0_gain, b1_gain, dig_Beam0_phase, dig_Beam1_phase):
        total_sum = 0
        total_delta = 0
        total_beam_phase = 0
        for _ in range(averages):
            data = SDR_getData(self.sdr)
            chan1 = data[0] * b0_gain
            chan2 = data[1] * b1_gain

            NumSamples = len(chan1)
            dig_beam0_rad = np.deg2rad(dig_Beam0_phase)
            dig_beam1_rad = np.deg2rad(dig_Beam1_phase)

            if dig_beam0_rad != 0:
                chan1 = np.fft.ifft(np.fft.fft(chan1) * np.exp(1.0j * dig_beam0_rad), n=NumSamples)[0:NumSamples]
            if dig_beam1_rad != 0:
                chan2 = np.fft.ifft(np.fft.fft(chan2) * np.exp(1.0j * dig_beam1_rad), n=NumSamples)[0:NumSamples]

            sum_chan = chan1 + chan2
            delta_chan = chan1 - chan2

            max_index = np.argmax(sum_chan)
            s_mag_sum = np.max([np.abs(sum_chan[max_index]), 10**-15])
            s_mag_delta = np.max([np.abs(delta_chan[max_index]), 10**-15])

            s_dbfs_sum = 20 * np.log10(s_mag_sum / (2**11))
            s_dbfs_delta = 20 * np.log10(s_mag_delta / (2**11))

            total_beam_phase += (np.angle(sum_chan[max_index]) - np.angle(delta_chan[max_index]))
            total_sum += s_dbfs_sum
            total_delta += s_dbfs_delta

        PeakValue_sum = total_sum / averages
        PeakValue_delta = total_delta / averages
        PeakValue_beam_phase = total_beam_phase / averages

        if np.sign(PeakValue_beam_phase) == -1:
            target_error = min(
                -0.01,
                (
                    np.sign(PeakValue_beam_phase) * (PeakValue_sum - PeakValue_delta)
                    + np.sign(PeakValue_beam_phase) * (PeakValue_sum + PeakValue_delta) / 2
                )
                / (PeakValue_sum + PeakValue_delta),
            )
        else:
            target_error = max(
                0.01,
                (
                    np.sign(PeakValue_beam_phase) * (PeakValue_sum - PeakValue_delta)
                    + np.sign(PeakValue_beam_phase) * (PeakValue_sum + PeakValue_delta) / 2
                )
                / (PeakValue_sum + PeakValue_delta),
            )

        return PeakValue_sum, PeakValue_delta, PeakValue_beam_phase, sum_chan, target_error

    def process_sweep(self, state):
        self.update_hardware_state(state)

        phase_list = self._apply_phase_cal(state.get("phaseList", [0] * 8))
        bandwidth = state.get("BW", 0)
        mode = state.get("mode", "Beam Sweep")
        b0_gain = state.get("B0_Gain", 1.0)
        b1_gain = state.get("B1_Gain", 1.0)
        dig_B0_phase = state.get("Beam0_Phase", 0)
        dig_B1_phase = state.get("Beam1_Phase", 0)
        phase_values, angle_values, phase_step_size = self._compute_phase_sweep(state)
        if not phase_values:
            phase_values = [0.0]
            angle_values = [0.0]

        gain = []
        delta = []
        beam_phase = []
        angle = []
        diff_error = []

        max_signal = -100000
        data_fft = np.zeros(1024)

        for idx, PhDelta in enumerate(phase_values):
            ADAR_set_Phase(self.array, PhDelta, phase_step_size, phase_list)
            SteerAngle = (
                angle_values[idx]
                if idx < len(angle_values)
                else self.ConvertPhaseToSteerAngle(PhDelta, self.SignalFreq, bandwidth)
            )

            p_sum, p_delta, p_beam_phase, sum_chan, t_error = self.getData(self.Averages, b0_gain, b1_gain, dig_B0_phase, dig_B1_phase)

            if p_sum > max_signal:
                max_signal = p_sum
                data_fft = sum_chan

            if mode != "Signal vs Time":
                gain.append(p_sum)
                delta.append(p_delta)
                beam_phase.append(p_beam_phase)
                angle.append(SteerAngle)
                diff_error.append(t_error)
            else:
                gain.append(p_sum)

        # FFT processing
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
            "ArrayDelta": [float(x) for x in delta],
            "ArrayBeamPhase": [float(x) for x in beam_phase],
            "ArrayAngle": [float(x) for x in angle],
            "ArrayError": [float(x) for x in diff_error],
            "max_gain": max_gain.tolist(),
            "xf": xf.tolist(),
        }

    def shutdown(self):
        """Gracefully shut down hardware, following defensive cleanup patterns."""
        try:
            if hasattr(self, "gpios") and self.gpios:
                self.gpios.gpio_vctrl_1 = 0
                self.gpios.gpio_vctrl_2 = 0
        except Exception as e:
            print(f"Warning: GPIO shutdown error (may be expected): {e}")

        try:
            if hasattr(self, "array") and self.array:
                for device in self.array.devices.values():
                    device.reset()
        except Exception as e:
            print(f"Warning: ADAR reset error (may be expected): {e}")

        try:
            if hasattr(self, "sdr") and self.sdr:
                if hasattr(self.sdr, "rx_destroy_buffer"):
                    self.sdr.rx_destroy_buffer()
        except Exception as e:
            print(f"Warning: SDR cleanup error (may be expected): {e}")

        try:
            if hasattr(self, "lo"):
                del self.lo
        except Exception:
            pass
        try:
            if hasattr(self, "sdr"):
                del self.sdr
        except Exception:
            pass
        try:
            if hasattr(self, "array"):
                del self.array
        except Exception:
            pass
        try:
            if hasattr(self, "gpios"):
                del self.gpios
        except Exception:
            pass


    def switch_to_cw_mode(self):
        """Reconfigure SDR for CW radar: 600 kHz sample rate, 100 kHz TX tone."""
        self._saved_sample_rate = int(self.sdr.sample_rate)
        self._saved_buffer_size = int(self.sdr.rx_buffer_size)
        self.sdr.sample_rate = CW_SAMPLE_RATE
        self.sdr.rx_rf_bandwidth = int(CW_SAMPLE_RATE * 2.5)
        self.sdr.rx_buffer_size = CW_BUFFER_SIZE
        fc_bin = round(CW_IF_HZ / CW_SAMPLE_RATE * CW_BUFFER_SIZE)
        fc_exact = fc_bin * CW_SAMPLE_RATE / CW_BUFFER_SIZE
        t = np.arange(CW_BUFFER_SIZE) / CW_SAMPLE_RATE
        iq = (np.cos(2 * np.pi * fc_exact * t) + 1j * np.sin(2 * np.pi * fc_exact * t)) * 2**14
        self.sdr.tx([iq * 0.5, iq])

    def switch_to_sweep_mode(self):
        """Restore SDR to normal beam-sweep configuration."""
        saved_rate = getattr(self, '_saved_sample_rate', self.SampleRate)
        saved_buf = getattr(self, '_saved_buffer_size', config.buffer_size)
        self.sdr.sample_rate = saved_rate
        self.sdr.rx_rf_bandwidth = int(saved_rate * 2.5)
        self.sdr.rx_buffer_size = saved_buf
        self.sdr.tx([np.zeros(saved_buf, dtype=complex), np.zeros(saved_buf, dtype=complex)])

    def process_cw_radar(self):
        """Acquire one CW radar frame: FFT of coherent Rx sum, cropped to ±300 Hz around 100 kHz IF."""
        data = SDR_getData(self.sdr)
        signal = data[0] + data[1]
        N = len(signal)
        win = np.blackman(N)
        sp = np.abs(np.fft.fft(signal * win))
        sp = np.fft.fftshift(sp)
        s_mag = np.maximum(sp / np.sum(win), 1e-15)
        s_dbfs = 20 * np.log10(s_mag / 2**11)
        freq = np.fft.fftshift(np.fft.fftfreq(N, 1.0 / CW_SAMPLE_RATE))
        mask = np.abs(freq - CW_IF_HZ) <= CW_DISPLAY_BW_HZ
        return {
            "freq_hz": freq[mask].tolist(),
            "spectrum_dbfs": s_dbfs[mask].tolist(),
        }


class PhaserServerSim:
    """Drop-in simulated backend that preserves the websocket payload contract."""

    def __init__(self):
        self.c = 299792458
        self.SignalFreq = getattr(config, "SignalFreq", 10.525e9)
        try:
            self.SignalFreq = load_hb100_cal()
            print("Found signal freq file, ", self.SignalFreq)
        except Exception:
            print("No signal freq found, keeping at ", self.SignalFreq)

        self.Rx_freq = getattr(config, "Rx_freq", 2.2e9)
        self.SampleRate = getattr(config, "SampleRate", 3e6)
        self.Rx_gain = getattr(config, "Rx_gain", 30)
        self.Tx_gain = getattr(config, "Tx_gain", -10)
        self.Averages = getattr(config, "Averages", 1)
        self.d = getattr(config, "d", 0.014)
        self.Tx_mode = "Transmit Disabled"

    def process_sweep(self, state):
        self.SignalFreq = state.get("SignalFreq", self.SignalFreq)
        self.Rx_freq = state.get("Rx_freq", self.Rx_freq)
        self.Rx_gain = state.get("Rx_gain", self.Rx_gain)
        self.Tx_gain = state.get("Tx_gain", self.Tx_gain)
        self.Averages = state.get("Averages", self.Averages)
        self.d = state.get("d", self.d)

        phase_values = state.get("PhaseValues", [])
        mode = state.get("mode", "Beam Sweep")
        bw = state.get("BW", 0)

        # Keep sim behavior aligned with real backend fallback logic.
        if not phase_values and mode in ("Static Phase", "Signal vs Time"):
            phase_values = [0.0]

        gain = []
        delta = []
        beam_phase = []
        angle = []
        diff_error = []

        target_angle = 15
        for ph_delta in phase_values:
            denom = (2 * np.pi * (self.SignalFreq - bw * 1_000_000) * self.d) + 1
            value1 = (self.c * np.radians(np.abs(ph_delta))) / denom
            clamped = max(min(1, value1), -1)
            theta = np.degrees(np.arcsin(clamped))
            steer_angle = theta if ph_delta >= 0 else -theta

            diff = steer_angle - target_angle
            sim_gain = -10 - 20 * np.log10(max(abs(diff) / 10, 1) + 0.1)
            sim_delta = -40 - 10 * np.log10(max(abs(diff) / 5, 1) + 0.1)
            sim_phase = np.clip(diff / 90, -1, 1)
            sim_error = np.clip(diff / 45, -1, 1)

            if mode != "Signal vs Time":
                gain.append(float(sim_gain))
                delta.append(float(sim_delta))
                beam_phase.append(float(sim_phase))
                angle.append(float(steer_angle))
                diff_error.append(float(sim_error))
            else:
                gain.append(float(sim_gain + np.random.normal(0, 1.5)))

        num_samples = 1024
        ts = 1 / float(self.SampleRate)
        xf = np.fft.fftfreq(num_samples, ts)
        xf = np.fft.fftshift(xf)

        max_gain = -80 * np.ones(num_samples)
        max_gain[num_samples // 2 + 50] = -15
        max_gain += np.random.normal(0, 1.0, num_samples)

        return {
            "ArrayGain": gain,
            "ArrayDelta": delta,
            "ArrayBeamPhase": beam_phase,
            "ArrayAngle": angle,
            "ArrayError": diff_error,
            "max_gain": max_gain.tolist(),
            "xf": xf.tolist(),
        }

    def shutdown(self):
        return

    def reload_calibration(self, task_name=None):
        if task_name in (None, "find_hb100"):
            try:
                self.SignalFreq = load_hb100_cal()
            except Exception:
                pass

    def switch_to_cw_mode(self):
        pass

    def switch_to_sweep_mode(self):
        pass

    def process_cw_radar(self):
        n_bins = 128
        freq = np.linspace(CW_IF_HZ - CW_DISPLAY_BW_HZ, CW_IF_HZ + CW_DISPLAY_BW_HZ, n_bins)
        noise = np.random.normal(-62, 2.5, n_bins)
        target_df = 75 * np.sin(2 * np.pi * 0.3 * time.time())
        peak = np.exp(-0.5 * ((freq - (CW_IF_HZ + target_df)) / 8) ** 2) * 20 - 5
        spectrum = np.maximum(noise + peak, -80.0)
        return {
            "freq_hz": freq.tolist(),
            "spectrum_dbfs": spectrum.tolist(),
        }


def default_serializer(obj):
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(
        obj,
        (
            np.int_,
            np.intc,
            np.intp,
            np.int8,
            np.int16,
            np.int32,
            np.int64,
            np.uint8,
            np.uint16,
            np.uint32,
            np.uint64,
        ),
    ):
        return int(obj)
    if isinstance(obj, (np.floating, np.float16, np.float32, np.float64)):
        return float(obj)
    if isinstance(obj, (np.complexfloating, complex, np.complex64, np.complex128)):
        # Return complex as magnitude (magnitude only for FFT plotting)
        return float(np.abs(obj))
    return obj


def build_lab_preset(lab_idx: int):
    """PDF-aligned lab presets (docs/2025_Phaser_labs_Python.pdf).

    Kept in sync with the frontend's localLabPreset() in
    frontend/src/main.js so the desktop-app path (which reaches this via
    BackendService) produces the same starting state as the browser path.
    """
    base = {
        "mode": "Beam Sweep",
        "gainList": [100, 100, 100, 100, 100, 100, 100, 100],
        "phaseList": [0, 0, 0, 0, 0, 0, 0, 0],
        "steer_res": 2.8125,
        "bits": 7,
        "ignore_res": True,
        "BW": 10,
        "B0_Gain": 1.0,
        "B1_Gain": 1.0,
        "Beam0_Phase": 0,
        "Beam1_Phase": 0,
        "ui_tab": "tab-rect",
    }

    if lab_idx == 1:
        # STEERING ANGLE (p.9): FFT tab, uniform array
        base.update({"mode": "Beam Sweep", "ui_tab": "tab-fft"})
    elif lab_idx == 2:
        # ARRAY FACTOR AND BEAMWIDTH (p.11): uniform 8-element
        base.update({"mode": "Beam Sweep", "ui_tab": "tab-rect"})
    elif lab_idx == 3:
        # SIDELOBES AND TAPERING (p.16): symmetric taper enforcement on
        base.update({
            "mode": "Beam Sweep", "ui_tab": "tab-rect",
            "symmetricTaper": True,
        })
    elif lab_idx == 4:
        # GRATING LOBES (p.17): elements 1,4,7 active → d_eff = 3d = 42mm
        base.update({
            "mode": "Beam Sweep", "ui_tab": "tab-rect",
            "gainList": [100, 0, 0, 100, 0, 0, 100, 0],
        })
    elif lab_idx == 5:
        # BEAM SQUINT (p.19): 500 MHz signal BW
        base.update({
            "mode": "Beam Sweep", "ui_tab": "tab-rect",
            "BW": 500,
        })
    elif lab_idx == 6:
        # QUANTIZATION SIDELOBES (p.21): Blackman taper is the PDF's stated
        # pre-programmed default; student then reduces Phase Shift Bits to
        # see quantization sidelobes.
        base.update({
            "mode": "Beam Sweep", "ui_tab": "tab-rect",
            "gainList": [6, 27, 66, 100, 100, 66, 27, 6],
            "ignore_res": True,
        })
    elif lab_idx == 7:
        # MEASURING THE ACTUAL ANTENNA PATTERN (p.14): Signal vs Time so
        # student manually rotates the HB100 to trace amplitude vs time.
        base.update({"mode": "Signal vs Time", "ui_tab": "tab-tracking"})
    elif lab_idx == 8:
        # MONOPULSE TRACKING (p.28): Tracking mode + Blackman taper +
        # delta/error display
        base.update({
            "mode": "Tracking", "ui_tab": "tab-rect",
            "gainList": [6, 27, 66, 100, 100, 66, 27, 6],
            "showDelta": True, "showError": True,
        })

    return base


class BackendService:
    """Framework-agnostic application service used by FastAPI and future IPC workers."""

    def __init__(self, sim_mode: bool):
        self.sim_mode = bool(sim_mode)
        self.hardware = None
        self.calibration_lock = threading.Lock()
        self._cal_log_file = None
        self._cal_process = None
        self.calibration_status = {
            "running": False,
            "task": None,
            "pid": None,
            "started_at": None,
            "finished_at": None,
            "returncode": None,
            "log_path": None,
            "outcome": None,
            "evidence": {},
            "last_lines": [],
        }

    def startup(self):
        if self.sim_mode:
            self.hardware = PhaserServerSim()
            print("FastAPI Application has started up (SIM mode).")
        else:
            try:
                self.hardware = PhaserServer()
                print("FastAPI Application has started up (REAL mode).")
            except Exception as e:
                print(f"Hardware initialization failed: {e}")
                print("Running in degraded mode - hardware features unavailable")
                self.hardware = None

    def shutdown(self):
        if self.hardware:
            self.hardware.shutdown()
            print("Hardware safely shut down.")

    def process_sweep(self, state_msg):
        if self.hardware is None:
            raise RuntimeError("Hardware service is not initialized")
        return self.hardware.process_sweep(state_msg)

    def switch_to_cw_mode(self):
        if self.hardware:
            self.hardware.switch_to_cw_mode()

    def switch_to_sweep_mode(self):
        if self.hardware:
            self.hardware.switch_to_sweep_mode()

    def process_cw_radar(self):
        if self.hardware is None:
            raise RuntimeError("Hardware not initialized")
        return self.hardware.process_cw_radar()

    def get_ui_state(self):
        signal_freq = getattr(config, "SignalFreq", 10.5e9)
        rx_freq = getattr(config, "Rx_freq", 2.4e9)
        rx_gain = getattr(config, "Rx_gain", 20)
        tx_gain = getattr(config, "Tx_gain", -40)
        averages = getattr(config, "Averages", 1)
        spacing = getattr(config, "d", 0.014)
        bandwidth = 10

        if self.hardware is not None:
            signal_freq = self.hardware.SignalFreq
            rx_freq = self.hardware.Rx_freq
            rx_gain = self.hardware.Rx_gain
            tx_gain = self.hardware.Tx_gain
            averages = self.hardware.Averages
            spacing = self.hardware.d
            bandwidth = getattr(self.hardware, "bandwidth", 10)

        # Check hardware connectivity
        hardware_connected = False
        if self.sim_mode:
            hardware_connected = True  # Sim is always "connected"
        elif self.hardware is None:
            hardware_connected = False  # Hardware init failed
        else:
            try:
                # Quick connectivity check - try to read an attribute from the SDR
                if hasattr(self.hardware, 'sdr') and self.hardware.sdr is not None:
                    _ = self.hardware.sdr.rx_lo
                    hardware_connected = True
            except Exception:
                hardware_connected = False

        return {
            "status": "ok",
            "data": {
                "SignalFreq": float(signal_freq),
                "Rx_freq": float(rx_freq),
                "Rx_gain": int(rx_gain),
                "Tx_gain": int(tx_gain),
                "Averages": int(averages),
                "d": float(spacing),
                "BW": float(bandwidth),
                "sim_mode": bool(self.sim_mode),
                "hardware_connected": hardware_connected,
                "lab_presets_supported": True,
            },
        }

    def get_lab_preset(self, lab_idx: int):
        if lab_idx < 1 or lab_idx > 8:
            return {"status": "error", "message": "Lab index must be in range 1..8"}
        return {"status": "ok", "data": build_lab_preset(lab_idx)}

    def get_calibration_status(self):
        with self.calibration_lock:
            return {"status": "ok", "data": dict(self.calibration_status)}

    def run_calibration(self, task_name: str):
        if self.sim_mode:
            return {"status": "error", "message": "Calibration is not available in sim mode"}
        try:
            # Release hardware before calibration so the script can use it
            if self.hardware is not None:
                print("[BackendService] Releasing hardware for calibration...")
                self.hardware.shutdown()
                self.hardware = None
            self._start_calibration_task(task_name)
            return {"status": "ok", "message": f"Started {task_name}"}
        except RuntimeError as e:
            return {"status": "error", "message": str(e)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cancel_calibration(self):
        with self.calibration_lock:
            if not self.calibration_status["running"]:
                return {"status": "error", "message": "No calibration task is running"}
            proc = self._cal_process
            if proc is None:
                return {"status": "error", "message": "No process reference available"}
            task_name = self.calibration_status["task"]
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._append_cal_line(f"[CANCELLED] {task_name} was cancelled by user")
        return {"status": "ok", "message": f"Cancelled {task_name}"}

    def reboot_phaser(self):
        """SSH into Phaser and reboot it."""
        if self.sim_mode:
            return {"status": "error", "message": "Reboot is not available in sim mode"}
        try:
            import subprocess
            result = subprocess.run(
                ["ssh", "-o", "StrictHostKeyChecking=no", "-o", "ConnectTimeout=5",
                 "root@phaser.local", "reboot"],
                capture_output=True, text=True, timeout=10
            )
            if result.returncode == 0 or "Connection to phaser.local closed" in result.stderr:
                return {"status": "ok", "message": "Phaser is rebooting. Please wait ~30 seconds."}
            return {"status": "error", "message": f"SSH failed: {result.stderr}"}
        except subprocess.TimeoutExpired:
            return {"status": "error", "message": "SSH connection timed out"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _append_cal_line(self, line):
        self.calibration_status["last_lines"].append(line)
        # Keep a short rolling tail for UI display.
        self.calibration_status["last_lines"] = self.calibration_status["last_lines"][-40:]
        if self._cal_log_file:
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self._cal_log_file.write(f"[{ts}] {line}\n")
            self._cal_log_file.flush()

    @staticmethod
    def _extract_hb100_peak(line):
        marker = "Peak frequency found at"
        if marker not in line:
            return None
        try:
            raw = line.split(marker, 1)[1].replace("GHz.", "").replace("GHz", "").strip()
            return float(raw)
        except Exception:
            return None

    def _calibration_reader(self, proc, task_name):
        evidence = {
            "network": [],
            "errors": [],
            "hb100_peak_ghz": None,
            "saved": False,
        }
        try:
            if proc.stdout:
                for line in proc.stdout:
                    text = line.rstrip()
                    self._append_cal_line(text)
                    if text.startswith("[OK]") or text.startswith("[FAIL]"):
                        evidence["network"].append(text)
                    if text.startswith("[ERROR]"):
                        evidence["errors"].append(text)
                    peak_ghz = self._extract_hb100_peak(text)
                    if peak_ghz is not None:
                        evidence["hb100_peak_ghz"] = peak_ghz
                    if "HB100 Freq saved to file" in text:
                        evidence["saved"] = True
        except Exception as e:
            self._append_cal_line(f"[reader] {e}")
        finally:
            rc = proc.wait()
            if rc == 0:
                self._reload_runtime_calibration(task_name)
                self._append_cal_line(f"[RESULT] {task_name} completed successfully")
            else:
                self._append_cal_line(f"[RESULT] {task_name} failed with return code {rc}")
            with self.calibration_lock:
                self.calibration_status["running"] = False
                self.calibration_status["returncode"] = rc
                self.calibration_status["pid"] = None
                self.calibration_status["task"] = task_name
                self.calibration_status["finished_at"] = time.time()
                self.calibration_status["outcome"] = "success" if rc == 0 else "error"
                self.calibration_status["evidence"] = evidence
            if self._cal_log_file:
                self._cal_log_file.close()
                self._cal_log_file = None

    def _reload_runtime_calibration(self, task_name):
        """Refresh live backend state after successful calibration subprocess."""
        # Reinitialize hardware if it was released for calibration
        if self.hardware is None:
            print("[BackendService] Reinitializing hardware after calibration...")
            self.hardware = PhaserServer()
        if hasattr(self.hardware, "reload_calibration"):
            self.hardware.reload_calibration(task_name)

    @staticmethod
    def _find_uv():
        """Check if uv is available on PATH."""
        return shutil.which("uv")

    def _resolve_calibration_python(self, repo_root):
        """Pick the interpreter used for calibration subprocesses.

        Priority:
        1) PHASER_CAL_PYTHON override
        2) uv run python (if uv is available and pyproject.toml exists)
        3) Repo virtualenv interpreter (.venv)
        4) Current process interpreter

        Returns (command_parts, source_description) where command_parts is a list
        of arguments to prepend to the script path.
        """
        env_python = os.environ.get("PHASER_CAL_PYTHON", "").strip()
        if env_python:
            return [env_python], "env:PHASER_CAL_PYTHON"

        # Use platform-specific venv first
        if os.name == "nt":
            venv_python = repo_root / ".venv-win" / "Scripts" / "python.exe"
            if venv_python.exists():
                return [str(venv_python)], "repo:.venv-win"
            # Fallback to generic .venv
            venv_python = repo_root / ".venv" / "Scripts" / "python.exe"
        else:
            venv_python = repo_root / ".venv-linux" / "bin" / "python"
            if venv_python.exists():
                return [str(venv_python)], "repo:.venv-linux"
            # Fallback to generic .venv
            venv_python = repo_root / ".venv" / "bin" / "python"
        if venv_python.exists():
            return [str(venv_python)], "repo:.venv"

        # Fallback to uv run if available
        uv_path = self._find_uv()
        if uv_path and (repo_root / "pyproject.toml").exists():
            return [uv_path, "run", "python"], "uv run python"

        return [sys.executable], "sys.executable"

    def _start_calibration_task(self, task_name):
        script_map = {
            "phaser_cal": "phaser_cal.py",
            "find_hb100": "phaser_find_hb100.py",
        }
        script_name = script_map.get(task_name)
        if not script_name:
            raise ValueError(f"Unknown calibration task: {task_name}")

        repo_root = Path(__file__).resolve().parent
        script_path = repo_root / script_name
        if not script_path.exists():
            raise FileNotFoundError(f"Calibration script not found: {script_name}")

        python_cmd, python_source = self._resolve_calibration_python(repo_root)

        with self.calibration_lock:
            if self.calibration_status["running"]:
                raise RuntimeError("A calibration task is already running")

            self.calibration_status["running"] = True
            self.calibration_status["task"] = task_name
            self.calibration_status["returncode"] = None
            self.calibration_status["started_at"] = time.time()
            self.calibration_status["finished_at"] = None
            self.calibration_status["outcome"] = None
            self.calibration_status["evidence"] = {}

            logs_dir = repo_root / "logs" / "calibration"
            logs_dir.mkdir(parents=True, exist_ok=True)
            run_stamp = time.strftime("%Y%m%d-%H%M%S")
            log_path = logs_dir / f"{run_stamp}_{task_name}.log"
            self._cal_log_file = open(log_path, "a", encoding="utf-8", buffering=1)
            self.calibration_status["log_path"] = str(log_path)

            full_cmd = python_cmd + [str(script_path)]
            self.calibration_status["last_lines"] = [
                f"Starting {script_name}...",
                f"command={' '.join(full_cmd)} ({python_source})",
                f"cwd={script_path.parent}",
                f"PHASER_RPI_URI={os.environ.get('PHASER_RPI_URI', '<unset>')}",
                f"PHASER_SDR_URI={os.environ.get('PHASER_SDR_URI', '<unset>')}",
                f"log_path={log_path}",
            ]

            env = os.environ.copy()
            env["MPLBACKEND"] = "Agg"  # Force non-interactive backend
            env.setdefault("PYTHONUNBUFFERED", "1")
            try:
                proc = subprocess.Popen(
                    full_cmd,
                    cwd=str(script_path.parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    env=env,
                )
            except Exception as exc:
                self._append_cal_line(f"[ERROR] Failed to launch {script_name}: {exc}")
                self.calibration_status["running"] = False
                self.calibration_status["returncode"] = -1
                self.calibration_status["finished_at"] = time.time()
                self.calibration_status["outcome"] = "launch_error"
                if self._cal_log_file:
                    self._cal_log_file.close()
                    self._cal_log_file = None
                raise

            self.calibration_status["pid"] = proc.pid
            self._cal_process = proc
            thread = threading.Thread(target=self._calibration_reader, args=(proc, task_name), daemon=True)
            thread.start()

