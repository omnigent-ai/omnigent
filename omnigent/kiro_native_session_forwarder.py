"""Structured session forwarder for the kiro-native harness.

Kiro CLI persists chat turns under ``~/.kiro/sessions/cli`` as session metadata
plus JSONL message records. The native Kiro terminal path injects web prompts
into the TUI; this forwarder mirrors Kiro's persisted assistant messages back
into the Omnigent conversation with ``external_conversation_item`` events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from omnigent.kiro_native_bridge import write_forwarder_ready

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 0.7
_POST_TIMEOUT_S = 30.0
_DISCOVERY_SKEW_MS = 10_000
_STATE_FILE = "kiro_session_forwarder.json"

_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0


@dataclass
class _ForwardState:
    """Durable cursor for one Kiro JSONL session file."""

    session_id: str | None = None
    byte_offset: int = 0


@dataclass(frozen=True)
class _KiroConversationMessage:
    """One conversation message parsed from Kiro's JSONL store."""

    message_id: str
    role: str
    text: str


def _kiro_cli_sessions_dir(home: Path | None = None) -> Path:
    """Return Kiro CLI's session directory for this user."""
    return (home or Path.home()) / ".kiro" / "sessions" / "cli"


def _read_state(bridge_dir: Path) -> _ForwardState:
    """Load the persisted forward cursor, or a cold default."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _ForwardState()
    session_id = data.get("session_id")
    byte_offset = data.get("byte_offset")
    return _ForwardState(
        session_id=session_id if isinstance(session_id, str) and session_id else None,
        byte_offset=byte_offset if isinstance(byte_offset, int) and byte_offset >= 0 else 0,
    )


def _write_state(bridge_dir: Path, state: _ForwardState) -> None:
    """Persist the forward cursor atomically."""
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(bridge_dir, 0o700)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(
        json.dumps({"session_id": state.session_id, "byte_offset": state.byte_offset}),
        encoding="utf-8",
    )
    os.replace(tmp, bridge_dir / _STATE_FILE)


def _parse_iso_epoch_ms(value: object) -> int:
    """Parse Kiro's ISO timestamp string into epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp() * 1000)


def _same_workspace(left: object, right: str) -> bool:
    """Return whether Kiro metadata cwd matches the runner workspace."""
    if not isinstance(left, str) or not left:
        return False
    try:
        return Path(left).expanduser().resolve() == Path(right).expanduser().resolve()
    except OSError:
        return left == right


# Competing-candidate sets already warned about, so the ambiguous branch logs
# once per distinct collision instead of every poll tick for the session's life.
# ponytail: grows by one entry per distinct concurrent collision — too rare to
# bound; add a cap only if a pathological churn ever shows up.
_ambiguous_discovery_warned: set[frozenset[str]] = set()


def _warn_ambiguous_discovery(workspace: str, session_ids: list[str]) -> None:
    """Warn once per distinct set of competing same-workspace Kiro sessions.

    Discovery returns ``None`` for both "no candidate yet" (transient) and ">=2
    candidates" (won't ever bind until one goes away). This makes the latter
    diagnosable without flooding logs on every ~0.7s poll.
    """
    key = frozenset(session_ids)
    if key in _ambiguous_discovery_warned:
        return
    _ambiguous_discovery_warned.add(key)
    _logger.warning(
        "kiro session discovery ambiguous; %d same-workspace sessions above the "
        "launch floor, binding none to avoid cross-talk; workspace=%s sessions=%s",
        len(session_ids),
        workspace,
        sorted(session_ids),
    )


