"""Configuration and transport regressions for workflow-owned reproduction."""

import json
import socket
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from dev.repro_env.runtime import isolated_env
from dev.repro_env.transport import Relay
from omnigent.harnesses.claude_native.bridge import ensure_claude_workspace_trusted
from tests.e2e_ui import conftest as fixtures


def test_isolates_inherited_native_state(tmp_path):
    env = isolated_env(
        {
            "LLM_API_KEY": "placeholder",
            "OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD": "999",
            "OMNIGENT_CONFIG_HOME": "/parent",
            "CLAUDE_CONFIG_DIR": "/parent-claude",
            "OPENAI_API_KEY": "parent-key",
            "NO_PROXY": "example.test",
        },
        tmp_path,
    )
    assert "LLM_API_KEY" not in env
    assert "OMNIGENT_RUNNER_ZYGOTE_CONTROL_FD" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["OMNIGENT_CONFIG_HOME"] == str(tmp_path / "config")
    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "claude-config")
    assert "127.0.0.1" in env["NO_PROXY"]


def test_onboarding_uses_selected_claude_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "selected"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    workspace = tmp_path / "workspace"
    ensure_claude_workspace_trusted(workspace)
    state = json.loads((tmp_path / "selected/.claude.json").read_text())
    assert state["hasCompletedOnboarding"]
    assert state["projects"][str(workspace)]["hasTrustDialogAccepted"]
    assert not (tmp_path / "home/.claude.json").exists()


def test_mock_config_honors_config_home_and_restores(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("original\n")
    with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
        assert "12345" in path.read_text()
    assert path.read_text() == "original\n"


@pytest.mark.parametrize("harness", ["claude", "codex"])
@pytest.mark.parametrize("owned", [True, False])
def test_mock_fixture_ignores_credential_placeholder(monkeypatch, harness, owned):
    monkeypatch.setenv("LLM_API_KEY", "synthetic-proxy-placeholder")
    monkeypatch.setattr(
        fixtures, "_server_state", {"runner_id": "runner", "workflow_owned": owned}
    )
    monkeypatch.setattr(fixtures, "_ensure_runner_online", lambda *_: None)
    monkeypatch.setattr(fixtures, f"_create_native_{harness}_session", lambda *_: "session")
    monkeypatch.setattr(fixtures.httpx, "delete", Mock())
    from contextlib import nullcontext

    configure = Mock(return_value=nullcontext())
    monkeypatch.setattr(fixtures, "_temp_omnigent_mock_config", configure)
    fixture = getattr(fixtures, f"native_{harness}_mock_session").__wrapped__
    journey = fixture("http://server", "http://model", None)
    assert next(journey) == ("http://server", "session")
    journey.close()
    assert configure.call_count == (0 if owned else 1)


def test_relays_streams_and_reconnects_without_restarting_service(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with socket.socket() as service:
        service.bind(("127.0.0.1", 0))
        service.listen()

        def echo():
            for _ in range(2):
                client, _ = service.accept()
                with client:
                    while data := client.recv(65536):
                        client.sendall(data)

        thread = threading.Thread(target=echo, daemon=True)
        thread.start()
        path = tmp_path / "service.sock"
        with Relay(unix_listener=path, tcp_target=service.getsockname()):
            for _ in range(2):
                with Relay(unix_target=path) as connection:
                    with socket.create_connection(
                        ("127.0.0.1", connection.port), timeout=5
                    ) as client:
                        for payload in (b"GET / HTTP/1.1\r\n\r\n", b"x" * 65536):
                            client.sendall(payload)
                            actual = b""
                            while len(actual) < len(payload):
                                actual += client.recv(len(payload) - len(actual))
                            assert actual == payload
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert not path.exists()
