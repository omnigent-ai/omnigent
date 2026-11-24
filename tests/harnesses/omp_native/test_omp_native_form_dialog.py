"""Exercise native HTML form conversion against the installed OMP public APIs."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.harnesses.omp_native.credentials import resolve_omp_package_root


def test_omp_form_dialog_with_native_parser() -> None:
    omp = os.environ.get("OMNIGENT_OMP_PATH") or shutil.which("omp")
    bun = shutil.which("bun")
    if not omp or not bun:
        pytest.skip("OMP and Bun are required for native form parser coverage")
    root = resolve_omp_package_root(omp)
    assert root is not None, "the installed OMP must expose its native package metadata"
    script = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/omp_native/omnigent_omp_form_dialog.test.mjs"
    )
    result = subprocess.run(
        [bun, "test", str(script)],
        env={**os.environ, "OMNIGENT_TEST_OMP_PACKAGE_ROOT": str(root)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
