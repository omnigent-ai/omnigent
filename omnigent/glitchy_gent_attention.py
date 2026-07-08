"""One-shot Glitchy Gent attention ticket updater.

This module is deliberately read-only with respect to Omnigent runtime state:
it only issues ``GET`` requests, classifies compact operational tickets, and
optionally rewrites the generated section of the vault ticket board.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx

Priority = Literal["P0", "P1", "P2", "P3"]
TicketState = Literal["watch", "incident", "needs-choice", "handled", "snoozed"]

DEFAULT_VAULT = Path("/E/omnigent-vault")
DEFAULT_SERVER_URL = "http://192.168.2.1:6767"
CONTROL_DOC_REL = Path("wiki/glitchy-gent-control-room.md")
LEDGER_DOC_REL = Path("wiki/glitchy-gent-incident-ledger.md")
BOARD_DOC_REL = Path("wiki/glitchy-gent-attention-tickets.md")
START_MARKER = "<!-- GLITCHY-GENT-ATTENTION:START -->"
END_MARKER = "<!-- GLITCHY-GENT-ATTENTION:END -->"
CONVERSATION_ID_RE = re.compile(r"\bconv_[A-Za-z0-9]+\b")
SENSITIVE_WORD_RE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|bearer|credential|jwt|password|secret|token)\b"
)
SENSITIVE_ASSIGNMENT_RE = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|AUTHORIZATION|BEARER|CREDENTIAL|JWT|PASSWORD|"
    r"SECRET|TOKEN)[A-Z0-9_]*)\s*[:=]\s*([^\s,;`]+)"
)
LONG_SECRETISH_RE = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")

_REVIVAL_PHRASES = (
    "hello",
    "testing",
    "are you there",
    "come back",
    "please come back",
    "wake up",
    "are you alive",
    "you there",
    "ping",
)
_TIMEOUT_PHRASES = (
    "timed out",
    "timeout",
    "exceeded the 240s harness idle watchdog",
    "idle watchdog",
)
_MUTATION_PHRASES = (
    "account",
    "create",
    "created",
    "register",
    "registration",
    "upload",
    "publish",
    "delete",
    "restart",
    "deploy",
    "mutation",
)
_BUSY_CHILD_TASK_STATUSES = frozenset({"queued", "in_progress", "running", "waiting"})
_ACTIVE_SESSION_STATUSES = frozenset({"running", "waiting"})


@dataclass(frozen=True)
class ChildActivity:
    """Compact child-session state used for parent watch tickets."""

    id: str
    title: str | None = None
    status: str | None = None
    busy: bool = False
    current_task_status: str | None = None
    last_task_error: dict[str, Any] | None = None
    pending_elicitations_count: int = 0

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> ChildActivity:
        """Parse a child-session API row into the compact activity shape."""
        return cls(
            id=str(raw["id"]),
            title=_optional_str(raw.get("title")),
            status=_optional_str(raw.get("status")),
            busy=bool(raw.get("busy", False)),
            current_task_status=_optional_str(raw.get("current_task_status")),
            last_task_error=_dict_or_none(raw.get("last_task_error")),
            pending_elicitations_count=_int_or_zero(raw.get("pending_elicitations_count")),
        )

    def is_active(self) -> bool:
        """Return whether this child appears to be actively working or waiting."""
        if self.busy:
            return True
        if self.status in _ACTIVE_SESSION_STATUSES:
            return True
        return self.current_task_status in _BUSY_CHILD_TASK_STATUSES


@dataclass(frozen=True)
class SessionActivity:
    """Compact read-only snapshot of a session plus recent activity signals."""

    id: str
    title: str | None
    status: str
    runner_online: bool | None = None
    host_online: bool | None = None
    archived: bool = False
    labels: dict[str, str] = field(default_factory=dict)
    agent_name: str | None = None
    last_task_error: dict[str, Any] | None = None
    pending_elicitations_count: int = 0
    parent_session_id: str | None = None
    recent_items_desc: list[dict[str, Any]] = field(default_factory=list)
    children: list[ChildActivity] = field(default_factory=list)

    @classmethod
    def from_api(
        cls,
        raw: dict[str, Any],
        *,
        recent_items_desc: Sequence[dict[str, Any]] = (),
        children: Sequence[ChildActivity] = (),
    ) -> SessionActivity:
        """Parse a session snapshot/list item into the compact activity shape."""
        return cls(
            id=str(raw["id"]),
            title=_optional_str(raw.get("title")),
            status=str(raw.get("status") or "idle"),
            runner_online=_optional_bool(raw.get("runner_online")),
            host_online=_optional_bool(raw.get("host_online")),
            archived=bool(raw.get("archived", False)),
            labels=_string_dict(raw.get("labels")),
            agent_name=_optional_str(raw.get("agent_name")),
            last_task_error=_dict_or_none(raw.get("last_task_error")),
            pending_elicitations_count=_int_or_zero(raw.get("pending_elicitations_count")),
            parent_session_id=_optional_str(raw.get("parent_session_id")),
            recent_items_desc=list(recent_items_desc),
            children=list(children),
        )

    @property
    def display_name(self) -> str:
        """Human label for markdown output."""
        return self.title or self.agent_name or self.id

    @property
    def is_attention_recovery(self) -> bool:
        """Return whether the session is an attention recovery lane."""
        role = self.labels.get("glitchy.role", "")
        name = self.display_name.lower()
        return role == "attention-recovery" or "attention recovery" in name


@dataclass(frozen=True)
class ItemSignals:
    """Derived signals from recent items, without carrying raw transcript text."""

    latest_item_is_error: bool = False
    has_recent_error: bool = False
    has_timeout: bool = False
    has_mutation_timeout: bool = False
    has_user_revival_after_error: bool = False
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class AttentionTicket:
    """Rendered attention ticket."""

    priority: Priority
    state: TicketState
    title: str
    session_name: str
    session_id: str
    evidence: tuple[str, ...]
    risk: str
    recommended_next_action: str
    allowed_action: str
    blocked_action: str
    sort_name: str = ""

    @property
    def sort_key(self) -> tuple[int, str, str]:
        """Stable ticket order."""
        return (_priority_rank(self.priority), self.sort_name or self.title, self.session_id)


class AttentionApiClient:
    """Small read-only HTTP client for the Omnigent Sessions API."""

    def __init__(self, server_url: str, *, timeout_s: float = 10.0) -> None:
        self.server_url = server_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.server_url,
            timeout=httpx.Timeout(timeout_s, connect=min(timeout_s, 10.0)),
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> AttentionApiClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def health(self) -> dict[str, Any]:
        """Fetch server health. Read-only."""
        return self._get_json("/health")

    def list_sessions(
        self,
        *,
        limit: int,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        """List recent sessions, including child rows, through the read-only API."""
        body = self._get_json(
            "/v1/sessions",
            params={
                "limit": limit,
                "sort_by": "updated_at",
                "order": "desc",
                "kind": "any",
                "include_archived": str(include_archived).lower(),
            },
        )
        return _list_data(body)

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        """Fetch a single session snapshot. Returns ``None`` for 404."""
        try:
            return self._get_json(
                f"/v1/sessions/{session_id}",
                params={"include_items": "false", "include_liveness": "true"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def list_recent_items(self, session_id: str, *, limit: int) -> list[dict[str, Any]]:
        """Fetch recent items newest-first. Read-only."""
        try:
            body = self._get_json(
                f"/v1/sessions/{session_id}/items",
                params={"limit": limit, "order": "desc"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        return _list_data(body)

    def list_child_sessions(self, session_id: str, *, limit: int = 100) -> list[ChildActivity]:
        """Fetch direct child-session summaries. Read-only."""
        try:
            body = self._get_json(
                f"/v1/sessions/{session_id}/child_sessions",
                params={"limit": limit, "order": "desc"},
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return []
            raise
        return [ChildActivity.from_api(row) for row in _list_data(body)]

    def _get_json(self, path: str, *, params: dict[str, object] | None = None) -> dict[str, Any]:
        """Issue one GET and return a JSON object."""
        response = self._client.get(path, params=params)
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict):
            raise ValueError(f"Expected JSON object from {path}, got {type(body).__name__}")
        return body


def collect_attention_activity(
    *,
    client: AttentionApiClient,
    vault: Path,
    session_limit: int,
    item_limit: int,
    include_archived_list: bool = False,
) -> list[SessionActivity]:
    """Collect current Omnigent activity without mutating runtime state."""
    known_ids = session_ids_from_vault(vault)
    list_rows = client.list_sessions(limit=session_limit, include_archived=include_archived_list)
    candidate_ids = {str(row["id"]) for row in list_rows if row.get("id")}
    candidate_ids.update(known_ids)

    activities: list[SessionActivity] = []
    for session_id in sorted(candidate_ids):
        snapshot = client.get_session(session_id)
        if snapshot is None:
            continue
        recent_items = (
            client.list_recent_items(session_id, limit=item_limit)
            if _should_fetch_items(snapshot, known_ids)
            else []
        )
        parent_id = _optional_str(snapshot.get("parent_session_id"))
        children = [] if parent_id else client.list_child_sessions(session_id, limit=100)
        activities.append(
            SessionActivity.from_api(
                snapshot,
                recent_items_desc=recent_items,
                children=children,
            )
        )
    return activities


def session_ids_from_vault(vault: Path) -> set[str]:
    """Extract known session IDs from the control-room docs and current board."""
    ids: set[str] = set()
    for rel in (CONTROL_DOC_REL, LEDGER_DOC_REL, BOARD_DOC_REL):
        path = vault / rel
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        ids.update(CONVERSATION_ID_RE.findall(text))
    return ids


def classify_sessions(sessions: Iterable[SessionActivity]) -> list[AttentionTicket]:
    """Classify session activity into deterministic P0/P1/P2/P3 tickets."""
    tickets: list[AttentionTicket] = []
    child_ids_with_parent_tickets: set[str] = set()
    parent_ids_with_child_tickets: set[str] = set()
    sessions_sorted = sorted(sessions, key=lambda s: (s.display_name.lower(), s.id))

    for session in sessions_sorted:
        active_children = [child for child in session.children if child.is_active()]
        if active_children:
            child_ids_with_parent_tickets.update(child.id for child in active_children)
            parent_ids_with_child_tickets.add(session.id)
            tickets.append(_active_children_ticket(session, active_children))

    for session in sessions_sorted:
        ticket = _classify_single_session(session)
        if ticket is None:
            continue
        if (
            ticket.priority == "P2"
            and session.parent_session_id is not None
            and session.id in child_ids_with_parent_tickets
        ):
            continue
        if ticket.priority == "P2" and session.id in parent_ids_with_child_tickets:
            continue
        tickets.append(ticket)

    deduped: dict[tuple[str, str, str], AttentionTicket] = {}
    for ticket in tickets:
        deduped[(ticket.priority, ticket.session_id, ticket.title)] = ticket
    return sorted(deduped.values(), key=lambda ticket: ticket.sort_key)


def render_board_section(
    tickets: Sequence[AttentionTicket],
    *,
    generated_at: dt.datetime,
    server_url: str,
    session_count: int,
) -> str:
    """Render the generated markdown section for the attention board."""
    generated = generated_at.astimezone(dt.timezone.utc).replace(microsecond=0)
    active = [ticket for ticket in tickets if ticket.priority in {"P0", "P1", "P2"}]
    backlog = [ticket for ticket in tickets if ticket.priority == "P3"]
    has_incident = any(t.priority in {"P0", "P1"} for t in active)
    mode = "Incident Recovery" if has_incident else "Ambient Watch"
    reason = _mode_reason(active)
    lines = [
        START_MARKER,
        "## Mode",
        "",
        f"- Current mode: `{mode}`",
        f"- Reason: {reason}",
        f"- Generated: `{generated.isoformat().replace('+00:00', 'Z')}`",
        f"- Source: read-only Omnigent snapshots from `{server_url}`",
        f"- Sessions inspected: `{session_count}`",
        "",
        "## Active Tickets",
        "",
    ]
    if active:
        for ticket in active:
            lines.extend(_render_ticket(ticket))
            lines.append("")
    else:
        lines.extend(["- No active `P0`, `P1`, or `P2` tickets.", ""])

    lines.extend(["## P3 Backlog", ""])
    if backlog:
        for ticket in backlog[:8]:
            lines.extend(_render_ticket(ticket))
            lines.append("")
    else:
        lines.extend(["- No stale hygiene tickets from the inspected sessions.", ""])

    lines.extend(
        [
            "## Operating Rule",
            "",
            "The librarian should speak only when a ticket is `P0`, `P1`, newly resolved, "
            "or requires Benjamin's choice. `P2` stays quiet unless Benjamin asks about it. "
            "`P3` goes to the incident ledger only.",
            "",
            END_MARKER,
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def update_board_text(
    existing: str,
    generated_section: str,
    *,
    generated_at: dt.datetime,
) -> str:
    """Replace only the generated section, adding markers on first migration."""
    text = _update_frontmatter_timestamp(existing, generated_at)
    if START_MARKER in text and END_MARKER in text:
        start = text.index(START_MARKER)
        end = text.index(END_MARKER, start) + len(END_MARKER)
        return text[:start].rstrip() + "\n\n" + generated_section.rstrip() + text[end:] + "\n"

    mode_pos = text.find("\n## Mode")
    if mode_pos != -1:
        prefix = text[:mode_pos].rstrip()
        return prefix + "\n\n" + generated_section

    return text.rstrip() + "\n\n" + generated_section


def sanitize_evidence(text: str, *, max_len: int = 180) -> str:
    """Redact sensitive-looking values and keep evidence compact."""
    sanitized = SENSITIVE_ASSIGNMENT_RE.sub(r"\1=[redacted]", text)
    if SENSITIVE_WORD_RE.search(sanitized):
        sanitized = LONG_SECRETISH_RE.sub("[redacted]", sanitized)
    sanitized = " ".join(sanitized.split())
    if len(sanitized) > max_len:
        sanitized = sanitized[: max_len - 1].rstrip() + "..."
    return sanitized


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the one-shot updater."""
    args = _build_arg_parser().parse_args(argv)
    vault = Path(args.vault)
    board_path = Path(args.board) if args.board else vault / BOARD_DOC_REL
    generated_at = _parse_now(args.now) if args.now else dt.datetime.now(dt.timezone.utc)

    try:
        with AttentionApiClient(args.server, timeout_s=args.timeout) as client:
            client.health()
            sessions = collect_attention_activity(
                client=client,
                vault=vault,
                session_limit=args.limit,
                item_limit=args.item_limit,
                include_archived_list=args.include_archived_list,
            )
    except Exception as exc:  # noqa: BLE001 - CLI should return a clear one-line failure
        print(f"glitchy-gent-attention: failed to read Omnigent state: {exc}", file=sys.stderr)
        return 2

    tickets = classify_sessions(sessions)
    section = render_board_section(
        tickets,
        generated_at=generated_at,
        server_url=args.server,
        session_count=len(sessions),
    )
    existing = _read_existing_board(board_path)
    updated = update_board_text(existing, section, generated_at=generated_at)

    if args.dry_run:
        print(updated, end="")
        return 0

    board_path.parent.mkdir(parents=True, exist_ok=True)
    board_path.write_text(updated, encoding="utf-8")
    print(f"Updated {board_path} with {len(tickets)} ticket(s).")
    return 0


