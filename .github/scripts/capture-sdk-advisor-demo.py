"""Capture the existing SDK-environment advisor regression in hosted CI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

sys.path.insert(0, str(Path.cwd()))
from tests.e2e import test_claude_native_gateway_advisor_rejection_e2e as scenario

output = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
output.mkdir(parents=True, exist_ok=True)
observations: list[dict[str, object]] = []
run_claude = scenario._run_claude


def record_launch(
    argv: list[str], env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    result = run_claude(argv, env, cwd)
    entry = {
        "advisor_disabled": env.get("CLAUDE_CODE_DISABLE_ADVISOR_TOOL") == "1",
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }
    observations.append(entry)
    print(json.dumps(entry), flush=True)
    return result


scenario._run_claude = record_launch
junit = Path("artifacts/pytest-attempt-1.xml")
code = pytest.main(
    [
        "tests/e2e/test_claude_native_gateway_advisor_rejection_e2e.py",
        "--reruns",
        "0",
        "--force-reruns",
        "0",
        "-q",
        "-s",
        "--junitxml=" + str(junit),
    ]
)
cases = list(ET.fromstring(junit.read_bytes()).iter("testcase")) if junit.exists() else []
passed = bool(cases) and all(
    case.find(tag) is None for case in cases for tag in ("skipped", "failure", "error")
)
qualified = code == 0 and passed and len(observations) == 2
receipt = {"pytest_exit_code": int(code), "actual_test_pass": qualified, "launches": observations}
(output / "sdk-advisor-results.json").write_text(json.dumps(receipt, indent=2) + "\n")
raise SystemExit(0 if qualified else 1)
