"""API route for importing normalized local harness transcripts."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import AsyncGenerator, AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast, get_args

import cachetools
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator, model_validator

from omnigent.db.utils import builtin_agent_id, now_epoch
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.debug_logging import add_audit_attrs, debug_event
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    CAP_IMPORT_SKIP_KNOWN,
    MAX_IMPORT_SKIP_IDS,
    HostImportLocalByIdFrame,
    HostImportLocalCancelFrame,
    HostImportLocalFrame,
    encode_host_frame,
)
from omnigent.native.native_coding_agents import native_coding_agent_for_harness
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._host_launch import (
    host_absent_error,
    resolve_host_owner,
)
from omnigent.server.routes._session_create_validation import resolve_project_session_create
from omnigent.server.routes.host_tunnel import PING_INTERVAL_S, LazyImportSessionPayload
from omnigent.server.schemas import SessionCreateRequest
from omnigent.session_import import (
    IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY,
    IMPORT_SOURCE_LABEL_KEY,
    ImportSource,
    title_from_items,
)
from omnigent.session_import.errors import (
    MISSING_SQLITE_MESSAGE,
    SKIPPED_IMPORT_CODES,
    ImportErrorCode,
    LocalImportError,
    import_code_is_retryable,
    mentions_missing_sqlite,
    missing_sqlite_error,
    reports_empty_session,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.conversation_store import ConversationAlreadyExistsError
from omnigent.stores.host_store import Host, HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore

_logger = logging.getLogger(__name__)

# Upper bound on items in one imported session, shared by the CLI-normalized
# ``/imports`` body and the host-streamed ``/imports/local`` path. Current
# loaders trim a longer history to ``session_import.local.IMPORT_MAX_ITEMS``
# (at most this), so this backstop only rejects older CLIs and hosts.
_MAX_IMPORT_ITEMS = 100_000
_LOCAL_IMPORT_STREAM_ERROR_MESSAGE = (
    "The local session import stopped unexpectedly. Retry the import or contact an administrator."
)


@dataclass(frozen=True)
class _ImportFailureReport:
    """What a client is told about an import that stopped part-way."""

    error_id: str
    message: str
    import_code: str
    retryable: bool
    # Extra machine-readable context (host_name, processed, total, ...).
    details: dict[str, object] = field(default_factory=dict)

    def body(self) -> dict[str, object]:
        """The ``error`` payload of the buffered route's HTTP body."""
        return {
            **self.details,
            "error_id": self.error_id,
            "message": self.message,
            "import_code": self.import_code,
            "retryable": self.retryable,
        }

    def stream_event(self) -> dict[str, object]:
        """The stream's ``{"event": "error", ...}`` line (import code as ``code``)."""
        return {
            **self.details,
            "event": "error",
            "error_id": self.error_id,
            "message": self.message,
            "code": self.import_code,
            "retryable": self.retryable,
        }


def _record_local_import_failure(exc: BaseException | None = None) -> _ImportFailureReport:
    """Log the active import exception and return what the client may see.

    A classified :class:`LocalImportError` already carries a safe, actionable
    message. Anything else may hold host paths or tracebacks, so the client gets
    only a generic message plus the error id that correlates to this log line.
    """
    error_id = f"err_{secrets.token_hex(16)}"
    if isinstance(exc, LocalImportError):
        _logger.warning(
            "Local session import stopped (%s): %s; error_id=%s",
            exc.import_code,
            exc.message,
            error_id,
            extra={"error_id": error_id, "import_code": exc.import_code},
        )
        details = {
            key: value
            for key, value in exc.details.items()
            if key not in ("import_code", "retryable", "code", "message", "event")
        }
        return _ImportFailureReport(error_id, exc.message, exc.import_code, exc.retryable, details)
    _logger.error(
        "Local session import failed; error_id=%s",
        error_id,
        exc_info=exc if exc is not None else True,
        extra={"error_id": error_id, "import_code": ImportErrorCode.INTERNAL},
    )
    return _ImportFailureReport(
        error_id,
        f"{_LOCAL_IMPORT_STREAM_ERROR_MESSAGE} Error ID: {error_id}.",
        ImportErrorCode.INTERNAL,
        True,
    )


class ImportItemInput(BaseModel):
    """One normalized existing Omnigent item received from the CLI."""

    type: str
    response_id: str = Field(min_length=1, max_length=64)
    data: dict[str, object]

    def to_item(self) -> NewConversationItem:
        """Validate the type-specific payload and return a new item entity."""
        try:
            data = parse_item_data(self.type, self.data)
            return NewConversationItem(type=self.type, response_id=self.response_id, data=data)
        except (TypeError, ValueError) as exc:
            raise OmnigentError(
                f"Invalid imported {self.type!r} item: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc


class ImportSessionRequest(BaseModel):
    """Request body for importing one local harness session.

    ``project_id`` files the imported session into a first-class project the
    caller owns, with the same ownership, default-fill, and mismatch-warning
    semantics as ``POST /v1/sessions``.
    """

    source: ImportSource
    external_session_id: str = Field(min_length=1, max_length=128)
    workspace: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=512)
    force: bool = False
    project_id: str | None = None
    # The importing CLI's own host, so the session binds back to the machine
    # the transcript came from and resumes there. Bound only alongside a
    # workspace (the workspace-required-for-host check constraint).
    host_id: str | None = None
    items: list[ImportItemInput] = Field(min_length=1, max_length=_MAX_IMPORT_ITEMS)

    @field_validator("external_session_id")
    @classmethod
    def strip_external_session_id(cls, value: str) -> str:
        """Reject a source session id that is only whitespace."""
        value = value.strip()
        if not value:
            raise ValueError("external_session_id must not be blank")
        return value


class ImportSessionResponse(BaseModel):
    """Result of importing or locating one source session."""

    session_id: str
    status: Literal["imported"]
    item_count: int


class LocalImportRequest(BaseModel):
    """Request to import local harness sessions from a host.

    Unlike ``/imports`` (the CLI posts already-normalized items), the server
    asks the chosen host to read + normalize its own transcripts over the
    tunnel — the transcripts live on the caller's machine, not the server. A
    supplied ``session_id`` loads that exact session without enumerating local
    history.
    """

    host_id: str
    # A specific harness, or "all" to import from every supported harness on
    # the host in one batch (each imported session keeps its own source).
    source: ImportSource | Literal["all"]
    limit: int = Field(default=10, ge=1, le=100)
    session_id: str | None = Field(default=None, min_length=1, max_length=128)

    @field_validator("session_id")
    @classmethod
    def strip_session_id(cls, value: str | None) -> str | None:
        """Reject an exact session id that is only whitespace."""
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError("session_id must not be blank")
        return value

    @model_validator(mode="after")
    def exact_import_needs_harness(self) -> LocalImportRequest:
        """An id is only meaningful within one harness namespace."""
        if self.session_id is not None and self.source == "all":
            raise ValueError("an exact session import requires a specific harness")
        return self


class ImportedSessionRef(BaseModel):
    """One freshly imported session: its new id plus display title.

    ``title`` is ``None`` when the session has no native title and no first user
    message to synthesize from; the UI falls back to a placeholder. Also the
    shape of each ``{"event": "session", ...}`` line the
    ``/imports/local/stream`` endpoint emits.
    """

    session_id: str
    title: str | None = None


class ImportFailureRef(BaseModel):
    """One session that could not be imported, with a user-facing reason.

    ``external_session_id`` / ``source`` name the source session when known (a
    host that reports only a count leaves them ``None``); ``reason`` explains the
    failure so the UI can show it instead of an anonymous "N failed". Also the
    shape of each ``{"event": "failed", ...}`` line on the stream endpoint, and
    of each skipped session (``{"event": "skipped", ...}``, code
    ``session_empty``): one that wasn't imported because it has no history.
    """

    external_session_id: str | None = None
    source: str | None = None
    reason: str
    # Stable import code (``omnigent.session_import.errors.ImportErrorCode``)
    # and whether re-running the import can succeed without user action.
    code: str = ImportErrorCode.SESSION_UNREADABLE
    retryable: bool = False
    # Correlates an ``internal`` failure with the server log line; clients show
    # it as a detail. ``None`` for every classified failure.
    error_id: str | None = None


class LocalImportResponse(BaseModel):
    """Buffered batch result for ``POST /v1/imports/local``.

    The streaming ``/imports/local/stream`` endpoint carries the same tally on
    its terminal ``{"event": "done", ...}`` line instead.
    """

    imported: int
    already_imported: int
    failed: int
    sessions: list[ImportedSessionRef]
    # One entry per failed session (with a reason); its length equals ``failed``.
    failures: list[ImportFailureRef] = Field(default_factory=list)
    # Sessions not imported because there was nothing to import (e.g. no
    # history); not failures, so older clients that read only ``failed`` and
    # ``failures`` never show them as errors. Its length equals ``skipped``.
    skipped: int = 0
    skipped_sessions: list[ImportFailureRef] = Field(default_factory=list)


