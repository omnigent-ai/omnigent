"""Post-hoc tool-RESULT policy audit for goose-native (observability only).

goose has no post-execution hook, so Omnigent cannot BLOCK a tool result on
native — goose has already returned it to the model by the time the
``toolResponse`` row appears in the store. But it can still AUDIT it: when a tool
completes, evaluate it against Omnigent's tool-RESULT policy
(``PHASE_TOOL_RESULT``) and record the decision, logging a warning when a result
would have been denied. This is the honest best-effort for the result checkpoint
on a TUI-mirror harness; the tool-CALL checkpoint IS enforced live (the goose
``PreToolUse`` policy hook, :mod:`omnigent.inner.goose_policy_hook`).

``POST /policies/evaluate`` parks an approval gate only for the TOOL_CALL /
LLM_REQUEST / REQUEST phases — NOT TOOL_RESULT — so this evaluation is
side-effect-free: it returns the verdict and records the evaluation without ever
prompting for a result that already ran.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from pathlib import Path

import httpx

from omnigent.goose_native_forwarder import (
    _connect_ro,
    _resolve_goose_session_id,
    _tool_request_from_part,
    default_sessions_db,
)
from omnigent.native_policy_hook import hook_payload_to_evaluation_request

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 1.0
_POST_TIMEOUT_S = 30.0
_STATE_FILE = "goose_audit_forwarder.json"
# How far back to scan for the toolRequest matching a new toolResponse (request
# and response are adjacent within a turn, so a small window suffices).
_REQUEST_SCAN_LIMIT = 80

_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0

_ACTION_ALLOW = "POLICY_ACTION_ALLOW"
_ACTION_UNSPECIFIED = "POLICY_ACTION_UNSPECIFIED"


def _result_text(part: dict[str, object]) -> str:
    """Best-effort flatten of a ``toolResponse`` part's result content to text."""
    result = part.get("toolResult")
    if not isinstance(result, dict):
        return ""
    value = result.get("value")
    if not isinstance(value, dict):
        return ""
    content = value.get("content")
    chunks: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    chunks.append(text)
    return "".join(chunks).strip()


def _request_map(con: sqlite3.Connection, goose_session_id: str) -> dict[str, object]:
    """Map ``toolRequest`` id → its parsed call, scanning recent messages."""
    out: dict[str, object] = {}
    try:
        rows = con.execute(
            "SELECT content_json FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (goose_session_id, _REQUEST_SCAN_LIMIT),
        ).fetchall()
    except sqlite3.Error:
        return out
    for (content_json,) in rows:
        if not isinstance(content_json, str):
            continue
        try:
            obj = json.loads(content_json)
        except ValueError:
            continue
        for part in obj if isinstance(obj, list) else [obj]:
            call = _tool_request_from_part(part)
            if call is not None and call.request_id not in out:
                out[call.request_id] = call
    return out


def read_new_tool_results(
    db_path: Path, goose_session_id: str, last_id: int
) -> list[tuple[int, str, dict[str, object], str]]:
    """Return ``(msg_id, tool_name, arguments, result_text)`` per new tool result.

    Reads ``toolResponse`` rows past *last_id* and correlates each to its
    ``toolRequest`` (same id) for the tool name + arguments. Responses with no
    matching request in the scan window are skipped (nothing to audit against).
    """
    con = _connect_ro(db_path)
    if con is None:
        return []
    try:
        requests = _request_map(con, goose_session_id)
        try:
            rows = con.execute(
                "SELECT id, content_json FROM messages "
                "WHERE session_id = ? AND id > ? ORDER BY id",
                (goose_session_id, last_id),
            ).fetchall()
        except sqlite3.Error:
            return []
    finally:
        con.close()
    out: list[tuple[int, str, dict[str, object], str]] = []
    for msg_id, content_json in rows:
        if not isinstance(msg_id, int) or not isinstance(content_json, str):
            continue
        try:
            obj = json.loads(content_json)
        except ValueError:
            continue
        for part in obj if isinstance(obj, list) else [obj]:
            if not isinstance(part, dict) or part.get("type") != "toolResponse":
                continue
            rid = part.get("id")
            if not isinstance(rid, str):
                continue
            call = requests.get(rid)
            if call is None:
                continue
            out.append((msg_id, call.name, call.arguments, _result_text(part)))  # type: ignore[attr-defined]
    return out


