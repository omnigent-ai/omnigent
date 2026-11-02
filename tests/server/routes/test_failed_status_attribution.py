"""Coverage for failure attribution on the server's ``failed`` status edge.

The server logs the first failure and later diagnostic enrichment from
``_publish_status``. These tests preserve turn identity, distinct causes,
and the publish path that produced each observation.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

import omnigent.server.routes._sessions.helpers as helpers
from omnigent.db.db_models import workspace_scope
from omnigent.debug_logging import _attributes
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.failed_status_diagnostics import (
    bound_runner_connection,
    failed_status_intake_attributes,
)
from omnigent.server.schemas import ErrorDetail
from tests.server.helpers import start_session_stream_collector

# Statuses that are not failures. Anything else a publish site can pass —
# the ``failed`` literal, or a relayed status variable that may hold it —
# has to carry an origin.
_NON_FAILURE_STATUSES = frozenset({"idle", "running", "waiting", "launching"})


@pytest.fixture(autouse=True)
def _clean_status_cache() -> None:
    """Keep the module-level status caches from leaking between tests."""
    helpers._session_status_cache.clear()
    helpers._session_active_response_cache.clear()
    helpers._session_failure_observations.clear()
    yield
    helpers._session_status_cache.clear()
    helpers._session_active_response_cache.clear()
    helpers._session_failure_observations.clear()


def _publish_failed(
    caplog: pytest.LogCaptureFixture,
    *,
    session_id: str = "conv_attr1",
    error: ErrorDetail | None = None,
    origin: str | None = None,
    response_id: str | None = None,
    previous: str | None = "running",
) -> logging.LogRecord:
    """Publish one failed edge and return the ERROR record it logged."""
    if previous is not None:
        helpers._session_status_cache[session_id] = previous
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        helpers._publish_status(
            session_id,
            "failed",
            error,
            response_id=response_id,
            failure_origin=origin,
        )
    records = [r for r in caplog.records if "session turn failed" in r.getMessage()]
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    return records[0]


def test_failed_edge_names_its_origin_code_and_previous_status(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ERROR line separates the causes that share this one signature."""
    record = _publish_failed(
        caplog,
        error=ErrorDetail(
            source="execution",
            code="runner_disconnected",
            message="Runner disconnected unexpectedly.",
        ),
        origin="runner_disconnected_mid_turn",
        response_id="resp_abc123",
    )
    message = record.getMessage()
    assert "origin=runner_disconnected_mid_turn" in message
    assert "code=runner_disconnected" in message
    assert "prev=running" in message
    # The human-readable detail keeps its position at the end of the line so
    # detail-matching queries built on the old shape still match.
    assert message.endswith(": Runner disconnected unexpectedly.")
    attributes = record.attributes
    assert attributes["origin"] == "runner_disconnected_mid_turn"
    assert attributes["code"] == "runner_disconnected"
    assert attributes["previous_status"] == "running"
    assert attributes["response_id"] == "resp_abc123"
    assert record.event_name == "session_turn_failed"
    assert record.session_id == "conv_attr1"


