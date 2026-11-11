"""Actor activation is bounded, generation-bound, and forgotten on cold boot."""

from __future__ import annotations

import hashlib
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent.host import substrate_bootstrap as bootstrap
from omnigent.host.identity_env import HOST_TOKEN_ENV_VAR
from omnigent.host.substrate_bootstrap import ActorSignals

_UID = "4a93f8f1-ddf3-4f2c-9c31-566dc240da4f"
_TOKEN = "activation-token-never-in-output"


@pytest.fixture(autouse=True)
def credential_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    directory = (tmp_path / "credentials").resolve()
    directory.mkdir(mode=0o700)
    monkeypatch.setattr(bootstrap, "TOKEN_DIR", directory)
    return directory


def test_credential_directory_must_be_private(credential_directory: Path) -> None:
    assert bootstrap._token_directory() == credential_directory
    credential_directory.chmod(0o755)
    with pytest.raises(bootstrap.ActivationError, match="private"):
        bootstrap._token_directory()


def payload(**changes: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "actor_uid": _UID,
        "generation": hashlib.sha256(_TOKEN.encode()).hexdigest(),
        "host_id": "host-test",
        "host_name": "test",
        "token": _TOKEN,
        "server_url": "https://omnigent.example.com",
        "repos": [],
        "host_config": None,
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"version": True},
        {"extra": "value"},
        {"actor_uid": "another-actor"},
        {"generation": "wrong"},
        {"server_url": "http://omnigent.example.com"},
        {"host_config": []},
        {"token": ""},
        {
            "repos": [
                {
                    "url": "https://example.com/repo",
                    "branch": None,
                    "repo_name": "../escape",
                    "git_clone": {},
                }
            ]
        },
    ],
)
def test_invalid_activation_does_not_expose_token(changes: dict[str, Any]) -> None:
    with pytest.raises(bootstrap.ActivationError) as error:
        bootstrap.Activation.parse(payload(**changes), _UID)
    assert _TOKEN not in str(error.value)


@pytest.mark.parametrize("name,valid", [(".github", True), (".", False), ("..", False)])
def test_repository_directory_is_a_safe_component(name: str, valid: bool) -> None:
    value = payload(
        repos=[
            {
                "url": "https://git.example.com/example/.github",
                "branch": None,
                "repo_name": name,
                "git_clone": {},
            }
        ]
    )
    if not valid:
        with pytest.raises(bootstrap.ActivationError):
            bootstrap.Activation.parse(value, _UID)
    else:
        assert bootstrap.Activation.parse(value, _UID).repos[0].repo_name == name


def test_duplicate_assignment_and_cold_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    state = bootstrap.Bootstrap(_UID, ActorSignals())
    launched = threading.Event()
    launch = Mock(side_effect=lambda _: launched.set())
    monkeypatch.setattr(state, "_launch", launch)
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(state.activate, [payload()] * 8))
    assert launched.wait(5)
    assert launch.call_count == 1
    assert state.activation
    assert state.activation.environment()[HOST_TOKEN_ENV_VAR] == _TOKEN
    assert _TOKEN not in repr(state.activation)
    assert _TOKEN not in repr(state.status())
    with pytest.raises(bootstrap.ActivationError, match="already bound"):
        state.activate(payload(host_id="other-host"))
    cold_boot = bootstrap.Bootstrap(_UID, ActorSignals())
    assert cold_boot.status() == {
        "stage": "waiting",
        "generation": None,
        "draining": False,
        "retryable": False,
    }


@pytest.mark.parametrize("prepare_exit", [0, 1])
def test_preparation_gates_host_launch(monkeypatch: pytest.MonkeyPatch, prepare_exit: int) -> None:
    state = bootstrap.Bootstrap(_UID, ActorSignals())
    activation = bootstrap.Activation.parse(payload(), _UID)
    process = Mock()
    process.wait.return_value = prepare_exit
    start = Mock(return_value=process)
    monkeypatch.setattr(bootstrap.subprocess, "Popen", start)
    state._launch(activation)
    assert start.call_count == (2 if prepare_exit == 0 else 1)
    for call in start.call_args_list:
        assert _TOKEN not in repr(call.args)
        assert call.kwargs["env"][HOST_TOKEN_ENV_VAR] == _TOKEN
        assert call.kwargs["start_new_session"] is True
    assert state.stage == "failed"  # Preparation or the final host process exited.