@dataclass
class _ImportLockEntry:
    """One process-local source lock and its active/waiting user count."""

    lock: asyncio.Lock
    users: int = 0


_IMPORT_LOCKS: WorkspaceScopedCache[tuple[ImportSource, str], _ImportLockEntry] = (
    WorkspaceScopedCache()
)
_IMPORT_LOCKS_GUARD = threading.Lock()


def _import_conversation_id(source: ImportSource, external_session_id: str) -> str:
    """Derive one stable database identity for an imported source session."""
    value = f"import:{source}:{external_session_id}"
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _import_event_line(payload: dict[str, object]) -> bytes:
    """Encode one NDJSON line for the ``/imports/local`` stream."""
    return (json.dumps(payload) + "\n").encode()


async def _serialize_source_import(body: ImportSessionRequest) -> AsyncIterator[None]:
    """Serialize concurrent imports for one source identity in this server."""
    key = (body.source, body.external_session_id)
    with _IMPORT_LOCKS_GUARD:
        entry = _IMPORT_LOCKS.setdefault(key, _ImportLockEntry(lock=asyncio.Lock()))
        entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        with _IMPORT_LOCKS_GUARD:
            entry.users -= 1
            if entry.users == 0:
                _IMPORT_LOCKS.pop(key, None)


# Per-frame (inter-session) timeout: the host streams one session at a time, so
# this bounds the gap between frames — one transcript's read — not the whole
# batch. A batch of any size can take arbitrarily long without tripping it, so
# this can be tight: it's how fast a stalled or silently-dropped host is caught.
# Heartbeats don't shorten it: one can't overtake a multi-MiB session frame
# still in transit on the same socket.
_HOST_IMPORT_TIMEOUT_S: float = 60.0
# Whole-request budget, kept under the ~300 s route timeout of typical ingress
# proxies so the import ends with an explicit "run it again to continue" rather
# than the proxy cutting the response mid-stream.
_LOCAL_IMPORT_STREAM_DEADLINE_S: float = 270.0
# A healthy host answers every ping, so a registered tunnel silent for longer
# than this many ping intervals is dead but not yet reaped (the ping loop only
# declares it dead at 3x); importing through it would just wait out a timeout.
_HOST_STALE_PING_MULTIPLE = 1.5
# Saves one host import runs at once. Each save mostly waits on the store, so
# overlapping them is what closes most of the gap with the CLI's parallel
# uploads; 1 is fully serial. Every save holds one asyncio default-executor
# thread while it writes, and all saves of an import land on the replica holding
# the host's tunnel, so this stays well under that pool: Python 3.12 sizes it
# min(32, cpu_count + 4), and with 4 per import three concurrent imports on one
# replica still leave over half of it to other requests. A deployment tunes it
# with ``OMNIGENT_LOCAL_IMPORT_CONCURRENCY`` or, per request, through
# ``app.state.local_import_concurrency``.
LOCAL_IMPORT_CONCURRENCY = 4
_LOCAL_IMPORT_MAX_CONCURRENCY = 32
# Overrides LOCAL_IMPORT_CONCURRENCY for every request; 1 restores serial saves.
LOCAL_IMPORT_CONCURRENCY_ENV = "OMNIGENT_LOCAL_IMPORT_CONCURRENCY"
# Estimated memory of one save, which holds the decoded items, their validated
# models and their JSON at once: ~2 bytes per serialized item byte plus ~1.7 KiB
# per item, measured on 100,000-item (21 MiB) and 12,600-item (125 MiB)
# transcripts, before the store's own copies. Serialized bytes alone miss the
# per-item part: four 100,000-item sessions fit in 128 MiB of them, yet held
# ~4 GiB and exhausted a replica.
_IMPORT_SAVE_COST_PER_BYTE = 2
_IMPORT_SAVE_COST_PER_ITEM = 1700
# Saves in flight, except the most expensive one, stay within this estimated
# cost, so a concurrent import peaks at most this much above a serial one
# (which holds its most expensive session) whatever the input: two large
# sessions never save at once. Typical sessions are far smaller (a real
# last-100: largest ~6 MiB estimated), so they still save four at a time.
_LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST = 64 * 1024 * 1024
# How long saves still in flight may run past the stream deadline before they
# are cancelled (and rolled back): 270 + 15 s leaves 15 s under the ~300 s proxy
# timeout for the closing events and a busy event loop, instead of the response
# being cut mid-body.
_LOCAL_IMPORT_DRAIN_GRACE_S = 15.0
# Strong refs to save tasks and stream closes that outlive their request
# (cancelled client, deadline overrun) until they finish rolling back.
# custom-lint: disable-next=workspace-scoped-cache -- holds task objects, no tenant keys
_ABANDONED_IMPORT_TASKS: set[asyncio.Task[Any]] = set()
# What a stream pull returns once the host stream has ended.
_STREAM_END: Any = object()


def _clamp_local_import_concurrency(value: int) -> int:
    return min(max(value, 1), _LOCAL_IMPORT_MAX_CONCURRENCY)


def _local_import_concurrency_from_env() -> int:
    """:data:`LOCAL_IMPORT_CONCURRENCY_ENV` clamped to 1-32, else the default."""
    raw = os.environ.get(LOCAL_IMPORT_CONCURRENCY_ENV, "").strip()
    if not raw:
        return LOCAL_IMPORT_CONCURRENCY
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "%s=%r is not an integer; using %d",
            LOCAL_IMPORT_CONCURRENCY_ENV,
            raw,
            LOCAL_IMPORT_CONCURRENCY,
        )
        return LOCAL_IMPORT_CONCURRENCY
    return _clamp_local_import_concurrency(value)


def _keep_until_done(task: asyncio.Task[Any]) -> None:
    """Hold a strong ref to a task nobody awaits until it finishes."""
    _ABANDONED_IMPORT_TASKS.add(task)
    task.add_done_callback(_ABANDONED_IMPORT_TASKS.discard)


def _session_save_cost(raw_items: object) -> int:
    """Estimated in-memory cost of saving one streamed session's items.

    Runs in a thread, like :func:`_session_item_bytes`. Anything the save
    rejects up front (not a list, over the item cap) costs nothing.
    """
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_IMPORT_ITEMS:
        return 0
    serialized = _session_item_bytes(raw_items)
    return _IMPORT_SAVE_COST_PER_BYTE * serialized + _IMPORT_SAVE_COST_PER_ITEM * len(raw_items)


def _admits_save(in_flight_costs: Iterable[int], cost: int | None) -> bool:
    """Whether a save of estimated ``cost`` may start beside ``in_flight_costs``.

    ``None`` is a session still undecoded (a chunked one, so large): it counts
    as the most expensive. See :data:`_LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST`.
    """
    costs = list(in_flight_costs)
    if not costs:
        return True
    if cost is None:
        return sum(costs) <= _LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST
    costs.append(cost)
    return sum(costs) - max(costs) <= _LOCAL_IMPORT_IN_FLIGHT_EXTRA_COST


def _session_item_bytes(raw_items: object) -> int:
    """Approximate serialized size of one streamed session's items.

    Runs in a thread: a near-cap session is tens of MiB, and encoding item by
    item lets the event loop take the GIL back between items.
    """
    if not isinstance(raw_items, list) or len(raw_items) > _MAX_IMPORT_ITEMS:
        return 0
    size = 0
    for raw in raw_items:
        try:
            size += len(json.dumps(raw, ensure_ascii=False, separators=(",", ":"), default=str))
        except (TypeError, ValueError):
            continue
    return size


@dataclass(frozen=True)
class ImportProgress:
    """Sessions processed so far in a host import, for a progress readout.

    ``total`` is ``None`` until the host knows how many sessions it will send.
    """

    done: int
    total: int | None
    # How many of ``done`` the host skipped unread because the server already
    # has them (see ``skip_external_session_ids``).
    skipped: int = 0


# Process-local cache of the external ids an interrupted batch already stored,
# per (user, host), so its re-run can tell the host to skip them. Best effort:
# a restart or another replica just re-reads everything (the server dedupes).
_CONTINUE_SKIP_TTL_S = 15 * 60
_CONTINUE_SKIP_MAX_ENTRIES = 1024
_CONTINUE_SKIP_IDS: WorkspaceScopedCache[tuple[str | None, str], tuple[str, ...]] = (
    WorkspaceScopedCache(
        lambda: cachetools.TTLCache(maxsize=_CONTINUE_SKIP_MAX_ENTRIES, ttl=_CONTINUE_SKIP_TTL_S)
    )
)


def _continue_skip_ids(user_id: str | None, host_id: str) -> list[str]:
    """Ids the last interrupted batch import from this host already has."""
    return list(_CONTINUE_SKIP_IDS.get((user_id, host_id), ()))


