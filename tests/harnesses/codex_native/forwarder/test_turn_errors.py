"""Turn errors for Codex forwarding.

Failed turns surface their reason; authentication errors include a re-auth hint.
Resume uses the same verdict. Empty turns warn and stay idle, as do clean turns.
A hook run that halts the prompt fails the turn with the hook's own reason.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.runner.turn_routing import (
    MARKER_FILE,
    ROUTED_PROMPT_BLOCK_PREFIX,
    clear_pending_replay,
    routed_prompt_block_reason,
    write_pending_replay,
    write_turn_routing_marker,
)
from tests.harnesses.codex_native.forwarder._support import (
    _RecordingClient,
)


def _seed_active_turn(bridge_dir: Path, turn_id: str) -> None:
    """
    Seed bridge state so a terminal turn edge clears the active turn.

    ``_terminal_turn_status_edge`` only produces an edge when the terminal
    event clears the recorded active turn id; without this seed it returns
    ``None`` as "stale".

    :param bridge_dir: Native Codex bridge directory (the test ``tmp_path``).
    :param turn_id: Active Codex turn id to record, e.g. ``"turn_123"``.
    :returns: None.
    """
    write_bridge_state(
        bridge_dir,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(bridge_dir / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(bridge_dir / "codex-home"),
            active_turn_id=turn_id,
        ),
    )


def test_classify_codex_error_auth_vs_generic() -> None:
    """The shared classifier flags auth errors and leaves the rest generic.

    This is the single classifier reused by both the live and resume paths;
    if it regresses, an expired-login failure would surface without the
    re-auth hint (or a disk-full error would wrongly demand re-auth). It
    prefers ``codexErrorInfo`` (variant / httpStatusCode) and falls back to
    the message text.
    """
    auth = fwd._CODEX_ERROR_KIND_AUTH
    generic = fwd._CODEX_ERROR_KIND_GENERIC
    # Structured codexErrorInfo: string variant, tagged object, http status.
    assert fwd._classify_codex_error({"codexErrorInfo": "Unauthorized"}, "nope") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": {"type": "Unauthorized"}}, "nope") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": {"httpStatusCode": 401}}, "nope") == auth
    # The real app-server enum serializes lowercase snake_case; it must match
    # via the structured path (message "nope" has no auth substring to fall
    # back on), case-insensitively.
    assert fwd._classify_codex_error({"codexErrorInfo": "unauthorized"}, "nope") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": {"type": "unauthorized"}}, "nope") == auth
    # Message-text fallback when codexErrorInfo is absent.
    assert fwd._classify_codex_error({}, "Please run codex login") == auth
    assert fwd._classify_codex_error({}, "ChatGPT session expired") == auth
    assert fwd._classify_codex_error({"codexErrorInfo": "Other"}, "disk full") == generic


def test_classify_codex_error_budget_exhausted_not_auth() -> None:
    """AI-gateway budget exhaustion (HTTP 403) must not classify as auth.

    The gateway returns PERMISSION_DENIED with HTTP 403 when a spending budget
    is exhausted.  The 403 and the word "403" in the message would otherwise
    trigger the auth classifier, sending users a misleading re-auth hint.
    """
    generic = fwd._CODEX_ERROR_KIND_GENERIC
    auth = fwd._CODEX_ERROR_KIND_AUTH

    # Realistic message shape from the AI gateway (budget name and id are
    # synthetic; see prod samples for the real shape).
    budget_msg = (
        'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED","message":'
        '"Budget \\"test-budget\\" (00000000-0000-0000-0000-000000000001) has reached its'
        " limit of $100. To continue, contact an admin to increase the budget or use a"
        ' different budget."}'
    )
    # Budget exhaustion is generic even when codexErrorInfo carries a 403 status.
    assert (
        fwd._classify_codex_error({"codexErrorInfo": {"httpStatusCode": 403}}, budget_msg)
        == generic
    )
    # Budget exhaustion is generic even when codexErrorInfo is absent.
    assert fwd._classify_codex_error({}, budget_msg) == generic

    # A disabled per-user rate limit (rate limit is set to 0) is also generic.
    rate_zero_msg = (
        'unexpected status 403 Forbidden: {"error_code":"PERMISSION_DENIED","message":'
        '"rate limit is set to 0 for user test@example.com"}'
    )
    assert fwd._classify_codex_error({}, rate_zero_msg) == generic

    # A genuine 401 auth failure must still classify as auth.
    assert (
        fwd._classify_codex_error({"codexErrorInfo": "unauthorized"}, "401 Unauthorized") == auth
    )
    # A genuine 403 permission error unrelated to budget must still classify as auth.
    assert (
        fwd._classify_codex_error({"codexErrorInfo": {"httpStatusCode": 403}}, "access denied")
        == auth
    )


def test_terminal_error_from_turn_reads_and_classifies_turn_error() -> None:
    """``_terminal_error_from_turn`` returns the classified ``turn.error``.

    The helper is the single source of truth for "did this turn fail"; both
    edge builders depend on it, so it must read ``turn.error`` and classify it.
    """
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {
                "message": "401 Unauthorized: login expired",
                "codexErrorInfo": "Unauthorized",
            },
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "401 Unauthorized: login expired"
    assert error.kind == fwd._CODEX_ERROR_KIND_AUTH
    assert error.is_auth is True


def test_terminal_error_from_turn_falls_back_to_error_item() -> None:
    """With no ``turn.error``, an ``error`` ThreadItem in ``turn.items`` is used.

    Both shapes exist in the app-server type system; the fallback keeps the fix
    correct on the version/path that emits the error as an item rather than as a
    ``turn.error`` object.
    """
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [
                {"type": "agentMessage", "id": "a", "text": "working"},
                {"type": "error", "message": "please run codex login"},
            ],
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "please run codex login"
    assert error.is_auth is True


def test_terminal_error_from_turn_prefers_turn_error_over_item() -> None:
    """``turn.error`` wins when both it and an ``error`` item are present."""
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "from turn.error"},
            "items": [{"type": "error", "message": "from item"}],
        }
    }

    error = fwd._terminal_error_from_turn(params)

    assert error is not None
    assert error.message == "from turn.error"


def test_terminal_error_from_notification_reads_usage_limit() -> None:
    """The standalone Codex ``error`` notification carries the visible reason."""
    error = fwd._terminal_error_from_notification(
        {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "willRetry": False,
            "error": {
                "message": "You've hit your usage limit.",
                "codexErrorInfo": "usageLimitExceeded",
            },
        }
    )

    assert error is not None
    assert error.message == "You've hit your usage limit."
    assert error.kind == fwd._CODEX_ERROR_KIND_GENERIC


@pytest.mark.asyncio
async def test_handle_event_surfaces_non_retrying_error_notification(tmp_path: Path) -> None:
    """A terminal standalone ``error`` notification reaches the session UI."""
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_123",
                "willRetry": False,
                "error": {
                    "message": "You've hit your usage limit.",
                    "codexErrorInfo": "usageLimitExceeded",
                },
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_session_status",
                "data": {
                    "status": "failed",
                    "response_id": "codex_turn_123",
                    "output": "You've hit your usage limit.",
                },
            },
        )
    ]


@pytest.mark.asyncio
async def test_handle_event_ignores_retrying_error_notification(tmp_path: Path) -> None:
    """Retryable Codex errors remain internal while Codex retries the turn."""
    client = _RecordingClient()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_123",
                "willRetry": True,
                "error": {"message": "connection dropped"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
    )

    assert client.posts == []


@pytest.mark.asyncio
async def test_handle_event_deduplicates_error_then_terminal_boundary(tmp_path: Path) -> None:
    """A standalone error owns the terminal status for its turn."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()
    error_event = {
        "method": "error",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "willRetry": False,
            "error": {"message": "You've hit your usage limit."},
        },
    }

    for event in (
        error_event,
        error_event,
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {
                    "id": "turn_123",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "text": ""}],
                },
            },
        },
    ):
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=event,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            expected_thread_id="thread_123",
            forwarder_state=forwarder_state,
        )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_session_status",
                "data": {
                    "status": "failed",
                    "response_id": "codex_turn_123",
                    "output": "You've hit your usage limit.",
                },
            },
        )
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


