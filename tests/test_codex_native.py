"""Tests for the native Codex wrapper helpers."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import click
import httpx
import pytest
import yaml

from omnigent import codex_native, codex_native_app_server, codex_native_forwarder
from omnigent._runner_startup import RunnerStartupProgress
from omnigent.codex_native_bridge import (
    CodexNativeBridgeState,
    clear_bridge_state,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.codex_native_elicitation import codex_elicitation_id
from omnigent.spec import load


def _write_codex_auth(path: Path, payload: object) -> None:
    """Write a test Codex auth.json payload."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _point_codex_auth_check_at(
    monkeypatch: pytest.MonkeyPatch, auth_path: Path, *, binary_present: bool
) -> None:
    """Redirect Codex availability checks away from the real machine state."""
    monkeypatch.setattr(
        codex_native,
        "_resolve_codex_auth_source",
        lambda: codex_native._CodexAuthSource(auth_path=auth_path),
    )
    monkeypatch.setattr(
        codex_native.shutil,
        "which",
        lambda name: f"/tmp/{name}" if binary_present else None,
    )


def test_codex_auth_unavailable_reason_binary_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing codex binary reports binary-missing before reading auth.json."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=False)

    assert codex_native._codex_auth_unavailable_reason() == "binary-missing"


def test_codex_auth_unavailable_reason_absent_auth_json_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed codex without auth.json reports needs-auth."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_chatgpt_tokens_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real ChatGPT/OAuth auth.json (tokens block) is available.

    Mirrors the openai/codex ``AuthDotJson`` shape: ``auth_mode=chatgpt`` with a
    ``tokens`` object. There is no top-level expiry field — access-token expiry
    lives in the JWT and is refreshed via ``refresh_token`` — so presence of the
    tokens is what marks the credential configured.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(
        auth_path,
        {
            "auth_mode": "chatgpt",
            "tokens": {
                "id_token": "header.payload.sig",
                "access_token": "header.payload.sig",
                "refresh_token": "opaque-refresh",
                "account_id": "org_test",
            },
            "last_refresh": "2026-06-25T15:04:05Z",
        },
    )

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_api_key_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real API-key auth.json (``auth_mode=api``) is available."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(auth_path, {"auth_mode": "api", "OPENAI_API_KEY": "sk-test"})

    assert codex_native._codex_auth_unavailable_reason() is None


def test_codex_auth_unavailable_reason_no_credential_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A parseable auth.json with no credential field reports needs-auth.

    e.g. a stub that records ``auth_mode`` but carries neither an
    ``OPENAI_API_KEY`` nor a ``tokens`` block — there is nothing to authenticate
    with, so the picker should warn rather than show Codex as ready.
    """
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    _write_codex_auth(auth_path, {"auth_mode": "chatgpt"})

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


def test_codex_auth_unavailable_reason_malformed_auth_needs_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Installed codex with malformed auth.json reports needs-auth."""
    auth_path = tmp_path / "codex-home" / "auth.json"
    _point_codex_auth_check_at(monkeypatch, auth_path, binary_present=True)
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("{not json", encoding="utf-8")

    assert codex_native._codex_auth_unavailable_reason() == "needs-auth"


class _FakeTerminalClient:
    """
    Minimal async client for terminal-launch helper tests.

    :param response: HTTP response returned from ``post``.
    """

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.posts: list[tuple[str, dict[str, Any], float | None]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any],
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Capture a terminal-launch request.

        :param url: Request URL.
        :param json: JSON request body.
        :param timeout: Request timeout.
        :returns: Canned response.
        """
        self.posts.append((url, json, timeout))
        return self.response


class _FakeCodexWebSocket:
    """
    Minimal websocket for Codex app-server handshake tests.

    It immediately responds to the ``initialize`` request and records
    every outbound payload.
    """

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.responses: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        """
        Capture an outbound websocket text frame.

        :param payload: JSON-RPC text frame.
        :returns: None.
        """
        self.sent.append(payload)
        message = json.loads(payload)
        if message.get("method") == "initialize":
            await self.responses.put(json.dumps({"id": message["id"], "result": {}}))

    def __aiter__(self) -> _FakeCodexWebSocket:
        """
        Return the async iterator used by the client reader task.

        :returns: This websocket.
        """
        return self

    async def __anext__(self) -> str:
        """
        Yield the next queued inbound websocket text frame.

        :returns: JSON-RPC text frame.
        """
        return await self.responses.get()

    async def close(self) -> None:
        """
        Mark the fake websocket closed.

        :returns: None.
        """
        self.closed = True


class _FakeCodexAppServerClient:
    """
    Test double for ``CodexAppServerClient``.

    :param response: JSON-RPC response returned from ``request``.
    :param error: Optional exception raised from ``request``.
    :param events: Optional events yielded from ``iter_events``.
    """

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        error: Exception | None = None,
        events: list[dict[str, Any]] | None = None,
    ) -> None:
        self.response = response or {"result": {"thread": {"id": "thread_123"}}}
        self.error = error
        self.events = events or []
        self.connected = False
        self.closed = False
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []

    async def connect(self) -> None:
        """
        Mark the fake client connected.

        :returns: None.
        """
        self.connected = True

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture one JSON-RPC request.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: Canned JSON-RPC response.
        """
        self.requests.append((method, params))
        if self.error is not None:
            raise self.error
        return self.response

    async def iter_events(self) -> Any:
        """
        Return an empty event stream.

        :returns: Async iterator with no events.
        """
        for event in self.events:
            yield event

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """
        Capture one JSON-RPC response sent to the fake app-server.

        :param request_id: JSON-RPC request id.
        :param result: JSON-RPC result payload.
        :returns: None.
        """
        self.responses.append((request_id, result))

    async def close(self) -> None:
        """
        Mark the fake client closed.

        :returns: None.
        """
        self.closed = True


def test_clear_bridge_state_removes_stale_runtime_state(tmp_path: Path) -> None:
    """
    Clearing bridge state removes the stale runtime pointer.

    New app-server launches reuse the same bridge directory, so a leftover
    ``state.json`` must disappear before web-message forwarding can read
    it. A regression that leaves the old state in place would make
    ``read_bridge_state`` return the stale thread id here.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    bridge_dir = tmp_path / "bridge"
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path="ws://127.0.0.1:1234",
            thread_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )

    clear_bridge_state(bridge_dir)

    assert read_bridge_state(bridge_dir) is None


def test_preload_codex_thread_for_resume_resumes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Preloading uses Codex ``thread/resume`` before bridge state is exposed.

    This helper is the guard against a web turn racing ahead of the TUI
    and hitting ``turn/start`` on an app-server that has not loaded the
    persisted thread yet. If the request method or params regress, this
    test fails on the captured fake client request.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    fake_client = _FakeCodexAppServerClient()

    def fake_client_factory(*_args: Any, **_kwargs: Any) -> _FakeCodexAppServerClient:
        """
        Return the fake app-server client.

        :returns: Fake client.
        """
        return fake_client

    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient",
        fake_client_factory,
    )

    asyncio.run(
        codex_native_app_server.preload_codex_thread_for_resume(
            "ws://127.0.0.1:1234",
            "019e96aa-0be2-7343-8d3b-6f914d60936b",
        )
    )

    assert fake_client.connected is True
    assert fake_client.requests == [
        (
            "thread/resume",
            {
                "threadId": "019e96aa-0be2-7343-8d3b-6f914d60936b",
                "excludeTurns": True,
            },
        )
    ]
    assert fake_client.closed is True


def _started_event(turn_id: str) -> dict[str, Any]:
    """
    Build a Codex ``turn/started`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: App-server event payload.
    """
    return {"method": "turn/started", "params": {"turn": {"id": turn_id}}}


def _thread_started_event(thread_id: str) -> dict[str, Any]:
    """
    Build a Codex ``thread/started`` notification.

    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    return {"method": "thread/started", "params": {"thread": {"id": thread_id}}}


def _completed_event(turn_id: str | None, *, thread_id: str | None = None) -> dict[str, Any]:
    """
    Build a Codex ``turn/completed`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``, or ``None``
        when testing legacy or malformed terminal events.
    :param thread_id: Optional Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    params: dict[str, Any] = {}
    if turn_id is not None:
        params["turnId"] = turn_id
    if thread_id is not None:
        params["threadId"] = thread_id
    return {"method": "turn/completed", "params": params}


def _failed_event(turn_id: str | None, *, thread_id: str | None = None) -> dict[str, Any]:
    """
    Build a Codex ``turn/failed`` notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``, or ``None``
        when testing legacy or malformed terminal events.
    :param thread_id: Optional Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    params: dict[str, Any] = {}
    if turn_id is not None:
        params["turnId"] = turn_id
    if thread_id is not None:
        params["threadId"] = thread_id
    return {"method": "turn/failed", "params": params}


def _agent_message_event(
    turn_id: str,
    item_id: str,
    text: str,
    *,
    thread_id: str = "thread_123",
) -> dict[str, Any]:
    """
    Build a Codex completed assistant-message notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_123"``.
    :param text: Assistant text payload, e.g. ``"done"``.
    :param thread_id: Codex thread id, e.g. ``"thread_123"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": thread_id,
            "turnId": turn_id,
            "item": {
                "type": "agentMessage",
                "id": item_id,
                "text": text,
            },
        },
    }


def _agent_message_delta_event(turn_id: str, item_id: str, delta: object) -> dict[str, Any]:
    """
    Build a Codex assistant-message delta notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_123"``.
    :param delta: Delta payload to include in the event, e.g. ``"hi"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/agentMessage/delta",
        "params": {
            "threadId": "thread_123",
            "turnId": turn_id,
            "itemId": item_id,
            "delta": delta,
        },
    }


def _plan_delta_event(turn_id: str, item_id: str, delta: object) -> dict[str, Any]:
    """
    Build a Codex plan delta notification.

    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex plan item id, e.g. ``"item_plan"``.
    :param delta: Delta payload to include in the event, e.g.
        ``"1. Inspect"``.
    :returns: App-server event payload.
    """
    return {
        "method": "item/plan/delta",
        "params": {
            "threadId": "thread_123",
            "turnId": turn_id,
            "itemId": item_id,
            "delta": delta,
        },
    }


def _expected_delta_data(
    delta: str,
    turn_id: str,
    item_id: str,
    *,
    item_type: str = "agentMessage",
) -> dict[str, Any]:
    """
    Build the Omnigent event data expected for one Codex native text delta.

    :param delta: Coalesced text fragment, e.g. ``"hello"``.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :param item_id: Codex item id, e.g. ``"item_agent"``.
    :param item_type: Codex item type, e.g. ``"agentMessage"``.
    :returns: Expected ``external_output_text_delta`` data payload.
    """
    return {
        "delta": delta,
        "message_id": f"codex:thread_123:{turn_id}:{item_type}:{item_id}",
        "index": 0,
        "final": False,
    }


def _expected_status_data(status: str, turn_id: str) -> dict[str, Any]:
    """
    Build the Omnigent event data expected for one Codex native status edge.

    :param status: Omnigent session status, e.g. ``"running"``.
    :param turn_id: Codex turn id, e.g. ``"turn_123"``.
    :returns: Expected ``external_session_status`` data payload.
    """
    return {"status": status, "response_id": f"codex_{turn_id}"}


def _usage_event(input_tokens: int, context_window: int = 200_000) -> dict[str, Any]:
    """
    Build a Codex token-usage update event.

    :param input_tokens: Context token count, e.g. ``1234``.
    :param context_window: Context window size, e.g. ``200000``.
    :returns: App-server event payload.
    """
    return {
        "method": "thread/tokenUsage/updated",
        "params": {
            "threadId": "thread_123",
            "tokenUsage": {
                "total": {
                    "inputTokens": input_tokens,
                    "contextWindow": context_window,
                },
            },
        },
    }


def _usage_coalescer(
    client: httpx.AsyncClient,
    session_id: str = "conv_123",
) -> codex_native_forwarder._SessionUsageCoalescer:
    """
    Build the required Codex usage coalescer for direct handler tests.

    :param client: HTTP client used by the coalescer.
    :param session_id: Omnigent session id, e.g. ``"conv_123"``.
    :returns: Usage coalescer bound to ``session_id``.
    """
    return codex_native_forwarder._SessionUsageCoalescer(client, session_id)


def _elicitation_tracker() -> codex_native_forwarder._CodexElicitationTaskTracker:
    """
    Build the required Codex elicitation tracker for direct handler tests.

    :returns: Fresh tracker with no pending hook tasks.
    """
    return codex_native_forwarder._CodexElicitationTaskTracker()


def test_materialize_codex_agent_spec_uses_codex_native_harness(tmp_path: Path) -> None:
    """
    The generated wrapper spec is self-contained and selects the
    isolated ``codex-native`` harness rather than the existing
    non-TUI ``codex`` harness.
    """
    spec_path = codex_native._materialize_codex_agent_spec(
        tmp_path,
        model="gpt-test",
    )

    raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert raw["name"] == "codex-native-ui"
    # Exact executor block: the spec must NOT carry a profile key —
    # the --profile CLI flag was removed, so routing is resolved at
    # launch time (provider config / global auth / ambient detection).
    assert raw["executor"] == {
        "harness": "codex-native",
        "model": "gpt-test",
    }


def test_materialized_codex_agent_spec_loads_as_valid_omnigent_yaml(
    tmp_path: Path,
) -> None:
    """
    The generated wrapper spec passes Omnigent YAML validation.

    This guards the session-create path, which registers the generated
    spec bundle and fails before Codex starts if ``codex-native`` is not
    accepted by the spec adapter.
    """
    spec_path = codex_native._materialize_codex_agent_spec(
        tmp_path,
        model="gpt-test",
    )

    spec = load(spec_path)

    assert spec.executor.config["harness"] == "codex-native"
    # The native wrapper opts into the spawn-write surface so the
    # wrapped codex can author agent configs and launch them as child
    # sessions; the bridge relay derives its tool set from this spec
    # via ToolManager, so a dropped flag silently removes
    # sys_session_create/send/close from the native CLI.
    assert spec.spawn is True
    # The native wrapper declares a default shell terminal so the
    # relay advertises the sys_terminal_* family to the wrapped
    # codex (the relay gate is a non-empty ``terminals:`` block on
    # this spec); a dropped block silently removes the terminal
    # tools from the native CLI.
    assert spec.terminals is not None
    assert spec.terminals["shell"].command == "bash"


@pytest.mark.parametrize(
    ("codex_args", "thread_id", "remote_url", "expected"),
    [
        # Fresh thread over a Unix socket (local ``omnigent codex``
        # cold start): no ``resume``/thread id, transport passed verbatim.
        (
            (),
            None,
            "unix:///tmp/app-server.sock",
            ["--remote", "unix:///tmp/app-server.sock"],
        ),
        # Resume an existing thread over a Unix socket (local reattach).
        (
            (),
            "thread_local",
            "unix:///tmp/app-server.sock",
            ["resume", "--remote", "unix:///tmp/app-server.sock", "thread_local"],
        ),
        # Fresh thread over a loopback ws endpoint.
        (
            (),
            None,
            "ws://127.0.0.1:9876",
            ["--remote", "ws://127.0.0.1:9876"],
        ),
        # Resume an existing thread over a loopback ws endpoint: the
        # host-spawned runner path. The app-server listens on ws:// there
        # (the codex CLI lacked unix:// listen support), so
        # the auto-created terminal must attach over that same ws URL. A
        # regression hardcoding ``unix://`` would break exactly this case.
        (
            (),
            "thread_host",
            "ws://127.0.0.1:9876",
            ["resume", "--remote", "ws://127.0.0.1:9876", "thread_host"],
        ),
        # Leading codex args are preserved ahead of the attach flags.
        (
            ("--model", "gpt-5.4-mini"),
            "thread_x",
            "ws://127.0.0.1:9876",
            [
                "--model",
                "gpt-5.4-mini",
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_x",
            ],
        ),
    ],
)
def test_build_codex_remote_args_passes_transport_verbatim(
    codex_args: tuple[str, ...],
    thread_id: str | None,
    remote_url: str,
    expected: list[str],
) -> None:
    """
    ``build_codex_remote_args`` emits the TUI ``--remote`` attach argv
    for both transports and both thread states.

    The transport URL is passed through verbatim so one builder serves
    both the local Unix-socket path and the host-spawned ``ws://`` path,
    and ``resume <thread_id>`` is appended iff a thread id is supplied.
    If this regressed to a hardcoded ``unix://`` prefix, the ws cases
    would fail and the host-spawned Codex terminal could not reach its
    TCP app-server (no terminal would render in the web UI).
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=codex_args,
            thread_id=thread_id,
            remote_url=remote_url,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("thread_id", "expected"),
    [
        # Fresh thread: -c overrides precede the bare --remote attach.
        (
            None,
            [
                "-c",
                'model="databricks-gpt-5-5"',
                "-c",
                'model_provider="omnigent_databricks"',
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Resume: -c overrides are global flags and MUST precede the
        # ``resume`` subcommand (codex rejects globals placed after it).
        (
            "thread_host",
            [
                "-c",
                'model="databricks-gpt-5-5"',
                "-c",
                'model_provider="omnigent_databricks"',
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_host",
            ],
        ),
    ],
)
def test_build_codex_remote_args_emits_config_overrides_before_subcommand(
    thread_id: str | None,
    expected: list[str],
) -> None:
    """
    ``build_codex_remote_args`` emits each ``config_overrides`` entry as a
    ``-c <value>`` global flag ahead of the attach flags.

    The ``--remote`` TUI is a separate process that does not inherit the
    app-server's ``-c`` flags; without these the TUI falls back to the
    OpenAI built-in provider (``requires_openai_auth = true``), renders
    the first-run login onboarding screen, and never creates a thread —
    so a host-spawned session hangs in ``running`` with no response.
    Asserting the exact argv (not just membership) guards two things at
    once: that the overrides are forwarded at all, and that they land
    *before* the ``resume`` subcommand — codex treats ``-c`` as a global
    option and rejects it when placed after a subcommand, which would
    abort TUI startup and reintroduce the hang.
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=(),
            thread_id=thread_id,
            remote_url="ws://127.0.0.1:9876",
            config_overrides=(
                'model="databricks-gpt-5-5"',
                'model_provider="omnigent_databricks"',
            ),
        )
        == expected
    )


@pytest.mark.parametrize(
    ("codex_args", "expected"),
    [
        # ``--flag value`` pair: both dropped.
        (("--sandbox", "read-only"), []),
        (("--ask-for-approval", "on-request"), []),
        # Option-adjacent: the next token is ANOTHER flag, not this flag's
        # value, so it must survive (the over-match bug dropped --model).
        (("--sandbox", "--model", "gpt"), ["--model", "gpt"]),
        # ``--flag=value`` single token: dropped whole, consumes nothing after.
        (("--ask-for-approval=on-failure",), []),
        (("--sandbox=read-only", "--model", "gpt"), ["--model", "gpt"]),
        # Short aliases: ``-a`` (== --ask-for-approval) triggers the SAME codex
        # startup abort as the long form, so it must be stripped too; ``-s``
        # (== --sandbox) is harmless but dropped for consistency. Both spellings
        # (space-separated and ``=value``-joined) are handled.
        (("-a", "never"), []),
        (("-a=never",), []),
        (("-s", "read-only"), []),
        (("-s=read-only", "--model", "gpt"), ["--model", "gpt"]),
        # Short alias option-adjacent to another flag: the next flag survives.
        (("-a", "--model", "gpt"), ["--model", "gpt"]),
        # Trailing flag at end-of-list: dropped cleanly, no value to consume.
        (("--model", "gpt", "--sandbox"), ["--model", "gpt"]),
        # Unrelated arg next to a stripped pair is preserved.
        (
            ("--model", "gpt", "--sandbox", "read-only", "--cwd", "/x"),
            ["--model", "gpt", "--cwd", "/x"],
        ),
        # A pre-existing bypass flag is de-duped (the caller re-adds one copy).
        (("--dangerously-bypass-approvals-and-sandbox", "--model", "gpt"), ["--model", "gpt"]),
        # No conflicting flags: everything passes through untouched.
        (("--model", "gpt-5.4-mini"), ["--model", "gpt-5.4-mini"]),
    ],
)
def test_strip_approval_sandbox_flags_only_consumes_real_values(
    codex_args: tuple[str, ...],
    expected: list[str],
) -> None:
    """
    ``_strip_approval_sandbox_flags`` drops the conflicting flags without
    over-matching the token that follows them.

    A ``--sandbox`` / ``--ask-for-approval`` flag consumes the next token as
    its value ONLY when that token is a real value (does not start with
    ``-``); a following flag or end-of-list consumes nothing, so unrelated
    args like ``--model gpt`` are never swallowed. The ``--flag=value``
    single-token spelling is dropped whole.
    """
    assert codex_native_app_server._strip_approval_sandbox_flags(codex_args) == expected


def test_build_codex_remote_args_default_keeps_approval_flags_no_bypass() -> None:
    """
    Default (``bypass_sandbox=False``) emits NO bypass flag and preserves the
    approval/sandbox flags the approval-mode presets pass through.

    The web "Full access" / "Read only" presets are sent as
    ``--sandbox`` / ``--ask-for-approval`` pairs inside ``codex_args``. With
    bypass off those must reach the TUI verbatim and the dangerous bypass
    flag must never appear — a regression here would either drop a user's
    chosen approval preset or silently escalate to full bypass.
    """
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=("--sandbox", "read-only", "--ask-for-approval", "on-request"),
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
    )

    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert args == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "on-request",
        "--remote",
        "ws://127.0.0.1:9876",
    ]


@pytest.mark.parametrize(
    ("codex_args", "thread_id", "expected"),
    [
        # Fresh thread, no conflicting flags: a single bypass flag is prepended.
        (
            (),
            None,
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Conflicting approval-preset flags are stripped (flag + its value),
        # unrelated args (model) survive, and the bypass flag is added once.
        # codex aborts if the bypass flag is combined with --sandbox /
        # --ask-for-approval, so leaving them in would break TUI startup.
        (
            ("--sandbox", "danger-full-access", "--ask-for-approval", "never", "--model", "gpt"),
            None,
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt",
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Resume path: the bypass flag is a global flag and MUST precede the
        # ``resume`` subcommand, and a pre-existing bypass flag is de-duped.
        (
            ("--dangerously-bypass-approvals-and-sandbox", "--sandbox", "read-only"),
            "thread_x",
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_x",
            ],
        ),
    ],
)
def test_build_codex_remote_args_bypass_emits_flag_and_strips_conflicts(
    codex_args: tuple[str, ...],
    thread_id: str | None,
    expected: list[str],
) -> None:
    """
    ``bypass_sandbox=True`` emits one ``--dangerously-bypass-approvals-and-
    sandbox`` and strips the conflicting ``--sandbox`` / ``--ask-for-approval``
    pairs.

    See :func:`omnigent.codex_native_app_server._strip_approval_sandbox_flags`.
    Asserting the exact argv guards three things: the bypass flag is present
    exactly once, the conflicting flag pairs are removed (with their values),
    and the bypass flag lands before any ``resume`` subcommand (codex rejects
    a global flag placed after a subcommand).
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=codex_args,
            thread_id=thread_id,
            remote_url="ws://127.0.0.1:9876",
            bypass_sandbox=True,
        )
        == expected
    )


