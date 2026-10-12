"""Real server replicas and a transparent, slice-keyed ingress for UI regressions.

Only the sandbox provider is local: it starts the real host CLI instead of a
cloud VM. Session creation, host/runner tunnels, dispatch, persistence and SSE
all use production code in separate processes. The ingress records traffic
without manufacturing API responses or stream events.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any, ClassVar

import httpx
import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import StreamingResponse

from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import RepoWorkspace

SLICE_KEY = "x-databricks-omnigent-slice-key"
_HOP_HEADERS = {"host", "connection", "keep-alive", "transfer-encoding"}


def terminate(process: subprocess.Popen[bytes]) -> None:
    """Stop only a process group created by this test."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


class LocalManagedHost(SandboxHostLauncher):
    """Replace cloud allocation with a gated local host subprocess."""

    provider: ClassVar[str] = "modal"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.gate = threading.Event()
        self.provisioning = threading.Event()
        self.process: subprocess.Popen[bytes] | None = None

    def prepare(self) -> None:
        pass

    def provision(self, name: str) -> str:
        self.provisioning.set()
        assert self.gate.wait(timeout=120), "browser never released sandbox provisioning"
        return name

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        assert not repos and host_config is None
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        if on_stage:
            on_stage("starting")
        with (self.root / "host.log").open("w") as log:
            self.process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent",
                    "host",
                    "--server",
                    server_url,
                    "--non-interactive",
                ],
                env={
                    **os.environ,
                    "OMNIGENT_HOST_ID": host_id,
                    "OMNIGENT_HOST_NAME": host_name,
                    "OMNIGENT_HOST_TOKEN": token,
                },
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        return str(workspace)

    def terminate(self, sandbox_id: str) -> None:
        if self.process is not None:
            terminate(self.process)
            self.process = None


def replica_app(root: Path, replica: str, runner_url: str) -> tuple[FastAPI, LocalManagedHost]:
    """Build the product app with shared durable stores, never shared registries."""
    from omnigent.runtime import init
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    uri = f"sqlite:///{root / 'replicas.db'}"
    artifacts = LocalArtifactStore(str(root / "artifacts"))
    agents = SqlAlchemyAgentStore(uri)
    conversations = SqlAlchemyConversationStore(uri)
    files = SqlAlchemyFileStore(uri)
    cache = AgentCache(artifact_store=artifacts, cache_dir=root / f"cache-{replica}")
    init(
        agent_store=agents,
        conversation_store=conversations,
        file_store=files,
        artifact_store=artifacts,
        agent_cache=cache,
    )
    launcher = LocalManagedHost(root)
    control = APIRouter()

    @control.post("/provision/release")
    async def release() -> dict[str, bool]:
        launcher.gate.set()
        return {"released": True}

    @control.post("/host/stop")
    async def stop_host() -> dict[str, bool]:
        launcher.gate.set()
        await asyncio.to_thread(launcher.terminate, "")
        return {"stopped": True}

    @control.get("/state")
    async def state(host_id: str = "") -> dict[str, Any]:
        return {
            "pid": os.getpid(),
            "provisioning": launcher.provisioning.is_set(),
            "host_local": bool(host_id and app.state.host_registry.get(host_id)),
        }

    app = create_app(
        agent_store=agents,
        conversation_store=conversations,
        file_store=files,
        artifact_store=artifacts,
        agent_cache=cache,
        host_store=HostStore(uri),
        extra_routers=[(control, "/__test", ["test-control"])],
        sandbox_config=ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                server_url=runner_url,
                token_ttl_s=3600,
                provider=launcher.provider,
                launcher_factory=lambda: launcher,
            )
        ),
    )

    return app, launcher


def ingress_app(replica_a: str, replica_b: str) -> FastAPI:
    """Forward keyless traffic to A and host-keyed traffic to B, including SSE."""
    exchanges: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=None), trust_env=False
        ) as client:
            app.state.upstream = client
            yield

    app = FastAPI(lifespan=lifespan)

    @app.get("/__test/network")
    async def network() -> list[dict[str, Any]]:
        return exchanges

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
    async def forward(request: Request, path: str) -> StreamingResponse:
        key = request.headers.get(SLICE_KEY)
        target = replica_b if key else replica_a
        body = await request.body()
        record: dict[str, Any] = {
            "path": request.url.path,
            "method": request.method,
            "key": key,
            "replica": "b" if key else "a",
            "request": body.decode(errors="replace"),
            "status": None,
            "body": "",
            "closed": False,
        }
        exchanges.append(record)
        client = app.state.upstream
        upstream = await client.send(
            client.build_request(
                request.method,
                f"{target}/{path}",
                params=request.query_params,
                headers={k: v for k, v in request.headers.items() if k not in _HOP_HEADERS},
                content=body,
            ),
            stream=True,
        )
        record["status"] = upstream.status_code

        async def chunks() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    record["body"] += chunk.decode(errors="replace")
                    yield chunk
            finally:
                record["closed"] = True
                await upstream.aclose()

        return StreamingResponse(
            chunks(),
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k not in _HOP_HEADERS},
        )

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["a", "b", "ingress"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--replica-a", required=True)
    parser.add_argument("--replica-b", required=True)
    args = parser.parse_args()
    launcher = None
    if args.role == "ingress":
        app = ingress_app(args.replica_a, args.replica_b)
    else:
        app, launcher = replica_app(args.root, args.role, args.replica_b)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    finally:
        if launcher is not None:
            launcher.gate.set()
            launcher.terminate("")


if __name__ == "__main__":
    main()
