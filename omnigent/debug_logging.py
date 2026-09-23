"""Client-side debug-log sink that ships process logs to a Databricks table.

Every Omnigent Python entrypoint (server / runner / host / harness) writes its
logs to a local file via :mod:`omnigent.process_logging`. This module adds an
extra logging handler that also forwards each record, as JSON, to a Databricks
Delta table through the ZeroBus REST ingest endpoint, so a whole session's logs
can be queried in one place (see the Omnigent Debuggability Plan, OMNI-4198).

By default, the sink is enabled only when the ``OMNIGENT_DEBUG_LOG_*``
environment variables are present -- the internal ``omni`` config CLI sets them
for internal users, so the feature is off by default for OSS users and
customers. Integrations can instead provide their own batch-send function.
Delivery is best-effort and fully non-blocking: records are queued and flushed
by a daemon thread, and any failure drops rows rather than disrupting the
process.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import queue
import socket
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from omnigent.errors import ErrorPhase, OmnigentError, classify_exception
from omnigent.process_logging import redact_log_text
from omnigent.runner.identity import RUNNER_ID_ENV_VAR
from omnigent.version import VERSION

# ── environment contract ────────────────────────────────────────────────────
# The insert endpoint carries the table (in its path) and the workspace id (in
# its host), so only four values are needed. See config_from_env.
CLIENT_ID_ENV_VAR = "OMNIGENT_DEBUG_LOG_CLIENT_ID"
CLIENT_SECRET_ENV_VAR = "OMNIGENT_DEBUG_LOG_CLIENT_SECRET"
WORKSPACE_URL_ENV_VAR = "OMNIGENT_DEBUG_LOG_WORKSPACE_URL"
ENDPOINT_ENV_VAR = "OMNIGENT_DEBUG_LOG_ENDPOINT"

# The host spawns a runner for one *primary* session and names the runner's log
# file after it. Subagent child sessions co-locate in the same runner process,
# so this is the primary session, not the only one — pass it explicitly at
# runner-level log callsites that have no per-request session id in scope.
PRIMARY_SESSION_ID_ENV_VAR = "OMNIGENT_RUNNER_PRIMARY_SESSION_ID"

# Authenticated user id (email) attribution. The multi-tenant server sets a
# request-scoped ContextVar per request; the single-user runner/host set the
# env var once at startup (a process constant — an env var, not a ContextVar,
# because a ContextVar set at startup is invisible to run_in_executor threads).
USER_ID_ENV_VAR = "OMNIGENT_USER_ID"
_user_id_var: ContextVar[str | None] = ContextVar("omnigent_debug_user_id", default=None)

# Session attribution for HTTP handlers and scoped lifecycle work on every
# process. Explicit record fields win; runner environment IDs are fallbacks.
_session_id_var: ContextVar[str | None] = ContextVar("omnigent_debug_session_id", default=None)

_runner_id_var: ContextVar[str | None] = ContextVar("omnigent_debug_runner_id", default=None)
_request_id_var: ContextVar[str | None] = ContextVar("omnigent_debug_request_id", default=None)


# Ambient lifecycle phase for the code currently executing. Set with
# ``phase_scope`` around each region (runner launch, harness setup/startup, turn)
# so any error logged inside inherits where it failed. Propagates into awaited
# coroutines and is copied into asyncio tasks at creation, so wrap the entry of a
# background task, not the scheduler. Unset outside a scoped region.
_phase_var: ContextVar[ErrorPhase | None] = ContextVar("omnigent_error_phase", default=None)

# Request-scoped bag of extra audit attributes a handler can attach so they ride
# the request's audit envelope end-event (e.g. POST /events' event type, a newly
# created session id) rather than emitting a separate row. Reset per request by
# the middleware; mutated in place so a handler running in a child context is
# still visible to the middleware. Unset outside a request.
_audit_attrs_var: ContextVar[dict[str, str] | None] = ContextVar(
    "omnigent_audit_attrs", default=None
)

# Origin deployment identity for the workspace_id/app_name columns. The
# multi-tenant managed service stamps a per-request ``record.workspace_id`` (via
# its logging ContextFilter), so the sink prefers that; the values below are the
# process-constant fallback for single-tenant deployments -- the DATABRICKS_* env
# a Databricks App injects, else parsed from the server URL a runner/host
# connected to an App carries. All absent on OSS/local (columns stay null).
ORIGIN_WORKSPACE_ID_ENV_VAR = "DATABRICKS_WORKSPACE_ID"
APP_NAME_ENV_VAR = "DATABRICKS_APP_NAME"
SERVER_URL_ENV_VAR = "RUNNER_SERVER_URL"

# Batching / delivery defaults.
_BATCH_MAX_RECORDS = 100
_FLUSH_INTERVAL_S = 2.0
_QUEUE_MAX_RECORDS = 10_000
_TOKEN_REFRESH_SKEW_S = 300.0
_HTTP_TIMEOUT_S = 10.0
# Logger-name prefixes the sink drops as noise: httpx/httpcore emit an
# "HTTP Request: …" line per call — high-volume plumbing the debug view doesn't
# want (and the sink's own uploads go through httpx).
_IGNORED_LOGGER_PREFIXES = ("httpx", "httpcore")

_HOSTNAME = socket.gethostname()

_logger = logging.getLogger(__name__)

# Diagnostics are throttled per category so a persistently broken endpoint
# surfaces the reason without flooding the local logs on every flush.
_DIAG_THROTTLE_S = 60.0
_diag_last: dict[str, float] = {}
_diag_lock = threading.Lock()


def _diag(key: str, msg: str, *args: object) -> None:
    """Log a throttled sink diagnostic (at most once per category per minute).

    Called from the uploader thread, so these lines reach the local file log and
    stderr but are never re-ingested by the sink itself (``emit`` skips its own
    thread). The first occurrence of each ``key`` logs immediately.
    """
    now = time.time()
    with _diag_lock:
        if now - _diag_last.get(key, 0.0) < _DIAG_THROTTLE_S:
            return
        _diag_last[key] = now
    _logger.warning("debug-log sink: " + msg, *args)


def _body_snippet(response: httpx.Response, limit: int = 300) -> str:
    """Return a short single-line preview of a response body for diagnostics."""
    try:
        text = " ".join(response.text.split())
    except Exception:  # noqa: BLE001 — diagnostics must never raise
        return "<unreadable body>"
    return text[:limit]


def _is_ignored_logger(name: str) -> bool:
    """Return whether a logger's records are dropped by the sink as noise."""
    return name.startswith(_IGNORED_LOGGER_PREFIXES)


