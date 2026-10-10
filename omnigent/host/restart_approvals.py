"""Retire approvals whose runner was lost when its host restarted."""

from __future__ import annotations

import logging
from collections.abc import Callable
from urllib.parse import quote

import httpx

_logger = logging.getLogger(__name__)

RESTART_NOTICE = (
    "The host restarted and interrupted your pending tool approval(s). "
    "The corresponding commands were not run. Their old approval cards are cancelled. "
    "Do not automatically repeat these commands. Wait for a fresh user request and a fresh "
    "approval before running a replacement. Acknowledge this interruption notice."
)


async def retire_restart_approvals(
    client: httpx.AsyncClient,
    *,
    host_id: str,
    started_at: float,
    runner_is_live: Callable[[str], bool],
) -> int:
    """Cancel pre-existing dead-runner approvals, then notify their native session.

    Called once per host process, never for a surviving tunnel reconnect.
    Cancellation uses the existing resolver and cannot replay the old command.
    A notice is submitted once; an ambiguous HTTP failure is not retried.
    """
    notified = 0
    after: str | None = None
    seen_cursors: set[str] = set()
    while True:
        params: dict[str, str | int] = {"kind": "any", "visibility": "all", "limit": 100}
        if after is not None:
            params["after"] = after
        response = await client.get("/v1/sessions", params=params)
        response.raise_for_status()
        page = response.json()
        for row in page.get("data", []):
            if not isinstance(row, dict) or row.get("host_id") != host_id:
                continue
            session_id = row.get("id")
            runner_id = row.get("runner_id")
            created_at = row.get("created_at")
            if (
                not isinstance(session_id, str)
                or not isinstance(created_at, (int, float))
                or created_at >= started_at
                or not row.get("external_session_id")
                or row.get("agent_name") not in ("claude-native-ui", "codex-native-ui")
                or (isinstance(runner_id, str) and runner_is_live(runner_id))
                or not row.get("pending_elicitations_count")
            ):
                continue
            path = f"/v1/sessions/{quote(session_id, safe='')}"
            try:
                snapshot_response = await client.get(path, params={"include_items": "false"})
                if snapshot_response.status_code == 404:
                    continue
                snapshot_response.raise_for_status()
                snapshot = snapshot_response.json()
                current_runner = snapshot.get("runner_id")
                if snapshot.get("host_id") != host_id or (
                    isinstance(current_runner, str) and runner_is_live(current_runner)
                ):
                    continue
                pending = snapshot.get("pending_elicitations") or []
                cancelled = 0
                for approval in pending:
                    if not isinstance(approval, dict):
                        continue
                    approval_params = approval.get("params")
                    target = (
                        approval_params.get("target_session_id")
                        if isinstance(approval_params, dict)
                        else None
                    )
                    if target is not None and target != session_id:
                        # Ancestor snapshots mirror child approvals. Only their owning
                        # session can resolve them and receive the restart notice.
                        continue
                    elicitation_id = approval.get("elicitation_id")
                    if not isinstance(elicitation_id, str) or not elicitation_id:
                        continue
                    result = await client.post(
                        f"{path}/elicitations/{quote(elicitation_id, safe='')}/resolve",
                        json={"action": "cancel"},
                    )
                    result.raise_for_status()
                    cancelled += 1
                if not cancelled:
                    continue
                # Message dispatch resumes the stored native ID; it never replays a tool.
                notice = await client.post(
                    f"{path}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": RESTART_NOTICE}],
                        },
                    },
                )
                notice.raise_for_status()
                notified += 1
                _logger.info("Retired %d restart approvals for session %s", cancelled, session_id)
            except httpx.HTTPError:
                _logger.exception("Could not retire restart approvals for session %s", session_id)
        if not page.get("has_more"):
            return notified
        cursor = page.get("last_id")
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ValueError("Session pagination did not advance during restart recovery")
        seen_cursors.add(cursor)
        after = cursor