def _read_last_id(bridge_dir: Path) -> int:
    """Load the high-water audited ``messages.id``, or 0 (cold)."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    last = data.get("last_id") if isinstance(data, dict) else None
    return last if isinstance(last, int) else 0


def _write_last_id(bridge_dir: Path, last_id: int) -> None:
    """Atomically persist the high-water audited id (tmp write + rename)."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps({"last_id": last_id}), encoding="utf-8")
    os.replace(tmp, bridge_dir / _STATE_FILE)


def clear_goose_audit_state(bridge_dir: Path) -> None:
    """Remove persisted audit state so a re-created terminal starts clean."""
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


async def _audit_one(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    tool_name: str,
    arguments: dict[str, object],
    result_text: str,
) -> None:
    """Evaluate one completed tool result against tool-RESULT policy (no gating)."""
    body = hook_payload_to_evaluation_request(
        "PostToolUse",
        {"tool_name": tool_name, "tool_input": arguments, "tool_output": result_text},
    )
    if body is None:
        return  # not policy-relevant here (e.g. an Omnigent-MCP tool)
    try:
        response = await client.post(f"/v1/sessions/{session_id}/policies/evaluate", json=body)
    except httpx.HTTPError:
        _logger.debug("goose result audit POST failed; session=%s tool=%s", session_id, tool_name)
        return
    if response.status_code >= 400 or not response.content:
        return
    try:
        verdict = response.json()
    except ValueError:
        return
    action = verdict.get("result") if isinstance(verdict, dict) else None
    if action in (_ACTION_ALLOW, _ACTION_UNSPECIFIED, None):
        return
    # Cannot block on native (goose already returned the result to the model);
    # surface it as an audit warning. The evaluation itself is recorded server-side.
    _logger.warning(
        "goose-native result-phase policy %s for tool %r (cannot block on native — "
        "result already returned); session=%s reason=%s",
        action,
        tool_name,
        session_id,
        (verdict.get("reason") if isinstance(verdict, dict) else None),
    )


async def forward_goose_audit_for_session(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    goose_session_name: str,
    db_path: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Poll for completed tool results and audit each against tool-RESULT policy.

    Never returns normally; cancel the task to stop it.
    """
    db = db_path or default_sessions_db()
    last_id = _read_last_id(bridge_dir)
    goose_session_id: str | None = None
    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    async with httpx.AsyncClient(
        base_url=base_url, headers=headers, auth=auth, timeout=timeout
    ) as client:
        while True:
            try:
                if goose_session_id is None:
                    goose_session_id = await asyncio.to_thread(
                        _resolve_goose_session_id, db, goose_session_name
                    )
                if goose_session_id is not None:
                    results = await asyncio.to_thread(
                        read_new_tool_results, db, goose_session_id, last_id
                    )
                    for msg_id, tool_name, arguments, result_text in results:
                        await _audit_one(
                            client,
                            session_id=session_id,
                            tool_name=tool_name,
                            arguments=arguments,
                            result_text=result_text,
                        )
                        last_id = max(last_id, msg_id)
                        await asyncio.to_thread(_write_last_id, bridge_dir, last_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "goose result audit poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def supervise_goose_audit_forwarder(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    goose_session_name: str,
    db_path: Path | None = None,
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    auth: httpx.Auth | None = None,
) -> None:
    """Run :func:`forward_goose_audit_for_session` under a restart supervisor."""
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = time.monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_goose_audit_for_session(
                base_url=base_url,
                headers=headers,
                session_id=session_id,
                bridge_dir=bridge_dir,
                goose_session_name=goose_session_name,
                db_path=db_path,
                poll_interval_s=poll_interval_s,
                auth=auth,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — supervisor restarts on any Exception
            crash_exc = exc
        if time.monotonic() - run_started_at >= _SUPERVISOR_HEALTHY_UPTIME_S:
            backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
        if crash_exc is not None:
            _logger.error(
                "goose result audit crashed; restarting in %.1fs; session=%s",
                backoff_s,
                session_id,
                exc_info=crash_exc,
            )
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