def test_idle_host_reactivation_requires_a_new_generation(monkeypatch):
    state = bootstrap.Bootstrap(_UID, ActorSignals())
    launch = Mock(return_value=True)
    monkeypatch.setattr(state, "_run_host", launch)
    state.activate(payload())
    state.worker.join(timeout=5)
    assert state.stage == "idle" and state.cleaned
    state.activate(payload())
    assert launch.call_count == 1
    token = "woken-host-token"
    state.activate(payload(token=token, generation=hashlib.sha256(token.encode()).hexdigest()))
    state.worker.join(timeout=5)
    assert launch.call_count == 2


def test_failed_preparation_can_retry_same_host_without_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = bootstrap.Bootstrap(_UID, ActorSignals())
    monkeypatch.setattr(state, "_run_host", Mock(side_effect=bootstrap.ActivationError("failed")))
    state.activate(payload())
    assert state.worker is not None
    state.worker.join(timeout=5)
    assert state.stage == "failed"
    with pytest.raises(bootstrap.ActivationError):
        state.activate(payload(host_id="another-host"))
    token = "fresh-token"
    state.activate(payload(token=token, generation=hashlib.sha256(token.encode()).hexdigest()))
    assert state.worker is not None
    state.worker.join(timeout=5)
    assert state.activation is not None and state.activation.token == token


@pytest.mark.skipif(
    sys.platform != "linux", reason="Actor process ownership uses Linux subreapers"
)
def test_failed_attempt_reaps_adopted_descendants() -> None:
    code = """
import subprocess, sys
import psutil
from omnigent.host.connect import _install_child_subreaper
from omnigent.host.substrate_bootstrap import _cleanup_children
assert _install_child_subreaper()
child = subprocess.Popen([sys.executable, '-c',
    "import subprocess; subprocess.Popen(['sleep', '600'], start_new_session=True)"],
    start_new_session=True)
assert child.wait(timeout=5) == 0
owner = psutil.Process()
assert owner.children(recursive=True)
assert _cleanup_children(owner)
assert not owner.children(recursive=True)
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)


def test_worker_eviction_keeps_host_alive_for_snapshot() -> None:
    signals = ActorSignals()
    signals.child = Mock()
    state = bootstrap.Bootstrap(_UID, signals)
    signals.forward(signal.SIGTERM, None)
    assert signals.signum is None
    assert state.status()["draining"] is True
    signals.child.assert_not_called()
    with pytest.raises(bootstrap.ActivationError, match="draining"):
        state.activate(payload())


def test_http_protocol_does_not_expose_exec_or_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    state = bootstrap.Bootstrap(_UID, ActorSignals())
    monkeypatch.setattr(state, "_launch", lambda _: None)
    with HTTPServer(("127.0.0.1", 0), bootstrap.handler(state)) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            with httpx.Client(
                base_url=f"http://127.0.0.1:{server.server_port}", trust_env=False
            ) as client:
                assert client.get("/readyz").status_code == 200
                assert client.get("/status").json()["stage"] == "waiting"
                assert client.post("/exec", json={"command": "id"}).status_code == 404
                assert (
                    client.post(
                        "/activate", content=b"x" * (bootstrap.MAX_ACTIVATION_BYTES + 1)
                    ).status_code
                    == 409
                )
                result = client.post("/activate", json=payload())
                assert result.status_code == 200
                assert _TOKEN not in result.text
                assert client.post("/activate", json=payload()).status_code == 200
                assert (
                    client.post("/activate", json=payload(host_id="another-host")).status_code
                    == 409
                )
        finally:
            server.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()