def test_detail_less_failure_still_reports_origin(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure with no ``ErrorDetail`` is the case that most needs a path."""
    record = _publish_failed(caplog, error=None, origin="native_terminal_boot_failed")
    message = record.getMessage()
    assert "origin=native_terminal_boot_failed" in message
    assert "code=none" in message
    assert message.endswith(": no detail")
    assert record.attributes["code"] == "none"
    # No turn to name; the sink drops null attributes rather than shipping
    # an empty column.
    assert record.attributes["response_id"] is None
    assert "response_id" not in _attributes(record, source="server")


def test_unattributed_failure_is_labelled_not_blank(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An origin-less publish stays greppable instead of logging an empty field."""
    record = _publish_failed(
        caplog,
        error=ErrorDetail(source="execution", code="runner_error", message="boom"),
        origin=None,
        previous=None,
    )
    assert "origin=unattributed" in record.getMessage()
    assert "prev=unknown" in record.getMessage()
    assert record.attributes["origin"] == "unattributed"


async def test_same_turn_failure_observations_preserve_detail(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collector = await start_session_stream_collector("conv_merge")
    detail = ErrorDetail(code="transient_upstream_error", message="Server error mid-response")
    try:
        helpers._publish_status("conv_merge", "running", response_id="resp_merge")
        await collector.next_event()
        monkeypatch.setattr(helpers.time, "time", lambda: 100.0)
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            for response_id, error in [
                (None, None),
                ("resp_merge", None),
                ("resp_merge", detail),
                ("resp_merge", detail),
                ("resp_merge", None),
            ]:
                if error is not None:
                    monkeypatch.setattr(helpers.time, "time", lambda: 200.0)
                helpers._publish_status(
                    "conv_merge",
                    "failed",
                    error,
                    response_id=response_id,
                    failure_origin="relayed_runner_status"
                    if error is None
                    else "external_session_status",
                )
        events = [await collector.next_event() for _ in range(5)]
    finally:
        await collector.stop()

    assert [event["response_id"] for event in events] == ["resp_merge"] * 5
    assert [event["error"] for event in events] == [None, None] + [detail.model_dump()] * 3
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"
    ]
    assert [r.attributes["code"] for r in records] == ["none", "transient_upstream_error"]
    assert records[1].attributes["failure_observation"] == "enriched"
    assert records[1].attributes["previous_failure_origin"] == "relayed_runner_status"
    assert records[1].attributes["first_failure_at_ms"] == "100000"
    assert all(r.attributes["response_id"] == "resp_merge" for r in records)


async def test_late_failure_does_not_close_a_different_active_turn(
    caplog: pytest.LogCaptureFixture,
) -> None:
    collector = await start_session_stream_collector("conv_late")
    try:
        with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
            helpers._publish_status(
                "conv_late",
                "failed",
                response_id="resp_old",
                failure_origin="relayed_runner_status",
            )
            helpers._publish_status("conv_late", "running", response_id="resp_new")
            await collector.next_event()
            await collector.next_event()
            helpers._publish_status(
                "conv_late",
                "failed",
                ErrorDetail(code="runner_error", message="old cause"),
                response_id="resp_old",
                failure_origin="external_session_status",
            )
        assert helpers._session_status_cache["conv_late"] == "running"
        assert helpers._session_active_response_cache["conv_late"] == "resp_new"
        await collector.assert_no_event(within=0.02)
    finally:
        await collector.stop()
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"
    ]
    assert len(records) == 2
    assert records[1].attributes["failure_observation"] == "enriched"
    assert records[1].attributes["failure_status_applied"] == "false"
    assert records[1].attributes["response_id"] == "resp_old"


async def test_unrecognized_response_failure_is_not_assumed_stale() -> None:
    collector = await start_session_stream_collector("conv_unrecognized")
    try:
        helpers._publish_status("conv_unrecognized", "running", response_id="resp_placeholder")
        await collector.next_event()
        helpers._publish_status(
            "conv_unrecognized",
            "failed",
            response_id="resp_actual",
            failure_origin="external_session_status",
        )
        event = await collector.next_event()
        assert event["status"] == "failed"
        assert event["response_id"] == "resp_actual"
        assert helpers._session_status_cache["conv_unrecognized"] == "failed"
    finally:
        await collector.stop()


@pytest.mark.parametrize(
    ("next_context", "disconnect_evidence", "expected_logs", "last_code"),
    [
        ({"failure_id": "second"}, None, 2, "none"),
        ({"failure_id": "first", "http_status": 503}, None, 2, "runner_error"),
        ({"failure_id": "first"}, {"drop_close_code": "1006"}, 2, "runner_error"),
        ({"failure_id": "first", "diagnostic_last_read_at": 200}, None, 1, "runner_error"),
    ],
)
def test_same_error_keeps_new_failure_evidence(
    caplog: pytest.LogCaptureFixture,
    next_context: dict[str, str | int],
    disconnect_evidence: dict[str, str] | None,
    expected_logs: int,
    last_code: str,
) -> None:
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        helpers._publish_status(
            "conv_evidence",
            "failed",
            ErrorDetail(code="runner_error", message="known cause"),
            response_id="resp_evidence",
            failure_origin="relayed_runner_status",
            failure_context={"failure_id": "first", "diagnostic_last_read_at": 100},
        )
        for _ in range(2):
            helpers._publish_status(
                "conv_evidence",
                "failed",
                response_id="resp_evidence",
                failure_origin="external_session_status",
                failure_context=next_context,
                disconnect_evidence=disconnect_evidence,
            )
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"
    ]
    assert len(records) == expected_logs
    assert records[0].attributes["code"] == "runner_error"
    assert records[-1].attributes["code"] == last_code
    if expected_logs == 2:
        observation = "additional" if next_context.get("failure_id") == "second" else "enriched"
        assert records[1].attributes["failure_observation"] == observation
        for key, value in (next_context | (disconnect_evidence or {})).items():
            assert records[1].attributes[key] == str(value)