def _classify_single_session(session: SessionActivity) -> AttentionTicket | None:
    signals = _item_signals(session.recent_items_desc)
    last_error = _last_error(session)
    evidence = _base_evidence(session)
    if signals.error_code or signals.error_message:
        evidence.append(_error_evidence(signals))
    elif last_error is not None:
        evidence.append(_last_task_error_evidence(last_error))

    if session.is_attention_recovery and session.status == "failed":
        return AttentionTicket(
            priority="P1",
            state="incident",
            title="Attention Recovery Spare Failed",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence),
            risk="Cold spare cannot be trusted as a recovery lane while it is failed.",
            recommended_next_action=(
                "Keep the primary attention librarian as the control lane; schedule spare "
                "repair only when Benjamin authorizes implementation work."
            ),
            allowed_action="Record and report the limitation.",
            blocked_action=(
                "No restart, delete, archive, or repair without explicit authorization."
            ),
            sort_name="attention recovery spare",
        )

    if session.pending_elicitations_count > 0:
        count = session.pending_elicitations_count
        evidence.append(f"{count} pending approval/input prompt(s) are outstanding.")
        return AttentionTicket(
            priority="P1",
            state="needs-choice",
            title="Session Waiting For Choice",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence),
            risk="Work is parked until a human decision is made.",
            recommended_next_action="Surface the pending choice with concise context.",
            allowed_action="Inspect and summarize the prompt.",
            blocked_action="Do not approve, decline, retry, or interrupt on Benjamin's behalf.",
            sort_name=session.display_name.lower(),
        )

    if session.status in _ACTIVE_SESSION_STATUSES and session.runner_online is False:
        return AttentionTicket(
            priority="P0",
            state="incident",
            title="Running Session Has No Runner",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence),
            risk="The visible session may be unable to receive or complete work.",
            recommended_next_action=(
                "Inspect read-only session and host state before advising recovery."
            ),
            allowed_action="Report status and preserve context.",
            blocked_action=(
                "No restart, reconnect, cancel, or repair without explicit authorization."
            ),
            sort_name=session.display_name.lower(),
        )

    if _is_p0_flow_damage(session, signals):
        title = (
            "Matrix Account Creation State Unknown"
            if "matrix" in session.display_name.lower()
            else "Session Flow Interrupted After Error"
        )
        risk = (
            "Retrying account creation could duplicate state or hide the real failure."
            if "matrix" in session.display_name.lower()
            else "Work may be lost or duplicated if recovery proceeds without inspecting receipts."
        )
        return AttentionTicket(
            priority="P0",
            state="incident",
            title=title,
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence + _p0_signal_evidence(signals)),
            risk=risk,
            recommended_next_action=(
                "Inspect read-only receipts and live status before retrying the failed operation."
            ),
            allowed_action="Advise, summarize, and inspect live status when requested.",
            blocked_action=(
                "No retry, restart, cancel, delete, or runtime repair without authorization."
            ),
            sort_name=session.display_name.lower(),
        )

    if signals.has_mutation_timeout:
        return AttentionTicket(
            priority="P1",
            state="incident",
            title="Tool Timeout With Unknown Side Effects",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence + ["recent timeout involved a mutation-shaped operation."]),
            risk="The operation may have partially succeeded; repeating it could duplicate work.",
            recommended_next_action="Inspect operation receipts before retrying.",
            allowed_action="Read receipts, logs, and status only.",
            blocked_action="No retry or compensating mutation without explicit authorization.",
            sort_name=session.display_name.lower(),
        )

    if session.status == "failed":
        code = _optional_str(last_error.get("code")) if last_error is not None else None
        priority: Priority = (
            "P3" if session.archived or code == "runner_disconnected" else "P1"
        )
        return AttentionTicket(
            priority=priority,
            state="incident" if priority == "P1" else "handled",
            title="Failed Session Needs Triage" if priority == "P1" else "Stale Failed Session",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence),
            risk=(
                "The session failed and may need recovery."
                if priority == "P1"
                else "Historical failure; no current user-flow signal was detected."
            ),
            recommended_next_action=(
                "Inspect recent read-only items before deciding whether to recover."
                if priority == "P1"
                else "Leave in backlog unless Benjamin resumes this work."
            ),
            allowed_action="Record and summarize current state.",
            blocked_action=(
                "No restart, delete, archive, or repair without explicit authorization."
            ),
            sort_name=session.display_name.lower(),
        )

    if session.status in _ACTIVE_SESSION_STATUSES:
        return AttentionTicket(
            priority="P2",
            state="watch",
            title="Active Session Watch",
            session_name=session.display_name,
            session_id=session.id,
            evidence=tuple(evidence),
            risk="Healthy active work may finish with a result or ask for help.",
            recommended_next_action="Watch for completion, error, or pending user choice.",
            allowed_action="Observe read-only state.",
            blocked_action=(
                "Do not interrupt, duplicate-dispatch, or restart while progress is visible."
            ),
            sort_name=session.display_name.lower(),
        )

    return None