@dataclass(frozen=True)
class DebugLogConfig:
    """Resolved configuration for the debug-log sink."""

    client_id: str
    client_secret: str
    workspace_url: str  # OIDC token-mint host, e.g. https://dbc-….cloud.databricks.com
    insert_url: str  # full ZeroBus …/tables/<table>/insert URL
    table: str  # catalog.schema.table, parsed from insert_url
    workspace_id: str  # numeric id, parsed from insert_url host


def _parse_insert_url(insert_url: str) -> tuple[str, str] | None:
    """Extract ``(table, workspace_id)`` from a ZeroBus insert URL.

    The URL shape is a fixed API contract:
    ``https://<workspace-id>.zerobus.<region>…/zerobus/v1/tables/<table>/insert``
    -- the table is the path segment after ``/tables/`` and the workspace id is
    the first DNS label of the host. Returns ``None`` if the URL is malformed.
    """
    try:
        parsed = urllib.parse.urlparse(insert_url)
        host = parsed.hostname or ""
        workspace_id = host.split(".", 1)[0]
        marker = "/tables/"
        start = parsed.path.find(marker)
        if start < 0:
            return None
        table = parsed.path[start + len(marker) :].split("/", 1)[0]
        if not table or not workspace_id:
            return None
        return table, workspace_id
    except ValueError:
        return None


def config_from_env() -> DebugLogConfig | None:
    """Build the sink config from the environment, or ``None`` when disabled.

    All four variables must be set for the sink to run. When none are set it
    stays silently off (the default for OSS/customers); a *partial* or malformed
    set logs one warning naming the problem, then disables — that partial case
    almost always means someone tried to enable it and slipped.
    """
    client_id = os.environ.get(CLIENT_ID_ENV_VAR)
    client_secret = os.environ.get(CLIENT_SECRET_ENV_VAR)
    workspace_url = os.environ.get(WORKSPACE_URL_ENV_VAR)
    insert_url = os.environ.get(ENDPOINT_ENV_VAR)
    if not (client_id and client_secret and workspace_url and insert_url):
        present = {
            CLIENT_ID_ENV_VAR: client_id,
            CLIENT_SECRET_ENV_VAR: client_secret,
            WORKSPACE_URL_ENV_VAR: workspace_url,
            ENDPOINT_ENV_VAR: insert_url,
        }
        missing = [name for name, value in present.items() if not value]
        # A partial set almost always means someone tried to enable it and slipped.
        if len(missing) < len(present):
            _logger.warning("debug-log sink disabled: missing env var(s): %s", ", ".join(missing))
        return None
    parsed = _parse_insert_url(insert_url)
    if parsed is None:
        _logger.warning("debug-log sink disabled: could not parse %s", ENDPOINT_ENV_VAR)
        return None
    table, workspace_id = parsed
    return DebugLogConfig(
        client_id=client_id,
        client_secret=client_secret,
        workspace_url=workspace_url.rstrip("/"),
        insert_url=insert_url,
        table=table,
        workspace_id=workspace_id,
    )


def runner_primary_session_id() -> str | None:
    """Return the runner's primary (spawn-time) session id, or ``None``.

    The host sets ``OMNIGENT_RUNNER_PRIMARY_SESSION_ID`` when it spawns a runner
    for a session. It is the best-available attribution for runner-level log
    callsites that have no per-request session id in scope (tunnel lifecycle,
    startup, infra). Prefer an explicit per-request session id wherever one is
    available -- a subagent turn runs in the same runner process, so this
    primary id would otherwise mis-attribute it to the parent.
    """
    return os.environ.get(PRIMARY_SESSION_ID_ENV_VAR) or None


def set_current_user_id(user_id: str | None) -> None:
    """Bind the current request's authenticated user (server middleware / WS boundary)."""
    _user_id_var.set(user_id or None)


@contextlib.contextmanager
def current_user_id_scope(user_id: str | None) -> Iterator[None]:
    """Bind ``user_id`` for the duration of the block, restoring the prior value on exit."""
    token = _user_id_var.set(user_id or None)
    try:
        yield
    finally:
        _user_id_var.reset(token)


def current_user_id() -> str | None:
    """Best-available user attribution the sink stamps when a record has no explicit user_id.

    Request-scoped ContextVar first (multi-tenant server, per request), then the
    process-constant ``OMNIGENT_USER_ID`` env (single-user runner/host). Both
    empty -> ``None``. On the runner/host this is the process **owner** (session/
    host owner), which in a shared session can differ from the per-turn initiator
    the server records -- inherent to a per-process attribution column.
    """
    return _user_id_var.get() or os.environ.get(USER_ID_ENV_VAR) or None


def set_current_request_id(request_id: str | None) -> None:
    """Bind the server HTTP request id; host-frame request ids are separate."""
    _request_id_var.set(request_id or None)


def set_current_runner_id(runner_id: str | None) -> None:
    """Bind a known runner without looking up a session on every log record."""
    _runner_id_var.set(runner_id or None)


@contextlib.contextmanager
def runner_log_scope(session_id: str | None, runner_id: str | None) -> Iterator[None]:
    """Attribute a launch, callback, or relay and restore the caller's context."""
    session_token = _session_id_var.set(session_id or None)
    runner_token = _runner_id_var.set(runner_id or None)
    try:
        yield
    finally:
        _runner_id_var.reset(runner_token)
        _session_id_var.reset(session_token)


def set_current_session_id(session_id: str | None) -> None:
    """Bind a known session in the current request or lifecycle task."""
    _session_id_var.set(session_id or None)


@contextlib.contextmanager
def current_session_id_scope(session_id: str | None) -> Iterator[None]:
    """Bind ``session_id`` for the duration of the block, restoring the prior value on exit."""
    token = _session_id_var.set(session_id or None)
    try:
        yield
    finally:
        _session_id_var.reset(token)