def test_codex_app_server_client_uses_codex_remote_handshake(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The Python client matches Codex's Unix-socket websocket transport
    and completes the initialize/initialized handshake.
    """
    fake_websocket = _FakeCodexWebSocket()
    captured_kwargs: dict[str, Any] = {}

    async def fake_unix_connect(**kwargs: Any) -> _FakeCodexWebSocket:
        """
        Capture the websocket connection arguments.

        :param kwargs: ``websockets.unix_connect`` keyword arguments.
        :returns: Fake websocket.
        """
        captured_kwargs.update(kwargs)
        return fake_websocket

    monkeypatch.setattr(
        codex_native_app_server.websockets,
        "unix_connect",
        fake_unix_connect,
    )

    async def run() -> None:
        """
        Open and close one Codex app-server client.

        :returns: None.
        """
        client = codex_native_app_server.CodexAppServerClient(
            tmp_path / "app-server.sock",
            client_name="test-client",
        )
        await client.connect()
        await client.close()

    asyncio.run(run())

    assert captured_kwargs == {
        "path": str(tmp_path / "app-server.sock"),
        "uri": "ws://localhost/rpc",
        "max_size": 128 << 20,
        "compression": None,
    }
    assert [json.loads(payload) for payload in fake_websocket.sent] == [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "test-client", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        },
        {"method": "initialized"},
    ]
    assert fake_websocket.closed


def test_codex_app_server_client_responds_to_server_requests(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The Codex websocket client can answer server-to-client requests.

    Native Codex elicitations arrive as JSON-RPC requests from the
    app-server to the Omnigent client. After AP/web resolves the
    prompt, the forwarder must send a result envelope with the same
    request id; otherwise Codex never observes the answer.
    """
    fake_websocket = _FakeCodexWebSocket()

    async def fake_unix_connect(**_kwargs: Any) -> _FakeCodexWebSocket:
        """
        Return the fake websocket.

        :returns: Fake websocket.
        """
        return fake_websocket

    monkeypatch.setattr(
        codex_native_app_server.websockets,
        "unix_connect",
        fake_unix_connect,
    )

    async def run() -> None:
        """
        Connect, send one server-request response, and close.

        :returns: None.
        """
        client = codex_native_app_server.CodexAppServerClient(
            tmp_path / "app-server.sock",
            client_name="test-client",
        )
        await client.connect()
        await client.respond("req_7", {"answers": {"framework": {"answers": ["React"]}}})
        await client.close()

    asyncio.run(run())

    assert json.loads(fake_websocket.sent[-1]) == {
        "id": "req_7",
        "result": {"answers": {"framework": {"answers": ["React"]}}},
    }
    assert fake_websocket.closed


def test_thread_id_from_started_event_ignores_unrelated_events() -> None:
    """
    Thread discovery only accepts well-formed Codex ``thread/started``
    notifications.
    """
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "remoteControl/status/changed"}
        )
        is None
    )
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "thread/started", "params": {"thread": {}}}
        )
        is None
    )
    assert (
        codex_native_forwarder._thread_id_from_started_event(
            {"method": "thread/started", "params": {"thread": {"id": "thread_123"}}}
        )
        == "thread_123"
    )


def test_wait_for_thread_started_uses_tui_created_thread() -> None:
    """
    Fresh Codex sessions let the TUI create the remote app-server
    thread, then discover that id from the broadcast ``thread/started``
    notification.
    """
    fake_client = _FakeCodexAppServerClient(
        events=[
            {"method": "remoteControl/status/changed", "params": {"status": "disabled"}},
            {"method": "thread/started", "params": {"thread": {"id": "thread_123"}}},
        ]
    )

    thread_id = asyncio.run(codex_native._wait_for_thread_started(fake_client))  # type: ignore[arg-type]

    assert thread_id == "thread_123"
    assert fake_client.requests == []


def test_wait_for_thread_started_fails_when_stream_ends() -> None:
    """
    A Codex TUI that exits before creating a thread fails loudly instead
    of leaving the Omnigent session without a bridge state.
    """
    fake_client = _FakeCodexAppServerClient(events=[])

    with pytest.raises(click.ClickException, match="event stream ended"):
        asyncio.run(codex_native._wait_for_thread_started(fake_client))  # type: ignore[arg-type]


def test_wait_for_thread_started_times_out_when_no_thread_event() -> None:
    """
    A Codex TUI that connects but never emits ``thread/started`` makes
    discovery time out rather than hang forever.

    The host-spawned runner runs ``wait_for_thread_started`` in a background
    task; without the timeout a TUI that starts its remote connection but
    never creates a thread would wedge that task (and leak the listener)
    indefinitely. A regression removing the ``asyncio.timeout`` guard would
    hang this test instead of raising.
    """

    class _NeverEmitsClient:
        """App-server client whose event stream blocks without ever yielding."""

        async def iter_events(self) -> Any:
            await asyncio.sleep(3600)
            yield {}  # pragma: no cover - unreachable; the await blocks first

    with pytest.raises(TimeoutError):
        asyncio.run(
            codex_native_forwarder.wait_for_thread_started(
                _NeverEmitsClient(),  # type: ignore[arg-type]
                timeout=0.05,
            )
        )


def test_supervise_forwarder_subscribes_existing_client_after_thread_discovery(
    tmp_path: Path,
) -> None:
    """
    Fresh Codex sessions pass the listener used to discover
    ``thread/started``. Once the thread id is known, the forwarder
    subscribes that connection so TUI-originated turn/item events are
    mirrored into the web session.
    """
    fake_client = _FakeCodexAppServerClient()

    async def run() -> None:
        """
        Run the forwarder against an empty event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            client=fake_client,  # type: ignore[arg-type]
        )

    asyncio.run(run())

    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    ]
    assert fake_client.closed


def test_supervise_forwarder_resumes_when_it_opens_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume paths open a new app-server client and subscribe via
    ``thread/resume``.
    """
    fake_client = _FakeCodexAppServerClient()

    def fake_client_factory(*_args: Any, **_kwargs: Any) -> _FakeCodexAppServerClient:
        """
        Return the fake app-server client.

        :returns: Fake client.
        """
        return fake_client

    # Patch at the source: the forwarder builds its fallback client via
    # client_for_transport, which constructs the app_server module's class.
    monkeypatch.setattr(
        "omnigent.codex_native_app_server.CodexAppServerClient", fake_client_factory
    )

    async def run() -> None:
        """
        Run the forwarder against an empty event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
        )

    asyncio.run(run())

    assert fake_client.connected
    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True})
    ]
    assert fake_client.closed


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # Rollout file missing — the first transient fresh-thread state.
        ("{'code': -32600, 'message': 'no rollout found for thread id thread_123'}", True),
        # Rollout file present but EMPTY — the second transient state the
        # fresh host-spawned TUI exposes. Previously treated as fatal, which
        # made the forwarder give up subscribing and stop syncing chat.
        (
            "{'code': -32603, 'message': 'failed to read thread: thread-store "
            "internal error: failed to read thread /x/rollout.jsonl: rollout at "
            "/x/rollout.jsonl is empty'}",
            True,
        ),
        # A real app-server error must stay fatal (not retried forever).
        ("{'code': -32000, 'message': 'permission denied'}", False),
    ],
)
def test_is_thread_not_ready_error_matches_no_rollout_and_empty_rollout(
    message: str, expected: bool
) -> None:
    """
    ``_is_thread_not_ready_error`` treats BOTH fresh-thread not-ready states
    (missing rollout, present-but-empty rollout) as retryable, and leaves
    unrelated errors fatal. If the empty-rollout case regressed to fatal, the
    host-spawned forwarder would give up subscribing and chat would not sync.
    """
    assert codex_native_forwarder._is_thread_not_ready_error(RuntimeError(message)) is expected


def test_subscribe_until_ready_retries_no_rollout_and_replays_messages(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh TUI threads can reject ``thread/resume`` until the first
    rollout exists; the forwarder retries and backfills resume items.
    """
    fake_client = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "turns": [
                        {
                            "id": "turn_123",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "item_user",
                                    "content": [{"type": "text", "text": "first"}],
                                },
                                {
                                    "type": "agentMessage",
                                    "id": "item_agent",
                                    "text": "reply",
                                },
                            ],
                        }
                    ]
                }
            }
        },
        error=RuntimeError(
            "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
        ),
    )
    calls = 0

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        nonlocal calls
        fake_client.requests.append((method, params))
        calls += 1
        if calls == 1:
            raise RuntimeError(
                "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
            )
        return fake_client.response

    async def fake_sleep(_delay: float) -> None:
        return None

    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    monkeypatch.setattr(fake_client, "request", fake_request)
    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                thread_id="thread_123",
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
            )

    asyncio.run(run())

    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_123", "excludeTurns": True}),
        ("thread/resume", {"threadId": "thread_123"}),
    ]
    assert [payload["data"]["item_data"]["role"] for payload in posted] == [
        "user",
        "assistant",
    ]


def test_subscribe_until_ready_replays_completed_turn_status(
    tmp_path: Path,
) -> None:
    """
    Resume replay closes a completed turn when live terminal events were missed.

    Host-spawned codex-native suppresses the runner's injection-task ``idle``
    edge, so a reconnect that misses both ``turn/started`` and
    ``turn/completed`` must recover the terminal status from explicit resume
    turn state instead of leaving the Omnigent session running forever.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    fake_client = _FakeCodexAppServerClient(
        response={
            "result": {
                "thread": {
                    "id": "thread_123",
                    "turns": [
                        {
                            "id": "turn_123",
                            "status": "completed",
                            "items": [
                                {
                                    "type": "userMessage",
                                    "id": "item_user",
                                    "content": [{"type": "text", "text": "first"}],
                                },
                                {
                                    "type": "agentMessage",
                                    "id": "item_agent",
                                    "text": "reply",
                                },
                            ],
                        }
                    ],
                }
            }
        }
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Subscribe and replay a completed turn.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                thread_id="thread_123",
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
            )

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_conversation_item",
        "external_conversation_item",
        "external_session_status",
    ]
    assert [payload["data"]["item_data"]["role"] for payload in posted[:2]] == [
        "user",
        "assistant",
    ]
    assert posted[2]["data"] == _expected_status_data("idle", "turn_123")
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.parametrize(
    "event,expected",
    [
        ({"method": "turn/started", "params": {}}, True),
        ({"method": "item/agentMessage/delta", "params": {}}, True),
        (
            {"method": "thread/status/changed", "params": {"status": {"type": "active"}}},
            True,
        ),
        # Idle status / fresh-thread announce / control noise do NOT imply a
        # rollout exists yet — they must NOT release the parked subscribe.
        (
            {"method": "thread/status/changed", "params": {"status": {"type": "idle"}}},
            False,
        ),
        ({"method": "thread/started", "params": {"thread": {"id": "t1"}}}, False),
        ({"method": "remoteControl/status/changed", "params": {"status": "disabled"}}, False),
        ({"result": {}, "id": 1}, False),
    ],
)
def test_event_indicates_thread_active(event: dict[str, Any], expected: bool) -> None:
    """Only turn/item/active-status events imply the thread's rollout exists.

    This predicate gates releasing the deferred subscription. A false
    positive (e.g. on the ``idle`` status codex emits at thread creation)
    would resume too early and reintroduce the no-rollout retry churn; a
    false negative would leave the forwarder parked through a real turn.
    """
    assert codex_native_forwarder._event_indicates_thread_active(event) is expected


def test_subscribe_until_ready_parks_until_signal_then_resumes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fresh, not-ready thread parks on the signal instead of polling.

    Reproduces the fresh-session path: ``thread/resume`` fails with ``no
    rollout found`` until the thread becomes active. With a ``ready_signal``
    provided, the subscribe must NOT busy-poll — it waits for the signal
    (set by the caller when the live stream shows the thread active), then
    retries and succeeds. ``_sleep`` is stubbed to raise so a regression
    back to blind-polling turns the task red instead of silently hammering
    the app-server.
    """
    fake_client = _FakeCodexAppServerClient(response={"result": {"thread": {"turns": []}}})
    not_ready = RuntimeError(
        "{'code': -32600, 'message': 'no rollout found for thread id thread_123'}"
    )

    async def run() -> int:
        ready = asyncio.Event()
        attempts = 0

        async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
            nonlocal attempts
            attempts += 1
            # Not-ready until the thread is "active" (signal set) — mirrors
            # codex deferring rollout materialization until the first turn.
            if not ready.is_set():
                raise not_ready
            return fake_client.response

        async def fake_sleep(_delay: float) -> None:
            raise AssertionError(
                "subscribe must park on ready_signal for a not-ready thread, not poll"
            )

        monkeypatch.setattr(fake_client, "request", fake_request)
        monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(lambda _req: httpx.Response(202, json={})),
        ) as client:
            task = asyncio.create_task(
                codex_native_forwarder._subscribe_until_ready(
                    fake_client,  # type: ignore[arg-type]
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    thread_id="thread_123",
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    ready_signal=ready,
                )
            )
            # Let the task make its first resume attempt and park. If it had
            # polled instead, fake_sleep would have raised and the task would
            # be done with an exception — so an un-done task proves it parked.
            for _ in range(8):
                await asyncio.sleep(0)
            assert not task.done(), "subscribe should still be parked on the signal"
            assert attempts >= 1

            # Caller observed the thread go active → release the wait.
            ready.set()
            await task
            return attempts

    attempts = asyncio.run(run())
    # ≥2: the initial not-ready attempt plus at least one post-signal retry
    # that succeeds. If it were still polling we'd never reach here (fake_sleep
    # raises); if it never retried after the signal it would hang on await task.
    assert attempts >= 2, attempts


def test_forwarder_ignores_thread_started_for_current_codex_thread(tmp_path: Path) -> None:
    """
    A duplicate ``thread/started`` notification does not rotate Omnigent sessions.

    Codex can broadcast the current thread after the forwarder has
    already bound it. This fails if the rotation detector treats every
    ``thread/started`` as a clear-session boundary.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_old",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_old",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )

    async def run() -> bool:
        """
        Drive the duplicate notification through the rotation detector.

        :returns: Whether a rotation occurred.
        """
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    client,
                    "conv_old",
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    client,
                    "conv_old",
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            return await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url=str(tmp_path / "app-server.sock"),
                event=_thread_started_event("thread_old"),
            )

    assert asyncio.run(run()) is False
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.session_id == "conv_old"
    assert state.thread_id == "thread_old"


def test_forwarder_rotates_session_on_new_codex_thread_and_posts_to_new_session(
    tmp_path: Path,
) -> None:
    """
    Native Codex thread switches create a replacement Omnigent session.

    This is the ``/clear`` regression shape: Codex keeps the terminal
    alive but starts a new app-server thread. The forwarder must move
    terminal ownership, update bridge state, resubscribe to the new
    thread, and send subsequent status/history events to the new AP
    session.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_old",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_old",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )
    fake_client = _FakeCodexAppServerClient()
    requests: list[tuple[str, str, dict[str, Any] | None]] = []
    posted_events: list[tuple[str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve Omnigent calls made during Codex session rotation.

        :param request: HTTP request from the forwarder.
        :returns: Fake Omnigent response.
        """
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_codex",
                    "runner_id": "runner_123",
                    "labels": {
                        "omnigent.wrapper": "codex-native-ui",
                        "omnigent.codex_native.bridge_id": "bridge_shared",
                    },
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path in {
            "/v1/sessions/conv_new",
            "/v1/sessions/conv_old",
        }:
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        if request.method == "POST" and request.url.path == (
            "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer"
        ):
            return httpx.Response(200, json={"id": "terminal_codex_main"})
        if request.method == "POST" and request.url.path == "/v1/sessions/conv_new/events":
            assert isinstance(body, dict)
            posted_events.append(("conv_new", body))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            500,
            json={"error": f"unexpected {request.method} {request.url.path}"},
        )

    async def run() -> None:
        """
        Drive the real rotation and event handlers.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as ap_client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    ap_client,
                    "conv_old",
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    ap_client,
                    "conv_old",
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
            )
            rotated = await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=ap_client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url="ws://127.0.0.1:9876",
                event=_thread_started_event("thread_new"),
            )
            assert rotated
            await codex_native_forwarder._subscribe_until_ready(
                fake_client,  # type: ignore[arg-type]
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                thread_id=target.thread_id,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
            )
            await codex_native_forwarder._handle_event(
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                event=_started_event("turn_new"),
                delta_coalescer=target.delta_coalescer,
                expected_thread_id=target.thread_id,
            )
            await codex_native_forwarder._handle_event(
                ap_client,
                session_id=target.session_id,
                bridge_dir=tmp_path,
                usage_coalescer=target.usage_coalescer,
                elicitation_tracker=target.elicitation_tracker,
                event=_agent_message_event(
                    "turn_new",
                    "item_new",
                    "after clear",
                    thread_id="thread_new",
                ),
                delta_coalescer=target.delta_coalescer,
                expected_thread_id=target.thread_id,
            )
            await target.delta_coalescer.close()

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.session_id == "conv_new"
    assert state.thread_id == "thread_new"
    # Rotation must re-persist the ws:// transport it was given, not a
    # clobbered unix path — otherwise the executor would dial a dead unix
    # socket after /clear and steering/interrupt would silently fail.
    assert state.socket_path == "ws://127.0.0.1:9876"
    assert fake_client.requests == [
        ("thread/resume", {"threadId": "thread_old", "excludeTurns": True}),
        ("thread/resume", {"threadId": "thread_new", "excludeTurns": True}),
    ]
    assert (
        "POST",
        "/v1/sessions",
        {
            "agent_id": "ag_codex",
            "labels": {
                "omnigent.wrapper": "codex-native-ui",
                "omnigent.codex_native.bridge_id": "bridge_shared",
            },
        },
    ) in requests
    assert (
        "POST",
        "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer",
        {"target_session_id": "conv_new"},
    ) in requests
    assert [
        payload["data"]["status"]
        for _, payload in posted_events
        if payload["type"] == "external_session_status"
    ] == ["running"]
    assert [
        payload["data"]["item_data"]["content"][0]["text"]
        for _, payload in posted_events
        if payload["type"] == "external_conversation_item"
    ] == ["after clear"]


