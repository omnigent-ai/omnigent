"""Read and normalize local coding-harness transcripts."""

from __future__ import annotations

import json
import os
import re
import subprocess
from hashlib import sha256
from pathlib import Path

from omnigent.claude_native_bridge import read_transcript_items_from_offset
from omnigent.codex_native import _CODEX_THREAD_ID_RE, _find_codex_rollout
from omnigent.entities import NewConversationItem, parse_item_data
from omnigent.kimi_native_credentials import resolve_user_kimi_home
from omnigent.kimi_native_forwarder import (
    read_kimi_wire_items,
    workdirs_for_kimi_sessions,
)
from omnigent.kiro_native_session_forwarder import (
    kiro_cli_sessions_dir,
    parse_kiro_jsonl_line,
)
from omnigent.opencode_native_app_server import (
    OpenCodeCliNotFoundError,
    find_opencode_cli,
)
from omnigent.opencode_native_forwarder import opencode_tool_output_text
from omnigent.session_import.models import (
    ImportSource,
    LocalSessionImport,
    SessionImportNotFoundError,
)

_PI_IMPORT_SESSION_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
_OPENCODE_IMPORT_SESSION_ID_RE = re.compile(r"ses_[A-Za-z0-9_-]+")
_MAX_EXTERNAL_SESSION_ID_LENGTH = 128
_MAX_RESPONSE_ID_LENGTH = 64
_OPENCODE_COMMAND_TIMEOUT_SECONDS = 120


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


def _recent_unique_session_ids(
    candidates: list[tuple[Path, str]],
    *,
    limit: int,
) -> tuple[str, ...]:
    """Return unique session ids ordered from newest transcript to oldest."""
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
    return tuple(ordered[:limit])


def _pi_session_id_from_path(path: Path) -> str | None:
    """Read a safe native session id from a Pi transcript header."""
    try:
        with path.open(encoding="utf-8") as handle:
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
) -> object:
    """Run one public OpenCode JSON command and decode stdout."""
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


