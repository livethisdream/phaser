"""Physics-based hardware stubs for --sim mode in phaser_headless.py.

The stubs mimic just enough of adi.one_bit_adc_dac / adi.adar1000_array /
adi.ad9361 for phaser_headless.py to run with no Phaser attached. The SDR
stub synthesizes element-level IQ from a boresight HB100 tone, applies the
element phases + taper the ADAR stub has captured, sums into two digital
sub-arrays (chan0 = elements 1-4, chan1 = elements 5-8), and returns the
same [chan0, chan1] shape SDR_getData produces on real hardware.

PORTED TO JAVASCRIPT. frontend/src/sim/ runs this same physics in the browser
so the dashboard works with no Pi attached (and so the GitHub Pages demo works
at all). This file is the source of truth; the port follows it.

Changing the physics here means:
  1. python tools/gen_sim_constants.py   -- if a constant moved
  2. mirror the change in frontend/src/sim/
  3. pytest tests/test_sim_parity.py     -- which will fail until you do
"""

import numpy as np


class _StubGPIOs:
    """Swallows attribute writes (gpio_vctrl_1 = 1, gpio_tx_sw = 0, ...)."""

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class _StubElement:
    """One element slot on the ADAR array.

    Models the two-stage write the real part has: `rx_gain` / `rx_phase` land
    in SPI shadow registers, and only a latch (RX_LOAD / `rx_load_spi`) moves
    them into the beam state the RF path actually uses. `latched_gain` and
    `latched_phase` are that beam state, and they are what SimSDR reads.

    Modelling this is the point, not pedantry: a caller that writes phases and
    forgets to latch gets a beam that never moves, on the real array and here
    alike -- which is the flat-line-instead-of-a-beam-pattern failure.
    """

    def __init__(self):
        # SPI shadow registers (what the driver writes).
        self.rx_gain = 100
        self.rx_phase = 0.0
        self.rx_attenuator = False
        # Live beam state (what the RF path uses). Starts matching the
        # shadow, so a correctly-latching caller behaves exactly as before.
        self.latched_gain = 100
        self.latched_phase = 0.0

    def latch(self):
        self.latched_gain = 0 if self.rx_attenuator else self.rx_gain
        self.latched_phase = self.rx_phase


class _StubChannel:
    """One of the four Rx channels on an ADAR1000 chip."""

    def __init__(self):
        self.rx_enable = False


class _StubDevice:
    """One ADAR1000 chip. reset() is a no-op; register writes are recorded."""

    def __init__(self):
        self.mode = "rx"
        self.channels = [_StubChannel() for _ in range(4)]

    def reset(self):
        pass


class _StubADARArray:
    """Stands in for adi.adar1000_array.

    Exposes:
      - elements: dict {1..8 -> _StubElement}, written by ADAR_set_Taper/Phase
      - devices:  dict {name -> _StubDevice}, iterated by ADAR_init/ADAR_set_mode
      - latch_rx_settings(), which commits the element writes to beam state

    The SDR stub reads back the *latched* gain/phase to build the per-element
    weighting used to synthesize IQ.
    """

    def __init__(self):
        self.elements = {i + 1: _StubElement() for i in range(8)}
        self.devices = {"BEAM0": _StubDevice(), "BEAM1": _StubDevice()}
        self.latch_count = 0
        # Intrinsic per-element phase error, degrees -- the thing the phase
        # calibration exists to cancel. Zero by default, so a plain sim run is
        # a perfectly matched array; a test sets it to give pcal something
        # real to correct.
        self.element_phase_error = [0.0] * 8

    def latch_rx_settings(self):
        """Commit every element's shadow registers to its beam state."""
        for element in self.elements.values():
            element.latch()
        self.latch_count += 1


