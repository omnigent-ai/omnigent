"""Read and normalize local coding-harness transcripts."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from hashlib import sha256
from itertools import chain
from pathlib import Path
from typing import BinaryIO, get_args

from omnigent.entities import ErrorData, MessageData, NewConversationItem, parse_item_data
from omnigent.entities.conversation import synthesize_conversation_title
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    _dedupe_compact_noop_echo,
    _transcript_items_from_entry,
    read_transcript_items_from_offset,
)
from omnigent.harnesses.codex_native.bridge import bridge_root, read_bridge_state
from omnigent.harnesses.codex_native.main import _CODEX_THREAD_ID_RE
from omnigent.harnesses.kimi_native.credentials import resolve_user_kimi_home
from omnigent.harnesses.kimi_native.forwarder import (
    KimiWireItem,
    _row_to_item,
    read_kimi_wire_items,
    workdirs_for_kimi_sessions,
)
from omnigent.harnesses.kiro_native.session_forwarder import (
    kiro_cli_sessions_dir,
    parse_kiro_jsonl_line,
)
from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeCliNotFoundError,
    find_opencode_cli,
)
from omnigent.harnesses.opencode_native.forwarder import opencode_tool_output_text
from omnigent.session_import.models import (
    ImportSource,
    LocalSessionImport,
    SessionImportEmptyError,
    SessionImportNotFoundError,
)

_logger = logging.getLogger(__name__)

_PI_IMPORT_SESSION_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_OPENCODE_IMPORT_SESSION_ID_RE = re.compile(r"ses_[A-Za-z0-9_-]+")
_MAX_EXTERNAL_SESSION_ID_LENGTH = 128
_MAX_RESPONSE_ID_LENGTH = 64
_OPENCODE_COMMAND_TIMEOUT_SECONDS = 120

# Transcript byte size past which an import is trimmed to the last compaction
# boundary instead of the full history. Below it the whole transcript imports
# (cheap, and the full record is useful to browse); above it the file has almost
# certainly been compacted at least once, so importing every pre-compaction
# record replays megabytes the live agent no longer sees. Shared by the Claude
# (``isCompactSummary``) and Codex (``compacted`` record) paths — see
# docs/session-compaction.md.
_IMPORT_COMPACT_TRIM_BYTES = 2 * 1024 * 1024

# An imported Codex compaction's last_item_id until the import route points it at the
# item stored before it.
_CODEX_COMPACTION_PLACEHOLDER_ITEM_ID = "codex:compaction"
_CODEX_COMPACTION_FALLBACK_SUMMARY = "[Codex compaction — context was compacted in Codex]"

# Most items one imported session keeps, counting the notice that says the rest
# was left out: the server's own ``_MAX_IMPORT_ITEMS``. A longer history keeps
# its first items instead of failing.
IMPORT_MAX_ITEMS = 100_000
IMPORT_TRIMMED_NOTICE_CODE = "import_history_trimmed"
_IMPORT_TRIMMED_NOTICE_RESPONSE_ID = "import:trimmed"

# A Claude or Codex transcript larger than this is parsed only until its kept
# records reach this many bytes (or the item cap, whichever comes first), so
# load time, host memory and the upload stop growing with the file. Sized from
# the import cost measured against a remote conversation store (~1 ms per item
# plus ~0.17 s per MB; 100,000 small items took 109-129 s): the worst case, a
# full item cap within the budget, is ~100 s + 134 MB x 0.17 s/MB ~ 123 s
# (~140 s with the overhead seen in those runs), about half of both the stream's
# 270 s budget and the CLI's import timeout
# (``omnigent.cli._IMPORT_REQUEST_TIMEOUT_S``). Items are about the size of
# their records.
IMPORT_READ_BUDGET_BYTES = 128 * 1024 * 1024


def _trimmed_history_notice(dropped: int | None, kept: int) -> NewConversationItem:
    """The visible last item of a trimmed import, saying what was left out.

    ``dropped`` is ``None`` when reading stopped early, so the rest was never
    counted and the notice doesn't claim a number.
    """
    imported = "item was" if kept == 1 else "items were"
    if dropped is None:
        left_out = "later history was left out"
    else:
        left_out = f"the {dropped:,} later {'item was' if dropped == 1 else 'items were'} left out"
    return NewConversationItem(
        type="error",
        response_id=_IMPORT_TRIMMED_NOTICE_RESPONSE_ID,
        data=ErrorData(
            source="execution",
            code=IMPORT_TRIMMED_NOTICE_CODE,
            level="info",
            title="Later history not imported",
            message=(
                f"This session was too long to import in full, so only its first "
                f"{kept:,} {imported} imported; {left_out}."
            ),
        ),
    )


def cap_import_items(
    items: Sequence[NewConversationItem],
    *,
    max_items: int = IMPORT_MAX_ITEMS,
) -> tuple[tuple[NewConversationItem, ...], int]:
    """Keep the first history that fits in ``max_items``.

    Returns ``(items, dropped)``. Under the cap the items are returned unchanged
    with ``dropped == 0``. Over it, the first ``max_items - 1`` items are kept
    and a notice item naming the number of dropped items is appended.
    """
    if len(items) <= max_items:
        return tuple(items), 0
    kept = tuple(items[: max_items - 1])
    dropped = len(items) - len(kept)
    return (*kept, _trimmed_history_notice(dropped, len(kept))), dropped


def _cap_session_items(session: LocalSessionImport) -> LocalSessionImport:
    """Apply :func:`cap_import_items`.

    A session from a budgeted read (:func:`_read_claude_head`,
    :func:`_read_codex_head`) is already within the cap and passes through.
    """
    items, dropped = cap_import_items(session.items, max_items=IMPORT_MAX_ITEMS)
    if dropped == 0:
        return session
    return dataclasses.replace(session, items=items, trimmed_item_count=dropped)


def _budgeted_session(session: LocalSessionImport, *, stopped_early: bool) -> LocalSessionImport:
    """Close a budgeted read: the notice goes last when reading stopped early."""
    if not stopped_early:
        return session
    # Loaders without their own item stop (Qwen, Pi, Kiro, Kimi) can read more
    # than the cap within the byte budget; keep the first ones, as Claude/Codex do.
    kept = session.items[: IMPORT_MAX_ITEMS - 1]
    notice = _trimmed_history_notice(None, len(kept))
    return dataclasses.replace(session, items=(*kept, notice), later_history_omitted=True)


def _size_over_read_budget(path: Path) -> int | None:
    """A transcript's size when it is read under the byte budget, else ``None``.

    An unreadable size reads whole, which reports the fault as it always has.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return None
    return size if size > IMPORT_READ_BUDGET_BYTES else None


def _next_budgeted_line(handle: BinaryIO, *, used: int, have_items: bool) -> bytes | None:
    """The next raw JSONL line of a budgeted read, or ``None`` to stop before it.

    Until the first importable record, a line is read whole however long (so a
    giant first record still imports). After that, a line that would take the
    read past :data:`IMPORT_READ_BUDGET_BYTES` ends it, read only far enough to
    know.
    """
    if not have_items:
        return handle.readline()
    remaining = max(0, IMPORT_READ_BUDGET_BYTES - used)
    raw = handle.readline(remaining + 1)
    return None if len(raw) > remaining else raw


def _read_transcript_lines(path: Path) -> tuple[list[str], bool]:
    """A JSONL transcript's lines, read no further than the byte budget.

    Returns ``(lines, stopped_early)``. A file within
    :data:`IMPORT_READ_BUDGET_BYTES` is read whole; a larger one stops before
    the line that would pass the budget (the first non-empty line is always
    read whole), so time and memory don't grow with the file. Invalid UTF-8 is
    replaced rather than failing the session.
    """
    if _size_over_read_budget(path) is None:
        with path.open(encoding="utf-8", errors="replace") as handle:
            return list(handle), False
    lines: list[str] = []
    used = 0
    have_record = False
    with path.open("rb") as handle:
        while True:
            raw = _next_budgeted_line(handle, used=used, have_items=have_record)
            if raw is None:
                return lines, True
            if not raw:
                return lines, False
            used += len(raw)
            have_record = have_record or bool(raw.strip())
            lines.append(raw.decode("utf-8", errors="replace"))


def _exceeds_compaction_trim_size(path: Path) -> bool:
    """Whether a transcript is large enough to trim to its last compaction.

    An unreadable size falls back to ``False`` — import the whole transcript
    rather than drop history on a failed ``stat``.
    """
    try:
        return path.stat().st_size > _IMPORT_COMPACT_TRIM_BYTES
    except OSError:
        return False


def _str_in(value: object, choices: frozenset[str] | set[str]) -> bool:
    """``value in choices`` for a JSON field that may hold a list or dict.

    A malformed record's list/dict field would raise ``unhashable type`` and
    fail the whole session; it only skips that record instead.
    """
    return isinstance(value, str) and value in choices


def _bounded_response_id(response_id: str) -> str:
    """Keep short native ids readable and hash long ids without collisions."""
    if len(response_id) <= _MAX_RESPONSE_ID_LENGTH:
        return response_id
    harness, separator, _ = response_id.partition(":")
    prefix = f"{harness}:sha256:" if separator else "sha256:"
    digest_length = _MAX_RESPONSE_ID_LENGTH - len(prefix)
    return prefix + sha256(response_id.encode()).hexdigest()[:digest_length]


