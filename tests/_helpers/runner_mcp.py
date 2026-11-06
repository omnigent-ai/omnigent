"""Runner MCP routes with caller-owned workspace and policy transport."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI

from omnigent.runner import mcp_routes
from omnigent.runner.mcp_execution_registry import McpExecutionRegistry
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec


class RunnerMcpHarness:
    """Register real routes while leaving configuration and faults to each test."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace: Path,
        server_client: httpx.AsyncClient,
        runner_workspace: Path | None = None,
        mcp_manager: Any = None,
        process_manager: Any = None,
        spec_resolver: Any = None,
    ) -> None:
        self.app = FastAPI()
        self.session_id = session_id
        self.mcp_manager = mcp_manager
        self.spec_cache: dict[str, Any] = {}
        self.resolved_spec: AgentSpec | None = None
        self.resolved_entry: Any = None
        self.resolve_error: Exception | None = None
        self.conversations: dict[str, str] = {}
        self.published: list[tuple[str, Any]] = []
        self.recovered: list[str] = []
        self.registry = McpExecutionRegistry()
        self.inboxes: dict[str, Any] = {}
        self.async_tasks: dict[str, Any] = {}
        self.agent_ids: dict[str, str] = {}

        async def recover(session_id: str) -> None:
            self.recovered.append(session_id)

        async def resolve_conv(response_id: str) -> str | None:
            return self.conversations.get(response_id)

        async def resolve_spec_or_none(session_id: str) -> AgentSpec | None:
            return self.resolved_spec

        async def resolve_entry(session_id: str) -> Any:
            if self.resolve_error is not None:
                raise self.resolve_error
            return self.resolved_entry

        async def runtime_cwd(session_id: str) -> Path:
            return workspace

        mcp_routes.register_mcp_routes(
            self.app,
            _publish_event=lambda sid, payload: self.published.append((sid, payload)),
            _recover_undrained_subagent_results=recover,
            _resolve_conversation_id=resolve_conv,
            _resolve_session_agent_spec_or_none=resolve_spec_or_none,
            _resolve_session_spec_entry=resolve_entry,
            _session_agent_ids=self.agent_ids,
            _session_async_tasks=self.async_tasks,
            _session_harness_name=lambda sid: "claude-sdk",
            _session_inboxes=self.inboxes,
            _session_runtime_cwd=runtime_cwd,
            _session_spec_cache=self.spec_cache,
            filesystem_registry=None,
            mcp_execution_registry=self.registry,
            mcp_manager=mcp_manager,
            process_manager=process_manager,
            resource_registry=SessionResourceRegistry(),
            runner_workspace=runner_workspace or workspace,
            server_client=server_client,
            spec_resolver=spec_resolver,
            terminal_registry=None,
        )

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://runner"
        ) as client:
            yield client

    async def execute(self, body: Any) -> tuple[int, Any]:
        async with self.client() as client:
            resp = await client.post(f"/v1/sessions/{self.session_id}/mcp/execute", json=body)
        return resp.status_code, resp.json()