def current_session_id() -> str | None:
    """Return the session bound to the current request or lifecycle scope."""
    return _session_id_var.get() or None


@contextlib.contextmanager
def phase_scope(phase: ErrorPhase) -> Iterator[None]:
    """Mark *phase* as active for the block, restoring the prior value on exit.

    Wrap each lifecycle region (runner launch, harness setup/startup, turn) so an
    error logged inside is located even when it carries no error code. For a
    background task, wrap the task body, not the scheduler (the context is copied
    at task creation).
    """
    token = _phase_var.set(phase)
    try:
        yield
    finally:
        _phase_var.reset(token)


def current_phase() -> ErrorPhase | None:
    """The ambient lifecycle phase, or ``None`` outside any ``phase_scope``."""
    return _phase_var.get()


def reset_request_audit_attrs() -> None:
    """Start a fresh per-request audit-attribute bag (server middleware).

    Called at the top of the request so a handler can attach attributes that
    ride the request's audit envelope ``ok``/``error`` row instead of emitting
    a separate row (see :func:`add_audit_attrs`).
    """
    _audit_attrs_var.set({})


def add_audit_attrs(**attrs: object) -> None:
    """Merge attributes onto the current request's audit envelope end-event.

    A no-op outside a request (bag unset -> e.g. on the runner). Mutates the
    bag in place so the value is visible to the middleware even though it runs
    the downstream app in a child context. Values are coerced to ``str`` and
    ``None`` dropped, matching the ``MAP<STRING,STRING>`` attributes column.
    """
    bag = _audit_attrs_var.get()
    if bag is None:
        return
    for key, value in attrs.items():
        if value is not None:
            bag[str(key)] = str(value)


def current_request_audit_attrs() -> dict[str, str]:
    """Return a copy of the current request's accumulated audit attributes."""
    return dict(_audit_attrs_var.get() or {})


def mark_request_audit_suppressed() -> None:
    """Suppress this request's audit envelope end-event (high-frequency echoes).

    For endpoints hit per streamed chunk (``POST /events`` with a transient
    ``external_*_delta`` / usage type) whose per-call row is pure noise — the
    content is already on the SSE-event logger. Recorded in the shared attribute
    bag (a reserved key the middleware reads), so it survives the middleware's
    child-context boundary like any other bag entry.
    """
    bag = _audit_attrs_var.get()
    if bag is not None:
        bag["_suppress"] = "1"