def _find_transcript(root: Path, session_id: str) -> Path | None:
    """Return the newest parent JSONL transcript whose stem matches the id."""
    matches = [
        path
        for path in root.rglob("*.jsonl")
        if path.stem == session_id and "subagents" not in path.parts and path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def _recent_unique_sessions_with_recency(
    candidates: list[tuple[Path, str]],
    *,
    limit: int,
) -> list[tuple[str, float]]:
    """Return ``(session_id, recency)`` pairs, newest transcript first.

    ``recency`` is the newest mtime seen for that id, kept so callers can merge
    sessions across harnesses by a single global recency order.
    """
    newest_by_id: dict[str, float] = {}
    for path, session_id in candidates:
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        newest_by_id[session_id] = max(newest_by_id.get(session_id, 0), modified_at)
    ordered = sorted(
        newest_by_id,
        key=lambda session_id: (newest_by_id[session_id], session_id),
        reverse=True,
    )
    return [(session_id, newest_by_id[session_id]) for session_id in ordered[:limit]]


def _recent_unique_session_ids(
    candidates: list[tuple[Path, str]],
    *,
    limit: int,
) -> tuple[str, ...]:
    """Return unique session ids ordered from newest transcript to oldest."""
    return tuple(sid for sid, _ in _recent_unique_sessions_with_recency(candidates, limit=limit))


def _pi_session_id_from_path(path: Path) -> str | None:
    """Read a safe native session id from a Pi transcript header."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            header = json.loads(handle.readline())
    except (OSError, ValueError):
        return None
    session_id = header.get("id") if isinstance(header, dict) else None
    if not isinstance(session_id, str) or not _is_safe_pi_import_session_id(session_id):
        return None
    return session_id


def _is_safe_pi_import_session_id(session_id: str) -> bool:
    """Match Pi's safe syntax within the import API's identity limit."""
    return (
        len(session_id) <= _MAX_EXTERNAL_SESSION_ID_LENGTH
        and _PI_IMPORT_SESSION_ID_RE.fullmatch(session_id) is not None
    )


def _is_safe_opencode_import_session_id(session_id: str) -> bool:
    """Accept native OpenCode ids without permitting CLI option injection."""
    return (
        len(session_id) <= _MAX_EXTERNAL_SESSION_ID_LENGTH
        and _OPENCODE_IMPORT_SESSION_ID_RE.fullmatch(session_id) is not None
    )


def _run_opencode_json(
    *arguments: str,
    opencode_path: str | None = None,
    empty_ok: bool = False,
) -> object:
    """Run one public OpenCode JSON command and decode stdout.

    With no sessions, ``session list`` prints nothing (exit 0) rather than
    ``[]``; ``empty_ok`` treats that empty stdout as an empty result instead of
    an "invalid JSON" error.
    """
    try:
        cli = find_opencode_cli(opencode_path)
    except OpenCodeCliNotFoundError as exc:
        raise SessionImportNotFoundError(str(exc)) from exc
    try:
        completed = subprocess.run(
            [cli, *arguments],
            check=False,
            capture_output=True,
            text=True,
            timeout=_OPENCODE_COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SessionImportNotFoundError(f"OpenCode export could not run: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise SessionImportNotFoundError(f"OpenCode command failed{suffix}")
    if empty_ok and not completed.stdout.strip():
        return []
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise SessionImportNotFoundError("OpenCode returned invalid JSON") from exc


def _qwen_session_locator(path: Path) -> str:
    """Qualify a Qwen id by project while staying within API limits."""
    project = path.parent.parent.name
    session_id = path.stem
    locator = f"{project}:{session_id}"
    if len(locator) <= _MAX_EXTERNAL_SESSION_ID_LENGTH:
        return locator
    project_digest = sha256(project.encode()).hexdigest()[:16]
    locator = f"{project_digest}:{session_id}"
    if len(locator) <= _MAX_EXTERNAL_SESSION_ID_LENGTH:
        return locator
    return f"{project_digest}:{sha256(session_id.encode()).hexdigest()}"


# Codex records the session's origin in ``session_meta.source``. Its own resume
# picker lists only interactive sources (Cli, VSCode); ``exec`` runs, ``mcp``
# sessions, and sub-agent / internal threads are automation the user never
# opened interactively. Match that so recent-import doesn't flood the sidebar
# with headless ``codex exec`` runs, whose first message is an injected
# instruction, which is what produced the "many instruction-like titles" report.
_CODEX_INTERACTIVE_SOURCES = frozenset({"cli", "vscode"})


def _codex_rollout_source(path: Path) -> object | None:
    """Return a Codex rollout's recorded ``session_meta.source``, if present.

    ``session_meta`` is the first record in a rollout, so only the first line is
    read. Returns the raw value (a string like ``"cli"`` / ``"exec"`` for
    top-level sources, or a dict for sub-agent / internal ones), or ``None`` when
    the file is unreadable or predates the ``source`` field.
    """
    try:
        with path.open("rb") as handle:
            # Bytes, not text: a text read decodes ahead of the first line, so
            # one bad byte later in the file used to raise here and fail the
            # whole Codex listing.
            first = handle.readline().decode("utf-8", errors="replace")
    except OSError:
        return None
    try:
        record = json.loads(first)
    except ValueError:
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    payload = record.get("payload")
    return payload.get("source") if isinstance(payload, dict) else None


def _codex_source_is_interactive(source: object) -> bool:
    """Whether a Codex session source is one the interactive picker shows.

    Mirrors Codex's resume picker (interactive = ``Cli`` / ``VSCode``). A missing
    source (very old rollouts predating the field) defaults to interactive so
    genuine history isn't dropped; any recorded non-interactive value (``exec``,
    ``mcp``, or a sub-agent / internal object) is excluded.
    """
    if source is None:
        return True
    return isinstance(source, str) and source.lower() in _CODEX_INTERACTIVE_SOURCES


def _unique_paths(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Keep the first occurrence of each configured storage path."""
    unique: list[Path] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return tuple(unique)


def _codex_native_storage_homes() -> tuple[Path, ...]:
    """Return private Codex homes under the producer's bridge root."""
    root = bridge_root()
    try:
        bridge_dirs = tuple(root.iterdir())
    except OSError:
        return ()
    homes: list[Path] = []
    for bridge_dir in bridge_dirs:
        try:
            if not bridge_dir.is_dir() or bridge_dir.is_symlink():
                continue
            codex_home = bridge_dir / "codex-home"
            if codex_home.is_dir() and not codex_home.is_symlink():
                homes.append(codex_home)
        except OSError:
            continue
    return tuple(homes)


def _codex_storage_homes(codex_home: Path | None = None) -> tuple[Path, ...]:
    """Return configured, normal, and native private Codex homes."""
    if codex_home is not None:
        return (codex_home,)
    configured = os.environ.get("CODEX_HOME")
    normal = Path.home() / ".codex"
    configured_home = Path(configured).expanduser() if configured else normal
    return _unique_paths((configured_home, normal, *_codex_native_storage_homes()))


def _codex_rollout_paths(home: Path) -> list[Path]:
    """Return active and archived Codex rollout files in one home."""
    rollouts: list[Path] = []
    sessions = home / "sessions"
    archived_sessions = home / "archived_sessions"
    if sessions.is_dir():
        rollouts.extend(path for path in sessions.glob("**/rollout-*.jsonl") if path.is_file())
    if archived_sessions.is_dir():
        rollouts.extend(
            path for path in archived_sessions.glob("rollout-*.jsonl") if path.is_file()
        )
    return rollouts


def _codex_rollout_ids(path: Path) -> tuple[str, str] | None:
    """``(thread id, rollout id)`` from a rollout filename.

    A reverted thread's new rollout is ``rollout-<ts>-<thread>_<rollout>.jsonl``;
    elsewhere the two ids are equal.
    """
    stem = path.stem
    rollout_id = stem[-36:]
    if not _CODEX_THREAD_ID_RE.fullmatch(rollout_id):
        return None
    thread_id = stem[-73:-37]
    if stem[-37:-36] == "_" and _CODEX_THREAD_ID_RE.fullmatch(thread_id):
        return thread_id, rollout_id
    return rollout_id, rollout_id


def _codex_rollout_session_id(path: Path) -> str | None:
    """Return the safe thread id encoded in a rollout filename."""
    ids = _codex_rollout_ids(path)
    return ids[0] if ids is not None else None


def _codex_rollout_candidates(
    homes: Sequence[Path],
) -> list[tuple[Path, Path, str]]:
    """Return ``(rollout, home, thread_id)`` candidates across Codex homes."""
    candidates: list[tuple[Path, Path, str]] = []
    for home in homes:
        for path in _codex_rollout_paths(home):
            session_id = _codex_rollout_session_id(path)
            if session_id is not None:
                candidates.append((path, home, session_id))
    return candidates