def _active_children_ticket(
    session: SessionActivity,
    active_children: Sequence[ChildActivity],
) -> AttentionTicket:
    child = sorted(active_children, key=lambda c: (c.title or c.id, c.id))[0]
    name = session.display_name.lower()
    title = "Postiz Worker Active" if "postiz" in name else "Child Worker Active"
    child_label = child.title or child.id
    child_status = (
        child.status or child.current_task_status or ("busy" if child.busy else "active")
    )
    return AttentionTicket(
        priority="P2",
        state="watch",
        title=title,
        session_name=session.display_name,
        session_id=session.id,
        evidence=tuple(
            _base_evidence(session)
            + [
                "child session "
                f"`{sanitize_evidence(child_label)}` / `{child.id}` is `{child_status}`.",
            ]
        ),
        risk="Healthy delegated work may produce a result or become blocked.",
        recommended_next_action=(
            "Wait for worker completion and surface the result when available."
        ),
        allowed_action="Watch read-only child state.",
        blocked_action="Do not interrupt or re-dispatch while progress is visible.",
        sort_name=session.display_name.lower(),
    )


def _item_signals(items_desc: Sequence[dict[str, Any]]) -> ItemSignals:
    latest_item_is_error = bool(items_desc and _is_error_item(items_desc[0]))
    error_index: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    has_timeout = False
    has_mutation_timeout = False

    for index, item in enumerate(items_desc):
        text = _item_text(item)
        lower = text.lower()
        item_has_timeout = _item_can_signal_timeout(item) and any(
            phrase in lower for phrase in _TIMEOUT_PHRASES
        )
        item_has_mutation = any(phrase in lower for phrase in _MUTATION_PHRASES)
        has_timeout = has_timeout or item_has_timeout
        has_mutation_timeout = has_mutation_timeout or (item_has_timeout and item_has_mutation)
        if error_index is None and _is_error_item(item):
            error_index = index
            error_code = _optional_str(item.get("code")) or _optional_str(item.get("type"))
            error_message = _optional_str(item.get("message")) or text

    revival_after_error = False
    if error_index is not None:
        revival_after_error = any(
            _is_revival_message(item) for item in items_desc[:error_index]
        )

    return ItemSignals(
        latest_item_is_error=latest_item_is_error,
        has_recent_error=error_index is not None,
        has_timeout=has_timeout,
        has_mutation_timeout=has_mutation_timeout,
        has_user_revival_after_error=revival_after_error,
        error_code=error_code,
        error_message=sanitize_evidence(error_message or "") if error_message else None,
    )