def test_forwarder_rotation_failure_preserves_old_target(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Failed Codex thread rotation leaves the old forwarding target usable.

    If Omnigent rejects replacement-session creation, the forwarder logs the
    event-handler failure and continues. The old target must remain
    intact; closing its coalescer before the Omnigent move succeeds would
    leave later old-thread streaming in a half-rotated state.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """

    class _FakeCoalescer:
        """
        Test coalescer that records lifecycle calls.

        :param session_id: Omnigent session id represented by this fake,
            e.g. ``"conv_old"``.
        """

        def __init__(self, session_id: str) -> None:
            """
            Initialize the fake coalescer.

            :param session_id: Omnigent session id represented by this fake,
                e.g. ``"conv_old"``.
            :returns: None.
            """
            self.session_id = session_id
            self.flushed = False
            self.closed = False

        async def flush(self) -> None:
            """
            Record a flush request.

            :returns: None.
            """
            self.flushed = True

        async def close(self) -> None:
            """
            Record a close request.

            :returns: None.
            """
            self.closed = True

    async def fail_create_thread_replacement_session(**_kwargs: object) -> str:
        """
        Simulate Omnigent rejecting the replacement-session operation.

        :returns: Never returns successfully.
        :raises RuntimeError: Always raised to model Omnigent failure.
        """
        raise RuntimeError("replacement failed")

    fake_delta_coalescer = _FakeCoalescer("conv_old")
    fake_usage_coalescer = _FakeCoalescer("conv_old")
    monkeypatch.setattr(
        codex_native_forwarder,
        "_create_thread_replacement_session",
        fail_create_thread_replacement_session,
    )

    async def run() -> codex_native_forwarder._ForwarderTarget:
        """
        Drive a failed new-thread rotation through the real detector.

        :returns: Forwarder target after the failed rotation attempt.
        """
        async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_old",
                thread_id="thread_old",
                delta_coalescer=fake_delta_coalescer,  # type: ignore[arg-type]
                usage_coalescer=fake_usage_coalescer,  # type: ignore[arg-type]
                elicitation_tracker=_elicitation_tracker(),
            )
            with pytest.raises(RuntimeError, match="replacement failed"):
                await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                    ap_client=client,
                    target=target,
                    bridge_dir=tmp_path,
                    app_server_url=str(tmp_path / "app-server.sock"),
                    event=_thread_started_event("thread_new"),
                )
            return target

    target = asyncio.run(run())

    assert target.session_id == "conv_old"
    assert target.thread_id == "thread_old"
    assert target.delta_coalescer is fake_delta_coalescer
    assert target.usage_coalescer is fake_usage_coalescer
    assert fake_delta_coalescer.flushed
    assert fake_usage_coalescer.flushed
    assert not fake_delta_coalescer.closed
    assert not fake_usage_coalescer.closed


@pytest.mark.parametrize(
    ("initial_active_turn_id", "events", "expected_active_turn_id", "expected_statuses"),
    [
        (None, [_started_event("turn_old")], "turn_old", ["running"]),
        (
            None,
            [_started_event("turn_old"), _completed_event("turn_old")],
            None,
            ["running", "idle"],
        ),
        (
            None,
            [_started_event("turn_old"), _failed_event("turn_old")],
            None,
            ["running", "failed"],
        ),
        (
            None,
            [_completed_event("turn_early", thread_id="thread_123")],
            None,
            ["idle"],
        ),
        (
            None,
            [_failed_event("turn_early", thread_id="thread_123")],
            None,
            ["failed"],
        ),
        (
            None,
            [_completed_event("turn_early", thread_id="thread_other")],
            None,
            [],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _completed_event("turn_old")],
            "turn_new",
            ["running", "running"],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _failed_event("turn_old")],
            "turn_new",
            ["running", "running"],
        ),
        (
            None,
            [_started_event("turn_old"), _started_event("turn_new"), _completed_event("turn_new")],
            None,
            ["running", "running", "idle"],
        ),
        # A no-id terminal event while turn_old is live is ambiguous: it is
        # ignored, so the active turn stays and no premature idle is posted
        # (the bug that hid the "working" spinner mid-turn).
        (
            None,
            [_started_event("turn_old"), _completed_event(None)],
            "turn_old",
            ["running"],
        ),
        ("turn_new", [_completed_event("turn_old")], "turn_new", []),
        ("turn_new", [_failed_event("turn_old")], "turn_new", []),
        ("turn_new", [_completed_event("turn_new")], None, ["idle"]),
    ],
)
def test_forwarder_tracks_active_turn_across_terminal_event_sequences(
    initial_active_turn_id: str | None,
    events: list[dict[str, Any]],
    expected_active_turn_id: str | None,
    expected_statuses: list[str],
    tmp_path: Path,
) -> None:
    """
    Forwarder turn lifecycle handling is ordered by terminal turn id.

    Rapid web sends can update the active Codex turn before an older
    terminal notification arrives. Matching terminal notifications must
    mark the session idle or failed, while stale terminal notifications
    must leave the newer active turn and session status untouched.

    :param initial_active_turn_id: Active turn id before replaying the
        sequence, e.g. ``"turn_new"``.
    :param events: Codex app-server notifications to replay.
    :param expected_active_turn_id: Expected bridge state after the
        sequence.
    :param expected_statuses: Expected external session status posts.
    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=initial_active_turn_id,
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay the Codex event sequence through the real handler.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in events:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                )

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == expected_active_turn_id
    assert [
        payload["data"]["status"]
        for payload in posted
        if payload["type"] == "external_session_status"
    ] == expected_statuses


def test_forwarder_posts_agent_item_after_stale_terminal_event(tmp_path: Path) -> None:
    """
    Stale turn completion does not block newer Codex response mirroring.

    This is the rapid-send failure shape: the web inject path has
    already advanced to ``turn_new`` when a delayed terminal event for
    ``turn_old`` arrives. The stale terminal event must not mark the
    session idle or prevent the newer assistant item from syncing.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay the stale-terminal/new-item sequence.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                _started_event("turn_old"),
                _started_event("turn_new"),
                _completed_event("turn_old"),
                _agent_message_event("turn_new", "item_agent", "new response"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                )

    asyncio.run(run())

    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_new"
    assert [
        payload["data"]["status"]
        for payload in posted
        if payload["type"] == "external_session_status"
    ] == ["running", "running"]
    assert [
        payload["data"]["item_data"]["content"][0]["text"]
        for payload in posted
        if payload["type"] == "external_conversation_item"
    ] == ["new response"]


def test_forwarder_posts_active_codex_agent_message_delta(tmp_path: Path) -> None:
    """
    Codex assistant deltas are forwarded as transient Omnigent text deltas.

    Breaking the ``item/agentMessage/delta`` branch would leave the
    web stream silent until Codex posts its completed ``agentMessage``
    item, so this asserts on the exact Omnigent event envelope.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay one active assistant-delta event.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_agent_message_delta_event("turn_123", "item_agent", "hel"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("hel", "turn_123", "item_agent"),
        }
    ]


def test_forwarder_persists_interrupted_codex_partial_agent_message(tmp_path: Path) -> None:
    """
    Interrupted Codex turns persist the visible partial assistant text.

    Codex interruption terminates the turn with ``turn/completed`` status
    ``interrupted`` and may never emit a completed ``agentMessage`` item.
    Without this fallback, Omnigent Web shows the streamed text live but loses it
    from durable history as soon as the turn ends.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay assistant deltas followed by an interrupted terminal boundary.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_123", "item_agent", "partial "),
                _agent_message_delta_event("turn_123", "item_agent", "answer"),
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {"id": "turn_123", "status": "interrupted"},
                    },
                },
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    delta_coalescer=coalescer,
                    forwarder_state=forwarder_state,
                )
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("partial answer", "turn_123", "item_agent"),
        },
        {
            "type": "external_session_interrupted",
            "data": {"response_id": "codex_turn_123"},
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "interrupted": True,
                    "content": [{"type": "output_text", "text": "partial answer"}],
                },
                "response_id": "codex_turn_123",
            },
        },
        {
            "type": "external_session_status",
            "data": _expected_status_data("idle", "turn_123"),
        },
    ]
    assert forwarder_state.partial_text_by_turn == {}
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


def test_forwarder_posts_active_codex_plan_delta(tmp_path: Path) -> None:
    """
    Codex plan deltas are forwarded as transient Omnigent text deltas.

    Plan mode uses ``item/plan/delta`` while rendering the visible
    plan. If this branch is missing, Omnigent web stays blank even though the
    Codex TUI is already showing the plan.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay one active plan-delta event.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_plan_delta_event("turn_123", "item_plan", "1. Inspect"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data(
                "1. Inspect",
                "turn_123",
                "item_plan",
                item_type="plan",
            ),
        }
    ]


def test_forwarder_recovers_active_turn_from_codex_plan_delta(tmp_path: Path) -> None:
    """
    Plan deltas mark the session running when ``turn/started`` was missed.

    Fresh TUI turns can begin before the forwarder finishes subscribing
    via ``thread/resume``. The delta itself carries both ``threadId``
    and ``turnId``; when no active turn is recorded yet and the thread
    matches bridge state, the forwarder adopts that turn, publishes the
    missing ``running`` status edge, and streams the plan instead of
    dropping the first visible content.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay a plan delta before any observed turn-start edge.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_plan_delta_event("turn_early", "item_plan", "Draft plan"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_session_status",
            "data": _expected_status_data("running", "turn_early"),
        },
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data(
                "Draft plan",
                "turn_early",
                "item_plan",
                item_type="plan",
            ),
        },
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_early"


def test_forwarder_recovers_active_turn_from_codex_agent_message_delta(tmp_path: Path) -> None:
    """
    Assistant deltas mark the session running when ``turn/started`` was missed.

    Some Codex turns begin before the forwarder has subscribed to the
    app-server event stream. If the first observed event is already an
    ``item/agentMessage/delta`` for the current thread, the forwarder must
    adopt that turn and publish ``external_session_status: running``; otherwise
    the web thread's ``Working...`` indicator stays idle until text happens to
    render.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay an assistant delta before any observed turn-start edge.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_agent_message_delta_event("turn_early", "item_agent", "Hello"),
                delta_coalescer=coalescer,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_session_status",
            "data": _expected_status_data("running", "turn_early"),
        },
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("Hello", "turn_early", "item_agent"),
        },
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_early"


def test_forwarder_recovers_user_before_recovered_agent_message_delta(tmp_path: Path) -> None:
    """
    Recovered assistant deltas do not stream above a missed user message.

    If the observer misses ``turn/started``, ``userMessage``, and
    ``item/started``, the first visible event may be an assistant delta.
    Adopting the turn is correct, but the forwarder must first recover and
    post the user item so the web transcript order remains user then
    assistant.

    :param tmp_path: Temporary bridge directory.
    :returns: None.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id=None,
        ),
    )
    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_early",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item_user",
                                "content": [{"type": "text", "text": "hello"}],
                            }
                        ],
                    }
                ],
            }
        }
    }
    fake_client = _FakeCodexAppServerClient(response=resume_response)
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay an assistant delta before all earlier turn events.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_agent_message_delta_event("turn_early", "item_agent", "Hello"),
                delta_coalescer=coalescer,
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_conversation_item",
        "external_session_status",
        "external_output_text_delta",
    ]
    assert posted[0]["data"]["item_data"]["role"] == "user"
    assert posted[0]["data"]["item_data"]["content"][0]["text"] == "hello"
    assert posted[1]["data"] == _expected_status_data("running", "turn_early")
    assert posted[2]["data"] == _expected_delta_data("Hello", "turn_early", "item_agent")
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]
    assert forwarder_state.has_posted_user_message("turn_early")


def test_forwarder_drops_stale_and_malformed_codex_agent_message_deltas(
    tmp_path: Path,
) -> None:
    """
    Codex assistant deltas only stream for the active turn.

    This prevents a delayed delta from an older rapid-send turn from
    appearing in the current web bubble. Non-string deltas are dropped
    before they can reach AP's strict ``data.delta`` validation.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_new",
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay stale, malformed, and valid delta notifications.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_old", "item_old", "stale"),
                _agent_message_delta_event("turn_new", "item_new", {"text": "bad"}),
                _agent_message_delta_event("turn_new", "item_new", "fresh"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("fresh", "turn_new", "item_new"),
        }
    ]


def test_forwarder_coalesces_codex_agent_message_deltas(tmp_path: Path) -> None:
    """
    Native Codex streaming does not post one Omnigent event per tiny delta.

    Breaking the coalescer would recreate the slow-drain failure where
    Codex finishes locally while the Omnigent SSE stream is still serialized
    behind many per-token HTTP POSTs. Stale and malformed deltas must
    still be filtered before text enters the coalesced buffer.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_new",
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay multiple delta notifications through the coalescer.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_old", "item_old", "stale"),
                _agent_message_delta_event("turn_new", "item_new", {"text": "bad"}),
                _agent_message_delta_event("turn_new", "item_new", "hel"),
                _agent_message_delta_event("turn_new", "item_new", "lo"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.flush()
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": _expected_delta_data("hello", "turn_new", "item_new"),
        }
    ]


def test_forwarder_posts_codex_usage_live_per_frame(
    tmp_path: Path,
) -> None:
    """
    Codex usage posts live (per frame) so the web UI cost badge updates
    mid-turn, not only at the turn boundary.

    Codex emits ``thread/tokenUsage/updated`` every few seconds; the forwarder
    flushes the coalescer right after recording each frame so the server can
    price and broadcast cost immediately. (Previously usage was deferred to the
    turn boundary, leaving the cost badge stuck until the turn ended.) The
    sparse cadence means the per-frame post does not block the high-frequency
    text-delta path, which has its own coalescer.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    posted: list[dict[str, Any]] = []
    posts_after_usage_updates: list[dict[str, Any]] = []
    posts_after_text_flush: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from both coalescers.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay usage updates, then text, then terminal completion.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            delta_coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            usage_coalescer = codex_native_forwarder._SessionUsageCoalescer(
                client,
                "conv_123",
            )
            elicitation_tracker = _elicitation_tracker()
            for event in [_usage_event(100), _usage_event(150)]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    event=event,
                    delta_coalescer=delta_coalescer,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                )
            posts_after_usage_updates.extend(posted)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                event=_agent_message_delta_event("turn_123", "item_agent", "visible text"),
                delta_coalescer=delta_coalescer,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
            )
            await delta_coalescer.flush()
            posts_after_text_flush.extend(posted)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                event=_completed_event("turn_123"),
                delta_coalescer=delta_coalescer,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
            )
            await delta_coalescer.close()
            await usage_coalescer.close()
            await elicitation_tracker.close()

    asyncio.run(run())

    # Usage now posts LIVE on each frame (so the cost badge moves mid-turn),
    # latest-only via the coalescer's dedup: 100 then 150. A value of ``[]``
    # here would mean usage was deferred again and the badge would stay stuck
    # until the turn boundary (the regression this guards).
    assert [p["type"] for p in posts_after_usage_updates] == [
        "external_session_usage",
        "external_session_usage",
    ]
    # The first frame posts every value (nothing posted yet); the second posts
    # only the CHANGED keys — context_window was unchanged, so the coalescer's
    # dedup drops it (proving latest-only diffing, not blind re-posting).
    assert posts_after_usage_updates[0]["data"] == {
        "context_tokens": 100,
        "context_window": 200_000,
        "cumulative_input_tokens": 100,
    }
    assert posts_after_usage_updates[1]["data"] == {
        "context_tokens": 150,
        "cumulative_input_tokens": 150,
    }
    # Text still streams via its own coalescer — the per-frame usage posts
    # neither swallowed nor blocked it.
    assert {
        "type": "external_output_text_delta",
        "data": _expected_delta_data("visible text", "turn_123", "item_agent"),
    } in posts_after_text_flush
    # Exactly two usage posts overall: the turn-boundary flush is a no-op
    # because 150 was already posted (dedup), so no duplicate lands at the end.
    assert [p["type"] for p in posted].count("external_session_usage") == 2


@pytest.mark.parametrize(
    ("deltas", "flush_interval_seconds", "flush_char_threshold", "expected_delta"),
    [
        (["timed"], 0.001, 1000, "timed"),
        (["abc", "de"], 60.0, 5, "abcde"),
        (["line\n"], 60.0, 1000, "line\n"),
    ],
)
def test_output_text_delta_coalescer_auto_flushes(
    deltas: list[str],
    flush_interval_seconds: float,
    flush_char_threshold: int,
    expected_delta: str,
) -> None:
    """
    The coalescer flushes without an explicit flush barrier.

    Each parametrized case isolates one automatic trigger: timer
    expiry, character threshold, and newline. The test waits for the
    Omnigent post directly instead of calling ``flush()``, so removing any
    trigger leaves that case stuck until ``wait_for`` fails.

    :param deltas: Text fragments appended to the coalescer, e.g.
        ``["abc", "de"]``.
    :param flush_interval_seconds: Timer budget for the first buffered
        delta, e.g. ``0.001``.
    :param flush_char_threshold: Buffered character threshold that
        triggers a flush, e.g. ``5``.
    :param expected_delta: Coalesced Omnigent delta payload.
    :returns: None.
    """
    posted: list[dict[str, Any]] = []

    async def run() -> None:
        """
        Append deltas and wait for the automatic Omnigent post.

        :returns: None.
        """
        posted_event = asyncio.Event()

        def handler(request: httpx.Request) -> httpx.Response:
            """
            Capture Omnigent event posts from the coalescer.

            :param request: HTTP request sent by the coalescer.
            :returns: Accepted response.
            """
            posted.append(json.loads(request.content))
            posted_event.set()
            return httpx.Response(202, json={"queued": False})

        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=flush_interval_seconds,
                flush_char_threshold=flush_char_threshold,
            )
            for delta in deltas:
                await coalescer.append(delta)
            await asyncio.wait_for(posted_event.wait(), timeout=1.0)
            await coalescer.close()

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_output_text_delta",
            "data": {"delta": expected_delta},
        }
    ]


def test_forwarder_flushes_coalesced_deltas_before_completed_agent_item(
    tmp_path: Path,
) -> None:
    """
    Completed Codex items cannot overtake buffered text deltas.

    The web stream should receive the live text tail before the durable
    completed ``agentMessage`` item for the same turn.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay buffered deltas followed by the completed assistant item.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _agent_message_delta_event("turn_123", "item_agent", "partial "),
                _agent_message_delta_event("turn_123", "item_agent", "tail"),
                _agent_message_event("turn_123", "item_agent", "complete response"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    delta_coalescer=coalescer,
                )
            await coalescer.close()

    asyncio.run(run())

    assert [payload["type"] for payload in posted] == [
        "external_output_text_delta",
        "external_conversation_item",
    ]
    assert posted[0]["data"] == _expected_delta_data("partial tail", "turn_123", "item_agent")
    assert posted[1]["data"]["item_data"]["content"][0]["text"] == "complete response"


def test_supervise_forwarder_continues_after_event_handler_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    One malformed Codex notification must not stop transcript mirroring.

    The native forwarder is long-lived. If one event raises during
    handling, later events from the same app-server stream still need
    to be processed or Codex responses stop syncing back to AP.
    """
    fake_client = _FakeCodexAppServerClient(
        events=[
            {"method": "turn/started", "params": {"turn": {"id": "turn_bad"}}},
            {"method": "turn/started", "params": {"turn": {"id": "turn_after"}}},
        ]
    )
    handled: list[str] = []

    async def fake_handle_event(
        _client: httpx.AsyncClient,
        *,
        session_id: str,
        bridge_dir: Path,
        event: dict[str, Any],
        delta_coalescer: codex_native_forwarder._OutputTextDeltaCoalescer | None = None,
        usage_coalescer: codex_native_forwarder._SessionUsageCoalescer | None = None,
        elicitation_tracker: codex_native_forwarder._CodexElicitationTaskTracker | None = None,
        expected_thread_id: str | None = None,
        codex_client: codex_native_app_server.CodexAppServerClient | None = None,
        forwarder_state: codex_native_forwarder._CodexForwarderState | None = None,
    ) -> None:
        """
        Fail the first event and record subsequent events.

        :param _client: Omnigent HTTP client.
        :param session_id: Omnigent session id, e.g. ``"conv_123"``.
        :param bridge_dir: Native Codex bridge directory.
        :param event: Codex event payload.
        :param delta_coalescer: Optional text-delta coalescer.
        :param usage_coalescer: Optional usage coalescer.
        :param elicitation_tracker: Optional elicitation tracker.
        :param expected_thread_id: Active Codex thread id.
        :param codex_client: Optional Codex app-server client.
        :param forwarder_state: Optional forwarder state.
        :returns: None.
        """
        del (
            session_id,
            bridge_dir,
            delta_coalescer,
            usage_coalescer,
            elicitation_tracker,
            expected_thread_id,
            codex_client,
            forwarder_state,
        )
        turn_id = event["params"]["turn"]["id"]
        if turn_id == "turn_bad":
            raise RuntimeError("bad event")
        handled.append(turn_id)

    monkeypatch.setattr(codex_native_forwarder, "_handle_event", fake_handle_event)

    async def run() -> None:
        """
        Run the supervisor against a two-event stream.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:1",
            headers={},
            session_id="conv_123",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            client=fake_client,  # type: ignore[arg-type]
        )

    asyncio.run(run())

    assert handled == ["turn_after"]
    assert "Codex forwarder event handling failed" in caplog.text


def test_forwarder_sends_codex_mcp_elicitation_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex MCP elicitation requests are forwarded to Omnigent and the Omnigent hook
    result is sent back to the app-server with the original JSON-RPC id.
    """
    fake_client = _FakeCodexAppServerClient()
    requests: list[httpx.Request] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the Omnigent hook request and return an accepted MCP result.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "action": "accept",
                "content": {"date": "tomorrow"},
                "_meta": None,
            },
        )

    async def run() -> None:
        """
        Drive one app-server request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert [request.url.path for request in requests] == [
        "/v1/sessions/conv_123/hooks/codex-elicitation-request"
    ]
    assert json.loads(requests[0].content) == codex_event
    assert fake_client.responses == [
        (
            3,
            {
                "action": "accept",
                "content": {"date": "tomorrow"},
                "_meta": None,
            },
        )
    ]


def test_forwarder_keeps_streaming_when_native_tui_answers_codex_elicitation(
    tmp_path: Path,
) -> None:
    """
    Native TUI approval must not park the Omnigent web mirror.

    The Omnigent hook remains pending when a separate native Codex client
    answers the prompt first. Codex app-server emits
    ``serverRequest/resolved`` with the original request id; the
    forwarder must mirror that exact resolution to Omnigent and still mirror
    later transcript events.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hook_cancelled.set()
                raise
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by native-side progress.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await asyncio.wait_for(
                codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                    event=codex_event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                ),
                timeout=0.2,
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {
                        "threadId": "thread_123",
                        "requestId": 3,
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=_agent_message_event("turn_123", "item_agent", "after approval"),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()
            await asyncio.wait_for(hook_cancelled.wait(), timeout=1.0)

    asyncio.run(run())

    assert fake_client.responses == []
    assert posted_events == [
        {
            "type": "external_elicitation_resolved",
            "data": {
                "elicitation_id": codex_elicitation_id(
                    "conv_123",
                    "mcpServer/elicitation/request",
                    3,
                ),
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "after approval"}],
                },
                "response_id": "codex_turn_123",
            },
        },
    ]