def _remember_continue_skip_ids(user_id: str | None, host_id: str, ids: Iterable[str]) -> None:
    """Remember the newest ids an interrupted batch import has, for its re-run."""
    newest = list(dict.fromkeys(reversed(list(ids))))[:MAX_IMPORT_SKIP_IDS]
    if newest:
        _CONTINUE_SKIP_IDS[(user_id, host_id)] = tuple(newest)


def _host_skips_known(host_conn: object) -> bool:
    """Whether the host can skip sessions the server already has, unread."""
    capabilities = getattr(getattr(host_conn, "hello", None), "capabilities", None) or ()
    return CAP_IMPORT_SKIP_KNOWN in capabilities


def _host_label(host: object) -> str:
    """A human name for the machine in import messages."""
    name = getattr(host, "name", None)
    return f"“{name}”" if isinstance(name, str) and name.strip() else "Your machine"


def _ago(seconds: float) -> str:
    """Render an elapsed time compactly, e.g. ``"45 s"``, ``"3 min"``, ``"2 h"``."""
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    if seconds < 90 * 60:
        return f"{round(seconds / 60)} min"
    if seconds < 36 * 3600:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / 86400)} d"


def _host_offline_error(host: Host) -> LocalImportError:
    """The 409 for a host with no tunnel here, naming the machine and its last sighting.

    A row that still reads as live is a host the picker shows online whose
    tunnel is gone (a server restart or a tunnel drop that has not reconnected
    yet), so the advice differs from a host that has been gone for a while.
    """
    label = _host_label(host)
    updated_at = getattr(host, "updated_at", None)
    details: dict[str, object] = {}
    name = getattr(host, "name", None)
    if isinstance(name, str):
        details["host_name"] = name
    if isinstance(updated_at, int):
        last_seen = max(0, now_epoch() - updated_at)
        details["last_seen_seconds"] = last_seen
        # A host that dropped a moment ago reads better than "0 s ago".
        seen = " (last seen just now)" if last_seen < 5 else f" (last seen {_ago(last_seen)} ago)"
    else:
        seen = ""
    status = getattr(host, "status", None)
    if isinstance(status, str) and isinstance(updated_at, int) and host_is_live(host):
        message = (
            f"{label} isn't connected right now{seen}. It usually reconnects within a "
            "minute — try again shortly, or restart `omnigent host` on that machine."
        )
    else:
        message = (
            f"{label} is offline{seen}. Start `omnigent host` on that machine, then try again."
        )
    return LocalImportError(
        message,
        import_code=ImportErrorCode.HOST_OFFLINE,
        code=ErrorCode.CONFLICT,
        details=details,
    )


def _host_wrong_replica_error(host: Host, absent: OmnigentError) -> LocalImportError:
    """The 400 for a live host whose tunnel is on another replica.

    Keeps the global ``wrong_replica`` code the client's keyless re-address
    matches on; the import code and message are for when that retry also
    lands elsewhere (the tunnel is moving between replicas).
    """
    name = getattr(host, "name", None)
    has_name = isinstance(name, str) and bool(name.strip())
    whose = f"“{name}”'s" if has_name else "your machine's"
    return LocalImportError(
        f"Couldn't reach {whose} connection. Try again in a few seconds.",
        import_code=ImportErrorCode.HOST_UNREACHABLE,
        code=absent.code,
        http_status=absent.http_status,
        details={"host_name": name} if has_name else None,
    )


async def _stream_local_sessions_from_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    source: str,
    limit: int,
    session_id: str | None = None,
    stats: dict[str, Any] | None = None,
    ping_interval_s: float | None = None,
    deadline_s: float | None = None,
    skip_external_session_ids: Sequence[str] = (),
) -> AsyncGenerator[Mapping[str, Any] | ImportProgress, None]:
    """Yield requested local sessions one at a time as they stream in.

    Sends a recent or exact import frame and drains the per-request queue the
    tunnel fills: each ``host.import_local_session`` frame yields one session
    dict (``{total, external_session_id, workspace, items, title, source}``),
    each heartbeat an :class:`ImportProgress`; the terminal
    ``host.import_local_done`` ends the stream. The caller persists each session
    as it arrives, so a large batch never buffers in one frame.

    :param ping_interval_s: The tunnel's ping cadence. When given, a tunnel that
        has been silent for :data:`_HOST_STALE_PING_MULTIPLE` intervals fails
        fast with ``host_unreachable`` instead of being waited on.
    :param deadline_s: Whole-stream budget; defaults to
        :data:`_LOCAL_IMPORT_STREAM_DEADLINE_S`.
    :param skip_external_session_ids: Sessions the server already has; sent to
        a host that advertises :data:`CAP_IMPORT_SKIP_KNOWN` with a batch request.
    :raises LocalImportError: If the host is unreachable, disconnects, stops
        responding, or the deadline passes.
    :raises OmnigentError: If the host reports a read failure.
    """
    if ping_interval_s is not None:
        last_frame_at = getattr(host_conn, "last_frame_at", None)
        if isinstance(last_frame_at, (int, float)):
            silent_for = time.time() - last_frame_at
            if silent_for > ping_interval_s * _HOST_STALE_PING_MULTIPLE:
                raise LocalImportError(
                    f"host '{host_conn.host_id}' has sent nothing for {silent_for:.0f}s",
                    import_code=ImportErrorCode.HOST_UNREACHABLE,
                    details={"silent_seconds": int(silent_for)},
                )
    request_id = secrets.token_hex(8)
    request_frame = (
        HostImportLocalByIdFrame(
            request_id=request_id,
            source=source,
            session_id=session_id,
            allow_session_chunks=True,
            progress=True,
        )
        if session_id is not None
        else HostImportLocalFrame(
            request_id=request_id,
            source=source,
            limit=limit,
            allow_session_chunks=True,
            progress=True,
            skip_external_session_ids=(
                list(skip_external_session_ids) if _host_skips_known(host_conn) else []
            ),
        )
    )
    frame = encode_host_frame(request_frame)
    queue: asyncio.Queue[tuple[str, Mapping[str, Any]]] = asyncio.Queue()
    host_conn.pending_import_local[request_id] = queue
    deadline = time.monotonic() + (
        deadline_s if deadline_s is not None else _LOCAL_IMPORT_STREAM_DEADLINE_S
    )
    frame_timeout = _HOST_IMPORT_TIMEOUT_S
    finished = False
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise LocalImportError(
                f"host '{host_conn.host_id}' connection lost during import",
                import_code=ImportErrorCode.HOST_DISCONNECTED,
            ) from exc
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _time_limit_error()
            # Decide which bound fired here, not by re-reading the clock: a coarse
            # event-loop clock (uvloop) can fire a hair early, reading as a silent host.
            deadline_bound = remaining <= frame_timeout
            try:
                kind, data = await asyncio.wait_for(
                    queue.get(), timeout=remaining if deadline_bound else frame_timeout
                )
            except asyncio.TimeoutError as exc:
                if deadline_bound:
                    raise _time_limit_error() from exc
                raise LocalImportError(
                    f"host '{host_conn.host_id}' stalled mid-import "
                    f"(no session within {frame_timeout:.0f}s)",
                    import_code=ImportErrorCode.HOST_UNRESPONSIVE,
                    details={"silent_seconds": int(frame_timeout)},
                ) from exc
            if kind == "disconnected":
                raise LocalImportError(
                    f"host '{host_conn.host_id}' disconnected mid-import",
                    import_code=ImportErrorCode.HOST_DISCONNECTED,
                )
            if kind == "progress":
                # A chunk slice ({}) only proves the host is alive; a heartbeat
                # also carries the host's count.
                done = data.get("done")
                if isinstance(done, int):
                    total = data.get("total")
                    skipped = data.get("skipped")
                    yield ImportProgress(
                        done=done,
                        total=total if isinstance(total, int) else None,
                        skipped=skipped if isinstance(skipped, int) else 0,
                    )
                continue
            if kind == "session":
                yield data
                continue
            # "done"
            finished = True
            if data.get("status") != "ok":
                if mentions_missing_sqlite(data.get("error")):
                    # An older host imports sqlite3 eagerly, so a Python built
                    # without it fails the whole read; say how to fix it.
                    raise missing_sqlite_error()
                # The host's own words (it keeps paths out of them), classified
                # so the client shows them instead of "stopped unexpectedly":
                # re-running reads the same files, so it isn't retryable.
                host_error = data.get("error")
                raise LocalImportError(
                    host_error
                    if isinstance(host_error, str) and host_error.strip()
                    else "The machine couldn't read its local sessions.",
                    import_code=ImportErrorCode.HOST_READ_FAILED,
                    code=ErrorCode.INTERNAL_ERROR,
                )
            # Sessions the host enumerated but couldn't read send no frame;
            # surface the count (every host) and per-session reasons (newer
            # hosts) so the caller's tally covers every target and can
            # explain each failure.
            if stats is not None:
                stats["host_failed"] = int(data.get("failed") or 0)
                stats["host_skipped"] = int(data.get("skipped") or 0)
                raw_failures = data.get("failures")
                stats["host_failures"] = (
                    [entry for entry in raw_failures if isinstance(entry, dict)]
                    if isinstance(raw_failures, list)
                    else []
                )
            return
    finally:
        host_conn.pending_import_local.pop(request_id, None)
        if not finished:
            # Stop the host reading transcripts nobody will persist (deadline,
            # client gone, or an error here). Older hosts ignore the frame.
            with contextlib.suppress(Exception):
                host_registry.send_text(
                    host_conn, encode_host_frame(HostImportLocalCancelFrame(request_id=request_id))
                )


