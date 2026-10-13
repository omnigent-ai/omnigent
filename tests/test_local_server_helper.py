"""Shared startup failures retain diagnostics and reap the real child process."""

import os
import subprocess
from pathlib import Path

import pytest

from tests._helpers.live_server import isolated_local_server


def test_startup_exit_reports_log_and_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr("tests._helpers.live_server.subprocess.Popen", record_child)
    with pytest.raises(AssertionError) as error:
        with isolated_local_server(
            tmp_path,
            bootstrap="print('startup-marker', flush=True); raise SystemExit(23)",
            health_timeout=30,
            poll_interval=0.02,
        ):
            pytest.fail("a failed child must not be yielded as a healthy server")
    assert "Server exited with code 23" in str(error.value)
    assert "startup-marker" in str(error.value)
    assert len(children) == 1 and children[0].returncode == 23


@pytest.mark.parametrize("inherit_env", [True, False])
@pytest.mark.parametrize("failing_process", ["server", "runner"])
def test_stack_startup_failure_closes_started_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failing_process: str, inherit_env: bool
):
    from tests._helpers.server_runner import server_runner

    children = []
    real_popen = subprocess.Popen

    def record_child(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        children.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", record_child)
    monkeypatch.setenv("OMNIGENT_RUNNER_ID", "parent-must-not-leak")
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD", "9999")
    monkeypatch.setenv("OMNIGENT_PROCESS_LOG_FILE", str(tmp_path / "parent.log"))
    monkeypatch.setenv("REMOVE_TEST_SETTING", "parent")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-test-marker")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "ambient-imports"))
    fail = f"""
import os
assert os.environ.get("OMNIGENT_RUNNER_ID") != "parent-must-not-leak"
assert "OMNIGENT_RUNNER_ZYGOTE_HARNESS_FD" not in os.environ
assert "OMNIGENT_PROCESS_LOG_FILE" not in os.environ
assert (os.environ.get("OPENAI_API_KEY") == "ambient-test-marker") is {inherit_env}
assert ("ambient-imports" in os.environ["PYTHONPATH"]) is {inherit_env}
assert os.environ["EXPLICIT_TEST_SETTING"] == "kept"
assert "REMOVE_TEST_SETTING" not in os.environ
print("stack-startup-marker", flush=True)
raise SystemExit(23)
"""
    with pytest.raises(AssertionError) as error:
        with server_runner(
            tmp_path,
            server_bootstrap=fail if failing_process == "server" else None,
            base_env=None if inherit_env else {"PATH": os.environ["PATH"]},
            server_env={"EXPLICIT_TEST_SETTING": "kept", "REMOVE_TEST_SETTING": None},
            poll_interval=0.02,
        ) as stack:
            for key in ("HOME", "OMNIGENT_DATA_DIR"):
                with pytest.raises(ValueError, match="owned by the isolated stack"):
                    stack.start_runner(env={key: str(tmp_path / "outside")})
            stack.start_runner(
                bootstrap=fail, env={"EXPLICIT_TEST_SETTING": "kept", "REMOVE_TEST_SETTING": None}
            )
            pytest.fail("a failed child must not be treated as a ready runner")
    assert "exited with code 23" in str(error.value)
    assert "stack-startup-marker" in str(error.value)
    assert len(children) == (1 if failing_process == "server" else 2)
    assert all(child.poll() is not None for child in children)


@pytest.mark.parametrize("exit_early", [True, False])
def test_host_failed_readiness_reaps_child_and_retains_isolated_diagnostics(
    tmp_path: Path, exit_early: bool
):
    import sys

    import httpx

    from tests._helpers.host import isolated_host

    for key in ("HOME", "OMNIGENT_HOST_ID"):
        with pytest.raises(ValueError):
            with isolated_host(tmp_path, "http://127.0.0.1:1", env={key: "invalid"}):
                pytest.fail("owned environment must be rejected before starting a host")
        assert not (tmp_path / "home").exists()

    handles = []

    def ready(host):
        handles.append(host)
        with httpx.Client(base_url="http://127.0.0.1:1", trust_env=False) as client:
            host.wait_online(client, timeout=2, poll_interval=0.02)

    bootstrap = """
import os, pathlib, time
assert 'REMOVE_TEST_SETTING' not in os.environ
assert os.environ['EXPLICIT_TEST_SETTING'] == 'kept'
assert pathlib.Path(os.environ['HOME']).is_relative_to(pathlib.Path.cwd())
assert pathlib.Path(os.environ['OMNIGENT_CONFIG_HOME'], 'config.yaml').exists()
print('isolated-host-startup-marker', flush=True)
with open(os.environ['OMNIGENT_PROCESS_LOG_FILE'], 'a') as log:
    print('secondary-writer-marker', file=log)
print('stdout-after-logger-marker', flush=True)
""" + ("raise SystemExit(23)" if exit_early else "time.sleep(60)")
    with pytest.raises(AssertionError, match="isolated-host-startup-marker"):
        with isolated_host(
            tmp_path,
            "http://127.0.0.1:1",
            base_env={"PATH": os.environ["PATH"], "REMOVE_TEST_SETTING": "parent"},
            env={"REMOVE_TEST_SETTING": None, "EXPLICIT_TEST_SETTING": "kept"},
            command=[sys.executable, "-c", bootstrap],
            cwd=tmp_path,
            ready=ready,
        ):
            pytest.fail("an unready host must not be yielded")
    assert len(handles) == 1
    log = handles[0].log_path.read_text()
    assert "secondary-writer-marker" in log and "stdout-after-logger-marker" in log
    assert handles[0].proc.poll() is not None
    if exit_early:
        assert handles[0].proc.returncode == 23