def _clean(value: object) -> str | None:
    """Coerce a missing/blank record attribute to ``None`` so a fallback engages.

    The managed service's logging filter sets ``record.workspace_id`` to ``""``
    (present-but-empty) for records emitted outside a workspace-bound request, so
    an empty value must fall through to the process-constant fallback, not win.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_databricks_app_host(url: str | None) -> tuple[str | None, str | None]:
    """Parse ``(app_name, workspace_id)`` from a Databricks Apps server URL.

    Apps URLs are ``https://<app_name>-<workspace_id>.<region>.databricksapps.com``;
    the app name may itself contain hyphens, so split on the last one and require a
    numeric workspace-id suffix. ``(None, None)`` for any non-Apps URL -- the
    managed service (``dbc-<hash>...``), localhost, or a custom domain.
    """
    if not url:
        return None, None
    host = urllib.parse.urlparse(url).hostname or ""
    if not host.endswith(".databricksapps.com"):
        return None, None
    label = host.split(".", 1)[0]
    app_name, sep, workspace_id = label.rpartition("-")
    if not sep or not app_name or not workspace_id.isdigit():
        return None, None
    return app_name, workspace_id


def _process_identity() -> tuple[str | None, str | None]:
    """Best-available ``(workspace_id, app_name)`` for single-tenant deployments.

    The fallback the sink applies when a record carries no per-request identity:
    the ``DATABRICKS_*`` env (a Databricks App injects it; a host connected to a
    managed service resolves its workspace id and publishes it there, and injects
    it into each runner it spawns), else the values parsed from the server URL a
    runner/host connected to an App carries. ``(None, None)`` on the managed
    service (which supplies identity per-record) and on OSS/local. Read fresh per
    call, not cached: the host resolves and sets the env after import.
    """
    url_app, url_ws = _parse_databricks_app_host(os.environ.get(SERVER_URL_ENV_VAR))
    workspace_id = _clean(os.environ.get(ORIGIN_WORKSPACE_ID_ENV_VAR)) or url_ws
    app_name = _clean(os.environ.get(APP_NAME_ENV_VAR)) or url_app
    return workspace_id, app_name


def debug_event(
    event_name: str,
    *,
    session_id: str | None = None,
    turn_id: str | None = None,
    user_id: str | None = None,
    **attributes: object,
) -> dict[str, object]:
    """Build a logging ``extra=`` payload naming a semantic event.

    Use at lifecycle callsites so the row carries an ``event_name`` and a
    string-valued attributes map, and pass ``session_id`` (and ``turn_id`` once
    it is wired) explicitly so the row is correlated to its session, e.g.::

        _logger.info("dispatching tool", extra=debug_event(
            "tool_call_dispatched", session_id=session_id,
            tool_call_id=tc.id, model=model))

    Explicit fields win over ambient lifecycle context. The sink enriches
    ordinary logs too: session/request/runner scopes on the server and host,
    primary-session and runner environment defaults on runner/harness rows.
    ``turn_id`` remains callsite-driven. ``user_id`` uses its existing request
    scope or process-owner environment fallback.
    """
    extra: dict[str, object] = {"event_name": event_name, "attributes": dict(attributes)}
    if session_id is not None:
        extra["session_id"] = session_id
    if turn_id is not None:
        extra["turn_id"] = turn_id
    if user_id is not None:
        extra["user_id"] = user_id
    return extra


def log_debug_event(
    logger: logging.Logger,
    event_name: str,
    *,
    session_id: str | None = None,
    **attributes: object,
) -> None:
    """Emit an informational observation without letting logging interrupt work."""
    with contextlib.suppress(Exception):
        extra = debug_event(event_name, session_id=session_id)
        extra["attributes"] = attributes
        logger.info(event_name, extra=extra)


def _stack_trace(record: logging.LogRecord) -> str | None:
    if record.exc_info:
        return "".join(traceback.format_exception(*record.exc_info))
    return record.exc_text or None


def _attributes(record: logging.LogRecord, source: str) -> dict[str, str]:
    raw = getattr(record, "attributes", None)
    attrs: dict[str, str] = {}
    if isinstance(raw, dict):
        # The target column is MAP<STRING,STRING>; coerce values, redact them,
        # and drop nulls. Event attributes share the same privacy boundary as
        # messages.
        attrs = {str(k): redact_log_text(str(v)) for k, v in raw.items() if v is not None}
    for key, value in (
        ("request_id", getattr(record, "request_id", None) or _request_id_var.get()),
        (
            "runner_id",
            getattr(record, "runner_id", None)
            or _runner_id_var.get()
            or (os.environ.get(RUNNER_ID_ENV_VAR) if source in {"runner", "harness"} else None),
        ),
    ):
        if value:
            attrs.setdefault(key, redact_log_text(str(value)))
    _stamp_error_dimensions(attrs, record)
    return attrs


def _stamp_error_dimensions(attrs: dict[str, str], record: logging.LogRecord) -> None:
    """Auto-attribute a logged exception the callsite did not classify itself.

    Any record carrying ``exc_info`` gains ``error_category`` / ``error_impact``
    derived from the exception (see :func:`omnigent.errors.classify_exception`),
    so every ``_logger.exception`` / ``exc_info=…`` site across the codebase is
    covered without per-site edits. Exception and explicit cause types make
    generic wrapper errors groupable without their messages. Explicit callsite
    values always win.
    """
    exc = record.exc_info[1] if isinstance(record.exc_info, tuple) else None
    if isinstance(exc, BaseException):
        attrs.setdefault("exception_type", type(exc).__name__)
        if exc.__cause__ is not None:
            attrs.setdefault("exception_cause_type", type(exc.__cause__).__name__)
    if isinstance(exc, BaseException) and not (
        "error_category" in attrs and "error_impact" in attrs
    ):
        category, impact = classify_exception(exc)
        attrs.setdefault("error_category", category.value)
        attrs.setdefault("error_impact", impact.value)
    # Locate only rows that are actually errors: an exception to place, or a row
    # already declaring itself an error via category/impact. Otherwise a benign
    # INFO/DEBUG line emitted inside a phase_scope (the whole turn loop is one)
    # would inherit a spurious error_phase from the ambient scope.
    is_error_row = exc is not None or "error_category" in attrs or "error_impact" in attrs
    if is_error_row and "error_phase" not in attrs:
        phase = _resolve_error_phase(exc)
        if phase is not None:
            attrs["error_phase"] = phase.value


def _resolve_error_phase(exc: BaseException | None) -> ErrorPhase | None:
    """Locate a logged error's lifecycle phase.

    Precedence: a coded ``OmnigentError``'s own (concrete) phase, then the ambient
    ``phase_scope`` for the region that was executing (covers uncoded exceptions
    and non-exception error logs), then a coded error's UNKNOWN fallback. Returns
    ``None`` when there is nothing to attribute (no code, no active scope), so the
    column stays empty rather than guessing.
    """
    coded = exc.phase if isinstance(exc, OmnigentError) else None
    if coded is not None and coded is not ErrorPhase.UNKNOWN:
        return coded
    ambient = current_phase()
    if ambient is not None:
        return ambient
    return coded


def record_to_row(record: logging.LogRecord, source: str) -> dict[str, object]:
    """Serialize a log record into one debug-logs table row.

    ``client_time`` is epoch microseconds and ``attributes`` a plain object --
    the two shapes the ZeroBus JSON path requires for the ``TIMESTAMP`` and
    ``MAP<STRING,STRING>`` columns respectively.

    Session attribution prefers an explicit record field, then the active
    request/lifecycle scope, then the primary-session environment on runner
    and harness rows only. Runner child-session requests bind their own ID;
    process-wide runner logs can still fall back to the primary session.
    Request and runner IDs follow the same explicit-before-ambient rule in
    ``attributes``. Server and host rows never use runner environment defaults.

    ``workspace_id``/``app_name`` describe the record's origin deployment: the
    managed service stamps ``record.workspace_id`` per request (so it wins),
    while single-tenant deployments fall back to the process-constant
    :func:`_process_identity`. ``app_name`` has no per-request source, so it is
    null on the managed service.
    """
    workspace_id, app_name = _process_identity()
    stack_trace = _stack_trace(record)
    return {
        "session_id": (
            getattr(record, "session_id", None)
            or current_session_id()
            or (runner_primary_session_id() if source in {"runner", "harness"} else None)
        ),
        "turn_id": getattr(record, "turn_id", None),
        "source": source,
        "event_name": getattr(record, "event_name", None),
        "level": record.levelname,
        "message": redact_log_text(record.getMessage()),
        "client_time": int(record.created * 1_000_000),
        "hostname": _HOSTNAME,
        "logger_name": record.name,
        "func_name": record.funcName,
        "app_version": VERSION,
        "stack_trace": redact_log_text(stack_trace) if stack_trace is not None else None,
        "attributes": _attributes(record, source),
        "log_id": uuid.uuid4().hex,
        "user_id": getattr(record, "user_id", None) or current_user_id(),
        "workspace_id": _clean(getattr(record, "workspace_id", None)) or workspace_id,
        "app_name": _clean(getattr(record, "app_name", None)) or app_name,
    }


class _TokenSource:
    """Mints and caches a ZeroBus-audience OAuth token from the SP creds.

    The token must be minted for the ``zerobusDirectWriteApi`` resource via the
    ``client_credentials`` grant -- a plain workspace token is rejected -- so we
    do the OIDC exchange by hand rather than through the SDK.
    """

    def __init__(self, config: DebugLogConfig, client: httpx.Client) -> None:
        self._config = config
        self._client = client
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str | None:
        with self._lock:
            if self._token and time.time() < self._expires_at - _TOKEN_REFRESH_SKEW_S:
                return self._token
            minted = self._mint()
            if minted is None:
                return None
            self._token, self._expires_at = minted
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def _authorization_details(self) -> str:
        parts = self._config.table.split(".")
        catalog = parts[0]
        schema = ".".join(parts[:2])
        return json.dumps(
            [
                {
                    "type": "unity_catalog_privileges",
                    "privileges": ["USE CATALOG"],
                    "object_type": "CATALOG",
                    "object_full_path": catalog,
                },
                {
                    "type": "unity_catalog_privileges",
                    "privileges": ["USE SCHEMA"],
                    "object_type": "SCHEMA",
                    "object_full_path": schema,
                },
                {
                    "type": "unity_catalog_privileges",
                    "privileges": ["SELECT", "MODIFY"],
                    "object_type": "TABLE",
                    "object_full_path": self._config.table,
                },
            ]
        )

    def _mint(self) -> tuple[str, float] | None:
        resource = f"api://databricks/workspaces/{self._config.workspace_id}/zerobusDirectWriteApi"
        try:
            response = self._client.post(
                f"{self._config.workspace_url}/oidc/v1/token",
                auth=(self._config.client_id, self._config.client_secret),
                data={
                    "grant_type": "client_credentials",
                    "scope": "all-apis",
                    "resource": resource,
                    "authorization_details": self._authorization_details(),
                },
                timeout=_HTTP_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            _diag(
                "token_transport",
                "token mint request to %s failed: %s",
                self._config.workspace_url,
                exc,
            )
            return None
        if response.status_code != 200:
            # The body carries the OAuth error (invalid_client, unauthorized
            # authorization_details, …) — the actionable part.
            _diag(
                "token_status",
                "token mint failed: status=%s body=%s",
                response.status_code,
                _body_snippet(response),
            )
            return None
        try:
            body = response.json()
        except ValueError:
            _diag("token_decode", "token mint returned a non-JSON body")
            return None
        token = body.get("access_token")
        if not token:
            _diag("token_missing", "token mint response had no access_token")
            return None
        expires_in = float(body.get("expires_in", 3600))
        return token, time.time() + expires_in


DebugLogRow = dict[str, object]
DebugLogSend = Callable[[list[DebugLogRow]], None]


class DebugLogHandler(logging.Handler):
    """Non-blocking handler that queues rows and sends them in batches.

    ``send`` runs on the handler's daemon thread and receives a prepared batch
    of debug-log row objects. It owns only delivery; this handler retains record
    serialization, queue overflow, batching, flush timing, and shutdown drains.
    """

    def __init__(self, source: str, send: DebugLogSend) -> None:
        super().__init__()
        self._source = source
        self._send = send
        self._closed = False
        self._start_worker()
        atexit.register(self.close)

    def _start_worker(self) -> None:
        """Create the queue and launch the sender thread.

        Re-invoked by :meth:`emit` when the thread has stopped — after a
        ``logging.config.dictConfig()`` (uvicorn) or an ``os.fork()`` (the
        runner ``_zygote``), which leave the handler attached but kill the
        thread. A fresh queue/thread lets delivery resume; the inherited ones
        (whose locks are in an indeterminate post-fork state) are dropped.
        """
        self._queue: queue.Queue[DebugLogRow] = queue.Queue(maxsize=_QUEUE_MAX_RECORDS)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="omnigent-debug-log", daemon=True)
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed

    def emit(self, record: logging.LogRecord) -> None:
        # Skip records the uploader itself emits (e.g. httpx), or a failing POST
        # would feed its own error logs back into the queue.
        if threading.current_thread() is self._thread:
            return
        # Drop chatty HTTP-client internals (httpx/httpcore) as noise.
        if _is_ignored_logger(record.name):
            return
        # logging.config.dictConfig() (uvicorn applies one at startup) calls
        # logging.shutdown() on every handler — which close()s this one and stops
        # its uploader thread while leaving it attached to root; os.fork() (the
        # runner _zygote) likewise kills the thread. Receiving a record means the
        # handler is still live, so revive it with fresh worker state. A real
        # shutdown emits nothing afterward, so this never fights atexit. emit()
        # is serialized by the Handler lock, so the restart happens once.
        if self._closed or not self._thread.is_alive():
            try:
                self._closed = False
                self._start_worker()
            except Exception:  # noqa: BLE001 — never break logging over the sink
                return
        try:
            row = record_to_row(record, self._source)
        except Exception:  # noqa: BLE001 — a logging handler must never raise into the app
            return
        try:
            self._queue.put_nowait(row)
        except queue.Full:
            # Shed the oldest row to keep the newest under sustained overflow.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(row)
            except queue.Empty:
                # The uploader drained the queue between our full put and this
                # get — there is room now and this one row is dropped, which is
                # acceptable for a best-effort sink shedding under overflow.
                pass

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    batch = self._collect_batch(self._FLUSH_WAIT)
                    if batch:
                        self._send(batch)
                except Exception:  # noqa: BLE001 — the uploader thread must never die
                    # A sender failure must not kill the worker: emit()'s
                    # self-heal only revives a *stopped* thread, so a crash would
                    # silently end delivery for the process. Drop and continue.
                    time.sleep(0.1)
            # Best-effort drain of whatever is left on shutdown.
            remaining = self._collect_batch(0.0)
            if remaining:
                self._send(remaining)
        except Exception:  # noqa: BLE001 — shutdown drain is best-effort
            pass

    _FLUSH_WAIT = _FLUSH_INTERVAL_S

    def _collect_batch(self, wait: float) -> list[DebugLogRow]:
        batch: list[DebugLogRow] = []
        try:
            batch.append(self._queue.get(timeout=wait) if wait else self._queue.get_nowait())
        except queue.Empty:
            return batch
        while len(batch) < _BATCH_MAX_RECORDS:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch

    def close(self) -> None:
        if self._closed:
            return
        # Capture the worker being stopped: a concurrent emit() can revive the
        # handler during the join and replace self._stop/self._thread.
        stop, thread = self._stop, self._thread
        self._closed = True
        stop.set()
        thread.join(timeout=5.0)
        super().close()


class ZerobusLogHandler(DebugLogHandler):
    """Default batched debug-log handler that delivers through ZeroBus."""

    def __init__(self, config: DebugLogConfig, source: str) -> None:
        self._config = config
        self._delivered_any = False
        super().__init__(source, self._post)

    def _start_worker(self) -> None:
        # Recreate the transport on every worker start. After a fork, inherited
        # httpx/token locks may be indeterminate and must not be reused.
        self._client = httpx.Client(timeout=_HTTP_TIMEOUT_S)
        self._tokens = _TokenSource(self._config, self._client)
        super()._start_worker()

    def _run(self) -> None:
        # The worker owns the client captured at start, so close() never shuts
        # it down while a post or token mint is in flight.
        client = self._client
        try:
            super()._run()
        finally:
            with contextlib.suppress(Exception):
                client.close()

    def _post(self, batch: list[DebugLogRow]) -> None:
        payload = json.dumps(batch)
        for attempt in range(3):
            token = self._tokens.token()
            if not token:
                # Mint failed — _mint already logged why; note the data loss.
                _diag("no_token", "no auth token (mint failing); dropping %d row(s)", len(batch))
                return
            try:
                response = self._client.post(
                    self._config.insert_url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    content=payload,
                    timeout=_HTTP_TIMEOUT_S,
                )
            except httpx.HTTPError as exc:
                _diag(
                    "post_transport", "insert POST to %s failed: %s", self._config.insert_url, exc
                )
                time.sleep(min(0.5 * 2**attempt, 3.0))
                continue
            if response.status_code == 200:
                if not self._delivered_any:
                    self._delivered_any = True
                    _logger.info(
                        "debug-log sink: first batch delivered to %s (%d row(s))",
                        self._config.table,
                        len(batch),
                    )
                return
            if response.status_code in (401, 403):
                # Wrong/expired token audience shows up here (not at mint).
                _diag(
                    "post_auth",
                    "insert rejected: status=%s body=%s",
                    response.status_code,
                    _body_snippet(response),
                )
                self._tokens.invalidate()  # stale/rotated token — refresh and retry
                continue
            _diag(
                "post_status",
                "insert failed: status=%s body=%s",
                response.status_code,
                _body_snippet(response),
            )
            time.sleep(min(0.5 * 2**attempt, 3.0))
        _diag("post_dropped", "dropped %d row(s) after 3 failed insert attempts", len(batch))


@dataclass(frozen=True, slots=True)
class _SseFileItem:
    """One queued SSE row: the safe fields pulled off the record in emit()."""

    session_id: str
    created: float
    level: str
    event: str | None
    attrs: dict[str, object]


class SseFileHandler(logging.Handler):
    """Append SSE-event records to per-session JSONL files (opt-in; non-blocking).

    :meth:`emit` never touches the filesystem: it pulls the safe fields off the
    record and enqueues them, and a daemon writer thread batches, groups by
    session, and appends to ``<log_dir>/<session_id>-sse.jsonl``. A single server
    fans out hundreds of concurrent sessions and SSE events (text deltas,
    terminal activity) are very frequent, so a synchronous write per event would
    block the workflow thread — this keeps all file I/O off the publish path.

    The writer groups a drained batch by session, so each pass issues one
    ``os.write`` per session touched (``O_APPEND`` keeps whole-line writes atomic
    even if a file is shared across a forked child). Open descriptors are bounded
    by an LRU cache so a many-session server never exhausts fds — an evicted
    session reopens (and keeps appending) on its next event. The queue is bounded
    and sheds the oldest record under sustained overload, so publish is never
    backpressured — best-effort debug data. Content is never written: only the
    event name and whitelisted ids/dimensions, the same safe subset the ZeroBus
    table gets.

    Retention is the operator's responsibility: files are appended without
    rotation and old per-session files are never reaped, so ``<log_dir>`` grows
    without bound until cleaned up externally. This is a debugging aid, not a
    managed log stream.
    """

    # Concurrently open per-session fds; the least-recently-used is closed when
    # exceeded (reopening in O_APPEND resumes the same file).
    _MAX_OPEN_FILES = 128
    # Queue bound before shedding the oldest record, records drained per write
    # pass, and the writer's idle wakeup interval.
    _QUEUE_MAX_RECORDS = 20_000
    _BATCH_MAX_RECORDS = 1_000
    _FLUSH_INTERVAL_S = 1.0

    def __init__(self, log_dir: Path, source: str) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._source = source
        self._closed = False
        self._start_worker()
        atexit.register(self.close)

    def _start_worker(self) -> None:
        """Create a fresh queue / fd-cache / thread and launch the writer.

        Re-invoked by :meth:`emit` when the thread has stopped — after a
        ``logging.config.dictConfig()`` (uvicorn) or ``os.fork()`` (the runner
        ``_zygote``), which leave the handler attached but kill the thread. The
        fresh fd cache drops any inherited descriptors (whose files belong to the
        parent) rather than writing to them.

        The queue / fd-cache / stop-event are also passed to the worker as
        arguments, so it operates solely on the state it was started with: a
        concurrent revive that reassigns these attributes cannot make a still-
        running old worker and the new one share one (non-thread-safe) fd cache.
        """
        self._queue: queue.Queue[_SseFileItem | threading.Event] = queue.Queue(
            maxsize=self._QUEUE_MAX_RECORDS
        )
        # session_id -> fd, LRU-ordered; owned by the writer thread (also bound
        # here for introspection/tests).
        self._fds: OrderedDict[str, int] = OrderedDict()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(self._queue, self._fds, self._stop),
            name="omnigent-sse-file-log",
            daemon=True,
        )
        self._thread.start()

    @staticmethod
    def _safe_session_id(session_id: str) -> str:
        """Sanitize a session id into a filesystem-safe basename."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in session_id) or "unknown"

    def _extract(self, record: logging.LogRecord) -> _SseFileItem | None:
        """Pull the safe fields off a record on the caller thread (cheap)."""
        session_id = getattr(record, "session_id", None)
        if not session_id:
            return None  # SSE records always carry one; nothing to key a file on.
        attrs = getattr(record, "attributes", None)
        return _SseFileItem(
            session_id=str(session_id),
            created=record.created,
            level=record.levelname,
            event=getattr(record, "event_name", None),
            attrs=attrs if isinstance(attrs, dict) else {},
        )

    def emit(self, record: logging.LogRecord) -> None:
        # Revive a worker stopped by dictConfig()/fork (see _start_worker).
        if self._closed or not self._thread.is_alive():
            try:
                self._closed = False
                self._start_worker()
            except Exception:  # noqa: BLE001 — never break logging over the sink
                return
        try:
            item = self._extract(record)
        except Exception:  # noqa: BLE001 — a logging handler must never raise into the app
            return
        if item is None:
            return
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            # Shed the oldest to keep the newest; best-effort, never block publish.
            try:
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except queue.Empty:
                # The writer drained the queue between our full put and this get,
                # so there is room now and this one record is dropped — acceptable
                # for a best-effort sink shedding under overload.
                pass

    def _run(
        self,
        work: queue.Queue[_SseFileItem | threading.Event],
        fds: OrderedDict[str, int],
        stop: threading.Event,
    ) -> None:
        # Operate only on the state this worker was started with (see
        # _start_worker): never read self._queue/_fds/_stop, so a concurrent
        # revive cannot repoint us at another worker's cache mid-run.
        try:
            while not stop.is_set():
                batch = self._collect_batch(work, self._FLUSH_INTERVAL_S)
                if batch:
                    self._write_batch(fds, batch)
            # Best-effort drain of whatever is left on shutdown.
            remaining = self._collect_batch(work, 0.0)
            if remaining:
                self._write_batch(fds, remaining)
        finally:
            for fd in fds.values():
                with contextlib.suppress(OSError):
                    os.close(fd)
            fds.clear()

    def _collect_batch(
        self, work: queue.Queue[_SseFileItem | threading.Event], wait: float
    ) -> list[_SseFileItem | threading.Event]:
        batch: list[_SseFileItem | threading.Event] = []
        try:
            batch.append(work.get(timeout=wait) if wait else work.get_nowait())
        except queue.Empty:
            return batch
        while len(batch) < self._BATCH_MAX_RECORDS:
            try:
                batch.append(work.get_nowait())
            except queue.Empty:
                break
        return batch

    def _fd_for(self, fds: OrderedDict[str, int], session_id: str) -> int | None:
        """Return an open append fd for *session_id* (writer thread only).

        Touches the LRU on reuse; on a miss, opens the session's file and evicts
        the least-recently-used fd when over the cap.
        """
        fd = fds.get(session_id)
        if fd is not None:
            fds.move_to_end(session_id)
            return fd
        path = self._log_dir / f"{self._safe_session_id(session_id)}-sse.jsonl"
        try:
            self._log_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        except OSError as exc:
            # Throttled: an unwritable log dir fails identically for every session
            # on every batch, which would otherwise flood the process logs.
            _diag("sse_file_open", "SSE file sink cannot open %s: %s", path, exc)
            return None
        fds[session_id] = fd
        if len(fds) > self._MAX_OPEN_FILES:
            _evicted_id, evicted_fd = fds.popitem(last=False)
            with contextlib.suppress(OSError):
                os.close(evicted_fd)
        return fd

    def _write_batch(
        self, fds: OrderedDict[str, int], batch: list[_SseFileItem | threading.Event]
    ) -> None:
        """Group a drained batch by session and issue one write per session.

        A ``threading.Event`` in the batch is a :meth:`flush` barrier: pending
        buffers are written before it is set, so its waiter is guaranteed the
        records queued before it are on disk.
        """
        buffers: dict[str, bytearray] = {}

        def flush_buffers() -> None:
            for session_id, buf in buffers.items():
                fd = self._fd_for(fds, session_id)
                if fd is None:
                    continue
                with contextlib.suppress(OSError):
                    os.write(fd, bytes(buf))
            buffers.clear()

        for item in batch:
            if isinstance(item, threading.Event):
                flush_buffers()
                item.set()
                continue
            buffers.setdefault(item.session_id, bytearray()).extend(self._format_line(item))
        flush_buffers()

    def _format_line(self, item: _SseFileItem) -> bytes:
        row = {
            "ts": datetime.fromtimestamp(item.created, tz=timezone.utc).isoformat(),
            "source": self._source,
            "level": item.level,
            "conversation_id": item.session_id,
            "event": item.event,
            "attrs": item.attrs,
        }
        return (json.dumps(row, default=str) + "\n").encode("utf-8")

    def flush(self, timeout: float = 5.0) -> None:
        """Block until records queued so far are written (for shutdown / tests)."""
        if self._closed or not self._thread.is_alive():
            return
        barrier = threading.Event()
        try:
            self._queue.put_nowait(barrier)
        except queue.Full:
            return  # best-effort; cannot guarantee a flush under overload
        barrier.wait(timeout)

    def close(self) -> None:
        if self._closed:
            return
        # Capture the worker we are tearing down: a concurrent emit() can revive
        # the handler during the join below, swapping in a fresh stop/thread. The
        # captured worker closes its own fds in _run's finally.
        stop, thread = self._stop, self._thread
        self._closed = True
        stop.set()
        thread.join(timeout=5.0)
        super().close()