def _time_limit_error() -> LocalImportError:
    """The stream deadline passed; the caller restates it with the batch's progress."""
    return LocalImportError(
        "local import reached its time limit",
        import_code=ImportErrorCode.TIME_LIMIT_REACHED,
        code=ErrorCode.INTERNAL_ERROR,
        http_status=503,
    )


def _interrupted_import_error(
    exc: LocalImportError,
    *,
    host: object | None,
    processed: int,
    imported: int,
    total: int | None,
    already_imported: int = 0,
    host_skips_known: bool = True,
) -> LocalImportError:
    """Restate a host-liveness failure with the machine's name and the batch's progress.

    The stream consumer knows only that the tunnel went quiet or away; the
    import loop knows how far it got, which is what tells the user that a re-run
    continues rather than starts over.
    """
    label = _host_label(host)
    of_total = f" of {total}" if total is not None else ""
    progress = f"after {processed}{of_total} session{'s' if (total or processed) != 1 else ''}"
    code = exc.import_code
    if code == ImportErrorCode.HOST_DISCONNECTED:
        message = (
            f"{label} disconnected {progress}. Reconnect it (run `omnigent host`) and "
            "import again — sessions already imported are skipped."
        )
    elif code == ImportErrorCode.HOST_UNRESPONSIVE:
        silent = exc.details.get("silent_seconds")
        quiet = f" (nothing for {silent} s)" if isinstance(silent, int) else ""
        message = (
            f"{label} stopped responding {progress}{quiet}. Check that the machine is "
            "awake and `omnigent host` is running, then import again — sessions "
            "already imported are skipped."
        )
    elif code == ImportErrorCode.HOST_UNREACHABLE:
        silent = exc.details.get("silent_seconds")
        quiet = f" (nothing heard for {_ago(silent)})" if isinstance(silent, int) else ""
        message = (
            f"{label} isn't responding{quiet}. Check that the machine is awake and "
            "online and `omnigent host` is running, then try again."
        )
    elif code == ImportErrorCode.TIME_LIMIT_REACHED:
        # Sessions already there count: the re-run continues from all of them.
        have = imported + already_imported
        count = f"{have}{of_total}" if total is not None else f"{have} session(s)"
        if host_skips_known:
            message = (
                f"Imported {count} before the time limit — run it again to continue; "
                "already imported sessions are skipped."
            )
        else:
            # This host re-reads every session on a re-run, so the same slow
            # reads can hit the limit again at the same point.
            message = (
                f"Imported {count} before the time limit. Import fewer sessions at a "
                "time, or update Omnigent on that machine so a re-run skips the ones "
                "already imported."
            )
    else:
        return exc
    details = {
        key: value for key, value in exc.details.items() if key not in ("import_code", "retryable")
    }
    name = getattr(host, "name", None)
    if isinstance(name, str):
        details["host_name"] = name
    details.update(processed=processed, imported=imported, total=total)
    return LocalImportError(
        message,
        import_code=code,
        code=exc.code,
        http_status=exc.http_status,
        details=details,
    )


def _host_failure_code(entry: dict[str, Any]) -> str:
    """The import code for one host-reported per-session failure.

    Newer hosts send a ``code`` for the kinds they classify; older hosts send
    only a reason, which means the transcript couldn't be read, unless it is
    the loaders' "has no importable history" (an empty session).
    """
    code = entry.get("code")
    if isinstance(code, str) and code:
        return code
    reason = entry.get("reason")
    if mentions_missing_sqlite(reason):
        return ImportErrorCode.HOST_PYTHON_MISSING_SQLITE
    if reports_empty_session(reason):
        return ImportErrorCode.SESSION_EMPTY
    return ImportErrorCode.SESSION_UNREADABLE


def _log_import_outcome(
    route: str,
    *,
    source: str,
    counts: dict[str, int],
    failures: list[ImportFailureRef],
    error: _ImportFailureReport | None,
    started_at: float,
) -> None:
    """Record one import's outcome where the debug sink and logs can see it.

    The stream answers 200 before anything is imported, so request metrics and
    the audit envelope (written when the response starts) can't tell a failed
    import from a good one; this event carries the tally and every code.
    """
    failure_codes: dict[str, int] = {}
    for failure in failures:
        failure_codes[failure.code] = failure_codes.get(failure.code, 0) + 1
        _logger.info(
            "Imported session failed (%s)",
            failure.code,
            extra=debug_event(
                "import_session_failed",
                route=route,
                source=failure.source or source,
                code=failure.code,
                retryable=failure.retryable,
                external_session_id=failure.external_session_id,
            ),
        )
    outcome = "error" if error is not None else ("partial" if failures else "ok")
    log = _logger.warning if error is not None else _logger.info
    log(
        "Local session import finished (%s): imported=%d already_imported=%d failed=%d skipped=%d",
        outcome,
        counts.get("imported", 0),
        counts.get("already_imported", 0),
        counts.get("failed", 0),
        counts.get("skipped", 0),
        extra=debug_event(
            "import_local_finished",
            route=route,
            source=source,
            outcome=outcome,
            imported=counts.get("imported", 0),
            already_imported=counts.get("already_imported", 0),
            failed=counts.get("failed", 0),
            skipped=counts.get("skipped", 0),
            total=counts.get("total"),
            code=error.import_code if error is not None else None,
            error_id=(error.error_id or None) if error is not None else None,
            failure_codes=",".join(f"{code}:{n}" for code, n in sorted(failure_codes.items()))
            or None,
            duration_ms=int((time.monotonic() - started_at) * 1000),
        ),
    )


def _already_imported_error(message: str, session_id: str) -> LocalImportError:
    """The 409 for a duplicate import, naming the session that already exists."""
    return LocalImportError(
        message,
        import_code=ImportErrorCode.ALREADY_IMPORTED,
        code=ErrorCode.CONFLICT,
        details={"session_id": session_id},
    )


# An import conversation without its items/external id that is older than this
# was abandoned by a request that died (pod restart), not one still writing:
# the stream deadline plus a generous allowance for one session's writes.
_ABANDONED_IMPORT_AGE_S = 600

# Strong refs to rollbacks that outlive their cancelled request.
# custom-lint: disable-next=workspace-scoped-cache -- holds task objects, no tenant keys
_PENDING_ROLLBACKS: set[asyncio.Task[None]] = set()


async def _rollback_import(conversation_store: ConversationStore, conversation_id: str) -> None:
    """Delete a half-written import; logged, never raised over the original error."""
    try:
        await conversation_store.delete_conversation(conversation_id)
    except Exception:
        _logger.exception("Could not roll back partial import %s", conversation_id)


def _rollback_import_in_background(
    conversation_store: ConversationStore,
    write: asyncio.Future[None],
    created: list[str],
) -> None:
    """Roll back a cancelled import once its in-flight writes have finished."""

    async def _run() -> None:
        with contextlib.suppress(BaseException):
            await write
        # Even a write that completed is undone: the caller never learned it
        # succeeded, and an import is all-or-nothing from its point of view.
        if created:
            await _rollback_import(conversation_store, created[0])

    task = asyncio.get_running_loop().create_task(_run())
    _PENDING_ROLLBACKS.add(task)
    task.add_done_callback(_PENDING_ROLLBACKS.discard)


# The ``internal`` text for one session; the error id travels as ``error_id``
# (clients show it as a detail), never inline.
_SESSION_INTERNAL_ERROR_MESSAGE = (
    "Import stopped because of an internal error. Try again; if it keeps happening, "
    "contact an administrator."
)


def _classify_store_error(
    conversation_store: ConversationStore, exc: BaseException
) -> LocalImportError | None:
    """Ask the store to classify a persistence failure; never raises.

    Stores that predate the hook (or test doubles that don't subclass the ABC)
    simply have no classification, and a hook that itself fails must not turn
    one bad session into a failed batch.
    """
    classify = getattr(conversation_store, "classify_import_error", None)
    if classify is None:
        return None
    try:
        classified = classify(exc)
    except Exception:  # noqa: BLE001 - a broken hook degrades to "internal"
        _logger.warning("classify_import_error raised; treating as unclassified", exc_info=True)
        return None
    return classified if isinstance(classified, LocalImportError) else None