def _recent_local_sessions_with_recency(
    source: ImportSource,
    *,
    limit: int,
) -> list[tuple[str, float]]:
    """List recent ``(session_id, recency)`` pairs for one local harness, newest first."""
    if source == "claude":
        configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".claude"
        root = home / "projects"
        candidates = [
            (path, path.stem)
            for path in root.rglob("*.jsonl")
            if "subagents" not in path.parts and path.is_file()
        ]
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    if source == "qwen":
        configured_home = os.environ.get("QWEN_HOME")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".qwen"
        paths = [path for path in (home / "projects").glob("*/chats/*.jsonl") if path.is_file()]
        candidates = [(path, _qwen_session_locator(path)) for path in paths]
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    if source == "kiro":
        root = kiro_cli_sessions_dir()
        candidates = [
            (path, path.stem)
            for path in root.glob("*.jsonl")
            if path.is_file() and path.with_suffix(".json").is_file()
        ]
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    if source == "opencode":
        payload = _run_opencode_json(
            "session",
            "list",
            "--format",
            "json",
            "--pure",
            empty_ok=True,
        )
        if not isinstance(payload, list):
            raise SessionImportNotFoundError("OpenCode returned an invalid session list")
        updated_by_id: dict[str, int | float] = {}
        for entry in payload:
            if not isinstance(entry, dict) or isinstance(entry.get("parentID"), str):
                continue
            session_id = entry.get("id")
            updated = entry.get("updated")
            if not isinstance(session_id, str) or not _is_safe_opencode_import_session_id(
                session_id
            ):
                continue
            timestamp = updated if isinstance(updated, (int, float)) else 0
            updated_by_id[session_id] = max(updated_by_id.get(session_id, 0), timestamp)
        ordered = sorted(
            updated_by_id,
            key=lambda session_id: (updated_by_id[session_id], session_id),
            reverse=True,
        )
        return [(session_id, float(updated_by_id[session_id])) for session_id in ordered[:limit]]

    if source == "pi":
        configured_home = os.environ.get("PI_CODING_AGENT_DIR")
        home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".pi" / "agent"
        )
        # Pi stores ids in the header, so discovery intentionally reads one line per file.
        candidates = [
            (path, session_id)
            for path in (home / "sessions").rglob("*.jsonl")
            if path.is_file() and (session_id := _pi_session_id_from_path(path)) is not None
        ]
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    if source == "kimi":
        home = resolve_user_kimi_home()
        candidates = [
            (path, path.parent.parent.parent.name)
            for path in (home / "sessions").glob("*/session_*/agents/main/wire.jsonl")
            if path.is_file()
        ]
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    if source == "codex":
        candidates = []
        for path, _, session_id in _codex_rollout_candidates(_codex_storage_homes()):
            # Read the rollout's source (one line) and skip non-interactive
            # runs (exec / mcp / sub-agent / internal) that Codex itself hides.
            if not _codex_source_is_interactive(_codex_rollout_source(path)):
                continue
            candidates.append((path, session_id))
        return _recent_unique_sessions_with_recency(candidates, limit=limit)

    raise ValueError(f"Unsupported import source: {source}")


def list_recent_local_session_ids(
    source: ImportSource,
    *,
    limit: int,
) -> tuple[str, ...]:
    """List recent parent session ids for one local harness, newest first."""
    return tuple(sid for sid, _ in _recent_local_sessions_with_recency(source, limit=limit))


def _normalize_recency(recency: float) -> float:
    """Fold millisecond timestamps to seconds so harnesses compare on one scale.

    File-based harnesses use mtime (Unix seconds ~1.7e9); OpenCode reports
    ``updated`` in epoch millis (~1.7e12). Without this a millis timestamp would
    always outrank a seconds one in a cross-harness merge.
    """
    return recency / 1000.0 if recency > 1e12 else recency


class RecentLocalSessions(list[tuple[ImportSource, str]]):
    """``(source, session_id)`` pairs, plus the harnesses whose listing failed.

    A list subclass so callers (and test doubles returning a plain list) keep
    working; read the extra field with ``getattr(result, "skipped_harnesses", ())``.
    """

    skipped_harnesses: tuple[tuple[ImportSource, str], ...] = ()


def list_recent_sessions_across_harnesses(*, limit: int) -> RecentLocalSessions:
    """Return the ``limit`` most recent sessions across every harness, newest first.

    Unlike calling :func:`list_recent_local_session_ids` per harness (which would
    yield ``limit`` *each*), this merges all harnesses into one global recency
    order and keeps the top ``limit`` — so "last N" means N total. A harness with
    no history or an unavailable CLI is skipped quietly; one whose listing
    fails is skipped too and named in ``skipped_harnesses`` as ``(source,
    exception class name)`` so the caller can say so.
    """
    scored: list[tuple[float, ImportSource, str]] = []
    skipped_harnesses: list[tuple[ImportSource, str]] = []
    for source in get_args(ImportSource):
        try:
            recent = _recent_local_sessions_with_recency(source, limit=limit)
        except SessionImportNotFoundError:
            continue
        except Exception as exc:  # noqa: BLE001 — one harness must not hide the rest
            # One harness's broken reader (a missing optional module, a corrupt
            # index) must not hide every other harness's sessions.
            _logger.warning("Skipping %s sessions: listing them failed", source, exc_info=True)
            skipped_harnesses.append((source, type(exc).__name__))
            continue
        scored.extend((_normalize_recency(recency), source, sid) for sid, recency in recent)
    scored.sort(key=lambda entry: (entry[0], entry[2]), reverse=True)
    result = RecentLocalSessions((source, sid) for _, source, sid in scored[:limit])
    result.skipped_harnesses = tuple(skipped_harnesses)
    return result


def _claude_workspace(transcript_path: Path) -> str | None:
    """Read the first usable cwd recorded in a Claude transcript."""
    with transcript_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            cwd_value = record.get("cwd") if isinstance(record, dict) else None
            if isinstance(cwd_value, str):
                cwd = cwd_value.strip()
                if cwd:
                    return cwd
    return None


def _claude_native_title(transcript_path: Path) -> str | None:
    """Return Claude Code's own session title, custom name over AI-generated.

    Claude appends ``custom-title`` (user rename) and ``ai-title`` (generated)
    lines to the JSONL as they change; the last of each wins. A user's rename
    beats the AI title.
    """
    custom: str | None = None
    ai: str | None = None
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "-title" not in line:  # cheap prefilter; the type is custom-title / ai-title
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "custom-title":
                    value = record.get("customTitle")
                    if isinstance(value, str) and value.strip():
                        custom = value.strip()
                elif record.get("type") == "ai-title":
                    value = record.get("aiTitle")
                    if isinstance(value, str) and value.strip():
                        ai = value.strip()
    except OSError:
        return None
    return custom or ai


def _items_from_last_compaction(
    items: Sequence[ClaudeTranscriptItem],
) -> Sequence[ClaudeTranscriptItem]:
    """Slice ``items`` from the last compaction summary onward.

    Claude flags the continuation summary it writes after compacting its own
    context with ``is_compact_summary`` — the durable compaction boundary (see
    :mod:`omnigent.harnesses.claude_native.bridge`). The live agent resumes from
    that summary, not the full transcript, so keeping the summary and everything
    after it mirrors the agent's working context. Returns ``items`` unchanged
    when the transcript was never compacted.
    """
    last = None
    for index, item in enumerate(items):
        if item.is_compact_summary:
            last = index
    return items if last is None else items[last:]


def _claude_import_item_data(item: ClaudeTranscriptItem) -> dict[str, object]:
    """Return an item's data, flagging a compaction summary as meta.

    Claude writes a continuation summary after compacting its own context; a
    large transcript is trimmed to start there, so the summary would otherwise
    become the sidebar title ("This session is being continued…"). It is durable
    context replayed to the agent, not a user turn; flag it ``is_meta`` so the
    title falls through to a real user message (matching Pi's branch summaries).
    """
    data = item.data
    if item.is_compact_summary and isinstance(data, dict) and not data.get("is_meta"):
        return {**data, "is_meta": True}
    if item.agent_message_candidate and not data.get("user_authored"):
        return {**data, "is_meta": True}
    return data


_CLAUDE_COMPACT_SUMMARY_MARKERS = (b'"isCompactSummary":true', b'"isCompactSummary": true')


def _last_claude_compaction_offset(transcript_path: Path) -> int:
    """Byte offset of the last compaction summary record, ``0`` when none.

    The same boundary :func:`_items_from_last_compaction` finds, without a full
    parse: only lines carrying the flag are decoded.
    """
    last = 0
    offset = 0
    with transcript_path.open("rb") as handle:
        for raw in handle:
            if raw.endswith(b"\n") and any(m in raw for m in _CLAUDE_COMPACT_SUMMARY_MARKERS):
                try:
                    entry = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    entry = None
                if (
                    isinstance(entry, dict)
                    and entry.get("type") == "user"
                    and entry.get("isCompactSummary") is True
                    and entry.get("isSidechain") is not True
                    and entry.get("isMeta") is not True
                ):
                    last = offset
            offset += len(raw)
    return last


def _read_claude_head(
    transcript_path: Path, start: int
) -> tuple[list[ClaudeTranscriptItem], bool]:
    """Parse a transcript from byte ``start`` until the cap or the byte budget.

    Returns ``(items, stopped_early)``: at most ``IMPORT_MAX_ITEMS - 1`` items
    (room for the notice), and whether reading stopped with importable history
    (or unread bytes) still ahead. Parses like
    :func:`read_transcript_items_from_offset` (complete records only), but
    never past the stop.
    """
    limit = IMPORT_MAX_ITEMS - 1
    items: list[ClaudeTranscriptItem] = []
    response_id: str | None = None
    used = 0
    offset = start
    line_number = 0
    with transcript_path.open("rb") as handle:
        handle.seek(start)
        while True:
            raw = _next_budgeted_line(handle, used=used, have_items=bool(items))
            if raw is None:
                return _dedupe_compact_noop_echo(items), True
            if not raw.endswith(b"\n"):
                # End of file, or a record still being written.
                return _dedupe_compact_noop_echo(items), False
            record_offset = offset
            offset += len(raw)
            used += len(raw)
            line_number += 1
            try:
                entry = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            previous_response_id = response_id
            response_id, parsed = _transcript_items_from_entry(
                entry,
                line_number=line_number,
                record_offset=record_offset,
                agent_name="claude-native-ui",
                current_response_id=response_id,
            )
            # As the full read's ``legacy_agent_messages``: a hidden team
            # envelope doesn't start a new assistant response.
            if any(item.agent_message_candidate for item in parsed):
                response_id = previous_response_id
            if not parsed:
                continue
            if len(items) >= limit:
                return _dedupe_compact_noop_echo(items), True
            items.extend(parsed)
            if len(items) > limit:
                del items[limit:]
                return _dedupe_compact_noop_echo(items), True


