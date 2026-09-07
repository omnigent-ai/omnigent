"""Focused tests for recovery-aware ``omnigent open``."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

import omnigent.cli as cli_module
from omnigent.cli import _HostHttpResult, cli
from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER

_BASE_URL = "http://localhost:8000"


@pytest.fixture(autouse=True)
def _isolate_open_from_user_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.cli._load_effective_config", dict)
    monkeypatch.setattr("omnigent.cli._host_http_headers_cache", {})
    monkeypatch.setattr("omnigent.cli._host_http_keyless_demotions", set())
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="some-other-host"),
    )


def _script_http(
    monkeypatch: pytest.MonkeyPatch,
    *responses: _HostHttpResult,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    remaining = iter(responses)

    def request(**kwargs: object) -> _HostHttpResult:
        calls.append(kwargs)
        return next(remaining)

    monkeypatch.setattr("omnigent.cli._host_http_json", request)
    return calls


@pytest.mark.parametrize(
    "recovery",
    ["already_connected", "native_terminal_ready", "runner_relaunched"],
)
def test_open_recovers_then_delegates_to_existing_attach_client(
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    """Open ensures readiness, then delegates to the matching attach surface."""
    calls = _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_old", "runner_id": "runner_old"}),
        _HostHttpResult(202, {"queued": False, "recovery": recovery}),
    )
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(
        cli,
        ["open", "conv_123", "--server", _BASE_URL, "--tools", "coding", "--debug-events"],
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["params"] == {
        "include_items": "false",
        "include_liveness": "false",
    }
    assert calls[1] == {
        "base_url": _BASE_URL,
        "method": "POST",
        "path": "/v1/sessions/conv_123/events",
        "json_body": {"type": "retry_session", "data": {}},
        "timeout_s": 120.0,
        "host_id": "host_old",
    }
    run_attach.assert_called_once_with(
        base_url=_BASE_URL,
        conversation_id="conv_123",
        client_tools="coding",
        debug_events=True,
        auto_open_conversation=False,
        suppress_slice_key_for_host=None,
    )


def test_open_shared_native_editor_fails_before_recovery_or_attach(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(
        monkeypatch,
        _HostHttpResult(
            200,
            {
                "host_id": "host_remote",
                "labels": {"omnigent.wrapper": "codex-native-ui"},
                "permission_level": 3,
            },
        ),
    )
    run_attach = Mock()
    native_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)
    monkeypatch.setattr("omnigent.claude_native._attach_with_reconnect", native_attach)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code != 0
    assert "Only the session owner can open its native TUI" in result.output
    assert "omnigent attach conv_123 --server http://localhost:8000" in result.output
    assert "attributed transcript collaboration" in result.output
    assert len(calls) == 1
    assert calls[0]["method"] == "GET"
    run_attach.assert_not_called()
    native_attach.assert_not_called()


def test_open_plain_session_preserves_proven_keyless_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy host stays keyless when recovery hands off to the REPL."""
    _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_legacy", "runner_id": "runner_old"}),
        _HostHttpResult(202, {"queued": False, "recovery": "already_connected"}),
    )
    cli_module._host_http_keyless_demotions.add((_BASE_URL, "host_legacy"))
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code == 0, result.output
    run_attach.assert_called_once_with(
        base_url=_BASE_URL,
        conversation_id="conv_123",
        client_tools=None,
        debug_events=False,
        auto_open_conversation=False,
        suppress_slice_key_for_host="host_legacy",
    )


