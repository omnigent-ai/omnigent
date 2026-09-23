"""Coverage for failure attribution on the server's ``failed`` status edge.

Every server-originated failed turn logs exactly one ERROR from
``_publish_status``, so that single line is the whole population a
reliability dashboard sees. These tests hold it to naming which publish
path produced the edge, and hold the publish sites to passing one.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

import omnigent.server.routes._sessions.helpers as helpers
from omnigent.debug_logging import _attributes
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