def _is_p0_flow_damage(session: SessionActivity, signals: ItemSignals) -> bool:
    if signals.has_user_revival_after_error:
        return True
    if session.status in _ACTIVE_SESSION_STATUSES and signals.latest_item_is_error:
        return True
    if session.status == "failed" and signals.has_user_revival_after_error:
        return True
    return False


def _p0_signal_evidence(signals: ItemSignals) -> list[str]:
    evidence: list[str] = []
    if signals.latest_item_is_error:
        evidence.append("recent transcript ends in an error item.")
    if signals.has_user_revival_after_error:
        evidence.append("user revival/testing message appeared after the error.")
    if signals.has_timeout:
        evidence.append("recent activity includes timeout/watchdog evidence.")
    return evidence


def _base_evidence(session: SessionActivity) -> list[str]:
    if session.runner_online is None:
        liveness = "unknown"
    else:
        liveness = "online" if session.runner_online else "offline"
    evidence = [f"session is `{session.status}`; runner is `{liveness}`."]
    if session.archived:
        evidence.append("session is archived.")
    return evidence


def _last_error(session: SessionActivity) -> dict[str, Any] | None:
    if session.last_task_error:
        return session.last_task_error
    code = session.labels.get("omnigent.last_task_error_code")
    message = session.labels.get("omnigent.last_task_error_message")
    if code or message:
        return {"code": code, "message": message}
    return None