def test_terminal_error_from_turn_none_for_clean_turn() -> None:
    """A turn with no ``error`` object or item yields ``None`` (no false positives)."""
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "agentMessage", "id": "a", "text": "done"}],
        }
    }

    assert fwd._terminal_error_from_turn(params) is None


def test_terminal_turn_status_edge_error_item_forces_failed(tmp_path: Path) -> None:
    """A ``turn/completed`` carrying an ``error`` item (no ``turn.error``) fails.

    The item-fallback path must flip the live edge to ``failed`` just like the
    ``turn.error`` path does.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "error", "message": "model stream broke"}],
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.message == "model stream broke"
    assert edge.source == "turn/completed:turn-error"


def test_terminal_turn_status_edge_turn_error_forces_failed(tmp_path: Path) -> None:
    """A ``turn/completed`` carrying ``turn.error`` is forced to ``failed``.

    This is the core of #1108: Codex reported a *completed* boundary, but the
    turn actually failed. The edge must be ``failed`` (not the silent ``idle``
    the method alone implies) and carry the classified error.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "model stream broke"},
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.turn_id == "turn_123"
    assert edge.error is not None
    assert edge.error.message == "model stream broke"
    assert edge.error.kind == fwd._CODEX_ERROR_KIND_GENERIC
    assert edge.source == "turn/completed:turn-error"