def test_open_waits_for_original_host_without_repeating_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_old", "runner_id": "runner_old"}),
        _HostHttpResult(
            503,
            {"error": {"code": "runner_unavailable", "message": "host is offline"}},
        ),
        _HostHttpResult(200, {"host_id": "host_old", "runner_id": "runner_new"}),
        _HostHttpResult(202, {"queued": False, "recovery": "runner_relaunched"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("omnigent.cli.time.sleep", sleeps.append)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code == 0, result.output
    assert sleeps == [5.0]
    assert result.output.count("Reconnect its original machine") == 1
    assert "host --background --server" in result.output
    assert "Session is ready; attaching." in result.output
    assert calls[3]["host_id"] == "host_old"
    run_attach.assert_called_once()


def test_open_wait_survives_transient_server_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable pane keeps polling when the known server briefly disappears."""
    calls = _script_http(
        monkeypatch,
        _HostHttpResult(0, "connection refused"),
        _HostHttpResult(200, {"host_id": "host_old"}),
        _HostHttpResult(202, {"queued": False, "recovery": "already_connected"}),
    )
    sleeps: list[float] = []
    monkeypatch.setattr("omnigent.cli.time.sleep", sleeps.append)
    previous = cli_module._OpenRecoveryAttempt(
        ready=False,
        detail="host is offline",
        host_id="host_old",
        wrapper_label=None,
    )

    recovered = cli_module._wait_for_open_session(
        base_url=_BASE_URL,
        conversation_id="conv_123",
        attempt=previous,
        no_wait=False,
    )

    assert recovered.ready is True
    assert sleeps == [5.0, 5.0]
    assert len(calls) == 3


def test_open_no_wait_returns_actionable_runner_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_old", "runner_id": "runner_old"}),
        _HostHttpResult(
            503,
            {"error": {"code": "runner_unavailable", "message": "host is offline"}},
        ),
    )
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(
        cli,
        ["open", "conv_123", "--server", _BASE_URL, "--no-wait"],
    )

    assert result.exit_code != 0
    assert "Session 'conv_123' is offline: host is offline" in result.output
    assert "Waiting for it to reconnect" not in result.output
    run_attach.assert_not_called()


def test_open_only_waits_for_runner_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_old", "runner_id": "runner_old"}),
        _HostHttpResult(
            410,
            {"error": {"code": "workspace_missing", "message": "workspace was deleted"}},
        ),
    )
    sleep = Mock()
    monkeypatch.setattr("omnigent.cli.time.sleep", sleep)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code != 0
    assert "workspace was deleted" in result.output
    sleep.assert_not_called()


def test_open_reconnects_local_matching_host_before_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_local", "runner_id": "runner_old"}),
        _HostHttpResult(
            503,
            {"error": {"code": "runner_unavailable", "message": "host is offline"}},
        ),
        _HostHttpResult(200, {"host_id": "host_local", "runner_id": "runner_new"}),
        _HostHttpResult(202, {"queued": False, "recovery": "runner_relaunched"}),
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    ensure_daemon = Mock(return_value=False)
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", ensure_daemon)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code == 0, result.output
    assert "reconnecting its host in the background" in result.output
    ensure_daemon.assert_called_once_with(_BASE_URL)
    run_attach.assert_called_once()


def test_open_does_not_create_identity_while_checking_remote_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery remains read-only on a machine with no existing host identity."""
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: None,
    )
    ensure_daemon = Mock()
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", ensure_daemon)

    reconnected = cli_module._maybe_reconnect_open_session_local_host(
        base_url=_BASE_URL,
        host_id="host_remote",
    )

    assert reconnected == cli_module._OpenLocalReconnect(attempted=False)
    ensure_daemon.assert_not_called()


def test_open_exits_for_rerun_when_local_reconnect_restarts_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Config drift cannot leave an open pane polling the replaced URL."""
    _script_http(
        monkeypatch,
        _HostHttpResult(200, {"host_id": "host_local", "runner_id": "runner_old"}),
        _HostHttpResult(
            503,
            {"error": {"code": "runner_unavailable", "message": "host is offline"}},
        ),
    )
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", Mock(return_value=True))
    monkeypatch.setattr(
        "omnigent.cli._discover_local_server_url",
        Mock(return_value="http://127.0.0.1:9123"),
    )
    update_url = Mock()
    monkeypatch.setattr("omnigent.cli._update_daemon_resolved_server_url", update_url)
    exit_for_change = Mock(side_effect=SystemExit(0))
    monkeypatch.setattr("omnigent.cli._exit_for_auth_mode_change", exit_for_change)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)

    result = CliRunner().invoke(cli, ["open", "conv_123", "--server", _BASE_URL])

    assert result.exit_code == 0, result.output
    update_url.assert_called_once_with("local", "http://127.0.0.1:9123")
    exit_for_change.assert_called_once_with(
        "http://127.0.0.1:9123",
        rerun_command=("omnigent open conv_123 --server http://127.0.0.1:9123"),
    )
    run_attach.assert_not_called()


@pytest.mark.asyncio
async def test_native_reconnect_exits_on_caller_after_local_server_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker reports a replacement URL; the event-loop thread exits."""
    import threading

    caller_thread = threading.get_ident()
    monkeypatch.setattr(
        "omnigent.host.identity.load_host_identity_if_present",
        lambda: SimpleNamespace(host_id="host_local"),
    )
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", Mock(return_value=True))
    monkeypatch.setattr(
        "omnigent.cli._discover_local_server_url",
        Mock(return_value="http://127.0.0.1:9123"),
    )
    monkeypatch.setattr("omnigent.cli._update_daemon_resolved_server_url", Mock())

    def exit_for_change(*_args: object, **_kwargs: object) -> None:
        assert threading.get_ident() == caller_thread
        raise SystemExit(0)

    monkeypatch.setattr("omnigent.cli._exit_for_auth_mode_change", exit_for_change)

    with pytest.raises(SystemExit) as exc_info:
        await cli_module._wait_for_open_session_async(
            base_url=_BASE_URL,
            conversation_id="conv_123",
            attempt=cli_module._OpenRecoveryAttempt(
                ready=False,
                detail="host is offline",
                host_id="host_local",
                wrapper_label="codex-native-ui",
            ),
        )

    assert exc_info.value.code == 0


@pytest.mark.asyncio
async def test_native_reconnect_wait_cancels_while_host_is_offline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleep_started = asyncio.Event()

    async def wait_forever(delay: float) -> None:
        assert delay == 5.0
        sleep_started.set()
        await asyncio.Future()

    monkeypatch.setattr(asyncio, "sleep", wait_forever)
    recover = Mock()
    monkeypatch.setattr("omnigent.cli._recover_open_session_once", recover)
    task = asyncio.create_task(
        cli_module._wait_for_open_session_async(
            base_url=_BASE_URL,
            conversation_id="conv_123",
            attempt=cli_module._OpenRecoveryAttempt(
                ready=False,
                detail="host is offline",
                host_id="host_remote",
                wrapper_label="codex-native-ui",
            ),
        )
    )

    await sleep_started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.done()
    recover.assert_not_called()


@pytest.mark.asyncio
async def test_native_recovery_cancellation_abandons_blocking_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling native recovery does not await its 120-second sync request."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def blocking_recover(**_kwargs: object) -> _HostHttpResult:
        started.set()
        release.wait(timeout=5.0)
        raise AssertionError("abandoned result must not be delivered")

    monkeypatch.setattr("omnigent.cli._recover_open_session_once", blocking_recover)
    task = asyncio.create_task(
        cli_module._recover_open_session_once_async(
            base_url=_BASE_URL,
            conversation_id="conv_123",
        )
    )
    while not started.is_set():
        await asyncio.sleep(0)

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        assert task.done()
    finally:
        release.set()


def test_open_native_session_refreshes_headers_without_local_rebind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_http(
        monkeypatch,
        _HostHttpResult(
            200,
            {
                "host_id": "host_remote",
                "runner_id": "runner_remote",
                "labels": {"omnigent.wrapper": "codex-native-ui"},
                "permission_level": None,
            },
        ),
        _HostHttpResult(202, {"queued": False, "recovery": "native_terminal_ready"}),
        _HostHttpResult(
            200,
            {
                "host_id": "host_remote",
                "runner_id": "runner_remote",
                "labels": {"omnigent.wrapper": "codex-native-ui"},
                "permission_level": None,
            },
        ),
        _HostHttpResult(202, {"queued": False, "recovery": "already_connected"}),
    )
    attached: list[tuple[str, dict[str, str]]] = []
    outcomes = iter((False, True))

    async def attach_local_terminal(url: str, *, headers: dict[str, str]) -> bool:
        attached.append((url, dict(headers)))
        return next(outcomes)

    monkeypatch.setattr("omnigent.claude_native.attach_local_terminal", attach_local_terminal)
    reconnect_calls: list[dict[str, object]] = []

    async def attach_with_reconnect(**kwargs: object) -> None:
        reconnect_calls.append(kwargs)
        attach = kwargs["attach"]
        recover = kwargs["recover"]
        assert callable(attach)
        assert callable(recover)
        assert await attach(kwargs["attach_url"], headers=kwargs["headers"]) is False
        # The management request in the real recovery callback replaces this
        # cache entry after the server rejects its expired bearer.
        cli_module._host_http_headers_cache[(_BASE_URL, "host_remote")] = {
            "Authorization": "Bearer fresh",
            OMNIGENT_SLICE_KEY_HEADER: "host_remote",
        }
        await recover()
        assert await attach(kwargs["attach_url"], headers=kwargs["headers"]) is True

    monkeypatch.setattr("omnigent.claude_native._attach_with_reconnect", attach_with_reconnect)
    cli_module._host_http_keyless_demotions.add((_BASE_URL, "host_remote"))
    remote_headers = Mock(
        return_value={
            "Authorization": "Bearer stale",
            OMNIGENT_SLICE_KEY_HEADER: "host_remote",
        }
    )
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)
    run_attach = Mock()
    monkeypatch.setattr("omnigent.chat.run_attach", run_attach)
    ensure_daemon = Mock()
    monkeypatch.setattr("omnigent.cli._ensure_host_daemon", ensure_daemon)

    result = CliRunner().invoke(cli, ["open", "conv/123", "--server", _BASE_URL])

    assert result.exit_code == 0, result.output
    assert attached == [
        (
            "ws://localhost:8000/v1/sessions/conv%2F123/"
            "resources/terminals/terminal_codex_main/attach",
            {"Authorization": "Bearer stale"},
        ),
        (
            "ws://localhost:8000/v1/sessions/conv%2F123/"
            "resources/terminals/terminal_codex_main/attach",
            {"Authorization": "Bearer fresh"},
        ),
    ]
    assert remote_headers.call_count == 1
    remote_headers.assert_called_with(server_url=_BASE_URL, host_id="host_remote")
    assert len(reconnect_calls) == 1
    assert reconnect_calls[0]["base_url"] == _BASE_URL
    assert reconnect_calls[0]["session_id"] == "conv/123"
    assert reconnect_calls[0]["terminal_id"] == "terminal_codex_main"
    assert reconnect_calls[0]["close_attach_on_terminal_gone"] is True
    # The native wrapper's normal resume path starts this machine's daemon and
    # can rebind. ``open`` instead attaches the terminal the remote host owns.
    ensure_daemon.assert_not_called()
    run_attach.assert_not_called()


