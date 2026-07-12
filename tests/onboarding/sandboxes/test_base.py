"""Provider-agnostic tests for the :class:`SandboxLauncher` base behavior.

The exec-model defaults (``run_background`` / ``start_host``) are shared by
every provider whose sandbox is a bare box the server execs into (Modal,
Daytona, E2B, Boxlite, Islo, …), so they are tested once here against a
minimal recording launcher rather than per provider.
"""

from __future__ import annotations

import subprocess
from typing import ClassVar

from omnigent.onboarding.sandboxes.base import RemoteCommandResult, SandboxLauncher


class _RecordingLauncher(SandboxLauncher):
    """Minimal exec-model launcher that records every ``run`` command."""

    provider: ClassVar[str] = "recording"

    def __init__(self, home: str = "/root") -> None:
        self.commands: list[str] = []
        self.backgrounded: list[str] = []
        self._home = home

    def prepare(self) -> None:  # pragma: no cover - unused preflight stub
        pass

    def provision(self, name: str) -> str:  # pragma: no cover - unused stub
        return "sb-1"

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        # start_host probes $HOME first; everything else returns empty.
        stdout = self._home if command == 'printf %s "$HOME"' else ""
        return RemoteCommandResult(returncode=0, stdout=stdout, stderr="")

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        # Capture the raw (pre-wrap) command so a test can prove a real shell
        # honors its env prefix, independent of the setsid/nohup wrapper.
        self.backgrounded.append(command)
        return super().run_background(sandbox_id, command, log_path=log_path)


def test_run_background_wraps_command_in_sh_c() -> None:
    """
    ``run_background`` must wrap the command in ``sh -c`` so env-var prefixes
    survive ``nohup``. ``nohup ENV=val cmd`` makes nohup try to exec a program
    literally named ``ENV=val`` ("No such file or directory") — re-parsing under
    ``sh -c`` lets the inner shell apply the assignment before running ``cmd``.
    Regression: managed Daytona/Modal hosts never came online because the
    in-sandbox ``omnigent host`` launch died on its ``OMNIGENT_HOST_TOKEN=…``
    prefix.
    """
    launcher = _RecordingLauncher()

    launcher.run_background("sb-1", "FOO=bar omnigent host --server https://srv")

    [cmd] = launcher.commands
    assert cmd == (
        "setsid nohup sh -c 'FOO=bar omnigent host --server https://srv' "
        "> /tmp/omnigent-host.log 2>&1 < /dev/null & echo launched"
    )


def test_start_host_env_prefix_is_honored_by_a_real_shell() -> None:
    """
    The env-prefixed command ``start_host`` hands to ``run_background`` must
    apply its ``OMNIGENT_HOST_*`` assignments when re-parsed by a shell — the
    exact thing the ``sh -c`` wrapper restores. Run the raw command through a
    real ``sh -c`` (the inner shell of the wrapper) with ``omnigent host``
    swapped for a probe that echoes the injected vars; the broken bare-``nohup``
    form would never reach this assignment-honoring shell.
    """
    launcher = _RecordingLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )
    assert workspace == "/root/workspace"

    [raw] = launcher.backgrounded
    # A nested `sh -c` reads the *inherited* env (a bare `$VAR` in the same
    # simple command would expand in the parent shell, before the temporary
    # assignment takes effect — and print empty).
    probe = raw.replace(
        "omnigent host --server https://srv",
        "sh -c 'printf %s:%s:%s "
        '"$OMNIGENT_HOST_TOKEN" "$OMNIGENT_HOST_ID" "$OMNIGENT_HOST_NAME"\'',
    )
    out = subprocess.run(
        ["sh", "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "tok-123:host_abc:managed-abc"
