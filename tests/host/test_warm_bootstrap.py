"""Warm Pod assignment, preparation, and managed-host handoff tests."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from omnigent.host import warm_bootstrap as bootstrap
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)

_TOKEN = "test-launch-token-do-not-log"
_POD_UID = "29e56ae8-8948-48b2-91dc-e3cd23b88873"


@pytest.fixture
def activation_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = tmp_path / "activation"
    monkeypatch.setenv(bootstrap.ACTIVATION_DIR_ENV_VAR, str(directory))
    monkeypatch.setenv(bootstrap.POD_UID_ENV_VAR, _POD_UID)
    return directory / "private"


def _payload(**overrides: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "pod_uid": _POD_UID,
        "host_id": "978ee4f06941429d955fdfc29712f3aa",
        "host_name": "warm-test-host",
        "token": _TOKEN,
        "server_url": "https://omnigent.example",
        "prepare_command": [sys.executable, "-c", "pass"],
        "generation": "assignment-1",
        **overrides,
    }


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("Timed out waiting for bootstrap state")


@pytest.fixture
def processes() -> Iterator[Callable[..., subprocess.Popen[str]]]:
    children: list[subprocess.Popen[str]] = []

    def start(mode: str, *, code: str | None = None) -> subprocess.Popen[str]:
        command = (
            [sys.executable, "-c", code]
            if code is not None
            else [sys.executable, "-m", bootstrap.__name__, mode]
        )
        process = subprocess.Popen(
            command,
            env=os.environ.copy(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        children.append(process)
        return process

    yield start
    for process in children:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate(timeout=5)
            pytest.fail("Bootstrap ignored termination")


def test_rejects_another_pod_before_writing(activation_dir: Path) -> None:
    with pytest.raises(bootstrap.BootstrapError, match="different Pod"):
        bootstrap.activate(_payload(pod_uid="another-pod"))
    assert not activation_dir.exists()


def test_missing_pod_uid_fails_closed(
    activation_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(bootstrap.POD_UID_ENV_VAR)
    with pytest.raises(bootstrap.BootstrapError, match="Pod UID is missing"):
        bootstrap.activate(_payload())
    assert not activation_dir.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"version": True},
        {"version": 2},
        {"prepare_command": "bash -lc true"},
        {"prepare_command": []},
        {"prepare_command": [None]},
        {"prepare_command": ["bash", "\0"]},
        {"token": ""},
        {"generation": "generation\nwith-newline"},
        {"extra_field": _TOKEN},
    ],
)
def test_invalid_payload_does_not_write_or_echo_credentials(
    activation_dir: Path, overrides: dict[str, Any]
) -> None:
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.activate(_payload(**overrides))
    assert _TOKEN not in str(error.value)
    assert not activation_dir.exists()


def test_private_assignment_and_credential_free_status(activation_dir: Path) -> None:
    assert bootstrap.status() == {"stage": "waiting", "generation": None}
    assert not bootstrap.ready()
    bootstrap.activate(_payload())

    assert activation_dir.stat().st_mode & 0o777 == 0o700
    for path in activation_dir.iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
    assert bootstrap.status() == {"stage": "bound", "generation": "assignment-1"}
    output = subprocess.run(
        [sys.executable, "-m", bootstrap.__name__, "status"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(output.stdout) == bootstrap.status()
    assert _TOKEN not in output.stdout + output.stderr


def test_same_assignment_retries_do_not_rewrite_private_file(activation_dir: Path) -> None:
    payload = _payload()
    bootstrap.activate(payload)
    before = (activation_dir / "activation.json").stat()
    bootstrap.activate(payload)
    after = (activation_dir / "activation.json").stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": "assignment-2"},
        {"token": "different-token"},
        {"host_id": "269d03160f7b4e22bb22eac4f09d6888"},
        {"prepare_command": ["bash", "-lc", "false"]},
    ],
)
def test_bound_pod_cannot_change_assignment(
    activation_dir: Path, overrides: dict[str, Any]
) -> None:
    bootstrap.activate(_payload())
    before = (activation_dir / "activation.json").read_bytes()
    with pytest.raises(bootstrap.BootstrapError, match="already bound"):
        bootstrap.activate(_payload(**overrides))
    assert (activation_dir / "activation.json").read_bytes() == before


def test_concurrent_allocators_cannot_overwrite_assignment(activation_dir: Path) -> None:
    payloads = [_payload(generation=f"assignment-{i}") for i in range(8)]

    def bind(payload: dict[str, Any]) -> bool:
        try:
            bootstrap.activate(payload)
        except bootstrap.BootstrapError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(bind, payloads))
    assert sum(accepted) == 1
    winner = payloads[accepted.index(True)]
    assert json.loads((activation_dir / "activation.json").read_text()) == winner
    with ThreadPoolExecutor(max_workers=8) as executor:
        assert all(executor.map(bind, [winner] * 8))


def test_uid_is_rechecked_when_reading_or_building_environment(
    activation_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = _payload()
    assignment = bootstrap.Activation.parse(payload)
    bootstrap.activate(payload)
    monkeypatch.setenv(bootstrap.POD_UID_ENV_VAR, "replacement-pod")
    with pytest.raises(bootstrap.BootstrapError, match="different Pod"):
        bootstrap.status()
    with pytest.raises(bootstrap.BootstrapError, match="different Pod"):
        assignment.environment()


def test_assigned_identity_overrides_inherited_environment(
    activation_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(HOST_ID_ENV_VAR, "stale-host")
    monkeypatch.setenv(HOST_NAME_ENV_VAR, "stale-name")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "stale-token")
    monkeypatch.setenv("IS_SANDBOX", "1")
    env = bootstrap.Activation.parse(_payload()).environment()
    assert env[HOST_ID_ENV_VAR] == _payload()["host_id"]
    assert env[HOST_NAME_ENV_VAR] == _payload()["host_name"]
    assert env[HOST_TOKEN_ENV_VAR] == _TOKEN
    assert env["IS_SANDBOX"] == "1"


def test_prepare_environment_output_redaction_and_restart(
    activation_dir: Path,
    tmp_path: Path,
    processes: Callable[..., subprocess.Popen[str]],
) -> None:
    marker = tmp_path / "prepared.json"
    script = tmp_path / "prepare.py"
    script.write_text(
        "import hashlib, json, os, pathlib, sys\n"
        f"marker = pathlib.Path({str(marker)!r})\n"
        "count = json.loads(marker.read_text())['count'] if marker.exists() else 0\n"
        f"token = os.environ[{HOST_TOKEN_ENV_VAR!r}]\n"
        "print(token)\n"
        "print(token, file=sys.stderr)\n"
        "marker.write_text(json.dumps({\n"
        "    'count': count + 1, 'digest': hashlib.sha256(token.encode()).hexdigest(),\n"
        f"    'host_id': os.environ[{HOST_ID_ENV_VAR!r}],\n"
        f"    'host_name': os.environ[{HOST_NAME_ENV_VAR!r}],\n"
        "    'argv': sys.argv,\n"
        "}))\n"
    )
    worker = processes("prepare")
    _wait_for(bootstrap.ready)
    assert bootstrap.status()["stage"] == "waiting"
    bootstrap.activate(_payload(prepare_command=[sys.executable, str(script)]))
    _wait_for(lambda: bootstrap.status()["stage"] == "prepared")
    result = json.loads(marker.read_text())
    assert result == {
        "count": 1,
        "digest": hashlib.sha256(_TOKEN.encode()).hexdigest(),
        "host_id": _payload()["host_id"],
        "host_name": _payload()["host_name"],
        "argv": [str(script)],
    }
    assert bootstrap.ready()
    previous_ready = (activation_dir / "ready.json").stat().st_mtime_ns
    worker.terminate()
    stdout, stderr = worker.communicate(timeout=5)
    assert worker.returncode == 128 + signal.SIGTERM
    assert stdout == stderr == ""

    processes("prepare")
    _wait_for(lambda: (activation_dir / "ready.json").stat().st_mtime_ns != previous_ready)
    assert bootstrap.status()["stage"] == "prepared"
    assert json.loads(marker.read_text())["count"] == 1


def test_preparation_failure_never_starts_host(
    activation_dir: Path, processes: Callable[..., subprocess.Popen[str]]
) -> None:
    worker = processes("prepare")
    host = processes("host")
    _wait_for(bootstrap.ready)
    bootstrap.activate(_payload(prepare_command=[sys.executable, "-c", "raise SystemExit(42)"]))
    _wait_for(lambda: bootstrap.status()["stage"] == "failed")
    stdout, stderr = host.communicate(timeout=5)
    assert host.returncode == 1
    assert stdout == ""
    assert stderr.strip() == "Sandbox workspace preparation failed."
    assert not bootstrap.ready()
    assert worker.poll() is None


def test_host_waits_then_execs_existing_host_command_with_assigned_environment(
    activation_dir: Path,
    tmp_path: Path,
    processes: Callable[..., subprocess.Popen[str]],
) -> None:
    marker = tmp_path / "host.json"
    executable = tmp_path / "host.py"
    executable.write_text(
        "import hashlib, json, os, pathlib, sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{\n"
        f"    'host_id': os.environ[{HOST_ID_ENV_VAR!r}],\n"
        f"    'host_name': os.environ[{HOST_NAME_ENV_VAR!r}],\n"
        f"    'digest': hashlib.sha256(os.environ[{HOST_TOKEN_ENV_VAR!r}].encode()).hexdigest(),\n"
        "    'server_url': sys.argv[1],\n"
        "}))\n"
    )
    host_code = (
        "import sys\n"
        "from omnigent.host import warm_bootstrap as bootstrap\n"
        "from omnigent.onboarding.sandboxes import kubernetes\n"
        "kubernetes._render_host_command = lambda url: "
        f"[sys.executable, {str(executable)!r}, url]\n"
        "sys.exit(bootstrap.main(['host']))\n"
    )
    payload = _payload()
    bootstrap.activate(payload)
    assignment = bootstrap.Activation.parse(payload)
    worker = processes("host", code=host_code)
    time.sleep(0.3)
    assert not marker.exists()
    assert worker.poll() is None
    bootstrap._set_stage(activation_dir, assignment, "prepared")
    stdout, stderr = worker.communicate(timeout=10)
    assert worker.returncode == 0
    assert stdout == stderr == ""
    assert json.loads(marker.read_text()) == {
        "host_id": payload["host_id"],
        "host_name": payload["host_name"],
        "digest": hashlib.sha256(_TOKEN.encode()).hexdigest(),
        "server_url": payload["server_url"],
    }

    restarted = processes("host", code=host_code)
    restarted.communicate(timeout=10)
    assert restarted.returncode == 0


def test_termination_forwards_to_preparation_and_allows_restart(
    activation_dir: Path,
    tmp_path: Path,
    processes: Callable[..., subprocess.Popen[str]],
) -> None:
    started = tmp_path / "started"
    stopped = tmp_path / "stopped"
    script = tmp_path / "prepare.py"
    script.write_text(
        "import pathlib, signal, time\n"
        "def stop(signum, frame):\n"
        f"    pathlib.Path({str(stopped)!r}).touch()\n"
        "    raise SystemExit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        f"marker = pathlib.Path({str(started)!r})\n"
        "if not marker.exists():\n"
        "    marker.touch()\n"
        "    while True:\n"
        "        time.sleep(0.1)\n"
    )
    bootstrap.activate(_payload(prepare_command=[sys.executable, str(script)]))
    worker = processes("prepare")
    _wait_for(started.exists)
    worker.terminate()
    worker.communicate(timeout=5)
    assert worker.returncode == 128 + signal.SIGTERM
    assert stopped.exists()
    assert bootstrap.status()["stage"] == "preparing"

    processes("prepare")
    _wait_for(lambda: bootstrap.status()["stage"] == "prepared")


@pytest.mark.parametrize("mode", ["prepare", "host"])
def test_idle_containers_terminate_promptly(
    activation_dir: Path,
    tmp_path: Path,
    mode: str,
    processes: Callable[..., subprocess.Popen[str]],
) -> None:
    if mode == "prepare":
        worker = processes(mode)
        _wait_for(bootstrap.ready)
    else:
        started = tmp_path / "host-started"
        code = (
            "from contextlib import contextmanager\n"
            "from pathlib import Path\n"
            "from omnigent.host import warm_bootstrap as bootstrap\n"
            "original_signals = bootstrap._signals\n"
            "@contextmanager\n"
            "def signals_with_ready_marker():\n"
            "    with original_signals() as signals:\n"
            f"        Path({str(started)!r}).touch()\n"
            "        yield signals\n"
            "bootstrap._signals = signals_with_ready_marker\n"
            "raise SystemExit(bootstrap.host())\n"
        )
        worker = processes(mode, code=code)
        _wait_for(started.exists)
    worker.terminate()
    worker.communicate(timeout=5)
    assert worker.returncode == 128 + signal.SIGTERM


def test_readiness_probe_import_avoids_heavy_dependencies() -> None:
    """A warm Pod runs the ``ready`` probe once a second in a fresh interpreter.

    Importing this module must stay lightweight: it must not pull in YAML or the
    ``omnigent.config`` machinery (which the full ``omnigent.host.identity``
    module does). Those recompile on every cold probe tick and dominate its CPU
    cost, so the probe reads the env-var names from the leaf
    ``omnigent.host.identity_env`` module instead.
    """
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, omnigent.host.warm_bootstrap; "
            "print(','.join(sorted(m for m in ('yaml', 'omnigent.config') "
            "if m in sys.modules)))",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "", (
        f"warm_bootstrap import unexpectedly pulled in heavy modules: {result.stdout.strip()}"
    )


def test_cli_rejects_bad_payload_without_echoing_input(activation_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", bootstrap.__name__, "activate"],
        input=f"{{bad-json:{_TOKEN}",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Invalid activation payload."
    assert not activation_dir.exists()


def test_activation_line_does_not_wait_for_websocket_stdin_close(activation_dir: Path) -> None:
    with subprocess.Popen(
        [sys.executable, "-m", bootstrap.__name__, "activate"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdin is not None
        process.stdin.write(json.dumps(_payload()) + "\n")
        process.stdin.flush()
        try:
            assert process.wait(timeout=5) == 0
        finally:
            if process.poll() is None:
                process.kill()
        assert bootstrap.status() == {"stage": "bound", "generation": "assignment-1"}


def test_activation_line_has_bounded_size(activation_dir: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", bootstrap.__name__, "activate"],
        input="x" * (bootstrap._MAX_ACTIVATION_BYTES + 1) + "\n",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert result.stderr.strip() == "Activation payload is too large."
    assert not activation_dir.exists()


def test_disabled_warm_pool_provider_import_does_not_require_fcntl() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import subprocess, sys\n"
            "sys.modules['fcntl'] = None\n"
            "from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool "
            "import AgentSandboxWarmPoolLauncher\n"
            "launcher = AgentSandboxWarmPoolLauncher(warm_pool=None)\n"
            "assert launcher._warm_pool is None\n",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
