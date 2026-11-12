"""Ensure a ``cline hub`` daemon is running and return its coordinates.

``cline hub start --json --cwd <dir>`` starts (or reuses) the workspace's hub
daemon and prints ``{"url", "authToken", "cwd", ...}`` on stdout. The hub is
a per-workspace shared daemon — sessions, not daemons, are the unit Omnigent
owns, so this never stops a hub it finds already running.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass

_DEFAULT_TIMEOUT_S = 30.0


class ClineHubStartError(RuntimeError):
    """``cline hub start`` failed or returned an unparseable record."""


@dataclass(frozen=True)
class ClineHubEndpoint:
    """A running hub's connection coordinates.

    :param ws_url: ``ws://host:port/hub`` the daemon listens on.
    :param auth_token: Bearer presented as the ``cline-hub-auth.`` subprotocol.
    """

    ws_url: str
    auth_token: str


def resolve_cline_executable() -> str:
    """Locate the ``cline`` binary; raise a clear error when absent."""
    exe = shutil.which("cline")
    if not exe:
        raise ClineHubStartError(
            "cline CLI not found on PATH; install it with `npm install -g cline`"
        )
    return exe


def parse_hub_start_json(raw: str) -> ClineHubEndpoint:
    """Parse ``cline hub start --json`` output into an endpoint."""
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ClineHubStartError("cline hub start returned a non-object record")
    url = str(record.get("url") or "").strip()
    token = str(record.get("authToken") or "").strip()
    if not url or not token:
        raise ClineHubStartError(f"cline hub start record missing url/authToken: {raw[:200]}")
    return ClineHubEndpoint(ws_url=url, auth_token=token)


async def ensure_cline_hub(
    *,
    cwd: str,
    executable: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> ClineHubEndpoint:
    """Start (or reuse) the workspace hub daemon; returns its coordinates."""
    exe = executable or resolve_cline_executable()
    proc = await asyncio.create_subprocess_exec(
        exe,
        "hub",
        "start",
        "--json",
        "--cwd",
        cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError as exc:
        proc.kill()
        raise ClineHubStartError(f"cline hub start timed out after {timeout_s}s") from exc
    if proc.returncode != 0:
        raise ClineHubStartError(
            f"cline hub start failed ({proc.returncode}): {err.decode(errors='replace')[:400]}"
        )
    return parse_hub_start_json(out.decode(errors="replace"))


__all__ = [
    "ClineHubEndpoint",
    "ClineHubStartError",
    "ensure_cline_hub",
    "parse_hub_start_json",
    "resolve_cline_executable",
]