class _FakeHttpResponse:
    def __init__(
        self,
        status_code: int,
        body: dict[str, object],
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = body
        self.text = ""
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}

    def json(self) -> dict[str, object]:
        return self.body


def _patch_host_http_client(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[_FakeHttpResponse],
) -> list[dict[str, str]]:
    """Install a scripted sync client and return its per-request headers."""
    remaining = iter(responses)
    client_headers: list[dict[str, str]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            client_headers.append(dict(kwargs["headers"]))  # type: ignore[arg-type]

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> _FakeHttpResponse:
            return next(remaining)

    monkeypatch.setattr("httpx.Client", FakeClient)
    return client_headers


def test_host_http_json_refreshes_rejected_auth_once_then_reuses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_headers = _patch_host_http_client(
        monkeypatch,
        [
            _FakeHttpResponse(
                302,
                {},
                headers={"Location": "/oidc/oauth2/v2.0/authorize"},
            ),
            _FakeHttpResponse(200, {"ok": True}),
            _FakeHttpResponse(200, {"ok": True}),
        ],
    )
    remote_headers = Mock(
        side_effect=[
            {"Authorization": "Bearer stale"},
            {"Authorization": "Bearer fresh"},
        ]
    )
    refresh_stored_token = Mock()
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)
    monkeypatch.setattr(
        "omnigent.chat._refreshable_stored_token",
        refresh_stored_token,
    )

    first = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/sessions/conv_123",
    )
    second = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/sessions/conv_123",
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert [headers.get("Authorization") for headers in client_headers] == [
        "Bearer stale",
        "Bearer fresh",
        "Bearer fresh",
    ]
    assert remote_headers.call_count == 2
    refresh_stored_token.assert_called_once_with(
        _BASE_URL,
        force_refresh=True,
        rejected_token="stale",
    )
    assert cli_module._host_http_headers_cache[(_BASE_URL, None)]["Authorization"] == (
        "Bearer fresh"
    )


