"""Shared fixtures. First conftest in this repo."""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


@pytest.fixture
def repo_root():
    return ROOT


@pytest.fixture
def recorder(monkeypatch):
    """Record every subprocess.run argv without executing anything.

    The seam is subprocess.run itself, so a test that says "this deploy opens
    at most N connections" cannot be fooled by a new call site added later.
    """
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(argv, *a, **kw):
        calls.append(list(argv) if not isinstance(argv, str) else argv)
        return Result()

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls
