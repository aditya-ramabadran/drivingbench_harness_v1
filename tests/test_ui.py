"""Exercise browser state without an additional frontend dependency tree."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_shared_ui_behavior():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is required for UI behavior checks")
    for script in ("ui_behavior.cjs", "trace_viewer_behavior.cjs"):
        result = subprocess.run(
            [node, str(Path(__file__).with_name(script))],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