# Process-wide sink; recreated only if a prior instance was closed (e.g. a
# logging reconfigure closed the root handlers out from under us).
_active_sink: DebugLogHandler | None = None
_sink_lock = threading.Lock()

# Dedicated logger for the server's outgoing SSE-event stream. It carries
# ``propagate=False`` (wired in attach_debug_log_sink / attach_sse_file_sink) so
# its high-volume records — one per emitted event, names + safe ids, never
# content — reach only the opt-in sinks below and never the on-disk/stderr logs.
# Two independent sinks may attach: the ZeroBus table (attach_debug_log_sink)
# and a local JSONL file (attach_sse_file_sink); either, both, or neither.
SSE_LOGGER_NAME = "omnigent.sse_events"

# Dedicated logger for server request audit events (the per-method
# start/ok/error envelope, WS lifecycle, and in-handler checkpoints). Like the
# SSE logger it gets the sink as its sole handler with ``propagate=False``, so
# audit rows populate the table without flooding the on-disk/stderr logs, and
# they disappear entirely when the sink is off (no env vars -> no handler).
AUDIT_LOGGER_NAME = "omnigent.audit_events"

# Opt-in local file sink for the SSE-event stream, independent of the ZeroBus
# table. Set OMNIGENT_SSE_LOG_TO_FILE truthy (1/true/yes/on) to also append each
# emitted event (safe subset — the same ids/dimensions the table gets, never
# content) as JSONL, split per session into
# ``<data-dir>/logs/<source>/<session_id>-sse.jsonl`` (e.g.
# ``~/.omnigent/logs/server/<session_id>-sse.jsonl``); useful for offline
# debugging when the table is unavailable. Files are not rotated or reaped —
# operators must clean up the directory themselves. See attach_sse_file_sink.
SSE_LOG_TO_FILE_ENV_VAR = "OMNIGENT_SSE_LOG_TO_FILE"
_sse_file_handler: SseFileHandler | None = None