def test_additional_failure_keeps_own_classification_through_enrichment(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        for error, context in [
            (
                ErrorDetail(code="runner_error", message="first cause"),
                {"failure_id": "first", "http_status": 503},
            ),
            (None, {"failure_id": "second"}),
            (None, {"failure_id": "second", "http_status": 503}),
            (
                ErrorDetail(code="transient_upstream_error", message="second cause"),
                {"failure_id": "second", "http_status": 503},
            ),
        ]:
            helpers._publish_status(
                "conv_additional",
                "failed",
                error,
                response_id="resp_additional",
                failure_origin="external_session_status",
                failure_context=context,
            )
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"
    ]
    assert [r.attributes["code"] for r in records] == [
        "runner_error",
        "none",
        "none",
        "transient_upstream_error",
    ]
    assert records[-1].attributes["failure_observation"] == "enriched"


@pytest.mark.parametrize(
    ("first_response", "next_response", "next_error"),
    [
        (None, None, None),
        ("resp_first", "resp_second", None),
        ("resp_first", None, None),
        ("resp_first", "resp_first", ErrorDetail(code="runner_error", message="second cause")),
    ],
)
def test_independent_failure_observations_remain_visible(
    caplog: pytest.LogCaptureFixture,
    first_response: str | None,
    next_response: str | None,
    next_error: ErrorDetail | None,
) -> None:
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        helpers._publish_status(
            "conv_independent",
            "failed",
            ErrorDetail(code="runner_error", message="first cause"),
            response_id=first_response,
            failure_origin="relayed_runner_status",
        )
        helpers._publish_status(
            "conv_independent",
            "failed",
            next_error,
            response_id=next_response,
            failure_origin="external_session_status",
        )
    records = [
        r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"
    ]
    assert len(records) == 2
    assert records[1].attributes["response_id"] == next_response
    assert records[1].getMessage().endswith(next_error.message if next_error else "no detail")


@pytest.mark.parametrize("status", ["running", "waiting", "idle"])
def test_new_activity_or_confirmed_completion_resets_failure_observation(
    caplog: pytest.LogCaptureFixture,
    status: str,
) -> None:
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        helpers._publish_status(
            "conv_reset",
            "failed",
            response_id="resp_reset",
            failure_origin="relayed_runner_status",
        )
        helpers._publish_status("conv_reset", status, turn_completed=status == "idle")
        helpers._publish_status(
            "conv_reset",
            "failed",
            response_id="resp_reset",
            failure_origin="relayed_runner_status",
        )
    assert (
        sum(getattr(r, "event_name", None) == "session_turn_failed" for r in caplog.records) == 2
    )


