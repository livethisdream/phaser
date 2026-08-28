"""Every name a deployed script imports from a sibling must actually exist.

This is the class of bug that killed the HB100 search: commit b66125a stripped
300 lines out of phaser_functions.py -- spec_est and the whole calibration
suite -- while leaving the `from phaser_functions import ...` lines in the
scripts that use them. Nothing failed at the time, because the Pi still carried
pyadi-iio's own fuller copy of that file. It only surfaced months later, once
deploys started overwriting the Pi's copy with ours, and then it presented as
"find HB100 freezes" rather than as an ImportError, because the traceback went
to a captured log the UI never surfaced.

Static, so it catches the break at the commit that causes it rather than on
someone's hardware.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Kept in step with install.sh's BACKEND_FILES.
DEPLOYED = [
    "phaser_headless.py", "phaser_cal_headless.py",
    "phaser_find_hb100_headless.py", "phaser_cw_radar.py",
    "ADAR_pyadi_functions.py", "SDR_functions.py", "phaser_functions.py",
    "config.py",
]

LOCAL_MODULES = {p.stem for p in ROOT.glob("*.py")}


def _module_level_names(module):
    """Top-level names a module exposes, without importing it.

    Static on purpose: importing would need numpy, pyadi-iio and real hardware.
    """
    tree = ast.parse((ROOT / f"{module}.py").read_text(encoding="utf-8"))
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            names.update((a.asname or a.name).split(".")[0] for a in node.names)
    return names


@pytest.mark.parametrize("filename", DEPLOYED)
def test_first_party_imports_resolve(filename):
    path = ROOT / filename
    if not path.exists():
        pytest.skip(f"{filename} not present")
    missing = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.ImportFrom) or node.level:
            continue
        if node.module not in LOCAL_MODULES:
            continue
        available = _module_level_names(node.module)
        for alias in node.names:
            if alias.name != "*" and alias.name not in available:
                missing.append(f"{filename}:{node.lineno} "
                               f"`from {node.module} import {alias.name}`")
    assert missing == [], (
        "deployed script imports a name its sibling does not define: "
        + "; ".join(missing))


def test_every_deployed_file_exists():
    """A file in the deploy list that does not exist ships a Pi a half-set."""
    absent = [f for f in DEPLOYED if not (ROOT / f).exists()]
    assert absent == [], f"deploy list names missing file(s): {absent}"
