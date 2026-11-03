"""MCP launch teardown must stay within its own runtime, including after failure."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests._helpers.live_server import terminate_process
from tests.e2e import test_run_mcp_url_env_expansion_e2e as scenario


@pytest.mark.parametrize(("turn_fails", "escalate"), [(False, False), (True, True)])
def test_mcp_cleanup_only_signals_its_own_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, turn_fails: bool, escalate: bool
) -> None:
    env = scenario._run_env(tmp_path / "home", "http://127.0.0.1:1", mcp_base_url=None)
    data_dir = str(tmp_path / "home" / ".omnigent")
    children: list[subprocess.Popen] = []
    term_targets: list[int] = []
    kill_targets: list[int] = []
    waits = 0

    def reject_cli_stop(*args: object, **kwargs: object) -> None:
        raise AssertionError("Cleanup must not invoke a machine-wide CLI stop")

    def intercepted_wait(
        processes: list[psutil.Process], *, timeout: float
    ) -> tuple[list[psutil.Process], list[psutil.Process]]:
        nonlocal waits
        waits += 1
        assert {process.pid for process in processes} <= {child.pid for child in children[:2]}
        return ([], processes) if escalate and waits == 1 else (processes, [])

    try:
        for runtime_dir in (data_dir, data_dir, data_dir + "-sibling"):
            children.append(
                subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(120)", "omnigent-fixture"],
                    env={**env, "OMNIGENT_DATA_DIR": runtime_dir},
                )
            )
        with monkeypatch.context() as patch:
            patch.setattr(scenario.subprocess, "run", reject_cli_stop)
            patch.setattr(
                psutil.Process, "terminate", lambda process: term_targets.append(process.pid)
            )
            patch.setattr(psutil.Process, "kill", lambda process: kill_targets.append(process.pid))
            patch.setattr(psutil, "wait_procs", intercepted_wait)
            if turn_fails:
                with pytest.raises(RuntimeError, match="synthetic turn failure"):
                    with scenario._local_omnigent(env, tmp_path):
                        raise RuntimeError("synthetic turn failure")
            else:
                with scenario._local_omnigent(env, tmp_path):
                    pass
        owned_pids = {child.pid for child in children[:2]}
        assert set(term_targets) == owned_pids
        assert set(kill_targets) == (owned_pids if escalate else set())
        assert all(child.poll() is None for child in children)
        assert env["OMNIGENT_DATA_DIR"] == data_dir
    finally:
        for child in children:
            terminate_process(child)