def test_failure_observations_are_workspace_scoped(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        for workspace in (11, 22, 11):
            with workspace_scope(workspace):
                helpers._publish_status(
                    "conv_scoped",
                    "failed",
                    response_id="resp_scoped",
                    failure_origin="relayed_runner_status",
                )
    assert (
        sum(getattr(r, "event_name", None) == "session_turn_failed" for r in caplog.records) == 2
    )


def _failure_publish_calls(source: str) -> list[ast.Call]:
    """Return ``_publish_status`` calls that can publish a failure."""
    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
        if name != "_publish_status":
            continue
        status = node.args[1] if len(node.args) > 1 else None
        if isinstance(status, ast.Constant) and status.value in _NON_FAILURE_STATUSES:
            continue
        calls.append(node)
    return calls


def test_oversized_failure_detail_is_capped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Oversized fallback text is bounded and reports its omitted length."""
    reply = "Synthetic failure detail\n\n" + "detail " * 2000
    record = _publish_failed(
        caplog,
        error=ErrorDetail(source="execution", code="native_turn_error", message=reply),
        origin="external_session_status",
    )
    message = record.getMessage()
    assert "code=native_turn_error" in message
    detail = message.split("): ", 1)[1]
    assert len(detail) < 4700, detail
    assert detail.startswith("Synthetic failure detail")
    assert "chars)" in detail


def test_multiline_failure_detail_keeps_its_line_structure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A multiline error within the limit retains its diagnostic structure."""
    trace = 'API Error: 400 {\n  "error": {\n    "code": 400,\n    "message": "bad"\n  }\n}'
    record = _publish_failed(
        caplog,
        error=ErrorDetail(source="execution", code="native_turn_error", message=trace),
        origin="external_session_status",
    )
    assert record.getMessage().endswith(trace)


def test_runner_exit_reason_keeps_its_diagnostics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A runner-exit reason retains its exit code, log path, and traceback."""
    reason = (
        "runner process exited with code 1 "
        "(log on host: ~/.omnigent/logs/runner/runner-abc123-def456-7-8.log)\n"
        + "\n".join(f"traceback line {n}" for n in range(120))
    )
    record = _publish_failed(
        caplog,
        error=ErrorDetail(source="execution", code="runner_exited", message=reason),
        origin="runner_exited",
    )
    message = record.getMessage()
    assert "runner process exited with code 1" in message
    assert "~/.omnigent/logs/runner/runner-abc123-def456-7-8.log" in message
    assert "traceback line 100" in message, "the log tail was clipped too aggressively"
    # The tail's own line structure is what makes it readable.
    assert "traceback line 0\ntraceback line 1" in message


def test_short_failure_detail_is_preserved_verbatim(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Truncation must not touch the ordinary one-line reason."""
    record = _publish_failed(
        caplog,
        error=ErrorDetail(
            source="execution",
            code="native_terminal_ensure_failed",
            message="Native Codex terminal failed to start",
        ),
        origin="native_terminal_boot_failed",
    )
    assert record.getMessage().endswith(": Native Codex terminal failed to start")


def test_every_failure_publish_site_names_itself() -> None:
    """A new failure path cannot silently rejoin the undifferentiated bucket.

    The dashboard groups these ERRORs by nothing finer than the function they
    come from, so an unattributed site is invisible among the dozen other
    causes rather than merely under-described.

    Scans the whole server package rather than a list of known files: a
    hardcoded list would stop covering the moment a failure path moves to a new
    module, which is exactly when the guard is needed.
    """
    server_root = Path(helpers.__file__).parents[2]
    checked = 0
    for path in sorted(server_root.rglob("*.py")):
        for call in _failure_publish_calls(path.read_text()):
            checked += 1
            keywords = {kw.arg for kw in call.keywords}
            assert "failure_origin" in keywords, (
                f"{path.relative_to(server_root)}:{call.lineno} publishes a possible "
                "failure without failure_origin; the ERROR it logs would be "
                "unattributable"
            )
    # A scan that silently matched nothing would pass forever.
    assert checked >= 7, f"expected the known failure publish sites, found {checked}"


@pytest.mark.parametrize(
    ("data", "field", "state"),
    [
        ({}, "error", "missing"),
        ({"error": None}, "error", "null"),
        ({"error": []}, "error", "invalid"),
        ({"error": {}}, "error", "empty"),
        ({"error": {"code": 42}}, "code", "invalid"),
        ({"error": {"message": "  "}}, "message", "empty"),
        ({"output": "private-synthetic-token"}, "output", "present"),
        ({"failure_detail": "https://example.test/private"}, "detail", "present"),
        ({"failure_context": {}}, "context", "empty"),
        ({"failure_context": {"native_hook_event": "StopFailure"}}, "context", "present"),
    ],
)
def test_failed_status_intake_records_availability_without_content(
    data: dict[str, object], field: str, state: str
) -> None:
    attributes = failed_status_intake_attributes(
        data, runner_id="runner_one", connection=None, source="relay_stream"
    )
    assert attributes[f"failure_wire_{field}"] == state
    assert attributes["failure_connection_state"] == "unavailable"
    assert "private-synthetic-token" not in str(attributes)
    assert "example.test" not in str(attributes)
    assert "StopFailure" not in str(attributes)


@pytest.mark.asyncio
async def test_bound_runner_observation_is_explicit_and_ignores_replacement_identity() -> None:
    registry = TunnelRegistry()
    observed = registry.register(
        "runner_one",
        Mock(),
        HelloFrame(runner_version="0.20.0", frame_protocol_version=1, connection_id="conn_old"),
    )
    assert bound_runner_connection(registry, "runner_one") is observed
    replacement = registry.register(
        "runner_one",
        Mock(),
        HelloFrame(runner_version="0.21.0", frame_protocol_version=1, connection_id="conn_new"),
    )
    attributes = failed_status_intake_attributes(
        {}, runner_id="runner_one", connection=observed, source="bound_runner_at_intake"
    )
    assert attributes["failure_connection_source"] == "bound_runner_at_intake"
    assert attributes["failure_runner_version"] == "0.20.0"
    assert attributes["failure_connection_id"] == "conn_old"
    assert attributes["failure_connection_generation"] == str(observed.generation)
    assert observed.generation != replacement.generation
    assert bound_runner_connection(registry, "missing") is None
    assert bound_runner_connection(None, "runner_one") is None


@pytest.mark.parametrize(
    "connection",
    [
        {"runner_id": "another_runner", "runner_version": "0.20.0"},
        {"runner_id": "runner_one", "runner_version": "https://example.test/private"},
    ],
)
def test_failed_status_intake_rejects_mismatched_or_unsafe_connection_fields(
    connection: dict[str, object],
) -> None:
    attributes = failed_status_intake_attributes(
        {}, runner_id="runner_one", connection=connection, source="relay_stream"
    )
    assert "failure_runner_version" not in attributes
    assert "another_runner" not in str(attributes)
    assert "example.test" not in str(attributes)
