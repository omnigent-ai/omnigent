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


def test_onboarding_uses_selected_claude_directory(tmp_path):
    env = isolated_env({"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": "/parent"}, tmp_path)
    workspace = tmp_path / "workspace"
    ensure_claude_workspace_trusted(workspace, env=env)
    state = json.loads((Path(env["CLAUDE_CONFIG_DIR"]) / ".claude.json").read_text())
    assert state["hasCompletedOnboarding"]
    assert state["projects"][str(workspace)]["hasTrustDialogAccepted"]
    assert not (tmp_path / "home/.claude.json").exists()


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


def test_relay_preserves_half_closed_connection_during_collection():
    import asyncio
    import gc

    payload = b"delayed-response" * 20000
    received = []
    request_received = threading.Event()
    respond_now = threading.Event()
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
                request_received.set()
                if respond_now.wait(5):
                    conn.sendall(payload)

        async def collect():
            # Let the request-side copy and its completion callbacks finish.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gc.collect()

        thread = threading.Thread(target=respond, daemon=True)
        thread.start()
        try:
            with Relay(tcp_target=service.getsockname()) as relay:
                with socket.create_connection(("127.0.0.1", relay.port), timeout=5) as client:
                    client.sendall(b"request")
                    client.shutdown(socket.SHUT_WR)
                    assert request_received.wait(5)
                    asyncio.run_coroutine_threadsafe(collect(), relay.loop).result(timeout=5)
                    respond_now.set()
                    response = b""
                    while data := client.recv(65536):
                        response += data
                    assert response == payload
        finally:
            respond_now.set()
            thread.join(timeout=5)
        assert not thread.is_alive()
        assert received == [b"request"]


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

    monkeypatch.setattr("dev.repro_env.doctor.launch_observations", lambda root: {})

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


def test_generated_codex_provider_prices_sessions_like_the_e2e_fixture(monkeypatch, tmp_path):
    from dev.repro_env.runtime import write_model_config
    from omnigent.llms.context_window import fetch_model_pricing_with_provider
    from omnigent.onboarding.provider_config import load_config
    from tests.helpers.ui_configuration import _CODEX_MOCK_PRICING_PER_MILLION

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")
    write_model_config(tmp_path, "http://127.0.0.1:12345", "mock-claude", "mock-codex")

    pricing = fetch_model_pricing_with_provider("mock-codex", load_config(), "codex")

    assert pricing is not None, "prepared codex provider must price codex-native sessions"
    input_rate, output_rate, cache_read_rate = _CODEX_MOCK_PRICING_PER_MILLION
    assert pricing.input_per_token == pytest.approx(input_rate / 1_000_000)
    assert pricing.output_per_token == pytest.approx(output_rate / 1_000_000)
    assert pricing.cache_read_per_token == pytest.approx(cache_read_rate / 1_000_000)


def test_supervisor_terminates_children_when_relay_cleanup_fails(tmp_path, monkeypatch):
    import time

    from dev.repro_env import runtime

    monkeypatch.setattr("dev.repro_env.doctor.launch_observations", lambda root: {})

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
    model_command = runtime.subprocess.Popen.call_args_list[0].args[0]
    assert model_command[1:3] == ["-m", "tests.server.integration.mock_llm_server"]
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