def debug_sink_enabled() -> bool:
    """Whether the debug-log sink is active in this process.

    A cheap gate for opt-in, table-only logging (e.g. the SSE-event stream):
    callers skip building records entirely when the sink is off, so the feature
    adds no cost for OSS / non-internal users who never enabled it.
    """
    # Intentionally lock-free: a single global-object read is atomic under the
    # GIL, and this is a best-effort gate — a stale read only mis-times one
    # record around enable/close, never corrupts state.
    return _active_sink is not None and not _active_sink.closed


def sse_event_logger() -> logging.Logger:
    """Return the table-only logger for SSE events (see :data:`SSE_LOGGER_NAME`).

    Records go only to the debug sink (attached with ``propagate=False`` in
    :func:`attach_debug_log_sink`); when the sink is disabled the logger has no
    handlers and records are dropped -- so gate on :func:`debug_sink_enabled`.
    """
    return logging.getLogger(SSE_LOGGER_NAME)


def audit_event_logger() -> logging.Logger:
    """Return the table-only logger for server request audit events.

    Records go only to the debug sink (attached with ``propagate=False`` in
    :func:`attach_debug_log_sink`); when the sink is disabled the logger has no
    handlers and records are dropped -- so gate on :func:`debug_sink_enabled`.
    """
    return logging.getLogger(AUDIT_LOGGER_NAME)