def test_forwarder_ignores_resolution_for_different_codex_request_id(
    tmp_path: Path,
) -> None:
    """
    Codex resolution must match the pending JSON-RPC request id.

    Same-thread activity is not enough: a different
    ``serverRequest/resolved.requestId`` may belong to another
    server-to-client request, so forwarding it would clear the wrong web
    approval card.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by an unrelated resolution.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {
                        "threadId": "thread_123",
                        "requestId": 999,
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=_agent_message_event("turn_123", "item_agent", "after approval"),
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    assert [event["type"] for event in posted_events] == ["external_conversation_item"]
    assert posted_events[0]["data"]["item_data"]["content"][0]["text"] == "after approval"


def test_forwarder_falls_back_to_terminal_turn_for_missed_resolution(
    tmp_path: Path,
) -> None:
    """
    A terminal Codex turn clears a matching pending web prompt.

    ``serverRequest/resolved`` is the exact signal for native-side
    approval, but if the forwarder misses that notification then an
    accepted ``turn/completed`` for the same turn is the next safe
    lifecycle boundary proving Codex is no longer waiting on the
    server-to-client request.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    hook_cancelled = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                hook_cancelled.set()
                raise
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by terminal-turn cleanup.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {
                            "id": "turn_123",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()
            await asyncio.wait_for(hook_cancelled.wait(), timeout=1.0)

    asyncio.run(run())

    assert fake_client.responses == []
    assert [event["type"] for event in posted_events] == [
        "external_session_status",
        "external_elicitation_resolved",
    ]
    assert posted_events[1]["data"]["elicitation_id"] == codex_elicitation_id(
        "conv_123",
        "mcpServer/elicitation/request",
        3,
    )


def test_forwarder_does_not_clear_pending_elicitation_for_stale_terminal_turn(
    tmp_path: Path,
) -> None:
    """
    Stale terminal turn events cannot clear a newer pending prompt.

    The fallback must run only after the active-turn guard accepts the
    terminal event. Otherwise a delayed completion from an older Codex
    turn could dismiss an approval card that belongs to the current
    native turn.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_events: list[dict[str, Any]] = []
    codex_event = {
        "id": 3,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_new",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_new",
        ),
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the hook open and capture subsequent Omnigent event posts.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()
        posted_events.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a pending elicitation followed by stale turn completion.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turn": {
                            "id": "turn_old",
                            "status": "completed",
                            "items": [],
                        },
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    assert fake_client.responses == []
    assert posted_events == []


def test_forwarder_sends_codex_request_user_input_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex requestUserInput frames use the same Omnigent hook path and relay
    its ``answers`` result back to app-server.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": "req_9",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_123",
            "questions": [{"id": "framework", "question": "Which framework?"}],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a requestUserInput result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(
            200,
            json={"answers": {"framework": {"answers": ["React"]}}},
        )

    async def run() -> None:
        """
        Drive one requestUserInput frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [("req_9", {"answers": {"framework": {"answers": ["React"]}}})]


def test_forwarder_flushes_plan_text_before_codex_request_user_input(
    tmp_path: Path,
) -> None:
    """
    Buffered plan deltas reach Omnigent before the final plan prompt.

    Codex can emit ``item/plan/delta`` and immediately send
    ``item/tool/requestUserInput`` for "Implement this plan?". The
    forwarder coalesces text deltas, so it must flush the buffer before
    posting the long-poll elicitation hook; otherwise web sees the
    prompt before the plan content, or the plan stays buffered while
    the hook waits for a user answer.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    fake_client = _FakeCodexAppServerClient()
    request_bodies: list[dict[str, Any]] = []
    request_paths: list[str] = []
    codex_event = {
        "id": "plan_prompt",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_plan_prompt",
            "questions": [
                {
                    "id": "plan_decision",
                    "header": "Plan",
                    "question": "Implement this plan?",
                    "isOther": False,
                    "isSecret": False,
                    "options": [{"label": "Yes, implement this plan"}],
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent posts in arrival order.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent response appropriate to the endpoint.
        """
        request_paths.append(request.url.path)
        request_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_decision": {"answers": ["Yes, implement this plan"]}}},
        )

    async def run() -> None:
        """
        Replay a plan delta followed by the plan-mode final prompt.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=_plan_delta_event("turn_123", "item_plan", "1. Inspect existing flow"),
                delta_coalescer=coalescer,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                delta_coalescer=coalescer,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()
            await coalescer.close()

    asyncio.run(run())

    assert request_paths == [
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/hooks/codex-elicitation-request",
    ]
    assert request_bodies[0] == {
        "type": "external_output_text_delta",
        "data": _expected_delta_data(
            "1. Inspect existing flow",
            "turn_123",
            "item_plan",
            item_type="plan",
        ),
    }
    assert request_bodies[1] == codex_event
    assert fake_client.responses == [
        (
            "plan_prompt",
            {"answers": {"plan_decision": {"answers": ["Yes, implement this plan"]}}},
        )
    ]


def test_forwarder_synthesizes_plan_implementation_prompt_after_completed_plan_turn(
    tmp_path: Path,
) -> None:
    """
    Completed Plan-mode turns surface the final implementation prompt in Omnigent Web.

    Codex's terminal TUI owns the ``Implement this plan?`` picker
    locally, so the app-server does not emit a native
    ``item/tool/requestUserInput`` request. The forwarder must bridge
    that terminal-only prompt through the existing Codex elicitation
    hook after the completed plan item and terminal turn event arrive.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(model="mock-model")
    request_paths: list[str] = []
    request_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent posts and decline the synthesized prompt.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent response appropriate to the endpoint.
        """
        request_paths.append(request.url.path)
        request_bodies.append(json.loads(request.content))
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_implementation": {"answers": ["No, stay in Plan mode"]}}},
        )

    async def run() -> None:
        """
        Replay a streamed plan, completed plan item, and terminal completion.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = codex_native_forwarder._OutputTextDeltaCoalescer(
                client,
                "conv_123",
                flush_interval_seconds=60.0,
                flush_char_threshold=1000,
            )
            for event in [
                _plan_delta_event("turn_123", "item_plan", "1. Inspect existing flow"),
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "1. Inspect existing flow",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    delta_coalescer=coalescer,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )
            await coalescer.close()

    asyncio.run(run())

    assert request_paths == [
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/events",
        "/v1/sessions/conv_123/hooks/codex-elicitation-request",
    ]
    assert request_bodies[0] == {
        "type": "external_output_text_delta",
        "data": _expected_delta_data(
            "1. Inspect existing flow",
            "turn_123",
            "item_plan",
            item_type="plan",
        ),
    }
    assert request_bodies[3] == {
        "id": "plan_implementation:turn_123",
        "method": "item/tool/requestUserInput",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "turn_123:plan_implementation",
            "questions": [
                {
                    "id": "plan_implementation",
                    "header": "Plan",
                    "question": "Implement this plan?",
                    "isOther": False,
                    "isSecret": False,
                    "options": [
                        {
                            "label": "Yes, implement this plan",
                            "description": "Switch to Default and start coding.",
                        },
                        {
                            "label": "Yes, clear context and implement",
                            "description": "Fresh thread with this plan.",
                        },
                        {
                            "label": "No, stay in Plan mode",
                            "description": "Continue planning with the model.",
                        },
                    ],
                }
            ],
        },
    }
    assert fake_client.requests == []


def test_forwarder_starts_default_turn_from_plan_implementation_prompt(
    tmp_path: Path,
) -> None:
    """
    Accepting the synthesized Plan prompt starts a Default-mode Codex turn.

    If the forwarder only displayed the web prompt without translating
    the answer back into Codex app-server actions, Omnigent Web would look
    interactive but selecting ``Yes, implement this plan`` would do
    nothing.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(model="mock-model")

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture Codex app-server requests and return a started turn.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope.
        """
        fake_client.requests.append((method, params))
        return {"result": {"turn": {"id": "turn_impl"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Accept the synthesized Plan implementation prompt.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={"answers": {"plan_implementation": {"answers": ["Yes, implement this plan"]}}},
        )

    async def run() -> None:
        """
        Replay a completed plan turn and answer its web prompt.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "1. Implement",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    assert fake_client.requests == [
        (
            "turn/start",
            {
                "threadId": "thread_123",
                "input": [{"type": "text", "text": "Implement the plan."}],
                "collaborationMode": {
                    "mode": "default",
                    "settings": {
                        "model": "mock-model",
                        "reasoning_effort": None,
                        "developer_instructions": None,
                    },
                },
            },
        )
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_impl"


def test_forwarder_starts_fresh_thread_from_clear_context_plan_prompt(
    tmp_path: Path,
) -> None:
    """
    The clear-context Plan prompt choice creates a fresh Codex thread.

    This mirrors the terminal TUI action closely enough for Omnigent Web:
    the bridge switches to the new thread, sends the clear-context
    implementation prompt, and records the new active turn.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(model="mock-model")

    async def fake_request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture Codex requests for fresh-thread plan implementation.

        :param method: JSON-RPC method.
        :param params: JSON-RPC params.
        :returns: JSON-RPC response envelope for the method.
        """
        fake_client.requests.append((method, params))
        if method == "thread/start":
            return {"result": {"thread": {"id": "thread_fresh"}}}
        return {"result": {"turn": {"id": "turn_fresh"}}}

    fake_client.request = fake_request  # type: ignore[method-assign]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Select the clear-context implementation option.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook or event response.
        """
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            200,
            json={
                "answers": {
                    "plan_implementation": {"answers": ["Yes, clear context and implement"]}
                }
            },
        )

    async def run() -> None:
        """
        Replay a completed plan turn and answer with clear-context.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "plan",
                            "id": "item_plan",
                            "text": "- do the work",
                        },
                    },
                },
                _completed_event("turn_123"),
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    assert fake_client.requests[0] == (
        "thread/start",
        {"model": "mock-model", "sessionStartSource": "clear"},
    )
    assert fake_client.requests[1][0] == "turn/start"
    assert fake_client.requests[1][1]["threadId"] == "thread_fresh"
    assert (
        fake_client.requests[1][1]["input"][0]["text"]
        == "A previous agent produced the plan below to accomplish the user's task. "
        "Implement the plan in a fresh context. Treat the plan as the source of "
        "user intent, re-read files as needed, and carry the work through "
        "implementation and verification.\n\n- do the work"
    )
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.thread_id == "thread_fresh"
    assert state.active_turn_id == "turn_fresh"


def test_forwarder_sends_codex_command_approval_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex command-approval request frames use the Omnigent hook path and
    relay its decision result back to app-server.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": 14,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "command": "date",
            "cwd": "/tmp/workspace",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a command approval result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one command approval frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [(14, {"decision": "accept"})]


def test_forwarder_routes_unregistered_child_command_approval_to_parent(
    tmp_path: Path,
) -> None:
    """
    Command approvals from an unregistered child thread are not dropped.

    Child thread registration can race behind server-to-client request
    frames. For ordinary transcript events an unknown non-parent
    ``threadId`` is stale and should be ignored, but an approval request
    must remain actionable. Until the child AP session is known, the
    forwarder posts the hook to the parent session so Nessie can answer it.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    child_started_event = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_unregistered",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}},
            }
        },
    }
    codex_event = {
        "id": 15,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_unregistered",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the AP hook request and accept the command.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one unregistered child-thread approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=child_started_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == [
        (
            "/v1/sessions/conv_parent/hooks/codex-elicitation-request",
            codex_event,
        )
    ]
    assert fake_client.responses == [(15, {"decision": "accept"})]


def test_forwarder_drops_unknown_thread_command_approval(
    tmp_path: Path,
) -> None:
    """
    Command approvals from unproven non-parent threads stay stale-dropped.

    The child-registration race exemption only applies after Codex has
    announced a child via ``thread/started`` metadata. A random stale
    thread id must not surface an approval card in the current parent
    session.

    :param tmp_path: Pytest temporary directory.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    codex_event = {
        "id": 16,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_stale_unknown",
            "turnId": "turn_stale",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Record any unexpected AP hook request.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one stale-thread approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == []
    assert fake_client.responses == []


def test_forwarder_drops_old_parent_child_command_approval(
    tmp_path: Path,
) -> None:
    """
    Announced child approvals must still match the active parent thread.

    A pending child marker from an old parent thread should not make a
    later stale approval actionable after the parent Codex thread has
    rotated.

    :param tmp_path: Pytest temporary directory.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    child_started_event = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_old_parent",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_old"}}},
            }
        },
    }
    codex_event = {
        "id": 17,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_old_parent",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Record any unexpected AP hook request.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one old-parent child approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=child_started_event,
                expected_thread_id="thread_old",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_new",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == []
    assert fake_client.responses == []


@dataclass(frozen=True)
class _CapturedSessionEvent:
    """
    Captured AP session event from a Codex forwarder test.

    :param session_id: AP session id parsed from the request path,
        e.g. ``"conv_new"``.
    :param body: Decoded session event body.
    """

    session_id: str
    body: dict[str, Any]


def test_supervise_forwarder_rotation_clears_unparented_pending_child_threads(
    tmp_path: Path,
) -> None:
    """
    Parent thread rotation clears unregistered child-thread markers.

    Codex may omit ``parent_thread_id`` on a child ``thread/started``
    notification. That gap marker is intentionally permissive while the
    parent thread is current, but it must not survive a parent ``/clear``
    rotation and make stale child approvals actionable in the new session.

    :param tmp_path: Pytest temporary directory.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_old",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_old",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )
    unparented_child_started = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_without_parent",
                "source": {"subAgent": {"thread_spawn": {}}},
            }
        },
    }
    stale_child_approval = {
        "id": 18,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_without_parent",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }
    fake_client = _FakeCodexAppServerClient(
        events=[
            unparented_child_started,
            _thread_started_event("thread_new"),
            _started_event("turn_new"),
            stale_child_approval,
        ]
    )
    session_events: list[_CapturedSessionEvent] = []
    hook_posts: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve AP calls made while the supervise loop rotates sessions.

        :param request: HTTP request from the forwarder.
        :returns: Fake AP response.
        """
        body = json.loads(request.content) if request.content else None
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_old":
            return httpx.Response(
                200,
                json={
                    "id": "conv_old",
                    "agent_id": "ag_codex",
                    "runner_id": "runner_123",
                    "labels": {},
                },
            )
        if request.method == "POST" and request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"id": "conv_new"})
        if request.method == "PATCH" and request.url.path in {
            "/v1/sessions/conv_new",
            "/v1/sessions/conv_old",
        }:
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[-1]})
        if request.method == "POST" and request.url.path == (
            "/v1/sessions/conv_old/resources/terminals/terminal_codex_main/transfer"
        ):
            return httpx.Response(200, json={"id": "terminal_codex_main"})
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            assert isinstance(body, dict)
            hook_posts.append(body)
            return httpx.Response(200, json={"decision": "accept"})
        if request.url.path.endswith("/events"):
            assert isinstance(body, dict)
            session_id = request.url.path.split("/")[3]
            session_events.append(_CapturedSessionEvent(session_id=session_id, body=body))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(
            500,
            json={"error": f"unexpected {request.method} {request.url.path}"},
        )

    async def run() -> None:
        """
        Run the supervise loop over rotation and stale child events.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_old",
            bridge_dir=tmp_path,
            app_server_url="ws://127.0.0.1:9876",
            thread_id="thread_old",
            client=fake_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )

    asyncio.run(run())

    assert [
        event.body["data"]["status"]
        for event in session_events
        if event.session_id == "conv_new" and event.body["type"] == "external_session_status"
    ] == ["running"]
    assert [event for event in session_events if event.session_id == "conv_old"] == []
    assert hook_posts == []
    assert fake_client.responses == []


def test_forwarder_sends_codex_permissions_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex permission-profile request frames are relayed through Omnigent and
    answered with the hook's permission-grant result.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": 15,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_permissions",
            "startedAtMs": 1,
            "cwd": "/tmp/workspace",
            "reason": "need network",
            "permissions": {"network": {"enabled": True}, "fileSystem": None},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a permissions approval result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(
            200,
            json={"permissions": {"network": {"enabled": True}}, "scope": "turn"},
        )

    async def run() -> None:
        """
        Drive one permissions request frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [
        (15, {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
    ]


def test_forwarder_logs_unsupported_codex_server_request(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Unsupported Codex server requests are visible in forwarder logs.

    Without this diagnostic, a new app-server request method can be
    delivered on the observer connection and disappear with no clue
    about which protocol adapter is missing.
    """
    fake_client = _FakeCodexAppServerClient()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if the unsupported request is accidentally posted to AP.

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        """
        raise AssertionError(f"unexpected Omnigent request: {request.method} {request.url}")

    async def run() -> None:
        """
        Drive one unsupported server request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "id": "unsupported_1",
                    "method": "item/tool/call",
                    "params": {"threadId": "thread_123"},
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )

    asyncio.run(run())

    assert fake_client.responses == []
    assert (
        "Codex forwarder ignored unsupported server request: method=item/tool/call" in caplog.text
    )


def test_forwarder_leaves_codex_elicitation_pending_on_empty_hook_body(
    tmp_path: Path,
) -> None:
    """
    Empty Omnigent hook responses represent timeout/disconnect fallback, not
    an approval. The forwarder must not synthesize an accept/decline
    result back to Codex.
    """
    fake_client = _FakeCodexAppServerClient()

    def handler(_request: httpx.Request) -> httpx.Response:
        """
        Return the Omnigent hook's fail-ask shape.

        :param _request: HTTP request sent by the forwarder.
        :returns: Empty successful response.
        """
        return httpx.Response(200)

    async def run() -> None:
        """
        Drive one timed-out elicitation request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event={
                    "id": 4,
                    "method": "mcpServer/elicitation/request",
                    "params": {
                        "mode": "form",
                        "message": "Pick a value",
                        "requestedSchema": {"type": "object", "properties": {}},
                    },
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == []


def test_forwarder_posts_user_message_on_assistant_item_started(tmp_path: Path) -> None:
    """
    The user message is recovered when the assistant STARTS, not finishes.

    On a fresh thread the live ``userMessage`` event can be missed. If
    recovery waited for the assistant's ``item/completed``, the assistant
    text deltas would already have streamed into a bubble rendered ABOVE
    the still-pending user bubble. Recovering at the assistant's
    ``item/started`` — which fires before any delta — commits the user
    message first, so the web UI renders the question above the reply.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_123",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item-1",
                                "content": [{"type": "text", "text": "hello codex"}],
                            }
                        ],
                    }
                ],
            }
        }
    }
    fake_client = _FakeCodexAppServerClient(response=resume_response)
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )

    async def run() -> None:
        """
        Deliver the assistant's ``item/started`` with the user missed live.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/started",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {"type": "agentMessage", "id": "msg_live"},
                    },
                },
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    items = [p for p in posted if p["type"] == "external_conversation_item"]
    # The user message is posted at assistant-start (before any delta),
    # so it lands first and the assistant streams below it.
    assert [p["data"]["item_data"]["role"] for p in items] == ["user"]
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "hello codex"
    # The recovery issued exactly one resume to fetch the user message, and
    # the turn is now marked so the item/completed backstop won't re-post.
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]
    assert forwarder_state.has_posted_user_message("turn_123")


def test_forwarder_posts_codex_user_and_agent_messages(tmp_path: Path) -> None:
    """
    Codex app-server completed message items are translated into
    external conversation items for the Omnigent session stream.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                        "completedAtMs": 1,
                    },
                },
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "agentMessage",
                            "id": "item_agent",
                            "text": "hello from codex",
                            "phase": None,
                            "memoryCitation": None,
                        },
                        "completedAtMs": 2,
                    },
                },
            )

    asyncio.run(run())

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello codex"}],
                },
                "response_id": "codex_turn_123",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [{"type": "output_text", "text": "hello from codex"}],
                },
                "response_id": "codex_turn_123",
            },
        },
    ]


def test_forwarder_recovers_missed_user_message_before_assistant(tmp_path: Path) -> None:
    """
    A missed live ``userMessage`` is recovered before the assistant reply.

    On a fresh thread the forwarder can subscribe after ``turn/start``, so
    the early ``userMessage`` event streams past before the subscription
    lands and only the ``agentMessage`` arrives live. Without recovery the
    assistant reply would be posted first and the resume backfill would
    add the user message after it, inverting the web bubbles. The
    forwarder must resume to recover the turn's user message and post it
    BEFORE the reply so Omnigent assigns it the earlier position.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    # Resume returns the full turn (user then assistant) with Codex's
    # positional resume ids; the recovery reads the userMessage from it.
    resume_response = {
        "result": {
            "thread": {
                "id": "thread_123",
                "turns": [
                    {
                        "id": "turn_123",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "item-1",
                                "content": [{"type": "text", "text": "hello codex"}],
                            },
                            {"type": "agentMessage", "id": "item-2", "text": "hello from codex"},
                        ],
                    }
                ],
            }
        }
    }
    fake_client = _FakeCodexAppServerClient(response=resume_response)
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )

    async def run() -> None:
        """
        Deliver only the assistant message live (user message missed).

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_agent_message_event("turn_123", "msg_live", "hello from codex"),
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    items = [p for p in posted if p["type"] == "external_conversation_item"]
    roles = [p["data"]["item_data"]["role"] for p in items]
    # User recovered and posted first, then the assistant reply.
    assert roles == ["user", "assistant"]
    assert items[0]["data"]["item_data"]["content"][0]["text"] == "hello codex"
    assert items[1]["data"]["item_data"]["content"][0]["text"] == "hello from codex"
    # The recovery issued exactly one resume to fetch the user message.
    assert [method for method, _ in fake_client.requests] == ["thread/resume"]


def test_forwarder_skips_user_recovery_when_user_seen_live(tmp_path: Path) -> None:
    """
    No recovery resume fires when the user message arrived live.

    On the happy path the live stream delivers ``userMessage`` before
    ``agentMessage``, so the forwarder must NOT issue a spurious
    ``thread/resume`` when the reply arrives — the turn is already known
    to have a posted user message.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    fake_client = _FakeCodexAppServerClient()
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_123",
        codex_client=fake_client,  # type: ignore[arg-type]
    )

    async def run() -> None:
        """
        Deliver the user message then the assistant message live.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "msg_user_live",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                    },
                },
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event=_agent_message_event("turn_123", "msg_live", "hello from codex"),
                expected_thread_id="thread_123",
                forwarder_state=forwarder_state,
            )

    asyncio.run(run())

    roles = [
        p["data"]["item_data"]["role"] for p in posted if p["type"] == "external_conversation_item"
    ]
    assert roles == ["user", "assistant"]
    # The live user message satisfied the ordering guarantee; no resume.
    assert fake_client.requests == []


def test_forwarder_posts_codex_turn_plan_update(tmp_path: Path) -> None:
    """
    Codex ``turn/plan/updated`` notifications are visible in Omnigent web.

    Plan mode emits plan state through a dedicated app-server
    notification rather than assistant text. If the forwarder ignores
    it, the terminal shows the plan while the web transcript appears to
    skip straight to the final prompt.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay one plan update notification.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "turn/plan/updated",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "explanation": None,
                        "plan": [
                            {"step": "Inspect Codex plan events", "status": "completed"},
                            {"step": "Mirror plans to web", "status": "inProgress"},
                            {"step": "Run checks", "status": "pending"},
                        ],
                    },
                },
            )

    asyncio.run(run())

    assert len(posted) == 1
    assert posted[0]["type"] == "external_conversation_item"
    data = posted[0]["data"]
    assert data["item_type"] == "message"
    assert data["response_id"] == "codex_turn_123"
    assert data["item_data"] == {
        "role": "assistant",
        "agent": "codex-native-ui",
        "content": [
            {
                "type": "output_text",
                "text": (
                    "Plan:\n"
                    "- [x] Inspect Codex plan events\n"
                    "- [~] Mirror plans to web\n"
                    "- [ ] Run checks"
                ),
            }
        ],
    }