def _internal_session_error(exc: BaseException) -> LocalImportError:
    """The ``internal`` failure for one session, logged under a fresh error id.

    The traceback may hold paths or payload fragments, so the client gets the
    generic message and the id that finds this log line.
    """
    error_id = f"err_{secrets.token_hex(16)}"
    _logger.error(
        "Imported session failed unexpectedly; error_id=%s",
        error_id,
        exc_info=exc,
        extra={"error_id": error_id, "import_code": ImportErrorCode.INTERNAL},
    )
    return LocalImportError(
        _SESSION_INTERNAL_ERROR_MESSAGE,
        import_code=ImportErrorCode.INTERNAL,
        code=ErrorCode.INTERNAL_ERROR,
        details={"error_id": error_id},
    )


def _session_store_error(
    conversation_store: ConversationStore, exc: BaseException
) -> LocalImportError:
    """The classified failure for a non-Omnigent error persisting one session.

    A store-classified error carries its own actionable message; anything else
    is :func:`_internal_session_error`.
    """
    classified = _classify_store_error(conversation_store, exc)
    if classified is None:
        return _internal_session_error(exc)
    _logger.warning(
        "Imported session failed (%s): %s",
        classified.import_code,
        classified.message,
        exc_info=exc,
        extra={"import_code": classified.import_code},
    )
    return classified


