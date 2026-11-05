"""Turn errors for Codex forwarding.

Failed turns surface their reason; authentication errors include a re-auth hint.
Resume uses the same verdict. Empty turns warn and stay idle, as do clean turns.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.harnesses.codex_egress import detect_certificate_failure
from omnigent.harnesses.codex_native import forwarder as fwd
from omnigent.harnesses.codex_native.bridge import (
    CodexNativeBridgeState,
    read_bridge_state,
    read_certificate_failure,
    record_certificate_failure,
    update_active_turn_id,
    write_bridge_state,
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


def test_classify_codex_error_ip_access_list_refusal_not_auth() -> None:
    """A workspace IP access list refusal (HTTP 403) is not a sign-in failure.

    Signing in again cannot clear a network policy, so the re-auth status and
    hint would point the user at the wrong fix.
    """
    generic = fwd._CODEX_ERROR_KIND_GENERIC
    # Synthetic address, workspace and request id; the wording is the gateway's.
    acl_message = (
        "unexpected status 403 Forbidden: Source IP address: 203.0.113.7 is blocked by "
        "Databricks IP ACL for workspace: 1234567890123456 "
        "[ReqId: 00000000-0000-4000-8000-000000000000]"
    )
    structured_403 = {"codexErrorInfo": {"httpStatusCode": 403}}
    assert fwd._classify_codex_error(structured_403, acl_message) == generic
    assert fwd._classify_codex_error({}, acl_message) == generic


@pytest.mark.parametrize(
    "request_id",
    [
        "0000a401-0000-4000-8000-000000000000",
        "00000000-4030-4000-8000-000000000000",
        "00000000-0000-4000-8000-0000000c4013",
    ],
)
def test_classify_codex_error_rate_limit_request_id_not_auth(request_id: str) -> None:
    """A 429 whose request id happens to contain "401"/"403" is not auth.

    The message fallback must match the status only as a standalone token,
    and an explicit rate limit never asks the user to sign in again.
    """
    message = f"exceeded retry limit, last status: 429 Too Many Requests, request id: {request_id}"
    assert fwd._classify_codex_error({}, message) == fwd._CODEX_ERROR_KIND_GENERIC


@pytest.mark.parametrize(
    "message",
    [
        "unexpected status 401 Unauthorized: token expired",
        "unexpected status 403 Forbidden: missing scope",
        "stream error: status 401, request id: 00000000-0000-4000-8000-000000000000",
    ],
)
def test_classify_codex_error_standalone_auth_status_is_auth(message: str) -> None:
    """A real 401/403 status in the message still classifies as auth."""
    assert fwd._classify_codex_error({}, message) == fwd._CODEX_ERROR_KIND_AUTH


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
@pytest.mark.parametrize("shape", ["legacy-message-only", "reconnect-without-http-status"])
async def test_handle_event_ignores_retrying_error_notification(
    tmp_path: Path, shape: str
) -> None:
    """Retryable Codex errors remain internal while Codex retries the turn.

    Without launcher certificate evidence, neither the legacy retry shape nor
    Codex's reconnect notification posts a status edge or interrupts the turn.
    """
    client = _RecordingClient()
    codex_client = _InterruptRecordingCodexClient()
    _seed_active_turn(tmp_path, "turn_123")
    params = (
        {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "willRetry": True,
            "error": {"message": "connection dropped"},
        }
        if shape == "legacy-message-only"
        else _connection_retry_params()
    )

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "error", "params": params},
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
    )

    assert client.posts == []
    assert codex_client.requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("detail", [None, "unstructured diagnostic"])
async def test_error_without_usable_detail_leaves_the_turn_to_turn_completed(
    tmp_path: Path, detail: object
) -> None:
    """An ``error`` with no usable detail yields to ``turn/completed``'s real error."""
    _seed_active_turn(tmp_path, "turn_123")
    client = _RecordingClient()
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]
    elicitation_tracker = fwd._CodexElicitationTaskTracker()
    forwarder_state = fwd._CodexForwarderState()

    for event in (
        {
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": "turn_123",
                "willRetry": False,
                "error": detail,
            },
        },
        {
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {
                    "id": "turn_123",
                    "status": "failed",
                    "error": {"message": "unexpected status 503 Service Unavailable"},
                },
            },
        },
    ):
        await fwd._handle_event(
            client,  # type: ignore[arg-type]
            session_id="conv_x",
            bridge_dir=tmp_path,
            event=event,  # type: ignore[arg-type]
            usage_coalescer=usage_coalescer,
            elicitation_tracker=elicitation_tracker,
            expected_thread_id="thread_123",
            forwarder_state=forwarder_state,
        )

    assert len(client.posts) == 1
    data = client.posts[0][1]["data"]
    assert {key: data.get(key) for key in ("status", "response_id", "output")} == {
        "status": "failed",
        "response_id": "codex_turn_123",
        "output": "unexpected status 503 Service Unavailable",
    }
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id is None


def _write_rollout(tmp_path: Path, content: bytes) -> None:
    """Write the bridge thread's Codex rollout under the private ``CODEX_HOME``."""
    rollout_dir = tmp_path / "codex-home" / "sessions" / "2026" / "10" / "15"
    rollout_dir.mkdir(parents=True, exist_ok=True)
    (rollout_dir / "rollout-2026-10-15T16-28-06-thread_123.jsonl").write_bytes(content)


def _turn_context_line(model: str | None) -> bytes:
    turn: dict[str, object] = {"turn_id": "turn_0", "cwd": "/repo"}
    if model is not None:
        turn["model"] = model
    return (json.dumps({"type": "turn_context", "payload": turn}) + "\n").encode()


async def _fail_turn(tmp_path: Path, compaction_status: str = "in_progress") -> dict[str, object]:
    """Fail the active turn with a terminal error and return the posted status data."""
    client = _RecordingClient()
    forwarder_state = fwd._CodexForwarderState(
        model="system.ai.gpt-6-1-sol", compaction_status_posted=compaction_status
    )
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
                "error": {"message": "unexpected status 403 Forbidden: budget limit reached"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        forwarder_state=forwarder_state,
    )
    return client.posts[0][1]["data"]


