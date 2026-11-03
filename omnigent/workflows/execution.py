"""Select an execution library without coupling it to workflow scheduling."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from omnigent.workflows.engine import MemberExecutor
from omnigent.workflows.models import ManagedSandbox, Workflow
from omnigent.workflows.simulation import simulate_member
from omnigent.workflows.state import ExecutionBinding


def validate_backend(workflow: Workflow, binding: ExecutionBinding) -> None:
    for team in workflow.teams:
        for member in team.members:
            placement = member.placement
            if binding.mode == "arclet" and not (
                isinstance(placement, ManagedSandbox) and placement.provider == "arclet"
            ):
                raise ValueError("Arclet requires sandbox placement with provider: arclet")


def validate_connection_url(server: str | None) -> None:
    if not server:
        raise ValueError("An execution server URL is required")
    try:
        url = httpx.URL(server)
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid execution server URL") from exc
    if (
        url.scheme not in {"http", "https"}
        or not url.host
        or url.userinfo
        or url.query
        or url.fragment
    ):
        raise ValueError("Use an HTTP(S) server URL without credentials, query, or fragment")


@asynccontextmanager
async def open_executor(binding: ExecutionBinding) -> AsyncIterator[MemberExecutor]:
    if binding.mode == "custom":
        raise ValueError("Supply custom executors directly to WorkflowRunner")
    if binding.mode == "simulation":
        yield simulate_member
        return
    validate_connection_url(binding.server)
    assert binding.server is not None
    from omnigent_client import SessionsNamespace

    from omnigent.workflows.arclet import ArcletExecutor
    from omnigent.workflows.connection import open_workflow_server_client
    from omnigent.workflows.sessions import SessionExecutor

    async with open_workflow_server_client(binding.server) as client:
        sessions = SessionsNamespace(client, binding.server)
        yield (
            ArcletExecutor(
                sessions, sandbox_provider=os.environ.get("OMNIGENT_WORKFLOW_ARCLET_PROVIDER")
            )
            if binding.mode == "arclet"
            else SessionExecutor(sessions)
        )