def attach_debug_log_sink(
    loggers: list[logging.Logger],
    *,
    source: str,
    level: int,
    send: DebugLogSend | None = None,
) -> None:
    """Attach the shared debug-log sink to *loggers* when configured.

    ``send`` is an optional integration hook receiving each prepared batch on a
    daemon thread. When omitted, the sink uses ZeroBus and is a no-op unless the
    ``OMNIGENT_DEBUG_LOG_*`` variables are set. Reuses one handler per process;
    ``Logger.addHandler`` is idempotent for a given instance, so repeated calls
    do not double-ship.
    """
    global _active_sink
    config: DebugLogConfig | None = None
    if send is None:
        config = config_from_env()
        if config is None:
            return
    with _sink_lock:
        if _active_sink is None or _active_sink.closed:
            try:
                if send is not None:
                    _active_sink = DebugLogHandler(source, send)
                elif config is not None:
                    _active_sink = ZerobusLogHandler(config, source)
                else:
                    return
            except Exception:  # noqa: BLE001 — the sink must never break logging setup
                # Handler construction must not take down process logging setup.
                _logger.warning("debug-log sink disabled: handler init failed", exc_info=True)
                return
            if config is None:
                _logger.info("debug-log sink enabled: source=%s custom sender", source)
            else:
                _logger.info(
                    "debug-log sink enabled: source=%s table=%s endpoint=%s",
                    source,
                    config.table,
                    config.insert_url,
                )
        _active_sink.setLevel(level)
        for target in loggers:
            target.addHandler(_active_sink)
        # SSE-event logger: attach the table sink here and keep the logger from
        # propagating to root, so per-token delta events populate the table
        # without flooding the on-disk/stderr logs. The file sink (if enabled)
        # attaches independently in attach_sse_file_sink.
        sse_logger = logging.getLogger(SSE_LOGGER_NAME)
        sse_logger.setLevel(level)
        sse_logger.propagate = False
        sse_logger.addHandler(_active_sink)
        # Table-only server audit-event logger (same rationale as the SSE
        # logger): sink-only, non-propagating, so request audit rows reach the
        # table but never the on-disk/stderr logs.
        audit_logger = logging.getLogger(AUDIT_LOGGER_NAME)
        audit_logger.setLevel(level)
        audit_logger.propagate = False
        audit_logger.addHandler(_active_sink)


