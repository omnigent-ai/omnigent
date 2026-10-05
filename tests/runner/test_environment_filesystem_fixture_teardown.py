"""The runner filesystem test fixtures stop their os_env helper subprocesses at teardown."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

# Reuses the real fixtures so the test exercises the fixtures under test. Each
# test proves its operation started a helper (so the teardown check cannot pass
# vacuously); the last one fails during setup to cover that cleanup path too.
_TOUCH_FILESYSTEM = """
from omnigent.entities import DEFAULT_ENVIRONMENT_ID
import pytest
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from tests.runner._os_env_fixture_teardown_probe import live_helper_pids
from tests.runner.test_environment_filesystem import (  # noqa: F401
    app,
    client,
    glob_client,
    glob_workspace,
    make_os_env,
    registry,
    workspace,
)

_FILESYSTEM = f"/v1/sessions/conv_test/resources/environments/{DEFAULT_ENVIRONMENT_ID}/filesystem"


async def test_touch_registry_filesystem(client):
    before = live_helper_pids()
    assert (await client.get(_FILESYSTEM)).status_code == 200
    assert live_helper_pids() - before, "the request should have started a helper"


async def test_touch_glob_filesystem(glob_client):
    before = live_helper_pids()
    assert (await glob_client.get(_FILESYSTEM)).status_code == 200
    assert live_helper_pids() - before, "the request should have started a helper"


async def test_touch_factory_env(make_os_env, tmp_path):
    os_env = make_os_env(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    before = live_helper_pids()
    assert (await os_env.shell("true"))["exit_code"] == 0
    assert live_helper_pids() - before, "the shell command should have started a helper"


@pytest.fixture
async def env_then_setup_failure(make_os_env, tmp_path):
    os_env = make_os_env(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert (await os_env.shell("true"))["exit_code"] == 0
    raise RuntimeError("injected setup failure")


async def test_setup_failure_after_helper_start(env_then_setup_failure):
    raise AssertionError("setup should have failed before the test body")
"""


def test_fixture_teardown_stops_the_helper(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytester.makepyfile(test_touch_filesystem=_TOUCH_FILESYSTEM)

    probe_log = tmp_path / "probe.jsonl"
    repo_root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (repo_root, os.environ.get("PYTHONPATH", "")) if p),
    )
    # Autoload would pull in every installed plugin (pytest-playwright,
    # structlog); the inner run needs only asyncio and the probe.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("OMNIGENT_FIXTURE_PROBE_LOG", str(probe_log))

    result = pytester.runpytest_subprocess(
        "-p",
        "asyncio",
        "-p",
        "tests.runner._os_env_fixture_teardown_probe",
        "-o",
        "asyncio_mode=auto",
    )
    result.assert_outcomes(passed=3, errors=1)
    result.stdout.fnmatch_lines(["*injected setup failure*"])

    assert probe_log.exists(), "probe wrote no records; was OMNIGENT_FIXTURE_PROBE_LOG propagated?"
    records = [
        json.loads(line)
        for line in probe_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    covered = {name for r in records for name in r["fixtures"]}
    assert covered == {"registry", "glob_client", "make_os_env"}, records
    leaked = [
        (r["test"], r["fixtures"], r["helpers_alive_after_teardown"])
        for r in records
        if r["helpers_alive_after_teardown"]
    ]
    assert not leaked, f"os_env helper processes outlived their fixtures: {leaked}"