def test_terminal_turn_status_edge_auth_turn_error_classified(tmp_path: Path) -> None:
    """An auth-classified ``turn.error`` rides the failed edge as ``auth``."""
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {
                "message": "Forbidden",
                "codexErrorInfo": {"type": "Unauthorized", "httpStatusCode": 403},
            },
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.is_auth is True


def test_terminal_turn_status_edge_failed_status_without_error(tmp_path: Path) -> None:
    """A ``turn.status == "failed"`` with no ``error`` object still fails.

    Defends against an app-server version that records the failed status but
    omits the populated ``turn.error`` — the edge must not fall back to ``idle``.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {"turn": {"id": "turn_123", "status": "failed"}}

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is None
    assert edge.source == "turn/completed:turn-failed"


def test_terminal_turn_status_edge_clean_turn_still_idle(tmp_path: Path) -> None:
    """A genuinely clean ``turn/completed`` still maps to ``idle`` (regression).

    The turn-error check must not break the happy path: no error → the edge
    stays ``idle`` with no attached error.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {
        "turn": {
            "id": "turn_123",
            "status": "completed",
            "items": [{"type": "agentMessage", "id": "a", "text": "all good"}],
        }
    }

    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "idle"
    assert edge.error is None
    assert edge.source == "turn/completed"


