"""Opt-in HTTP fault controls for real-process runner lifecycle journeys."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request

import omnigent
from omnigent.server import session_live_state

router = APIRouter()


@router.get("/state/{runner_id}")
async def state(request: Request, runner_id: str, host_id: str = "") -> dict[str, Any]:
    """Inspect ownership without changing runner/session lifecycle state."""
    registry = request.app.state.tunnel_registry
    return {
        "pid": os.getpid(),
        "source_file": str(Path(omnigent.__file__).resolve()),
        "runner_local": registry.get(runner_id) is not None,
        "last_liveness_stamp": session_live_state.last_liveness_stamp(runner_id),
        "host_local": bool(host_id and request.app.state.host_registry.get(host_id)),
    }


@router.post("/retire/{runner_id}")
async def retire(request: Request, runner_id: str) -> dict[str, bool]:
    """Retire the real tunnel; its normal socket teardown runs the callbacks."""
    registry = request.app.state.tunnel_registry
    session = registry.get(runner_id)
    return {"retired": session is not None and registry.deregister(runner_id, session) is session}


DEBUG_ROUTERS = [(router, "/__runner_lifecycle", ["test-control"])]
