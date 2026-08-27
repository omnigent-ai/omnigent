"""Client-side debug-log sink that ships process logs to a Databricks table.

Every Omnigent Python entrypoint (server / runner / host / harness) writes its
logs to a local file via :mod:`omnigent.process_logging`. This module adds an
extra logging handler that also forwards each record, as JSON, to a Databricks
Delta table through the ZeroBus REST ingest endpoint, so a whole session's logs
can be queried in one place (see the Omnigent Debuggability Plan, OMNI-4198).

The sink is enabled only when the ``OMNIGENT_DEBUG_LOG_*`` environment variables
are present -- the internal ``omni`` config CLI sets them for internal users, so
the feature is off by default for OSS users and customers. Uploading is
best-effort and fully non-blocking: records are queued and flushed by a daemon
thread, and any failure (auth, network, overflow) drops rows rather than
disrupting the process.
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
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass

import httpx

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

    ``session_id``/``turn_id`` are populated only from what the callsite passes;
    ``user_id`` additionally has an ambient fallback the sink applies when the
    record carries none (a request-scoped ContextVar on the server, the
    ``OMNIGENT_USER_ID`` env on the runner/host). Freeform ``_logger.debug("…")``
    calls need no ``extra``; they ship with null correlation columns and an empty
    attributes map.
    """
    extra: dict[str, object] = {"event_name": event_name, "attributes": dict(attributes)}
    if session_id is not None:
        extra["session_id"] = session_id
    if turn_id is not None:
        extra["turn_id"] = turn_id
    if user_id is not None:
        extra["user_id"] = user_id
    return extra


def _stack_trace(record: logging.LogRecord) -> str | None:
    if record.exc_info:
        return "".join(traceback.format_exception(*record.exc_info))
    return record.exc_text or None


def _attributes(record: logging.LogRecord) -> dict[str, str]:
    raw = getattr(record, "attributes", None)
    if not isinstance(raw, dict):
        return {}
    # The target column is MAP<STRING,STRING>; coerce values and drop nulls.
    return {str(k): str(v) for k, v in raw.items() if v is not None}


