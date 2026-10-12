"""Real product server whose managed sandbox is a bounded process sandbox on this machine.

Only the provider is local: each spin-up command (``$HOME`` probe, ``mkdir``, ``git clone``,
the real ``omnigent host``) runs here under a per-process file-size cap standing in for a
sandbox's storage budget. The launcher overrides no spin-up step, so the product's clone
command reaches ``git`` unmodified.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, ClassVar

import click
import uvicorn
from fastapi import APIRouter, FastAPI

from omnigent.onboarding.sandboxes.base import ExecModelHostLauncher, RemoteCommandResult
from tests.e2e_ui.start_session._managed_replica_server import terminate

COMMANDS_LOG = "commands.log"
_TOKEN_ASSIGNMENT = re.compile(r"(OMNIGENT_HOST_TOKEN=)\S+")


class BoundedLocalSandbox(ExecModelHostLauncher):
    """Exec-model sandbox on this machine with a per-process file-size budget."""

    provider: ClassVar[str] = "modal"

    def __init__(self, root: Path, *, file_size_kib: int, git_config: Path) -> None:
        self.root = root
        self.file_size_kib = file_size_kib
        self.git_config = git_config
        self.hosts: dict[str, subprocess.Popen[bytes]] = {}
        self._lock = threading.Lock()

    def prepare(self) -> None:
        pass

    def provision(self, name: str) -> str:
        sandbox_id = f"{name}-{uuid.uuid4().hex[:6]}"
        (self.root / sandbox_id).mkdir(parents=True)
        return sandbox_id

    def _home(self, sandbox_id: str) -> Path:
        home = self.root / sandbox_id
        if not home.is_dir():
            raise click.ClickException(f"sandbox '{sandbox_id}' does not exist")
        return home

    def _env(self, sandbox_id: str) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        }
        env.update(
            HOME=str(self._home(sandbox_id)),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.git_config),
            GIT_TERMINAL_PROMPT="0",
            PATH=f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}",
        )
        return env

    def _argv(self, command: str) -> list[str]:
        return ["bash", "-c", f"ulimit -f {self.file_size_kib} && {command}"]

    def _record(self, sandbox_id: str, command: str) -> None:
        redacted = _TOKEN_ASSIGNMENT.sub(r"\1<redacted>", command)
        with self._lock, (self.root / COMMANDS_LOG).open("a") as log:
            log.write(f"{sandbox_id}\t{redacted}\n")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self._record(sandbox_id, command)
        result = subprocess.run(
            self._argv(command),
            cwd=self._home(sandbox_id),
            env=self._env(sandbox_id),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {result.returncode}): {command}: {result.stderr.strip()[-1500:]}"
            )
        return RemoteCommandResult(result.returncode, result.stdout, result.stderr)

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        self._record(sandbox_id, command)
        home = self._home(sandbox_id)
        with (home / "omnigent-host.log").open("ab") as log:
            process = subprocess.Popen(
                self._argv(command),
                cwd=home,
                env=self._env(sandbox_id),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        with self._lock:
            self.hosts[sandbox_id] = process
        return RemoteCommandResult(0, "launched\n", "")

    def terminate(self, sandbox_id: str) -> None:
        with self._lock:
            process = self.hosts.pop(sandbox_id, None)
        if process is not None:
            terminate(process)

    def terminate_all(self) -> None:
        for sandbox_id in list(self.hosts):
            self.terminate(sandbox_id)


def build_app(
    root: Path, port: int, *, file_size_kib: int, git_config: Path
) -> tuple[FastAPI, BoundedLocalSandbox]:
    from omnigent.runtime import init
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
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
    launcher = BoundedLocalSandbox(
        root / "sandboxes", file_size_kib=file_size_kib, git_config=git_config
    )
    control = APIRouter()

    @control.post("/hosts/stop")
    async def stop_hosts() -> dict[str, Any]:
        launcher.terminate_all()
        return {"stopped": True}

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
                server_url=f"http://127.0.0.1:{port}",
                token_ttl_s=3600,
                provider=launcher.provider,
                launcher_factory=lambda: launcher,
            )
        ),
    )
    return app, launcher


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--file-size-kib", type=int, required=True)
    parser.add_argument("--git-config", type=Path, required=True)
    args = parser.parse_args()
    app, launcher = build_app(
        args.root, args.port, file_size_kib=args.file_size_kib, git_config=args.git_config
    )
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")
    finally:
        launcher.terminate_all()


if __name__ == "__main__":
    main()