def _discover_kiro_session_jsonl(
    *,
    workspace: str,
    launch_epoch_ms: int,
    sessions_dir: Path | None = None,
) -> tuple[str, Path] | None:
    """Find this Omnigent session's Kiro JSONL file — only when it's unambiguous.

    Candidates are Kiro sessions in the same workspace (``cwd``) with a parseable
    ``created_at`` at/after the launch floor (``launch_epoch_ms`` minus a small
    skew, which already drops pre-existing sessions). We bind **only when exactly
    one** session qualifies.

    Each Kiro session is its own JSONL keyed by Kiro's minted id, so two fresh
    sessions launched in the same workspace within the skew window both qualify.
    Picking newest-by-``updated_at`` (the old behaviour) would latch onto
    whichever session most recently emitted a turn — i.e. it can bind the *other*
    session's transcript and silently cross-talk it into this conversation. With
    two or more candidates we can't tell which JSONL is ours, so we return
    ``None`` and retry rather than guess (logged once via
    :func:`_warn_ambiguous_discovery` so the ambiguous case is distinct from "not
    written yet"). A brief delay is safe; mirroring the wrong conversation is not.
    Mirrors cursor-native's "bind only when exactly one chat qualifies"
    (:func:`omnigent.cursor_native_forwarder._discover_store`).

    The resume/fork path doesn't reach here: when the Kiro id is already known the
    caller binds it directly via :func:`_kiro_session_jsonl_for_id`.
    """
    root = sessions_dir or _kiro_cli_sessions_dir()
    if not root.is_dir():
        return None
    floor_ms = max(0, launch_epoch_ms - _DISCOVERY_SKEW_MS)
    candidates: list[tuple[str, Path]] = []
    for metadata_path in root.glob("*.json"):
        session_id = metadata_path.stem
        jsonl_path = root / f"{session_id}.jsonl"
        if not jsonl_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(metadata, dict) or not _same_workspace(metadata.get("cwd"), workspace):
            continue
        created_ms = _parse_iso_epoch_ms(metadata.get("created_at"))
        # Require a parseable created_at at/after the floor. A fresh session always
        # stamps created_at, so a missing/garbled one can't be confirmed in-window;
        # dropping it stops an undateable straggler from poisoning the "exactly one"
        # count and silently blocking discovery forever.
        if not created_ms or created_ms < floor_ms:
            continue
        candidates.append((session_id, jsonl_path))
    if len(candidates) > 1:
        _warn_ambiguous_discovery(workspace, [session_id for session_id, _ in candidates])
        return None
    return candidates[0] if candidates else None


def _kiro_session_jsonl_for_id(
    session_id: str,
    *,
    workspace: str,
    sessions_dir: Path | None = None,
) -> Path | None:
    """Return the JSONL path for a known Kiro session id, if it is usable."""
    root = sessions_dir or _kiro_cli_sessions_dir()
    metadata_path = root / f"{session_id}.json"
    jsonl_path = root / f"{session_id}.jsonl"
    if not jsonl_path.is_file():
        return None
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(metadata, dict) or not _same_workspace(metadata.get("cwd"), workspace):
        return None
    return jsonl_path


def _read_new_kiro_messages(
    jsonl_path: Path,
    byte_offset: int,
) -> tuple[list[_KiroConversationMessage], int]:
    """Read conversation messages after *byte_offset* from Kiro's JSONL file."""
    messages: list[_KiroConversationMessage] = []
    try:
        with jsonl_path.open("rb") as handle:
            handle.seek(byte_offset)
            # Advance only past newline-terminated lines: Kiro appends to this
            # JSONL live, so the final line may be a record mid-write (no
            # trailing ``\n``). Persisting ``handle.tell()`` past such a partial
            # line would skip the record once Kiro finishes writing it. Hold the
            # offset at the last complete line and re-read the tail next poll.
            offset = byte_offset
            for raw_line in handle:
                if not raw_line.endswith(b"\n"):
                    break
                offset += len(raw_line)
                try:
                    line = raw_line.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                message = _parse_kiro_jsonl_line(line)
                if message is not None:
                    messages.append(message)
            return messages, offset
    except OSError:
        return [], byte_offset


def _parse_kiro_jsonl_line(line: str) -> _KiroConversationMessage | None:
    """Parse one Kiro JSONL line into a mirrorable conversation message."""
    try:
        record = json.loads(line)
    except ValueError:
        return None
    if not isinstance(record, dict):
        return None
    kind = record.get("kind")
    if kind == "Prompt":
        role = "user"
    elif kind == "AssistantMessage":
        role = "assistant"
    else:
        return None
    data = record.get("data")
    if not isinstance(data, dict):
        return None
    message_id = data.get("message_id")
    if not isinstance(message_id, str) or not message_id:
        return None
    text = _kiro_content_text(data.get("content")).strip()
    if not text:
        return None
    return _KiroConversationMessage(message_id=message_id, role=role, text=text)


def _kiro_content_text(content: object) -> str:
    """Join text blocks from Kiro's persisted message content."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("kind") == "text" and isinstance(block.get("data"), str):
            parts.append(block["data"])
        elif block.get("type") in {"text", "output_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


async def _post_conversation_message(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    agent_name: str,
    message: _KiroConversationMessage,
) -> None:
    """POST one Kiro message as an external conversation item."""
    if message.role == "assistant":
        item_data = {
            "role": "assistant",
            "agent": agent_name,
            "content": [{"type": "output_text", "text": message.text}],
        }
    else:
        item_data = {
            "role": "user",
            "content": [{"type": "input_text", "text": message.text}],
        }
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": item_data,
                "response_id": f"kiro:{message.message_id}",
            },
        },
    )
    resp.raise_for_status()


async def _patch_external_session_id(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    external_session_id: str,
) -> None:
    """Persist Kiro's native CLI session id onto the Omnigent session."""
    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": external_session_id},
    )
    # The server rejects overwrites with a different id. Forwarding must keep
    # running in that case; losing chat mirroring would be worse than failing to
    # improve cold resume for an already-conflicted session.
    if resp.status_code >= 400:
        _logger.warning(
            "AP rejected Kiro external_session_id PATCH (%s); session=%s kiro_session=%s",
            resp.status_code,
            session_id,
            external_session_id,
        )
        return