@pytest.mark.parametrize(
    ("status_code", "response_headers"),
    [
        (401, {}),
        (403, {}),
        (302, {"Location": "https://workspace.example.com/.auth/callback"}),
    ],
)
def test_host_http_json_replays_persistent_auth_rejection_only_once(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
    response_headers: dict[str, str],
) -> None:
    client_headers = _patch_host_http_client(
        monkeypatch,
        [
            _FakeHttpResponse(status_code, {}, headers=response_headers),
            _FakeHttpResponse(status_code, {}, headers=response_headers),
        ],
    )
    remote_headers = Mock(
        side_effect=[
            {"Authorization": "Bearer stale"},
            {"Authorization": "Bearer still-rejected"},
        ]
    )
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/sessions/conv_123",
    )

    assert result.status_code == status_code
    assert [headers.get("Authorization") for headers in client_headers] == [
        "Bearer stale",
        "Bearer still-rejected",
    ]
    assert remote_headers.call_count == 2


def test_host_http_json_does_not_replay_identical_rejected_bearer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_headers = _patch_host_http_client(
        monkeypatch,
        [_FakeHttpResponse(401, {})],
    )
    remote_headers = Mock(return_value={"Authorization": "Bearer unchanged"})
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)
    monkeypatch.setattr("omnigent.chat._refreshable_stored_token", Mock(return_value=None))

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/sessions/conv_123",
    )

    assert result.status_code == 401
    assert client_headers == [{"Authorization": "Bearer unchanged"}]
    assert remote_headers.call_count == 2