_COMPACTION_MODELS = {
    "compaction_model": "system.ai.gpt-6-astra",
    "selected_model": "system.ai.gpt-6-1-sol",
}
_NO_COMPACTION_MODELS = {"compaction_model": None, "selected_model": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("rollout_model", "compaction_status", "catalog_entries", "reported"),
    [
        ("system.ai.gpt-6-astra", "in_progress", None, True),
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            ({"comp_hash": "3000"}, {"comp_hash": "2911"}),
            True,
        ),
        # Matching catalog entries mean Codex compacted on the selected model itself.
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            ({"comp_hash": "3000"}, {"comp_hash": "3000"}),
            False,
        ),
        # With matching hashes, Codex compacts on the model with the larger usable window.
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            (
                {"comp_hash": "3000", "effective_context_window_percent": 95},
                {"comp_hash": "3000", "effective_context_window_percent": 90},
            ),
            True,
        ),
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            (
                {
                    "comp_hash": "3000",
                    "context_window": 300000,
                    "effective_context_window_percent": 80,
                },
                {"comp_hash": "3000", "effective_context_window_percent": 95},
            ),
            False,
        ),
        # A hash declared by only one model can't trigger it, so the windows decide.
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            ({"comp_hash": "3000"}, {}),
            False,
        ),
        # Without both windows, the catalog can't rule the compaction out.
        (
            "system.ai.gpt-6-astra",
            "in_progress",
            ({"comp_hash": "3000", "context_window": None}, {"comp_hash": "3000"}),
            True,
        ),
        # Gateway and Codex spellings of the selected model compare equal.
        ("gpt-6.1-sol", "in_progress", None, False),
        ("system.ai.gpt-6-astra", "completed", None, False),
        (None, "in_progress", None, False),
    ],
    ids=[
        "previous-model",
        "catalog-hash-changed",
        "catalog-rules-it-out",
        "previous-usable-window-larger",
        "selected-usable-window-larger",
        "one-hash-missing",
        "catalog-window-unknown",
        "selected-model",
        "outside-compaction",
        "no-recorded-model",
    ],
)
async def test_error_during_compaction_reports_the_previous_model_it_ran_on(
    tmp_path: Path,
    rollout_model: str | None,
    compaction_status: str,
    catalog_entries: tuple[dict[str, object], dict[str, object]] | None,
    reported: bool,
) -> None:
    """A compaction failure names the rollout's previous model when Codex compacts on it."""
    _seed_active_turn(tmp_path, "turn_123")
    if catalog_entries is not None:
        catalog = tmp_path / "catalog.json"
        catalog.write_text(
            json.dumps(
                {
                    "models": [
                        {"slug": slug, "context_window": 272000, **entry}
                        for slug, entry in zip(
                            ("system.ai.gpt-6-astra", "system.ai.gpt-6-1-sol"),
                            catalog_entries,
                            strict=True,
                        )
                    ]
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "codex-home").mkdir()
        (tmp_path / "codex-home" / "config.toml").write_text(
            f'model_catalog_json = "{catalog}"\n', encoding="utf-8"
        )
    _write_rollout(tmp_path, _turn_context_line(rollout_model))

    data = await _fail_turn(tmp_path, compaction_status)

    assert data["status"] == "failed"
    assert {key: data.get(key) for key in _COMPACTION_MODELS} == (
        _COMPACTION_MODELS if reported else _NO_COMPACTION_MODELS
    )


@pytest.mark.asyncio
async def test_a_corrupt_rollout_line_does_not_hide_the_previous_model(tmp_path: Path) -> None:
    _seed_active_turn(tmp_path, "turn_123")
    _write_rollout(tmp_path, b"\xff\xfe torn line\n" + _turn_context_line("system.ai.gpt-6-astra"))

    data = await _fail_turn(tmp_path)

    assert {key: data.get(key) for key in _COMPACTION_MODELS} == _COMPACTION_MODELS


@pytest.mark.asyncio
async def test_a_failed_diagnosis_still_posts_the_turn_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn is already marked surfaced, so a lost edge would leave no error card."""
    _seed_active_turn(tmp_path, "turn_123")

    def _unreadable(_bridge_dir: Path, _selected_model: str) -> str | None:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(fwd, "_failed_compaction_model", _unreadable)

    data = await _fail_turn(tmp_path)

    assert data["status"] == "failed"
    assert "budget limit reached" in str(data["output"])
    assert {key: data.get(key) for key in _COMPACTION_MODELS} == _NO_COMPACTION_MODELS


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_id", ["old_turn", None])
async def test_stale_or_ambiguous_error_cannot_fail_a_newer_turn(
    tmp_path: Path, turn_id: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("INFO", logger=fwd.__name__)
    _seed_active_turn(tmp_path, "new_turn")
    client = _RecordingClient()
    forwarder_state = fwd._CodexForwarderState()

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": turn_id,
                "willRetry": False,
                "error": {"message": "old turn failed"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        forwarder_state=forwarder_state,
    )

    assert client.posts == []
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id == "new_turn"
    assert forwarder_state.surfaced_terminal_error_turns == set()
    ignored = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_terminal_error_ignored"
    ]
    assert len(ignored) == 1
    assert ignored[0].session_id == "conv_x"
    assert ignored[0].attributes == {"reason": "stale_or_ambiguous_turn"}


@pytest.mark.asyncio
@pytest.mark.parametrize("bridge_exists", [False, True])
async def test_stateful_idless_error_cannot_fail_an_idle_bridge(
    tmp_path: Path, bridge_exists: bool
) -> None:
    if bridge_exists:
        _seed_active_turn(tmp_path, "previous_turn")
        assert fwd.clear_active_turn_id_if_matches(tmp_path, "previous_turn")
    client = _RecordingClient()
    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "willRetry": False,
                "error": {"message": "unattributed error"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        forwarder_state=fwd._CodexForwarderState(),
    )
    assert client.posts == []


@pytest.mark.asyncio
async def test_child_thread_error_needs_no_bridge_to_fail_its_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A child thread has no bridge: a detailed error fails its turn once."""
    monkeypatch.chdir(tmp_path)
    client = _RecordingClient()
    forwarder_state = fwd._CodexForwarderState()
    for _ in range(2):
        assert await fwd._maybe_handle_turn_event(
            client,  # type: ignore[arg-type]
            session_id="conv_child",
            bridge_dir=None,
            method="error",
            params={
                "threadId": "thread_child",
                "turnId": "turn_child",
                "willRetry": False,
                "error": {"message": "child turn failed"},
            },
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_child"),  # type: ignore[arg-type]
            delta_coalescer=None,
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            codex_client=None,
            forwarder_state=forwarder_state,
        )

    statuses = [body["data"]["status"] for _, body in client.posts]
    assert statuses == ["failed"]
    assert forwarder_state.surfaced_terminal_error_turns == {"turn_child"}
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_correlated_error_recovers_a_missed_turn_start(tmp_path: Path) -> None:
    _seed_active_turn(tmp_path, "previous_turn")
    assert fwd.clear_active_turn_id_if_matches(tmp_path, "previous_turn")
    client = _RecordingClient()
    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "error",
            "params": {
                "threadId": "thread_123",
                "turnId": "missed_start_turn",
                "willRetry": False,
                "error": {"message": "turn failed before its start was observed"},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        forwarder_state=fwd._CodexForwarderState(),
    )
    assert len(client.posts) == 1
    assert client.posts[0][1]["data"]["status"] == "failed"
    assert client.posts[0][1]["data"]["response_id"] == "codex_missed_start_turn"


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

    edge = fwd._resume_turn_status_edge_for_latest_turn(tmp_path, "thread_123", turns)

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
@pytest.mark.parametrize(
    ("message", "expected_output"),
    [
        ("model stream broke", "model stream broke"),
        (
            "Incomplete response returned, reason: max_output_tokens",
            "Incomplete response returned, reason: max_output_tokens\n\n"
            "The model reached its response length limit. Ask it to continue or split "
            "the request into smaller parts.",
        ),
        (
            "Incomplete response returned, reason: content_filter",
            "Incomplete response returned, reason: content_filter",
        ),
        (
            "Invalid max_output_tokens configuration",
            "Invalid max_output_tokens configuration",
        ),
        (
            "Failed to shutdown thread persistence",
            "Failed to shutdown thread persistence",
        ),
    ],
)
async def test_post_turn_status_edge_surfaces_generic_error_output(
    message: str, expected_output: str
) -> None:
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
            message=message,
            kind=fwd._CODEX_ERROR_KIND_GENERIC,
        ),
    )

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    assert len(client.posts) == 1
    _url, body = client.posts[0]
    assert body["type"] == "external_session_status"
    data = body["data"]
    assert data["status"] == "failed"
    assert data["output"] == expected_output
    assert edge.error is not None
    assert edge.error.message == message
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
async def test_provider_token_command_failure_posts_without_codex_login_hint(
    tmp_path: Path,
) -> None:
    """A failed model provider token command is not a Codex login problem."""
    _seed_active_turn(tmp_path, "turn_123")
    message = (
        "provider auth command `isaac` exited with status exit status: 1: [INFO] Access token "
        "expiring. Attempting silent refresh.\n[ERROR] Silent token refresh failed: Failed to "
        "refresh token: HTTP Error 400: Bad Request.\nError: Could not get an AI DevTools "
        "access token. Run `isaac` to log in and try again."
    )
    params = {
        "turn": {
            "id": "turn_123",
            "status": "failed",
            "error": {"message": message, "codexErrorInfo": "other"},
        }
    }
    edge = fwd._terminal_turn_status_edge(tmp_path, "turn/completed", params)
    assert edge is not None
    client = _RecordingClient()

    await fwd._post_turn_status_edge(client, "conv_x", edge)

    data = client.posts[0][1]["data"]
    assert data["status"] == "failed"
    assert data["output"] == message
    assert "reauth_required" not in data


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