def test_forwarder_posts_completed_codex_plan_item() -> None:
    """
    Completed Codex ``plan`` thread items are mirrored into Omnigent history.

    This covers resume/replay and final transcript state, where the
    plan arrives as a completed thread item rather than a live
    ``turn/plan/updated`` notification.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "plan",
                "id": "plan_123",
                "text": "1. Inspect\n2. Implement\n3. Verify",
            },
            _capture_handler(posted),
        )
    )

    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": "codex-native-ui",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "1. Inspect\n2. Implement\n3. Verify",
                        }
                    ],
                },
                "response_id": "codex_turn_123",
            },
        }
    ]


def _capture_handler(posted: list[dict[str, Any]]) -> Callable[[httpx.Request], httpx.Response]:
    """
    Build a MockTransport handler that records forwarder event posts.

    :param posted: List to append each decoded request body to.
    :returns: Handler that records the body and returns ``202``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture one Omnigent event post from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    return handler


async def _replay_completed_item(
    item: dict[str, Any], handler: Callable[..., httpx.Response]
) -> None:
    """
    Drive one Codex ``item/completed`` notification through the forwarder.

    :param item: Codex item payload, e.g. a ``commandExecution`` item.
    :param handler: MockTransport handler capturing the Omnigent posts.
    :returns: None.
    """
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    ) as client:
        await codex_native_forwarder._handle_event(
            client,
            session_id="conv_123",
            bridge_dir=Path("/tmp"),
            usage_coalescer=_usage_coalescer(client),
            elicitation_tracker=_elicitation_tracker(),
            event={
                "method": "item/completed",
                "params": {
                    "threadId": "thread_123",
                    "turnId": "turn_123",
                    "item": item,
                },
            },
        )


def test_forwarder_posts_codex_command_execution_tool_call() -> None:
    """
    A completed Codex ``commandExecution`` becomes a function-call pair.

    Native Codex sessions run Codex's own shell tool, so the single
    ``item/completed`` (which carries both the command and its
    aggregated output) must be mirrored as the Omnigent ``function_call`` /
    ``function_call_output`` pair the web UI renders. The item shape
    here matches a real app-server capture.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_abc123",
                "command": "/bin/zsh -lc 'cat hello.txt'",
                "cwd": "/repo",
                "status": "completed",
                "aggregatedOutput": "hello world\n",
                "exitCode": 0,
                "durationMs": 0,
            },
            _capture_handler(posted),
        )
    )

    # Both items must be posted: a function_call carrying the command as
    # arguments, then a function_call_output carrying the shell output.
    # A single post would mean the result was dropped; the call_id must
    # match across both so the UI pairs them into one tool card.
    assert posted == [
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "codex-native-ui",
                    "name": "shell",
                    "arguments": '{"command": "/bin/zsh -lc \'cat hello.txt\'", "cwd": "/repo"}',
                    "call_id": "call_abc123",
                },
                "response_id": "codex_turn_123",
            },
        },
        {
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {
                    "call_id": "call_abc123",
                    "output": "hello world\n",
                },
                "response_id": "codex_turn_123",
            },
        },
    ]


def test_forwarder_surfaces_failed_command_exit_code() -> None:
    """
    A non-zero command exit is surfaced in the mirrored output.

    Codex reports ``exitCode`` separately from ``aggregatedOutput``, so a
    failed command would look successful in the UI unless the forwarder
    folds the exit code into the output text.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_fail",
                "command": "/bin/zsh -lc 'exit 3'",
                "cwd": "/repo",
                "status": "failed",
                "aggregatedOutput": "boom\n",
                "exitCode": 3,
                "durationMs": 1,
            },
            _capture_handler(posted),
        )
    )

    outputs = [
        p["data"]["item_data"]["output"]
        for p in posted
        if p["data"]["item_type"] == "function_call_output"
    ]
    # The exit code (3, from the replayed item above) must appear appended
    # to the captured stderr/stdout. If the suffix were missing the output
    # would be just "boom\n" — a failed command indistinguishable from a
    # successful one in the UI.
    assert outputs == ["boom\n\n[exit code: 3]"]


def test_forwarder_posts_codex_file_change_tool_call() -> None:
    """
    A completed Codex ``fileChange`` becomes an apply_patch tool card.

    The item shape (``changes`` with ``path`` / ``kind`` / ``diff``)
    matches a real app-server capture; the forwarder must pass the
    changes through as arguments and summarize them as the output.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "fileChange",
                "id": "call_patch",
                "changes": [
                    {
                        "path": "/repo/greeting.py",
                        "kind": {"type": "add"},
                        "diff": "print('hi')\n",
                    }
                ],
                "status": "completed",
            },
            _capture_handler(posted),
        )
    )

    assert [p["data"]["item_type"] for p in posted] == [
        "function_call",
        "function_call_output",
    ]
    call = posted[0]["data"]["item_data"]
    assert call["name"] == "apply_patch"
    # Arguments carry the raw changes so the diff is recoverable in the UI.
    assert json.loads(call["arguments"]) == {
        "changes": [
            {"path": "/repo/greeting.py", "kind": {"type": "add"}, "diff": "print('hi')\n"}
        ]
    }
    # Output summarizes each change as "<kind> <path>" from real fields.
    assert posted[1]["data"]["item_data"]["output"] == "add /repo/greeting.py"


def test_forwarder_posts_codex_web_search_tool_call() -> None:
    """
    A completed Codex ``webSearch`` becomes a web_search tool card.

    Codex does not surface search results, so the queries it ran are the
    only result data; the forwarder uses them as the output rather than
    inventing a result. The item shape matches a real app-server capture.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "webSearch",
                "id": "ws_123",
                "query": "python latest stable version",
                "action": {
                    "type": "search",
                    "query": "python latest stable version",
                    "queries": ["python latest stable version"],
                },
            },
            _capture_handler(posted),
        )
    )

    call = posted[0]["data"]["item_data"]
    assert call["name"] == "web_search"
    assert json.loads(call["arguments"]) == {"query": "python latest stable version"}
    assert posted[1]["data"]["item_data"]["output"] == "python latest stable version"


def test_forwarder_drops_codex_tool_item_missing_required_field(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A malformed tool item is dropped, not mirrored with invented fields.

    A ``commandExecution`` with no ``command`` must not be posted as a
    tool call with an empty/placeholder command, because that would
    create a misleading tool card. The forwarder logs and skips it.
    """
    posted: list[dict[str, Any]] = []
    asyncio.run(
        _replay_completed_item(
            {
                "type": "commandExecution",
                "id": "call_bad",
                "cwd": "/repo",
                "status": "completed",
                "aggregatedOutput": "",
                "exitCode": 0,
            },
            _capture_handler(posted),
        )
    )

    # Nothing is posted: a malformed item is dropped rather than mirrored
    # with a fabricated command, and the drop is logged for diagnosis.
    assert posted == []
    assert "Codex commandExecution missing command" in caplog.text


def test_forwarder_skips_item_retry_on_ambiguous_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An ambiguous item-POST failure stops retries instead of re-posting.

    A lost response (e.g. read timeout) after the server already
    appended the item and published ``session.input.consumed`` is
    indistinguishable from a failed send. External items are not deduped
    server-side, so a retry would persist a second copy — the duplicate
    message bug in the web UI. The forwarder must give up after
    the first ambiguous attempt.

    A failure here (more than one POST attempt) is exactly the
    duplicate-item regression this guards against.
    """
    attempts: list[str] = []
    sleep_delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Record retry delays without slowing the test.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """
        sleep_delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a read timeout (response lost).

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ReadTimeout: For every attempt, simulating the
            server committing the item but the response being lost.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ReadTimeout("response lost", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item against the failing transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_external_item(
                client,
                "conv_123",
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                response_id="codex_turn_123",
            )

    asyncio.run(run())

    # Exactly one POST attempt: the ambiguous failure must not be
    # retried. Two or three attempts would mean the forwarder re-posted
    # a possibly-committed item — the duplicate-bubble bug.
    assert attempts == ["external_conversation_item"]
    # Returned before any backoff: no retry was even scheduled.
    assert sleep_delays == []


def test_forwarder_retries_item_on_connect_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A provably-undelivered item POST keeps its full retry budget.

    A connection error proves no request bytes reached the server, so
    the item cannot have been committed — retrying is safe and dropping
    early would lose messages whenever the server is briefly
    unreachable. The complement to the ambiguous-skip test, guarding
    the duplicate fix from turning into a message-loss bug.
    """
    attempts: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Skip retry backoff to keep the test fast.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every item POST with a connection error (never delivered).

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ConnectError: For every attempt, simulating an
            unreachable server.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item against the unreachable transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_external_item(
                client,
                "conv_123",
                item_type="message",
                item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                response_id="codex_turn_123",
            )

    asyncio.run(run())

    # The full retry budget was spent: connect errors are safe to retry,
    # so the forwarder must not give up after the first attempt. Fewer
    # attempts would mean the ambiguous-skip is over-broad (message loss).
    assert attempts == ["external_conversation_item"] * codex_native_forwarder._POST_MAX_ATTEMPTS


def test_forwarder_still_retries_status_on_ambiguous_transport_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The ambiguous-failure skip applies only to conversation items.

    Status events are idempotent (last-write-wins), so re-posting one
    that may already have landed is harmless — and keeping the retries
    preserves delivery of the running/idle badge. A failure here means
    the skip leaked beyond ``external_conversation_item`` and transient
    events lost their retry budget.
    """
    attempts: list[str] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Skip retry backoff to keep the test fast.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail every status POST with a read timeout.

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        :raises httpx.ReadTimeout: For every attempt.
        """
        attempts.append(json.loads(request.content)["type"])
        raise httpx.ReadTimeout("response lost", request=request)

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one status edge against the failing transport.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._post_status(client, "conv_123", "running")

    asyncio.run(run())

    # All attempts spent: status posts keep retrying through ambiguous
    # failures because re-delivery is harmless and dropping early would
    # strand the session badge.
    assert attempts == ["external_session_status"] * codex_native_forwarder._POST_MAX_ATTEMPTS


def test_forwarder_retries_transient_external_item_rejection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Transient Omnigent failures do not drop the mirrored Codex item.

    This test fails if ``_post_external_item`` gives up after the first
    retryable HTTP status instead of retrying the same item post.
    """
    posted: list[dict[str, Any]] = []
    sleep_delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        """
        Record retry delays without slowing the test.

        :param seconds: Delay requested by the forwarder.
        :returns: None.
        """
        sleep_delays.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Reject the first item post transiently, then accept the retry.

        :param request: HTTP request sent by the forwarder.
        :returns: HTTP response for this attempt.
        """
        posted.append(json.loads(request.content))
        if len(posted) == 1:
            return httpx.Response(503, text="starting")
        return httpx.Response(202, json={"queued": False})

    monkeypatch.setattr(codex_native_forwarder, "_sleep", fake_sleep)

    async def run() -> None:
        """
        Post one mirrored Codex item through the real handler.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "agentMessage",
                            "id": "item_agent",
                            "text": "retry me",
                        },
                    },
                },
            )

    asyncio.run(run())

    # Exactly two posts proves the first transient rejection was retried
    # once and then accepted; one would mean a dropped item, while more
    # would mean the forwarder retried after success.
    assert len(posted) == 2
    assert posted[0] == posted[1]
    assert posted[1]["data"]["item_data"]["content"][0]["text"] == "retry me"
    assert sleep_delays == [0.1]


def test_forwarder_logs_rejected_external_item(
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """
    Omnigent 4xx responses are logged so mirror failures are diagnosable.
    """

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad payload")

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user",
                            "content": [{"type": "text", "text": "hello codex"}],
                        },
                    },
                },
            )

    asyncio.run(run())

    assert "failed to post Codex conversation item: status=400 body=bad payload" in caplog.text


def test_forwarder_marks_codex_skill_user_message_as_meta(tmp_path: Path) -> None:
    """
    Codex ``<skill>`` user messages are hidden durable context.

    Codex persists skill bodies as user messages wrapped in
    ``<skill>...</skill>``. The forwarder must preserve that message
    for Omnigent resume/history replay while tagging it ``is_meta`` so UI
    clients can hide it.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent event posts from the forwarder.

        :param request: HTTP request sent by the forwarder.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Replay one normal and one Codex skill user-message item.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_normal",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user_normal",
                            "content": [{"type": "text", "text": "hello"}],
                        },
                    },
                },
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=_elicitation_tracker(),
                event={
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_skill",
                        "item": {
                            "type": "userMessage",
                            "id": "item_user_skill",
                            "content": [
                                {
                                    "type": "text",
                                    "text": (
                                        "<skill>\n<name>grill-me</name>\nAsk questions.\n</skill>"
                                    ),
                                }
                            ],
                        },
                    },
                },
            )

    asyncio.run(run())

    assert len(posted) == 2
    normal_data = posted[0]["data"]["item_data"]
    skill_data = posted[1]["data"]["item_data"]
    assert "is_meta" not in normal_data
    assert skill_data["is_meta"] is True
    assert skill_data["content"][0]["text"].startswith("<skill>")


def test_local_run_prints_resume_hint_after_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    Local ``omnigent codex`` prints a copyable resume hint on exit.

    This exercises the run-level call site, not just the formatter:
    a regression that prepares and attaches successfully but forgets
    to echo the final ``--resume`` prompt fails on the captured stderr.
    """
    spec_path = tmp_path / "codex.yaml"
    spec_path.write_text("name: codex-native-ui\nprompt: hi\n", encoding="utf-8")
    opened: list[tuple[str, str, bool]] = []

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the fake local server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without starting a process.

        :param args: Positional startup arguments.
        :param kwargs: Keyword startup arguments.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return prepared Codex terminal details without launching Codex.

        :param kwargs: Terminal preparation keyword arguments.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return codex_native.PreparedCodexTerminal(
            session_id="conv_codex_fresh",
            terminal_id=codex_native.codex_terminal_resource_id(),
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**kwargs: object) -> None:
        """
        Simulate a completed Codex attach session.

        :param kwargs: Attach keyword arguments.
        :returns: None.
        """
        del kwargs

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 23456)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr("omnigent.chat._bundle_agent", lambda path: b"bundle")
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)
    monkeypatch.setattr(
        codex_native,
        "open_conversation_link_if_enabled",
        lambda **kwargs: opened.append(
            (
                kwargs["base_url"],
                kwargs["conversation_id"],
                kwargs["enabled"],
            )
        ),
    )

    codex_native._run_with_local_server(
        spec_path,
        session_id=None,
        resume_picker=False,
        codex_args=(),
        command="codex",
        model=None,
        prompt=None,
        auto_open_conversation=True,
    )

    captured = capsys.readouterr()
    web_ui = "Web UI: http://127.0.0.1:23456/c/conv_codex_fresh"
    resume_hint = "Resume with: omnigent codex --resume conv_codex_fresh"
    assert web_ui in captured.err
    assert resume_hint in captured.err
    assert captured.err.index(web_ui) < captured.err.index(resume_hint)
    assert opened == [("http://127.0.0.1:23456", "conv_codex_fresh", True)]


def test_local_resume_does_not_print_redundant_resume_hint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """
    ``omnigent codex --resume`` does not echo another resume prompt.

    This prevents the hint from becoming always-on noise after a user
    has already chosen the conversation id to resume.
    """
    spec_path = tmp_path / "codex.yaml"
    spec_path.write_text("name: codex-native-ui\nprompt: hi\n", encoding="utf-8")

    class _Proc:
        """Stub for the local server subprocess."""

        def poll(self) -> None:
            """
            Pretend the fake local server is alive.

            :returns: None.
            """

    def fake_start_server(*args: object, **kwargs: object) -> Any:
        """
        Return a minimal server handle without starting a process.

        :param args: Positional startup arguments.
        :param kwargs: Keyword startup arguments.
        :returns: Fake local server handle.
        """
        del args, kwargs
        return SimpleNamespace(proc=_Proc(), runner_id="runner_local", log_path=None)

    async def fake_prepare(**kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return prepared Codex terminal details without launching Codex.

        :param kwargs: Terminal preparation keyword arguments.
        :returns: Prepared fake terminal.
        """
        del kwargs
        return codex_native.PreparedCodexTerminal(
            session_id="conv_codex_existing",
            terminal_id=codex_native.codex_terminal_resource_id(),
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**kwargs: object) -> None:
        """
        Simulate a completed Codex attach session.

        :param kwargs: Attach keyword arguments.
        :returns: None.
        """
        del kwargs

    monkeypatch.setattr("omnigent.chat._find_free_port", lambda: 23457)
    monkeypatch.setattr("omnigent.chat._start_local_server", fake_start_server)
    monkeypatch.setattr("omnigent.chat._stop_local_server", lambda server: None)
    monkeypatch.setattr("omnigent.chat._wait_for_server", lambda *a, **k: None)
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)

    codex_native._run_with_local_server(
        spec_path,
        session_id="conv_codex_existing",
        resume_picker=False,
        codex_args=(),
        command="codex",
        model=None,
        prompt=None,
    )

    captured = capsys.readouterr()
    assert "Web UI: http://127.0.0.1:23457/c/conv_codex_existing" in captured.err
    assert "Resume with:" not in captured.err


def test_run_codex_native_does_not_require_local_codex_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The wrapper no longer owns the Codex process.

    A missing local ``codex`` binary must not fail preflight because the
    daemon-spawned runner resolves and starts Codex. If the old local
    preflight comes back, this test raises before ``fake_remote`` records
    the command.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    remote_called = False

    def fake_which(command: str) -> str | None:
        """
        Return fake executable paths.

        :param command: Command name passed to ``shutil.which``.
        :returns: Fake absolute path for tmux, otherwise ``None``.
        """
        if command == "tmux":
            return "/usr/bin/tmux"
        return None

    def fake_remote(
        base_url: str,
        spec_path: Path,
        *,
        session_id: str | None,
        resume_picker: bool,
        codex_args: tuple[str, ...],
        model: str | None,
        prompt: str | None,
        auto_open_conversation: bool,
    ) -> None:
        """
        Record that the remote daemon path was selected.

        :returns: None.
        """
        nonlocal remote_called
        del (
            base_url,
            spec_path,
            session_id,
            resume_picker,
            codex_args,
            model,
            prompt,
            auto_open_conversation,
        )
        remote_called = True

    monkeypatch.setattr(codex_native.shutil, "which", fake_which)
    monkeypatch.setattr(codex_native, "_run_with_remote_server", fake_remote)

    codex_native.run_codex_native(
        server="http://localhost:8000",
        session_id=None,
        codex_args=(),
        command="codex",
    )

    assert remote_called is True


def test_record_launch_for_fresh_session_persists_current_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh Codex sessions record the cwd used for future resumes.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.codex_native_state import read_launch_state

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))

    codex_native._record_launch_for_fresh_session("conv_abc")

    state = read_launch_state("conv_abc")
    assert state is not None
    assert state.working_directory == str(workspace.resolve())


def test_align_working_directory_with_session_matching_cwd_is_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Resume from the recorded cwd must not prompt or mutate cwd.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.codex_native_state import write_launch_state

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(tmp_path.resolve()))

    def fail_prompt(**_kwargs: object) -> str:
        """
        Fail if a prompt is shown for a matching cwd.

        :returns: Never returns.
        """
        raise AssertionError("matching cwd should not prompt")

    monkeypatch.setattr(codex_native, "_prompt_codex_resume_workspace_action", fail_prompt)

    codex_native._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == tmp_path.resolve()