def test_host_http_json_does_not_refresh_for_unrelated_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client_headers = _patch_host_http_client(
        monkeypatch,
        [
            _FakeHttpResponse(
                307,
                {},
                headers={"Location": "/v1/sessions/conv_other"},
            )
        ],
    )
    remote_headers = Mock(return_value={"Authorization": "Bearer current"})
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/sessions/conv_123",
    )

    assert result.status_code == 307
    assert [headers.get("Authorization") for headers in client_headers] == ["Bearer current"]
    remote_headers.assert_called_once_with(server_url=_BASE_URL, host_id=None)


def test_host_http_json_retries_wrong_replica_keyless_and_remembers_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            _FakeHttpResponse(400, {"error": {"code": "wrong_replica"}}),
            _FakeHttpResponse(202, {"recovery": "already_connected"}),
            _FakeHttpResponse(202, {"recovery": "already_connected"}),
        ]
    )
    client_headers: list[dict[str, str]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            client_headers.append(dict(kwargs["headers"]))  # type: ignore[arg-type]

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> _FakeHttpResponse:
            return next(responses)

    monkeypatch.setattr("httpx.Client", FakeClient)
    remote_headers = Mock(
        return_value={
            "Authorization": "Bearer token",
            OMNIGENT_SLICE_KEY_HEADER: "host_legacy",
        }
    )
    monkeypatch.setattr("omnigent.chat._remote_headers", remote_headers)

    first = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="POST",
        path="/v1/sessions/conv_123/events",
        host_id="host_legacy",
    )
    second = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="POST",
        path="/v1/sessions/conv_123/events",
        host_id="host_legacy",
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert [headers.get(OMNIGENT_SLICE_KEY_HEADER) for headers in client_headers] == [
        "host_legacy",
        None,
        None,
    ]
    assert (_BASE_URL, "host_legacy") in cli_module._host_http_keyless_demotions
    remote_headers.assert_called_once_with(server_url=_BASE_URL, host_id="host_legacy")