_CERTIFICATE_LINE = (
    "Failed to fetch safe flags from proxy: [SSL: SSLV3_ALERT_CERTIFICATE_EXPIRED] "
    "ssl/tls alert certificate expired (_ssl.c:2580)"
)


@pytest.mark.parametrize(
    ("message", "turn_id", "reason"),
    [
        ("Failed to shutdown thread persistence", None, "thread_persistence_shutdown"),
        ("private-error-detail", "child-turn", "unclassified"),
        ("x" * 5000 + "Failed to shutdown thread persistence", "child-turn", "unclassified"),
    ],
)
async def test_status_audit_correlates_child_errors_without_copying_error_text(
    caplog: pytest.LogCaptureFixture,
    message: str,
    turn_id: str | None,
    reason: str,
) -> None:
    caplog.set_level(logging.INFO, logger=fwd._logger.name)
    client = _RecordingClient()
    await fwd._post_turn_status_edge(
        client,
        "child-session",
        fwd._CodexTurnStatusEdge(
            status="failed",
            turn_id=turn_id,
            source="error",
            error=fwd._CodexTerminalError(message=message, kind=fwd._CODEX_ERROR_KIND_GENERIC),
        ),
    )
    [record] = [
        record
        for record in caplog.records
        if getattr(record, "event_name", None) == "codex_turn_status"
    ]
    assert record.session_id == "child-session"
    assert record.attributes["has_turn_id"] is (turn_id is not None)
    assert record.attributes["has_error"] is True
    assert record.attributes["native_error_kind"] == "generic"
    assert record.attributes["native_error_reason"] == reason
    assert record.attributes["protocol_source"] == "error"
    assert "private-error-detail" not in json.dumps(record.attributes)
    assert message not in record.getMessage()
    assert client.posts[0][1]["data"]["output"] == message


def _connection_retry_params(*, http_status: int | None = None) -> dict:
    """The ``error``/``willRetry`` notification codex-cli 0.154 emits per reconnect."""
    return {
        "threadId": "thread_123",
        "turnId": "turn_123",
        "willRetry": True,
        "error": {
            "message": "Reconnecting... waiting for network",
            "codexErrorInfo": {"responseStreamDisconnected": {"httpStatusCode": http_status}},
            "additionalDetails": "Connection failed: error sending request",
        },
    }


