"""Terminal prepare tests for Codex session."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest

from omnigent._runner_startup import RunnerStartupProgress
from omnigent.harnesses.codex_native import main as codex_native
from tests.harnesses.codex_native.session._support import (
    _write_source_rollout,
)


def test_wrapper_spec_raw_instructions_resolves_prompt(tmp_path: Path) -> None:
    """The ``omnigent codex`` wrapper's own materialized spec is resolvable.

    Its ``prompt`` field is real ``AgentSpec.instructions`` content, not
    framework-composed text, so it must reach ``developer_instructions``
    like any other codex-native author instructions.
    """
    spec_path = codex_native._materialize_codex_agent_spec(tmp_path, model=None)
    result = codex_native._wrapper_spec_raw_instructions(spec_path)
    assert result is not None
    assert "Codex is running in the session terminal" in result


def test_wrapper_spec_raw_instructions_degrades_on_malformed_spec(tmp_path: Path) -> None:
    """A malformed wrapper spec must not block the terminal launch."""
    bad_spec = tmp_path / "bad.yaml"
    bad_spec.write_text("not: [valid, agent, spec")
    assert codex_native._wrapper_spec_raw_instructions(bad_spec) is None


@pytest.mark.asyncio
async def test_prepare_codex_terminal_fresh_session_passes_developer_instructions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Direct-wrapper fresh launch passes raw author instructions through to
    ``build_codex_native_server`` as ``developer_instructions``.

    The direct-wrapper call site in ``_prepare_codex_terminal``
    (``codex_native.py``), where the CLI-launched path can discard the value
    while the managed-host path still receives it.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "codex-bridge"
    )

    async def _fake_create_session(_client: object, _bundle: bytes, *, bridge_id: str) -> str:
        del _client, _bundle, bridge_id
        return "conv_fresh_di"

    captured: dict[str, Any] = {}

    class _Sentinel(Exception):
        pass

    def _fake_build_codex_native_server(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Sentinel

    monkeypatch.setattr(codex_native, "_create_codex_session", _fake_create_session)
    monkeypatch.setattr(codex_native, "build_codex_native_server", _fake_build_codex_native_server)

    with pytest.raises(_Sentinel):
        await codex_native._prepare_codex_terminal(
            base_url="http://test",
            headers={},
            session_id=None,
            runner_id=None,
            session_bundle=b"fake-bundle",
            codex_args=(),
            command="codex",
            model=None,
            developer_instructions="Be a concise, careful coding assistant.",
        )

    assert captured.get("developer_instructions") == "Be a concise, careful coding assistant."
    assert captured["session_id"] == "conv_fresh_di"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_failure", [None, "terminal", "client"])
@pytest.mark.parametrize("error", [RuntimeError, asyncio.CancelledError])
async def test_prepare_codex_terminal_closes_resources_when_cleanup_is_interrupted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cleanup_failure: str | None,
    error: type[BaseException],
) -> None:
    """A failed or cancelled close must not skip the remaining owned resources."""
    from unittest.mock import AsyncMock

    closed: list[str] = []

    async def close_resource(name: str) -> None:
        closed.append(name)
        if cleanup_failure == name:
            raise error("cleanup failed")

    async def close_terminal(**_kwargs: Any) -> None:
        await close_resource("terminal")

    async def close_client() -> None:
        await close_resource("client")

    async def close_server() -> None:
        await close_resource("server")

    def progress(_progress: object, message: str) -> None:
        if message == "Codex terminal ready.":
            raise error("startup failed")

    client = SimpleNamespace(close=close_client)
    server = SimpleNamespace(
        start=AsyncMock(),
        close=close_server,
        codex_cli_version=(0, 154, 0),
        config_overrides=[],
        env={},
        codex_home=tmp_path / "codex-home",
    )
    preload = AsyncMock(return_value=client)
    monkeypatch.setattr("omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(
        codex_native,
        "_fetch_codex_session",
        AsyncMock(
            return_value={
                "labels": {codex_native._WRAPPER_LABEL_KEY: codex_native._WRAPPER_LABEL_VALUE},
                "external_session_id": "thread_test",
            }
        ),
    )
    monkeypatch.setattr(codex_native, "_find_running_codex_terminal", AsyncMock(return_value=None))
    monkeypatch.setattr(codex_native, "_ensure_local_codex_resume_rollout", AsyncMock())
    monkeypatch.setattr(codex_native, "build_codex_native_server", lambda **_kwargs: server)
    monkeypatch.setattr(codex_native, "preload_codex_thread_for_resume", preload)
    monkeypatch.setattr(
        codex_native,
        "_launch_codex_terminal",
        AsyncMock(
            return_value=SimpleNamespace(
                terminal_id="terminal_test",
            )
        ),
    )
    monkeypatch.setattr(codex_native, "_update_startup_progress", progress)
    monkeypatch.setattr(codex_native, "_close_codex_terminal", close_terminal)

    with pytest.raises(error, match="cleanup failed" if cleanup_failure else "startup failed"):
        await codex_native._prepare_codex_terminal(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_test",
            runner_id=None,
            session_bundle=None,
            codex_args=(),
            command="codex",
            model=None,
        )
    assert preload.await_args.kwargs["retain_client"] is True
    assert closed == ["terminal", "client", "server"]


def test_run_with_local_server_threads_raw_instructions_to_prepare_terminal_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The real outer call site (``_run_with_local_server``) threads the
    wrapper spec's raw instructions all the way into ``_prepare_codex_terminal``
    on a FRESH session — with ``_prepare_codex_terminal`` itself REAL, not
    faked. Only ``build_codex_native_server`` — one level further in — is
    faked (and made to raise immediately after capturing its kwargs), to
    observe what the real ``_prepare_codex_terminal`` call actually
    forwards, without needing to fake the rest of the app-server lifecycle.
    """
    spec_path = codex_native._materialize_codex_agent_spec(tmp_path, model=None)

    class _Proc:
        def poll(self) -> None:
            return None

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def _fake_create_session(_client: object, _bundle: bytes, *, bridge_id: str) -> str:
        del _client, _bundle, bridge_id
        return "conv_fresh_wiring"

    captured: dict[str, Any] = {}

    class _Sentinel(Exception):
        pass

    def _fake_build_codex_native_server(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Sentinel

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 12401)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(codex_native, "_resolve_session_id_for_resume", lambda **kwargs: None)
    monkeypatch.setattr(codex_native, "_create_codex_session", _fake_create_session)
    monkeypatch.setattr(codex_native, "build_codex_native_server", _fake_build_codex_native_server)

    with pytest.raises(_Sentinel):
        codex_native._run_with_local_server(
            spec_path,
            session_id=None,
            resume_picker=False,
            codex_args=(),
            command="codex",
            model=None,
            prompt=None,
            auto_open_conversation=False,
        )

    assert captured.get("developer_instructions") is not None
    assert "Codex is running in the session terminal" in captured["developer_instructions"]