def _error_evidence(signals: ItemSignals) -> str:
    code = signals.error_code or "error"
    message = f": {signals.error_message}" if signals.error_message else ""
    return sanitize_evidence(f"recent error `{code}`{message}")


def _last_task_error_evidence(error: dict[str, Any]) -> str:
    code = _optional_str(error.get("code")) or "error"
    message = _optional_str(error.get("message")) or ""
    return sanitize_evidence(f"last task error `{code}`: {message}")


def _render_ticket(ticket: AttentionTicket) -> list[str]:
    lines = [
        f"### {ticket.priority} - {ticket.title}",
        "",
        f"- Ticket state: `{ticket.state}`",
        f"- Session: `{sanitize_evidence(ticket.session_name)}` / `{ticket.session_id}`",
    ]
    for evidence in ticket.evidence:
        lines.append(f"- Evidence: {sanitize_evidence(evidence)}")
    lines.extend(
        [
            f"- Risk: {sanitize_evidence(ticket.risk)}",
            f"- Recommended next action: {sanitize_evidence(ticket.recommended_next_action)}",
            f"- Allowed action: {sanitize_evidence(ticket.allowed_action)}",
            f"- Blocked action: {sanitize_evidence(ticket.blocked_action)}",
        ]
    )
    return lines


def _mode_reason(active: Sequence[AttentionTicket]) -> str:
    p0 = [ticket for ticket in active if ticket.priority == "P0"]
    p1 = [ticket for ticket in active if ticket.priority == "P1"]
    p2 = [ticket for ticket in active if ticket.priority == "P2"]
    if p0:
        return f"active `P0` ticket: {sanitize_evidence(p0[0].title)}."
    if p1:
        return f"active `P1` ticket: {sanitize_evidence(p1[0].title)}."
    if p2:
        return f"no active `P0`/`P1`; watching {len(p2)} healthy active work item(s)."
    return "no active attention tickets from the inspected sessions."