class _InterruptRecordingCodexClient:
    """Codex app-server client stub that records the RPCs the forwarder issues."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        return {"result": {}}


def _record_launcher_certificate_failure(bridge_dir: Path) -> None:
    failure = detect_certificate_failure(_CERTIFICATE_LINE)
    assert failure is not None
    record_certificate_failure(bridge_dir, failure)


@pytest.mark.asyncio
async def test_handle_event_fails_retrying_turn_on_launcher_certificate_failure(
    tmp_path: Path,
) -> None:
    """A reconnect loop behind a launcher-reported certificate failure fails the turn.

    The failure names the certificate. Codex never ends such a turn on its
    own, so the forwarder interrupts it, publishes the failure with the next
    step, and owns the turn's terminal status so the interrupt's own boundary
    does not flip it back to idle.
    """
    client = _RecordingClient()
    codex_client = _InterruptRecordingCodexClient()
    _seed_active_turn(tmp_path, "turn_123")
    _record_launcher_certificate_failure(tmp_path)
    forwarder_state = fwd._CodexForwarderState(
        parent_session_id="conv_x",
        codex_client=codex_client,  # type: ignore[arg-type]
        model="gpt-5",
    )
    usage_coalescer = fwd._SessionUsageCoalescer(client, "conv_x")  # type: ignore[arg-type]

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "error", "params": _connection_retry_params()},
        usage_coalescer=usage_coalescer,
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
        forwarder_state=forwarder_state,
    )

    assert codex_client.requests == [
        ("turn/interrupt", {"threadId": "thread_123", "turnId": "turn_123"})
    ]
    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_x/events"
    assert body["type"] == "external_session_status"
    assert body["data"]["status"] == "failed"
    assert body["data"]["response_id"] == "codex_turn_123"
    assert "reauth_required" not in body["data"]
    output = body["data"]["output"]
    assert output.startswith(
        "Codex could not connect to its model endpoint for gpt-5: the TLS certificate has expired"
    )
    assert "SSLV3_ALERT_CERTIFICATE_EXPIRED" in output
    assert "run dbcert" in output
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id is None

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {"id": "turn_123", "status": "interrupted", "items": []},
            },
        },
        usage_coalescer=usage_coalescer,
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
        forwarder_state=forwarder_state,
    )

    # The interrupt's own boundary signals the interruption but never posts a
    # second status edge that would flip the failure back to idle.
    status_posts = [
        body["data"]["status"]
        for _url, body in client.posts
        if body["type"] == "external_session_status"
    ]
    assert status_posts == ["failed"]


@pytest.mark.asyncio
async def test_handle_event_retry_after_mcp_certificate_line_is_left_to_codex(
    tmp_path: Path,
) -> None:
    """An MCP server's expired certificate on stderr does not kill a reconnecting turn."""
    client = _RecordingClient()
    codex_client = _InterruptRecordingCodexClient()
    _seed_active_turn(tmp_path, "turn_123")
    mcp_line = (
        "2026-10-16T12:00:00.000000Z  WARN codex_mcp::rmcp_client: failed to connect MCP "
        "server `docs`: error sending request: invalid peer certificate: Expired"
    )
    failure = detect_certificate_failure(mcp_line)
    if failure is not None:
        record_certificate_failure(tmp_path, failure)

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "error", "params": _connection_retry_params()},
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
        forwarder_state=fwd._CodexForwarderState(
            parent_session_id="conv_x",
            codex_client=codex_client,  # type: ignore[arg-type]
            model="gpt-5",
        ),
    )

    assert codex_client.requests == []
    assert all(body["data"].get("status") != "failed" for _url, body in client.posts)
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id == "turn_123"


@pytest.mark.asyncio
async def test_handle_event_retry_with_http_status_is_not_blamed_on_certificate(
    tmp_path: Path,
) -> None:
    """A retry that got an HTTP response reached the endpoint over TLS, so Codex keeps retrying."""
    client = _RecordingClient()
    codex_client = _InterruptRecordingCodexClient()
    _seed_active_turn(tmp_path, "turn_123")
    _record_launcher_certificate_failure(tmp_path)

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "error", "params": _connection_retry_params(http_status=503)},
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
    )

    assert client.posts == []
    assert codex_client.requests == []


@pytest.mark.asyncio
async def test_handle_event_without_turn_state_leaves_retry_to_codex(tmp_path: Path) -> None:
    """Without per-turn state (child sessions) the forwarder neither interrupts nor posts."""
    client = _RecordingClient()
    codex_client = _InterruptRecordingCodexClient()
    _seed_active_turn(tmp_path, "turn_123")
    _record_launcher_certificate_failure(tmp_path)

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "error", "params": _connection_retry_params()},
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
        codex_client=codex_client,  # type: ignore[arg-type]
    )

    assert client.posts == []
    assert codex_client.requests == []
    state = read_bridge_state(tmp_path)
    assert state is not None and state.active_turn_id == "turn_123"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "params",
    [
        {
            "threadId": "thread_123",
            "turn": {"id": "turn_123", "status": "completed", "items": [{"type": "agentMessage"}]},
        },
        {"threadId": "thread_123", "turnId": "turn_123"},
    ],
    ids=["status-completed", "legacy-turn-id-only"],
)
async def test_completed_turn_clears_launcher_certificate_record(
    tmp_path: Path, params: dict
) -> None:
    """A turn that reaches the model proves the egress works; the launch-time line is forgotten."""
    client = _RecordingClient()
    _seed_active_turn(tmp_path, "turn_123")
    _record_launcher_certificate_failure(tmp_path)

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={"method": "turn/completed", "params": params},
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
    )

    assert read_certificate_failure(tmp_path) is None
    assert [post[1]["data"]["status"] for post in client.posts] == ["idle"]


@pytest.mark.asyncio
async def test_interrupted_turn_keeps_launcher_certificate_record(tmp_path: Path) -> None:
    """Stopping a turn that never reached the model is not proof the certificate works."""
    client = _RecordingClient()
    _seed_active_turn(tmp_path, "turn_123")
    _record_launcher_certificate_failure(tmp_path)

    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=tmp_path,
        event={
            "method": "turn/completed",
            "params": {
                "threadId": "thread_123",
                "turn": {"id": "turn_123", "status": "interrupted", "items": []},
            },
        },
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        expected_thread_id="thread_123",
    )

    assert read_certificate_failure(tmp_path) is not None


