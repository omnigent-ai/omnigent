"""Runner control failures stay diagnosable from what reaches the server.

A native control request that fails on a shared runner crosses to the server
only as its HTTP response plus the server's own log rows; the runner's log may
never leave the host. These tests pin what that response and those rows carry
for a child session: a stable failure ID, bounded exception metadata, and the
child's session id — never the raw exception text.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.debug_logging import PRIMARY_SESSION_ID_ENV_VAR
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import native_controls
from omnigent.server.routes._sessions import helpers as session_helpers
from tests.debug_log_helpers import capture_debug_rows
from tests.runner.conftest import _build_app_for_spec, _runner_client
from tests.runner.native_helpers import _harness_spec

_PARENT = "11111111111111111111111111111111"
_CHILD = "22222222222222222222222222222222"
_AGENT = "33333333333333333333333333333333"
_MODEL = "claude-opus-4-7"
_MODEL_CHANGE = {"type": "model_change", "model": _MODEL}
_FAILURE_ID_RE = re.compile(r"err_[0-9a-f]{32}")
# Stands for any server-side detail a response must not relay verbatim.
_PRIVATE_DETAIL = "synthetic failure in /private/runner/path"
_FORWARD_LOGGER = "omnigent.server.routes.sessions"


@pytest.fixture(autouse=True)
def _isolated_claude_model_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``/model claude-opus-4-7`` without a live pane or gateway pins."""
    for var in (
        "ANTHROPIC_DEFAULT_OPUS_MODEL",
        "ANTHROPIC_DEFAULT_SONNET_MODEL",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        claude_native_bridge,
        "read_model_env",
        lambda _bridge_dir: {"ANTHROPIC_CUSTOM_MODEL_OPTION": _MODEL},
    )
    monkeypatch.setattr(claude_native_bridge, "read_claude_status_model", lambda _bridge_dir: None)


async def _child_session_app() -> FastAPI:
    """Build the real runner app with a claude-native child session registered."""
    app, _ = await _build_app_for_spec(_harness_spec("claude-native"))
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": _CHILD, "agent_id": _AGENT}
        )
        assert created.status_code == 201, created.text
    return app


def _runner_client_for(app: FastAPI, *, raise_app_exceptions: bool = True) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://runner",
    )