class SimSDR:
    """Stub adi.ad9361. Records LO/gain/etc.; rx() synthesizes IQ per call.

    The array stub is passed in so rx() can read the currently-commanded
    per-element rx_gain and rx_phase and produce a physically-consistent
    beam response.
    """

    # Target sits at boresight (0°) — matches an HB100 pointed straight at
    # the array, per user preference.
    TARGET_ANGLE_DEG = 0.0

    # IF tone the sim places in the baseband spectrum (Hz). Real target
    # appears at some frequency after downconversion; we just pick a value
    # inside the passband so the FFT plot shows a peak.
    TARGET_IF_HZ = 1.0e6

    # Interferer defaults. All off by default so plain sim mode remains a
    # clean single-target scene. Enabled and configured from phaser_headless
    # via set_interferer(); the frontend "Simulator Interferer" panel is
    # hidden unless the app is loaded with ?instructor=1.
    #
    # NOTE: Interferer runs at the SAME IF as the target on purpose. This
    # is the classic textbook MVDR setup — two co-frequency sources at
    # different arrival angles, where the covariance matrix has clean
    # rank-2 structure and MVDR can place a hard null on the interferer.
    # A different-IF interferer is more realistic but shows much weaker
    # nulling because the sources are then temporally uncorrelated.
    INTERFERER_IF_HZ = 1.0e6  # same as TARGET_IF_HZ (see comment above)

    # Output scaling. Chosen so a full-taper on-boresight beam peaks around
    # -10 dBFS in the sum channel, matching typical HB100 signal levels on real
    # hardware (leaves headroom before the 2^11 fixed-point full-scale).
    AMP_SCALE = 60.0

    # Additive complex noise, per sub-array, independent. Sigma keeps
    # per-element SNR ~28 dB, so the beam pattern shows a clean main lobe with
    # a realistic sidelobe-vs-noise ratio. Set to 0 to get a noiseless array --
    # tests/test_sim_parity.py does exactly that, because NumPy's PCG64 stream
    # cannot be reproduced in JS and only the deterministic physics can be
    # compared.
    NOISE_SIGMA = 4.0

    # CW radar scene. Each target is (velocity_mps, angle_deg, amplitude);
    # velocity is positive for closing targets. The zero-velocity entry is the
    # Tx->Rx leakage every real CW radar has, and it is deliberately the
    # strongest return -- it is what puts the big spike at 0 m/s that the
    # workshop asks you to look past.
    CW_DEFAULT_TARGETS = (
        (0.0, 0.0, 1.0),     # direct leakage / stationary clutter
        (2.5, 0.0, 0.45),    # a walking target, closing
        (-1.2, 15.0, 0.25),  # a slower one receding, off boresight
    )

    # FMCW radar scene. Each target is (range_m, velocity_mps, angle_deg,
    # amplitude); velocity is positive for closing targets. Ranges are chosen
    # to sit inside the default 20 m window and to be resolvable at the
    # default 500 MHz of chirp bandwidth (0.3 m resolution), so the range plot
    # shows separate returns rather than one smear.
    FMCW_DEFAULT_TARGETS = (
        (1.0, 0.0, 0.0, 1.0),    # the lab's corner reflector, stationary at 1 m
        (3.5, 1.8, 0.0, 0.6),    # something walking in at 3.5 m
        (7.0, -2.4, 12.0, 0.35), # receding, off boresight
    )

    def __init__(self, array, signal_freq, element_spacing, sample_rate=3e6,
                 buffer_size=1024 * 16):
        self._array = array
        self._SignalFreq = signal_freq
        self._d = element_spacing
        self.sample_rate = int(sample_rate)
        self.rx_buffer_size = int(buffer_size)

        # Attributes touched by SDR_init / SDR_setRx / SDR_setTx / CW radar
        self.rx_lo = 0
        self.tx_lo = 0
        self.rx_rf_bandwidth = int(sample_rate)
        self.tx_rf_bandwidth = int(sample_rate)
        self.rx_enabled_channels = [0, 1]
        self.tx_enabled_channels = [0, 1]
        self.tx_cyclic_buffer = True
        self.gain_control_mode_chan0 = "manual"
        self.gain_control_mode_chan1 = "manual"
        self.rx_hardwaregain_chan0 = 30
        self.rx_hardwaregain_chan1 = 30
        self.tx_hardwaregain_chan0 = -30
        self.tx_hardwaregain_chan1 = -30

        # Interferer state (default: disabled)
        self._interferer_enable = False
        self._interferer_angle_deg = 30.0
        self._interferer_power_db = 0.0   # relative to target amplitude

        # CW radar state. Off until phaser_headless enters cw_radar mode, at
        # which point rx() synthesizes Doppler returns instead of the plain
        # HB100 beamforming scene.
        self._cw_enable = False
        self._cw_signal_freq = 100_000.0
        self._cw_output_freq = 12.2e9
        self._cw_targets = list(self.CW_DEFAULT_TARGETS)

        # FMCW radar state. Like the CW state above, off until the backend
        # enters the mode.
        self._fmcw_enable = False
        self._fmcw_targets = list(self.FMCW_DEFAULT_TARGETS)
        self._fmcw = {
            "chirp_bw": 500e6,
            "ramp_time": 1e-3,
            "pri": 1e-3,
            "num_chirps": 1,
            "signal_freq": 100_000.0,
            "output_freq": 12.2e9,
        }

        # Tx buffer bookkeeping, so enter_cw_mode's tx()/tx_destroy_buffer()
        # calls have something to land on.
        self._tx_buffer = None

        self._rng = np.random.default_rng(0)

    def set_signal_freq(self, freq_hz):
        """Kept in sync from phaser_headless.set_signal_freq so the spatial
        phase progression uses the current SignalFreq."""
        self._SignalFreq = float(freq_hz)

    def set_interferer(self, enable=None, angle_deg=None, power_db=None):
        """Configure the simulator interferer. Called from phaser_headless
        when the frontend (instructor mode) updates interferer state."""
        if enable is not None:
            self._interferer_enable = bool(enable)
        if angle_deg is not None:
            self._interferer_angle_deg = float(angle_deg)
        if power_db is not None:
            self._interferer_power_db = float(power_db)

    def set_cw_mode(self, enable, signal_freq=None, output_freq=None,
                    targets=None):
        """Switch the synthesized scene between beamforming and CW radar.

        phaser_headless calls this when entering/leaving cw_radar mode, so
        --sim exercises the whole radar path -- capture, Doppler processing,
        waterfall, taper response -- with no Phaser attached. Radar mode used
        to be refused outright under --sim, which meant the radar UI could not
        be developed or demonstrated without hardware on the bench.
        """
        self._cw_enable = bool(enable)
        if self._cw_enable:
            self._fmcw_enable = False
        if signal_freq is not None:
            self._cw_signal_freq = float(signal_freq)
        if output_freq is not None:
            self._cw_output_freq = float(output_freq)
        if targets is not None:
            self.set_cw_targets(targets)

    def set_cw_targets(self, targets):
        """Replace the simulated CW scene. Each entry is (v_mps, angle_deg, amp)."""
        scene = []
        for entry in targets or ():
            try:
                v, angle, amp = entry
            except (TypeError, ValueError):
                continue
            scene.append((float(v), float(angle), float(amp)))
        self._cw_targets = scene or list(self.CW_DEFAULT_TARGETS)

    def set_fmcw_mode(self, enable, **params):
        """Switch the synthesized scene to (or away from) FMCW radar.

        Accepts any of chirp_bw, ramp_time, pri, num_chirps, signal_freq,
        output_freq, and a `targets` list. CW and FMCW are mutually exclusive:
        entering one leaves the other, the same way the hardware's mode
        dispatcher works.
        """
        targets = params.pop("targets", None)
        for key, value in params.items():
            if key in self._fmcw and value is not None:
                self._fmcw[key] = float(value)
        if targets is not None:
            self.set_fmcw_targets(targets)
        self._fmcw_enable = bool(enable)
        if self._fmcw_enable:
            self._cw_enable = False

    def set_fmcw_targets(self, targets):
        """Replace the simulated FMCW scene.

        Each entry is (range_m, velocity_mps, angle_deg, amplitude).
        """
        scene = []
        for entry in targets or ():
            try:
                r, v, angle, amp = entry
            except (TypeError, ValueError):
                continue
            scene.append((float(r), float(v), float(angle), float(amp)))
        self._fmcw_targets = scene or list(self.FMCW_DEFAULT_TARGETS)

    def _synthesize_fmcw(self):
        """FMCW scene: one beat tone per target, with slow-time Doppler phase.

        The Phaser mixes the echo against the transmitted ramp in hardware, so
        what reaches the Pluto is already the beat -- there is no chirp to
        synthesize, only its result. For a target at range R closing at v:

            f_b = 2*S*R/c        (S = chirp_bw / ramp_time)
            f_d = 2*v*f_c/c

        and sample n of chirp m carries phase
            2*pi*[ (signal_freq + f_b + f_d)*t_fast + f_d*m*PRI ]

        The fast-time term is what the range FFT reads; the slow-time term,
        advancing by f_d*PRI per chirp, is what the Doppler FFT reads. Those
        are exactly the two relations phaser_radar_dsp inverts, so a sign error
        on either side shows up as a target in the wrong place rather than
        cancelling out.

        Samples are laid out chirp-major to match chirp_matrix(), which
        reshapes (num_chirps, samples_per_chirp) and transposes.
        """
        n_total = int(self.rx_buffer_size)
        fs = float(self.sample_rate)
        cfg = self._fmcw

        num_chirps = max(1, int(cfg["num_chirps"]))
        spc = max(1, n_total // num_chirps)
        usable = spc * num_chirps

        slope = float(cfg["chirp_bw"]) / float(cfg["ramp_time"])
        f_c = float(cfg["output_freq"])
        c = 299_792_458.0
        wavelength = c / max(f_c, 1.0)

        t_fast = np.arange(spc) / fs
        m_idx = np.arange(num_chirps)

        chan_sub = [
            np.zeros(n_total, dtype=np.complex64),
            np.zeros(n_total, dtype=np.complex64),
        ]

        for target_range, velocity, angle_deg, amplitude in self._fmcw_targets:
            f_beat = 2.0 * slope * float(target_range) / c
            f_doppler = 2.0 * float(velocity) * f_c / c

            fast_phase = 2 * np.pi * (float(cfg["signal_freq"]) + f_beat + f_doppler) * t_fast
            slow_phase = 2 * np.pi * f_doppler * m_idx * float(cfg["pri"])

            wave = np.exp(1j * (fast_phase[None, :] + slow_phase[:, None]))
            wave = wave.ravel().astype(np.complex64)

            if usable < n_total:
                # A buffer that is not a whole number of chirps gets the
                # remainder zero-filled; chirp_matrix drops it anyway.
                wave = np.concatenate([wave, np.zeros(n_total - usable, np.complex64)])

            self._superpose_wave(chan_sub, wave, angle_deg, amplitude, wavelength)

        return chan_sub

    # enter_cw_mode loads a cyclic Tx buffer and tears it down again. Nothing
    # in the sim transmits, but the calls have to succeed.
    def tx(self, data):
        self._tx_buffer = data

    def tx_destroy_buffer(self):
        self._tx_buffer = None

    def _synthesize_cw(self):
        """CW radar scene: the IF tone plus one Doppler-shifted line per target.

        A target closing at v shifts the return by f_d = 2*v*f_carrier/c, so it
        lands at (signal_freq + f_d) in the baseband spectrum. That is exactly
        the relation process_cw_frame inverts to get velocity back, which makes
        this a real round trip rather than a canned plot: get the LO or the
        carrier wrong and the recovered velocity is wrong with it.
        """
        N = self.rx_buffer_size
        fs = float(self.sample_rate)
        t = np.arange(N) / fs

        c = 299_792_458.0
        # Spatial phase uses the transmitted carrier, not the HB100 SignalFreq:
        # in radar mode the array is receiving our own echo at output_freq.
        wavelength = c / max(self._cw_output_freq, 1.0)

        chan_sub = [
            np.zeros(N, dtype=np.complex64),
            np.zeros(N, dtype=np.complex64),
        ]

        for velocity, angle_deg, amplitude in self._cw_targets:
            f_doppler = 2.0 * velocity * self._cw_output_freq / c
            self._superpose_source(
                chan_sub, t,
                if_hz=self._cw_signal_freq + f_doppler,
                arrival_angle_deg=angle_deg,
                amplitude=amplitude,
                wavelength=wavelength,
            )

        return chan_sub

    def _superpose_source(self, chan_sub, t, if_hz, arrival_angle_deg,
                          amplitude, wavelength):
        """Add one narrowband plane-wave source at (arrival_angle_deg, if_hz,
        amplitude) into the two sub-array accumulators.

        Physical model per element k (k=0..7):
          phi_incident_k = 2*pi * (k * d / lambda) * sin(theta)
          elem_signal   = (gain_k/127) * exp(j*if*t)
                          * exp(j*(phi_incident_k - rx_phase_k))
        Sub-array 0 = elements 0..3, sub-array 1 = elements 4..7 (matches
        the ADAR device_element_map wiring in _do_init_hardware).
        """
        wave = np.exp(1j * 2 * np.pi * if_hz * t).astype(np.complex64)
        self._superpose_wave(chan_sub, wave, arrival_angle_deg, amplitude,
                             wavelength)

    def _superpose_wave(self, chan_sub, wave, arrival_angle_deg, amplitude,
                        wavelength):
        """The element loop of _superpose_source, over a precomputed waveform.

        Split out for the FMCW scene, whose waveform is not a single tone: its
        phase advances across slow time as well as fast, so it cannot be
        expressed as exp(j*2*pi*f*t) for one f. The spatial half -- taper,
        latched phase, element error, sub-array mapping -- is identical, which
        is the point of sharing it: the simulated radar array steers and tapers
        exactly like the simulated beamforming array.
        """
        theta_rad = np.radians(arrival_angle_deg)
        for k in range(8):
            el = self._array.elements[k + 1]
            # Latched beam state, not the shadow registers -- an unlatched
            # write must not steer the simulated array either.
            gain = max(0.0, min(127.0, float(el.latched_gain))) / 127.0
            rx_phase_rad = np.radians(float(el.latched_phase))
            # Intrinsic element phase error, which the commanded rx_phase has
            # to absorb via the phase calibration.
            err_rad = np.radians(float(self._array.element_phase_error[k]))
            phi_incident = (
                2 * np.pi * (k * self._d / wavelength) * np.sin(theta_rad) + err_rad
            )
            elem_signal = amplitude * gain * wave * np.exp(
                1j * (phi_incident - rx_phase_rad)
            )
            sub = 0 if k < 4 else 1
            chan_sub[sub] = chan_sub[sub] + elem_signal

    def _synthesize_channels(self):
        """Return (chan0, chan1) as 1D complex arrays, shape (buffer_size,).

        In CW radar mode the scene comes from _synthesize_cw() instead (the
        Doppler returns), and in FMCW mode from _synthesize_fmcw() (beat tones
        with slow-time Doppler); only the scaling and noise below are shared.

        Sums:
          - Target: plane wave at TARGET_ANGLE_DEG, IF = TARGET_IF_HZ, unit amp
          - Interferer (if enabled): plane wave at interferer_angle_deg,
            IF = INTERFERER_IF_HZ, amp = 10^(power_db/20) relative to target
          - Independent complex noise per sub-array
        """
        N = self.rx_buffer_size
        fs = float(self.sample_rate)

        if self._fmcw_enable:
            chan_sub = self._synthesize_fmcw()
        elif self._cw_enable:
            chan_sub = self._synthesize_cw()
        else:
            t = np.arange(N) / fs

            c = 299_792_458.0
            wavelength = c / self._SignalFreq

            chan_sub = [
                np.zeros(N, dtype=np.complex64),
                np.zeros(N, dtype=np.complex64),
            ]

            # Target (always present)
            self._superpose_source(
                chan_sub, t,
                if_hz=self.TARGET_IF_HZ,
                arrival_angle_deg=self.TARGET_ANGLE_DEG,
                amplitude=1.0,
                wavelength=wavelength,
            )

            # Interferer (only when instructor mode has enabled it). Uses the
            # same IF as the target — see class comment for rationale.
            if self._interferer_enable:
                interf_amp = 10.0 ** (self._interferer_power_db / 20.0)
                self._superpose_source(
                    chan_sub, t,
                    if_hz=self.INTERFERER_IF_HZ,
                    arrival_angle_deg=self._interferer_angle_deg,
                    amplitude=interf_amp,
                    wavelength=wavelength,
                )

        amp = self.AMP_SCALE
        chan0 = chan_sub[0] * amp
        chan1 = chan_sub[1] * amp

        noise_sigma = self.NOISE_SIGMA
        if noise_sigma <= 0:
            return chan0, chan1
        chan0 = chan0 + (self._rng.normal(0, noise_sigma, N)
                         + 1j * self._rng.normal(0, noise_sigma, N)).astype(np.complex64)
        chan1 = chan1 + (self._rng.normal(0, noise_sigma, N)
                         + 1j * self._rng.normal(0, noise_sigma, N)).astype(np.complex64)

        return chan0, chan1

    def rx(self):
        c0, c1 = self._synthesize_channels()
        return [c0, c1]


def make_stub_gpios():
    return _StubGPIOs()


def make_stub_array():
    return _StubADARArray()