def create_imports_router(
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    project_store: ProjectStore | None = None,
    host_registry: HostRegistry | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Create the local-session import router."""
    router = APIRouter()

    async def _persist_import(
        *,
        source: ImportSource,
        external_session_id: str,
        items: list[NewConversationItem],
        workspace: str | None,
        user_id: str | None,
        native_title: str | None = None,
        project_id: str | None = None,
        host_id: str | None = None,
    ) -> tuple[str, str | None]:
        """Create the conversation, append items, stamp import labels, grant owner.

        Shared by ``/imports`` (client-normalized items) and ``/imports/local``
        (server-read transcripts). ``native_title`` is the harness's own title
        when the caller has one; otherwise the title is synthesized from the
        first user message. ``project_id`` files the session into a project the
        caller owns (``/imports/local`` passes none). ``host_id`` binds the
        session to the host that read the transcript (``/imports/local``), so
        resuming defaults to the machine the workspace lives on; bound only
        alongside a workspace (the ``ck_conversations_workspace_required_for_host``
        check constraint). Caller handles the already-imported / force decision
        first. Returns ``(conversation id, title)``.
        """
        native_agent = native_coding_agent_for_harness(f"{source}-native")
        if native_agent is None:
            raise OmnigentError(
                f"Unsupported import source: {source}",
                code=ErrorCode.INVALID_INPUT,
            )
        agent_id = builtin_agent_id(native_agent.agent_name)
        if await asyncio.to_thread(agent_store.get, agent_id) is None:
            raise OmnigentError(
                f"The {native_agent.display_name} built-in agent is unavailable",
                code=ErrorCode.INTERNAL_ERROR,
            )
        # Route the optional target project through the shared create
        # chokepoint: ownership (unowned/unknown → 404), default-fill of
        # omitted fields from the project config, and mismatch warnings all
        # behave exactly as on POST /v1/sessions. Only genuinely-present
        # fields go into the body so absent ones stay defaultable.
        create_kwargs: dict[str, Any] = {"agent_id": agent_id}
        if workspace is not None:
            create_kwargs["workspace"] = workspace
            # The workspace path lives on the importing host; bind there so
            # resume lands on the right machine. Requires a workspace (check
            # constraint), so only set host_id when one is recorded.
            if host_id is not None:
                create_kwargs["host_id"] = host_id
        if project_id is not None:
            create_kwargs["project_id"] = project_id
        resolved_create = await resolve_project_session_create(
            body=SessionCreateRequest(**create_kwargs),
            user_id=user_id,
            project_store=project_store,
        )
        agent_id = resolved_create.body.agent_id
        workspace = resolved_create.body.workspace
        resolved_host_id = resolved_create.body.host_id
        title = (native_title or "").strip() or title_from_items(items)
        conversation_id = _import_conversation_id(source, external_session_id)

        def _create() -> Any:
            return conversation_store.create_conversation(
                title=title,
                agent_id=agent_id,
                host_id=resolved_host_id,
                workspace=workspace,
                conversation_id=conversation_id,
                project_id=resolved_create.project_id,
            )

        # Set once the row exists, so a rollback knows what to delete even when
        # a later write raised or the request was cancelled mid-write.
        created: list[str] = []

        async def _already_imported(existing: Any | None) -> LocalImportError:
            # Name the session that holds the import; a store may assign its own
            # ids, so the requested id is only the last resort.
            if existing is None:
                existing = await asyncio.to_thread(
                    conversation_store.find_conversation_by_external_session_id,
                    external_session_id,
                )
            return _already_imported_error(
                "This source session has already been imported",
                existing.id if existing is not None else conversation_id,
            )

        async def _write() -> None:
            try:
                conversation = await asyncio.to_thread(_create)
            except ConversationAlreadyExistsError as exc:
                # The deterministic id is taken: by a finished import (a real
                # duplicate), or by one a crash left half-written, which would
                # otherwise read as "already imported" forever.
                existing = await asyncio.to_thread(
                    conversation_store.get_conversation, conversation_id
                )
                if existing is None or not await _is_abandoned_import(
                    existing, source, external_session_id, user_id
                ):
                    raise await _already_imported(existing) from exc
                if not await _discard_abandoned_import(existing):
                    raise await _already_imported(None) from exc
                try:
                    conversation = await asyncio.to_thread(_create)
                except ConversationAlreadyExistsError as again:
                    raise await _already_imported(None) from again
            # A store may assign its own id (e.g. a numeric key), so every
            # later write targets the created row, not the requested id.
            created.append(conversation.id)
            await asyncio.to_thread(conversation_store.append, conversation.id, items)
            labels = {
                **native_agent.presentation_labels,
                IMPORT_SOURCE_LABEL_KEY: source,
                IMPORT_EXTERNAL_SESSION_ID_LABEL_KEY: external_session_id,
            }
            await asyncio.to_thread(conversation_store.set_labels, conversation.id, labels)
            if permission_store is not None and user_id is not None:
                await asyncio.to_thread(permission_store.ensure_user, user_id)
                await asyncio.to_thread(
                    permission_store.grant,
                    user_id,
                    conversation.id,
                    LEVEL_OWNER,
                )
            # Last: the external id is the dedupe key, so the source session only
            # counts as imported once everything above has landed.
            await asyncio.to_thread(
                conversation_store.set_external_session_id,
                conversation.id,
                external_session_id,
            )

        # Shielded: a cancelled request (client gone, deadline, shutdown) must
        # not interrupt the writes half-way; the rollback then waits for them
        # to finish before deleting, so nothing is written after the delete.
        write = asyncio.ensure_future(_write())
        try:
            await asyncio.shield(write)
        except Exception:
            if created:
                await _rollback_import(conversation_store, created[0])
            raise
        except BaseException:
            _rollback_import_in_background(conversation_store, write, created)
            raise
        return created[0], title

    async def _is_abandoned_import(
        conversation: Any, source: ImportSource, external_session_id: str, user_id: str | None
    ) -> bool:
        """Whether this importer may replace an import a dead request half-wrote.

        Only the deterministic import id qualifies (a native run of the same
        session has its own id). It must be older than any live import could
        be, and either never got its external id (written last) or holds no
        items (older servers wrote the external id first). The id derives from
        the source session alone, so another user importing the same external
        id lands on the same row: only its owner may discard it.
        """
        if conversation.id != _import_conversation_id(source, external_session_id):
            return False
        created_at = getattr(conversation, "created_at", None)
        if isinstance(created_at, (int, float)) and (
            now_epoch() - created_at < _ABANDONED_IMPORT_AGE_S
        ):
            return False
        if getattr(conversation, "external_session_id", None) == external_session_id:
            page = await asyncio.to_thread(conversation_store.list_items, conversation.id, limit=1)
            if page.data:
                return False
        return await _importer_owns_partial(conversation.id, user_id)

    async def _discard_abandoned_import(conversation: Any) -> bool:
        """Delete a half-written import judged abandoned, unless it was replaced since.

        The judgment awaits reads, so a concurrent import may already have
        replaced the row with a complete one; a changed ``created_at`` means
        that happened, and the fresh row is kept (``False``).
        """
        current = await asyncio.to_thread(conversation_store.get_conversation, conversation.id)
        if current is not None and getattr(current, "created_at", None) != getattr(
            conversation, "created_at", None
        ):
            return False
        _logger.warning("Replacing an abandoned partial import %s", conversation.id)
        await conversation_store.delete_conversation(conversation.id)
        return True

    async def _importer_owns_partial(conversation_id: str, user_id: str | None) -> bool:
        """Whether ``user_id`` may discard a half-written import row.

        The owner check is the one ``--force`` replacement uses. A row that
        never got its owner grant (the grant is written after the items)
        belongs to nobody, so the importer may replace it. Auth off (no
        permission store or user) is single-user, like the host check.
        """
        if permission_store is None or user_id is None:
            return True
        try:
            await require_access(
                user_id, conversation_id, LEVEL_OWNER, permission_store, conversation_store
            )
            return True
        except OmnigentError:
            pass
        try:
            return not await asyncio.to_thread(permission_store.has_any_grants, conversation_id)
        except NotImplementedError:
            # Stores whose ownership is fixed at create time (no grant rows)
            # can't have an ownerless row; the owner check above is final.
            return False

    @router.post(
        "/imports",
        response_model=ImportSessionResponse,
        dependencies=[
            Depends(require_json_content_type),
            Depends(_serialize_source_import),
        ],
    )
    async def import_session(
        body: ImportSessionRequest,
        request: Request,
        response: Response,
    ) -> ImportSessionResponse:
        """Import one normalized transcript, optionally replacing its prior import."""
        user_id = require_user(request, auth_provider)
        items = [item.to_item() for item in body.items]
        existing = await asyncio.to_thread(
            conversation_store.find_conversation_by_external_session_id,
            body.external_session_id,
        )
        if existing is not None and await _is_abandoned_import(
            existing, body.source, body.external_session_id, user_id
        ):
            # Half-written by a request that died: replace it, don't report it.
            if await _discard_abandoned_import(existing):
                existing = None
            else:
                existing = await asyncio.to_thread(
                    conversation_store.get_conversation, existing.id
                )
        if existing is not None:
            await require_access(
                user_id,
                existing.id,
                LEVEL_OWNER,
                permission_store,
                conversation_store,
            )
            if not body.force:
                # Matches a prior import or a native run of the same session
                # (both record the external id), so "exists", not "imported".
                # The id lets the CLI point at it instead of failing.
                add_audit_attrs(import_code=ImportErrorCode.ALREADY_IMPORTED)
                raise _already_imported_error(
                    f"This {body.source} session already exists as {existing.id}", existing.id
                )

        if existing is not None:
            await conversation_store.delete_conversation(existing.id)

        try:
            session_id, _title = await _persist_import(
                source=body.source,
                external_session_id=body.external_session_id,
                items=items,
                workspace=body.workspace,
                user_id=user_id,
                native_title=body.title,
                project_id=body.project_id,
                host_id=body.host_id,
            )
        except OmnigentError:
            # Already a deliberate response (e.g. an unknown project).
            raise
        except Exception as exc:
            # A storage failure the backend recognizes gets its status and message;
            # anything else is `internal` with the id of its log line.
            failure = _session_store_error(conversation_store, exc)
            error_id = failure.details.get("error_id")
            add_audit_attrs(
                import_code=failure.import_code,
                error_id=error_id if isinstance(error_id, str) else None,
            )
            raise failure from exc

        response.status_code = 201
        return ImportSessionResponse(
            session_id=session_id,
            status="imported",
            item_count=len(items),
        )

    def _resolve_import_target(
        request: Request, body: LocalImportRequest
    ) -> tuple[str | None, HostConnection, Host]:
        """Validate a host-mediated import and return ``(user_id, host_conn, host)``.

        Shared by the buffered ``/imports/local`` and the streaming
        ``/imports/local/stream``. Raises the usual HTTP error ahead of any
        response body when host infra is missing, the caller doesn't own the
        host, or it isn't connected.
        """
        if host_registry is None or host_store is None:
            raise OmnigentError(
                "host-mediated import is not available on this server",
                code=ErrorCode.INTERNAL_ERROR,
            )
        user_id = require_user(request, auth_provider)
        # Owns-host check + live connection, mirroring the runner-launch path.
        host = resolve_host_owner(user_id=user_id, host_id=body.host_id, host_store=host_store)
        host_conn = host_registry.get(body.host_id)
        if host_conn is None:
            # A live host absent from THIS replica is a wrong-replica landing, not
            # an offline host: WRONG_REPLICA (400) so the client re-addresses
            # keyless, CONFLICT (409) only when the row is genuinely stale.
            absent = host_absent_error(host)
            if absent.code == ErrorCode.CONFLICT:
                raise _host_offline_error(host)
            if absent.code == ErrorCode.WRONG_REPLICA:
                raise _host_wrong_replica_error(host, absent)
            raise absent
        return user_id, host_conn, host

    def _local_import_concurrency(request: Request) -> int:
        """Concurrent saves for this host import, as the deployment configured it.

        ``app.state.local_import_concurrency`` (a callable, read per request
        inside its request context) wins over :data:`LOCAL_IMPORT_CONCURRENCY_ENV`;
        any failure of either read falls back to the default.
        """
        concurrency_fn = getattr(request.app.state, "local_import_concurrency", None)
        if concurrency_fn is None:
            return _local_import_concurrency_from_env()
        try:
            value = int(concurrency_fn())
        except Exception:  # noqa: BLE001 - a broken override must not fail the import
            _logger.warning("local_import_concurrency failed; using the default", exc_info=True)
            return LOCAL_IMPORT_CONCURRENCY
        return _clamp_local_import_concurrency(value)

    async def _import_local_core(
        body: LocalImportRequest,
        user_id: str | None,
        host_conn: HostConnection,
        counts: dict[str, int],
        failures: list[ImportFailureRef],
        *,
        skipped_sessions: list[ImportFailureRef] | None = None,
        host: object | None = None,
        ping_interval_s: float | None = None,
        concurrency: int = 1,
    ) -> AsyncIterator[ImportedSessionRef | ImportProgress]:
        """Import the host's requested sessions, up to ``concurrency`` saves at once.

        Keeps reading the host's stream while earlier sessions save, bounded by
        ``concurrency`` saves and their estimated memory (:func:`_admits_save`);
        refs come out as saves finish, so their order may differ from the host's.
        Yields one ref per newly imported session, plus an :class:`ImportProgress`
        after each processed session and each host heartbeat, and tracks the
        running tally in ``counts`` (``imported`` / ``already_imported`` /
        ``failed`` / ``skipped``); each failed session appends an
        :class:`ImportFailureRef` (with a reason) to ``failures``, so
        ``len(failures) == counts["failed"]``, and each skipped one (no history
        to import) to ``skipped_sessions``.
        Persists each session as its frame arrives, so a large batch never
        buffers. Raises if the host read drops mid-stream, after the saves
        already started have finished (retry is idempotent, and re-import of a
        success comes back as already-imported, never a duplicate); a
        :class:`LocalImportError` raised for the host's liveness carries a message
        naming the machine and how far the batch got.
        """
        assert host_registry is not None  # guaranteed by _resolve_import_target
        # Each session carries its own source (an "all" import mixes harnesses),
        # falling back to the request source for a single-harness import.
        valid_sources = set(get_args(ImportSource))
        counts["imported"] = 0
        counts["already_imported"] = 0
        counts["failed"] = 0
        counts["skipped"] = 0
        skipped_refs = skipped_sessions if skipped_sessions is not None else []
        # The host's own count (including sessions it failed to read, which
        # send no frame) and the batch size, from heartbeats and session frames.
        host_done = 0
        total: int | None = None
        # Sessions the host skipped unread because an interrupted run already
        # has them: already imported, folded into the tally as they're reported.
        host_skipped = 0
        # Keyed by the registry's canonical id, whatever spelling the client used.
        skip_ids = (
            _continue_skip_ids(user_id, host_conn.host_id) if body.session_id is None else []
        )
        # External ids this run confirmed are in the store (imported or
        # already there), remembered if the run stops early.
        confirmed: list[str] = []

        def _note_skipped(reported: int) -> None:
            nonlocal host_skipped
            if reported > host_skipped:
                counts["already_imported"] += reported - host_skipped
                host_skipped = reported

        def _fail(
            external_session_id: object,
            source: object,
            reason: str,
            code: str = ImportErrorCode.SESSION_UNREADABLE,
            error_id: str | None = None,
        ) -> None:
            counts["failed"] += 1
            failures.append(
                ImportFailureRef(
                    external_session_id=(
                        external_session_id if isinstance(external_session_id, str) else None
                    ),
                    source=source if isinstance(source, str) else None,
                    reason=reason,
                    code=code,
                    retryable=import_code_is_retryable(code),
                    error_id=error_id,
                )
            )

        def _skip(external_session_id: object, source: object, reason: str, code: str) -> None:
            counts["skipped"] += 1
            skipped_refs.append(
                ImportFailureRef(
                    external_session_id=(
                        external_session_id if isinstance(external_session_id, str) else None
                    ),
                    source=source if isinstance(source, str) else None,
                    reason=reason,
                    code=code,
                    retryable=import_code_is_retryable(code),
                )
            )

        def _processed() -> int:
            return (
                counts["imported"]
                + counts["already_imported"]
                + counts["failed"]
                + counts["skipped"]
            )

        async def _import_one(session: dict[str, Any]) -> ImportedSessionRef | None:
            external_session_id = session.get("external_session_id")
            raw_items = session.get("items")
            session_source = session.get("source")
            source = (
                session_source
                if session_source in valid_sources
                else (body.source if body.source in valid_sources else None)
            )
            if (
                not isinstance(external_session_id, str)
                or not isinstance(raw_items, list)
                or source is None
                # Mirror the /imports item cap so one oversized transcript can't
                # balloon a batch import's memory.
                or len(raw_items) > _MAX_IMPORT_ITEMS
            ):
                too_large = isinstance(raw_items, list) and len(raw_items) > _MAX_IMPORT_ITEMS
                _fail(
                    external_session_id,
                    session_source,
                    "This session's transcript was malformed or too large to import.",
                    ImportErrorCode.SESSION_TOO_LARGE
                    if too_large
                    else ImportErrorCode.SESSION_UNREADABLE,
                )
                return None
            # The guard above rejected None and anything outside valid_sources
            # (get_args(ImportSource), which excludes "all"), so this is a
            # concrete harness — narrow off the request's ImportSource | "all".
            source = cast(ImportSource, source)
            try:
                existing = await asyncio.to_thread(
                    conversation_store.find_conversation_by_external_session_id,
                    external_session_id,
                )
                if existing is not None and await _is_abandoned_import(
                    existing, source, external_session_id, user_id
                ):
                    if await _discard_abandoned_import(existing):
                        existing = None
                if existing is not None:
                    counts["already_imported"] += 1
                    confirmed.append(external_session_id)
                    return None
                # Off the event loop: a near-cap session is 100,000 validations,
                # and other saves of the same import run meanwhile.
                items = await asyncio.to_thread(
                    lambda: [ImportItemInput.model_validate(raw).to_item() for raw in raw_items]
                )
                workspace = session.get("workspace")
                native_title = session.get("title")
                session_id, title = await _persist_import(
                    source=source,
                    external_session_id=external_session_id,
                    items=items,
                    workspace=workspace if isinstance(workspace, str) else None,
                    user_id=user_id,
                    native_title=native_title if isinstance(native_title, str) else None,
                    host_id=body.host_id,
                )
            except OmnigentError as exc:
                # A create that lost the dedup race collides on the deterministic
                # conversation id (CONFLICT). That's the same source session, so
                # it's already-imported, never a failure and never a duplicate.
                if exc.code == ErrorCode.CONFLICT and (
                    not isinstance(exc, LocalImportError)
                    or exc.import_code == ImportErrorCode.ALREADY_IMPORTED
                ):
                    counts["already_imported"] += 1
                    confirmed.append(external_session_id)
                    return None
                if isinstance(exc, LocalImportError):
                    code = exc.import_code
                elif exc.code == ErrorCode.INVALID_INPUT:
                    code = ImportErrorCode.SESSION_UNREADABLE
                else:
                    code = ImportErrorCode.INTERNAL
                _fail(external_session_id, source, exc.message, code)
                return None
            except ValueError:
                _fail(external_session_id, source, "This session's data could not be imported.")
                return None
            except Exception as exc:  # noqa: BLE001 - one session's failure must not end the batch
                # Storage errors (gRPC, DB driver, encryption) are not
                # OmnigentErrors. Left uncaught they ended the whole stream
                # mid-body, which the browser saw as a bare "network error".
                failure = _session_store_error(conversation_store, exc)
                error_id = failure.details.get("error_id")
                _fail(
                    external_session_id,
                    source,
                    failure.message,
                    failure.import_code,
                    error_id if isinstance(error_id, str) else None,
                )
                return None
            counts["imported"] += 1
            confirmed.append(external_session_id)
            return ImportedSessionRef(session_id=session_id, title=title)

        # Set by the stream to the sessions the host couldn't read (no frame
        # arrives for them): a count (every host) plus per-session reasons (newer
        # hosts). Folded into ``failed`` after the loop.
        stats: dict[str, Any] = {}
        stream = _stream_local_sessions_from_host(
            host_registry=host_registry,
            host_conn=host_conn,
            source=body.source,
            limit=body.limit,
            session_id=body.session_id,
            stats=stats,
            ping_interval_s=ping_interval_s,
            skip_external_session_ids=skip_ids,
        )

        async def _pull() -> Any:
            try:
                return await stream.__anext__()
            except StopAsyncIteration:
                return _STREAM_END

        def _close_stream() -> None:
            # Closing a stream parked at a yield runs its cleanup, which tells
            # the host to stop.
            _keep_until_done(asyncio.get_running_loop().create_task(stream.aclose()))

        max_saves = max(1, concurrency)
        loop = asyncio.get_running_loop()
        # Each save task and the estimated memory of the session it holds.
        in_flight: dict[asyncio.Task[ImportedSessionRef | None], int] = {}
        next_frame: asyncio.Task[Any] | None = None
        # A session read but not yet admitted, and its cost (None: undecoded).
        waiting_session: Any = None
        waiting_cost: int | None = None
        # Reading more frames; ``stream_open`` stays set until the stream
        # itself has finished (ended or raised), which a deadline cut doesn't do.
        streaming = True
        stream_open = True
        stream_error: Exception | None = None
        # The stream stops at its own deadline; saves already running get a grace
        # period past it, then are cancelled (and rolled back).
        drain_by = time.monotonic() + _LOCAL_IMPORT_STREAM_DEADLINE_S + _LOCAL_IMPORT_DRAIN_GRACE_S
        try:
            while streaming or in_flight or waiting_session is not None:
                if waiting_session is not None and (
                    max_saves == 1 or _admits_save(in_flight.values(), waiting_cost)
                ):
                    session, waiting_session = waiting_session, None
                    if isinstance(session, LazyImportSessionPayload):
                        session = await asyncio.to_thread(session.take)
                        if max_saves > 1:
                            waiting_cost = await asyncio.to_thread(
                                _session_save_cost, session.get("items")
                            )
                    # Copies this context (request, workspace, audit) like to_thread.
                    save = loop.create_task(_import_one(session))
                    in_flight[save] = waiting_cost or 0
                if (
                    streaming
                    and next_frame is None
                    and waiting_session is None
                    and len(in_flight) < max_saves
                ):
                    next_frame = loop.create_task(_pull())
                waiting: set[asyncio.Task[Any]] = set(in_flight)
                if next_frame is not None:
                    waiting.add(next_frame)
                done, _pending = await asyncio.wait(
                    waiting,
                    timeout=max(0.0, drain_by - time.monotonic()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # Saves still running this late would end past the proxy
                    # timeout. Undo them (none is reported or remembered); the
                    # re-run imports them.
                    for task in in_flight:
                        task.cancel()
                        _keep_until_done(task)
                    in_flight.clear()
                    # A host failure that ended the stream first is still the
                    # reason the import stopped.
                    if stream_error is None:
                        stream_error = _time_limit_error()
                    streaming = False
                    break
                for task in [t for t in done if t is not next_frame]:
                    in_flight.pop(task)
                    ref = task.result()
                    if ref is not None:
                        yield ref
                    yield ImportProgress(done=max(_processed(), host_done), total=total)
                if next_frame is None or next_frame not in done:
                    continue
                pulled = next_frame
                next_frame = None
                try:
                    session = pulled.result()
                except Exception as exc:  # noqa: BLE001 - raised once the saves in flight finish
                    streaming = stream_open = False
                    stream_error = exc
                    continue
                if session is _STREAM_END:
                    streaming = stream_open = False
                    continue
                if isinstance(session, ImportProgress):
                    _note_skipped(session.skipped)
                    host_done = max(host_done, session.done)
                    total = session.total if session.total is not None else total
                    if total is not None:
                        counts["total"] = total
                    yield ImportProgress(done=max(_processed(), host_done), total=total)
                    continue
                # A failed chunked session's placeholder carries ``total: 0``.
                if isinstance(session.get("total"), int) and session["total"] > 0:
                    total = session["total"]
                    counts["total"] = total
                waiting_session = session
                waiting_cost = (
                    None
                    if isinstance(session, LazyImportSessionPayload) or max_saves == 1
                    else await asyncio.to_thread(_session_save_cost, session.get("items"))
                )
        finally:
            # Only an early exit (client gone, an unexpected error) leaves saves
            # here: a cancelled save rolls back once its writes finish, through
            # _persist_import's shielded path. Nothing is awaited (this also runs
            # under GeneratorExit), so a cancelled caller can't be held up.
            for task in in_flight:
                task.cancel()
                _keep_until_done(task)
            if next_frame is not None and not next_frame.done():
                # A pull cancelled mid-read runs the stream's cleanup (the host's
                # cancel frame) itself; one cancelled before it started leaves
                # the stream parked, so close it once the pull settles (a close
                # can't overlap a running pull, and is a no-op on a finished one).
                next_frame.cancel()
                next_frame.add_done_callback(lambda _task: _close_stream())
            else:
                if next_frame is not None and not next_frame.cancelled():
                    next_frame.exception()  # consumed: an early exit dropped this frame
                if stream_open:
                    _close_stream()
        if stream_error is not None:
            if not isinstance(stream_error, LocalImportError):
                raise stream_error
            if body.session_id is None:
                _remember_continue_skip_ids(user_id, host_conn.host_id, [*skip_ids, *confirmed])
            raise _interrupted_import_error(
                stream_error,
                host=host,
                processed=_processed(),
                imported=counts["imported"],
                already_imported=counts["already_imported"],
                total=total,
                host_skips_known=_host_skips_known(host_conn),
            ) from stream_error
        _note_skipped(int(stats.get("host_skipped", 0)))
        if body.session_id is None:
            # Ran to the end: nothing left to continue.
            _CONTINUE_SKIP_IDS.pop((user_id, host_conn.host_id), None)
        # Fold in sessions the host enumerated but couldn't read. Newer hosts send
        # a per-session reason; older hosts send only a count, so synthesize a
        # generic reason for each so ``failed`` still equals ``len(failures)``.
        host_failures = stats.get("host_failures") or []
        if host_failures:
            for entry in host_failures:
                reason = str(entry.get("reason") or "This session could not be read on the host.")
                code = _host_failure_code(entry)
                if code in SKIPPED_IMPORT_CODES:
                    _skip(entry.get("external_session_id"), entry.get("source"), reason, code)
                    continue
                if code == ImportErrorCode.HOST_PYTHON_MISSING_SQLITE:
                    # A legacy host's raw ImportError text isn't actionable.
                    reason = MISSING_SQLITE_MESSAGE
                _fail(
                    entry.get("external_session_id"),
                    entry.get("source"),
                    reason,
                    code,
                )
        else:
            for _ in range(int(stats.get("host_failed", 0))):
                _fail(None, None, "This session could not be read on the host.")

    @router.post(
        "/imports/local",
        response_model=LocalImportResponse,
        dependencies=[Depends(require_json_content_type)],
    )
    async def import_local_sessions(
        body: LocalImportRequest,
        request: Request,
    ) -> LocalImportResponse | JSONResponse:
        """Import local transcripts from a chosen host.

        The transcripts live on the caller's machine, so the read happens on
        the connected host over its tunnel — the server can't see them. The
        host loads an exact id or enumerates recent sessions, then normalizes
        them; the server imports those not already imported.

        Buffered form: returns the whole batch's tally in one JSON body. For a
        live per-session list use ``POST /v1/imports/local/stream``. Not atomic:
        each session is persisted as its frame arrives, so if the host drops
        mid-stream this raises after the sessions read so far are already
        committed; a retry is idempotent (they come back as already-imported).
        """
        user_id, host_conn, host = _resolve_import_target(request, body)
        counts: dict[str, int] = {}
        sessions: list[ImportedSessionRef] = []
        failures: list[ImportFailureRef] = []
        skipped_sessions: list[ImportFailureRef] = []
        started_at = time.monotonic()

        def _finish(error: _ImportFailureReport | None) -> None:
            _log_import_outcome(
                "imports_local",
                source=body.source,
                counts=counts,
                failures=failures,
                error=error,
                started_at=started_at,
            )
            add_audit_attrs(
                import_code=error.import_code if error is not None else None,
                error_id=error.error_id if error is not None else None,
                imported=counts.get("imported", 0),
                already_imported=counts.get("already_imported", 0),
                failed=counts.get("failed", 0),
                skipped=counts.get("skipped", 0),
            )

        try:
            async for event in _import_local_core(
                body,
                user_id,
                host_conn,
                counts,
                failures,
                skipped_sessions=skipped_sessions,
                host=host,
                ping_interval_s=PING_INTERVAL_S,
                # Serial, as before: this is the fallback for clients without
                # the stream, and its session list keeps the host's order.
                concurrency=1,
            ):
                if isinstance(event, ImportedSessionRef):
                    sessions.append(event)
        except OmnigentError as exc:
            report = _record_local_import_failure(exc)
            _finish(report)
            return JSONResponse(
                status_code=exc.http_status,
                content={"error": {**exc.details, **report.body(), "code": exc.code}},
            )
        except Exception as exc:  # noqa: BLE001 - classified body instead of a bare 500
            report = _record_local_import_failure(exc)
            _finish(report)
            return JSONResponse(
                status_code=500,
                content={"error": {**report.body(), "code": ErrorCode.INTERNAL_ERROR}},
            )
        _finish(None)
        return LocalImportResponse(
            imported=counts.get("imported", 0),
            already_imported=counts.get("already_imported", 0),
            failed=counts.get("failed", 0),
            sessions=sessions,
            failures=failures,
            skipped=counts.get("skipped", 0),
            skipped_sessions=skipped_sessions,
        )

    @router.post(
        "/imports/local/stream",
        # Streams NDJSON, not a modeled JSON body — declare the media type so the
        # generated OpenAPI doesn't imply an application/json response.
        responses={200: {"content": {"application/x-ndjson": {}}}},
        dependencies=[Depends(require_json_content_type)],
    )
    async def import_local_sessions_stream(
        body: LocalImportRequest,
        request: Request,
    ) -> StreamingResponse:
        """Stream local transcripts from a chosen host.

        Same import as the buffered ``POST /v1/imports/local``, but responds with
        NDJSON: one ``{"event": "session", ...}`` line per newly imported session
        as its save finishes (several save at once, so not in the host's order),
        so the caller lists sessions as they arrive rather than waiting out the
        whole batch, and ``{"event": "progress", "done",
        "total"}`` lines as the host works through the batch. Each session that
        could not be imported emits one ``{"event": "failed",
        "external_session_id", "source", "reason"}`` line (after the successes),
        each one skipped because it has no history one ``{"event": "skipped",
        ...}`` line (same shape, code ``session_empty``), and a terminal
        ``{"event": "done", ...}`` carries the tally plus the full ``failures``
        and ``skipped_sessions`` lists. A mid-stream failure emits ``{"event": "error",
        "error_id", "message", "code", "retryable"}`` before ``done`` (the
        sessions read so far are already committed and a retry is idempotent).
        Request validation still fails ahead of the stream with the usual HTTP
        error.
        """
        user_id, host_conn, host = _resolve_import_target(request, body)
        concurrency = _local_import_concurrency(request)

        async def _events() -> AsyncIterator[bytes]:
            counts: dict[str, int] = {}
            failures: list[ImportFailureRef] = []
            skipped_sessions: list[ImportFailureRef] = []
            error: _ImportFailureReport | None = None
            started_at = time.monotonic()
            last_progress: tuple[int, int | None] | None = None
            try:
                async for event in _import_local_core(
                    body,
                    user_id,
                    host_conn,
                    counts,
                    failures,
                    skipped_sessions=skipped_sessions,
                    host=host,
                    ping_interval_s=PING_INTERVAL_S,
                    concurrency=concurrency,
                ):
                    if isinstance(event, ImportProgress):
                        # Heartbeats and per-session updates often repeat a count.
                        if (event.done, event.total) != last_progress:
                            last_progress = (event.done, event.total)
                            yield _import_event_line(
                                {"event": "progress", "done": event.done, "total": event.total}
                            )
                        continue
                    yield _import_event_line(
                        {"event": "session", "session_id": event.session_id, "title": event.title}
                    )
            except Exception as exc:  # noqa: BLE001 - the stream must always end with done
                # The 200 + partial body is already sent, so report the failure inline;
                # a body that just stops reads as a network error and loses the tally.
                # Cancellation (client gone) is a BaseException and still propagates.
                error = _record_local_import_failure(exc)
            except BaseException:
                # Client gone or server shutting down: nothing more can be sent,
                # but the outcome is still worth a record.
                _log_import_outcome(
                    "imports_local_stream",
                    source=body.source,
                    counts=counts,
                    failures=failures,
                    error=_ImportFailureReport(
                        "", "stream cancelled", ImportErrorCode.STREAM_INTERRUPTED, True
                    ),
                    started_at=started_at,
                )
                raise
            _log_import_outcome(
                "imports_local_stream",
                source=body.source,
                counts=counts,
                failures=failures,
                error=error,
                started_at=started_at,
            )
            # Per-session failures (with reasons) after the successes so the
            # caller can name each one, not just count them.
            for failure in failures:
                yield _import_event_line({"event": "failed", **failure.model_dump()})
            # A separate event so clients that predate it ignore it instead of
            # listing an empty session as a failure.
            for skipped in skipped_sessions:
                yield _import_event_line({"event": "skipped", **skipped.model_dump()})
            if error is not None:
                yield _import_event_line(error.stream_event())
            total = counts.get("total")
            yield _import_event_line(
                {
                    "event": "done",
                    "imported": counts.get("imported", 0),
                    "already_imported": counts.get("already_imported", 0),
                    "failed": counts.get("failed", 0),
                    "failures": [failure.model_dump() for failure in failures],
                    "skipped": counts.get("skipped", 0),
                    "skipped_sessions": [skipped.model_dump() for skipped in skipped_sessions],
                    "total": total if isinstance(total, int) else None,
                    "complete": error is None,
                }
            )

        return StreamingResponse(_events(), media_type="application/x-ndjson")

    return router