def test_host_http_json_clears_stale_keyless_demotion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        [
            _FakeHttpResponse(400, {"error": {"code": "wrong_replica"}}),
            _FakeHttpResponse(200, {"ok": True}),
        ]
    )
    client_headers: list[dict[str, str]] = []

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            client_headers.append(dict(kwargs["headers"]))  # type: ignore[arg-type]

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def request(self, *_args: object, **_kwargs: object) -> _FakeHttpResponse:
            return next(responses)

    monkeypatch.setattr("httpx.Client", FakeClient)
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        Mock(return_value={OMNIGENT_SLICE_KEY_HEADER: "host_modern"}),
    )
    demotion = (_BASE_URL, "host_modern")
    cli_module._host_http_keyless_demotions.add(demotion)

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/hosts/host_modern",
        host_id="host_modern",
    )

    assert result.status_code == 200
    assert [headers.get(OMNIGENT_SLICE_KEY_HEADER) for headers in client_headers] == [
        None,
        "host_modern",
    ]
    assert demotion not in cli_module._host_http_keyless_demotions


def test_host_http_json_uses_request_local_demotion_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A concurrent state update cannot suppress this request's fallback."""
    client_headers = _patch_host_http_client(
        monkeypatch,
        [
            _FakeHttpResponse(400, {"error": {"code": "wrong_replica"}}),
            _FakeHttpResponse(200, {"ok": True}),
        ],
    )
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        Mock(return_value={OMNIGENT_SLICE_KEY_HEADER: "host_modern"}),
    )
    demotion = (_BASE_URL, "host_modern")
    cli_module._host_http_keyless_demotions.add(demotion)
    resolve_headers = cli_module._resolve_host_request_headers

    def resolve_then_clear(**kwargs: object) -> tuple[dict[str, str], bool]:
        headers, was_demoted = resolve_headers(**kwargs)  # type: ignore[arg-type]
        cli_module._host_http_keyless_demotions.discard(demotion)
        return headers, was_demoted

    monkeypatch.setattr(cli_module, "_resolve_host_request_headers", resolve_then_clear)

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/hosts/host_modern",
        host_id="host_modern",
    )

    assert result.status_code == 200
    assert [headers.get(OMNIGENT_SLICE_KEY_HEADER) for headers in client_headers] == [
        None,
        "host_modern",
    ]


def test_host_http_json_keeps_demotion_when_keyed_retry_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speculative keyed retry is not committed when both routes reject it."""
    client_headers = _patch_host_http_client(
        monkeypatch,
        [
            _FakeHttpResponse(400, {"error": {"code": "wrong_replica"}}),
            _FakeHttpResponse(400, {"error": {"code": "wrong_replica"}}),
        ],
    )
    monkeypatch.setattr(
        "omnigent.chat._remote_headers",
        Mock(return_value={OMNIGENT_SLICE_KEY_HEADER: "host_modern"}),
    )
    demotion = (_BASE_URL, "host_modern")
    cli_module._host_http_keyless_demotions.add(demotion)

    result = cli_module._host_http_json(
        base_url=_BASE_URL,
        method="GET",
        path="/v1/hosts/host_modern",
        host_id="host_modern",
    )

    assert result.status_code == 400
    assert [headers.get(OMNIGENT_SLICE_KEY_HEADER) for headers in client_headers] == [
        None,
        "host_modern",
    ]
    assert demotion in cli_module._host_http_keyless_demotions