class _ContinuingCodexClient:
    """Caller-controlled native receipt, separate from the start acknowledgement."""

    supports_user_message_ids = True

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict]] = []
        self.client_id = ""
        self.turn_status = "inProgress"
        self.receipt_fault = ""
        self.closed = False

    def snapshot(self) -> dict:
        item = {"type": "userMessage", "id": "input", "clientId": self.client_id}
        thread_id = "thread_123"
        turn_id = "turn_auto"
        if self.receipt_fault == "missing":
            item.pop("clientId")
        elif self.receipt_fault == "foreign":
            item["clientId"] = "someone-else"
        elif self.receipt_fault == "source":
            turn_id = "turn_a"
        elif self.receipt_fault == "empty_item":
            item["id"] = ""
        elif self.receipt_fault == "thread":
            thread_id = "foreign-thread"
        turns = [{"id": turn_id, "status": self.turn_status, "items": [item]}]
        if self.receipt_fault == "duplicate":
            turns.append({"id": "another-turn", "status": "inProgress", "items": [item]})
        return {"result": {"thread": {"id": thread_id, "turns": turns}}}

    async def request(self, method: str, params: dict) -> dict:
        self.requests.append((method, params))
        if method == "turn/start":
            self.client_id = params["clientUserMessageId"]
            return {"result": {"turn": {"id": "ack-is-not-proof"}}}
        return self.snapshot() if method == "thread/read" else {"result": {}}

    async def close(self) -> None:
        self.closed = True

    async def connect(self) -> None:
        self.closed = False


_OUTPUT_LIMIT_ERROR = {"message": "Incomplete response returned, reason: max_output_tokens"}


async def _forward_output_limit_event(
    client: _RecordingClient,
    codex: _ContinuingCodexClient,
    state: fwd._CodexForwarderState,
    bridge_dir: Path,
    method: str,
    params: dict,
    delta_coalescer: fwd._OutputTextDeltaCoalescer | None = None,
) -> None:
    await fwd._handle_event(
        client,  # type: ignore[arg-type]
        session_id="conv_x",
        bridge_dir=bridge_dir,
        event={"method": method, "params": params},
        delta_coalescer=delta_coalescer,
        expected_thread_id="thread_123",
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),  # type: ignore[arg-type]
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        codex_client=codex,
        forwarder_state=state,  # type: ignore[arg-type]
    )


def _output_limit_completion(turn_id: str = "turn_a", **changes: object) -> dict:
    return {
        "threadId": "thread_123",
        "turn": {
            "id": turn_id,
            "status": "failed",
            "error": _OUTPUT_LIMIT_ERROR,
            **changes,
        },
    }


@pytest.mark.parametrize("standalone", [False, True])
async def test_output_limit_exact_receipt_continues_once_and_preserves_partial_output(
    tmp_path: Path,
    standalone: bool,
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "item/completed",
        {
            "threadId": "thread_123",
            "turnId": "turn_a",
            "item": {"type": "agentMessage", "id": "partial", "text": "First half."},
        },
    )
    if standalone:
        await _forward_output_limit_event(
            client,
            codex,
            state,
            tmp_path,
            "error",
            {
                "threadId": "thread_123",
                "turnId": "turn_a",
                "willRetry": False,
                "error": _OUTPUT_LIMIT_ERROR,
            },
        )
        assert codex.requests == []
    for _ in range(2):
        await _forward_output_limit_event(
            client,
            codex,
            state,
            tmp_path,
            "turn/completed",
            _output_limit_completion(),
        )
    owner = read_bridge_state(tmp_path)
    assert owner is not None and owner.active_turn_id == "turn_auto"
    assert owner.automatic_input_outcome == "accepted"
    assert [method for method, _ in codex.requests] == ["turn/start", "thread/read"]
    assert codex.requests[0][1]["clientUserMessageId"] == owner.automatic_input_client_message_id
    assert [
        body["data"]["status"]
        for _, body in client.posts
        if body["type"] == "external_session_status"
    ] == ["running"]
    assert any(body["data"].get("item_data", {}).get("content") for _, body in client.posts)
    await _forward_output_limit_event(
        client,
        codex,
        fwd._CodexForwarderState(),
        tmp_path,
        "turn/completed",
        _output_limit_completion("turn_auto"),
    )
    assert len([method for method, _ in codex.requests if method == "turn/start"]) == 1
    assert read_bridge_state(tmp_path).automatic_input_outcome == "completed"
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert client.posts[-1][1]["data"]["status"] == "failed"


@pytest.mark.parametrize(
    "message",
    [
        "Invalid max_output_tokens configuration",
        "Incomplete response returned, reason: content_filter",
        "Incomplete response returned, reason: max_output_tokens extra",
        "prefix Incomplete response returned, reason: max_output_tokens",
        "401 Unauthorized",
    ],
)
async def test_output_limit_lookalikes_do_not_continue(tmp_path: Path, message: str) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "turn/completed",
        _output_limit_completion(error={"message": message}),
    )
    assert codex.requests == []
    assert client.posts[-1][1]["data"]["status"] == "failed"


