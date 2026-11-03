import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def test_snapshot_update_verifies_only_failed_cases() -> None:
    workflow = Path(__file__).resolve().parents[2] / ".github/workflows/ui-snapshot-update.yml"
    steps = yaml.safe_load(workflow.read_text())["jobs"]["render"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Compare and verify the regenerated baselines"
    )
    shim = f'uv() {{ shift 2; {shlex.quote(sys.executable)} -m pytest "$@"; }}\n'
    for mode, exit_code, cases in (
        ("clean", 0, ["unchanged", "changed"]),
        ("regenerated", 0, ["unchanged", "changed", "changed"]),
        ("broken", 1, ["unchanged", "changed", "changed"]),
        ("collection", 2, []),
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "pytest.ini").write_text("[pytest]\nmarkers = visual\n")
            (root / "conftest.py").write_text(
                "def pytest_addoption(parser):\n"
                "    parser.addoption('--ui-skip-build', action='store_true')\n"
                "    parser.addoption('--ignore-size-diff', action='store_true')\n"
            )
            suite = root / "tests/e2e_ui/visual"
            suite.mkdir(parents=True)
            (suite / "test_cases.py").write_text(
                """import os
from pathlib import Path
import pytest

mode = os.environ['SNAPSHOT_TEST_MODE']
if mode == 'collection':
    raise RuntimeError('Broken story index')

@pytest.mark.visual
@pytest.mark.parametrize('case', ['unchanged', 'changed'])
def test_snapshot(case, request):
    with Path('executed').open('a') as log:
        log.write(case + '\\n')
    if case == 'changed' and mode != 'clean':
        baseline = Path('regenerated')
        if not baseline.exists() or mode == 'broken':
            baseline.touch()
            # Snapshot updates fail during fixture teardown.
            request.addfinalizer(lambda: pytest.fail('Snapshot changed'))
        else:
            assert not request.config.getoption('--ignore-size-diff')
"""
            )
            result = subprocess.run(
                ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", shim + script],
                cwd=root,
                env={
                    **os.environ,
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                    "PYTEST_ADDOPTS": "",
                    "SNAPSHOT_TEST_MODE": mode,
                },
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == exit_code, result.stdout + result.stderr
            log = root / "executed"
            assert (log.read_text().splitlines() if log.exists() else []) == cases, mode


if __name__ == "__main__":
    test_snapshot_update_verifies_only_failed_cases()