def sse_file_sink_enabled() -> bool:
    """Whether the opt-in SSE-event file sink is active in this process.

    Lock-free by design (a single global-object read is atomic under the GIL);
    a stale read only mis-times one record around enable, never corrupts state.
    """
    return _sse_file_handler is not None


def sse_logging_enabled() -> bool:
    """Whether any SSE-event sink (ZeroBus table or local file) is active.

    The gate for :func:`omnigent.runtime.session_stream._log_sse_event`: it
    builds a record only when at least one sink will consume it, so the feature
    stays free for anyone who enabled neither.
    """
    return debug_sink_enabled() or sse_file_sink_enabled()


def attach_sse_file_sink(*, source: str, level: int) -> None:
    """Attach the opt-in SSE-event file sink when ``OMNIGENT_SSE_LOG_TO_FILE`` is truthy.

    A no-op unless the boolean env var is set (1/true/yes/on). Writes one JSONL
    file per session under ``<data-dir>/logs/<source>/`` (e.g.
    ``~/.omnigent/logs/server/<session_id>-sse.jsonl``). Independent of the
    ZeroBus table sink: it configures the SSE logger's level and
    ``propagate=False`` itself, so enabling only the file sink still keeps the
    high-volume SSE records off the on-disk/stderr process logs. Idempotent per
    process.
    """
    global _sse_file_handler
    # Local imports to avoid an import cycle: process_logging imports this module.
    from omnigent.process_logging import env_truthy, process_log_dir

    if not env_truthy(os.environ.get(SSE_LOG_TO_FILE_ENV_VAR)):
        return
    with _sink_lock:
        if _sse_file_handler is not None:
            return
        log_dir = process_log_dir(source)
        try:
            handler = SseFileHandler(log_dir, source)
        except Exception:  # noqa: BLE001 — the sink must never break logging setup
            _logger.warning("SSE file sink disabled: handler init failed", exc_info=True)
            return
        handler.setLevel(level)
        _sse_file_handler = handler
        sse_logger = logging.getLogger(SSE_LOGGER_NAME)
        sse_logger.setLevel(level)
        sse_logger.propagate = False
        sse_logger.addHandler(handler)
        _logger.info("SSE file sink enabled: source=%s dir=%s", source, log_dir)
