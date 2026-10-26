# Hardware probes

Diagnostic scripts that run **on the Pi**, against a real kit with a powered
HB100 aimed at the array. They are not part of the pytest suite: `pyproject.toml`
collects `test_*.py` under `tests/`, and nothing here matches that, so `pytest`
walks past the directory. Running one means copying it next to the backend and
running it by hand:

```
scp tests/hardware/cal_probe.py analog@<kit>:/home/analog/pyadi-iio/examples/phaser/
ssh analog@<kit> 'cd /home/analog/pyadi-iio/examples/phaser && python3 -u cal_probe.py'
```

All three are read-only: they save no calibration and leave the array at full
gain. They expect `phaser_functions.py` and `config.py` beside them, which is
what the backend directory already has.

## What each one answered

These were written to diagnose one real failure on kit **phaser-00**
(2026-10-15): calibration aborting at the array-contrast gate with
`contrast: 0.93 dB`, while the HB100 search on the same kit read 75 dB SNR.
They are kept because the questions recur, not as a record of that week.

| script | question |
| :-- | :-- |
| `cal_probe.py` | Is the contrast gate firing on a stale RX buffer or on a genuinely deaf chain? Prints the contrast under four flush strategies and sweeps `Rx_freq`. |
| `lo_cliff_probe.py` | Is a sharp reception edge a PLL lock failure or an analog limit? Fine sweep, hysteresis check, and seven relock attempts. |
| `lo_vs_pluto.py` | Is the edge on the LO axis or the IF axis? Parks the Pluto's `rx_lo` and moves only the CN0566 LO, which separates two variables that normally move together. |

The last one is the useful trick. `LO = SignalFreq + Rx_freq` with the source
fixed, so tuning normally moves both and no sweep can say which one the limit
is on — an earlier sweep recorded an "LO ceiling" that was really an IF
ceiling. Holding `rx_lo` still and moving the LO walks the tone across
baseband instead of retuning the receiver, which varies one axis at a time.

`phaser_characterize_headless.py` in the repo root is the productised
descendant of `cal_probe.py`'s IF sweep. Use that to actually set a kit up;
these are for when something does not add up.
