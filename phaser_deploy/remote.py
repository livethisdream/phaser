"""Everything that talks to the Pi.

The load-bearing idea is ssh_argv(): it is the ONLY place a remote command is
turned into a string, and it is a pure function. Two consequences.

First, it closes the two-shells hole. Passing argv lists to subprocess kills the
LOCAL shell (cmd.exe on Windows, /bin/sh elsewhere), but ssh still concatenates
its trailing arguments and hands them to /bin/sh on the Pi -- so a remote command
built by f-string interpolation is still a shell injection waiting for a path
with a space in it. Remote commands are therefore argv lists too, joined exactly
once, here, by shlex.join.

Second, it is the test seam. Because it is pure, asserting on the exact argv
needs no network, no Pi, and no mocks -- just call it and compare lists.
"""

import os
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

# The ssh/scp programs as argv prefixes. Module-level so tests can replace them
# with a fake, e.g. [sys.executable, "tests/fake_ssh.py"].
SSH_BASE = ["ssh"]
SCP_BASE = ["scp"]


@dataclass(frozen=True)
class Target:
    """Who and where. Frozen so it can't drift mid-run."""
    user: str
    host: str

    def __str__(self):
        return f"{self.user}@{self.host}"


def ssh_argv(target, remote_argv=(), *, tty=False, no_pty=False, batch=False,
             mux=None, timeout=10, accept_new=False):
    """Build a complete ssh argv. Pure -- no I/O, no globals beyond SSH_BASE.

    tty:        allocate a pty (-t). Only ever valid with a real terminal.
    no_pty:     forbid a pty (-T). MANDATORY when stdin carries binary data --
                a pty performs CR/LF translation and silently corrupts a stream.
    batch:      -o BatchMode=yes. Refuses password auth, so use it only for
                probes whose failure is an answer rather than a problem.
    """
    argv = list(SSH_BASE)
    if tty:
        argv.append("-t")
    if no_pty:
        argv.append("-T")
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if accept_new:
        argv += ["-o", "StrictHostKeyChecking=accept-new"]
    if mux:
        argv += ["-o", f"ControlPath={mux}"]
    argv += ["-o", f"ConnectTimeout={timeout}"]
    argv.append(str(target))
    if remote_argv:
        # The single point at which a remote command becomes a string.
        argv.append(shlex.join([str(a) for a in remote_argv]))
    return argv


def scp_argv(sources, dest, *, recursive=False, mux=None):
    """Build an scp argv. Local paths stay as separate argv elements."""
    argv = list(SCP_BASE)
    if recursive:
        argv.append("-r")
    if mux:
        argv += ["-o", f"ControlPath={mux}"]
    argv += [str(s) for s in sources]
    argv.append(dest)
    return argv


def find_ssh():
    """Absolute path to ssh, or None.

    shutil.which is given an explicit PATH: on Windows it otherwise searches the
    current directory first, so a stray ssh.exe in the repo would win.
    """
    return shutil.which("ssh", path=os.environ.get("PATH", ""))


class SshSession:
    """Owns the ControlMaster socket for one deploy.

    A context manager rather than the atexit handler this replaces, because
    atexit does not run on Ctrl-Break on Windows or on os._exit -- which left a
    background master ssh and a stale socket behind. try/finally always runs.

    Multiplexing is unavailable on Windows (its OpenSSH has never supported
    ControlMaster), so there the session is inert and every call reconnects.
    """

    def __init__(self, target, enabled=True):
        self.target = target
        self.enabled = enabled and os.name != "nt"
        self.path = None
        self._dir = None

    def __enter__(self):
        if not self.enabled:
            return self
        self._dir = tempfile.mkdtemp(prefix="phaser-mux-")
        path = os.path.join(self._dir, "cm")
        result = subprocess.run(
            list(SSH_BASE) + [
                "-o", "ControlMaster=yes", "-o", f"ControlPath={path}",
                "-o", "ControlPersist=300", "-N", "-f", str(self.target),
            ]
        )
        if result.returncode == 0:
            self.path = path
        else:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        return self

    def __exit__(self, *exc):
        if self.path:
            subprocess.run(
                list(SSH_BASE) + ["-O", "exit", "-o", f"ControlPath={self.path}",
                                  str(self.target)],
                capture_output=True,
            )
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
        self.path = None
        self._dir = None
        return False
