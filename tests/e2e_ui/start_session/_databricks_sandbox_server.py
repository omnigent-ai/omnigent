"""Real product server on the `databricks` sandbox provider, backed by a local Sandbox API.

The server is configured through ``parse_sandbox_config`` exactly as a ``provider:
databricks`` deployment, so the production ``DatabricksSandboxLauncher`` drives the
real ``databricks-sdk`` client over HTTP. Only the workspace is local: the ``api`` role
serves the Sandbox REST calls the SDK makes and runs each exec-sync command unmodified
with bash on this machine, under a per-command file-size cap standing in for sandbox
storage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

COMMANDS_LOG = "commands.log"
_SECRET_ENV = re.compile(r"TOKEN|BEARER|SECRET|KEY", re.IGNORECASE)
_RUNNING = "SANDBOX_STATE_RUNNING"
_MAX_EXEC_TIMEOUT_S = 1800.0


def _not_found(name: str) -> JSONResponse:
    return JSONResponse(
        {"error_code": "NOT_FOUND", "message": f"Sandbox '{name}' does not exist"},
        status_code=404,
    )


def _seconds(duration: object) -> float:
    if isinstance(duration, str) and duration.endswith("s"):
        with contextlib.suppress(ValueError):
            return min(float(duration[:-1]), _MAX_EXEC_TIMEOUT_S)
    return _MAX_EXEC_TIMEOUT_S


def _kill_processes_with_home(home: Path) -> None:
    """Stop every process this sandbox started, including detached ``setsid`` hosts."""
    marker = f"HOME={home}".encode()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if marker in environ:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(int(entry.name), signal.SIGKILL)


def sandbox_api_app(
    root: Path, *, owner_token: str, file_size_kib: int, git_config: Path
) -> FastAPI:
    """The Databricks Sandbox REST subset used by ``DatabricksSandboxLauncher``."""
    app = FastAPI()
    sandboxes: dict[str, dict[str, Any]] = {}
    lock = threading.Lock()
    root.mkdir(parents=True, exist_ok=True)

    def authorized(request: Request) -> bool:
        return request.headers.get("authorization") == f"Bearer {owner_token}"

    def record(sandbox_id: str, cmd: str, args: list[str], envs: dict[str, str]) -> None:
        shown = {
            key: ("<redacted>" if _SECRET_ENV.search(key) else value)
            for key, value in envs.items()
        }
        with lock, (root / COMMANDS_LOG).open("a") as log:
            log.write(f"{sandbox_id}\t{shown}\t{[cmd, *args]!r}\n")

    @app.middleware("http")
    async def require_owner(request: Request, call_next: Any) -> Any:
        if request.url.path.startswith("/api/") and not authorized(request):
            return JSONResponse(
                {"error_code": "UNAUTHENTICATED", "message": "owner token required"},
                status_code=401,
            )
        return await call_next(request)

    @app.get("/.well-known/databricks-config")
    def host_metadata() -> dict[str, str]:
        return {"workspace_id": "1234567890"}

    @app.get("/api/2.0/sandboxes")
    def list_sandboxes() -> dict[str, Any]:
        with lock:
            return {"sandboxes": list(sandboxes.values())}

    @app.post("/api/2.0/sandboxes")
    async def create_sandbox(request: Request, sandbox_id: str) -> dict[str, Any]:
        body = await request.json()
        home = root / sandbox_id
        home.mkdir(parents=True)
        sandbox = {
            "name": f"sandboxes/{sandbox_id}",
            "display_name": body.get("display_name") or sandbox_id,
            "status": {"state": _RUNNING},
        }
        with lock:
            sandboxes[sandbox_id] = sandbox
        return sandbox

    @app.get("/api/2.0/sandboxes/{sandbox_id}")
    def get_sandbox(sandbox_id: str) -> Any:
        with lock:
            sandbox = sandboxes.get(sandbox_id)
        return sandbox if sandbox is not None else _not_found(sandbox_id)

    @app.post("/api/2.0/sandboxes/{sandbox_id}/start")
    def start_sandbox(sandbox_id: str) -> Any:
        with lock:
            sandbox = sandboxes.get(sandbox_id)
            if sandbox is not None:
                sandbox["status"] = {"state": _RUNNING}
        return sandbox if sandbox is not None else _not_found(sandbox_id)

    @app.delete("/api/2.0/sandboxes/{sandbox_id}")
    def delete_sandbox(sandbox_id: str) -> Any:
        with lock:
            sandbox = sandboxes.pop(sandbox_id, None)
        if sandbox is None:
            return _not_found(sandbox_id)
        _kill_processes_with_home(root / sandbox_id)
        return {}

    @app.post("/api/2.0/sandbox-exec/sandboxes/{sandbox_id}/exec-sync")
    async def exec_sync(sandbox_id: str, request: Request) -> Any:
        with lock:
            known = sandbox_id in sandboxes
        if not known:
            return _not_found(sandbox_id)
        body = await request.json()
        cmd, args, envs = body["cmd"], list(body.get("args") or []), dict(body.get("envs") or {})
        record(sandbox_id, cmd, args, envs)
        home = root / sandbox_id
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_", "GIT_", "PYTEST_"))
        }
        env.update(
            HOME=str(home),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(git_config),
            GIT_TERMINAL_PROMPT="0",
            PATH=f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
            **envs,
        )
        argv = ["/bin/bash", "-c", f'ulimit -f {file_size_kib}; exec "$0" "$@"', cmd, *args]

        def run() -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    argv,
                    cwd="/",
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=_seconds(body.get("execution_timeout")),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return None

        completed = await asyncio.to_thread(run)
        command_id = uuid.uuid4().hex
        if completed is None:
            return {"command_id": command_id, "status": "EXECUTE_COMMAND_STATUS_TIMED_OUT"}
        return {
            "command_id": command_id,
            "status": "EXECUTE_COMMAND_STATUS_COMPLETED",
            "exit_code": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        }

    app.state.stop_all = lambda: [
        _kill_processes_with_home(root / sandbox_id) for sandbox_id in list(sandboxes)
    ]
    return app


def product_app(root: Path, port: int, *, workspace_host: str, owner_token: str) -> FastAPI:
    """The product app as a ``provider: databricks`` deployment with a connected owner."""
    from omnigent.runtime import init
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.managed_hosts import parse_sandbox_config
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.stores.host_store import HostStore

    uri = f"sqlite:///{root / 'server.db'}"
    artifacts = LocalArtifactStore(str(root / "artifacts"))
    agents = SqlAlchemyAgentStore(uri)
    conversations = SqlAlchemyConversationStore(uri)
    files = SqlAlchemyFileStore(uri)
    cache = AgentCache(artifact_store=artifacts, cache_dir=root / "cache")
    init(
        agent_store=agents,
        conversation_store=conversations,
        file_store=files,
        artifact_store=artifacts,
        agent_cache=cache,
    )
    deployment = parse_sandbox_config(
        {
            "provider": "databricks",
            "server_url": f"http://127.0.0.1:{port}",
            "databricks": {"workspace_host": workspace_host},
        }
    )
    assert deployment is not None
    connection = {"token": owner_token, "workspace_host": workspace_host}
    deployment = dataclasses.replace(
        deployment, owner_credentials={"databricks": lambda _owner: connection}
    )
    return create_app(
        agent_store=agents,
        conversation_store=conversations,
        file_store=files,
        artifact_store=artifacts,
        agent_cache=cache,
        host_store=HostStore(uri),
        sandbox_config=deployment,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["api", "server"])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--owner-token", required=True)
    parser.add_argument("--workspace-host")
    parser.add_argument("--file-size-kib", type=int)
    parser.add_argument("--git-config", type=Path)
    args = parser.parse_args()
    if args.role == "api":
        app = sandbox_api_app(
            args.root,
            owner_token=args.owner_token,
            file_size_kib=args.file_size_kib,
            git_config=args.git_config,
        )
        control = APIRouter()

        @control.post("/__test/sandboxes/stop")
        def stop() -> dict[str, bool]:
            app.state.stop_all()
            return {"stopped": True}

        app.include_router(control)
    else:
        app = product_app(
            args.root, args.port, workspace_host=args.workspace_host, owner_token=args.owner_token
        )
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    finally:
        if args.role == "api":
            app.state.stop_all()


if __name__ == "__main__":
    main()
