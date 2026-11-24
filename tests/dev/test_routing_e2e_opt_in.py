"""Gateway opt-in must precede session fixture startup without blocking mock cases."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("opt_in", "include_gateway", "expected_tests", "expected_skips", "router_started"),
    [
        pytest.param(False, True, 2, 1, False, id="mixed-without-opt-in"),
        pytest.param(False, False, 1, 0, False, id="mock-only-without-opt-in"),
        pytest.param(True, True, 2, 0, True, id="mixed-with-opt-in"),
    ],
)
def test_routing_opt_in_precedes_gateway_session_fixture_startup(
    tmp_path: Path,
    opt_in: bool,
    include_gateway: bool,
    expected_tests: int,
    expected_skips: int,
    router_started: bool,
) -> None:
    source = Path(__file__).resolve().parents[2] / "tests/e2e/routing/conftest.py"
    tree = ast.parse(source.read_text())
    gate_env = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "_RUN_GATE_ENV"
            for target in node.targets
        )
    )
    fixtures = ast.Module(
        body=[
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name
            in {
                "_require_gateway_opt_in",
                "_require_opt_in",
                "mock_router",
                "_reset_mock_router",
            }
        ],
        type_ignores=[],
    )
    # Retain the real fixture scopes and dependencies, replacing only resource startup.
    (tmp_path / "conftest.py").write_text(
        "from __future__ import annotations\n"
        "import os\n"
        "from collections.abc import Iterator\n"
        "from pathlib import Path\n"
        "import pytest\n"
        f"_RUN_GATE_ENV = {gate_env!r}\n"
        "class MockRouter:\n"
        "    def reset(self): pass\n"
        "def serve_mock_router():\n"
        "    Path('router-started').touch()\n"
        "    yield MockRouter()\n" + ast.unparse(fixtures) + "\n@pytest.fixture(scope='session')\n"
        "def routing_client(mock_router): return mock_router\n"
    )
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = mock_only: isolated mock case\n")
    (tmp_path / "test_cases.py").write_text(
        "import pytest\n"
        + ("def test_gateway(routing_client): pass\n" if include_gateway else "")
        + "@pytest.mark.mock_only\n"
        "def test_mock_only(): pass\n"
    )
    probe_env = {
        **os.environ,
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        "PYTEST_ADDOPTS": "",
    }
    probe_env.pop(gate_env, None)
    if opt_in:
        probe_env[gate_env] = "1"
    report = tmp_path / "result.xml"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-c",
            str(tmp_path / "pytest.ini"),
            "--rootdir",
            str(tmp_path),
            "--confcutdir",
            str(tmp_path),
            "--junitxml",
            str(report),
        ],
        cwd=tmp_path,
        env=probe_env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    suite = ET.parse(report).getroot().find("testsuite")
    assert suite is not None
    assert int(suite.attrib["tests"]) == expected_tests
    assert int(suite.attrib["skipped"]) == expected_skips
    assert (tmp_path / "router-started").exists() is router_started