def test_terminal_turn_status_edge_empty_turn_idle_and_warns(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A zero-item ``turn/completed`` maps to ``idle`` and emits a WARN.

    An empty turn is not an error, but it is unusual enough to log: it maps to
    ``idle`` (so the session closes) while a WARN records the anomaly.
    """
    _seed_active_turn(tmp_path, "turn_123")
    params = {"turn": {"id": "turn_123", "status": "completed", "items": []}}

    with caplog.at_level("WARNING", logger="omnigent.harnesses.codex_native.forwarder"):
        edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)

    assert edge is not None
    assert edge.status == "idle"
    assert edge.error is None
    assert any(
        "empty turn" in record.getMessage() and record.levelname == "WARNING"
        for record in caplog.records
    ), "expected a WARN log for the empty (zero-item) turn"


def test_omnigent_status_from_resume_turn_error_parity() -> None:
    """Resume parity: a completed resume turn carrying ``turn.error`` → ``failed``.

    Without this, a reconnect that backfills from ``thread/resume`` would close
    the session as ``idle`` even though the turn had errored — the resume-path
    half of the silent-success bug.
    """
    turn_with_error = {
        "id": "turn_123",
        "status": "completed",
        "error": {"message": "rate limited"},
    }
    turn_clean = {
        "id": "turn_123",
        "status": "completed",
        "items": [{"type": "agentMessage", "id": "a", "text": "hi"}],
    }

    assert fwd._omnigent_status_from_resume_turn(turn_with_error) == "failed"
    # Parity check: the clean turn still resolves to idle.
    assert fwd._omnigent_status_from_resume_turn(turn_clean) == "idle"


def test_resume_terminal_status_edge_attaches_error(tmp_path: Path) -> None:
    """The resume edge carries the classified error like the live edge does."""
    write_bridge_state(
        tmp_path,
        CodexNativeBridgeState(
            session_id="conv_x",
            socket_path=str(tmp_path / "app-server.sock"),
            thread_id="thread_123",
            codex_home=str(tmp_path / "codex-home"),
            active_turn_id="turn_123",
        ),
    )
    turns = [
        {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": "please sign in again"},
        }
    ]

    edge = fwd._resume_terminal_status_edge_for_latest_turn(tmp_path, "thread_123", turns)

    assert edge is not None
    assert edge.status == "failed"
    assert edge.error is not None
    assert edge.error.is_auth is True
    assert edge.source == "thread/resume:turn-error"
    # The active turn id is cleared once the terminal edge is derived.
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.asyncio
async def test_post_turn_status_edge_surfaces_generic_error_output() -> None:
    """A failed edge with a generic error surfaces the message as output.

    The reason must reach the server (as ``output``) rather than being dropped;
    a generic error carries no re-auth flag.
    """
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(
        status="failed",
        turn_id="turn_123",
        source="turn/completed:turn-error",
        error=fwd._CodexTerminalError(
            message="model stream broke",
            kind=fwd._CODEX_ERROR_KIND_GENERIC,
        ),
    )

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["type"] == "external_session_status"
    data = body["data"]
    assert data["status"] == "failed"
    assert data["output"] == "model stream broke"
    # Generic errors do not demand re-auth.
    assert "reauth_required" not in data


@pytest.mark.asyncio
async def test_post_turn_status_edge_auth_error_includes_reauth_hint() -> None:
    """A failed edge with an auth error flags re-auth and appends the hint."""
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(
        status="failed",
        turn_id="turn_123",
        source="turn/completed:turn-error",
        error=fwd._CodexTerminalError(
            message="401 Unauthorized",
            kind=fwd._CODEX_ERROR_KIND_AUTH,
        ),
    )

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    data = body["data"]
    assert data["status"] == "failed"
    assert data["reauth_required"] is True
    assert "401 Unauthorized" in data["output"]
    assert fwd._CODEX_REAUTH_HINT in data["output"]


@pytest.mark.asyncio
async def test_post_turn_status_edge_clean_idle_has_no_output() -> None:
    """A normal idle edge (no error) posts status only — the success path."""
    client = _RecordingClient()
    edge = fwd._CodexTurnStatusEdge(status="idle", turn_id="turn_123", source="turn/completed")

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    data = body["data"]
    assert data["status"] == "idle"
    assert "output" not in data
    assert "reauth_required" not in data


def _hook_run_params(
    *,
    status: str,
    entries: list[dict[str, str]] | None = None,
    event_name: str = "userPromptSubmit",
    source_path: str | None = "/home/user/.codex/hooks.json",
) -> dict[str, object]:
    """
    Build Codex ``hook/started`` / ``hook/completed`` params for ``turn_123``.

    :param status: Run status, e.g. ``"blocked"``.
    :param entries: Hook output entries, e.g. ``[{"kind": "feedback", "text": "no"}]``.
    :param event_name: Hook event, e.g. ``"userPromptSubmit"``.
    :param source_path: Hook file the run came from; ``None`` omits the field.
    :returns: Notification params shaped like the app-server's ``HookRunSummary``.
    """
    run: dict[str, object] = {
        "id": "hook_run_1",
        "eventName": event_name,
        "status": status,
        "statusMessage": None,
        "entries": entries or [],
        "handlerType": "command",
        "executionMode": "sync",
        "scope": "turn",
        "source": "user",
        "displayOrder": 0,
        "startedAt": 1,
        "completedAt": 2,
        "durationMs": 1,
    }
    if source_path is not None:
        run["sourcePath"] = source_path
    return {"threadId": "thread_123", "turnId": "turn_123", "run": run}


_MISSING_SCRIPT_FEEDBACK = (
    "python3: can't open file '/tmp/plugin/activate.py': [Errno 2] No such file or directory"
)
_ROUTING_HANDOFF_NOTICE = routed_prompt_block_reason("gpt-5.6")


def _hook_run_error(
    params: dict[str, object], bridge_dir: Path, session_id: str = "conv_x"
) -> fwd._CodexTerminalError | None:
    """Classify the hook run in *params* the way the forwarder does for *session_id*."""
    return fwd._terminal_error_from_hook_run(
        fwd._hook_run_from_params(params), bridge_dir=bridge_dir, session_id=session_id
    )


def _owe_replay(bridge_dir: Path, session_id: str = "conv_x") -> None:
    """Record the replay the runner owes *session_id*, as it does before the hook blocks."""
    assert write_pending_replay(
        bridge_dir,
        session_id=session_id,
        prompt="hello",
        blocked_turn_id="turn_123",
        model="gpt-5.6",
    )


def test_terminal_error_from_hook_run_blocked_prompt_carries_hook_output(tmp_path: Path) -> None:
    """A blocked ``userPromptSubmit`` run yields the TUI's label, the hook text, and the file."""
    error = _hook_run_error(
        _hook_run_params(
            status="blocked", entries=[{"kind": "feedback", "text": _MISSING_SCRIPT_FEEDBACK}]
        ),
        tmp_path,
    )

    assert error is not None
    assert error.message == (
        f"Blocked by hook: {_MISSING_SCRIPT_FEEDBACK}\nHook: /home/user/.codex/hooks.json"
    )
    assert error.kind == fwd._CODEX_ERROR_KIND_GENERIC


def test_terminal_error_from_hook_run_stopped_prompt_uses_stop_reason(tmp_path: Path) -> None:
    """A ``continue: false`` run reads as stopped, with its stop reason."""
    error = _hook_run_error(
        _hook_run_params(
            status="stopped",
            entries=[{"kind": "stop", "text": "stopped by hook"}],
            source_path=None,
        ),
        tmp_path,
    )

    assert error is not None
    assert error.message == "Stopped by hook: stopped by hook"


def test_terminal_error_from_hook_run_without_user_output_names_outcome(tmp_path: Path) -> None:
    """Model-addressed ``context`` entries are not user-facing; the label stands alone."""
    error = _hook_run_error(
        _hook_run_params(
            status="blocked",
            entries=[{"kind": "context", "text": "for the model"}],
            source_path=None,
        ),
        tmp_path,
    )

    assert error is not None
    assert error.message == "Blocked by hook"


def test_terminal_error_from_hook_run_mixed_entries_keep_user_facing_order(
    tmp_path: Path,
) -> None:
    """``context`` stays hidden while the user-facing entries keep their order."""
    error = _hook_run_error(
        _hook_run_params(
            status="blocked",
            entries=[
                {"kind": "context", "text": "for the model"},
                {"kind": "warning", "text": "plugin cache is stale"},
                {"kind": "error", "text": _MISSING_SCRIPT_FEEDBACK},
                {"kind": "feedback", "text": "run the plugin sync and retry"},
            ],
            source_path=None,
        ),
        tmp_path,
    )

    assert error is not None
    assert error.message == (
        "Blocked by hook: plugin cache is stale\n"
        f"{_MISSING_SCRIPT_FEEDBACK}\n"
        "run the plugin sync and retry"
    )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param(
            _hook_run_params(
                status="failed", entries=[{"kind": "error", "text": "hook exited with code 1"}]
            ),
            id="failed-run-lets-the-turn-continue",
        ),
        pytest.param(_hook_run_params(status="completed"), id="completed"),
        pytest.param(_hook_run_params(status="running"), id="running"),
        pytest.param(
            _hook_run_params(
                status="blocked",
                event_name="preToolUse",
                entries=[{"kind": "feedback", "text": "no rm -rf"}],
            ),
            id="pre-tool-use-block-denies-a-call-not-the-turn",
        ),
        pytest.param(
            {"threadId": "thread_123", "turnId": "turn_123", "run": "garbage"}, id="malformed"
        ),
    ],
)
def test_terminal_error_from_hook_run_ignores_runs_that_leave_the_turn_running(
    params: dict[str, object], tmp_path: Path
) -> None:
    """Only a halted ``userPromptSubmit`` run is a turn failure."""
    assert _hook_run_error(params, tmp_path) is None


