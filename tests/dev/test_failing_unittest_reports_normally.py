"""A failing unittest.TestCase must produce a normal pytest failure report."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROBE_SOURCE = textwrap.dedent(
    """
    import unittest


    class TestProbe(unittest.TestCase):
        def test_deliberate_fail(self):
            self.assertIn("x", ["a", "b"])
    """
)


def test_failing_unittest_testcase_reports_normally(tmp_path: Path) -> None:
    probe = tmp_path / "test_failprobe.py"
    probe.write_text(_PROBE_SOURCE)

    # Avoid inheriting the outer pytest process's configuration.
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            str(_REPO_ROOT / "pyproject.toml"),
            probe.name,
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr

    assert "INTERNALERROR" not in output, (
        "pytest aborted with an internal error instead of reporting the "
        f"unittest failure:\n{output}"
    )
    assert result.returncode == 1, (
        "expected exit code 1 (test failed, reported normally), got "
        f"{result.returncode}:\n{output}"
    )
    assert "1 failed" in output, f"normal failure summary missing:\n{output}"
    assert "test_deliberate_fail" in output, (
        f"failing test's report missing from output:\n{output}"
    )