def _read_existing_board(board_path: Path) -> str:
    try:
        return board_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()
        return (
            "---\n"
            "type: attention-ticket-board\n"
            "status: active\n"
            f"created: {now}\n"
            f"updated: {now}\n"
            "tags: [glitchy-gent, attention-librarian, tickets, omnigent]\n"
            "---\n\n"
            "# Glitchy Gent Attention Tickets\n\n"
            "Current compact ticket board for the attention librarian. This is not a raw "
            "transcript dump. It is the browsable operational queue used to switch "
            "between Ambient Watch and Incident Recovery.\n"
        )


def _update_frontmatter_timestamp(text: str, generated_at: dt.datetime) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---", 4)
    if end == -1:
        return text
    stamp = generated_at.astimezone(dt.timezone.utc).replace(microsecond=0)
    replacement = f"updated: {stamp.isoformat().replace('+00:00', 'Z')}"
    frontmatter = text[:end]
    if re.search(r"(?m)^updated:\s*.*$", frontmatter):
        frontmatter = re.sub(r"(?m)^updated:\s*.*$", replacement, frontmatter)
    else:
        frontmatter = frontmatter.rstrip() + "\n" + replacement
    return frontmatter + text[end:]


def _should_fetch_items(snapshot: dict[str, Any], known_ids: set[str]) -> bool:
    session_id = str(snapshot.get("id", ""))
    labels = _string_dict(snapshot.get("labels"))
    status = str(snapshot.get("status") or "")
    return (
        session_id in known_ids
        or status in {"running", "waiting", "failed"}
        or bool(snapshot.get("last_task_error"))
        or bool(labels.get("omnigent.last_task_error_code"))
        or _int_or_zero(snapshot.get("pending_elicitations_count")) > 0
    )


