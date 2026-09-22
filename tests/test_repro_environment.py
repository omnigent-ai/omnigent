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


def test_relay_preserves_response_after_client_half_close(tmp_path):
    payload = b"delayed-response" * 20000
    received = []
    with socket.socket() as service:
        service.bind(("127.0.0.1", 0))
        service.listen()
        service.settimeout(5)

        def respond():
            conn, _ = service.accept()
            with conn:
                conn.settimeout(5)
                request = b""
                while data := conn.recv(65536):
                    request += data
                received.append(request)
                conn.sendall(payload)

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        # Both listener and client must work with a Unix path longer than sun_path.
        directory = tmp_path / ("long-directory-" * 9)
        directory.mkdir(mode=0o700)
        path = directory / "service.sock"
        with Relay(unix_listener=path, tcp_target=service.getsockname()):
            with Relay(unix_target=path) as relay:
                with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
                    client.sendall(b"request")
                    client.shutdown(socket.SHUT_WR)
                    response = b""
                    while data := client.recv(65536):
                        response += data
                    assert response == payload
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert received == [b"request"]


def test_relay_requires_private_socket_directory(tmp_path):
    directory = tmp_path / "shared"
    directory.mkdir(mode=0o755)
    path = directory / "service.sock"
    with pytest.raises(ValueError, match="owner-only"):
        with Relay(unix_listener=path, tcp_target=("127.0.0.1", 1)):
            pytest.fail("must reject before accepting connections")
    assert not path.exists()


@pytest.mark.parametrize("present", range(1, 7))
def test_partial_prepared_environment_fails_before_spawning_mock(monkeypatch, present):
    keys = ("OMNIGENT_REPRO_SERVER_URL", "OMNIGENT_REPRO_MODEL_URL", "OMNIGENT_REPRO_RUNNER_ID")
    for index, key in enumerate(keys):
        monkeypatch.setenv(key, "configured" if present & (1 << index) else "")
    spawn = Mock(side_effect=AssertionError("must not spawn a different mock"))
    monkeypatch.setattr(fixtures.subprocess, "Popen", spawn)
    with pytest.raises(RuntimeError, match="Incomplete prepared reproduction environment"):
        next(fixtures.mock_llm_server_url.__wrapped__(None))
    spawn.assert_not_called()


@pytest.mark.parametrize("key", ["pid", "runner_pid", "database_uri", "restart_server"])
def test_workflow_owned_state_explains_unsupported_access(key):
    state = fixtures._ServerState(workflow_owned=True)
    with pytest.raises(RuntimeError, match="Workflow-owned reproduction does not expose"):
        _ = state[key]


def test_mock_config_backup_survives_and_blocks_overwrite(monkeypatch, tmp_path):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    backup = tmp_path / "config.yaml.e2e-backup"
    path.write_bytes(b"original config\n")
    with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
        assert backup.read_bytes() == b"original config\n"
        assert backup.stat().st_mode & 0o077 == 0
    assert path.read_bytes() == b"original config\n"
    assert not backup.exists()
    backup.write_bytes(b"interrupted run original\n")
    with pytest.raises(RuntimeError, match="recover the original config"):
        with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
            pytest.fail("must not overwrite a recovery backup")
    assert backup.read_bytes() == b"interrupted run original\n"
    assert path.read_bytes() == b"original config\n"


def test_serve_rejects_previous_attempt_without_discarding_stop(tmp_path):
    from dev.repro_env.runtime import serve

    state = tmp_path / "environment.json"
    state.write_text('{"status":"stopped"}')
    (tmp_path / "stop").touch()
    with pytest.raises(ValueError, match="fresh --output directory"):
        serve(tmp_path, 60)
    assert (tmp_path / "stop").exists()
    assert json.loads(state.read_text()) == {"status": "stopped"}


def test_stop_during_startup_is_successful_cancellation(tmp_path, monkeypatch):
    import time

    from dev.repro_env import runtime

    state = tmp_path / "environment.json"
    state.write_text(
        json.dumps(
            {
                "status": "starting",
                "workspace": str(tmp_path),
                "expires_at": time.time() + 60,
            }
        )
    )
    child = Mock(pid=123)
    child.poll.return_value = None

    def spawn(*args, **kwargs):
        (tmp_path / "stop").touch()
        return child

    monkeypatch.setattr(runtime.subprocess, "Popen", spawn)
    kill = Mock()
    monkeypatch.setattr(runtime.os, "killpg", kill)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value.json.return_value = {}
    monkeypatch.setattr(runtime.httpx, "Client", Mock(return_value=client))
    runtime.supervise(tmp_path)
    assert json.loads(state.read_text())["status"] == "stopped"
    assert kill.call_count == 2
    child.wait.assert_called()