def test_terminal_error_from_hook_run_exempts_routing_handoff_only_while_replay_owed(
    tmp_path: Path,
) -> None:
    """Smart Routing's block is a handoff only while its reason and the owed replay agree.

    The runner records the replay before the route-turn hook blocks and clears
    it once delivered, so the reason text alone (another hook could print it)
    never hides a block, nor does a record left by another session or one
    already delivered. The session's routing marker outlives the replay and
    therefore does not count either.
    """
    routed = _hook_run_params(
        status="blocked", entries=[{"kind": "feedback", "text": _ROUTING_HANDOFF_NOTICE}]
    )

    error = _hook_run_error(routed, tmp_path)
    assert error is not None
    assert error.message.startswith(f"Blocked by hook: {_ROUTING_HANDOFF_NOTICE}")

    _owe_replay(tmp_path, session_id="conv_other")
    assert _hook_run_error(routed, tmp_path) is not None
    _owe_replay(tmp_path)
    assert _hook_run_error(routed, tmp_path) is None
    clear_pending_replay(tmp_path)
    assert _hook_run_error(routed, tmp_path) is not None

    assert write_turn_routing_marker(tmp_path, session_id="conv_x", decision_id="decision_1")
    assert _hook_run_error(routed, tmp_path) is not None
    (tmp_path / MARKER_FILE).unlink()

    _owe_replay(tmp_path)
    for entries in (
        [{"kind": "feedback", "text": f"{ROUTED_PROMPT_BLOCK_PREFIX}gpt-5.6"}],
        [
            {"kind": "feedback", "text": _ROUTING_HANDOFF_NOTICE},
            {"kind": "error", "text": "rejected by the team policy hook"},
        ],
    ):
        assert _hook_run_error(_hook_run_params(status="blocked", entries=entries), tmp_path)