def list_recent_local_session_ids(
    source: ImportSource,
    *,
    limit: int,
) -> tuple[str, ...]:
    """List recent parent session ids for one local harness."""
    if source == "claude":
        configured_home = os.environ.get("CLAUDE_CONFIG_DIR")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".claude"
        root = home / "projects"
        candidates = [
            (path, path.stem)
            for path in root.rglob("*.jsonl")
            if "subagents" not in path.parts and path.is_file()
        ]
        return _recent_unique_session_ids(candidates, limit=limit)

    if source == "qwen":
        configured_home = os.environ.get("QWEN_HOME")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".qwen"
        paths = [path for path in (home / "projects").glob("*/chats/*.jsonl") if path.is_file()]
        candidates = [(path, _qwen_session_locator(path)) for path in paths]
        return _recent_unique_session_ids(candidates, limit=limit)

    if source == "kiro":
        root = kiro_cli_sessions_dir()
        candidates = [
            (path, path.stem)
            for path in root.glob("*.jsonl")
            if path.is_file() and path.with_suffix(".json").is_file()
        ]
        return _recent_unique_session_ids(candidates, limit=limit)

    if source == "opencode":
        payload = _run_opencode_json(
            "session",
            "list",
            "--format",
            "json",
            "--pure",
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
        return tuple(ordered[:limit])

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
        return _recent_unique_session_ids(candidates, limit=limit)

    if source == "kimi":
        home = resolve_user_kimi_home()
        candidates = [
            (path, path.parent.parent.parent.name)
            for path in (home / "sessions").glob("*/session_*/agents/main/wire.jsonl")
            if path.is_file()
        ]
        return _recent_unique_session_ids(candidates, limit=limit)

    if source == "codex":
        configured_home = os.environ.get("CODEX_HOME")
        home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        rollouts: list[Path] = []
        sessions = home / "sessions"
        archived_sessions = home / "archived_sessions"
        if sessions.is_dir():
            rollouts.extend(path for path in sessions.glob("**/rollout-*.jsonl") if path.is_file())
        if archived_sessions.is_dir():
            rollouts.extend(
                path for path in archived_sessions.glob("rollout-*.jsonl") if path.is_file()
            )
        candidates = []
        for path in rollouts:
            session_id = path.stem[-36:]
            if _CODEX_THREAD_ID_RE.fullmatch(session_id):
                candidates.append((path, session_id))
        return _recent_unique_session_ids(candidates, limit=limit)

    raise ValueError(f"Unsupported import source: {source}")


def _claude_workspace(transcript_path: Path) -> str | None:
    """Read the first usable cwd recorded in a Claude transcript."""
    with transcript_path.open(encoding="utf-8") as handle:
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

    parsed = read_transcript_items_from_offset(
        transcript_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
    )
    items = tuple(
        NewConversationItem(
            type=item.item_type,
            response_id=item.response_id,
            data=parse_item_data(item.item_type, item.data),
        )
        for item in parsed.items
    )
    if not items:
        raise SessionImportNotFoundError(
            f"Claude Code session {session_id!r} has no importable history"
        )
    return LocalSessionImport(
        source="claude",
        external_session_id=session_id,
        workspace=_claude_workspace(transcript_path),
        items=items,
    )


def _codex_message_data(payload: dict[str, object]) -> dict[str, object] | None:
    """Convert a visible Codex message payload to Omnigent message data."""
    role = payload.get("role")
    if role not in {"user", "assistant"}:
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
        elif role == "user" and block.get("type") in {"input_image", "input_file"}:
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
    elif item_type in {"function_call", "custom_tool_call"}:
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
            normalized_type = "function_call"
    elif item_type in {"function_call_output", "custom_tool_call_output"}:
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


def _find_archived_codex_rollout(codex_home: Path, session_id: str) -> Path | None:
    """Return the newest archived Codex rollout matching a session id."""
    archived_sessions = codex_home / "archived_sessions"
    if not archived_sessions.is_dir():
        return None
    suffix = f"-{session_id}.jsonl"
    matches = [
        path
        for path in archived_sessions.glob("rollout-*.jsonl")
        if path.name.endswith(suffix) and path.is_file()
    ]
    return max(matches, key=lambda path: path.stat().st_mtime) if matches else None


def load_codex_session(
    session_id: str,
    *,
    codex_home: Path | None = None,
) -> LocalSessionImport:
    """Load one Codex session from its local rollout JSONL file."""
    configured_home = os.environ.get("CODEX_HOME")
    home = codex_home or (Path(configured_home).expanduser() if configured_home else None)
    home = home or Path.home() / ".codex"
    rollout_path = _find_codex_rollout(home, session_id) or _find_archived_codex_rollout(
        home, session_id
    )
    if rollout_path is None:
        raise SessionImportNotFoundError(f"Codex session {session_id!r} was not found")

    workspace: str | None = None
    turn_id = "history"
    items: list[NewConversationItem] = []
    with rollout_path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
                continue
            payload = record["payload"]
            if record.get("type") == "session_meta":
                cwd = payload.get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    workspace = cwd.strip()
                continue
            if record.get("type") == "turn_context":
                candidate = payload.get("turn_id")
                if isinstance(candidate, str) and candidate:
                    turn_id = candidate
                continue
            if record.get("type") != "response_item":
                continue
            item = _codex_response_item(payload, response_id=f"codex:{turn_id}")
            if item is not None:
                items.append(item)

    if not items:
        raise SessionImportNotFoundError(f"Codex session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="codex",
        external_session_id=session_id,
        workspace=workspace,
        items=tuple(items),
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

    records: list[dict[str, object]] = []
    with transcript_path.open(encoding="utf-8") as handle:
        for line in handle:
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
        raise SessionImportNotFoundError(
            f"Qwen Code session {session_id!r} has no importable history"
        )
    return LocalSessionImport(
        source="qwen",
        external_session_id=_qwen_session_locator(transcript_path),
        workspace=workspace,
        items=tuple(items),
    )


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
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SessionImportNotFoundError(
            f"Kiro session {session_id!r} has unreadable metadata"
        ) from exc
    workspace_value = metadata.get("cwd") if isinstance(metadata, dict) else None
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    try:
        messages = [
            message
            for line in transcript_path.read_text(encoding="utf-8").splitlines()
            if (message := parse_kiro_jsonl_line(line)) is not None
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
        raise SessionImportNotFoundError(f"Kiro session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="kiro",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )


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
    if role not in {"user", "assistant"}:
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
    records: list[dict[str, object]] = []
    with transcript_path.open(encoding="utf-8") as handle:
        for line in handle:
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
        raise SessionImportNotFoundError(f"Pi session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="pi",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )


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
    mirrored = read_kimi_wire_items(wire_path, 0)
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
        raise SessionImportNotFoundError(f"Kimi session {session_id!r} has no importable history")
    return LocalSessionImport(
        source="kimi",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )


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
    if role not in {"user", "assistant"}:
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
        raise SessionImportNotFoundError(
            f"OpenCode session {session_id!r} has no importable history"
        )
    workspace_value = info.get("directory") if isinstance(info, dict) else None
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    return LocalSessionImport(
        source="opencode",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
    )


def load_local_session(source: ImportSource, session_id: str) -> LocalSessionImport:
    """Load one local session from the selected first-party harness."""
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