def record_to_row(record: logging.LogRecord, source: str) -> dict[str, object]:
    """Serialize a log record into one debug-logs table row.

    ``client_time`` is epoch microseconds and ``attributes`` a plain object --
    the two shapes the ZeroBus JSON path requires for the ``TIMESTAMP`` and
    ``MAP<STRING,STRING>`` columns respectively.

    ``workspace_id``/``app_name`` describe the record's origin deployment: the
    managed service stamps ``record.workspace_id`` per request (so it wins),
    while single-tenant deployments fall back to the process-constant
    :func:`_process_identity`. ``app_name`` has no per-request source, so it is
    null on the managed service.
    """
    workspace_id, app_name = _process_identity()
    return {
        "session_id": getattr(record, "session_id", None),
        "turn_id": getattr(record, "turn_id", None),
        "source": source,
        "event_name": getattr(record, "event_name", None),
        "level": record.levelname,
        "message": record.getMessage(),
        "client_time": int(record.created * 1_000_000),
        "hostname": _HOSTNAME,
        "logger_name": record.name,
        "func_name": record.funcName,
        "app_version": VERSION,
        "stack_trace": _stack_trace(record),
        "attributes": _attributes(record),
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


class ZerobusLogHandler(logging.Handler):
    """Non-blocking logging handler that batches records to a ZeroBus table."""

    def __init__(self, config: DebugLogConfig, source: str) -> None:
        super().__init__()
        self._config = config
        self._source = source
        self._closed = False
        self._delivered_any = False
        self._start_worker()
        atexit.register(self.close)

    def _start_worker(self) -> None:
        """Create the queue/client and launch the uploader thread.

        Re-invoked by :meth:`emit` when the thread has stopped — after a
        ``logging.config.dictConfig()`` (uvicorn) or an ``os.fork()`` (the
        runner ``_zygote``), which leave the handler attached but kill the
        thread. Fresh queue/client/thread let delivery resume; the inherited
        ones (whose locks are in an indeterminate post-fork state) are dropped.
        """
        self._queue: queue.Queue[dict[str, object]] = queue.Queue(maxsize=_QUEUE_MAX_RECORDS)
        self._client = httpx.Client(timeout=_HTTP_TIMEOUT_S)
        self._tokens = _TokenSource(self._config, self._client)
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
        # handler is still live, so revive the worker (fresh queue/client/thread)
        # and delivery resumes on the next line. A real shutdown emits nothing
        # afterward, so this never fights atexit. emit() is serialized by the
        # Handler lock, so the restart happens once.
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
        # This worker owns the client captured at start and closes it on exit,
        # so close() never shuts the client down from another thread while a
        # post/token-mint is in flight (which raises inside httpx and would
        # otherwise kill this thread with an uncaught exception).
        client = self._client
        try:
            while not self._stop.is_set():
                try:
                    batch = self._collect_batch(self._FLUSH_WAIT)
                    if batch:
                        self._post(batch)
                except Exception:  # noqa: BLE001 — the uploader thread must never die
                    # A transient error (or a closed client during a shutdown
                    # race) must not kill the worker: emit()'s self-heal only
                    # revives a *stopped* thread, so a crash would silently end
                    # delivery for the process. Drop and keep going.
                    time.sleep(0.1)
            # Best-effort drain of whatever is left on shutdown.
            remaining = self._collect_batch(0.0)
            if remaining:
                self._post(remaining)
        except Exception:  # noqa: BLE001 — shutdown drain is best-effort
            pass
        finally:
            with contextlib.suppress(Exception):
                client.close()

    _FLUSH_WAIT = _FLUSH_INTERVAL_S

    def _collect_batch(self, wait: float) -> list[dict[str, object]]:
        batch: list[dict[str, object]] = []
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

    def _post(self, batch: list[dict[str, object]]) -> None:
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

    def close(self) -> None:
        if self._closed:
            return
        # Signal the worker and let it finish and close its own client. Capture
        # the worker we are tearing down first: a concurrent emit() (under the
        # handler lock) can revive the sink during the join below, reassigning
        # self._stop/_thread to fresh ones. Do NOT close the client here — the
        # worker's shutdown drain may still be minting a token / posting, and
        # closing it underneath raises inside httpx and kills the thread.
        stop, thread = self._stop, self._thread
        self._closed = True
        stop.set()
        thread.join(timeout=5.0)
        super().close()


# Process-wide sink; recreated only if a prior instance was closed (e.g. a
# logging reconfigure closed the root handlers out from under us).
_active_sink: ZerobusLogHandler | None = None
_sink_lock = threading.Lock()

# Dedicated logger for the server's outgoing SSE-event stream. It gets the sink
# as its sole handler with ``propagate=False`` (wired in attach_debug_log_sink),
# so its high-volume, table-only records — one per emitted event, names + safe
# ids, never content — never reach the on-disk/stderr logs.
SSE_LOGGER_NAME = "omnigent.sse_events"


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


def attach_debug_log_sink(loggers: list[logging.Logger], *, source: str, level: int) -> None:
    """Attach the shared debug-log sink to *loggers* when configured.

    A no-op unless the ``OMNIGENT_DEBUG_LOG_*`` variables are set. Reuses one
    handler per process; ``Logger.addHandler`` is idempotent for a given
    instance, so repeated calls do not double-ship.
    """
    global _active_sink
    config = config_from_env()
    if config is None:
        return
    with _sink_lock:
        if _active_sink is None or _active_sink.closed:
            try:
                _active_sink = ZerobusLogHandler(config, source)
            except Exception:  # noqa: BLE001 — the sink must never break logging setup
                # Honor the module's best-effort contract: handler construction
                # (httpx client, uploader thread, probe timer) failing must not
                # take down configure_process_logging() and thus process startup.
                _logger.warning("debug-log sink disabled: handler init failed", exc_info=True)
                return
            _logger.info(
                "debug-log sink enabled: source=%s table=%s endpoint=%s",
                source,
                config.table,
                config.insert_url,
            )
        _active_sink.setLevel(level)
        for target in loggers:
            target.addHandler(_active_sink)
        # Table-only SSE-event logger: the sink is its sole handler and it does
        # not propagate to root, so per-token delta events populate the table
        # without flooding the on-disk/stderr logs.
        sse_logger = logging.getLogger(SSE_LOGGER_NAME)
        sse_logger.setLevel(level)
        sse_logger.propagate = False
        sse_logger.addHandler(_active_sink)