@pytest.mark.asyncio
async def test_handle_event_hook_blocked_prompt_fails_turn_and_owns_terminal_boundary(
    tmp_path: Path,
) -> None:
    """A hook-blocked prompt surfaces as the turn's failure instead of a silent idle.

    Codex follows the ``blocked`` run with a zero-item ``turn/completed``; that
    boundary must not flip the session back to ``idle`` and hide the reason.
    """
    _seed_active_turn(tmp_path, "turn_123")
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()

    for event in (
        {"method": "hook/started", "params": _hook_run_params(status="running")},
        {
            "method": "hook/completed",
            "params": _hook_run_params(
                status="blocked", entries=[{"kind": "feedback", "text": _MISSING_SCRIPT_FEEDBACK}]
            ),
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {"id": "turn_123", "status": "completed", "items": [], "error": None},
            },
        },
    ):
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=event,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            expected_thread_id="thread_123",
            forwarder_state=forwarder_state,
        )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_session_status",
                "data": {
                    "status": "failed",
                    "response_id": "codex_turn_123",
                    "output": (
                        f"Blocked by hook: {_MISSING_SCRIPT_FEEDBACK}\n"
                        "Hook: /home/user/.codex/hooks.json"
                    ),
                },
            },
        )
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.asyncio
async def test_handle_event_failed_hook_run_leaves_turn_lifecycle_alone(tmp_path: Path) -> None:
    """A non-blocking hook failure posts nothing; the turn still ends idle on its own."""
    _seed_active_turn(tmp_path, "turn_123")
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()

    for event in (
        {
            "method": "hook/completed",
            "params": _hook_run_params(
                status="failed", entries=[{"kind": "error", "text": "hook exited with code 1"}]
            ),
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {
                    "id": "turn_123",
                    "status": "completed",
                    "items": [{"type": "agentMessage", "id": "a", "text": "done"}],
                },
            },
        },
    ):
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=event,
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            expected_thread_id="thread_123",
            forwarder_state=forwarder_state,
        )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_session_status",
                "data": {"status": "idle", "response_id": "codex_turn_123"},
            },
        )
    ]