def test_align_working_directory_with_session_switches_to_recorded_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Choosing ``switch`` changes cwd before Codex resume continues.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.codex_native_state import write_launch_state

    recorded = tmp_path / "recorded"
    current = tmp_path / "current"
    recorded.mkdir()
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(recorded.resolve()))
    monkeypatch.setattr(
        codex_native,
        "_prompt_codex_resume_workspace_action",
        lambda **_kwargs: "switch",
    )

    codex_native._align_working_directory_with_session("conv_abc")

    assert Path.cwd().resolve() == recorded.resolve()


def test_align_working_directory_with_session_missing_recorded_cwd_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Recorded-but-missing cwd fails loud instead of starting Codex wrong.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary workspace and state root.
    :returns: None.
    """
    from omnigent.codex_native_state import write_launch_state

    current = tmp_path / "current"
    missing = tmp_path / "missing"
    current.mkdir()
    monkeypatch.chdir(current)
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_STATE_DIR", str(tmp_path / "state"))
    write_launch_state("conv_abc", str(missing))

    with pytest.raises(click.ClickException) as excinfo:
        codex_native._align_working_directory_with_session("conv_abc")

    assert "conv_abc" in excinfo.value.message
    assert str(missing) in excinfo.value.message


def test_codex_resume_workspace_options_name_cancel_action(tmp_path: Path) -> None:
    """
    The cwd mismatch prompt names cancellation explicitly.

    A generic ``leave`` option is ambiguous because Codex does not
    continue from the current cwd; the caller cancels resume.

    :param tmp_path: Temporary recorded workspace path.
    :returns: None.
    """
    options = codex_native._codex_resume_workspace_action_options(
        recorded_path=tmp_path,
    )

    assert [(option.action, option.label) for option in options] == [
        ("switch", f"Switch working directory to {tmp_path}"),
        ("cancel", "Cancel resume"),
    ]


def test_run_with_remote_server_aligns_cwd_before_daemon_prepare(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Remote resume aligns cwd before daemon runner preparation samples it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary paths for prepared Codex details.
    :returns: None.
    """
    import omnigent.chat as chat_mod
    import omnigent.cli as cli_mod
    import omnigent.host.identity as identity_mod

    order: list[str] = []

    def fake_ensure_host_daemon(*_args: object, **_kwargs: object) -> None:
        """
        Record daemon startup order.

        :returns: None.
        """
        order.append("ensure-daemon")

    async def fake_prepare_codex_terminal_via_daemon(
        **kwargs: object,
    ) -> codex_native.PreparedCodexTerminal:
        """
        Record terminal preparation order.

        :param kwargs: Preparation keyword arguments.
        :returns: Prepared reattached Codex terminal.
        """
        assert kwargs["host_id"] == "host_local"
        assert kwargs["session_id"] == "conv_abc"
        assert kwargs["workspace"] == str(aligned_dir.resolve())
        assert isinstance(kwargs["startup_progress"], RunnerStartupProgress)
        order.append("prepare")
        return codex_native.PreparedCodexTerminal(
            session_id="conv_abc",
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=True,
        )

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Record attach order.

        :returns: None.
        """
        order.append("attach")

    monkeypatch.setattr(chat_mod, "_remote_headers", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(chat_mod, "_server_auth", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_mod, "_ensure_host_daemon", fake_ensure_host_daemon)
    monkeypatch.setattr(
        identity_mod,
        "load_or_create_host_identity",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    start_dir = tmp_path / "start"
    aligned_dir = tmp_path / "aligned"
    start_dir.mkdir()
    aligned_dir.mkdir()
    monkeypatch.chdir(start_dir)

    monkeypatch.setattr(
        codex_native,
        "_resolve_session_id_for_resume",
        lambda **_kwargs: "conv_abc",
    )

    def fake_align_working_directory(_session_id: str) -> None:
        """
        Simulate resume alignment changing the process cwd.

        :param _session_id: Session id being aligned.
        :returns: None.
        """
        order.append("align")
        os.chdir(aligned_dir)

    monkeypatch.setattr(
        codex_native,
        "_align_working_directory_with_session",
        fake_align_working_directory,
    )
    monkeypatch.setattr(
        codex_native,
        "_prepare_codex_terminal_via_daemon",
        fake_prepare_codex_terminal_via_daemon,
    )
    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)

    codex_native._run_with_remote_server(
        "https://example.com",
        tmp_path / "codex.yaml",
        session_id="conv_abc",
        resume_picker=False,
        codex_args=(),
        model=None,
        prompt=None,
    )

    assert order == ["align", "ensure-daemon", "prepare", "attach"]


def test_run_with_local_server_records_fresh_session_before_attach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Local fresh sessions persist launch cwd before terminal attach.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary paths for fake server and Codex details.
    :returns: None.
    """
    import omnigent.chat as chat_mod

    order: list[str] = []

    class _FakeServer:
        """
        Minimal local server handle.

        :param proc: Fake server process.
        :param runner_id: Stable runner id.
        """

        proc = object()
        runner_id = "runner_local"

    async def fake_prepare_codex_terminal(**_kwargs: object) -> codex_native.PreparedCodexTerminal:
        """
        Return a freshly prepared Codex terminal.

        :returns: Prepared Codex terminal.
        """
        order.append("prepare")
        return codex_native.PreparedCodexTerminal(
            session_id="conv_fresh",
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
            bridge_dir=tmp_path / "bridge",
            thread_id="thread_123",
            app_server_url="ws://127.0.0.1:9876",
            app_server=None,
            event_client=None,
            reattached=False,
        )

    async def fake_attach_with_forwarder(**_kwargs: object) -> None:
        """
        Record attach after launch-state recording.

        :returns: None.
        """
        order.append("attach")

    monkeypatch.setattr(chat_mod, "_find_free_port", lambda: 9876)
    monkeypatch.setattr(chat_mod, "_start_local_server", lambda *_args, **_kwargs: _FakeServer())
    monkeypatch.setattr(chat_mod, "_wait_for_server", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(chat_mod, "_stop_local_server", lambda _server: None)
    monkeypatch.setattr(chat_mod, "_bundle_agent", lambda _path: b"bundle")
    monkeypatch.setattr(codex_native, "_resolve_session_id_for_resume", lambda **_kwargs: None)
    monkeypatch.setattr(codex_native, "_prepare_codex_terminal", fake_prepare_codex_terminal)
    monkeypatch.setattr(codex_native, "_attach_with_forwarder", fake_attach_with_forwarder)
    monkeypatch.setattr(
        codex_native,
        "_record_launch_for_fresh_session",
        lambda session_id: order.append(f"record:{session_id}"),
    )

    codex_native._run_with_local_server(
        tmp_path / "codex.yaml",
        session_id=None,
        resume_picker=False,
        codex_args=(),
        command="/opt/codex/bin/codex",
        model=None,
        prompt=None,
    )

    assert order == ["prepare", "record:conv_fresh", "attach"]


@pytest.mark.asyncio
async def test_prepare_codex_terminal_via_daemon_creates_runner_and_ensures_terminal(
    monkeypatch: pytest.MonkeyPatch,
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
            return httpx.Response(200, json={"id": "terminal_codex_main"})
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
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

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
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

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


def test_launch_codex_terminal_starts_fresh_remote_tui() -> None:
    """
    Fresh sessions let the Codex TUI create the remote app-server
    thread instead of resuming a pre-created rollout-less thread.

    :returns: None.
    """
    client = _FakeTerminalClient(httpx.Response(200, json={"id": "terminal_codex_main"}))

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=("-c", "approval_policy=on-request"),
            command="/opt/codex/bin/codex",
            thread_id=None,
            remote_url="ws://127.0.0.1:9876",
            env={"CODEX_HOME": "/tmp/codex-home"},
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert launched.tmux_socket is None
    assert launched.tmux_target is None
    assert client.posts[0][1]["spec"]["args"] == [
        "-c",
        "approval_policy=on-request",
        "--remote",
        "ws://127.0.0.1:9876",
    ]
    assert client.posts[0][1]["spec"]["command"] == "/opt/codex/bin/codex"
    assert client.posts[0][1]["spec"]["tmux_allow_passthrough"] is True
    assert client.posts[0][1]["spec"]["tmux_start_on_attach"] is True


def test_launch_codex_terminal_uses_remote_resume_order() -> None:
    """
    Terminal launch uses the Codex resume subcommand with ``--remote``
    before the thread id, matching Codex CLI parsing coverage.

    :returns: None.
    """
    client = _FakeTerminalClient(httpx.Response(200, json={"id": "terminal_codex_main"}))

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=("-c", "approval_policy=on-request"),
            command="/opt/codex/bin/codex",
            thread_id="thread_123",
            remote_url="ws://127.0.0.1:9876",
            env={"CODEX_HOME": "/tmp/codex-home"},
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert client.posts == [
        (
            "/v1/sessions/conv_abc/resources/terminals",
            {
                "terminal": "codex",
                "session_key": "main",
                "spec": {
                    "command": "/opt/codex/bin/codex",
                    "args": [
                        "-c",
                        "approval_policy=on-request",
                        "resume",
                        "--remote",
                        "ws://127.0.0.1:9876",
                        "thread_123",
                    ],
                    "os_env_type": "caller_process",
                    "cwd": str(Path.cwd()),
                    "env": {"CODEX_HOME": "/tmp/codex-home"},
                    "scrollback": 100_000,
                    "tmux_allow_passthrough": True,
                    "tmux_start_on_attach": True,
                },
            },
            30.0,
        )
    ]


def test_launch_codex_terminal_extracts_tmux_attach_metadata(
    tmp_path: Path,
) -> None:
    """
    Terminal launch returns the runner tmux coordinates needed for
    direct local attach.

    This fails if the Codex wrapper only keeps the terminal id and is
    forced back through the WebSocket terminal bridge even when the
    runner exposed a local tmux socket.

    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    socket_path = tmp_path / "tmux.sock"
    client = _FakeTerminalClient(
        httpx.Response(
            200,
            json={
                "id": "terminal_codex_main",
                "metadata": {
                    "tmux_socket": str(socket_path),
                    "tmux_target": "main",
                },
            },
        )
    )

    launched = asyncio.run(
        codex_native._launch_codex_terminal(
            client,  # type: ignore[arg-type]
            "conv_abc",
            codex_args=(),
            command="/opt/codex/bin/codex",
            thread_id="thread_123",
            remote_url="ws://127.0.0.1:9876",
            env={},
        )
    )

    assert launched.terminal_id == "terminal_codex_main"
    assert launched.tmux_socket == socket_path
    assert launched.tmux_target == "main"


def test_attach_with_forwarder_uses_direct_tmux_when_socket_is_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A local runner tmux socket bypasses the WebSocket terminal bridge.

    Breaking the direct attach branch would make this call invoke
    ``_attach_with_reconnect`` and fail the test before any fake tmux
    attach is recorded.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    socket_path = tmp_path / "tmux.sock"
    socket_path.touch()
    attached: list[tuple[Path, str]] = []

    async def fake_attach_direct_tmux(path: Path, target: str) -> None:
        """
        Record the direct tmux attach request.

        :param path: Tmux socket path.
        :param target: Tmux target.
        :returns: None.
        """
        attached.append((path, target))

    async def fail_attach_with_reconnect(**_kwargs: object) -> None:
        """
        Fail if the WebSocket bridge path is used.

        :returns: None.
        """
        raise AssertionError("WebSocket attach path should not be used")

    monkeypatch.setattr("omnigent.codex_native.shutil.which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(codex_native, "_attach_direct_tmux", fake_attach_direct_tmux)
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fail_attach_with_reconnect)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=socket_path,
                tmux_target="main",
                bridge_dir=tmp_path / "bridge",
                thread_id="thread_123",
                app_server_url="ws://127.0.0.1:9876",
                app_server=None,
                event_client=None,
                reattached=True,
            ),
            prompt=None,
        )
    )

    assert attached == [(socket_path, "main")]


def test_attach_with_forwarder_attaches_before_waiting_for_fresh_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Fresh tmux Codex sessions attach before waiting for ``thread/started``.

    The Codex pane waits for the first tmux client before starting, so
    waiting for the app-server thread before attaching would deadlock
    and would also force Codex to query terminal colors while detached.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    attach_started = asyncio.Event()
    allow_attach_exit = asyncio.Event()
    forwarded_threads: list[str | None] = []

    class _FakeAppServer:
        """Minimal app-server handle for attach cleanup."""

        async def close(self) -> None:
            """
            Close the fake app-server.

            :returns: None.
            """

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Record that the terminal attach started and wait to exit.

        :returns: None.
        """
        attach_started.set()
        await allow_attach_exit.wait()

    async def fake_initialize_fresh_terminal_thread(**kwargs: object) -> str:
        """
        Assert initialization runs only after the attach has started.

        :param kwargs: Initialization keyword arguments.
        :returns: Fake Codex thread id.
        """
        del kwargs
        assert attach_started.is_set()
        allow_attach_exit.set()
        return "thread_123"

    def fake_start_codex_forwarder(**kwargs: object) -> asyncio.Task[None]:
        """
        Record the thread id used for the forwarder.

        :param kwargs: Forwarder keyword arguments.
        :returns: Cancellable no-op task.
        """
        prepared = kwargs["prepared"]
        assert isinstance(prepared, codex_native.PreparedCodexTerminal)
        forwarded_threads.append(prepared.thread_id)
        return asyncio.create_task(asyncio.sleep(3600))

    async def fake_start_initial_turn(_socket_path: Path, _thread_id: str, _prompt: str) -> None:
        """
        Fail if an initial prompt is unexpectedly sent.

        :returns: None.
        """
        raise AssertionError("no initial prompt expected")

    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)
    monkeypatch.setattr(
        codex_native,
        "_initialize_fresh_terminal_thread",
        fake_initialize_fresh_terminal_thread,
    )
    monkeypatch.setattr(codex_native, "_start_codex_forwarder", fake_start_codex_forwarder)
    monkeypatch.setattr(codex_native, "_start_initial_turn", fake_start_initial_turn)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "tmux.sock",
                tmux_target="main",
                bridge_dir=tmp_path / "bridge",
                thread_id=None,
                app_server_url="ws://127.0.0.1:9876",
                app_server=_FakeAppServer(),  # type: ignore[arg-type]
                event_client=object(),  # type: ignore[arg-type]
                reattached=False,
            ),
            prompt=None,
        )
    )

    assert forwarded_threads == ["thread_123"]