@pytest.mark.parametrize("fence", ["capability", "interrupted", "source", "thread", "shell"])
async def test_output_limit_requires_compatible_completed_source(
    tmp_path: Path, fence: str
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    params = _output_limit_completion()
    if fence == "capability":
        codex.supports_user_message_ids = False
    elif fence == "interrupted":
        params["turn"]["status"] = "interrupted"
    elif fence == "source":
        params["turn"]["id"] = "stale-source"
    elif fence == "thread":
        params["threadId"] = "other-thread"
    else:
        write_bridge_state(
            tmp_path, replace(read_bridge_state(tmp_path), active_shell_turn_id="shell")
        )
    await _forward_output_limit_event(client, codex, state, tmp_path, "turn/completed", params)
    assert codex.requests == []
    if fence == "capability":
        assert "Ask it to continue" in client.posts[-1][1]["data"]["output"]


@pytest.mark.parametrize(
    "item_type",
    [
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "webSearch",
        "imageView",
        "imageGeneration",
        "collabAgentToolCall",
    ],
)
@pytest.mark.parametrize("finished", [False, True])
async def test_output_limit_tool_matrix_preserves_results_and_requires_closed_tools(
    tmp_path: Path,
    item_type: str,
    finished: bool,
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    item = {"type": item_type, "id": "tool", "command": "echo prepared"}
    params = {"threadId": "thread_123", "turnId": "turn_a", "item": item}
    await _forward_output_limit_event(client, codex, state, tmp_path, "item/started", params)
    if finished:
        item.update(status="completed", exitCode=0, aggregatedOutput="prepared")
        await _forward_output_limit_event(client, codex, state, tmp_path, "item/completed", params)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert bool(codex.requests) is finished
    assert bool(read_bridge_state(tmp_path).automatic_input_client_message_id) is finished


@pytest.mark.parametrize(
    "fault", ["missing", "foreign", "source", "empty_item", "thread", "duplicate"]
)
async def test_output_limit_ack_never_proves_delivery_or_replays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    monkeypatch.setattr(fwd, "_OUTPUT_LIMIT_CONTINUE_TIMEOUT_SECONDS", 1.0)
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    codex.receipt_fault = fault
    request = codex.request
    reads = 0

    async def bounded_read(method: str, params: dict) -> dict:
        nonlocal reads
        if method == "thread/read":
            reads += 1
            if reads > 1:
                raise ConnectionError("receipt verification transport closed")
        return await request(method, params)

    monkeypatch.setattr(codex, "request", bounded_read)
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    first = read_bridge_state(tmp_path)
    assert first.automatic_input_outcome == "uncertain"
    assert first.automatic_input_turn_id is None
    assert any(body["data"].get("item_type") == "error" for _, body in client.posts)
    guidance = [
        body["data"]["item_data"]["message"]
        for _, body in client.posts
        if body["data"].get("item_type") == "error"
    ]
    assert all(
        "unconfirmed" in message and "Inspect the terminal" in message for message in guidance
    )
    assert all("Ask it to continue" not in message for message in guidance)
    await _forward_output_limit_event(
        client,
        codex,
        fwd._CodexForwarderState(),
        tmp_path,
        "turn/completed",
        _output_limit_completion(),
    )
    assert [method for method, _ in codex.requests].count("turn/start") == 1
    assert read_bridge_state(tmp_path).automatic_input_deadline == first.automatic_input_deadline


@pytest.mark.parametrize("receipt_after_reconnect", [False, True])
async def test_output_limit_reconnect_uses_one_fixed_budget_and_one_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt_after_reconnect: bool,
) -> None:
    monkeypatch.setattr(fwd, "_OUTPUT_LIMIT_CONTINUE_TIMEOUT_SECONDS", 0.05)
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    normal = codex.request

    async def request(method: str, params: dict) -> dict:
        if method == "turn/start":
            await normal(method, params)
            await asyncio.sleep(0.03)
            raise ConnectionError("ack lost")
        return await normal(method, params)

    async def connect() -> None:
        if not receipt_after_reconnect:
            await asyncio.sleep(0.04)

    monkeypatch.setattr(codex, "request", request)
    monkeypatch.setattr(codex, "connect", connect)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert [method for method, _ in codex.requests].count("turn/start") == 1
    owner = read_bridge_state(tmp_path)
    assert owner.automatic_input_outcome == (
        "accepted" if receipt_after_reconnect else "uncertain"
    )
    assert ("thread/read" in [method for method, _ in codex.requests]) is receipt_after_reconnect


@pytest.mark.parametrize(
    "change",
    ["stop", "turn", "aba", "session_id", "thread_id", "socket_path", "codex_home", "shell"],
)
async def test_output_limit_flush_cannot_send_after_newer_control(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import record_codex_interrupt

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")

    async def flush(*args, **kwargs) -> None:
        owner = read_bridge_state(tmp_path)
        assert owner.active_turn_id is None
        if change == "stop":
            record_codex_interrupt(tmp_path, expected=owner)
        elif change in {"turn", "aba"}:
            update_active_turn_id(tmp_path, "manual")
            if change == "aba":
                update_active_turn_id(tmp_path, None)
        else:
            field = "pending_shell_command_id" if change == "shell" else change
            write_bridge_state(tmp_path, replace(owner, **{field: "replacement"}))

    monkeypatch.setattr(fwd, "_flush_turn_diff", flush)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert codex.requests == []
    assert read_bridge_state(tmp_path).active_turn_id == ("manual" if change == "turn" else None)


@pytest.mark.parametrize(
    "change", ["stop", "turn", "aba", "session_id", "thread_id", "socket_path", "codex_home"]
)
async def test_output_limit_receipt_cannot_adopt_newer_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import record_codex_interrupt

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    normal = codex.request

    async def request(method: str, params: dict) -> dict:
        result = await normal(method, params)
        if method == "turn/start":
            owner = read_bridge_state(tmp_path)
            if change == "stop":
                record_codex_interrupt(tmp_path, expected=owner)
            elif change in {"turn", "aba"}:
                update_active_turn_id(tmp_path, "manual")
                if change == "aba":
                    update_active_turn_id(tmp_path, None)
            else:
                write_bridge_state(tmp_path, replace(owner, **{change: "replacement"}))
        return result

    monkeypatch.setattr(codex, "request", request)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    methods = [method for method, _ in codex.requests]
    assert methods.count("turn/start") == 1
    assert methods.count("turn/interrupt") == int(change in {"stop", "turn", "aba"})
    owner = read_bridge_state(tmp_path)
    assert owner.active_turn_id == ("manual" if change == "turn" else None)
    assert owner.automatic_input_outcome != "accepted"


@pytest.mark.parametrize("receipt", ["owned", "foreign", "missing"])
@pytest.mark.parametrize("cancel_fails", [False, True])
async def test_output_limit_late_receipt_cancels_exact_input_once_and_waits_for_native_idle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    receipt: str,
    cancel_fails: bool,
) -> None:
    from omnigent.harnesses.codex_native.bridge import (
        claim_codex_automatic_input,
        record_codex_interrupt,
    )

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    claimed = claim_codex_automatic_input(
        tmp_path, read_bridge_state(tmp_path), client_message_id="exact"
    )
    assert claimed is not None
    record_codex_interrupt(tmp_path, expected=claimed)
    normal = codex.request

    async def request(method: str, params: dict) -> dict:
        result = await normal(method, params)
        if cancel_fails:
            raise ConnectionError("cancel lost")
        return result

    monkeypatch.setattr(codex, "request", request)
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "turn/started",
        {"threadId": "thread_123", "turn": {"id": "late"}},
    )
    item = {
        "type": "userMessage",
        "id": "input",
        "content": [
            {"type": "text", "text": fwd._OUTPUT_LIMIT_CONTINUE_TEXT},
        ],
    }
    if receipt != "missing":
        item["clientId"] = "exact" if receipt == "owned" else "foreign"
    for _ in range(2):
        await _forward_output_limit_event(
            client,
            codex,
            state,
            tmp_path,
            "item/completed",
            {
                "threadId": "thread_123",
                "turnId": "late",
                "item": item,
            },
        )
    assert len(codex.requests) == int(receipt == "owned")
    assert read_bridge_state(tmp_path).active_turn_id == "late"
    if receipt == "owned":
        assert read_bridge_state(tmp_path).automatic_input_outcome == (
            "cancel_unconfirmed" if cancel_fails else "cancel_acknowledged"
        )
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "turn/completed",
        {
            "threadId": "thread_123",
            "turn": {"id": "late", "status": "interrupted"},
        },
    )
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert client.posts[-1][1]["data"]["status"] == "idle"


