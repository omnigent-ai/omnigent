"""Coverage for failure attribution on the server's ``failed`` status edge.

Every server-originated failed turn logs exactly one ERROR from
``_publish_status``, so that single line is the whole population a
reliability dashboard sees. These tests hold it to naming which publish
path produced the edge, and hold the publish sites to passing one.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest

import omnigent.server.routes._sessions.helpers as helpers
from omnigent.debug_logging import _attributes
from omnigent.runner.transports.ws_tunnel.frames import HelloFrame
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.failed_status_diagnostics import (
    bound_runner_connection,
    failed_status_intake_attributes,
)
from omnigent.server.schemas import ErrorDetail

# Statuses that are not failures. Anything else a publish site can pass —
# the ``failed`` literal, or a relayed status variable that may hold it —
# has to carry an origin.
_NON_FAILURE_STATUSES = frozenset({"idle", "running", "waiting", "launching"})


@pytest.fixture(autouse=True)
def _clean_status_cache() -> None:
    """Keep the module-level status caches from leaking between tests."""
    helpers._session_status_cache.clear()
    helpers._session_active_response_cache.clear()
    yield
    helpers._session_status_cache.clear()
    helpers._session_active_response_cache.clear()
    for handle, _publish in list(helpers._held_bare_failures.values()):
        handle.cancel()
    helpers._held_bare_failures.clear()


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


def _call_name(node: ast.expr) -> str | None:
    """Return the called name of ``f`` or ``module.f``."""
    return node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", None)


def _failure_publish_calls(source: str) -> list[ast.Call]:
    """Return ``_publish_status`` calls, direct or bound with ``partial``, that can fail."""
    calls: list[ast.Call] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        args = node.args
        # ``functools.partial(_publish_status, ...)`` publishes later; check it the same way.
        if (
            _call_name(node.func) == "partial"
            and args
            and _call_name(args[0]) == "_publish_status"
        ):
            args = args[1:]
        elif _call_name(node.func) != "_publish_status":
            continue
        status = args[1] if len(args) > 1 else None
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


def _hold_bare_relayed_failure(session_id: str) -> None:
    """Hold a causeless ``failed`` edge the way the relay does for an older runner."""
    helpers.hold_bare_failure(
        session_id,
        functools.partial(
            helpers._publish_status,
            session_id,
            "failed",
            None,
            failure_origin="relayed_runner_status",
        ),
    )


def _turn_failures(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event_name", None) == "session_turn_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("repeats", [1, 2], ids=["one-bare-edge", "re-sent-bare-edge"])
async def test_held_bare_failure_is_replaced_by_its_cause(
    caplog: pytest.LogCaptureFixture, repeats: int
) -> None:
    """The turn fails once, with the hook's cause and the status it was leaving."""
    helpers._session_status_cache["conv_bare"] = "running"
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        for _ in range(repeats):
            _hold_bare_relayed_failure("conv_bare")
        assert helpers._session_status_cache.get("conv_bare") == "running"
        helpers._publish_status(
            "conv_bare",
            "failed",
            ErrorDetail(
                code="transient_upstream_error", message="API Error: Server error mid-response."
            ),
            failure_origin="external_session_status",
        )
    [failure] = _turn_failures(caplog)
    assert failure.attributes["code"] == "transient_upstream_error"
    assert failure.attributes["origin"] == "external_session_status"
    assert failure.attributes["previous_status"] == "running"
    assert "conv_bare" not in helpers._held_bare_failures
    # The cancelled grace timer must not publish the bare edge afterwards.
    await asyncio.sleep(0)
    assert len(_turn_failures(caplog)) == 1


@pytest.mark.asyncio
async def test_held_bare_failure_publishes_when_its_cause_never_arrives(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a cause, the bare edge publishes unchanged once the grace ends."""
    monkeypatch.setattr(helpers, "ERROR_HOOK_GRACE_S", 0.01)
    helpers._session_status_cache["conv_bare"] = "running"
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        _hold_bare_relayed_failure("conv_bare")
        for _ in range(100):
            if _turn_failures(caplog):
                break
            await asyncio.sleep(0.01)
    [failure] = _turn_failures(caplog)
    assert failure.attributes["code"] == "none"
    assert failure.attributes["origin"] == "relayed_runner_status"
    assert failure.attributes["previous_status"] == "running"
    assert helpers._session_status_cache.get("conv_bare") == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("next_status", ["running", "idle"])
async def test_other_edge_publishes_the_held_bare_failure_first(
    caplog: pytest.LogCaptureFixture, next_status: str
) -> None:
    """A later non-failure edge keeps today's order: the bare failure, then the edge."""
    helpers._session_status_cache["conv_bare"] = "running"
    with caplog.at_level(logging.ERROR, logger="omnigent.server.routes.sessions"):
        _hold_bare_relayed_failure("conv_bare")
        helpers._publish_status("conv_bare", next_status)
    [failure] = _turn_failures(caplog)
    assert failure.attributes["code"] == "none"
    assert failure.attributes["previous_status"] == "running"
    # ``failed`` stays sticky against a trailing idle; a running edge clears it.
    expected = "failed" if next_status == "idle" else "running"
    assert helpers._session_status_cache.get("conv_bare") == expected
    assert "conv_bare" not in helpers._held_bare_failures


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