def _read_kiro_cumulative_credits(metadata_path: Path) -> tuple[float | None, str | None]:
    """Sum the per-turn credit metering from a Kiro session ``.json`` snapshot.

    kiro-cli meters in credits, not tokens: each turn under
    ``session_state.conversation_metadata.user_turn_metadatas`` carries a
    ``metering_usage`` list of ``{"value": <float>, "unit": "credit"}`` entries
    (token counts are 0), and the CLI shows a per-turn ``Credits:`` line. The
    cumulative session cost is the sum of every turn's credit values. This data
    lives only in the ``.json`` snapshot, not the ``.jsonl`` transcript the
    forwarder tails.

    :returns: ``(cumulative_credits, model_id)``, or ``(None, None)`` when the
        file is missing/unparseable or carries no metering yet.
    """
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    try:
        data = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(data, dict):
        return None, None
    session_state = data.get("session_state")
    if not isinstance(session_state, dict):
        return None, None
    conversation_metadata = session_state.get("conversation_metadata")
    turns = (
        conversation_metadata.get("user_turn_metadatas")
        if isinstance(conversation_metadata, dict)
        else None
    )
    if not isinstance(turns, list):
        return None, None
    total = 0.0
    saw_credit = False
    for turn in turns:
        if not isinstance(turn, dict):
            continue
        metering = turn.get("metering_usage")
        if not isinstance(metering, list):
            continue
        for entry in metering:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            if isinstance(value, int | float) and not isinstance(value, bool):
                total += float(value)
                saw_credit = True
    if not saw_credit:
        return None, None
    model_id: str | None = None
    rts_model_state = session_state.get("rts_model_state")
    if isinstance(rts_model_state, dict):
        model_info = rts_model_state.get("model_info")
        if isinstance(model_info, dict):
            candidate = model_info.get("model_id")
            if isinstance(candidate, str) and candidate:
                model_id = candidate
    return total, model_id


def _read_kiro_current_model(metadata_path: Path) -> str | None:
    """Return kiro's current model id from the session ``.json`` snapshot.

    Reads ``session_state.rts_model_state.model_info.model_id`` (e.g. ``"auto"``
    or ``"claude-haiku-4.5"``), independent of the metering read above so it is
    available at launch before any turn. kiro updates it in place on a ``/model``
    switch, so polling it mirrors both the launched model and live TUI switches.

    :returns: The model id, or ``None`` when the file is missing/unparseable or
        carries no model state yet.
    """
    try:
        raw = metadata_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    session_state = data.get("session_state")
    if not isinstance(session_state, dict):
        return None
    rts_model_state = session_state.get("rts_model_state")
    if not isinstance(rts_model_state, dict):
        return None
    model_info = rts_model_state.get("model_info")
    if not isinstance(model_info, dict):
        return None
    model_id = model_info.get("model_id")
    return model_id if isinstance(model_id, str) and model_id else None