async def test_output_limit_deferred_error_preserves_native_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fwd, "_OUTPUT_LIMIT_TERMINAL_WAIT_SECONDS", 0.001, raising=False)
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "error",
        {
            "threadId": "thread_123",
            "turnId": "turn_a",
            "willRetry": False,
            "error": _OUTPUT_LIMIT_ERROR,
        },
    )
    assert read_bridge_state(tmp_path).active_turn_id == "turn_a"
    await state.pending_output_limit_errors["turn_a"].timer
    assert read_bridge_state(tmp_path).active_turn_id == "turn_a"
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert codex.requests == []
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert client.posts[-1][1]["data"]["status"] == "idle"


@pytest.mark.parametrize("native_status", ["inProgress", "completed", "failed"])
async def test_output_limit_replay_reconciles_exact_receipt_without_resubmission(
    tmp_path: Path,
    native_status: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import claim_codex_automatic_input

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    assert claim_codex_automatic_input(
        tmp_path, read_bridge_state(tmp_path), client_message_id="exact"
    )
    codex.client_id = "exact"
    codex.turn_status = native_status
    await fwd._replay_resume_response(
        client,
        session_id="conv_x",
        bridge_dir=tmp_path,
        response=codex.snapshot(),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
        codex_client=codex,
    )
    assert codex.requests == []
    owner = read_bridge_state(tmp_path)
    assert owner.automatic_input_turn_id == "turn_auto"
    assert owner.active_turn_id == ("turn_auto" if native_status == "inProgress" else None)
    assert client.posts[-1][1]["data"]["status"] == (
        "running"
        if native_status == "inProgress"
        else "failed"
        if native_status == "failed"
        else "idle"
    )


@pytest.mark.parametrize("status", [None, "inProgress", "unknown"])
async def test_output_limit_terminal_tool_snapshot_fails_closed(
    tmp_path: Path, status: str | None
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client,
        codex,
        state,
        tmp_path,
        "turn/completed",
        _output_limit_completion(
            items=[{"type": "commandExecution", "id": "tool", "status": status}]
        ),
    )
    assert codex.requests == []
    assert client.posts[-1][1]["data"]["status"] == "failed"


@pytest.mark.parametrize("native_status", ["inProgress", "completed", "failed"])
async def test_output_limit_old_replay_after_manual_turn_does_not_reacquire_control(
    tmp_path: Path,
    native_status: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import claim_codex_automatic_input

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    assert claim_codex_automatic_input(
        tmp_path, read_bridge_state(tmp_path), client_message_id="exact"
    )
    update_active_turn_id(tmp_path, "manual")
    update_active_turn_id(tmp_path, None)
    codex.client_id = "exact"
    codex.turn_status = native_status
    await fwd._replay_resume_response(
        client,
        session_id="conv_x",
        bridge_dir=tmp_path,
        response=codex.snapshot(),
        usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
        elicitation_tracker=fwd._CodexElicitationTaskTracker(),
        forwarder_state=state,
        codex_client=codex,
    )
    assert codex.requests == []
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)


@pytest.mark.parametrize("native_status", ["inProgress", "completed", "failed"])
@pytest.mark.parametrize(
    "change", ["stop", "turn", "aba", "session_id", "thread_id", "socket_path", "codex_home"]
)
async def test_output_limit_status_publication_checks_final_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    native_status: str,
    change: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import record_codex_interrupt

    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    codex.turn_status = native_status
    _seed_active_turn(tmp_path, "turn_a")
    settle = fwd.settle_codex_automatic_input

    def change_after_settlement(*args, **kwargs):
        result = settle(*args, **kwargs)
        if kwargs["outcome"] == "accepted":
            owner = read_bridge_state(tmp_path)
            if change == "stop":
                record_codex_interrupt(tmp_path, expected=owner)
            elif change in {"turn", "aba"}:
                update_active_turn_id(tmp_path, "manual")
                if change == "aba":
                    update_active_turn_id(tmp_path, None)
            else:
                write_bridge_state(tmp_path, replace(owner, **{change: "replacement"}))
        return result

    monkeypatch.setattr(fwd, "settle_codex_automatic_input", change_after_settlement)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)
    assert read_bridge_state(tmp_path).active_turn_id == (
        "manual"
        if change == "turn"
        else None
        if change == "aba" or (change == "stop" and native_status != "inProgress")
        else "turn_auto"
    )


