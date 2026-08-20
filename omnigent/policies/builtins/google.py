"""Built-in Google Workspace access policies (MCP-agnostic).

Three sibling policy-callable factories, one per Google **resource**, sharing
the helpers in this module:

- :func:`gdrive_policy`   — Drive / Docs / Sheets / Slides files
  (read-allowlist, create gating, write-only-to-created, comments).
- :func:`gmail_policy`    — Gmail messages / drafts / send
  (default: read + draft, but **don't send** and don't modify).
- :func:`gcalendar_policy` — Calendar events
  (default: read-only).

They are split by resource, not by Google product name: Drive/Docs/Sheets/Slides
are all the same underlying resource (a Drive file keyed by a file ID), so they
share one policy; Gmail and Calendar are distinct resources with distinct verbs,
so they get their own. Each opines only on its own service's tools and abstains
on everything else, so they compose freely (attach any subset).

All three follow the policy contract (see
:mod:`omnigent.policies.schema`): ``tool_call`` events carry
``data = {"name", "arguments"}``, ``tool_result`` events carry
``data = {"result": <stringified-output>}``, and responses are the flat
``{"result": ..., "reason": ...}`` shape. Created file / draft IDs are tracked
across turns via the engine's persisted ``session_state`` (not closure state —
the engine is rebuilt per evaluation); this requires the server-side enforcement
path, which carries ``session_state``.

MCP-agnostic: tools are recognized by their *canonical* name after stripping a
server prefix. Defaults cover the standard ``mcp__google__*`` server and the
Databricks-hosted ``google__*`` server; override per-policy via ``tool_prefixes``.

.. important::

    **Scope — these policies enforce ONLY at the Google MCP tool-call
    boundary.** They recognize ``google__*`` / ``mcp__google__*`` tools and
    *abstain* on every other tool. They are one enforcement layer, NOT a
    complete sandbox over the underlying Google data. They do **not** cover
    other ways an agent can reach the same Drive / Gmail / Calendar data:

    - **Shell / code execution** (e.g. ``sys_os_shell``) calling the Google
      REST API directly (``https://www.googleapis.com/...``) with a token.
    - **``web_fetch`` / ``web_search``** reading a public or link-shared
      document by URL.
    - **HTTP-gateway / raw HTTP** access, or **UC functions / custom tools**
      that wrap Google internally under a non-``google__`` tool name.
    - **Sub-agents** that have their own Google tools or a shell.

    For an end-to-end "Google only via the governed MCP" guarantee, combine
    these with defense in depth: sandbox the shell (no network egress, no
    Google credentials in its environment), keep the MCP credential out of
    the agent's shell env, compose a shell/HTTP-egress policy, and ensure
    sub-agents inherit the same policies. Extending enforcement to the HTTP
    gateway is tracked as future work.

Each factory must be referenced via ``function: {path, arguments}`` with a
non-empty ``arguments`` block (the registry declares them ``kind: "factory"``).

YAML usage::

    policies:
      gdrive_guard:
        type: function
        function:
          path: omnigent.policies.builtins.google.gdrive_policy
          arguments: {read_all: false, read_files: ["1AbC..."], allow_create: true}
      gmail_guard:
        type: function
        function:
          path: omnigent.policies.builtins.google.gmail_policy
          arguments: {allow_send: false}
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from omnigent.policies.schema import PolicyEvent, PolicyResponse, StateUpdateEntry

# ── Shared constants ──────────────────────────────────────────────────────────

# Session-state keys for IDs the agent created this session (lists, so APPEND
# state-updates work — see ``engine._apply_one``). Public: they surface in the
# conversation's persisted ``session_state`` and callers may inspect them.
CREATED_FILES_STATE_KEY = "gdrive_created_file_ids"
CREATED_DRAFTS_STATE_KEY = "gmail_created_draft_ids"

# Tool-name prefixes stripped to obtain the canonical tool name. Longest-first
# so ``mcp__google__`` wins. Covers the standard + Databricks-hosted servers.
_DEFAULT_TOOL_PREFIXES: tuple[str, ...] = ("mcp__google__", "google__")

# Arg keys (in ``event["data"]["arguments"]``) carrying a target file ID.
_FILE_ID_ARG_KEYS: tuple[str, ...] = (
    "document_id",
    "file_id",
    "spreadsheet_id",
    "presentation_id",
)

# Result keys whose string value is a newly-created file ID. Covers both the
# raw Google API camelCase (``documentId``) and the snake_case a wrapping MCP
# may re-emit (``document_id``) — the Databricks Google MCP filters create
# results down to snake_case fields, so both spellings must be recognized.
_FILE_RESULT_ID_KEYS: frozenset[str] = frozenset(
    {
        "id",
        "documentId",
        "spreadsheetId",
        "presentationId",
        "fileId",
        "document_id",
        "spreadsheet_id",
        "presentation_id",
        "file_id",
    }
)

# Max recursion depth when scanning a tool-result payload for created IDs.
# Bounds the walk so a crafted, deeply-nested MCP response can't exhaust the
# Python stack; real Google results are only a few levels deep.
_MAX_RESULT_SCAN_DEPTH = 20

# Patterns that pull a file ID out of a Google URL, most- to least-specific.
_FILE_ID_URL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/document/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/spreadsheets/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/presentation/d/([a-zA-Z0-9_-]+)"),
    re.compile(r"/file/d/([a-zA-Z0-9_-]+)"),
)

# ── Drive / Docs / Sheets / Slides tool sets ──────────────────────────────────

_DRIVE_READ_TOOLS: frozenset[str] = frozenset(
    {
        "docs_document_get",
        "docs_document_list",
        "docs_document_inspect_structure",
        "docs_document_export",
        "docs_document_export_as_markdown",
        "docs_document_search",
        "drive_search",
        "drive_file_get",
        "drive_file_list",
        "drive_file_export",
        "drive_comment_list",
        "drive_comment_get",
        "sheets_search",
        "sheets_spreadsheet_get",
        "sheets_values_get",
        "sheets_values_batch_get",
        "sheets_grid_data_get",
        "slides_search",
        "slides_presentation_get",
        "slides_page_get",
    }
)
_DRIVE_CREATE_TOOLS: frozenset[str] = frozenset(
    {
        "docs_document_create",
        "docs_document_create_from_markdown",
        "drive_file_create",
        "sheets_spreadsheet_create",
        "slides_presentation_create",
    }
)
_DRIVE_COMMENT_TOOLS: frozenset[str] = frozenset(
    {"drive_comment_create", "drive_comment_reply_create"}
)
_DRIVE_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "docs_document_batch_update",
        "docs_document_edit_section",
        "docs_document_apply_template_styles",
        "drive_file_update",
        "drive_file_delete",
        "drive_file_copy",
        "sheets_values_update",
        "sheets_values_append",
        "sheets_spreadsheet_batch_update",
        "slides_presentation_batch_update",
    }
)
# Canonical-name prefixes the Drive policy owns (unrecognized → fail closed).
_DRIVE_OWNED_PREFIXES: tuple[str, ...] = ("docs_", "drive_", "sheets_", "slides_")

# ── Bell-LaPadula "no write-down" constants ────────────────────────────────────
#
# Bell-LaPadula is the classic lattice-based *confidentiality* model. Its
# "*-property" (star property) forbids a **write down**: a subject cleared for a
# sensitive level must not write to a less-classified object, because that moves
# classified data into a less-protected place (a leak).
#
# We model a simple two-level lattice — *confidential* vs. everything else — from
# a caller-supplied allowlist of confidential file IDs (``confidential_files``).
# There is intentionally no dependence on any per-document classification label:
# the set of confidential documents is declared explicitly, so the policy works
# on any Drive tenant. Once the session has read a confidential file, its writes
# are confined to the confidential set (a write elsewhere would be a write-down).
#
# Scope: this enforces only the write-down rule. It deliberately does NOT enforce
# the "simple security property" (no read-up) — the agent must be able to read
# confidential files for the containment to engage. It is a one-directional
# write-side guard, not a full multilevel-security kernel.

# Session-state key: has the session read any confidential file this session?
# Public: it surfaces in the conversation's persisted ``session_state``.
READ_CONFIDENTIAL_STATE_KEY = "gdrive_read_confidential"

# ── Gmail tool sets ───────────────────────────────────────────────────────────

# Arg keys carrying a target draft ID on a draft update / delete call.
_GMAIL_DRAFT_ID_ARG_KEYS: tuple[str, ...] = ("draft_id", "id")
# Result keys whose string value is a newly-created draft ID.
_GMAIL_DRAFT_RESULT_ID_KEYS: frozenset[str] = frozenset({"id", "draftId"})

_GMAIL_READ_TOOLS: frozenset[str] = frozenset(
    {
        "gmail_search",
        "gmail_message_list",
        "gmail_message_get",
        "gmail_thread_list",
        "gmail_thread_get",
        "gmail_profile_get",
    }
)
_GMAIL_SEND_TOOLS: frozenset[str] = frozenset({"gmail_message_send"})
_GMAIL_DRAFT_CREATE_TOOLS: frozenset[str] = frozenset({"gmail_draft_create"})
_GMAIL_DRAFT_WRITE_TOOLS: frozenset[str] = frozenset({"gmail_draft_update", "gmail_draft_delete"})
_GMAIL_MODIFY_TOOLS: frozenset[str] = frozenset({"gmail_message_modify", "gmail_thread_modify"})
_GMAIL_OWNED_PREFIXES: tuple[str, ...] = ("gmail_",)

# ── Calendar tool sets ────────────────────────────────────────────────────────

_CAL_READ_TOOLS: frozenset[str] = frozenset(
    {
        "calendar_search",
        "calendar_event_list",
        "calendar_event_get",
        "calendar_calendar_list",
        "calendar_freebusy_query",
    }
)
_CAL_CREATE_TOOLS: frozenset[str] = frozenset(
    {"calendar_event_create", "calendar_calendar_create"}
)
_CAL_MODIFY_TOOLS: frozenset[str] = frozenset({"calendar_event_update", "calendar_event_delete"})
_CAL_OWNED_PREFIXES: tuple[str, ...] = ("calendar_",)

_ALLOW: PolicyResponse = {"result": "ALLOW"}


# ── Shared helpers ────────────────────────────────────────────────────────────


def _deny(reason: str) -> PolicyResponse:
    """
    Build a DENY response with a human-readable reason.

    :param reason: Why the operation was blocked, e.g.
        ``"Read restricted to the configured allowlist."``.
    :returns: A :class:`PolicyResponse` with a DENY decision.
    """
    return {"result": "DENY", "reason": reason}


def _canonical_tool_name(tool_name: str, prefixes: tuple[str, ...]) -> str:
    """
    Strip the first matching server prefix to get the canonical name.

    :param tool_name: Raw tool name, e.g. ``"mcp__google__drive_file_get"`` or
        ``"google__drive_file_get"``.
    :param prefixes: Prefixes to try, longest-first.
    :returns: The canonical name (``"drive_file_get"``), or *tool_name*
        unchanged when no prefix matches.
    """
    for prefix in prefixes:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix) :]
    return tool_name


def _normalize_file_ref(value: str) -> str:
    """
    Reduce a file reference to its bare Google file ID.

    :param value: A bare file ID or a Google URL, e.g.
        ``"https://docs.google.com/document/d/1AbC/edit"`` or ``"1AbC"``.
    :returns: The extracted file ID, or the stripped input unchanged when no
        URL pattern matches. Empty string for empty input.
    """
    value = value.strip()
    if not value:
        return ""
    for pattern in _FILE_ID_URL_PATTERNS:
        match = pattern.search(value)
        if match:
            return match.group(1)
    parsed = urlparse(value)
    if parsed.netloc in {"drive.google.com", "docs.google.com"}:
        query = parse_qs(parsed.query)
        if query.get("id"):
            return query["id"][0]
    return value


def _normalize_file_refs(values: list[str] | None) -> set[str]:
    """
    Normalize a list of file references to a set of bare IDs.

    :param values: File IDs and/or Google URLs. ``None`` → empty list.
    :returns: Set of bare file IDs, empties dropped.
    """
    return {ref for value in (values or []) if (ref := _normalize_file_ref(value))}


def _extract_ids_from_args(  # type: ignore[explicit-any]
    args: dict[str, Any],
    keys: tuple[str, ...],
) -> set[str]:
    """
    Pull target resource IDs out of a tool call's arguments.

    :param args: The ``event["data"]["arguments"]`` dict, e.g.
        ``{"document_id": "1AbC"}``.
    :param keys: Arg keys to inspect, e.g. :data:`_FILE_ID_ARG_KEYS`.
    :returns: Set of normalized IDs (empty for un-scopeable calls like search).
    """
    ids: set[str] = set()
    for key in keys:
        value = args.get(key)
        if isinstance(value, str):
            normalized = _normalize_file_ref(value)
            if normalized:
                ids.add(normalized)
    return ids


def _extract_created_ids(  # type: ignore[explicit-any]
    payload: Any,
    id_keys: frozenset[str],
    max_depth: int = _MAX_RESULT_SCAN_DEPTH,
) -> set[str]:
    """
    Recursively collect created resource IDs from a result payload.

    The walk is depth-bounded (:data:`_MAX_RESULT_SCAN_DEPTH`) so a crafted,
    deeply-nested MCP response cannot exhaust the Python recursion stack;
    anything below the limit is simply not scanned (it would only ever add
    more permitted IDs, so truncating is safe — fail closed).

    :param payload: Parsed ``tool_result`` payload — dict, list, or scalar,
        e.g. ``{"documentId": "1New"}``.
    :param id_keys: Keys whose string value is a created ID, e.g.
        :data:`_FILE_RESULT_ID_KEYS`.
    :param max_depth: Remaining levels to descend; decremented per recursion.
        At ``0`` the walk stops. Defaults to :data:`_MAX_RESULT_SCAN_DEPTH`.
    :returns: Set of IDs found under any key in *id_keys*.
    """
    if max_depth <= 0:
        return set()
    ids: set[str] = set()
    if isinstance(payload, dict):
        for key, nested in payload.items():
            if key in id_keys and isinstance(nested, str) and nested.strip():
                ids.add(nested.strip())
            ids.update(_extract_created_ids(nested, id_keys, max_depth - 1))
    elif isinstance(payload, list):
        for item in payload:
            ids.update(_extract_created_ids(item, id_keys, max_depth - 1))
    return ids


def _parse_result_payload(data: Any) -> Any:  # type: ignore[explicit-any]
    """
    Extract and parse the tool output from a ``tool_result`` event.

    Handles the server-side shape (``{"result": "<json-or-text>"}``, value
    stringified) and the runner-side shape (raw string / already-structured).

    :param data: The raw ``event["data"]`` on a ``tool_result`` event.
    :returns: The parsed structure when the payload was JSON, else the raw value.
    """
    inner = data
    if isinstance(data, dict) and "result" in data:
        inner = data["result"]
    if isinstance(inner, str):
        with contextlib.suppress(ValueError, TypeError):
            return json.loads(inner)
    return inner


def _append_updates(
    new_ids: set[str],
    tracked: object,
    state_key: str,
) -> list[StateUpdateEntry]:
    """
    Build APPEND state-updates for IDs not already tracked.

    :param new_ids: IDs discovered in this result.
    :param tracked: Current value of the session-state list (honored only when
        it is a list).
    :param state_key: Session-state key to append under.
    :returns: One APPEND entry per genuinely-new ID, sorted; empty when nothing
        is new.
    """
    already = set(tracked) if isinstance(tracked, list) else set()
    return [
        {"key": state_key, "action": "append", "value": rid} for rid in sorted(new_ids - already)
    ]


def _created_ids_from_state(  # type: ignore[explicit-any]
    session_state: dict[str, Any],
    state_key: str,
) -> set[str]:
    """
    Read a tracked-IDs list out of session state as a set.

    :param session_state: The event's ``session_state`` dict.
    :param state_key: Key holding the list of tracked IDs.
    :returns: Set of tracked IDs (empty when absent or not a list).
    """
    raw = session_state.get(state_key) or []
    return set(raw) if isinstance(raw, list) else set()


def _resolve_prefixes(tool_prefixes: list[str] | None) -> tuple[str, ...]:
    """
    Resolve the server-prefix tuple from a factory's ``tool_prefixes`` arg.

    :param tool_prefixes: Override list, or ``None`` for the defaults.
    :returns: Prefix tuple to strip when canonicalizing tool names.
    """
    return tuple(tool_prefixes) if tool_prefixes is not None else _DEFAULT_TOOL_PREFIXES


@dataclass(frozen=True)
class _ParsedToolCall:  # type: ignore[explicit-any]  # synthesized __init__ has an Any arg
    """
    The fields a ``_decide_*`` helper needs from a ``tool_call`` event.

    :param canonical: Canonical tool name with the server prefix stripped, e.g.
        ``"drive_file_get"``.
    :param raw_tool: Original tool name as sent, e.g.
        ``"mcp__google__drive_file_get"`` (used verbatim in DENY messages).
    :param args: The tool arguments dict, e.g. ``{"document_id": "1AbC"}``
        (empty dict when absent or non-dict).
    """

    canonical: str
    raw_tool: str
    args: dict[str, Any]  # type: ignore[explicit-any]


def _parse_tool_call(event: PolicyEvent, prefixes: tuple[str, ...]) -> _ParsedToolCall | None:
    """
    Extract the canonical name, raw name, and args from a ``tool_call`` event.

    :param event: A ``tool_call`` policy event.
    :param prefixes: Server prefixes to strip for canonicalization.
    :returns: A :class:`_ParsedToolCall` when the event carries a string tool
        name, else ``None`` (the caller abstains).
    """
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    raw_tool = data.get("name")
    if not isinstance(raw_tool, str):
        return None
    args = data.get("arguments")
    args = args if isinstance(args, dict) else {}
    return _ParsedToolCall(_canonical_tool_name(raw_tool, prefixes), raw_tool, args)


# ── Google Drive / Docs / Sheets / Slides ─────────────────────────────────────


@dataclass(frozen=True)
class _DriveCfg:
    """
    Resolved Drive-policy configuration passed to :func:`_decide_drive_tool_call`.

    :param read_all: Allow all reads (``True``) vs restrict to ``read_ids``.
    :param read_ids: Normalized file IDs readable in restricted mode.
    :param write_ids: Normalized file IDs writable regardless of creation.
    :param comment_ids: Normalized file IDs the agent may comment on.
    :param allow_create: Whether new-file creation is permitted.
    :param confidential_ids: Normalized file IDs that form the confidential
        compartment. Non-empty enables Bell-LaPadula "no write-down": once the
        session reads any of these, writes are confined to this set.
    :param write_down_action: Verdict on a write-down violation — ``"DENY"``
        (default) or ``"ASK"`` (human approval).
    :param deny_reason: Reason prefix for DENY decisions.
    """

    read_all: bool
    read_ids: set[str]
    write_ids: set[str]
    comment_ids: set[str]
    allow_create: bool
    confidential_ids: set[str]
    write_down_action: str
    deny_reason: str


def _escalate(cfg: _DriveCfg, reason: str) -> PolicyResponse:
    """
    Build the configured write-down escalation response (DENY or ASK).

    :param cfg: Resolved Drive configuration (carries ``write_down_action``).
    :param reason: Human-readable explanation of the violated rule.
    :returns: A :class:`PolicyResponse` with the configured verdict.
    """
    action = cfg.write_down_action
    return {"result": action, "reason": f"{cfg.deny_reason} {reason}"}  # type: ignore[typeddict-item]


def _read_confidential_update(event: PolicyEvent, cfg: _DriveCfg) -> list[StateUpdateEntry]:
    """
    Flag the session as having read a confidential file, if this read is one.

    Inspects the originating read's target file IDs (from ``request_data``) and,
    when any is in the configured confidential compartment, emits a state-update
    setting :data:`READ_CONFIDENTIAL_STATE_KEY`. This is the "clearance rises on
    read" step of Bell-LaPadula, reduced to a single latch (the compartment is
    two-level: confidential vs. not).

    :param event: A ``tool_result`` event for a Drive read tool.
    :param cfg: Resolved Drive configuration (holds ``confidential_ids``).
    :returns: A one-element ``state_updates`` list when a confidential file was
        read (and the latch isn't already set), else empty.
    """
    if not cfg.confidential_ids:
        return []
    session_state = event.get("session_state") or {}
    if session_state.get(READ_CONFIDENTIAL_STATE_KEY):
        return []  # already latched — nothing to update
    request_data = event.get("request_data")
    args = request_data.get("arguments") if isinstance(request_data, dict) else None
    read_ids = _extract_ids_from_args(args if isinstance(args, dict) else {}, _FILE_ID_ARG_KEYS)
    if read_ids & cfg.confidential_ids:
        return [{"key": READ_CONFIDENTIAL_STATE_KEY, "action": "set", "value": True}]
    return []


def _check_no_write_down(
    target_ids: set[str],
    event: PolicyEvent,
    cfg: _DriveCfg,
) -> PolicyResponse | None:
    """
    Apply Bell-LaPadula's "no write-down" rule to a write / comment / create.

    Only meaningful once ``confidential_ids`` is configured. When the session
    has read a confidential file, a write is allowed only if its target is
    itself in the confidential compartment; any other target (a less-protected
    file, or a brand-new one) is a write-down and is DENYed (or ASKed). Returns
    ``None`` to abstain (session hasn't read confidential material, or the write
    stays inside the compartment).

    :param target_ids: Normalized target file IDs from the call (empty for a
        create, which has no pre-existing target).
    :param event: The ``tool_call`` event.
    :param cfg: Resolved Drive configuration.
    :returns: A DENY/ASK :class:`PolicyResponse` on violation, else ``None``.
    """
    if not cfg.confidential_ids:
        return None
    session_state = event.get("session_state") or {}
    if not session_state.get(READ_CONFIDENTIAL_STATE_KEY):
        return None  # no confidential data read yet — no containment
    # A write stays legal only when every target is inside the compartment.
    if target_ids and target_ids <= cfg.confidential_ids:
        return None
    return _escalate(
        cfg,
        "Bell-LaPadula (no write-down): this session has read a confidential "
        "document, so writes are confined to the confidential set; this target "
        "is outside it.",
    )


def _decide_drive_tool_call(
    event: PolicyEvent, prefixes: tuple[str, ...], cfg: _DriveCfg
) -> PolicyResponse | None:
    """
    Apply the Drive access rules to one ``tool_call`` event.

    :param event: The ``tool_call`` policy event.
    :param prefixes: Server prefixes to strip for canonicalization.
    :param cfg: Resolved Drive configuration.
    :returns: A :class:`PolicyResponse` decision, or ``None`` to abstain (read
        allowed, or tool not owned by this policy).
    """
    parsed = _parse_tool_call(event, prefixes)
    if parsed is None:
        return None
    canonical, raw_tool, args = parsed.canonical, parsed.raw_tool, parsed.args
    target_ids = _extract_ids_from_args(args, _FILE_ID_ARG_KEYS)
    created_ids = _created_ids_from_state(
        event.get("session_state") or {}, CREATED_FILES_STATE_KEY
    )

    if canonical in _DRIVE_READ_TOOLS:
        if cfg.read_all or (target_ids and target_ids <= cfg.read_ids):
            return None
        return _deny(
            f"{cfg.deny_reason} Read restricted to the configured allowlist; "
            f"this call targets a file outside it (or cannot be scoped)."
        )
    # Bell-LaPadula "no write-down" runs *before* the access-scope rules on any
    # tool that emits data into a file: once the session has read confidential
    # material, a write-down leak is blocked even if the file is otherwise
    # writable (e.g. one the agent created this session).
    if cfg.confidential_ids and (
        canonical in _DRIVE_WRITE_TOOLS
        or canonical in _DRIVE_COMMENT_TOOLS
        or canonical in _DRIVE_CREATE_TOOLS
    ):
        violation = _check_no_write_down(target_ids, event, cfg)
        if violation is not None:
            return violation

    if canonical in _DRIVE_CREATE_TOOLS:
        return (
            None
            if cfg.allow_create
            else _deny(f"{cfg.deny_reason} Creating new files is not permitted.")
        )
    if canonical in _DRIVE_COMMENT_TOOLS:
        if target_ids and target_ids <= (cfg.comment_ids | created_ids):
            return None
        return _deny(f"{cfg.deny_reason} Commenting is restricted to permitted files.")
    if canonical in _DRIVE_WRITE_TOOLS:
        if not target_ids:
            return _deny(f"{cfg.deny_reason} Write call carries no identifiable target file.")
        # ``confidential_files`` is purely a containment declaration: it does NOT
        # by itself grant write access. Writing to a confidential file still
        # requires it to be created this session or in ``write_files`` — so a
        # confidential doc the agent created stays writable (until it reads
        # another confidential file, which the no-write-down check above gates).
        if target_ids <= (cfg.write_ids | created_ids):
            return None
        extra = " or the configured write allowlist" if cfg.write_ids else ""
        return _deny(
            f"{cfg.deny_reason} Writes are restricted to files this agent "
            f"created this session{extra}."
        )
    if canonical.startswith(_DRIVE_OWNED_PREFIXES):
        return _deny(f"{cfg.deny_reason} Tool {raw_tool!r} is not permitted.")
    return None


def _record_created_drive(event: PolicyEvent, prefixes: tuple[str, ...]) -> PolicyResponse | None:
    """
    Record created file IDs from a Drive create tool's result.

    :param event: A ``tool_result`` event.
    :param prefixes: Server prefixes for canonicalization.
    :returns: ALLOW with APPEND ``state_updates`` for new file IDs, or ``None``.
    """
    raw_tool = event.get("target")
    if not isinstance(raw_tool, str):
        return None
    if _canonical_tool_name(raw_tool, prefixes) not in _DRIVE_CREATE_TOOLS:
        return None
    new_ids = _extract_created_ids(_parse_result_payload(event.get("data")), _FILE_RESULT_ID_KEYS)
    if not new_ids:
        return None
    session_state = event.get("session_state") or {}
    updates = _append_updates(
        new_ids, session_state.get(CREATED_FILES_STATE_KEY), CREATED_FILES_STATE_KEY
    )
    if not updates:
        return None
    return {"result": "ALLOW", "state_updates": updates}


def _record_drive_result(
    event: PolicyEvent, prefixes: tuple[str, ...], cfg: _DriveCfg
) -> PolicyResponse | None:
    """
    Handle a Drive ``tool_result``: track created IDs and confidential reads.

    Merges two independent state effects into one response so both survive:

    - Created-file tracking (:func:`_record_created_drive`) — always on.
    - Confidential-read latch (:func:`_read_confidential_update`) — only when
      ``confidential_ids`` is configured; flags the session once it reads a file
      from the confidential compartment.

    :param event: A ``tool_result`` event.
    :param prefixes: Server prefixes for canonicalization.
    :param cfg: Resolved Drive configuration.
    :returns: ALLOW with the combined ``state_updates``, or ``None`` when there
        is nothing to record.
    """
    created = _record_created_drive(event, prefixes)
    updates: list[StateUpdateEntry] = list(created["state_updates"]) if created else []

    if cfg.confidential_ids:
        raw_tool = event.get("target")
        if isinstance(raw_tool, str) and _canonical_tool_name(raw_tool, prefixes) in (
            _DRIVE_READ_TOOLS
        ):
            updates.extend(_read_confidential_update(event, cfg))

    if not updates:
        return None
    return {"result": "ALLOW", "state_updates": updates}


def gdrive_policy(
    *,
    read_all: bool = True,
    read_files: list[str] | None = None,
    allow_create: bool = False,
    write_files: list[str] | None = None,
    comment_files: list[str] | None = None,
    confidential_files: list[str] | None = None,
    write_down_action: str = "DENY",
    tool_prefixes: list[str] | None = None,
    deny_reason: str = "Google Drive operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a Google Drive / Docs / Sheets / Slides access policy callable.

    :param read_all: When ``True`` (default), all recognized read tools are
        allowed. When ``False``, reads are restricted to ``read_files``.
    :param read_files: File IDs / Google URLs readable in restricted mode, e.g.
        ``["1AbC", "https://docs.google.com/document/d/1Def/edit"]``. ``None``
        means none.
    :param allow_create: Whether the agent may create new files. Default ``False``.
    :param write_files: File IDs / URLs writable regardless of creation. ``None``
        means none.
    :param comment_files: File IDs / URLs the agent may comment on (in addition
        to files it created). ``None`` means none.
    :param confidential_files: File IDs / Google URLs that form the confidential
        compartment. When non-empty, this layers Bell-LaPadula's classic
        confidentiality rule — the "*-property", i.e. **no write down** — on top
        of the access rules: once the session reads any file in this set, its
        writes are confined to the same set. A write to any other file (or a
        brand-new one) would move confidential data into a less-protected place
        and is blocked. The compartment is declared explicitly (rather than
        inferred from a per-document label), so it works on any Drive tenant.
        ``None`` / empty means the rule is off and the base access policy behaves
        exactly as before. This enforces only the write-down rule; it does NOT
        restrict reads (the agent must read a confidential file for the
        containment to engage) and does NOT by itself grant write access to the
        listed files — writing to a confidential file still requires it to be
        created this session or in ``write_files``. Note the latch engages only
        on reads that target a confidential file *by id*; content-returning
        reads that don't name a specific file (e.g. ``drive_search``, listing,
        or exports) can surface confidential text without engaging containment.
    :param write_down_action: Verdict on a write-down violation — ``"DENY"``
        (default, hard block) or ``"ASK"`` (human approval). Ignored when
        ``confidential_files`` is empty.
    :param tool_prefixes: Server prefixes to strip when canonicalizing tool
        names. ``None`` uses the standard + Databricks defaults.
    :param deny_reason: Reason text attached to DENY decisions.
    :returns: A one-argument policy callable.
    :raises ValueError: If ``write_down_action`` is not ``"DENY"`` or ``"ASK"``.
    """
    normalized_action = write_down_action.strip().upper()
    if normalized_action not in {"DENY", "ASK"}:
        raise ValueError(
            f"gdrive_policy: write_down_action must be 'DENY' or 'ASK', got {write_down_action!r}"
        )
    cfg = _DriveCfg(
        read_all=read_all,
        read_ids=_normalize_file_refs(read_files),
        write_ids=_normalize_file_refs(write_files),
        comment_ids=_normalize_file_refs(comment_files),
        allow_create=allow_create,
        confidential_ids=_normalize_file_refs(confidential_files),
        write_down_action=normalized_action,
        deny_reason=deny_reason,
    )
    prefixes = _resolve_prefixes(tool_prefixes)

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Route a Drive event: track created files / confidential reads, gate calls.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        phase = event.get("type")
        if phase == "tool_result":
            return _record_drive_result(event, prefixes, cfg)
        if phase == "tool_call":
            return _decide_drive_tool_call(event, prefixes, cfg)
        return None

    return _evaluate


# ── Gmail ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _GmailCfg:
    """
    Resolved Gmail-policy configuration passed to :func:`_decide_gmail_tool_call`.

    :param allow_read: Whether the agent may read mail.
    :param allow_send: Whether the agent may send mail.
    :param allow_drafts: Whether the agent may create / edit its own drafts.
    :param allow_modify: Whether the agent may modify messages / threads.
    :param deny_reason: Reason prefix for DENY decisions.
    """

    allow_read: bool
    allow_send: bool
    allow_drafts: bool
    allow_modify: bool
    deny_reason: str


def _decide_gmail_tool_call(
    event: PolicyEvent, prefixes: tuple[str, ...], cfg: _GmailCfg
) -> PolicyResponse | None:
    """
    Apply the Gmail access rules to one ``tool_call`` event.

    :param event: The ``tool_call`` policy event.
    :param prefixes: Server prefixes to strip for canonicalization.
    :param cfg: Resolved Gmail configuration.
    :returns: A :class:`PolicyResponse` decision, or ``None`` to abstain.
    """
    parsed = _parse_tool_call(event, prefixes)
    if parsed is None:
        return None
    canonical, raw_tool, args = parsed.canonical, parsed.raw_tool, parsed.args

    if canonical in _GMAIL_READ_TOOLS:
        return (
            None if cfg.allow_read else _deny(f"{cfg.deny_reason} Reading mail is not permitted.")
        )
    if canonical in _GMAIL_SEND_TOOLS:
        return (
            None
            if cfg.allow_send
            else _deny(f"{cfg.deny_reason} Sending mail is not permitted (draft-only mode).")
        )
    if canonical in _GMAIL_DRAFT_CREATE_TOOLS:
        return (
            None
            if cfg.allow_drafts
            else _deny(f"{cfg.deny_reason} Creating drafts is not permitted.")
        )
    if canonical in _GMAIL_DRAFT_WRITE_TOOLS:
        if not cfg.allow_drafts:
            return _deny(f"{cfg.deny_reason} Editing drafts is not permitted.")
        created = _created_ids_from_state(
            event.get("session_state") or {}, CREATED_DRAFTS_STATE_KEY
        )
        target_ids = _extract_ids_from_args(args, _GMAIL_DRAFT_ID_ARG_KEYS)
        if target_ids and target_ids <= created:
            return None
        return _deny(
            f"{cfg.deny_reason} Draft edits are restricted to drafts created this session."
        )
    if canonical in _GMAIL_MODIFY_TOOLS:
        return (
            None
            if cfg.allow_modify
            else _deny(f"{cfg.deny_reason} Modifying messages / threads is not permitted.")
        )
    if canonical.startswith(_GMAIL_OWNED_PREFIXES):
        return _deny(f"{cfg.deny_reason} Tool {raw_tool!r} is not permitted.")
    return None


def _record_created_draft(event: PolicyEvent, prefixes: tuple[str, ...]) -> PolicyResponse | None:
    """
    Record created draft IDs from a Gmail draft-create tool's result.

    :param event: A ``tool_result`` event.
    :param prefixes: Server prefixes for canonicalization.
    :returns: ALLOW with APPEND ``state_updates`` for new draft IDs, or ``None``.
    """
    raw_tool = event.get("target")
    if not isinstance(raw_tool, str):
        return None
    if _canonical_tool_name(raw_tool, prefixes) not in _GMAIL_DRAFT_CREATE_TOOLS:
        return None
    new_ids = _extract_created_ids(
        _parse_result_payload(event.get("data")), _GMAIL_DRAFT_RESULT_ID_KEYS
    )
    if not new_ids:
        return None
    session_state = event.get("session_state") or {}
    updates = _append_updates(
        new_ids, session_state.get(CREATED_DRAFTS_STATE_KEY), CREATED_DRAFTS_STATE_KEY
    )
    if not updates:
        return None
    return {"result": "ALLOW", "state_updates": updates}


def gmail_policy(
    *,
    allow_read: bool = True,
    allow_send: bool = False,
    allow_drafts: bool = True,
    allow_modify: bool = False,
    tool_prefixes: list[str] | None = None,
    deny_reason: str = "Gmail operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a Gmail access policy callable.

    :param allow_read: Whether the agent may read mail. Default ``True``.
    :param allow_send: Whether the agent may send mail. Default ``False`` — the
        draft-but-don't-send guardrail.
    :param allow_drafts: Whether the agent may create drafts (and edit / delete
        the ones it created). Default ``True``.
    :param allow_modify: Whether the agent may modify messages / threads (labels,
        move, trash). Default ``False``.
    :param tool_prefixes: Server prefixes to strip. ``None`` uses the defaults.
    :param deny_reason: Reason text attached to DENY decisions.
    :returns: A one-argument policy callable.
    """
    cfg = _GmailCfg(
        allow_read=allow_read,
        allow_send=allow_send,
        allow_drafts=allow_drafts,
        allow_modify=allow_modify,
        deny_reason=deny_reason,
    )
    prefixes = _resolve_prefixes(tool_prefixes)

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Route a Gmail event: record created drafts on results, gate tool calls.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        phase = event.get("type")
        if phase == "tool_result":
            return _record_created_draft(event, prefixes)
        if phase == "tool_call":
            return _decide_gmail_tool_call(event, prefixes, cfg)
        return None

    return _evaluate


# ── Google Calendar ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _CalCfg:
    """
    Resolved Calendar-policy config passed to :func:`_decide_calendar_tool_call`.

    :param allow_read: Whether the agent may read calendars / events.
    :param allow_create_events: Whether the agent may create events / calendars.
    :param allow_modify_events: Whether the agent may update / delete events.
    :param deny_reason: Reason prefix for DENY decisions.
    """

    allow_read: bool
    allow_create_events: bool
    allow_modify_events: bool
    deny_reason: str


def _decide_calendar_tool_call(
    event: PolicyEvent, prefixes: tuple[str, ...], cfg: _CalCfg
) -> PolicyResponse | None:
    """
    Apply the Calendar access rules to one ``tool_call`` event.

    :param event: The ``tool_call`` policy event.
    :param prefixes: Server prefixes to strip for canonicalization.
    :param cfg: Resolved Calendar configuration.
    :returns: A :class:`PolicyResponse` decision, or ``None`` to abstain.
    """
    parsed = _parse_tool_call(event, prefixes)
    if parsed is None:
        return None
    canonical, raw_tool = parsed.canonical, parsed.raw_tool

    if canonical in _CAL_READ_TOOLS:
        return (
            None
            if cfg.allow_read
            else _deny(f"{cfg.deny_reason} Reading the calendar is not permitted.")
        )
    if canonical in _CAL_CREATE_TOOLS:
        return (
            None
            if cfg.allow_create_events
            else _deny(f"{cfg.deny_reason} Creating events / calendars is not permitted.")
        )
    if canonical in _CAL_MODIFY_TOOLS:
        return (
            None
            if cfg.allow_modify_events
            else _deny(f"{cfg.deny_reason} Modifying or deleting events is not permitted.")
        )
    if canonical.startswith(_CAL_OWNED_PREFIXES):
        return _deny(f"{cfg.deny_reason} Tool {raw_tool!r} is not permitted.")
    return None


def gcalendar_policy(
    *,
    allow_read: bool = True,
    allow_create_events: bool = False,
    allow_modify_events: bool = False,
    tool_prefixes: list[str] | None = None,
    deny_reason: str = "Google Calendar operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a Google Calendar access policy callable.

    :param allow_read: Whether the agent may read calendars / events. Default
        ``True``.
    :param allow_create_events: Whether the agent may create events / calendars.
        Default ``False``.
    :param allow_modify_events: Whether the agent may update / delete events.
        Default ``False``.
    :param tool_prefixes: Server prefixes to strip. ``None`` uses the defaults.
    :param deny_reason: Reason text attached to DENY decisions.
    :returns: A one-argument policy callable.
    """
    cfg = _CalCfg(
        allow_read=allow_read,
        allow_create_events=allow_create_events,
        allow_modify_events=allow_modify_events,
        deny_reason=deny_reason,
    )
    prefixes = _resolve_prefixes(tool_prefixes)

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Gate Calendar tool calls; abstain on other phases and non-Calendar tools.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        if event.get("type") != "tool_call":
            return None
        return _decide_calendar_tool_call(event, prefixes, cfg)

    return _evaluate


# ── Registry (one list, three entries — one per policy) ───────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.google.gdrive_policy",
        "kind": "factory",
        "name": "Google Drive / Docs / Sheets Access",
        "description": (
            "Controls access to Google Drive files, Docs, Sheets, and Slides through "
            "any Google MCP server. Restricts reads to an allowlist and restricts "
            "writes/comments to files the agent created this session plus explicitly "
            "allowed files. Optionally enforces Bell-LaPadula's 'no write-down' rule "
            "via a confidential-file compartment: once the session reads a confidential "
            "file, its writes are confined to that set so classified data can't leak "
            "into a less-protected file."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "read_all": {
                    "type": "boolean",
                    "description": "Allow all reads. When false, only read_files are readable.",
                    "default": True,
                },
                "read_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File IDs or Google URLs readable when read_all is false.",
                },
                "allow_create": {
                    "type": "boolean",
                    "description": "Allow creating new files (docs, sheets, slides, Drive files).",
                    "default": False,
                },
                "write_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File IDs or URLs writable regardless of creation.",
                },
                "comment_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File IDs or URLs the agent may comment on.",
                },
                "confidential_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "File IDs or Google URLs forming the confidential "
                    "compartment. When set, Bell-LaPadula 'no write-down' engages: "
                    "after the session reads one of these, writes are confined to the "
                    "set. Empty (default) disables the rule.",
                },
                "write_down_action": {
                    "type": "string",
                    "enum": ["DENY", "ASK"],
                    "description": "Verdict on a write-down violation. "
                    "Ignored unless confidential_files is set.",
                    "default": "DENY",
                },
                "tool_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Server tool-name prefixes to strip "
                    "(default: mcp__google__, google__).",
                },
            },
        },
    },
    {
        "handler": "omnigent.policies.builtins.google.gmail_policy",
        "kind": "factory",
        "name": "Gmail Access",
        "description": (
            "Controls Gmail access through any Google MCP server. Defaults to allowing "
            "reads and drafts but blocking sending and message modification; draft edits "
            "are restricted to drafts the agent created this session."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "allow_read": {
                    "type": "boolean",
                    "description": "Allow reading mail (search / list / get).",
                    "default": True,
                },
                "allow_send": {
                    "type": "boolean",
                    "description": "Allow sending mail. Off by default (draft-only).",
                    "default": False,
                },
                "allow_drafts": {
                    "type": "boolean",
                    "description": "Allow creating drafts and editing the agent's own drafts.",
                    "default": True,
                },
                "allow_modify": {
                    "type": "boolean",
                    "description": "Allow modifying messages / threads (labels, move, trash).",
                    "default": False,
                },
                "tool_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Server tool-name prefixes to strip "
                    "(default: mcp__google__, google__).",
                },
            },
        },
    },
    {
        "handler": "omnigent.policies.builtins.google.gcalendar_policy",
        "kind": "factory",
        "name": "Google Calendar Access",
        "description": (
            "Controls Google Calendar access through any Google MCP server. Defaults to "
            "read-only — allows reading events but blocks creating, updating, and "
            "deleting them."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "allow_read": {
                    "type": "boolean",
                    "description": "Allow reading calendars / events / free-busy.",
                    "default": True,
                },
                "allow_create_events": {
                    "type": "boolean",
                    "description": "Allow creating events / calendars.",
                    "default": False,
                },
                "allow_modify_events": {
                    "type": "boolean",
                    "description": "Allow updating / deleting events.",
                    "default": False,
                },
                "tool_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Server tool-name prefixes to strip "
                    "(default: mcp__google__, google__).",
                },
            },
        },
    },
]
