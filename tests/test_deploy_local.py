"""Preflight tiers, executable resolution, and the no-shell invariant."""

import ast
import os
from pathlib import Path

import pytest

from phaser_deploy.local import (
    Level, check_local, check_network, find_exe, npm_argv, report,
)
from phaser_deploy.remote import Target

ROOT = Path(__file__).resolve().parent.parent


def _python_sources():
    yield ROOT / "deploy.py"
    yield from (ROOT / "phaser_deploy").glob("*.py")


def test_no_shell_true_anywhere():
    """shell=True is /bin/sh on POSIX and cmd.exe on Windows.

    Every deployment bug this tool has had -- unexpanded globs, %VAR%
    expansion, && re-parsing, quoting differences -- came from one of those two
    interpreting a string we built. Asserting on the AST closes the class
    permanently, including for code nobody has written yet.
    """
    offenders = []
    for path in _python_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "shell":
                        offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], f"shell= passed to a subprocess call at: {offenders}"


def test_missing_ssh_is_a_blocking_finding(monkeypatch):
    """Never checked before: on Windows the OpenSSH client is optional, and
    without it every command died with a bare 'exit code 1'."""
    import phaser_deploy.local as loc
    monkeypatch.setattr(loc, "find_exe", lambda name: None if name == "ssh" else "/x")
    findings = check_local(ROOT, want_build=False, need_ssh=True)
    assert any(f.code == "no_ssh" and f.level is Level.BLOCK for f in findings)


def test_ssh_not_required_when_not_deploying(monkeypatch):
    import phaser_deploy.local as loc
    monkeypatch.setattr(loc, "find_exe", lambda name: None)
    findings = check_local(ROOT, want_build=False, need_ssh=False)
    assert not any(f.code == "no_ssh" for f in findings)


def test_npm_argv_uses_the_resolved_executable(monkeypatch):
    """On Windows npm is npm.cmd; passing the bare name relies on PATHEXT."""
    import phaser_deploy.local as loc
    monkeypatch.setattr(loc, "find_exe",
                        lambda name: r"C:\Program Files\nodejs\npm.cmd")
    assert npm_argv("run", "build")[0] == r"C:\Program Files\nodejs\npm.cmd"


def test_npm_argv_is_none_when_absent(monkeypatch):
    import phaser_deploy.local as loc
    monkeypatch.setattr(loc, "find_exe", lambda name: None)
    assert npm_argv("run", "build") is None


def test_find_exe_passes_explicit_path(monkeypatch):
    """shutil.which searches the CWD first on Windows unless PATH is explicit,
    so a stray ssh.exe beside the repo would otherwise win."""
    seen = {}

    def fake_which(name, path=None):
        seen["path"] = path
        return "/usr/bin/ssh"

    monkeypatch.setattr("phaser_deploy.local.shutil.which", fake_which)
    monkeypatch.setenv("PATH", "/custom/bin")
    find_exe("ssh")
    assert seen["path"] == "/custom/bin"


def test_dns_and_tcp_are_distinguished():
    """Conflating them told users 'the Pi is off' when the real problem was
    that .local did not resolve -- two different fixes."""
    findings = check_network(Target("analog", "definitely-not-a-host.invalid"))
    assert [f.code for f in findings] == ["dns"]


def test_report_returns_false_only_on_block(capsys):
    from phaser_deploy.local import Finding
    assert report([Finding(Level.WARN, "w", "a warning")]) is True
    assert report([Finding(Level.BLOCK, "b", "a blocker")]) is False
