"""Focused deterministic tests of OMP's shared native extension and adapter."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_omp_native_extension_javascript() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for native OMP extension coverage")
    script = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/omp_native/omnigent_omp_native_extension.test.cjs"
    )
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
