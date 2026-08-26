# Archive

Superseded design notes, status write-ups, and troubleshooting logs from
earlier phases of the project — mostly the PyWebView/Electron desktop-app
era and the network/hardware debugging that preceded the current
Pi-hosted browser architecture.

They were previously untracked and listed by name in `.gitignore`, which
meant they existed only on one machine and would have been lost on the
next clone. They are kept here for provenance, not as documentation.

**Treat nothing in this directory as current.** For how the system works
today, see the top-level [README](../README.md). Specific things known to
be out of date:

- The desktop/Electron/Tauri migration notes describe an architecture that
  was abandoned in favour of the browser + WebSocket design.
- Network and hardware-URI troubleshooting notes predate the systemd unit
  installed by `scripts/setup-pi.sh`.
- Several "SOLUTION"/"COMPLETE"/"SUMMARY" files document one-off fixes
  that have since been folded into the code or superseded outright.
