"""A failing unittest.TestCase test must yield a normal pytest failure report.

Drives the contributor journey end-to-end: write a deliberately failing
``unittest.TestCase`` test, run the dev environment's own ``pytest`` on it as
a subprocess under the repo's pytest configuration (as a contributor's shell
would), and assert the run ends with an ordinary ``1 failed`` report instead
of aborting at report time with ``INTERNALERROR AttributeError: 'tuple'
object has no attribute 'value'``, which destroys the failure output the
contributor needs. The crash comes from structlog-config's auto-loaded pytest
plugin storing its own tuples on ``item._excinfo``, clobbering the state
pytest's unittest integration keeps there; the repo's pytest config must keep
that plugin blocked (or its collision otherwise neutralized).
"""

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

    # Same interpreter and installed plugin set as the dev environment, run
    # under the repo's pytest configuration; strip the outer pytest's own
    # state so the child run is hermetic.
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