async def _post_external_model_change(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    model: str,
) -> None:
    """Mirror kiro's current model to the web as ``external_model_change``.

    The server persists this as ``model_override`` (so the picker shows the real
    model instead of falling back to the harness name) and deliberately does NOT
    forward a ``/model`` back to the runner, so mirroring the model the TUI is
    already on cannot loop. Mirrors cursor-native's terminal->web model mirror.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_model_change", "data": {"model": model}},
    )
    resp.raise_for_status()


async def _post_session_cost(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    cumulative_cost_usd: float,
    model: str | None,
) -> None:
    """POST Kiro's cumulative credit spend as authoritative session cost.

    kiro-cli reports cost in credits and there is no credit->USD conversion
    available, so credits are forwarded 1:1 into ``cumulative_cost_usd`` (the
    same convention the Copilot relay uses for its AI-credit total). The server
    treats this value as authoritative and monotonic, in preference to
    token x catalog pricing, via the ``external_session_usage`` event used by
    the claude-/codex-native forwarders.
    """
    data: dict[str, object] = {"cumulative_cost_usd": cumulative_cost_usd}
    if model:
        data["model"] = model
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_session_usage", "data": data},
    )
    resp.raise_for_status()


async def forward_kiro_session_to_omnigent(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    expected_session_id: str | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Tail Kiro's session JSONL and mirror assistant messages into AP."""
    state = _read_state(bridge_dir)
    jsonl_path: Path | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    mirrored_external_session_id: str | None = None
    last_posted_cost: float | None = None
    last_posted_model: str | None = None
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if state.session_id is None or jsonl_path is None or not jsonl_path.exists():
                    discovered: tuple[str, Path] | None = None
                    if expected_session_id:
                        expected_path = await asyncio.to_thread(
                            _kiro_session_jsonl_for_id,
                            expected_session_id,
                            workspace=workspace,
                        )
                        if expected_path is not None:
                            discovered = (expected_session_id, expected_path)
                    elif discovered is None:
                        discovered = await asyncio.to_thread(
                            _discover_kiro_session_jsonl,
                            workspace=workspace,
                            launch_epoch_ms=launch_epoch_ms,
                        )
                    if discovered is not None:
                        discovered_session_id, discovered_path = discovered
                        if state.session_id != discovered_session_id:
                            state = _ForwardState(session_id=discovered_session_id, byte_offset=0)
                            _write_state(bridge_dir, state)
                        jsonl_path = discovered_path
                if jsonl_path is not None and state.session_id is not None:
                    if mirrored_external_session_id != state.session_id:
                        await _patch_external_session_id(
                            client,
                            session_id=session_id,
                            external_session_id=state.session_id,
                        )
                        mirrored_external_session_id = state.session_id
                    messages, byte_offset = await asyncio.to_thread(
                        _read_new_kiro_messages,
                        jsonl_path,
                        state.byte_offset,
                    )
                    for message in messages:
                        # Mirror the transcript only. Running/idle status for
                        # kiro-native is owned by the PTY watcher's ``emit_status``
                        # (``runner/resource_registry.py``), matching the other
                        # forwarder-backed native harnesses (goose/qwen/hermes),
                        # whose forwarders mirror the transcript, not the
                        # running/idle session status. Posting status here too
                        # double-sourced it (#1137).
                        await _post_conversation_message(
                            client,
                            session_id=session_id,
                            agent_name=agent_name,
                            message=message,
                        )
                    if byte_offset != state.byte_offset:
                        state.byte_offset = byte_offset
                        _write_state(bridge_dir, state)
                    write_forwarder_ready(bridge_dir)
                    # Forward kiro's credit metering as authoritative session
                    # cost. It lives in the ``.json`` snapshot (sibling of the
                    # tailed ``.jsonl``), not the transcript, and is cumulative;
                    # the server treats ``cumulative_cost_usd`` as monotonic, so
                    # only post when it advances.
                    cumulative_cost, cost_model = await asyncio.to_thread(
                        _read_kiro_cumulative_credits, jsonl_path.with_suffix(".json")
                    )
                    if cumulative_cost is not None and (
                        last_posted_cost is None or cumulative_cost > last_posted_cost
                    ):
                        await _post_session_cost(
                            client,
                            session_id=session_id,
                            cumulative_cost_usd=cumulative_cost,
                            model=cost_model,
                        )
                        last_posted_cost = cumulative_cost
                    # Mirror kiro's current model so the web picker shows the real
                    # model (not the harness name) at launch and after a live
                    # ``/model`` switch. Read independently of metering so it is
                    # available before the first turn; posted only when it changes.
                    current_model = await asyncio.to_thread(
                        _read_kiro_current_model, jsonl_path.with_suffix(".json")
                    )
                    if current_model is not None and current_model != last_posted_model:
                        await _post_external_model_change(
                            client, session_id=session_id, model=current_model
                        )
                        last_posted_model = current_model
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "kiro session forwarder poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


def _supervisor_monotonic() -> float:
    """Indirection so tests can stub the supervisor clock."""
    return time.monotonic()


async def _supervisor_sleep(seconds: float) -> None:
    """Indirection so tests can stub supervisor sleep."""
    await asyncio.sleep(seconds)


async def supervise_kiro_session_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    agent_name: str,
    workspace: str,
    launch_epoch_ms: int,
    expected_session_id: str | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run the Kiro session forwarder under a restart supervisor."""
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = _supervisor_monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_kiro_session_to_omnigent(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name=agent_name,
                workspace=workspace,
                launch_epoch_ms=launch_epoch_ms,
                expected_session_id=expected_session_id,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
            _logger.warning(
                "kiro session forwarder returned unexpectedly; restarting; "
                "session=%s bridge_dir=%s",
                session_id,
                bridge_dir,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - supervisor restarts on any crash
            crash_exc = exc
        if _supervisor_monotonic() - run_started_at >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "kiro session forwarder crashed; restarting in %.1fs; session=%s bridge_dir=%s",
                backoff_s,
                session_id,
                bridge_dir,
                exc_info=crash_exc,
            )
        await _supervisor_sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _SUPERVISOR_MAX_BACKOFF_S)


__all__ = [
    "forward_kiro_session_to_omnigent",
    "supervise_kiro_session_forwarder",
]