@pytest.mark.asyncio
async def test_handle_event_routing_shaped_block_after_replay_posts_failed_edge(
    tmp_path: Path,
) -> None:
    """Once the replay has landed, a block using the routing words is a real rejection.

    The session marker the route-turn hook left behind must not exempt it: the
    forwarder posts the failed edge and owns the terminal boundary as for any
    other hook rejection.
    """
    _seed_active_turn(tmp_path, "turn_123")
    assert write_turn_routing_marker(tmp_path, session_id="conv_x", decision_id="decision_1")
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "hook/completed",
            "params": _hook_run_params(
                status="blocked", entries=[{"kind": "feedback", "text": _ROUTING_HANDOFF_NOTICE}]
            ),
        },
        usage_coalescer=usage_coalescer,
        elicitation_tracker=elicitation_tracker,
        expected_thread_id="thread_123",
        forwarder_state=forwarder_state,
    )

    assert len(client.posts) == 1
    path, body = client.posts[0]
    assert path == "/v1/sessions/conv_x/events"
    assert body["data"]["status"] == "failed"
    assert body["data"]["output"].startswith(f"Blocked by hook: {_ROUTING_HANDOFF_NOTICE}")
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None


@pytest.mark.asyncio
async def test_handle_event_smart_routing_block_does_not_fail_replayed_turn(
    tmp_path: Path,
) -> None:
    """Smart Routing's deliberate prompt block is a handoff, not a failure.

    The runner replays the prompt on the routed model and waits for the blocked
    turn's own ``turn/completed`` to clear the active turn, so the forwarder
    must post no failed edge and leave that boundary intact. The runner recorded
    the replay it owes before the hook blocked, as it does for real.
    """
    _seed_active_turn(tmp_path, "turn_123")
    _owe_replay(tmp_path)
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "hook/completed",
            "params": _hook_run_params(
                status="blocked", entries=[{"kind": "feedback", "text": _ROUTING_HANDOFF_NOTICE}]
            ),
        },
        usage_coalescer=usage_coalescer,
        elicitation_tracker=elicitation_tracker,
        expected_thread_id="thread_123",
        forwarder_state=forwarder_state,
    )

    assert client.posts == []
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id == "turn_123"

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {"id": "turn_123", "status": "completed", "items": [], "error": None},
            },
        },
        usage_coalescer=usage_coalescer,
        elicitation_tracker=elicitation_tracker,
        expected_thread_id="thread_123",
        forwarder_state=forwarder_state,
    )

    assert client.posts == [
        (
            "/v1/sessions/conv_x/events",
            {
                "type": "external_session_status",
                "data": {"status": "idle", "response_id": "codex_turn_123"},
            },
        )
    ]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.active_turn_id is None