def _load_claude_budgeted(session_id: str, transcript_path: Path, size: int) -> LocalSessionImport:
    """:func:`load_claude_session` for a transcript past the read budget.

    Starts at the last compaction boundary as the full read does, then keeps
    the first items that fit.
    """
    start = 0
    if size > _IMPORT_COMPACT_TRIM_BYTES:
        start = _last_claude_compaction_offset(transcript_path)
    parsed, stopped_early = _read_claude_head(transcript_path, start)
    items = tuple(
        NewConversationItem(
            type=item.item_type,
            response_id=item.response_id,
            data=parse_item_data(item.item_type, _claude_import_item_data(item)),
        )
        for item in parsed
    )
    if not items:
        raise SessionImportEmptyError(
            f"Claude Code session {session_id!r} has no importable history"
        )
    session = LocalSessionImport(
        source="claude",
        external_session_id=session_id,
        workspace=_claude_workspace(transcript_path),
        items=items,
        native_title=_claude_native_title(transcript_path),
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def load_claude_session(
    session_id: str,
    *,
    claude_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Claude Code parent session from its local JSONL transcript."""
    configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
    home = claude_home or (Path(configured_home).expanduser() if configured_home else None)
    root = (home or Path.home() / ".claude") / "projects"
    transcript_path = _find_transcript(root, session_id)
    if transcript_path is None:
        raise SessionImportNotFoundError(f"Claude Code session {session_id!r} was not found")
    budgeted_size = _size_over_read_budget(transcript_path)
    if budgeted_size is not None:
        return _load_claude_budgeted(session_id, transcript_path, budgeted_size)

    parsed = read_transcript_items_from_offset(
        transcript_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
        legacy_agent_messages=True,
    )
    # A large transcript has almost certainly compacted; import only what the
    # agent would still see (from the last compaction boundary). See
    # docs/session-compaction.md.
    source_items: Sequence[ClaudeTranscriptItem] = parsed.items
    if _exceeds_compaction_trim_size(transcript_path):
        source_items = _items_from_last_compaction(parsed.items)
    items = tuple(
        NewConversationItem(
            type=item.item_type,
            response_id=item.response_id,
            data=parse_item_data(item.item_type, _claude_import_item_data(item)),
        )
        for item in source_items
    )
    if not items:
        raise SessionImportEmptyError(
            f"Claude Code session {session_id!r} has no importable history"
        )
    return LocalSessionImport(
        source="claude",
        external_session_id=session_id,
        workspace=_claude_workspace(transcript_path),
        items=items,
        native_title=_claude_native_title(transcript_path),
    )


def _codex_message_data(payload: dict[str, object]) -> dict[str, object] | None:
    """Convert a visible Codex message payload to Omnigent message data."""
    role = payload.get("role")
    if not isinstance(role, str) or role not in {"user", "assistant"}:
        return None
    expected_type = "input_text" if role == "user" else "output_text"
    raw_content = payload.get("content")
    if not isinstance(raw_content, list):
        return None
    content: list[dict[str, object]] = []
    for block in raw_content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str) and text:
            content.append({"type": expected_type, "text": text})
        elif role == "user" and _str_in(block.get("type"), {"input_image", "input_file"}):
            content.append(dict(block))
    if not content:
        return None
    data: dict[str, object] = {"role": role, "content": content}
    if role == "assistant":
        data["agent"] = "codex-native-ui"
    elif _codex_internal_user_message(content):
        data["is_meta"] = True
    return data


_CODEX_INTERNAL_USER_PREFIXES = (
    "# AGENTS.md instructions for ",
    "<app-context>",
    "<collaboration_mode>",
    "<environment_context>",
    "<permissions instructions>",
    "<plugins_instructions>",
    "<skill>",
    "<skills_instructions>",
    "The following is the Codex agent history ",
    "The following is the Codex agent history added ",
)


def _codex_internal_user_message(content: list[dict[str, object]]) -> bool:
    """Identify Codex-injected user-role context that should stay hidden."""
    text = next(
        (block.get("text") for block in content if isinstance(block.get("text"), str)),
        None,
    )
    return isinstance(text, str) and text.lstrip().startswith(_CODEX_INTERNAL_USER_PREFIXES)


def _codex_tool_output(value: object) -> str | None:
    """Flatten Codex string or typed-text-block tool output."""
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return None
    text_blocks = [
        block["text"]
        for block in value
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
    return "".join(text_blocks) if text_blocks else None


def _codex_response_item(
    payload: dict[str, object],
    *,
    response_id: str,
) -> NewConversationItem | None:
    """Convert one supported Codex response item to an Omnigent item."""
    item_type = payload.get("type")
    normalized_type = item_type
    data: dict[str, object] | None = None
    if item_type == "message":
        data = _codex_message_data(payload)
    elif _str_in(item_type, {"function_call", "custom_tool_call"}):
        name = payload.get("name")
        arguments = payload.get("arguments" if item_type == "function_call" else "input")
        call_id = payload.get("call_id")
        if all(isinstance(value, str) for value in (name, arguments, call_id)):
            data = {
                "agent": "codex-native-ui",
                "name": name,
                "arguments": arguments,
                "call_id": call_id,
            }
            namespace = payload.get("namespace")
            if isinstance(namespace, str) and namespace:
                data["namespace"] = namespace
            normalized_type = "function_call"
    elif _str_in(item_type, {"function_call_output", "custom_tool_call_output"}):
        call_id = payload.get("call_id")
        output = _codex_tool_output(payload.get("output"))
        if isinstance(call_id, str) and output is not None:
            data = {"call_id": call_id, "output": output}
            normalized_type = "function_call_output"
    if data is None or not isinstance(normalized_type, str):
        return None
    return NewConversationItem(
        type=normalized_type,
        response_id=response_id[:64],
        data=parse_item_data(normalized_type, data),
    )


def _find_codex_rollout_in_homes(
    homes: Sequence[Path], session_id: str
) -> tuple[Path, Path] | None:
    """Return the newest matching Codex rollout and its containing home."""
    selected: tuple[Path, Path] | None = None
    selected_mtime = 0.0
    for path, home, candidate_id in _codex_rollout_candidates(homes):
        if candidate_id != session_id:
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        if selected is None or modified_at > selected_mtime:
            selected = (path, home)
            selected_mtime = modified_at
    return selected


def _codex_thread_name_from_index(home: Path, session_id: str) -> str | None:
    """Return the user's thread rename from ``session_index.jsonl``, if any.

    Renaming a Codex thread appends ``{id, thread_name, updated_at}`` here; it
    is codex's authoritative rename store and, unlike ``threads.title`` in the
    state DB, always reflects the rename. Last entry for the id wins.
    """
    index = home / "session_index.jsonl"
    name: str | None = None
    try:
        with index.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if session_id not in line:  # cheap prefilter before JSON parse
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and record.get("id") == session_id:
                    value = record.get("thread_name")
                    if isinstance(value, str) and value.strip():
                        name = value.strip()
    except OSError:
        return None
    return name


def _codex_native_title(home: Path, session_id: str) -> str | None:
    """Return the user's custom Codex thread title, or None if auto-derived.

    A rename lands in ``session_index.jsonl`` (``thread_name``) — the reliable
    source — so check that first. As a fallback, ``state_<n>.sqlite``'s
    ``threads.title`` holds the raw first user message until renamed, so only a
    title that diverges from ``first_user_message`` is a real custom name;
    otherwise the first-message synthesis is better.
    """
    indexed = _codex_thread_name_from_index(home, session_id)
    if indexed:
        return indexed
    try:
        # Lazy import: some Python builds (pyenv/Homebrew without SQLite
        # headers) lack ``_sqlite3``; without it the title falls back to the
        # first user message.
        import sqlite3
    except ImportError:
        return None

    def _state_db_version(path: Path) -> int:
        match = re.search(r"state_(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    dbs = sorted(home.glob("state_*.sqlite"), key=_state_db_version, reverse=True)
    for db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute(
                    "SELECT title, first_user_message FROM threads WHERE id = ?",
                    (session_id,),
                ).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if row is None:
            continue
        title = (row[0] or "").strip()
        first_message = (row[1] or "").strip()
        return title if title and title != first_message else None
    return None


def _codex_compacted_baseline_items(payload: dict[str, object]) -> list[NewConversationItem]:
    """Convert a Codex ``compacted`` record's replacement_history into items.

    Codex appends ``{type: "compacted", payload: {replacement_history: [...]}}``
    after compacting; ``replacement_history`` is the post-compaction context
    baseline it resumes from (the summary plus any retained messages), each entry
    response-item shaped. Unsupported entries (e.g. reasoning) parse to ``None``
    and are skipped, mirroring the ordinary response-item path.
    """
    history = payload.get("replacement_history")
    if not isinstance(history, list):
        return []
    baseline: list[NewConversationItem] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        item = _codex_response_item(entry, response_id="codex:compaction")
        if item is not None:
            baseline.append(item)
    return baseline


def _codex_has_visible_history(items: Sequence[NewConversationItem]) -> bool:
    """Whether a Codex session has anything beyond its injected context.

    Codex records hidden context (AGENTS.md, environment) as user messages
    before the first prompt, so a session closed without one holds only those
    and would import as a blank session. A compaction baseline counts as
    history even when its summary is hidden.
    """
    return any(
        item.response_id == "codex:compaction"
        or not (isinstance(item.data, MessageData) and item.data.is_meta)
        for item in items
    )


def _scan_codex_rollout(rollout_path: Path) -> tuple[int, str, str | None]:
    """Where a budgeted Codex read starts, without a full parse.

    Returns ``(offset, turn_id, workspace)``: the byte offset of the last
    ``compacted`` record with a usable baseline (``0`` when none), the turn id
    in effect there, and the last ``session_meta`` cwd — what the full read in
    :func:`load_codex_session` ends up with. Only lines naming one of those
    record types are decoded.
    """
    offset = 0
    last = 0
    turn_id = "history"
    turn_at_last = turn_id
    workspace: str | None = None
    with rollout_path.open("rb") as handle:
        for raw in handle:
            record_offset = offset
            offset += len(raw)
            if (
                b'"compacted"' not in raw
                and b'"turn_context"' not in raw
                and b'"session_meta"' not in raw
            ):
                continue
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            kind = record.get("type")
            if kind == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    workspace = cwd.strip()
            elif kind == "turn_context":
                candidate = payload.get("turn_id")
                if isinstance(candidate, str) and candidate:
                    turn_id = candidate
            elif kind == "compacted" and _codex_compacted_baseline_items(payload):
                last = record_offset
                turn_at_last = turn_id
    return last, turn_at_last, workspace


def _read_codex_head(
    rollout_path: Path, start: int, *, turn_id: str, trim_at_compaction: bool
) -> tuple[list[NewConversationItem], bool, str | None]:
    """Parse a rollout from byte ``start`` until the cap or the byte budget.

    Returns ``(items, stopped_early, workspace)`` with the record handling of
    :func:`load_codex_session`: at most ``IMPORT_MAX_ITEMS - 1`` items (room
    for the notice), whether reading stopped with importable history (or
    unread bytes) still ahead, and the last ``session_meta`` cwd read.
    """
    limit = IMPORT_MAX_ITEMS - 1
    items: list[NewConversationItem] = []
    workspace: str | None = None
    used = 0
    with rollout_path.open("rb") as handle:
        handle.seek(start)
        while True:
            raw = _next_budgeted_line(handle, used=used, have_items=bool(items))
            if raw is None:
                return items, True, workspace
            if not raw:
                return items, False, workspace
            used += len(raw)
            try:
                record = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            kind = record.get("type")
            if kind == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    workspace = cwd.strip()
            elif kind == "turn_context":
                candidate = payload.get("turn_id")
                if isinstance(candidate, str) and candidate:
                    turn_id = candidate
            elif kind == "compacted":
                compaction = _codex_compaction_item(payload)
                if compaction is not None:
                    if trim_at_compaction:
                        items = [compaction]
                    else:
                        items.append(compaction)
            elif kind == "response_item":
                item = _codex_response_item(payload, response_id=f"codex:{turn_id}")
                if item is None:
                    continue
                if len(items) >= limit:
                    return items, True, workspace
                items.append(item)


def _load_codex_budgeted(
    session_id: str, home: Path, rollout_path: Path, size: int
) -> LocalSessionImport:
    """:func:`load_codex_session` for a rollout past the read budget.

    Starts at the last compaction boundary as the full read does, then keeps
    the first items that fit.
    """
    trim_at_compaction = size > _IMPORT_COMPACT_TRIM_BYTES
    start, turn_id, scanned_workspace = 0, "history", None
    if trim_at_compaction:
        start, turn_id, scanned_workspace = _scan_codex_rollout(rollout_path)
    items, stopped_early, read_workspace = _read_codex_head(
        rollout_path, start, turn_id=turn_id, trim_at_compaction=trim_at_compaction
    )
    if not _codex_has_visible_history(items):
        raise SessionImportEmptyError(f"Codex session {session_id!r} has no importable history")
    session = LocalSessionImport(
        source="codex",
        external_session_id=session_id,
        workspace=scanned_workspace or read_workspace,
        items=tuple(items),
        native_title=_codex_native_title(home, session_id),
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def _codex_thread_row(home: Path, thread_id: str) -> dict[str, object] | None:
    """A thread's row from the newest ``state_<n>.sqlite`` in ``home``.

    Columns vary by Codex version. See :func:`_codex_thread_location`.
    """
    try:
        # Lazy import: some Python builds lack ``_sqlite3``; without it the
        # rollout is found by filename.
        import sqlite3
    except ImportError:
        return None

    def _state_db_version(path: Path) -> int:
        match = re.search(r"state_(\d+)\.sqlite$", path.name)
        return int(match.group(1)) if match else -1

    for db in sorted(home.glob("state_*.sqlite"), key=_state_db_version, reverse=True):
        try:
            con = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
            try:
                con.row_factory = sqlite3.Row
                row = con.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            continue
        if row is not None:
            return dict(row)
    return None


def _codex_recorded_rollout(home: Path, thread_row: dict[str, object] | None) -> Path | None:
    """The rollout file the thread store names for a thread, if it exists."""
    recorded = thread_row.get("rollout_path") if thread_row else None
    if not isinstance(recorded, str) or not recorded.strip():
        return None
    path = Path(recorded.strip()).expanduser()
    if not path.is_absolute():
        path = home / path
    return path if path.suffix == ".jsonl" and path.is_file() else None


def _codex_thread_location(
    homes: Sequence[Path], thread_id: str
) -> tuple[Path, Path, dict[str, object] | None] | None:
    """A thread's current rollout, its home, and its thread-store row.

    A revert starts a new rollout (``rollout-<ts>-<thread>_<rollout>.jsonl``) and
    can leave the old one newer by mtime, so the thread store's ``rollout_path``
    wins over a filename match. Across homes the newest rollout wins.
    """
    selected: tuple[Path, Path, dict[str, object] | None] | None = None
    selected_mtime = 0.0
    for home in homes:
        thread_row = _codex_thread_row(home, thread_id)
        rollout_path = _codex_recorded_rollout(home, thread_row)
        if rollout_path is None:
            located = _find_codex_rollout_in_homes((home,), thread_id)
            rollout_path = located[0] if located is not None else None
        if rollout_path is None:
            continue
        try:
            modified_at = rollout_path.stat().st_mtime
        except OSError:
            continue
        if selected is None or modified_at > selected_mtime:
            selected = (rollout_path, home, thread_row)
            selected_mtime = modified_at
    return selected


def _codex_thread_archived(
    home: Path, thread_row: dict[str, object] | None, rollout_path: Path
) -> bool:
    """Whether Codex archived the thread: ``threads.archived`` or an archived_sessions/ rollout."""
    if thread_row is not None and thread_row.get("archived"):
        return True
    return (home / "archived_sessions").resolve() in rollout_path.resolve().parents


def _codex_record_ordinal(record: dict[str, object], position: int) -> int:
    """A record's ``ordinal``; rollouts written before ordinals fall back to the line position."""
    ordinal = record.get("ordinal")
    if isinstance(ordinal, int) and not isinstance(ordinal, bool):
        return ordinal
    return position


# The model-input keys a compaction carries per replacement_history entry type;
# anything else (Codex ids and metadata) stays out of the resumed context.
_CODEX_COMPACTED_MESSAGE_KEYS: dict[object, tuple[str, ...]] = {
    "message": ("type", "role", "content"),
    "function_call": ("type", "name", "arguments", "call_id", "namespace"),
    "custom_tool_call": ("type", "name", "input", "call_id"),
    "function_call_output": ("type", "call_id", "output"),
    "custom_tool_call_output": ("type", "call_id", "output"),
}


def _codex_compaction_item(payload: dict[str, object]) -> NewConversationItem | None:
    """A ``compacted`` record as a compaction item, the shape a live session stores.

    ``replacement_history`` is the context Codex resumes from. Entries the import
    can't map (reasoning, encrypted content) are dropped. ``None`` when nothing
    usable remains.
    """
    history = payload.get("replacement_history")
    if not isinstance(history, list):
        return None
    messages: list[dict[str, object]] = []
    for entry in history:
        if (
            not isinstance(entry, dict)
            or _codex_response_item(entry, response_id="codex:compaction") is None
        ):
            continue
        keys = _CODEX_COMPACTED_MESSAGE_KEYS.get(entry.get("type"))
        if keys is not None:
            messages.append({key: entry[key] for key in keys if key in entry})
    if not messages:
        return None
    summary = payload.get("message")
    data: dict[str, object] = {
        "summary": summary.strip()
        if isinstance(summary, str) and summary.strip()
        else _CODEX_COMPACTION_FALLBACK_SUMMARY,
        "last_item_id": _CODEX_COMPACTION_PLACEHOLDER_ITEM_ID,
        "token_count": 0,
        "compacted_messages": messages,
    }
    window_id = payload.get("window_id")
    if isinstance(window_id, (int, str)) and not isinstance(window_id, bool):
        data["window_id"] = window_id
    return NewConversationItem(
        type="compaction", response_id="codex:compaction", data=parse_item_data("compaction", data)
    )


def _codex_native_bridge_dirs() -> tuple[Path, ...]:
    """Return real bridge directories under the producer's bridge root."""
    root = bridge_root()
    try:
        entries = tuple(root.iterdir())
    except OSError:
        return ()
    bridge_dirs: list[Path] = []
    for entry in entries:
        try:
            if entry.is_dir() and not entry.is_symlink():
                bridge_dirs.append(entry)
        except OSError:
            continue
    return tuple(bridge_dirs)


def _codex_native_thread_ids(omnigent_session_id: str) -> frozenset[str]:
    """Return the Codex thread ids that native bridges record for an Omnigent session id.

    The web UI shows Omnigent's session id, which "Session by ID" import used to
    reject.
    """
    thread_ids: set[str] = set()
    for bridge_dir in _codex_native_bridge_dirs():
        try:
            state = read_bridge_state(bridge_dir)
        except (ValueError, RecursionError):
            # The JSON decoder rejects oversized integers and deep nesting this way.
            continue
        if state is not None and state.session_id == omnigent_session_id:
            thread_ids.add(state.thread_id)
    return frozenset(thread_ids)


def _find_codex_rollout_by_rollout_id(home: Path, rollout_id: str) -> Path | None:
    """The newest rollout in ``home`` whose filename ends with ``rollout_id``."""
    selected: Path | None = None
    selected_mtime = 0.0
    for path in _codex_rollout_paths(home):
        ids = _codex_rollout_ids(path)
        if ids is None or ids[1] != rollout_id:
            continue
        try:
            modified_at = path.stat().st_mtime
        except OSError:
            continue
        if selected is None or modified_at > selected_mtime:
            selected, selected_mtime = path, modified_at
    return selected


def _codex_inherited_lines(
    home: Path, meta: dict[str, object], *, seen: set[str], limit: int | None = None
) -> list[str]:
    """The rollout lines a forked or reverted rollout inherits, oldest first.

    Such a rollout can hold only ``session_meta`` plus its new turns; the rest is
    the base rollout's records before ``history_base.end_ordinal_exclusive``.
    ``history_base.thread_id`` names the base *rollout*: the id a rollout filename
    ends with. ``seen`` (rollout ids) stops cycles; ``limit`` caps a nested cutoff
    at the outer one.
    """
    base = meta.get("history_base")
    if not isinstance(base, dict):
        return []
    base_id = base.get("thread_id")
    end = base.get("end_ordinal_exclusive")
    if not isinstance(base_id, str) or base_id in seen:
        return []
    if isinstance(end, bool) or not isinstance(end, int) or end <= 0:
        return []
    if limit is not None:
        end = min(end, limit)
    seen.add(base_id)
    base_path = _find_codex_rollout_by_rollout_id(home, base_id)
    if base_path is None:
        return []
    lines: list[str] = []
    with base_path.open(encoding="utf-8", errors="replace") as handle:
        for position, line in enumerate(handle):
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            payload = record.get("payload")
            if record.get("type") == "session_meta" and isinstance(payload, dict):
                # A base that is itself forked starts at its own cutoff's ordinal,
                # so its inherited records can fall under ``end`` even when its
                # own session_meta does not.
                lines.extend(_codex_inherited_lines(home, payload, seen=seen, limit=end))
            if _codex_record_ordinal(record, position) >= end:
                break
            lines.append(line)
    return lines


def _codex_items_from_lines(
    lines: Iterable[str], *, trim_at_compaction: bool
) -> tuple[list[NewConversationItem], str | None]:
    """Rollout lines to items, with the last ``session_meta`` cwd.

    Each usable ``compacted`` record becomes a compaction item. Past the size
    threshold the import restarts at each one, so it matches what the agent
    resumes with and drops the records it no longer sees. See
    docs/session-compaction.md.
    """
    workspace: str | None = None
    turn_id = "history"
    items: list[NewConversationItem] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            continue
        payload = record["payload"]
        kind = record.get("type")
        if kind == "session_meta":
            cwd = payload.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                workspace = cwd.strip()
        elif kind == "turn_context":
            candidate = payload.get("turn_id")
            if isinstance(candidate, str) and candidate:
                turn_id = candidate
        elif kind == "compacted":
            compaction = _codex_compaction_item(payload)
            if compaction is not None:
                items = [compaction] if trim_at_compaction else [*items, compaction]
        elif kind == "response_item":
            item = _codex_response_item(payload, response_id=f"codex:{turn_id}")
            if item is not None:
                items.append(item)
    return items, workspace


def _codex_import_title(
    home: Path,
    thread_id: str,
    thread_row: dict[str, object] | None,
    items: Sequence[NewConversationItem],
) -> str | None:
    """The thread's custom title, or its thread-store title when no visible prompt was imported.

    A thread trimmed to its last compaction can hold no user message to name it.
    """
    custom = _codex_native_title(home, thread_id)
    if custom or thread_row is None:
        return custom
    if any(
        isinstance(item.data, MessageData) and item.data.role == "user" and not item.data.is_meta
        for item in items
    ):
        return None
    title = thread_row.get("title")
    if not isinstance(title, str):
        return None
    return synthesize_conversation_title([{"type": "input_text", "text": title}])


def _codex_located_thread(
    homes: Sequence[Path], session_id: str, *, explicit_home: bool
) -> tuple[str, Path, Path, dict[str, object] | None] | None:
    """``(thread_id, rollout, home, thread_row)`` for a Codex thread id or a native Omnigent id.

    The web UI shows Omnigent's session id; a codex-native bridge records which
    thread serves it.
    """
    located = _codex_thread_location(homes, session_id)
    if located is not None:
        return (session_id, *located)
    if explicit_home:
        return None
    selected: tuple[str, Path, Path, dict[str, object] | None] | None = None
    selected_mtime = 0.0
    for thread_id in sorted(_codex_native_thread_ids(session_id)):
        location = _codex_thread_location(homes, thread_id)
        if location is None:
            continue
        try:
            modified_at = location[0].stat().st_mtime
        except OSError:
            continue
        if selected is None or modified_at > selected_mtime:
            selected = (thread_id, *location)
            selected_mtime = modified_at
    return selected


def load_codex_session(
    session_id: str,
    *,
    codex_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Codex thread by thread id, or by the Omnigent id of a native session.

    Reads the rollout the thread store names plus the history it inherits through
    ``history_base``; each ``compacted`` record becomes a compaction item.
    """
    homes = _codex_storage_homes(codex_home)
    located = _codex_located_thread(homes, session_id, explicit_home=codex_home is not None)
    if located is None:
        raise SessionImportNotFoundError(f"Codex session {session_id!r} was not found")
    thread_id, rollout_path, home, thread_row = located
    archived = _codex_thread_archived(home, thread_row, rollout_path)
    budgeted_size = _size_over_read_budget(rollout_path)
    if budgeted_size is not None:
        # The baseline at the last compaction stands in for inherited history.
        session = _load_codex_budgeted(thread_id, home, rollout_path, budgeted_size)
        return dataclasses.replace(
            session,
            archived=archived,
            native_title=_codex_import_title(home, thread_id, thread_row, session.items),
        )

    with rollout_path.open(encoding="utf-8", errors="replace") as handle:
        own_lines = list(handle)
    meta: dict[str, object] = {}
    if own_lines:
        try:
            first = json.loads(own_lines[0])
        except json.JSONDecodeError:
            first = None
        if isinstance(first, dict) and first.get("type") == "session_meta":
            payload = first.get("payload")
            meta = payload if isinstance(payload, dict) else {}
    own_ids = _codex_rollout_ids(rollout_path)
    own_rollout_id = own_ids[1] if own_ids is not None else thread_id
    inherited_lines = _codex_inherited_lines(home, meta, seen={own_rollout_id})
    size = sum(len(line.encode("utf-8")) for line in chain(inherited_lines, own_lines))
    items, workspace = _codex_items_from_lines(
        chain(inherited_lines, own_lines),
        trim_at_compaction=size > _IMPORT_COMPACT_TRIM_BYTES,
    )
    if not _codex_has_visible_history(items):
        raise SessionImportEmptyError(f"Codex session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="codex",
        external_session_id=thread_id,
        workspace=workspace,
        items=tuple(items),
        native_title=_codex_import_title(home, thread_id, thread_row, items),
        archived=archived,
    )


def _qwen_message_data(record: dict[str, object]) -> dict[str, object] | None:
    """Convert one visible Qwen recording row to Omnigent message data."""
    # Qwen records assistant events as type="assistant" while message.role is "model".
    record_type = record.get("type")
    if record_type == "user":
        role = "user"
        content_type = "input_text"
    elif record_type == "assistant":
        role = "assistant"
        content_type = "output_text"
    else:
        return None
    message = record.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("parts"), list):
        return None
    content = [
        {"type": content_type, "text": part["text"]}
        for part in message["parts"]
        if isinstance(part, dict) and isinstance(part.get("text"), str) and part["text"]
    ]
    if not content:
        return None
    data: dict[str, object] = {"role": role, "content": content}
    if role == "assistant":
        data["agent"] = "qwen-native-ui"
    return data


def _qwen_active_branch(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return Qwen records on the current leaf's root-to-leaf path."""
    linked = [record for record in records if isinstance(record.get("uuid"), str)]
    if not linked or all("parentUuid" not in record for record in linked):
        return records
    by_id = {record["uuid"]: record for record in linked}
    if len(by_id) != len(linked):
        return []

    branch: list[dict[str, object]] = []
    current = linked[-1]
    seen: set[str] = set()
    while True:
        record_id = current["uuid"]
        if not isinstance(record_id, str) or record_id in seen:
            return []
        seen.add(record_id)
        branch.append(current)
        parent_id = current.get("parentUuid")
        if parent_id is None:
            branch.reverse()
            return branch
        if not isinstance(parent_id, str) or parent_id not in by_id:
            return []
        current = by_id[parent_id]


def load_qwen_session(
    session_id: str,
    *,
    qwen_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Qwen Code session from its project chat recording."""
    configured_home = os.environ.get("QWEN_HOME")
    home = qwen_home or (Path(configured_home).expanduser() if configured_home else None)
    root = (home or Path.home() / ".qwen") / "projects"
    qualified = ":" in session_id
    matches = [
        path
        for path in root.glob("*/chats/*.jsonl")
        if path.is_file()
        and (_qwen_session_locator(path) == session_id if qualified else path.stem == session_id)
    ]
    if not matches:
        raise SessionImportNotFoundError(f"Qwen Code session {session_id!r} was not found")
    if len(matches) > 1:
        choices = ", ".join(sorted(_qwen_session_locator(path) for path in matches))
        raise SessionImportNotFoundError(
            f"Qwen Code session {session_id!r} is ambiguous; use one of: {choices}"
        )
    transcript_path = matches[0]

    lines, stopped_early = _read_transcript_lines(transcript_path)
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        records.append(record)

    workspace: str | None = None
    items: list[NewConversationItem] = []
    for record_number, record in enumerate(_qwen_active_branch(records), start=1):
        if workspace is None:
            cwd = record.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                workspace = cwd.strip()
        data = _qwen_message_data(record)
        if data is None:
            continue
        record_id = record.get("uuid")
        response_id = (
            f"qwen:{record_id}"
            if isinstance(record_id, str) and record_id
            else f"qwen:{record_number}"
        )
        items.append(
            NewConversationItem(
                type="message",
                response_id=_bounded_response_id(response_id),
                data=parse_item_data("message", data),
            )
        )
    if not items:
        raise SessionImportEmptyError(
            f"Qwen Code session {session_id!r} has no importable history"
        )
    session = LocalSessionImport(
        source="qwen",
        external_session_id=_qwen_session_locator(transcript_path),
        workspace=workspace,
        items=tuple(items),
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def load_kiro_session(
    session_id: str,
    *,
    kiro_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Kiro CLI session from its metadata and JSONL transcript."""
    root = kiro_cli_sessions_dir(kiro_home)
    transcript_path = next(
        (path for path in root.glob("*.jsonl") if path.is_file() and path.stem == session_id),
        None,
    )
    if transcript_path is None:
        raise SessionImportNotFoundError(f"Kiro session {session_id!r} was not found")
    metadata_path = transcript_path.with_suffix(".json")
    if not metadata_path.is_file():
        raise SessionImportNotFoundError(f"Kiro session {session_id!r} was not found")
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError) as exc:
        raise SessionImportNotFoundError(
            f"Kiro session {session_id!r} has unreadable metadata"
        ) from exc
    workspace_value = metadata.get("cwd") if isinstance(metadata, dict) else None
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    try:
        lines, stopped_early = _read_transcript_lines(transcript_path)
        messages = [
            message for line in lines if (message := parse_kiro_jsonl_line(line)) is not None
        ]
    except OSError as exc:
        raise SessionImportNotFoundError(
            f"Kiro session {session_id!r} has an unreadable transcript"
        ) from exc
    items = tuple(
        NewConversationItem(
            type="message",
            response_id=_bounded_response_id(f"kiro:{message.message_id}"),
            data=parse_item_data(
                "message",
                {
                    "role": message.role,
                    **({"agent": "kiro-native-ui"} if message.role == "assistant" else {}),
                    "content": [
                        {
                            "type": "output_text" if message.role == "assistant" else "input_text",
                            "text": message.text,
                        }
                    ],
                },
            ),
        )
        for message in messages
    )
    if not items:
        raise SessionImportEmptyError(f"Kiro session {session_id!r} has no importable history")
    session = LocalSessionImport(
        source="kiro",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def _pi_text(content: object) -> str:
    """Flatten Pi string or typed-text content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    )


def _pi_message_content(content: object, *, role: str) -> list[dict[str, object]]:
    """Map Pi text and user-image blocks without changing their order."""
    content_type = "input_text" if role == "user" else "output_text"
    if isinstance(content, str):
        return [{"type": content_type, "text": content}] if content else []
    if not isinstance(content, list):
        return []
    normalized: list[dict[str, object]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if block.get("type") == "text" and isinstance(text, str) and text:
            normalized.append({"type": content_type, "text": text})
            continue
        data = block.get("data")
        mime_type = block.get("mimeType")
        if (
            role == "user"
            and block.get("type") == "image"
            and isinstance(data, str)
            and data
            and isinstance(mime_type, str)
            and mime_type.startswith("image/")
        ):
            normalized.append(
                {"type": "input_image", "image_url": f"data:{mime_type};base64,{data}"}
            )
    return normalized


def _pi_active_branch(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return Pi entries on the current leaf's root-to-leaf path."""
    header = next((record for record in records if record.get("type") == "session"), {})
    version = header.get("version")
    if not isinstance(version, int) or version < 2:
        legacy_parent_id: str | None = None
        migrated: list[dict[str, object]] = []
        for index, record in enumerate(records):
            if record.get("type") == "session":
                migrated.append(record)
                continue
            entry = dict(record)
            legacy_entry_id = f"legacy-{index}"
            entry["id"] = legacy_entry_id
            entry["parentId"] = legacy_parent_id
            migrated.append(entry)
            legacy_parent_id = legacy_entry_id
        records = migrated
    entries = [
        record
        for record in records
        if record.get("type") != "session" and isinstance(record.get("id"), str)
    ]
    if not entries:
        return []
    if all("parentId" not in entry for entry in entries):
        return entries
    by_id = {entry["id"]: entry for entry in entries}
    if len(by_id) != len(entries):
        return []
    branch: list[dict[str, object]] = []
    current = entries[-1]
    seen: set[str] = set()
    while True:
        entry_id = current["id"]
        if not isinstance(entry_id, str) or entry_id in seen:
            return []
        seen.add(entry_id)
        branch.append(current)
        parent_id = current.get("parentId")
        if parent_id is None:
            branch.reverse()
            return branch
        if not isinstance(parent_id, str) or parent_id not in by_id:
            return []
        current = by_id[parent_id]


def _pi_message_items(record: dict[str, object]) -> tuple[NewConversationItem, ...]:
    """Convert one Pi message entry to visible Omnigent items."""
    if record.get("type") == "branch_summary":
        summary = record.get("summary")
        if not isinstance(summary, str) or not summary:
            return ()
        entry_id = record.get("id")
        response_id = f"pi:{entry_id}" if isinstance(entry_id, str) else "pi:history"
        return (
            NewConversationItem(
                type="message",
                response_id=_bounded_response_id(response_id),
                data=parse_item_data(
                    "message",
                    {
                        "role": "user",
                        "is_meta": True,
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "The following is a summary of a branch that this "
                                    "conversation came back from:\n\n<summary>\n"
                                    f"{summary}\n</summary>"
                                ),
                            }
                        ],
                    },
                ),
            ),
        )
    message = record.get("message")
    if record.get("type") != "message" or not isinstance(message, dict):
        return ()
    entry_id = record.get("id")
    response_id = f"pi:{entry_id}" if isinstance(entry_id, str) else "pi:history"
    role = message.get("role")
    if role == "toolResult":
        call_id = message.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return ()
        return (
            NewConversationItem(
                type="function_call_output",
                response_id=_bounded_response_id(response_id),
                data=parse_item_data(
                    "function_call_output",
                    {"call_id": call_id, "output": _pi_text(message.get("content"))},
                ),
            ),
        )
    if not isinstance(role, str) or role not in {"user", "assistant"}:
        return ()

    items: list[NewConversationItem] = []
    content = message.get("content")
    if role == "user":
        normalized = _pi_message_content(content, role=role)
        if not normalized:
            return ()
        items.append(
            NewConversationItem(
                type="message",
                response_id=_bounded_response_id(response_id),
                data=parse_item_data(
                    "message",
                    {"role": "user", "content": normalized},
                ),
            )
        )
        return tuple(items)

    interrupted = message.get("stopReason") == "aborted"

    def append_assistant_text(blocks: list[dict[str, object]]) -> None:
        if not blocks:
            return
        data: dict[str, object] = {
            "role": "assistant",
            "agent": "pi-native-ui",
            "content": blocks,
        }
        if interrupted:
            data["interrupted"] = True
        items.append(
            NewConversationItem(
                type="message",
                response_id=_bounded_response_id(response_id),
                data=parse_item_data("message", data),
            )
        )

    if isinstance(content, str):
        append_assistant_text(_pi_message_content(content, role="assistant"))
    elif isinstance(content, list):
        pending_text: list[dict[str, object]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                pending_text.extend(_pi_message_content([block], role="assistant"))
                continue
            if block.get("type") != "toolCall":
                continue
            append_assistant_text(pending_text)
            pending_text = []
            call_id = block.get("id")
            name = block.get("name")
            if (
                not isinstance(call_id, str)
                or not call_id
                or not isinstance(name, str)
                or not name
            ):
                continue
            arguments = block.get("arguments")
            serialized_arguments = (
                arguments
                if isinstance(arguments, str)
                else json.dumps(arguments if arguments is not None else {}, separators=(",", ":"))
            )
            # Only message items support interrupted state; retain aborted-turn tool calls.
            items.append(
                NewConversationItem(
                    type="function_call",
                    response_id=_bounded_response_id(response_id),
                    data=parse_item_data(
                        "function_call",
                        {
                            "agent": "pi-native-ui",
                            "name": name,
                            "arguments": serialized_arguments,
                            "call_id": call_id,
                        },
                    ),
                )
            )
        append_assistant_text(pending_text)
    return tuple(items)


def load_pi_session(
    session_id: str,
    *,
    pi_home: Path | None = None,
) -> LocalSessionImport:
    """Load the active branch of one Pi coding-agent JSONL session."""
    configured_home = os.environ.get("PI_CODING_AGENT_DIR")
    home = pi_home or (Path(configured_home).expanduser() if configured_home else None)
    root = (home or Path.home() / ".pi" / "agent") / "sessions"
    if not _is_safe_pi_import_session_id(session_id):
        raise SessionImportNotFoundError(f"Pi session {session_id!r} was not found")
    matches = [
        path
        for path in root.rglob("*.jsonl")
        if path.is_file() and _pi_session_id_from_path(path) == session_id
    ]
    if not matches:
        raise SessionImportNotFoundError(f"Pi session {session_id!r} was not found")
    if len(matches) > 1:
        raise SessionImportNotFoundError(f"Pi session {session_id!r} is ambiguous across projects")
    transcript_path = matches[0]
    lines, stopped_early = _read_transcript_lines(transcript_path)
    records: list[dict[str, object]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    header = next((record for record in records if record.get("type") == "session"), {})
    if header.get("id") != session_id:
        raise SessionImportNotFoundError(
            f"Pi session {session_id!r} has mismatched transcript metadata"
        )
    workspace_value = header.get("cwd")
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    items = tuple(
        item for record in _pi_active_branch(records) for item in _pi_message_items(record)
    )
    if not items:
        raise SessionImportEmptyError(f"Pi session {session_id!r} has no importable history")
    session = LocalSessionImport(
        source="pi",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def _read_kimi_wire_items_budgeted(wire_path: Path) -> tuple[list[KimiWireItem], bool]:
    """:func:`read_kimi_wire_items` from line 0, read no further than the byte budget.

    A wire log within the budget takes the forwarder's own reader unchanged;
    a larger one is parsed line by line until the budget, with the same row
    mapping. Returns ``(items, stopped_early)``.
    """
    if _size_over_read_budget(wire_path) is None:
        return read_kimi_wire_items(wire_path, 0), False
    lines, stopped_early = _read_transcript_lines(wire_path)
    items: list[KimiWireItem] = []
    for line_no, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("{"):
            continue
        try:
            row = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(row, dict) and (item := _row_to_item(line_no, row)) is not None:
            items.append(item)
    return items, stopped_early


def load_kimi_session(
    session_id: str,
    *,
    kimi_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Kimi Code session from its append-only wire log."""
    home = kimi_home or resolve_user_kimi_home()
    matches = [
        path
        for path in (home / "sessions").glob("*/session_*/agents/main/wire.jsonl")
        if path.is_file() and path.parent.parent.parent.name == session_id
    ]
    if not matches:
        raise SessionImportNotFoundError(f"Kimi session {session_id!r} was not found")
    if len(matches) > 1:
        raise SessionImportNotFoundError(
            f"Kimi session {session_id!r} is ambiguous across workspaces"
        )
    wire_path = matches[0]
    session_dir = wire_path.parent.parent.parent
    workspace_value = workdirs_for_kimi_sessions(home).get(str(session_dir))
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    mirrored, stopped_early = _read_kimi_wire_items_budgeted(wire_path)
    items = tuple(
        NewConversationItem(
            type="message",
            response_id=_bounded_response_id(item.response_id),
            data=parse_item_data(
                "message",
                {
                    "role": item.role,
                    **({"agent": "kimi-native-ui"} if item.role == "assistant" else {}),
                    "content": [
                        {
                            "type": "output_text" if item.role == "assistant" else "input_text",
                            "text": item.text,
                        }
                    ],
                },
            ),
        )
        for item in mirrored
        if item.kind == "message"
    )
    if not items:
        raise SessionImportEmptyError(f"Kimi session {session_id!r} has no importable history")
    session = LocalSessionImport(
        source="kimi",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )
    return _budgeted_session(session, stopped_early=stopped_early)


def _opencode_file_content(
    part: dict[str, object],
    *,
    role: str,
) -> dict[str, object] | None:
    """Convert one exported OpenCode file part to a durable content block."""
    mime = part.get("mime")
    url = part.get("url")
    if isinstance(mime, str) and mime.startswith("image/") and isinstance(url, str) and url:
        return {
            "type": "input_image" if role == "user" else "output_image",
            "image_url": url,
        }
    filename = part.get("filename")
    label = filename if isinstance(filename, str) and filename else mime
    if not isinstance(label, str) or not label:
        label = "attachment"
    return {
        "type": "input_text" if role == "user" else "output_text",
        "text": f"[attachment: {label}]",
    }


def _opencode_message_items(
    message: dict[str, object],
    *,
    message_number: int,
) -> tuple[NewConversationItem, ...]:
    """Normalize one exported OpenCode message while preserving part order."""
    info = message.get("info")
    parts = message.get("parts")
    if not isinstance(info, dict) or not isinstance(parts, list):
        return ()
    role = info.get("role")
    if not isinstance(role, str) or role not in {"user", "assistant"}:
        return ()
    message_id = info.get("id")
    native_id = message_id if isinstance(message_id, str) and message_id else str(message_number)
    response_id = _bounded_response_id(f"opencode:{native_id}")
    items: list[NewConversationItem] = []
    pending_content: list[dict[str, object]] = []

    def flush_content() -> None:
        if not pending_content:
            return
        data: dict[str, object] = {"role": role, "content": list(pending_content)}
        if role == "assistant":
            data["agent"] = "opencode-native-ui"
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=parse_item_data("message", data),
            )
        )
        pending_content.clear()

    for raw_part in parts:
        if not isinstance(raw_part, dict):
            continue
        part: dict[str, object] = raw_part
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                pending_content.append(
                    {
                        "type": "input_text" if role == "user" else "output_text",
                        "text": text,
                    }
                )
            continue
        if part_type == "file":
            content = _opencode_file_content(part, role=role)
            if content is not None:
                pending_content.append(content)
            continue
        if part_type == "step-finish":
            flush_content()
            continue
        if part_type != "tool" or role != "assistant":
            continue
        flush_content()
        call_id = part.get("callID")
        name = part.get("tool")
        state = part.get("state")
        if (
            not isinstance(call_id, str)
            or not call_id
            or not isinstance(name, str)
            or not name
            or not isinstance(state, dict)
        ):
            continue
        arguments = state.get("input")
        serialized_arguments = (
            arguments
            if isinstance(arguments, str)
            else json.dumps(
                arguments if arguments is not None else {},
                separators=(",", ":"),
                ensure_ascii=True,
            )
        )
        items.append(
            NewConversationItem(
                type="function_call",
                response_id=response_id,
                data=parse_item_data(
                    "function_call",
                    {
                        "agent": "opencode-native-ui",
                        "name": name,
                        "arguments": serialized_arguments,
                        "call_id": call_id,
                    },
                ),
            )
        )
        status = state.get("status")
        output: str | None = None
        if status == "completed":
            output = opencode_tool_output_text(state)
        elif status == "error":
            error = state.get("error")
            output = f"[error] {error}" if error else "[error]"
        if output is not None:
            items.append(
                NewConversationItem(
                    type="function_call_output",
                    response_id=response_id,
                    data=parse_item_data(
                        "function_call_output",
                        {"call_id": call_id, "output": output},
                    ),
                )
            )
    flush_content()
    return tuple(items)


def load_opencode_session(
    session_id: str,
    *,
    opencode_path: str | None = None,
) -> LocalSessionImport:
    """Load one session through OpenCode's supported JSON export command."""
    if not _is_safe_opencode_import_session_id(session_id):
        raise SessionImportNotFoundError(f"OpenCode session {session_id!r} was not found")
    payload = _run_opencode_json("export", session_id, "--pure", opencode_path=opencode_path)
    if not isinstance(payload, dict):
        raise SessionImportNotFoundError(
            f"OpenCode session {session_id!r} returned an invalid export"
        )
    info = payload.get("info")
    exported_id = info.get("id") if isinstance(info, dict) else None
    if exported_id != session_id:
        raise SessionImportNotFoundError(
            f"OpenCode export id {exported_id!r} did not match {session_id!r}"
        )
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    items = tuple(
        item
        for message_number, message in enumerate(messages, start=1)
        if isinstance(message, dict)
        for item in _opencode_message_items(message, message_number=message_number)
    )
    if not items:
        raise SessionImportEmptyError(f"OpenCode session {session_id!r} has no importable history")
    workspace_value = info.get("directory") if isinstance(info, dict) else None
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    # OpenCode auto-generates a session title (info.title); carry it as the
    # native title instead of synthesizing from the first message.
    title_value = info.get("title") if isinstance(info, dict) else None
    native_title = (
        title_value.strip() if isinstance(title_value, str) and title_value.strip() else None
    )
    return LocalSessionImport(
        source="opencode",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
        native_title=native_title,
    )


# A 32-hex conversation id, or the long numeric id some stores assign: never a
# harness session id.
_OMNIGENT_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}|[0-9]{10,}")
OMNIGENT_SESSION_ID_HINT = (
    "That looks like an Omnigent session id; import takes the harness's own session id."
)


def load_local_session(source: ImportSource, session_id: str) -> LocalSessionImport:
    """Load one local session from the selected first-party harness.

    Shared by ``omnigent import`` and the host's import handler. A history over
    :data:`IMPORT_MAX_ITEMS` keeps its first items (after any compaction trim
    the harness loader applied); a Claude, Codex, Qwen, Pi, Kiro or Kimi
    transcript over :data:`IMPORT_READ_BUDGET_BYTES` is parsed only that far.
    """
    try:
        return _cap_session_items(_load_uncapped_local_session(source, session_id))
    except SessionImportEmptyError:
        raise
    except SessionImportNotFoundError as exc:
        # People paste the id from an Omnigent URL; say which id is wanted.
        if str(exc).endswith("was not found") and _OMNIGENT_SESSION_ID_RE.fullmatch(
            session_id.strip().lower()
        ):
            raise SessionImportNotFoundError(f"{exc}. {OMNIGENT_SESSION_ID_HINT}") from exc
        raise


def _load_uncapped_local_session(source: ImportSource, session_id: str) -> LocalSessionImport:
    if source == "claude":
        return load_claude_session(session_id)
    if source == "codex":
        return load_codex_session(session_id)
    if source == "qwen":
        return load_qwen_session(session_id)
    if source == "kiro":
        return load_kiro_session(session_id)
    if source == "pi":
        return load_pi_session(session_id)
    if source == "kimi":
        return load_kimi_session(session_id)
    if source == "opencode":
        return load_opencode_session(session_id)
    raise ValueError(f"Unsupported import source: {source}")


__all__ = [
    "IMPORT_MAX_ITEMS",
    "IMPORT_READ_BUDGET_BYTES",
    "IMPORT_TRIMMED_NOTICE_CODE",
    "OMNIGENT_SESSION_ID_HINT",
    "cap_import_items",
    "list_recent_local_session_ids",
    "load_claude_session",
    "load_codex_session",
    "load_kimi_session",
    "load_kiro_session",
    "load_local_session",
    "load_opencode_session",
    "load_pi_session",
    "load_qwen_session",
]