def _fail_slash_command_injection(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Make the tmux injection fail: the handled ``claude_native_model_failed`` path."""
    calls: list[str] = []

    def _failing_inject(bridge_dir: Any, *, command: str, timeout_s: float, **_: Any) -> None:
        del bridge_dir, timeout_s
        calls.append(command)
        raise RuntimeError(_PRIVATE_DETAIL)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _failing_inject)
    return calls


def _fail_bridge_lookup(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> list[str]:
    """Make the bridge lookup raise before any handler-level ``except`` can catch it."""
    calls: list[str] = []

    async def _failing_lookup(*, session_id: str, **_: Any) -> str:
        calls.append(session_id)
        raise exc

    monkeypatch.setattr(native_controls, "_claude_native_bridge_id_for_session", _failing_lookup)
    return calls


def _failure_id(payload: str) -> str:
    match = _FAILURE_ID_RE.search(payload)
    assert match is not None, f"no stable failure ID in: {payload!r}"
    return match.group(0)


def _rows_with_failure_id(rows: list[dict[str, Any]], failure_id: str) -> list[dict[str, Any]]:
    return [row for row in rows if row["attributes"].get("failure_id") == failure_id]


def _warning_rows(
    rows: list[dict[str, Any]], *, logger: str | None = None
) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row["level"] == "WARNING" and (logger is None or row["logger_name"] == logger)
    ]


def _model_change_failure_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
    [row] = [r for r in _warning_rows(rows) if "model change failed" in str(r["message"])]
    return row


def _await_value(value: Any) -> Callable[..., Coroutine[Any, Any, Any]]:
    async def _coro(*_args: Any, **_kwargs: Any) -> Any:
        return value

    return _coro


async def test_unexpected_control_failure_answers_a_structured_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception no runner handler covers still yields a coded, correlatable 500."""
    app = await _child_session_app()
    calls = _fail_bridge_lookup(monkeypatch, RuntimeError(_PRIVATE_DETAIL))

    async with _runner_client_for(app, raise_app_exceptions=False) as client:
        with capture_debug_rows("runner") as rows:
            response = await client.post(f"/v1/sessions/{_CHILD}/events", json=_MODEL_CHANGE)

    assert calls == [_CHILD]
    assert response.status_code == 500
    assert _PRIVATE_DETAIL not in response.text
    assert response.headers.get("content-type", "").startswith("application/json"), (
        f"bare framework 500 instead of a structured error body: {response.text!r}"
    )
    body = response.json()
    # The runner's coded error envelope, with the client-safe detail as its message.
    assert body["error"] == {"code": "runner_request_failed", "message": body["detail"]}, body
    failure_id = _failure_id(response.text)
    assert body["failure_context"]["failure_id"] == failure_id
    [row] = _rows_with_failure_id(rows, failure_id)
    assert row["session_id"] == _CHILD
    assert row["attributes"]["exception_type"] == "RuntimeError"
    assert row["event_name"], "the row must name the failing operation"


async def test_handled_control_failure_returns_a_failure_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The client-safe 503 keeps its code and carries an ID the runner row shares."""
    app = await _child_session_app()
    calls = _fail_slash_command_injection(monkeypatch)

    async with _runner_client_for(app) as client:
        with capture_debug_rows("runner") as rows:
            response = await client.post(f"/v1/sessions/{_CHILD}/events", json=_MODEL_CHANGE)

    assert calls == [f"/model {_MODEL}"]
    assert response.status_code == 503
    body = response.json()
    assert body["error"] == "claude_native_model_failed"
    assert _PRIVATE_DETAIL not in response.text
    failure_id = _failure_id(response.text)
    [row] = _rows_with_failure_id(rows, failure_id)
    assert row["attributes"]["exception_type"] == "RuntimeError"


async def test_handled_control_failure_row_names_the_child_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a shared runner the failing child's row is not attributed to the primary session."""
    monkeypatch.setenv(PRIMARY_SESSION_ID_ENV_VAR, _PARENT)
    app = await _child_session_app()
    _fail_slash_command_injection(monkeypatch)

    async with _runner_client_for(app) as client:
        with capture_debug_rows("runner") as rows:
            response = await client.post(f"/v1/sessions/{_CHILD}/events", json=_MODEL_CHANGE)

    assert response.status_code == 503
    row = _model_change_failure_row(rows)
    assert row["session_id"] == _CHILD, f"attributed to {row['session_id']!r}"


async def test_forward_of_an_unexpected_runner_500_keeps_the_child_failure_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server forward + model-failure rows retain the runner's failure ID for the child."""
    app = await _child_session_app()
    _fail_bridge_lookup(monkeypatch, RuntimeError(_PRIVATE_DETAIL))
    published: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        session_helpers, "_publish_error_event", lambda sid, err: published.append((sid, err))
    )

    async with _runner_client_for(app, raise_app_exceptions=False) as runner_client:
        monkeypatch.setattr(session_helpers, "_get_runner_client", _await_value(runner_client))
        with capture_debug_rows("server") as rows:
            result = await session_helpers._forward_session_change_to_runner_impl(
                _CHILD, None, _MODEL_CHANGE
            )
            assert result is not None and result.status_code == 500
            assert session_helpers._surface_model_change_forward_failure(_CHILD, _MODEL, result)

    assert _PRIVATE_DETAIL not in result.body
    failure_id = _failure_id(result.body)
    forward_rows = _warning_rows(rows, logger=_FORWARD_LOGGER)
    assert len(forward_rows) == 2, [r["message"] for r in forward_rows]
    for row in forward_rows:
        assert row["session_id"] == _CHILD
        assert row["attributes"].get("failure_id") == failure_id, row
    [(notice_session, notice)] = published
    assert notice_session == _CHILD
    assert notice.code == "model_change_not_applied"


async def test_coded_control_failures_keep_their_status_and_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: an already-coded failure keeps the existing structured 400."""
    app = await _child_session_app()
    _fail_bridge_lookup(monkeypatch, ValueError("unknown model spelling"))

    async with _runner_client_for(app) as client:
        response = await client.post(f"/v1/sessions/{_CHILD}/events", json=_MODEL_CHANGE)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_input"


async def test_handled_control_failure_row_names_the_child_without_a_primary_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Control: with no primary session configured the request scope attributes the child."""
    monkeypatch.delenv(PRIMARY_SESSION_ID_ENV_VAR, raising=False)
    app = await _child_session_app()
    _fail_slash_command_injection(monkeypatch)

    async with _runner_client_for(app) as client:
        with capture_debug_rows("runner") as rows:
            response = await client.post(f"/v1/sessions/{_CHILD}/events", json=_MODEL_CHANGE)

    assert response.status_code == 503
    assert _model_change_failure_row(rows)["session_id"] == _CHILD