def test_attach_with_forwarder_closes_active_rotated_session_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Wrapper exit closes the terminal on the active rotated Omnigent session.

    Codex ``/clear`` transfers the terminal resource to a replacement
    session. Shutdown must follow the bridge state written by the
    forwarder; closing the original session would leave the transferred
    terminal live.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake bridge state.
    :returns: None.
    """
    bridge_dir = tmp_path / "bridge"
    closed_terminals: list[tuple[str, str]] = []
    app_server_closed = False

    class _FakeAppServer:
        """Minimal app-server handle for attach cleanup."""

        async def close(self) -> None:
            """
            Record app-server cleanup.

            :returns: None.
            """
            nonlocal app_server_closed
            app_server_closed = True

    async def fake_attach_terminal_resource(**_kwargs: object) -> None:
        """
        Simulate ``/clear`` rotating Omnigent ownership during attach.

        :returns: None.
        """
        write_bridge_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id="conv_rotated",
                socket_path=str(tmp_path / "codex.sock"),
                thread_id="thread_after_clear",
                codex_home=str(tmp_path / "codex-home"),
            ),
        )

    def fake_start_codex_forwarder(**_kwargs: object) -> asyncio.Task[None]:
        """
        Return a cancellable no-op forwarder task.

        :returns: Running task that never completes on its own.
        """
        return asyncio.create_task(asyncio.sleep(3600))

    async def fake_close_codex_terminal(**kwargs: object) -> None:
        """
        Record the terminal close target.

        :param kwargs: Close helper keyword arguments.
        :returns: None.
        """
        closed_terminals.append(
            (
                str(kwargs["session_id"]),
                str(kwargs["terminal_id"]),
            )
        )

    monkeypatch.setattr(codex_native, "_attach_terminal_resource", fake_attach_terminal_resource)
    monkeypatch.setattr(codex_native, "_start_codex_forwarder", fake_start_codex_forwarder)
    monkeypatch.setattr(codex_native, "_close_codex_terminal", fake_close_codex_terminal)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_original",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "tmux.sock",
                tmux_target="main",
                bridge_dir=bridge_dir,
                thread_id="thread_before_clear",
                app_server_url="ws://127.0.0.1:9876",
                app_server=_FakeAppServer(),  # type: ignore[arg-type]
                event_client=None,
                reattached=False,
            ),
            prompt=None,
        )
    )

    assert closed_terminals == [("conv_rotated", "terminal_codex_main")]
    assert app_server_closed


def test_attach_terminal_resource_runner_owned_missing_socket_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Runner-owned Codex terminals do not fall back to WebSocket attach.

    The CLI should only attach to the runner's tmux socket for this
    shape; if the socket metadata is stale or non-local, falling back to
    the Omnigent terminal WebSocket would reintroduce CLI-owned terminal IO.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """

    async def fail_attach_with_reconnect(**_kwargs: object) -> None:
        """
        Fail if the WebSocket bridge path is used.

        :returns: None.
        """
        raise AssertionError("Runner-owned Codex attach must not use WebSocket")

    monkeypatch.setattr("omnigent.codex_native.shutil.which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fail_attach_with_reconnect)

    with pytest.raises(click.ClickException) as exc_info:
        asyncio.run(
            codex_native._attach_terminal_resource(
                base_url="http://127.0.0.1:8000",
                headers={},
                prepared=codex_native.PreparedCodexTerminal(
                    session_id="conv_abc",
                    terminal_id="terminal_codex_main",
                    tmux_socket=tmp_path / "missing.sock",
                    tmux_target="main",
                    bridge_dir=tmp_path / "bridge",
                    thread_id="thread_123",
                    app_server_url=None,
                    app_server=None,
                    event_client=None,
                    reattached=True,
                ),
                recover=None,
            )
        )

    message = str(exc_info.value)
    assert "Runner-owned Codex terminal requires direct tmux attach" in message
    assert str(tmp_path / "missing.sock") in message
    assert "WebSocket" not in message


def test_attach_with_forwarder_falls_back_when_tmux_socket_is_not_local(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Non-local runner sockets keep using the Omnigent terminal attach bridge.

    This is the remote-runner case: the resource may advertise a socket
    path from another host, but the CLI can only direct-attach when that
    path exists locally.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory used for fake socket paths.
    :returns: None.
    """
    websocket_attaches: list[str] = []
    bridge_dir = tmp_path / "bridge"
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_rotated",
            socket_path=str(tmp_path / "codex.sock"),
            thread_id="thread_456",
            codex_home=str(tmp_path / "codex-home"),
        ),
    )

    async def fail_attach_direct_tmux(_path: Path, _target: str) -> None:
        """
        Fail if direct tmux attach is attempted.

        :returns: None.
        """
        raise AssertionError("Direct tmux attach should not be used")

    async def fake_attach_with_reconnect(**kwargs: object) -> None:
        """
        Record the WebSocket attach URL.

        :param kwargs: Attach loop keyword arguments.
        :returns: None.
        """
        attach_url = kwargs["attach_url"]
        assert isinstance(attach_url, str)
        active_session_id_reader = kwargs["active_session_id_reader"]
        assert callable(active_session_id_reader)
        assert active_session_id_reader() == "conv_rotated"
        websocket_attaches.append(attach_url)

    monkeypatch.setattr("omnigent.codex_native.shutil.which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(codex_native, "_attach_direct_tmux", fail_attach_direct_tmux)
    monkeypatch.setattr(codex_native, "_attach_with_reconnect", fake_attach_with_reconnect)

    asyncio.run(
        codex_native._attach_with_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            prepared=codex_native.PreparedCodexTerminal(
                session_id="conv_abc",
                terminal_id="terminal_codex_main",
                tmux_socket=tmp_path / "missing.sock",
                tmux_target="main",
                bridge_dir=bridge_dir,
                thread_id="thread_123",
                app_server_url="ws://127.0.0.1:9876",
                app_server=None,
                event_client=None,
                reattached=True,
            ),
            prompt=None,
        )
    )

    assert websocket_attaches == [
        "ws://127.0.0.1:8000/v1/sessions/conv_abc/resources/terminals/terminal_codex_main/attach"
    ]


def test_session_usage_data_extracts_cumulative_tokens() -> None:
    """
    ``_session_usage_data_from_params`` surfaces Codex's cumulative input /
    output tokens as ``cumulative_*`` fields (for server-side cost pricing).

    Codex's ``tokenUsage.total`` is cumulative across the thread, so these are
    the session totals; without them codex-native ``session_usage.total_cost_usd``
    stays 0 (codex produces no ``response.completed`` for the Omnigent relay).
    """
    params = {
        "threadId": "thread_123",
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "outputTokens": 250,
                "contextWindow": 200000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["cumulative_input_tokens"] == 1000
    assert data["cumulative_output_tokens"] == 250
    # Existing context-ring fields still flow.
    assert data["context_tokens"] == 1000
    assert data["context_window"] == 200000
    # No ``cachedInputTokens`` in the frame ⇒ no cache field forwarded. A
    # failure here would mean the server splits a phantom cache bucket out of
    # the input total, under-counting non-cached input.
    assert "cumulative_cache_read_input_tokens" not in data


def test_session_usage_data_forwards_cached_input_tokens() -> None:
    """
    ``cachedInputTokens`` is forwarded as ``cumulative_cache_read_input_tokens``
    while ``cumulative_input_tokens`` stays the FULL input total.

    Codex's ``inputTokens`` is inclusive of cached tokens (codex-rs
    ``non_cached_input = input_tokens - cached_input_tokens``). The forwarder
    must report both faithfully so the server can split the cheaper cache-read
    portion out before pricing — otherwise cached tokens are billed at the full
    input rate (the cost over-report this fix targets).
    """
    params = {
        "threadId": "thread_123",
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "cachedInputTokens": 800,
                "outputTokens": 250,
                "contextWindow": 200000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    # Full input total preserved (NOT pre-subtracted) — the server owns the
    # split. If this were 200, the forwarder would have double-applied the
    # subtraction the server also does.
    assert data["cumulative_input_tokens"] == 1000
    # Cached count surfaced for the server to price at the cache-read rate.
    assert data["cumulative_cache_read_input_tokens"] == 800


def test_session_usage_data_without_output_tokens_omits_cumulative_output() -> None:
    """
    A usage notification lacking ``outputTokens`` yields no
    ``cumulative_output_tokens`` — the server then prices input only rather
    than treating a missing field as zero output.
    """
    params = {"tokenUsage": {"total": {"inputTokens": 500, "contextWindow": 200000}}}
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["cumulative_input_tokens"] == 500
    assert "cumulative_output_tokens" not in data


def test_session_usage_data_context_tokens_uses_last_turn_input() -> None:
    """
    ``context_tokens`` (the context-ring value) should reflect the LAST
    turn's input — how much of the window the latest request occupied —
    not the cumulative total across the whole thread.

    When ``tokenUsage.last`` is present, ``context_tokens`` comes from
    ``last.inputTokens``; ``cumulative_input_tokens`` still uses
    ``total.inputTokens`` for cost pricing.
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 4_800_000,
                "outputTokens": 200_000,
                "contextWindow": 1_178_000,
            },
            "last": {
                "inputTokens": 950_000,
                "outputTokens": 12_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    # Ring shows current context occupancy from the last turn.
    assert data["context_tokens"] == 950_000
    # Cost pricing uses cumulative totals.
    assert data["cumulative_input_tokens"] == 4_800_000
    assert data["cumulative_output_tokens"] == 200_000
    assert data["context_window"] == 1_178_000


def test_session_usage_data_context_tokens_falls_back_without_last() -> None:
    """
    When ``tokenUsage.last`` is absent (e.g. first frame before a turn
    completes), ``context_tokens`` falls back to ``total.inputTokens``.
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 1000,
                "outputTokens": 250,
                "contextWindow": 200_000,
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_tokens"] == 1000
    assert data["cumulative_input_tokens"] == 1000


def test_session_usage_data_context_tokens_falls_back_when_last_missing_input() -> None:
    """
    When ``tokenUsage.last`` is present but lacks a usable ``inputTokens``,
    ``context_tokens`` falls back to ``total.inputTokens`` rather than being
    omitted (which would leave the UI ring stuck on a stale value from a
    previous coalescer frame).
    """
    params = {
        "tokenUsage": {
            "total": {
                "inputTokens": 3000,
                "outputTokens": 500,
                "contextWindow": 200_000,
            },
            "last": {
                "outputTokens": 100,
                # inputTokens intentionally absent
            },
        },
    }
    data = codex_native_forwarder._session_usage_data_from_params(params)
    assert data is not None
    assert data["context_tokens"] == 3000


def test_usage_coalescer_flush_attaches_model_to_every_post() -> None:
    """
    ``flush`` attaches the recorded model to each token-bearing post.

    The server prices cumulative codex tokens into ``total_cost_usd`` only
    when the post carries a ``model`` (codex-native sessions have no
    ``llm.model`` to fall back on). Codex sends settings (model) and usage
    in separate frames, so the coalescer must remember the model and stamp
    it on every flush — including the second turn's post, where only the
    token counts changed and the dedup would otherwise omit the model.
    """
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture Omnigent usage posts from the coalescer.

        :param request: HTTP request sent by the coalescer.
        :returns: Accepted response.
        """
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Record two usage frames (model known) and flush each.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            coalescer = _usage_coalescer(client)
            coalescer.record(
                {"tokenUsage": {"total": {"inputTokens": 1000, "outputTokens": 250}}},
                model="gpt-5.1-codex",
            )
            await coalescer.flush()
            # A later turn: only token counts change, model arrives as None
            # on the usage frame, yet the post must still carry the model.
            coalescer.record(
                {"tokenUsage": {"total": {"inputTokens": 2000, "outputTokens": 600}}},
            )
            await coalescer.flush()

    asyncio.run(run())

    assert len(posted) == 2
    assert posted[0]["data"]["model"] == "gpt-5.1-codex"
    assert posted[0]["data"]["cumulative_input_tokens"] == 1000
    assert posted[0]["data"]["cumulative_output_tokens"] == 250
    # Second post still carries the model even though only tokens changed.
    assert posted[1]["data"]["model"] == "gpt-5.1-codex"
    assert posted[1]["data"]["cumulative_input_tokens"] == 2000
    assert posted[1]["data"]["cumulative_output_tokens"] == 600


# ── Codex subagent tracking and dedup ────────────────────────────────────────


def _collab_item_completed_event(
    *,
    parent_thread_id: str = "thread_parent",
    child_thread_id: str = "thread_child",
    item_id: str = "collab_1",
) -> dict[str, Any]:
    """
    Build a Codex ``collabAgentToolCall`` ``item/completed`` notification.

    :param parent_thread_id: Codex parent thread id.
    :param child_thread_id: Codex child thread id.
    :param item_id: Codex item id for the collab item.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": "spawnAgent",
                "senderThreadId": parent_thread_id,
                "receiverThreadIds": [child_thread_id],
            },
        },
    }


def _collab_item_started_event(
    *,
    parent_thread_id: str = "thread_parent",
    child_thread_id: str = "thread_child",
    item_id: str = "collab_1",
) -> dict[str, Any]:
    """
    Build a Codex ``collabAgentToolCall`` ``item/started`` notification.

    :param parent_thread_id: Codex parent thread id.
    :param child_thread_id: Codex child thread id.
    :param item_id: Codex item id for the collab item.
    :returns: App-server event payload.
    """
    return {
        "method": "item/started",
        "params": {
            "threadId": parent_thread_id,
            "turnId": "turn_parent",
            "item": {
                "type": "collabAgentToolCall",
                "id": item_id,
                "tool": "spawnAgent",
                "senderThreadId": parent_thread_id,
                "receiverThreadIds": [child_thread_id],
            },
        },
    }


def _child_agent_message_event(
    *,
    child_thread_id: str = "thread_child",
    turn_id: str = "turn_child",
    item_id: str = "child_msg",
    text: str = "child output",
) -> dict[str, Any]:
    """
    Build a Codex ``agentMessage`` notification from a child thread.

    :param child_thread_id: Codex child thread id.
    :param turn_id: Codex turn id.
    :param item_id: Codex item id.
    :param text: Assistant text content.
    :returns: App-server event payload.
    """
    return {
        "method": "item/completed",
        "params": {
            "threadId": child_thread_id,
            "turnId": turn_id,
            "item": {
                "type": "agentMessage",
                "id": item_id,
                "text": text,
            },
        },
    }


def _child_resume_response(
    *,
    child_thread_id: str = "thread_child",
    parent_thread_id: str = "thread_parent",
    turn_id: str = "turn_child",
    item_id: str = "child_msg",
    text: str = "child output",
    agent_nickname: str = "Euclid",
    agent_role: str = "explorer",
) -> dict[str, Any]:
    """
    Build a Codex ``thread/resume`` response for a child thread.

    :param child_thread_id: Codex child thread id.
    :param parent_thread_id: Codex parent thread id in the spawn source.
    :param turn_id: Turn id for the replayed item.
    :param item_id: Item id for the replayed item.
    :param text: Text content of the replayed item.
    :param agent_nickname: Codex-assigned agent nickname.
    :param agent_role: Codex-assigned agent role.
    :returns: JSON-RPC ``thread/resume`` response.
    """
    return {
        "result": {
            "thread": {
                "id": child_thread_id,
                "agentNickname": agent_nickname,
                "agentRole": agent_role,
                "source": {
                    "subAgent": {
                        "thread_spawn": {
                            "parent_thread_id": parent_thread_id,
                        }
                    }
                },
                "turns": [
                    {
                        "id": turn_id,
                        "items": [{"type": "agentMessage", "id": item_id, "text": text}],
                    }
                ],
            }
        }
    }


class _PerThreadFakeCodexClient(_FakeCodexAppServerClient):
    """
    Test double that returns per-thread ``thread/resume`` responses.

    The base ``_FakeCodexAppServerClient.request`` returns the same canned
    response for every call. This subclass dispatches by the requested
    ``threadId`` so tests can give child threads a different resume payload
    from the parent.

    :param thread_responses: Mapping from Codex thread id to JSON-RPC
        response payload, e.g.
        ``{"thread_child": {"result": {...}}}``.
    :param default_response: Fallback response for thread ids that have no
        explicit entry.
    """

    def __init__(
        self,
        thread_responses: dict[str, dict[str, Any]],
        default_response: dict[str, Any] | None = None,
    ) -> None:
        """
        Initialise with per-thread responses.

        :param thread_responses: Per-thread response map.
        :param default_response: Fallback response when the requested
            thread id is not in ``thread_responses``.
        :returns: None.
        """
        super().__init__(response=default_response or {"result": {"thread": None}})
        self.thread_responses = thread_responses

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Dispatch to a per-thread response or the default.

        :param method: JSON-RPC method, e.g. ``"thread/resume"``.
        :param params: JSON-RPC params.
        :returns: Per-thread or default JSON-RPC response.
        """
        self.requests.append((method, params))
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id in self.thread_responses:
            return self.thread_responses[thread_id]
        return self.response


def _transcript_posts(
    posted: list[tuple[str, dict[str, Any]]],
    session_id: str,
) -> list[dict[str, Any]]:
    """
    Filter Omnigent posts to the ``external_conversation_item`` events for one session.

    :param posted: All captured Omnigent posts as ``(path, body)`` tuples.
    :param session_id: Omnigent session id to filter for.
    :returns: List of ``external_conversation_item`` body dicts.
    """
    return [
        body
        for path, body in posted
        if path == f"/v1/sessions/{session_id}/events"
        and body["type"] == "external_conversation_item"
    ]


def _registration_posts(
    posted: list[tuple[str, dict[str, Any]]],
    parent_session_id: str,
) -> list[dict[str, Any]]:
    """
    Filter Omnigent posts to the ``external_codex_subagent_start`` events for a parent.

    :param posted: All captured Omnigent posts as ``(path, body)`` tuples.
    :param parent_session_id: Parent Omnigent session id to filter for.
    :returns: List of ``external_codex_subagent_start`` body dicts.
    """
    return [
        body
        for path, body in posted
        if path == f"/v1/sessions/{parent_session_id}/events"
        and body["type"] == "external_codex_subagent_start"
    ]


def _make_omnigent_handler(
    posted: list[tuple[str, dict[str, Any]]],
    child_session_id: str = "conv_child",
) -> Callable[[httpx.Request], httpx.Response]:
    """
    Build an Omnigent ``MockTransport`` handler that registers a child session on demand.

    :param posted: Mutable list collecting all captured Omnigent requests.
    :param child_session_id: Omnigent child session id to return for
        ``external_codex_subagent_start`` events.
    :returns: Request handler for ``httpx.MockTransport``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the request and return the appropriate mock response.

        :param request: Incoming HTTP request.
        :returns: Mock Omnigent response.
        """
        body = json.loads(request.content)
        posted.append((request.url.path, body))
        if body.get("type") == "external_codex_subagent_start":
            return httpx.Response(
                202, json={"queued": False, "child_session_id": child_session_id}
            )
        return httpx.Response(202, json={"queued": False})

    return handler


def test_forwarder_dedupes_replay_and_live_child_item(
    tmp_path: Path,
) -> None:
    """
    A child transcript item written during backfill replay is not rewritten
    by the matching live ``item/completed`` event.

    This is the primary regression test for the duplicate-write bug. The
    forwarder discovers a child thread via a ``collabAgentToolCall`` and
    replays its backlog. Seconds later the same item arrives live on the
    event stream. With a correct total dedup key (``threadId:turnId:item.id``
    derived identically in both paths) the second delivery should be
    silently dropped. The test fails when either path produces a key of
    ``None`` or a key derived from different field values.

    The critical assertion:

    - Exactly **one** ``external_conversation_item`` to ``conv_child``
      proves dedup fired on the live delivery.
    - If it were two, the total-key derivation differs between paths or
      ``claim_item_key`` was bypassed.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    codex_client = _PerThreadFakeCodexClient(
        thread_responses={"thread_child": _child_resume_response()}
    )
    events = [
        # Parent turn — causes child registration + backfill replay.
        _collab_item_completed_event(),
        # Live delivery of the SAME child item already written during replay.
        _child_agent_message_event(text="child output"),
    ]
    codex_client.events = events

    async def run() -> None:
        """
        Run supervise_forwarder against the event sequence.

        :returns: None.
        """
        await codex_native_forwarder.supervise_forwarder(
            base_url="http://127.0.0.1:8000",
            headers={},
            session_id="conv_parent",
            bridge_dir=tmp_path,
            app_server_url=str(tmp_path / "app-server.sock"),
            thread_id="thread_parent",
            client=codex_client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    # Exactly one item posted: the replay delivery.
    # Two would mean the live delivery was not deduped against the replay.
    assert len(child_posts) == 1, (
        f"Expected exactly 1 transcript post to conv_child (replay only); "
        f"got {len(child_posts)}. Duplicate write survived dedup."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "child output"


def test_forwarder_does_not_double_write_stable_id_item_delivered_twice(
    tmp_path: Path,
) -> None:
    """
    A child item with a stable ``id`` posted twice in the same turn is
    written to Omnigent only once.

    This is the primary stable-id dedup case: the same ``item/completed``
    event may arrive once from the backfill replay and once live. The
    dedup key ``threadId:turnId:item.id`` must be identical both times so
    the second delivery is dropped by ``claim_item_key``.

    Anonymous items (no ``id``) cannot be reliably deduped across replay
    and live because there is no stable identity to key on. Only items with
    Codex-assigned ``id`` fields are guaranteed to deduplicate correctly.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    state.note_child_thread("thread_child", "conv_child")

    stable_event = {
        "method": "item/completed",
        "params": {
            "threadId": "thread_child",
            "turnId": "turn_child",
            # Stable item id — dedup must key on this.
            "item": {"type": "agentMessage", "id": "msg_abc123", "text": "stable output"},
        },
    }

    async def run() -> None:
        """
        Deliver the same stable-id child event twice with a shared state.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            uc = codex_native_forwarder._SessionUsageCoalescer(client, "conv_parent")
            for _ in range(2):
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_parent",
                    bridge_dir=tmp_path,
                    event=stable_event,
                    usage_coalescer=uc,
                    expected_thread_id="thread_parent",
                    forwarder_state=state,
                    elicitation_tracker=_elicitation_tracker(),
                )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    # Exactly one post: the stable id is claimed on first delivery and
    # claim_item_key returns False on the second, preventing a duplicate.
    assert len(child_posts) == 1, (
        f"Expected 1 transcript post for stable-id item; "
        f"got {len(child_posts)}. Stable-id items are not deduped correctly."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "stable output"


def test_forwarder_child_thread_started_does_not_rotate_parent_session(
    tmp_path: Path,
) -> None:
    """
    A ``thread/started`` event from a Codex AgentControl child does not
    rotate the parent Omnigent session.

    Native ``/clear`` starts a new top-level thread and must rotate.
    Child threads also emit ``thread/started`` when they begin — those
    events carry ``source.subAgent.thread_spawn`` and must be ignored
    by the rotation check, otherwise the parent's Omnigent session would be
    replaced every time a child starts.

    The test fails if ``_maybe_rotate_session_on_thread_started`` returns
    ``True`` for a child ``thread/started`` event.
    """
    codex_write_bridge_state = write_bridge_state  # noqa: F841 — alias for clarity.
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_parent",
            socket_path=str(tmp_path / "sock"),
            thread_id="thread_parent",
            codex_home=str(tmp_path / "home"),
        ),
    )

    child_thread_started_event: dict[str, Any] = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}},
            }
        },
    }
    ap_posts: list[tuple[str, dict[str, Any]]] = []

    async def run() -> bool:
        """
        Drive the child thread-started event through the rotation check.

        :returns: Whether a session rotation occurred.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(ap_posts)),
        ) as ap_client:
            target = codex_native_forwarder._ForwarderTarget(
                session_id="conv_parent",
                thread_id="thread_parent",
                delta_coalescer=codex_native_forwarder._OutputTextDeltaCoalescer(
                    ap_client, "conv_parent"
                ),
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    ap_client, "conv_parent"
                ),
                elicitation_tracker=_elicitation_tracker(),
            )
            return await codex_native_forwarder._maybe_rotate_session_on_thread_started(
                ap_client=ap_client,
                target=target,
                bridge_dir=tmp_path,
                app_server_url=str(tmp_path / "sock"),
                event=child_thread_started_event,
            )

    rotated = asyncio.run(run())
    assert rotated is False, (
        "Expected child thread/started to NOT rotate the parent session; "
        "it rotated. _thread_started_is_subagent guard is missing or broken."
    )
    # No Omnigent calls should have been made during rotation detection.
    assert ap_posts == []


def test_forwarder_routes_live_child_items_to_child_session(
    tmp_path: Path,
) -> None:
    """
    Live ``item/completed`` events for a known child thread are routed
    to the child Omnigent session, not the parent.

    Once a child thread is registered in ``forwarder_state.subagents_by_thread``,
    the routing layer must direct any event carrying the child's ``threadId``
    to the child session. The test fails if live child items land on the
    parent session or are dropped silently.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    state.note_child_thread("thread_child", "conv_child")

    live_child_event = _child_agent_message_event(text="live child message")

    async def run() -> None:
        """
        Deliver one live child event through _handle_event with a registered child.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                event=live_child_event,
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    client, "conv_parent"
                ),
                elicitation_tracker=_elicitation_tracker(),
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )

    asyncio.run(run())

    child_posts = _transcript_posts(posted, "conv_child")
    parent_posts = _transcript_posts(posted, "conv_parent")

    assert len(child_posts) == 1, (
        f"Expected 1 transcript post to conv_child; got {len(child_posts)}. "
        "Live child items are not being routed to the child session."
    )
    assert child_posts[0]["data"]["item_data"]["content"][0]["text"] == "live child message"
    assert parent_posts == [], (
        f"Got {len(parent_posts)} transcript post(s) to conv_parent; "
        "child items must not route to the parent session."
    )


