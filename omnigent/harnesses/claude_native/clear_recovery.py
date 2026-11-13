"""Retry completed native rotations' notifications and detach after a restart."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import httpx

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    _write_json_file,
    url_component,
)
from omnigent.native._native_supersession import post_clear_supersession

_logger = logging.getLogger(__name__)
_PENDING_DIRECTORY = "pending_clear_supersessions"


def record_clear_supersession(bridge_dir: Path, old_session_id: str, new_session_id: str) -> None:
    """Persist cleanup after rotation work, before consuming its hook record."""
    if old_session_id == new_session_id:
        return
    pair = [old_session_id, new_session_id]
    key = hashlib.sha256(json.dumps(pair).encode()).hexdigest()
    _write_json_file(
        bridge_dir / _PENDING_DIRECTORY / f"{key}.json",
        {"old_session_id": old_session_id, "new_session_id": new_session_id},
    )


def pending_clear_supersessions(bridge_dir: Path) -> list[tuple[Path, str, str]]:
    records = []
    for path in sorted((bridge_dir / _PENDING_DIRECTORY).glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            old = value.get("old_session_id") if isinstance(value, dict) else None
            new = value.get("new_session_id") if isinstance(value, dict) else None
            if not isinstance(old, str) or not old or not isinstance(new, str) or not new:
                raise ValueError("Invalid pending clear supersession")
            records.append((path, old, new))
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            _logger.warning("Could not read pending clear cleanup %s", path, exc_info=True)
    return records


async def replay_clear_supersessions(
    client: httpx.AsyncClient, pending: list[tuple[Path, str, str]], *, agent_name: str
) -> None:
    """Retry cleanup independently of the hook cursor and current active session."""
    for path, old, new in pending:
        try:
            rekey = await client.patch(
                f"/v1/sessions/{url_component(old)}",
                json={"labels": {BRIDGE_ID_LABEL_KEY: f"{old}-cleared"}},
            )
            if rekey.status_code in (403, 404):
                completed = True
            else:
                rekey.raise_for_status()
                completed = await post_clear_supersession(
                    client,
                    old_session_id=old,
                    new_session_id=new,
                    agent_name=agent_name,
                    detach_runner=True,
                )
        except (httpx.HTTPError, ConnectionError):
            _logger.warning("Could not replay clear cleanup for %s", old, exc_info=True)
            continue
        if completed:
            try:
                await asyncio.to_thread(path.unlink, missing_ok=True)
            except OSError:
                _logger.warning("Could not acknowledge clear cleanup %s", path, exc_info=True)