def test_run_with_local_server_threads_raw_instructions_to_prepare_terminal_resume(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Same as the fresh-session sibling, but for an existing session id
    (``resolved_session_id is not None`` — the RESUME branch of
    ``_prepare_codex_terminal``, e.g. cold-resume with no live terminal).
    """
    spec_path = codex_native._materialize_codex_agent_spec(tmp_path, model=None)

    class _Proc:
        def poll(self) -> None:
            return None

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def _fake_fetch_codex_session(_client: object, _session_id: str) -> dict[str, Any]:
        del _client, _session_id
        return {
            "labels": {codex_native._WRAPPER_LABEL_KEY: codex_native._WRAPPER_LABEL_VALUE},
            "external_session_id": "019e96aa-0be2-7343-8d3b-6f914d60936b",
        }

    async def _fake_find_running_codex_terminal(_client: object, _session_id: str) -> None:
        del _client, _session_id
        return

    async def _fake_ensure_local_codex_resume_rollout(*args: object, **kwargs: object) -> Path:
        del args, kwargs
        return tmp_path / "rollout.jsonl"

    captured: dict[str, Any] = {}

    class _Sentinel(Exception):
        pass

    def _fake_build_codex_native_server(**kwargs: object) -> object:
        captured.update(kwargs)
        raise _Sentinel

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 12402)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr(
        codex_native,
        "_resolve_session_id_for_resume",
        lambda **kwargs: "conv_resume_wiring",
    )
    monkeypatch.setattr(
        codex_native, "_align_working_directory_with_session", lambda session_id: None
    )
    monkeypatch.setattr(codex_native, "_fetch_codex_session", _fake_fetch_codex_session)
    monkeypatch.setattr(
        codex_native, "_find_running_codex_terminal", _fake_find_running_codex_terminal
    )
    monkeypatch.setattr(
        codex_native,
        "_ensure_local_codex_resume_rollout",
        _fake_ensure_local_codex_resume_rollout,
    )
    monkeypatch.setattr(codex_native, "build_codex_native_server", _fake_build_codex_native_server)

    with pytest.raises(_Sentinel):
        codex_native._run_with_local_server(
            spec_path,
            session_id="conv_resume_wiring",
            resume_picker=False,
            codex_args=(),
            command="codex",
            model=None,
            prompt=None,
            auto_open_conversation=False,
        )

    assert captured.get("developer_instructions") is not None
    assert "Codex is running in the session terminal" in captured["developer_instructions"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure_status", [200, 503])
async def test_prepare_codex_terminal_via_daemon_creates_runner_and_ensures_terminal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    ensure_status: int,
) -> None:
    """
    Daemon preparation owns session create, runner launch, and terminal ensure.

    This exercises the real ``_prepare_codex_terminal_via_daemon`` orchestration
    against an ``httpx.MockTransport`` Omnigent server. Removing terminal launch arg
    persistence, daemon runner launch, the runner re-bind (which clears
    ``omnigent.stopped`` on resume), the ``ensure_native_terminal``
    request, or terminal metadata decoding turns this test red.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent import _startup_events as startup

    caplog.set_level("INFO", logger="omnigent.startup")
    original_async_client = httpx.AsyncClient
    calls: list[tuple[str, str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Route Omnigent requests issued by daemon preparation.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        path = request.url.path
        body: object = None
        if request.content:
            content_type = request.headers.get("content-type", "")
            body = (
                json.loads(request.content)
                if content_type.startswith("application/json")
                else request.content
            )
        calls.append((request.method, path, body))
        if request.method == "POST" and path == "/v1/sessions":
            return httpx.Response(201, json={"session_id": "conv_new"})
        if request.method == "PATCH" and path == "/v1/sessions/conv_new":
            return httpx.Response(200, json={})
        if request.method == "GET" and path == "/v1/hosts/host_local":
            return httpx.Response(200, json={"status": "online"})
        if request.method == "GET" and path == "/v1/sessions/conv_new":
            return httpx.Response(200, json={})
        if request.method == "POST" and path == "/v1/hosts/host_local/runners":
            return httpx.Response(200, json={"runner_id": "runner_new"})
        if request.method == "GET" and path == "/v1/runners/runner_new/status":
            return httpx.Response(200, json={"online": True})
        if request.method == "POST" and path.endswith("/resources/terminals"):
            return httpx.Response(ensure_status, json={"id": "terminal_codex_main"})
        if request.method == "GET" and path.endswith("/resources/terminals/terminal_codex_main"):
            return httpx.Response(
                200,
                json={
                    "id": "terminal_codex_main",
                    "metadata": {
                        "tmux_socket": "/tmp/codex.sock",
                        "tmux_target": "codex:main",
                    },
                },
            )
        return httpx.Response(404, json={"error": {"message": path}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        """
        Inject the mock Omnigent transport into clients created by the helper.

        :param args: Positional ``httpx.AsyncClient`` args.
        :param kwargs: Keyword ``httpx.AsyncClient`` args.
        :returns: Async client using the mock transport.
        """
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(codex_native.httpx, "AsyncClient", client_factory)
    progress_updates: list[str] = []

    expected_error = (
        pytest.raises(click.ClickException) if ensure_status != 200 else contextlib.nullcontext()
    )
    with expected_error:
        with startup.native_startup_attempt(harness="codex-native"):
            prepared = await codex_native._prepare_codex_terminal_via_daemon(
                base_url="https://example.com",
                headers={},
                session_id=None,
                session_bundle=b"bundle",
                codex_args=("--config", "approval_policy=on-request"),
                model="gpt-5.4-mini",
                host_id="host_local",
                workspace="/repo",
                startup_progress=RunnerStartupProgress(update=progress_updates.append),
            )
    if ensure_status != 200:
        events = [r.attributes["event"] for r in caplog.records if r.name == "omnigent.startup"]
        assert events[-1] == "launch_failed"
        assert "terminal_available" not in events
        return

    assert prepared.session_id == "conv_new"
    assert prepared.terminal_id == "terminal_codex_main"
    assert prepared.tmux_socket == Path("/tmp/codex.sock")
    assert prepared.tmux_target == "codex:main"
    assert prepared.app_server_url is None
    assert prepared.app_server is None
    assert prepared.event_client is None
    assert prepared.reattached is False
    create_body = next(
        body for method, path, body in calls if method == "POST" and path == "/v1/sessions"
    )
    assert isinstance(create_body, bytes)
    assert b'"terminal_launch_args"' in create_body
    assert b"approval_policy=on-request" in create_body
    assert (
        "POST",
        "/v1/hosts/host_local/runners",
        {"session_id": "conv_new", "workspace": "/repo"},
    ) in calls
    # Runner re-bind clears omnigent.stopped on resume.
    assert ("PATCH", "/v1/sessions/conv_new", {"runner_id": "runner_new"}) in calls
    assert (
        "POST",
        "/v1/sessions/conv_new/resources/terminals",
        {"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
    ) in calls
    assert progress_updates == [
        "Creating Codex session...",
        "Starting runner...",
        "Waiting for runner...",
        "Starting Codex terminal...",
        "Codex terminal ready.",
    ]

    events = [r.attributes for r in caplog.records if r.name == "omnigent.startup"]
    assert [e["event"] for e in events] == [
        "launch_started",
        "session_resolved",
        "runner_requested",
        "runner_connected",
        "session_runner_bound",
        "terminal_available",
        "launch_incomplete",
    ]
    assert len({e["attempt_id"] for e in events}) == 1
    assert all(
        r.session_id == "conv_new"
        for r in caplog.records
        if r.name == "omnigent.startup" and r.attributes["event"] != "launch_started"
    )


@pytest.mark.asyncio
async def test_prepare_codex_terminal_via_daemon_overlaps_create_and_host_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A fresh launch runs session create and the host-online poll concurrently.

    Against a remote server the create costs ~2s and the host poll ~0.6s, and
    running them back to back paid both. The host wait must therefore start
    before the create finishes, and a fresh launch must not wait for the host a
    second time afterwards.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    order: list[str] = []
    host_waits = 0

    async def fake_create(
        client: object,
        bundle: bytes,
        *,
        bridge_id: str | None,
        terminal_launch_args: list[str] | None = None,
    ) -> str:
        """Record the create window, yielding so a concurrent wait can start."""
        del client, bundle, bridge_id, terminal_launch_args
        order.append("create:start")
        await asyncio.sleep(0)
        order.append("create:end")
        return "conv_new"

    async def fake_host_online(client: object, host_id: str, *, timeout_s: float) -> None:
        """Record the host-wait window and count how often it is entered."""
        nonlocal host_waits
        del client, host_id, timeout_s
        host_waits += 1
        order.append("host:start")
        await asyncio.sleep(0)
        order.append("host:end")

    async def fake_launch(
        client: object,
        *,
        host_id: str,
        session_id: str,
        workspace: str,
        fresh: bool = False,
    ) -> str:
        """Stand in for the runner launch."""
        del client, host_id, session_id, workspace, fresh
        return "runner_new"

    async def fake_runner_online(client: object, runner_id: str, *, timeout_s: float) -> None:
        """Pretend the runner connected."""
        del client, runner_id, timeout_s

    async def fake_bind(client: object, session_id: str, runner_id: str) -> None:
        """Skip the re-bind PATCH."""
        del client, session_id, runner_id

    async def fake_ensure(client: object, session_id: str) -> None:
        """Skip the terminal ensure request."""
        del client, session_id

    async def fake_terminal_ready(
        client: object, session_id: str, *, timeout_s: float
    ) -> codex_native.LaunchedCodexTerminal:
        """Return a ready terminal without polling."""
        del client, session_id, timeout_s
        return codex_native.LaunchedCodexTerminal(
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
        )

    monkeypatch.setattr(codex_native, "_create_codex_session", fake_create)
    monkeypatch.setattr(codex_native, "wait_for_host_online", fake_host_online)
    monkeypatch.setattr(codex_native, "launch_or_reuse_daemon_runner", fake_launch)
    monkeypatch.setattr(codex_native, "wait_for_runner_online", fake_runner_online)
    monkeypatch.setattr(codex_native, "_bind_session_runner", fake_bind)
    monkeypatch.setattr(codex_native, "_ensure_codex_terminal_on_runner", fake_ensure)
    monkeypatch.setattr(codex_native, "_wait_for_codex_terminal_ready", fake_terminal_ready)

    prepared = await codex_native._prepare_codex_terminal_via_daemon(
        base_url="https://example.com",
        headers={},
        session_id=None,
        session_bundle=b"bundle",
        codex_args=(),
        model=None,
        host_id="host_local",
        workspace="/repo",
    )

    assert prepared.session_id == "conv_new"
    assert order.index("host:start") < order.index("create:end")
    assert host_waits == 1


@pytest.mark.asyncio
async def test_prepare_codex_terminal_via_daemon_live_resume_skips_config_patch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """
    Warm reattach leaves live terminal state alone.

    If a Codex terminal is already running, updating ``terminal_launch_args`` or
    ``model_override`` would only change the database for a later cold start and
    silently mislead the user. The helper must return the live terminal and warn
    instead of PATCHing the session. It also must not rewrite the live Codex
    rollout from Omnigent history while the app-server may be appending to it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param capsys: Pytest capture fixture.
    :returns: None.
    """
    original_async_client = httpx.AsyncClient
    calls: list[tuple[str, str, object]] = []
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    live_rollout = _write_source_rollout(
        codex_home=codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_live")),
        thread_id=thread_id,
        source_cwd="/repo",
    )
    before = live_rollout.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Route Omnigent requests for a live resume.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        path = request.url.path
        body: object = json.loads(request.content) if request.content else None
        calls.append((request.method, path, body))
        if request.method == "GET" and path == "/v1/sessions/conv_live":
            return httpx.Response(
                200,
                json={
                    "labels": {"omnigent.wrapper": "codex-native-ui"},
                    "external_session_id": thread_id,
                },
            )
        if request.method == "GET" and path.endswith("/resources/terminals/terminal_codex_main"):
            return httpx.Response(
                200,
                json={
                    "id": "terminal_codex_main",
                    "metadata": {
                        "tmux_socket": "/tmp/live.sock",
                        "tmux_target": "live:main",
                    },
                },
            )
        if request.method == "GET" and path == "/v1/sessions/conv_live/items":
            return httpx.Response(500, json={"error": {"message": "unexpected items fetch"}})
        if request.method == "PATCH":
            return httpx.Response(500, json={"error": {"message": "unexpected patch"}})
        return httpx.Response(404, json={"error": {"message": path}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        """
        Inject the mock Omnigent transport into clients created by the helper.

        :param args: Positional ``httpx.AsyncClient`` args.
        :param kwargs: Keyword ``httpx.AsyncClient`` args.
        :returns: Async client using the mock transport.
        """
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(codex_native.httpx, "AsyncClient", client_factory)

    prepared = await codex_native._prepare_codex_terminal_via_daemon(
        base_url="https://example.com",
        headers={},
        session_id="conv_live",
        session_bundle=None,
        codex_args=("--model", "gpt-5.4-mini"),
        model="gpt-5.4-mini",
        host_id="host_local",
        workspace="/repo",
    )

    assert prepared.session_id == "conv_live"
    assert prepared.terminal_id == "terminal_codex_main"
    assert prepared.tmux_socket == Path("/tmp/live.sock")
    assert prepared.tmux_target == "live:main"
    assert prepared.app_server_url is None
    assert prepared.thread_id == thread_id
    assert prepared.reattached is True
    assert ("GET", "/v1/sessions/conv_live/items", None) not in calls
    assert not any(method == "PATCH" for method, _path, _body in calls)
    assert live_rollout.read_bytes() == before
    assert "Ignoring Codex launch args/model" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_prepare_codex_terminal_hot_resume_does_not_rewrite_rollout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Local hot reattach leaves live Codex rollout state alone.

    ``_prepare_codex_terminal`` has an early return when the terminal
    resource is already running. That hot path must not synthesize or
    rewrite rollout files from Omnigent history because Codex may be appending
    to the same JSONL file concurrently.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for isolated bridge state.
    :returns: None.
    """
    original_async_client = httpx.AsyncClient
    session_id = "conv_hot_codex"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    bridge_id = "bridge_hot_codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    live_rollout = _write_source_rollout(
        codex_home=codex_home_for_bridge_dir(bridge_dir_for_bridge_id(bridge_id)),
        thread_id=thread_id,
        source_cwd=str(workspace),
    )
    before = live_rollout.read_bytes()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve a codex-native session, live terminal, and Omnigent item history.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        path = request.url.path
        calls.append((request.method, path))
        if request.method == "GET" and path == f"/v1/sessions/{session_id}":
            return httpx.Response(
                200,
                json={
                    "labels": {
                        "omnigent.wrapper": "codex-native-ui",
                        "omnigent.codex_native.bridge_id": bridge_id,
                    },
                    "external_session_id": thread_id,
                },
            )
        if (
            request.method == "GET"
            and path == f"/v1/sessions/{session_id}/resources/terminals/terminal_codex_main"
        ):
            return httpx.Response(
                200,
                json={
                    "id": "terminal_codex_main",
                    "metadata": {
                        "tmux_socket": "/tmp/live-codex.sock",
                        "tmux_target": "codex-live:main",
                    },
                },
            )
        if request.method == "GET" and path == f"/v1/sessions/{session_id}/items":
            return httpx.Response(500, json={"error": {"message": "unexpected items fetch"}})
        return httpx.Response(404, json={"error": {"message": path}})

    transport = httpx.MockTransport(handler)

    def client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
        """
        Inject the mock Omnigent transport into clients created by the helper.

        :param args: Positional ``httpx.AsyncClient`` args.
        :param kwargs: Keyword ``httpx.AsyncClient`` args.
        :returns: Async client using the mock transport.
        """
        kwargs["transport"] = transport
        return original_async_client(*args, **kwargs)

    monkeypatch.setattr(codex_native.httpx, "AsyncClient", client_factory)

    prepared = await codex_native._prepare_codex_terminal(
        base_url="https://example.com",
        headers={},
        session_id=session_id,
        runner_id="runner_local",
        session_bundle=None,
        codex_args=(),
        command="/opt/codex/bin/codex",
        model=None,
    )

    assert prepared.reattached is True
    assert prepared.session_id == session_id
    assert prepared.thread_id == thread_id
    assert prepared.tmux_socket == Path("/tmp/live-codex.sock")
    assert prepared.tmux_target == "codex-live:main"
    assert ("GET", f"/v1/sessions/{session_id}/items") not in calls
    assert live_rollout.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body"),
    [
        (404, {"error": {"code": "not_found", "message": "Resource not found"}}),
        (
            409,
            {
                "error": {
                    "code": "conflict",
                    "message": (
                        "conversation 'conv_abc' is not bound to a runner; "
                        "resume the session to bind a registered runner"
                    ),
                }
            },
        ),
        (
            503,
            {
                "error": {
                    "code": "runner_unavailable",
                    "message": (
                        "runner 'runner_token_dead' is offline for conversation 'conv_abc'"
                    ),
                }
            },
        ),
    ],
)
async def test_find_running_codex_terminal_known_misses_relaunch(
    status_code: int,
    body: dict[str, Any],
) -> None:
    """
    Missing terminals or unavailable prior runners relaunch cleanly.

    These are the explicit reattach-miss shapes: absent terminal,
    unbound conversation, and stale runner. They let resume bind the
    current runner and launch ``codex resume``.

    :param status_code: HTTP status returned by the Omnigent resource lookup.
    :param body: Structured Omnigent error body for the lookup.
    :returns: None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a reattach miss response.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        assert request.url.path.endswith("/resources/terminals/terminal_codex_main")
        return httpx.Response(status_code, json=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        found = await codex_native._find_running_codex_terminal(client, "conv_abc")

    assert found is None


@pytest.mark.asyncio
async def test_find_running_codex_terminal_unexpected_error_still_raises() -> None:
    """
    Non-reattach failures still fail loud.

    :returns: None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return an unexpected server error.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        del request
        return httpx.Response(500, text="database unavailable")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        with pytest.raises(click.ClickException) as excinfo:
            await codex_native._find_running_codex_terminal(client, "conv_abc")

    assert "Failed to fetch Codex terminal (500)" in excinfo.value.message
    assert "database unavailable" in excinfo.value.message


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [409, 502, 503])
async def test_find_running_codex_terminal_generic_errors_still_raise(
    status_code: int,
) -> None:
    """
    Generic infra failures are not treated as "no terminal".

    :param status_code: HTTP status returned by the Omnigent resource lookup.
    :returns: None.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a non-reattach failure response.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        del request
        return httpx.Response(
            status_code,
            json={"error": {"code": "internal_error", "message": "database unavailable"}},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="https://example.com") as client:
        with pytest.raises(click.ClickException) as excinfo:
            await codex_native._find_running_codex_terminal(client, "conv_abc")

    assert f"Failed to fetch Codex terminal ({status_code})" in excinfo.value.message
    assert "database unavailable" in excinfo.value.message