@pytest.mark.parametrize("outcome", [None, "uncertain"])
@pytest.mark.parametrize("activity", ["resume", "start", "delta"])
async def test_output_limit_claim_retires_completed_source_before_receipt_after_restart(
    tmp_path: Path,
    outcome: str | None,
    activity: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import claim_codex_automatic_input

    client, codex = _RecordingClient(), _ContinuingCodexClient()
    _seed_active_turn(tmp_path, "turn_a")
    claim = claim_codex_automatic_input(
        tmp_path, read_bridge_state(tmp_path), client_message_id="exact"
    )
    assert claim is not None
    if outcome is not None:
        assert fwd.settle_codex_automatic_input(tmp_path, expected=claim, outcome=outcome)
    restarted = fwd._CodexForwarderState()
    if activity == "resume":
        await fwd._replay_resume_response(
            client,
            session_id="conv_x",
            bridge_dir=tmp_path,
            response={
                "result": {
                    "thread": {
                        "id": "thread_123",
                        "turns": [
                            {
                                "id": "turn_a",
                                "status": "inProgress",
                                "items": [],
                            }
                        ],
                    }
                }
            },
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            forwarder_state=restarted,
            codex_client=codex,
        )
    else:
        params = {"threadId": "thread_123", "turnId": "turn_a"}
        if activity == "start":
            params["turn"] = {"id": "turn_a"}
            method = "turn/started"
        else:
            params.update(itemId="old-text", delta="stale source text")
            method = "item/agentMessage/delta"
        coalescer = fwd._OutputTextDeltaCoalescer(client, "conv_x")
        try:
            await _forward_output_limit_event(
                client,
                codex,
                restarted,
                tmp_path,
                method,
                params,
                delta_coalescer=coalescer,
            )
        finally:
            await coalescer.close()
    owner = read_bridge_state(tmp_path)
    assert owner.active_turn_id is None
    assert owner.automatic_input_turn_id is None
    assert owner.automatic_input_outcome == outcome
    assert codex.requests == []
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)
    await _forward_output_limit_event(
        client,
        codex,
        restarted,
        tmp_path,
        "error",
        {
            "threadId": "thread_123",
            "turnId": "turn_a",
            "willRetry": False,
            "error": _OUTPUT_LIMIT_ERROR,
        },
    )
    assert any(_OUTPUT_LIMIT_ERROR["message"] in json.dumps(body) for _, body in client.posts)
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)
    assert codex.requests == []


@pytest.mark.parametrize(
    "native_status", ["paused", "", "future-status", None, {"type": "paused"}]
)
async def test_output_limit_unknown_receipt_status_does_not_prove_native_completion(
    tmp_path: Path,
    native_status: object,
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    codex.turn_status = native_status
    _seed_active_turn(tmp_path, "turn_a")
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    owner = read_bridge_state(tmp_path)
    assert owner.automatic_input_turn_id == "turn_auto"
    assert owner.automatic_input_outcome == "uncertain"
    assert owner.active_turn_id is None
    assert [method for method, _ in codex.requests] == ["turn/start", "thread/read"]
    assert any(_OUTPUT_LIMIT_ERROR["message"] in json.dumps(body) for _, body in client.posts)
    guidance = [
        body["data"]["item_data"]["message"]
        for _, body in client.posts
        if body["data"].get("item_type") == "error"
    ]
    assert guidance
    assert all(
        "unconfirmed" in message and "Inspect the terminal" in message for message in guidance
    )
    assert all("Ask it to continue" not in message for message in guidance)
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)


@pytest.mark.parametrize("outcome", [None, "uncertain"])
@pytest.mark.parametrize("delivery", ["resume", "turn/completed", "turn/failed"])
@pytest.mark.parametrize("native_status", ["failed", "completed", "interrupted"])
@pytest.mark.parametrize("newer", ["idle", "manual_active", "manual_idle", "stop"])
async def test_output_limit_retired_source_terminal_is_error_only_after_restart(
    tmp_path: Path,
    outcome: str | None,
    delivery: str,
    native_status: str,
    newer: str,
) -> None:
    from omnigent.harnesses.codex_native.bridge import (
        claim_codex_automatic_input,
        record_codex_interrupt,
    )

    client, codex = _RecordingClient(), _ContinuingCodexClient()
    _seed_active_turn(tmp_path, "turn_a")
    claim = claim_codex_automatic_input(
        tmp_path, read_bridge_state(tmp_path), client_message_id="exact"
    )
    assert claim is not None
    if outcome is not None:
        assert fwd.settle_codex_automatic_input(tmp_path, expected=claim, outcome=outcome)
    if newer.startswith("manual"):
        update_active_turn_id(tmp_path, "manual")
        if newer == "manual_idle":
            update_active_turn_id(tmp_path, None)
    elif newer == "stop":
        assert record_codex_interrupt(tmp_path, expected=read_bridge_state(tmp_path))
    observed = read_bridge_state(tmp_path)
    restarted = fwd._CodexForwarderState()
    turn = {"id": "turn_a", "status": native_status, "error": _OUTPUT_LIMIT_ERROR, "items": []}
    if delivery == "resume":
        await fwd._replay_resume_response(
            client,
            session_id="conv_x",
            bridge_dir=tmp_path,
            response={"result": {"thread": {"id": "thread_123", "turns": [turn]}}},
            usage_coalescer=fwd._SessionUsageCoalescer(client, "conv_x"),
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            forwarder_state=restarted,
            codex_client=codex,
        )
    else:
        await _forward_output_limit_event(
            client, codex, restarted, tmp_path, delivery, {"threadId": "thread_123", "turn": turn}
        )
    assert read_bridge_state(tmp_path) == observed
    assert codex.requests == []
    assert not any(body["type"] == "external_session_status" for _, body in client.posts)
    assert any(_OUTPUT_LIMIT_ERROR["message"] in json.dumps(body) for _, body in client.posts)


async def test_output_limit_deadline_before_send_settles_uncertainty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, codex, state = _RecordingClient(), _ContinuingCodexClient(), fwd._CodexForwarderState()
    _seed_active_turn(tmp_path, "turn_a")
    monkeypatch.setattr(fwd, "_OUTPUT_LIMIT_CONTINUE_TIMEOUT_SECONDS", 0.01)

    async def slow_flush(*args, **kwargs) -> None:
        await asyncio.sleep(0.03)

    monkeypatch.setattr(fwd, "_flush_turn_diff", slow_flush)
    await _forward_output_limit_event(
        client, codex, state, tmp_path, "turn/completed", _output_limit_completion()
    )
    assert codex.requests == []
    assert read_bridge_state(tmp_path).automatic_input_outcome == "uncertain"
    assert read_bridge_state(tmp_path).active_turn_id is None
    assert any(_OUTPUT_LIMIT_ERROR["message"] in json.dumps(body) for _, body in client.posts)
    assert any("Ask it to continue" in json.dumps(body) for _, body in client.posts)
