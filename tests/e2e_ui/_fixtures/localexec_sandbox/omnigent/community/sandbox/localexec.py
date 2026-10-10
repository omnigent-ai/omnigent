"""Local exec-model sandbox provider for e2e tests: each sandbox is a private ``HOME``
directory where ``run`` executes the managed-launch commands with ``bash``. Registered via
the ``omnigent.sandbox_providers`` entry point in the sibling ``.dist-info`` directory."""

from __future__ import annotations

import contextlib
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)

PROVIDER = "localexec"
_PGID_FILE = ".omnigent-host.pgid"


@dataclass(frozen=True)
class LocalExecConfig:
    """The ``sandbox.localexec`` block: where sandboxes live and their environment."""

    root: str
    env: dict[str, str] = field(default_factory=dict)


class LocalExecLauncher(SandboxLauncher):
    """Exec-model launcher whose sandbox is a directory on the server's machine."""

    provider = PROVIDER
    supports_cli_bootstrap = False

    def __init__(self, *, config: LocalExecConfig) -> None:
        self._root = Path(config.root)
        self._env = dict(config.env)

    def prepare(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)

    def provision(self, name: str) -> str:
        sandbox_id = f"{name}-{secrets.token_hex(4)}"
        self._home(sandbox_id).mkdir(parents=True)
        return sandbox_id

    def _home(self, sandbox_id: str) -> Path:
        return self._root / sandbox_id

    def _environment(self, sandbox_id: str) -> dict[str, str]:
        venv_bin = str(Path(sys.executable).parent)
        return {
            "HOME": str(self._home(sandbox_id)),
            "PATH": os.pathsep.join([venv_bin, "/usr/local/bin", "/usr/bin", "/bin"]),
            "LANG": "C.UTF-8",
            **self._env,
        }

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        home = self._home(sandbox_id)
        if not home.is_dir():
            raise click.ClickException(f"sandbox '{sandbox_id}' does not exist")
        completed = subprocess.run(
            ["bash", "-c", command],
            cwd=home,
            env=self._environment(sandbox_id),
            capture_output=True,
            text=True,
            check=False,
        )
        if check and completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise click.ClickException(
                f"command exited {completed.returncode} in sandbox '{sandbox_id}': {detail}"
            )
        return RemoteCommandResult(
            returncode=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
        )

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        home = self._home(sandbox_id)
        with open(home / Path(log_path).name, "ab") as log:
            process = subprocess.Popen(
                ["sh", "-c", supervise_host_command(command)],
                cwd=home,
                env=self._environment(sandbox_id),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        (home / _PGID_FILE).write_text(str(process.pid))
        return RemoteCommandResult(returncode=0, stdout="launched\n", stderr="")

    def terminate(self, sandbox_id: str) -> None:
        home = self._home(sandbox_id)
        pgid_file = home / _PGID_FILE
        if pgid_file.is_file():
            terminate_process_group(int(pgid_file.read_text()))
        shutil.rmtree(home, ignore_errors=True)


def terminate_process_group(pgid: int) -> None:
    """Stop a detached host supervisor and everything it spawned."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break
        time.sleep(1.0)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pgid, os.WNOHANG)


def contribution() -> SandboxProviderContribution:
    """Entry point registering the ``localexec`` provider."""
    return SandboxProviderContribution(
        name="omnigent-e2e-localexec-sandbox",
        providers={
            PROVIDER: SandboxProviderMetadata(
                name=PROVIDER,
                launcher_class=f"{__name__}:LocalExecLauncher",
                config_model=LocalExecConfig,
            )
        },
    )