def test_forwarder_collab_item_started_registers_child_before_completed(
    tmp_path: Path,
) -> None:
    """
    ``item/started`` for a collab-agent spawn registers the child session
    so live child events can be routed immediately.

    Codex emits both ``item/started`` and ``item/completed`` for a
    ``collabAgentToolCall``. Registration at ``item/started`` lets the
    forwarder route child events that arrive before ``item/completed``.
    The test checks that after ``item/started`` is processed, the child
    thread is already in ``forwarder_state.subagents_by_thread``.
    """
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )

    started_event = _collab_item_started_event()

    async def run() -> None:
        """
        Drive the collab item/started through _handle_event.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(_make_omnigent_handler(posted)),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                event=started_event,
                usage_coalescer=codex_native_forwarder._SessionUsageCoalescer(
                    client, "conv_parent"
                ),
                elicitation_tracker=_elicitation_tracker(),
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )

    asyncio.run(run())

    regs = _registration_posts(posted, "conv_parent")
    assert len(regs) == 1, (
        f"Expected 1 child registration after item/started; got {len(regs)}. "
        "collab-agent children must be registered at item/started."
    )
    assert state.session_for_child_thread("thread_child") == "conv_child", (
        "Expected thread_child to be registered in forwarder_state after "
        "item/started, but it was not found."
    )


def test_completed_item_key_is_total_never_empty() -> None:
    """
    ``_completed_item_key`` returns a non-empty string for every item shape.

    The dedup key must be total: if it returned ``None`` or an empty string
    for any item, that item would bypass the dedup gate and could be written
    twice. This test covers three shapes:
    - item with a stable ``id`` (normal case)
    - item missing ``id`` (positional fallback via ``peek_anon_item_key``)
    - item missing ``id`` *and* ``turnId`` (worst-case envelope)
    """
    state = codex_native_forwarder._CodexForwarderState()

    # Case 1: stable item id — the normal production path.
    key1, is_anon1 = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"},
        {"id": "item-abc", "type": "agentMessage"},
        state,
    )
    assert isinstance(key1, str) and key1, (
        f"Key for stable item id must be a non-empty string; got {key1!r}"
    )
    assert not is_anon1, "Stable-id item must not be flagged anonymous"

    # Case 2: no item id — must produce a positional fallback key.
    key2, is_anon2 = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"},
        {"type": "agentMessage"},  # no id
        state,
    )
    assert isinstance(key2, str) and key2, (
        f"Key for anonymous item must be a non-empty string; got {key2!r}"
    )
    assert is_anon2, "Anonymous item must be flagged is_anon=True"

    # Case 3: no item id AND no turnId — must still produce a non-empty string.
    key3, _ = codex_native_forwarder._completed_item_key(
        {"threadId": "t"},  # no turnId
        {"type": "agentMessage"},  # no id
        state,
    )
    assert isinstance(key3, str) and key3, (
        f"Key without turnId must be a non-empty string; got {key3!r}"
    )


def test_completed_item_key_two_anon_items_same_turn_distinct() -> None:
    """
    Two anonymous items in the same (thread, turn) get distinct dedup keys
    when the counter is advanced between them.

    The positional counter is peeked (not advanced) during key derivation.
    Only after ``advance_anon_counter`` does the next peek return a new slot.
    This test simulates the correct claim-and-advance cycle for two sequential
    anonymous items in the same turn.
    """
    state = codex_native_forwarder._CodexForwarderState()
    params = {"threadId": "t", "turnId": "u"}
    item = {"type": "agentMessage"}  # no id

    # First item: peek key, claim, advance.
    key_a, _ = codex_native_forwarder._completed_item_key(params, item, state)
    assert state.claim_item_key(key_a)
    state.advance_anon_counter("t", "u")

    # Second item: peek key after counter advanced — must be different.
    key_b, _ = codex_native_forwarder._completed_item_key(params, item, state)

    assert key_a != key_b, (
        f"Two sequential anonymous items must get distinct keys after advancing "
        f"the counter; both got {key_a!r}. advance_anon_counter is not working."
    )


def test_completed_item_key_claimed_anon_slot_rejected_on_reclaim() -> None:
    """
    Once an anonymous item key is claimed, a second claim for the same key
    returns ``False``.

    The anonymous counter is only advanced after a successful claim, so
    peeking before advancing gives the same slot. This tests the dedup gate
    directly: after claim+advance the slot anon-0 is in ``synced_item_keys``
    and a re-claim must be rejected.

    Note: anonymous-item dedup across replay vs live is only reliable when
    both paths see the counter at the same value — which holds when they both
    call ``_handle_completed_item`` sequentially on the same connection (the
    counter advances after each successful claim). The primary dedup mechanism
    for production items is the stable ``item.id`` path, not this fallback.
    """
    state = codex_native_forwarder._CodexForwarderState()

    # Simulate the first delivery: peek the key, claim it, advance the counter.
    key_a, _ = codex_native_forwarder._completed_item_key(
        {"threadId": "t", "turnId": "u"}, {"type": "agentMessage"}, state
    )
    assert state.claim_item_key(key_a)
    state.advance_anon_counter("t", "u")

    # anon-0 is now in synced_item_keys — a direct re-claim must be rejected.
    assert not state.claim_item_key("t:u:anon-0"), (
        "anon-0 must be rejected after the first claim; the slot is already in synced_item_keys."
    )


def test_forwarder_resolves_child_thread_elicitation_on_child_session(
    tmp_path: Path,
) -> None:
    """
    A child-thread elicitation resolves on the child session, not the parent.

    When a collab child thread raises an elicitation, the approval card is
    published on the child Omnigent session (``route_session_id``). Codex's
    ``serverRequest/resolved`` must clear it on that same child session;
    resolving on the parent leaves the child's card stuck for any web user
    watching the child.
    """
    fake_client = _FakeCodexAppServerClient()
    hook_started = asyncio.Event()
    posted_paths: list[str] = []
    state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_parent")
    state.note_child_thread("thread_child", "conv_child")
    request_event = {
        "id": 7,
        "method": "mcpServer/elicitation/request",
        "params": {
            "threadId": "thread_child",
            "turnId": "turn_child",
            "serverName": "booking",
            "mode": "form",
            "message": "Pick a date",
            "requestedSchema": {"type": "object", "properties": {}},
        },
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        """
        Hold the child hook open; record the session path other events post to.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent event response for non-hook posts.
        """
        if request.url.path.endswith("/hooks/codex-elicitation-request"):
            hook_started.set()
            await asyncio.Future()  # pending approval — never resolves natively
        posted_paths.append(request.url.path)
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        """
        Drive a child-thread elicitation request, then its resolution.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            usage_coalescer = _usage_coalescer(client)
            # Routes to the child thread → the approval card is published on
            # conv_child (the hook POST blocks, simulating a pending prompt).
            await asyncio.wait_for(
                codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_parent",
                    bridge_dir=tmp_path,
                    usage_coalescer=usage_coalescer,
                    elicitation_tracker=elicitation_tracker,
                    event=request_event,
                    codex_client=fake_client,  # type: ignore[arg-type]
                    forwarder_state=state,
                ),
                timeout=0.2,
            )
            await asyncio.wait_for(hook_started.wait(), timeout=1.0)
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=usage_coalescer,
                elicitation_tracker=elicitation_tracker,
                event={
                    "method": "serverRequest/resolved",
                    "params": {"threadId": "thread_child", "requestId": 7},
                },
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.close()

    asyncio.run(run())

    # The resolution must post to the CHILD session's events. With the old
    # session_id=session_id (parent), it posts to conv_parent and the child's
    # approval card never clears for a web user watching the child.
    assert "/v1/sessions/conv_child/events" in posted_paths, (
        f"resolution should target the child session; got {posted_paths}"
    )
    assert "/v1/sessions/conv_parent/events" not in posted_paths, (
        f"resolution must not target the parent session; got {posted_paths}"
    )


def _write_source_rollout(*, codex_home: Path, thread_id: str, source_cwd: str) -> Path:
    """
    Write a realistic source Codex rollout for clone tests.

    Builds the on-disk shape Codex produces: a date-partitioned
    ``sessions/YYYY/MM/DD/rollout-<ts>-<thread_id>.jsonl`` whose first
    line is ``session_meta`` (carrying the thread ``id`` and ``cwd``),
    followed by a ``turn_context`` (structural ``cwd``) and two
    *historical* records that mention *source_cwd* inside their bodies —
    a developer message and a function_call_output. The historical
    mentions exist to prove the clone leaves them untouched.

    :param codex_home: The ``CODEX_HOME`` to write under, e.g.
        ``Path("/tmp/.../codex-home")``.
    :param thread_id: Thread id / rollout stem, e.g. ``"019e96aa-...."``.
    :param source_cwd: Working directory recorded in the source, e.g.
        ``"/repo/worktree-a"``.
    :returns: Path to the written rollout.
    """
    rollout_dir = codex_home / "sessions" / "2026" / "06" / "05"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    rollout = rollout_dir / f"rollout-2026-06-05T15-23-07-{thread_id}.jsonl"
    records = [
        {
            "timestamp": "2026-06-05T07:23:34.547Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "cwd": source_cwd, "originator": "test"},
        },
        {
            "timestamp": "2026-06-05T07:23:34.549Z",
            "type": "turn_context",
            "payload": {"turn_id": "turn_1", "cwd": source_cwd, "approval_policy": "on-request"},
        },
        {
            "timestamp": "2026-06-05T07:23:34.554Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": f"<env> cwd: {source_cwd} </env>"}],
            },
        },
        {
            "timestamp": "2026-06-05T07:23:40.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": f"{source_cwd}/tests/foo.py:42: AssertionError",
            },
        },
    ]
    with rollout.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")
    return rollout


def test_clone_codex_rollout_rewrites_id_and_structural_cwd_into_clone_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning rewrites only the thread id and structural cwd in the clone home.

    Proves the surgical rewrite: ``session_meta.id`` becomes the target
    id, ``session_meta.cwd`` and ``turn_context.cwd`` become the clone
    workspace, the rollout lands in the CLONE's CODEX_HOME under the
    target id, and record order is preserved.
    """
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    target_thread = "019eaa11-1111-7222-8333-444455556666"
    source_cwd = "/repo/worktree-source"
    clone_cwd = tmp_path / "worktree-clone"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    _write_source_rollout(codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd)
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id=target_thread,
        clone_codex_home=clone_home,
        clone_workspace=clone_cwd,
    )

    assert result is not None
    # Lands in the CLONE's home, under the target id, preserving the
    # source's date-partitioned layout.
    assert clone_home in result.parents, f"{result} not under clone home {clone_home}"
    assert source_home not in result.parents
    assert result.name == f"rollout-2026-06-05T15-23-07-{target_thread}.jsonl"

    records = [json.loads(line) for line in result.read_text().splitlines()]
    # Order preserved: session_meta, turn_context, message, function_call_output.
    assert [r["type"] for r in records] == [
        "session_meta",
        "turn_context",
        "response_item",
        "response_item",
    ]
    meta = records[0]["payload"]
    assert meta["id"] == target_thread, "session_meta.id must be rewritten to the target thread id"
    assert meta["cwd"] == str(clone_cwd), (
        "session_meta.cwd must be rewritten to the clone workspace"
    )
    assert records[1]["payload"]["cwd"] == str(clone_cwd), "turn_context.cwd must be rewritten"


def test_clone_codex_rollout_leaves_historical_cwd_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning never rewrites cwd inside message / tool-output bodies.

    The historical ``cwd`` mentions (a developer message and a
    function_call_output) record what actually happened in the source
    workspace; rewriting them would fabricate history. Only the two
    structural fields move.
    """
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    target_thread = "019eaa11-1111-7222-8333-444455556666"
    source_cwd = "/repo/worktree-source"
    clone_cwd = tmp_path / "worktree-clone"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    source_rollout = _write_source_rollout(
        codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd
    )
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id=target_thread,
        clone_codex_home=clone_home,
        clone_workspace=clone_cwd,
    )

    assert result is not None
    records = [json.loads(line) for line in result.read_text().splitlines()]
    developer_text = records[2]["payload"]["content"][0]["text"]
    tool_output = records[3]["payload"]["output"]
    # Both historical bodies still reference the SOURCE cwd verbatim.
    assert source_cwd in developer_text, "historical message body must be preserved"
    assert source_cwd in tool_output, "historical tool output must be preserved"
    assert str(clone_cwd) not in developer_text, "clone cwd must not leak into message history"
    assert str(clone_cwd) not in tool_output, "clone cwd must not leak into tool output"
    # Historical lines must be copied byte-for-byte — not re-serialized —
    # so whitespace / key order / escaping are preserved exactly. The
    # source fixture writes records with default (spaced) JSON separators;
    # if the cloner re-serialized them they would lose those spaces and
    # this byte comparison would fail. Indices 2 and 3 are the developer
    # message and the function_call_output (both historical).
    source_lines = source_rollout.read_text().splitlines()
    clone_lines = result.read_text().splitlines()
    assert clone_lines[2] == source_lines[2], "historical message line must be byte-identical"
    assert clone_lines[3] == source_lines[3], "historical tool-output line must be byte-identical"


def test_clone_codex_rollout_leaves_source_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source rollout is read-only — cloning never mutates it."""
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    source_cwd = "/repo/worktree-source"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    source_rollout = _write_source_rollout(
        codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd
    )
    before = source_rollout.read_bytes()
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id="019eaa11-1111-7222-8333-444455556666",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert source_rollout.read_bytes() == before, (
        "source rollout must be byte-identical after clone"
    )


def test_clone_codex_rollout_returns_none_when_source_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning returns None when the source rollout isn't on this host.

    The caller treats None as "launch fresh" — a fork to a host without
    the source rollout must not strand the clone pointing at a missing
    thread.
    """
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_missing",
        source_thread_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        target_thread_id="019eaa11-1111-7222-8333-444455556666",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert result is None


def test_clone_codex_rollout_returns_none_for_unsafe_target_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An unsafe target id is rejected before any filesystem write.

    Guards against path traversal via the minted id being interpolated
    into the rollout filename.
    """
    from omnigent.codex_native_bridge import bridge_dir_for_bridge_id, codex_home_for_bridge_dir

    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", tmp_path / "bridges")
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    _write_source_rollout(codex_home=source_home, thread_id=source_thread, source_cwd="/repo/src")
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id="../../etc/passwd",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert result is None


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_synthesizes_omnigent_history(
    tmp_path: Path,
) -> None:
    """
    Cross-machine Codex resume rebuilds a local rollout from Omnigent history.

    The server can know the Omnigent conversation and Codex thread id while the
    current host has no ``$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl``.
    This helper must fetch committed Omnigent items, follow pagination, and write
    the response items before ``codex resume <thread>`` launches. If it only
    checked for local rollout state, this test would leave no file to read.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME`` and cwd.
    """
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex-home"
    requested_urls: list[str] = []
    first_page = [
        {
            "id": "msg_user_1",
            "response_id": "codex_turn_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "open TODO.md"}],
        },
        {
            "id": "fc_shell_1",
            "response_id": "codex_turn_1",
            "type": "function_call",
            "name": "shell",
            "arguments": '{"command":"cat TODO.md"}',
            "call_id": "call_shell_1",
        },
    ]
    second_page = [
        {
            "id": "fco_shell_1",
            "response_id": "codex_turn_1",
            "type": "function_call_output",
            "call_id": "call_shell_1",
            "output": "contents",
        },
        {
            "id": "msg_assistant_1",
            "response_id": "codex_turn_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "TODO.md says contents"}],
        },
        {
            "id": "msg_user_interrupted",
            "response_id": "codex_turn_interrupted",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "apply risky change"}],
        },
        {
            "id": "msg_assistant_interrupted",
            "response_id": "codex_turn_interrupted",
            "type": "message",
            "role": "assistant",
            "interrupted": True,
            "content": [{"type": "output_text", "text": "partially applied"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve two chronological item pages.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        requested_urls.append(str(request.url))
        assert request.url.path == "/v1/sessions/conv_codex/items"
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(
                200,
                json={"data": first_page, "has_more": True, "last_id": "fc_shell_1"},
            )
        assert after == "fc_shell_1"
        return httpx.Response(200, json={"data": second_page, "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=workspace.resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert rollout is not None
    assert codex_home in rollout.parents
    assert rollout.name.endswith(f"-{thread_id}.jsonl")
    records = [
        json.loads(line)
        for line in rollout.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["type"] for record in records] == [
        "session_meta",
        "turn_context",
        "response_item",
        "event_msg",
        "response_item",
        "response_item",
        "response_item",
        "event_msg",
    ]
    assert records[0]["payload"]["id"] == thread_id
    assert records[0]["payload"]["cwd"] == str(workspace.resolve())
    # codex >= 0.133 refuses to parse a rollout whose session_meta lacks
    # timestamp/cli_version, and its thread-store backfill breaks resume
    # when model_provider is absent (verified against codex 0.136.0).
    assert records[0]["payload"]["timestamp"] == records[0]["timestamp"]
    assert records[0]["payload"]["cli_version"] == "0.0.0"
    assert records[0]["payload"]["model_provider"] == "omnigent_databricks"
    assert records[1]["payload"]["turn_id"] == "turn_1"
    assert records[1]["payload"]["cwd"] == str(workspace.resolve())
    assert records[2]["payload"] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "open TODO.md"}],
        "id": "msg_user_1",
    }
    # Visible-turn reconstruction reads event_msg mirrors, not
    # response_item history: without them codex resumes an empty thread.
    assert records[3]["payload"] == {
        "type": "user_message",
        "message": "open TODO.md",
        "images": [],
        "local_images": [],
        "text_elements": [],
    }
    assert records[4]["payload"] == {
        "type": "function_call",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_shell_1",
        "id": "fc_shell_1",
    }
    assert records[5]["payload"] == {
        "type": "function_call_output",
        "call_id": "call_shell_1",
        "output": "contents",
        "id": "fco_shell_1",
    }
    assert records[6]["payload"] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "TODO.md says contents"}],
        "id": "msg_assistant_1",
    }
    assert records[7]["payload"] == {
        "type": "agent_message",
        "message": "TODO.md says contents",
        "phase": "final_answer",
        "memory_citation": None,
    }
    restored_payloads = [
        record["payload"] for record in records if record["type"] == "response_item"
    ]
    assert all(payload.get("id") != "msg_user_interrupted" for payload in restored_payloads)
    assert all(payload.get("id") != "msg_assistant_interrupted" for payload in restored_payloads)
    restored_text = json.dumps(restored_payloads)
    assert "apply risky change" not in restored_text
    assert "partially applied" not in restored_text
    assert any("after=fc_shell_1" in url for url in requested_urls), (
        f"history pagination was not followed; requests were {requested_urls!r}"
    )


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_preserves_existing_rollout(
    tmp_path: Path,
) -> None:
    """
    Codex cold resume does not rewrite an existing local rollout.

    A local rollout is Codex runtime state, not a cache. If it already
    exists, the helper must return it untouched instead of fetching AP
    history and rewriting the file.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    """
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/stale/cwd",
    )
    before = existing.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if Omnigent history is fetched despite a local rollout.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        del request
        raise AssertionError("existing rollout should avoid Omnigent history fetch")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=(tmp_path / "workspace").resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert rollout == existing
    assert existing.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_item", "message"),
    [
        (
            {
                "id": "fc_bad",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "shell",
                "call_id": "call_shell_1",
            },
            "function_call 'fc_bad' has non-string arguments",
        ),
        (
            {
                "id": "fco_bad",
                "response_id": "codex_turn_1",
                "type": "function_call_output",
                "call_id": "call_shell_1",
            },
            "function_call_output 'fco_bad' has non-string output",
        ),
    ],
)
async def test_ensure_local_codex_resume_rollout_rejects_malformed_tool_history(
    tmp_path: Path,
    bad_item: dict[str, Any],
    message: str,
) -> None:
    """
    Codex rollout synthesis fails loudly for corrupt Omnigent tool history.

    Tool call ``arguments`` and tool ``output`` are required Omnigent string
    fields. Missing values must not be invented as ``{}`` or ``""``,
    because Codex would then resume a tool history that never happened.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    :param bad_item: Malformed Omnigent item to serve.
    :param message: Expected diagnostic substring.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve malformed Omnigent history.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent item page.
        """
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(200, json={"data": [bad_item], "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match=message):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
                codex_home=tmp_path / "codex-home",
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_rejects_unsafe_thread_id(
    tmp_path: Path,
) -> None:
    """
    Codex cold resume fails loudly for an unsafe persisted thread id.

    The caller relies on this helper to make ``codex resume <thread>`` viable.
    Returning ``None`` would silently launch Codex without the guaranteed
    rollout this path is responsible for preparing.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if Omnigent history is fetched for an unsafe thread id.

        :param request: Incoming mock HTTP request.
        :returns: Never returns.
        """
        del request
        raise AssertionError("unsafe thread id should be rejected before Omnigent fetch")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="not a safe Codex rollout id"):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id="../../etc/passwd",
                codex_home=tmp_path / "codex-home",
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )


def test_mint_codex_thread_id_is_uuidv7() -> None:
    """
    Minted thread ids are valid UUIDv7 strings.

    Codex thread ids are UUIDv7; the clone resumes via
    ``codex resume <minted_id>``, so the format must match what Codex
    accepts (verified end-to-end by the opt-in fork e2e).
    """
    import uuid as _uuid

    minted = codex_native._mint_codex_thread_id()
    parsed = _uuid.UUID(minted)
    assert parsed.version == 7
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)


def test_command_execution_appends_sandbox_bypass_guidance_on_namespace_error() -> None:
    """A codex shell command that fails because codex's own command sandbox
    cannot start (no unprivileged user namespaces in a hardened container) gets
    actionable recovery guidance appended, instead of surfacing only the opaque
    ``bwrap: No permissions to create new namespace`` output (issue #657)."""
    item = {
        "command": "/bin/zsh -lc 'echo hi'",
        "aggregatedOutput": (
            "bwrap: No permissions to create new namespace, likely because the "
            "kernel does not allow non-privileged user namespaces.\n"
        ),
        "exitCode": 1,
    }
    tool_call = codex_native_forwarder._command_execution_tool_call("call_1", item)
    assert tool_call is not None
    # The raw bwrap output and the exit code are preserved verbatim...
    assert "No permissions to create new namespace" in tool_call.output
    assert "[exit code: 1]" in tool_call.output
    # ...with actionable recovery guidance appended (the "Full access" preset
    # and the config sandbox_mode workaround).
    assert "Full access" in tool_call.output
    assert "danger-full-access" in tool_call.output


def test_command_execution_leaves_normal_output_untouched() -> None:
    """A successful command keeps its output verbatim — the guidance only fires
    on the sandbox-namespace failure, never on ordinary output (issue #657)."""
    item = {"command": "pwd", "aggregatedOutput": "/repo\n", "exitCode": 0}
    tool_call = codex_native_forwarder._command_execution_tool_call("call_1", item)
    assert tool_call is not None
    assert tool_call.output == "/repo\n"
    assert "Full access" not in tool_call.output
    assert "danger-full-access" not in tool_call.output


def test_forwarder_mirrors_codex_context_compaction(tmp_path: Path) -> None:
    """
    Codex context-compaction surfaces as external_compaction_status (#1255).

    A ``contextCompaction`` item/started shows the spinner (in_progress) and
    the ``thread/compacted`` notification clears it (completed). Both signals
    were previously dropped, so the web UI never indicated Codex compacted —
    increasingly relevant with GPT-5.1-Codex-Max auto-compaction.
    """
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_123",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    forwarder_state = codex_native_forwarder._CodexForwarderState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record /events bodies; 202 for events, 200 otherwise."""
        if request.url.path.endswith("/events"):
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(200, json={})

    async def run() -> None:
        """Drive a compaction start item then the completion notification."""
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {"type": "contextCompaction", "id": "item_c"},
                    },
                },
                {"method": "thread/compacted", "params": {"threadId": "thread_123"}},
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    compaction = [p for p in posted if p.get("type") == "external_compaction_status"]
    assert compaction == [
        {"type": "external_compaction_status", "data": {"status": "in_progress"}},
        {"type": "external_compaction_status", "data": {"status": "completed"}},
    ]
