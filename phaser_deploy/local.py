"""Local-side checks, and the Finding type the preflight reports in.

Checks are dependency-ordered, so "report every problem at once" is not
achievable -- you cannot ask the Pi which packages it has while TCP is refused.
What IS achievable is tiers: run every check in a tier, report all of them, and
stop at the end of the first tier that produced a blocker. That turns the
one-error-per-run cycle (fix key, rerun, learn about deps, rerun, learn about
sudo) into at most three rounds, each of which tells you everything it can.
"""

import enum
import os
import shutil
import socket
import sys
from dataclasses import dataclass

from . import advice as _advice

MIN_PYTHON = (3, 9)


class Level(enum.Enum):
    BLOCK = "BLOCK"
    WARN = "WARN"
    INFO = "INFO"


@dataclass(frozen=True)
class Finding:
    level: Level
    code: str
    message: str
    remedy: str = ""


def find_exe(name):
    """shutil.which with an explicit PATH.

    On Windows, which() searches the current directory first unless PATH is
    passed explicitly -- so a stray ssh.exe beside the repo would be preferred
    over the real one. Passing PATH also makes the tests hermetic.
    """
    return shutil.which(name, path=os.environ.get("PATH", ""))


def npm_argv(*args):
    """argv for npm, with the executable fully resolved.

    NOTE: on Windows npm is npm.cmd, and CreateProcess implicitly runs .cmd
    files through cmd.exe -- so despite passing a list, cmd.exe IS in this
    pipeline and its metacharacter/%VAR% rules apply. That is safe only because
    every argument here is a hardcoded literal. Never interpolate a path, a
    host, or anything user-supplied into an npm argv.
    """
    exe = find_exe("npm")
    if exe is None:
        return None
    return [exe, *args]


def check_local(root, *, want_build, need_ssh=True):
    """Tier 1: everything answerable without touching the network."""
    findings = []

    if sys.version_info < MIN_PYTHON:
        have = ".".join(str(n) for n in sys.version_info[:3])
        want = ".".join(str(n) for n in MIN_PYTHON)
        findings.append(Finding(
            Level.BLOCK, "old_python",
            f"Python {want}+ required, running {have}",
            _advice.advice("old_python", want=want, have=have)))

    if need_ssh and find_exe("ssh") is None:
        # Never checked before: on Windows the OpenSSH client is an optional
        # feature, and without it every command failed with a bare exit code.
        findings.append(Finding(
            Level.BLOCK, "no_ssh", "'ssh' not found on PATH",
            _advice.advice("no_ssh")))

    if want_build and npm_argv() is None:
        findings.append(Finding(
            Level.BLOCK, "no_npm", "'npm' not found on PATH",
            _advice.advice("no_npm")))

    return findings


def resolve_host(host):
    """(ok, detail) for DNS only.

    Split from the TCP check because the two have different causes and
    different fixes -- and because an unresolvable name can hang for 20+
    seconds on Windows, which should not be reported as "the Pi is off".
    """
    try:
        socket.getaddrinfo(host, 22, proto=socket.IPPROTO_TCP)
        return True, None
    except socket.gaierror as exc:
        return False, str(exc)


def tcp_open(host, port=22, timeout=10):
    """(ok, detail) for a plain TCP connect."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, None
    except OSError as exc:
        return False, str(exc)


def check_network(target, *, timeout=10):
    """Tier 2: is the Pi there, and can we authenticate without a password?"""
    findings = []
    ok, detail = resolve_host(target.host)
    if not ok:
        findings.append(Finding(
            Level.BLOCK, "dns", f"cannot resolve '{target.host}' ({detail})",
            _advice.advice("no_dns")))
        return findings

    ok, detail = tcp_open(target.host, timeout=timeout)
    if not ok:
        findings.append(Finding(
            Level.BLOCK, "tcp", f"cannot reach {target.host} on port 22 ({detail})",
            _advice.advice("unreachable", user=target.user, host=target.host)))
    return findings


def report(findings, *, stream=None):
    """Print every finding. True if none of them blocks."""
    stream = stream or sys.stdout
    order = {Level.BLOCK: 0, Level.WARN: 1, Level.INFO: 2}
    for f in sorted(findings, key=lambda f: order[f.level]):
        print(f"  {f.level.value}: {f.message}", file=stream)
        for line in (f.remedy.splitlines() if f.remedy else []):
            print(f"    {line}", file=stream)
    return not any(f.level is Level.BLOCK for f in findings)
