"""Runs the widget's plain-node self-check (client.js page fitting) under pytest."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_clamp_check_passes() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    script = Path(__file__).with_name("clamp_check.mjs")
    result = subprocess.run(
        [node, str(script)], capture_output=True, text=True, cwd=script.parent, timeout=60
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
