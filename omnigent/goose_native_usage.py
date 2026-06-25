"""Session token + cost usage forwarder for the goose-native harness.

Unlike cursor-native (which needs a ``stop`` hook to surface usage), goose
persists running totals directly on its ``sessions`` row:
``accumulated_input_tokens`` / ``accumulated_output_tokens`` /
``accumulated_total_tokens`` and ``accumulated_cost`` — goose's OWN priced cost.
So this runner-owned poller just reads that row for the launch's session and
POSTs an ``external_session_usage`` event — the same server contract
claude-/codex-/cursor-native use — so the web UI's session-cost badge and
per-model token breakdown light up with no frontend changes.

Because goose gives an authoritative cost, we forward it directly as
``cumulative_cost_usd`` (display) and ``policy_cost_usd`` (the cost-budget
enforcement gate); the cumulative tokens + model ride along so the server can
still render the token breakdown (and re-price if the model is in its catalog).

The poller binds to exactly this launch's ``sessions`` row (resolved by the
unique ``--name``), whose ``accumulated_*`` columns are monotonic for the life of
the row — so there is no per-turn summing or generation-id dedup to do (cursor
needs both); we simply POST whenever the row's totals advance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.goose_native_forwarder import (
    _connect_ro,
    _resolve_goose_session_id,
    default_sessions_db,
)

_logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_S = 1.0
_POST_TIMEOUT_S = 30.0
_STATE_FILE = "goose_usage_forwarder.json"

# Supervisor backoff (mirrors cursor_native_usage.supervise_cursor_usage_forwarder).
_SUPERVISOR_INITIAL_BACKOFF_S = 1.0
_SUPERVISOR_MAX_BACKOFF_S = 30.0
_SUPERVISOR_HEALTHY_UPTIME_S = 60.0


@dataclass(frozen=True)
class GooseUsage:
    """Cumulative usage read from a goose ``sessions`` row.

    :param input_tokens: ``accumulated_input_tokens``.
    :param output_tokens: ``accumulated_output_tokens``.
    :param total_tokens: ``accumulated_total_tokens``.
    :param cost: ``accumulated_cost`` (goose's own priced USD), or ``None``.
    :param model: The model name from ``model_config_json``, or ``None``.
    """

    input_tokens: int
    output_tokens: int
    total_tokens: int
    cost: float | None
    model: str | None

    def post_key(self) -> list[object]:
        """A JSON-stable identity used to POST only when totals actually change."""
        return [self.input_tokens, self.output_tokens, self.total_tokens, self.cost, self.model]

    def has_usage(self) -> bool:
        """Whether there is anything worth reporting yet."""
        return self.total_tokens > 0 or self.input_tokens > 0 or bool(self.cost)


def _model_from_config_json(raw: object) -> str | None:
    """Pull ``model_name`` out of a goose ``model_config_json`` value."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        obj = json.loads(raw)
    except ValueError:
        return None
    if isinstance(obj, dict):
        name = obj.get("model_name") or obj.get("model")
        if isinstance(name, str) and name:
            return name
    return None


def _coerce_int(value: object) -> int:
    """Coerce a SQLite token column to a non-negative int (0 on NULL/odd)."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        return int(value) if value >= 0 else 0
    return 0


def read_session_usage(db_path: Path, goose_session_id: str) -> GooseUsage | None:
    """Read cumulative usage for *goose_session_id* from the ``sessions`` row.

    :returns: A :class:`GooseUsage`, or ``None`` when the row/store is not
        readable yet.
    """
    con = _connect_ro(db_path)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT accumulated_input_tokens, accumulated_output_tokens, "
            "accumulated_total_tokens, accumulated_cost, model_config_json "
            "FROM sessions WHERE id = ?",
            (goose_session_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        con.close()
    if row is None:
        return None
    cost = None
    if row[3] is not None:
        with contextlib.suppress(TypeError, ValueError):
            cost = float(row[3])
    return GooseUsage(
        input_tokens=_coerce_int(row[0]),
        output_tokens=_coerce_int(row[1]),
        total_tokens=_coerce_int(row[2]),
        cost=cost,
        model=_model_from_config_json(row[4]),
    )


def _usage_post_body(usage: GooseUsage) -> dict[str, object]:
    """Build the ``external_session_usage`` ``data`` payload.

    goose's ``accumulated_cost`` is authoritative, so it goes to both
    ``cumulative_cost_usd`` (display) and ``policy_cost_usd`` (the cost-budget
    gate). Tokens + model let the server render the breakdown (and re-price when
    the model is in its catalog). goose does not track cache-read tokens
    separately, so none is sent.
    """
    data: dict[str, object] = {
        "cumulative_input_tokens": usage.input_tokens,
        "cumulative_output_tokens": usage.output_tokens,
    }
    if usage.cost is not None:
        data["cumulative_cost_usd"] = usage.cost
        data["policy_cost_usd"] = usage.cost
    if usage.model:
        data["model"] = usage.model
    return data


def _read_last_key(bridge_dir: Path) -> list[object] | None:
    """Load the last-posted usage key, or ``None`` (cold)."""
    try:
        data = json.loads((bridge_dir / _STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _write_last_key(bridge_dir: Path, key: list[object]) -> None:
    """Atomically persist the last-posted usage key (tmp write + rename)."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    tmp = bridge_dir / (_STATE_FILE + ".tmp")
    tmp.write_text(json.dumps(key), encoding="utf-8")
    os.replace(tmp, bridge_dir / _STATE_FILE)


def clear_goose_usage_state(bridge_dir: Path) -> None:
    """Remove persisted usage state so a re-created terminal starts clean."""
    with contextlib.suppress(OSError):
        (bridge_dir / _STATE_FILE).unlink()


async def forward_goose_usage_to_session(
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
    """Poll this launch's ``sessions`` row and POST cumulative usage to AP.

    Resolves the goose ``sessions.id`` by ``name``, then reads its
    ``accumulated_*`` totals each poll and POSTs an ``external_session_usage``
    event whenever they advance. The last-posted key is persisted to
    ``bridge_dir`` so a supervisor restart resumes without a redundant POST.
    Never returns normally; cancel the task to stop it.
    """
    db = db_path or default_sessions_db()
    last_key = _read_last_key(bridge_dir)
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
                    usage = await asyncio.to_thread(read_session_usage, db, goose_session_id)
                    if usage is not None and usage.has_usage():
                        key = usage.post_key()
                        if key != last_key:
                            resp = await client.post(
                                f"/v1/sessions/{session_id}/events",
                                json={
                                    "type": "external_session_usage",
                                    "data": _usage_post_body(usage),
                                },
                            )
                            resp.raise_for_status()
                            # Persist only after a successful POST so a failed
                            # flush is retried on the next poll.
                            last_key = key
                            await asyncio.to_thread(_write_last_key, bridge_dir, key)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "goose usage forwarder poll failed; session=%s bridge_dir=%s",
                    session_id,
                    bridge_dir,
                )
            await asyncio.sleep(poll_interval_s)


async def supervise_goose_usage_forwarder(
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
    """Run :func:`forward_goose_usage_to_session` under a restart supervisor.

    Mirrors the other goose-native supervisors: per-poll errors are swallowed in
    the loop, but a crash in client setup restarts with bounded exponential
    backoff. :class:`asyncio.CancelledError` propagates for clean teardown; the
    persisted last-posted key means restarts resume exactly.
    """
    backoff_s = _SUPERVISOR_INITIAL_BACKOFF_S
    while True:
        run_started_at = time.monotonic()
        crash_exc: Exception | None = None
        try:
            await forward_goose_usage_to_session(
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
                "goose usage forwarder crashed; restarting in %.1fs; session=%s",
                backoff_s,
                session_id,
                exc_info=crash_exc,
            )
        await asyncio.sleep(backoff_s)
        backoff_s = min(backoff_s * 2.0, _SUPERVISOR_MAX_BACKOFF_S)