def test_relay_shutdown_closes_an_open_stream(tmp_path):
    import time

    with socket.socket() as service:
        service.bind(("127.0.0.1", 0))
        service.listen()
        service.settimeout(15)
        closed = threading.Event()

        def stream():
            conn, _ = service.accept()
            with conn:
                conn.settimeout(15)
                conn.sendall(b"ready")
                if conn.recv(1) == b"":
                    closed.set()

        thread = threading.Thread(target=stream, daemon=True)
        thread.start()
        path = tmp_path / "active.sock"
        with socket.socket(socket.AF_UNIX) as client:
            try:
                client.settimeout(5)
                with Relay(unix_listener=path, tcp_target=service.getsockname()):
                    client.connect(str(path))
                    assert client.recv(5) == b"ready"
                    started = time.monotonic()
                    # Keep the client open until the relay has shut down.
                assert time.monotonic() - started < 5
                assert client.recv(1) == b""
                assert closed.wait(5)
            finally:
                client.close()
                thread.join(timeout=5)
        assert not path.exists()


def test_generated_provider_config_disables_runner_idle_shutdown(monkeypatch, tmp_path):
    from dev.repro_env.runtime import write_model_config
    from omnigent.runner._entry import _load_runner_idle_timeout_s_from_config

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    write_model_config(tmp_path, "http://127.0.0.1:12345", "mock-claude", "mock-codex")
    assert _load_runner_idle_timeout_s_from_config() == 0


@pytest.mark.parametrize("raise_in_test", [False, True])
def test_mock_config_restores_symlink_target(monkeypatch, tmp_path, raise_in_test):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    target = tmp_path / "original.yaml"
    target.write_text("original provider config\n")
    config = tmp_path / "config.yaml"
    config.symlink_to(target.name)
    link_inode = config.lstat().st_ino
    try:
        with fixtures._temp_omnigent_mock_config("http://127.0.0.1:12345", "claude"):
            assert "12345" in target.read_text()
            if raise_in_test:
                raise ValueError("test assertion failed")
    except ValueError:
        assert raise_in_test
    assert config.is_symlink()
    assert config.lstat().st_ino == link_inode
    assert config.readlink() == Path(target.name)
    assert target.read_text() == "original provider config\n"


def test_supervisor_terminates_children_when_relay_cleanup_fails(tmp_path, monkeypatch):
    import time

    from dev.repro_env import runtime

    state = tmp_path / "environment.json"
    state.write_text(
        json.dumps(
            {
                "status": "starting",
                "workspace": str(tmp_path),
                "expires_at": time.time() + 60,
            }
        )
    )
    models = tmp_path / "tests/server/integration/repro_models.json"
    models.parent.mkdir(parents=True)
    models.write_text('{"claude-native":"mock-claude","codex-native":"mock-codex"}')
    children = [Mock(pid=123 + i) for i in range(3)]
    for child in children:
        child.poll.return_value = None
    monkeypatch.setattr(runtime.subprocess, "Popen", Mock(side_effect=children))
    kill = Mock()
    monkeypatch.setattr(runtime.os, "killpg", kill)
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=False)
    client.get.return_value.status_code = 200
    client.get.return_value.json.return_value = {"online": True}
    monkeypatch.setattr(runtime.httpx, "Client", Mock(return_value=client))
    relays = [Mock(), Mock()]
    for relay in relays:
        relay.__enter__ = Mock(side_effect=lambda: (tmp_path / "stop").touch())
        relay.__exit__ = Mock()
    relays[-1].__exit__.side_effect = TimeoutError("stuck relay")
    monkeypatch.setattr(runtime, "Relay", Mock(side_effect=relays))
    runtime.supervise(tmp_path)
    final = json.loads(state.read_text())
    assert final["status"] == "failed"
    assert "stuck relay" in final["error"]
    for relay in relays:
        relay.__exit__.assert_called_once()
    for child in children:
        child.wait.assert_called()
        kill.assert_any_call(child.pid, runtime.signal.SIGTERM)
        kill.assert_any_call(child.pid, runtime.signal.SIGKILL)


def test_generated_config_keeps_idle_runner_alive(monkeypatch, tmp_path):
    import asyncio

    from dev.repro_env.runtime import write_model_config
    from omnigent.runner._entry import (
        _load_runner_idle_timeout_s_from_config,
        _run_inactivity_monitor,
    )

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    write_model_config(tmp_path, "http://127.0.0.1:12345", "mock-claude", "mock-codex")
    shutdown = Mock()

    async def check():
        loop = asyncio.get_running_loop()
        options = {
            "get_last_activity": lambda: loop.time() - 7200,
            "has_active_work": lambda: False,
            "request_shutdown": shutdown,
            "poll_interval_s": 0.001,
        }
        await _run_inactivity_monitor(idle_timeout_s=0.01, **options)
        shutdown.assert_called_once()
        shutdown.reset_mock()
        await _run_inactivity_monitor(
            idle_timeout_s=_load_runner_idle_timeout_s_from_config(), **options
        )
        shutdown.assert_not_called()

    asyncio.run(check())
