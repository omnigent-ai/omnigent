"""Bridge protected native quota-wait files into Omnigent session status."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import stat
import time
from pathlib import Path

import httpx

from omnigent._native_post_delivery import post_external_session_status

_SESSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}")
_WAIT_TTL_SECONDS = 90.0


def quota_wait_path(directory: Path, session_id: str) -> Path:
    """Return the non-identifying filename shared with the native hook."""
    return directory / f"{hashlib.sha256(session_id.encode()).hexdigest()}.json"


def _read_wait(path: Path, *, runner_id: str, now: float) -> tuple[str, dict[str, object]] | None:
    try:
        metadata = path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > 16_384
        ):
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) - {
        "schema_version",
        "runner_id",
        "session_id",
        "state",
        "admission_not_before",
        "admission_delay_seconds",
        "current_rate_ppm_per_second",
        "burst_multiplier",
        "linear_schedule_delta_ppm",
        "updated_at",
    }:
        return None
    session_id = payload.get("session_id")
    updated_at = payload.get("updated_at")
    if (
        payload.get("schema_version") != 1
        or payload.get("runner_id") != runner_id
        or payload.get("state") != "waiting"
        or not isinstance(session_id, str)
        or _SESSION.fullmatch(session_id) is None
        or quota_wait_path(path.parent, session_id) != path
        or not isinstance(updated_at, (int, float))
        or isinstance(updated_at, bool)
        or not 0 <= now - float(updated_at) <= _WAIT_TTL_SECONDS
    ):
        return None
    quota_wait: dict[str, object] = {}
    for key in (
        "admission_delay_seconds",
        "current_rate_ppm_per_second",
        "burst_multiplier",
    ):
        value = payload.get(key)
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                or (key == "burst_multiplier" and value < 1)
            ):
                return None
            quota_wait[key] = float(value)
    delta = payload.get("linear_schedule_delta_ppm")
    if delta is not None:
        if isinstance(delta, bool) or not isinstance(delta, int):
            return None
        quota_wait["linear_schedule_delta_ppm"] = delta
    return session_id, quota_wait


async def watch_quota_waits(
    client: httpx.AsyncClient,
    directory: Path,
    *,
    runner_id: str,
    poll_seconds: float = 0.5,
    iterations: int | None = None,
    previously_waiting: set[str] | None = None,
) -> None:
    """Publish changed waits and a clearing running edge after file removal."""
    waiting = set() if previously_waiting is None else set(previously_waiting)
    completed = 0
    while iterations is None or completed < iterations:
        current: dict[str, dict[str, object]] = {}
        now = time.time()
        try:
            metadata = directory.lstat()
            protected = (
                stat.S_ISDIR(metadata.st_mode)
                and metadata.st_uid == os.geteuid()
                and not metadata.st_mode & 0o077
            )
            paths = list(directory.glob("*.json")) if protected else []
        except OSError:
            paths = []
        for path in paths:
            result = _read_wait(path, runner_id=runner_id, now=now)
            if result is not None:
                session_id, quota_wait = result
                current[session_id] = quota_wait
        for session_id, quota_wait in current.items():
            try:
                await post_external_session_status(
                    client,
                    session_id=session_id,
                    status="running",
                    blocked_on="quota pacing",
                    quota_wait=quota_wait,
                )
            except httpx.HTTPError:
                continue
        for session_id in waiting - current.keys():
            try:
                await post_external_session_status(client, session_id=session_id, status="running")
            except httpx.HTTPError:
                continue
        waiting = set(current)
        completed += 1
        if iterations is None or completed < iterations:
            await asyncio.sleep(poll_seconds)


__all__ = ["quota_wait_path", "watch_quota_waits"]
