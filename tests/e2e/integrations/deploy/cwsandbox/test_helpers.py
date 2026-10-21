"""Offline regression tests for the CoreWeave Sandbox guide's helpers."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

pytest.importorskip("cwsandbox")

ROOT = Path(__file__).resolve().parents[5]


def _load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def managed(monkeypatch):
    module = _load(Path(__file__).with_name("e2e_managed.py"))
    monkeypatch.delenv("OMNIGENT_API_TOKEN", raising=False)
    yield module
    module.CLIENT.close()


def test_login_uses_stdin_and_does_not_echo_credentials(monkeypatch, capsys):
    module = _load(ROOT / "deploy/cwsandbox/login.py")
    monkeypatch.delenv("OMNIGENT_CWSANDBOX_AUTH_STRATEGY", raising=False)
    process = Mock()
    process.result.return_value = SimpleNamespace(returncode=0, stdout="private output")
    sandbox = Mock()
    sandbox.exec.return_value = process
    sdk = Mock()
    sdk.from_id.return_value.result.return_value = sandbox
    monkeypatch.setattr(module, "Sandbox", sdk)

    module.login("sandbox-id", "https://server.example", "alice", "test-password")

    from cwsandbox import AuthStrategy

    sdk.from_id.assert_called_once_with("sandbox-id", auth=AuthStrategy.WANDB)
    command = sandbox.exec.call_args.args[0]
    assert command == ["bash", "-lc", "omnigent login https://server.example >/dev/null 2>&1"]
    assert sandbox.exec.call_args.kwargs["stdin"] is True
    process.stdin.write.assert_called_once_with(b"alice\ntest-password\n")
    process.stdin.close.assert_called_once_with()
    output = capsys.readouterr().out
    assert "Sandbox login saved" in output
    assert "test-password" not in output
    assert "private output" not in output

    process.result.return_value = SimpleNamespace(returncode=1, stderr="private failure")
    with pytest.raises(SystemExit, match="Sandbox login failed") as exc:
        module.login("sandbox-id", "https://server.example", "alice", "wrong-password")
    assert "private failure" not in str(exc.value)


@pytest.mark.parametrize("username,password", [("alice\nbob", "pw"), ("alice", "pw\r")])
def test_login_rejects_multiline_credentials(username, password):
    module = _load(ROOT / "deploy/cwsandbox/login.py")
    with pytest.raises(ValueError, match="newlines"):
        module.login("sandbox-id", "https://server.example", username, password)


def test_managed_auth_uses_saved_login_and_explicit_override(managed, monkeypatch):
    from omnigent import cli_auth

    refresh = Mock(return_value="refreshed-token")
    load = Mock(return_value="saved-token")
    monkeypatch.setattr(cli_auth, "refresh_stored_token", refresh)
    monkeypatch.setattr(cli_auth, "load_token", load)
    managed.authenticate("https://server.example")
    assert managed.CLIENT.headers["Authorization"] == "Bearer refreshed-token"
    refresh.assert_called_once_with("https://server.example")
    load.assert_not_called()

    refresh.return_value = None
    managed.authenticate("https://server.example")
    assert managed.CLIENT.headers["Authorization"] == "Bearer saved-token"

    refresh.reset_mock()
    load.reset_mock()
    monkeypatch.setenv("OMNIGENT_API_TOKEN", "explicit-token")
    managed.authenticate("https://server.example")
    assert managed.CLIENT.headers["Authorization"] == "Bearer explicit-token"
    refresh.assert_not_called()
    load.assert_not_called()


def test_managed_agent_selection_requires_openai_agents(managed, monkeypatch):
    agents = [{"id": "native", "name": "codex-native-ui", "harness": "codex-native"}]
    response = httpx.Response(
        200,
        json={"data": agents},
        request=httpx.Request("GET", "https://server.example/v1/agents"),
    )
    monkeypatch.setattr(managed.CLIENT, "get", lambda *a, **kw: response)
    with pytest.raises(SystemExit, match="openai-agents"):
        managed.pick_agent("https://server.example")
    with pytest.raises(SystemExit, match="Native agents"):
        managed.pick_agent("https://server.example", "native")

    agents.extend(
        [
            {"id": "other", "name": "other", "harness": "openai-agents"},
            {"id": "probe", "name": "e2e-probe", "harness": "openai-agents"},
        ]
    )
    response = httpx.Response(
        200,
        json={"data": agents},
        request=httpx.Request("GET", "https://server.example/v1/agents"),
    )
    assert managed.pick_agent("https://server.example") == "probe"
    assert managed.pick_agent("https://server.example", "other") == "other"
