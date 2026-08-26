"""The deploy tool must stay runnable with a bare Python and nothing installed."""

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
FIRST_PARTY = {"phaser_deploy"}


def _top_level_imports(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:          # relative import, first-party by definition
                continue
            if node.module:
                names.add(node.module.split(".")[0])
    return names


def _sources():
    yield ROOT / "deploy.py"
    yield from sorted((ROOT / "phaser_deploy").glob("*.py"))


@pytest.mark.skipif(sys.version_info < (3, 10),
                    reason="sys.stdlib_module_names needs 3.10+")
@pytest.mark.parametrize("path", list(_sources()), ids=lambda p: p.name)
def test_deploy_tool_imports_only_stdlib(path):
    """No third-party dependency, ever.

    This is what lets a tester run `python deploy.py <host>` with nothing
    installed -- no venv, no uv sync, no pip. It matters most on Windows, where
    the project's real dependency set (pyadi-iio, scipy, matplotlib) is heavy at
    best and unavailable at worst, and where the alternative is telling a
    colleague to install a toolchain before they can deploy anything.

    Adding a convenient third-party import here would quietly cost that.
    """
    offenders = sorted(
        name for name in _top_level_imports(path)
        if name not in sys.stdlib_module_names and name not in FIRST_PARTY
    )
    assert offenders == [], (
        f"{path.name} imports non-stdlib module(s): {offenders}. "
        "The deploy tool must run on a bare interpreter."
    )
