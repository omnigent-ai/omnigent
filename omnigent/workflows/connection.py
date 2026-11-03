"""One authenticated server connection shared by every workflow member."""

from __future__ import annotations

import os
import threading
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager

import httpx

from omnigent.inner.databricks_executor import (
    _DatabricksBearerAuth,
    _DatabricksCliProfileAuthConfig,
    _read_databrickscfg_host,
    databrickscfg_workspace_id_for_profile,
)
from omnigent.util.threaded_auth import ThreadedAuth


class _ProfileAuth(ThreadedAuth):
    def __init__(self, profile: str, host: str) -> None:
        self._source = _DatabricksBearerAuth(
            _DatabricksCliProfileAuthConfig(profile=profile, host=host), profile_name=profile
        )
        self._lock = threading.Lock()

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response, None]:
        # Parallel members share the CLI token cache and serialize refresh.
        with self._lock:
            token = self._source.current_token()
        if not token:
            raise ValueError("The selected Databricks profile returned no token")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request


@asynccontextmanager
async def open_workflow_server_client(server: str) -> AsyncIterator[httpx.AsyncClient]:
    """Use one explicit profile, or the existing Omnigent login, for the whole run."""
    from omnigent.chat import _server_auth
    from omnigent.cli_auth import (
        DATABRICKS_ORG_ID_HEADER,
        load_databricks_workspace_host,
        open_server_client,
    )
    from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
    from omnigent.session_host import route_session_request

    profile = os.environ.get("OMNIGENT_WORKFLOW_DATABRICKS_PROFILE", "").strip()
    workspace_id = None
    if profile:
        host = _read_databrickscfg_host(profile)
        if not host:
            raise ValueError(f"Databricks profile {profile!r} has no workspace host")
        workspace = load_databricks_workspace_host(server) or server
        if httpx.URL(host).copy_with(path="/") != httpx.URL(workspace).copy_with(path="/"):
            raise ValueError("The selected Databricks profile does not match the workflow server")
        auth = _ProfileAuth(profile, host)
        workspace_id = databrickscfg_workspace_id_for_profile(profile)
    else:
        auth = _server_auth(server_url=server, session_id=None)

    async def route(request: httpx.Request) -> None:
        await route_session_request(request)
        if workspace_id:
            request.headers[DATABRICKS_ORG_ID_HEADER] = workspace_id

    async with open_server_client(
        server,
        auth=auth,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=3660,
    ) as client:
        client.event_hooks["request"].append(route)
        yield client