def _item_text(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("type", "code", "message", "name", "arguments", "output"):
        value = item.get(key)
        if isinstance(value, str):
            parts.append(value)
    content = item.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    return "\n".join(parts)


def _is_error_item(item: dict[str, Any]) -> bool:
    if item.get("type") == "error":
        return True
    status = item.get("status")
    return status in {"error", "failed"}


def _item_can_signal_timeout(item: dict[str, Any]) -> bool:
    """Return whether timeout text in this item is operational evidence."""
    if _is_error_item(item):
        return True
    if item.get("timed_out") is True:
        return True
    if item.get("type") != "function_call_output":
        return False
    return '"timed_out": true' in _item_text(item).lower()


def _is_revival_message(item: dict[str, Any]) -> bool:
    if item.get("type") != "message" or item.get("role") != "user":
        return False
    text = " ".join(_item_text(item).lower().split())
    if len(text) > 240:
        return False
    return any(phrase in text for phrase in _REVIVAL_PHRASES)


def _list_data(body: dict[str, Any]) -> list[dict[str, Any]]:
    data = body.get("data", [])
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def _priority_rank(priority: Priority) -> int:
    return {"P0": 0, "P1": 1, "P2": 2, "P3": 3}[priority]


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _dict_or_none(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _string_dict(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items() if isinstance(k, str) and v is not None}


def _int_or_zero(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return 0


def _parse_now(raw: str) -> dt.datetime:
    normalized = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _default_server_url() -> str:
    return (
        os.environ.get("OMNIGENT_ATTENTION_SERVER_URL")
        or os.environ.get("OMNIGENT_SERVER_URL")
        or DEFAULT_SERVER_URL
    )


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Update the Glitchy Gent attention ticket board from live Omnigent state."
    )
    parser.add_argument("--server", default=_default_server_url(), help="Omnigent server URL")
    parser.add_argument(
        "--vault",
        default=os.environ.get("OMNIGENT_VAULT", str(DEFAULT_VAULT)),
        help="Vault root containing wiki/glitchy-gent-*.md",
    )
    parser.add_argument(
        "--board",
        default=None,
        help="Board markdown path. Defaults to <vault>/wiki/glitchy-gent-attention-tickets.md",
    )
    parser.add_argument("--limit", type=int, default=100, help="Recent sessions to inspect")
    parser.add_argument("--item-limit", type=int, default=25, help="Recent items per candidate")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument(
        "--include-archived-list",
        action="store_true",
        help=(
            "Also page archived sessions from the list endpoint. Known vault IDs are always "
            "checked."
        ),
    )
    parser.add_argument("--dry-run", action="store_true", help="Print the updated board only")
    parser.add_argument(
        "--now",
        default=None,
        help=argparse.SUPPRESS,
    )
    return parser


if __name__ == "__main__":
    raise SystemExit(main())
