"""
Docker Sandbox (``sbx``) launcher.

Implements the CLI-bootstrap subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for local
`Docker Sandboxes <https://docs.docker.com/ai/sandboxes/>`_ — microVMs
driven by the external ``sbx`` binary. Unlike the cloud launchers
(Daytona, Modal) there is no Python SDK: all transport is ``subprocess``
against ``sbx``.

Platform notes that shape this launcher:

- **Local, not cloud.** Sandboxes run on the machine invoking the CLI,
  so this is a CLI-bootstrap provider only (``omnigent sandbox
  create/connect``). There is no server-managed flow.
- **Own Docker daemon per sandbox.** Each sbx sandbox is a microVM with
  its own Docker daemon, so the in-session agent can run nested Docker
  dev containers safely.
- **Default image + full install.** Sandboxes boot sbx's default image
  (no prebaked omnigent), so :meth:`wheel_install_command` does a full
  dependency install and :meth:`provision` runs a one-time setup step to
  install the host's runtime deps + the Claude Code CLI.
- **No inbound port forwarding.** ``supports_local_port_forward`` stays
  ``False`` (matching Modal), so the CLI auto-skips the Databricks App
  OAuth step.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
)

# ── Constants ──────────────────────────────────────────

TEMPLATE_ENV_VAR: str = "OMNIGENT_SBX_TEMPLATE"
"""Environment variable naming a container image to boot sbx sandboxes
from (``sbx create -t``), overriding sbx's default agent image."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_SBX_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the LOCAL environment
variables whose values are forwarded into the in-sandbox host process
(``sbx exec -e NAME=VALUE``) — typically harness LLM credentials
(``ANTHROPIC_API_KEY``, ``CLAUDE_CODE_OAUTH_TOKEN``, gateway URLs).
Names, not values: read from the invoking shell at exec time."""

KITS_ENV_VAR: str = "OMNIGENT_SBX_KITS"
"""Environment variable naming (comma- or whitespace-separated) sbx kit
references applied at provision time (``sbx create --kit``)."""

# One-time root setup run after `sbx create` to make sbx's default image
# a viable Omnigent host: ensure python/pip, git, tmux, node/npm, and the
# Claude Code CLI. Idempotent — already-present tools no-op. (Discovery
# task confirms the base image's gaps; absent tools are installed, present
# ones skipped.)
_SETUP_COMMAND: str = (
    "set -e; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "export DEBIAN_FRONTEND=noninteractive; pkgs=''; "
    'command -v python3 >/dev/null 2>&1 || pkgs="$pkgs python3"; '
    'command -v pip3 >/dev/null 2>&1 || pkgs="$pkgs python3-pip"; '
    'command -v git >/dev/null 2>&1 || pkgs="$pkgs git"; '
    'command -v tmux >/dev/null 2>&1 || pkgs="$pkgs tmux"; '
    'command -v node >/dev/null 2>&1 || pkgs="$pkgs nodejs"; '
    'command -v npm >/dev/null 2>&1 || pkgs="$pkgs npm"; '
    'if [ -n "$pkgs" ]; then apt-get update && '
    "apt-get install -y --no-install-recommends $pkgs; fi; fi; "
    "command -v claude >/dev/null 2>&1 || npm install -g @anthropic-ai/claude-code"
)


class SbxSandboxLauncher(SandboxLauncher):
    """:class:`SandboxLauncher` for local Docker Sandboxes via ``sbx``."""

    provider: ClassVar[str] = "sbx"
    supports_cli_bootstrap: ClassVar[bool] = True
    # sbx microVMs expose no local->sandbox port-forward path, so the
    # Databricks App OAuth flow is unsupported (CLI auto-skips it).
    supports_local_port_forward: ClassVar[bool] = False
    wheel_build_index_url: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        template: str | None = None,
        env: Sequence[str] | None = None,
        kits: Sequence[str] | None = None,
    ) -> None:
        """
        :param template: Container image for ``sbx create -t``, or
            ``None`` to resolve :data:`TEMPLATE_ENV_VAR` / sbx default.
        :param env: Local env-var NAMES forwarded into the host process,
            or ``None`` to resolve :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param kits: sbx kit references applied at create, or ``None`` to
            resolve :data:`KITS_ENV_VAR`.
        """
        self._template = template
        self._env_names = tuple(env) if env is not None else None
        self._kits = tuple(kits) if kits is not None else None
        self._binary: str | None = None

    def _sbx_binary(self) -> str:
        """Locate the ``sbx`` binary, caching the result."""
        if self._binary is None:
            found = shutil.which("sbx")
            if found is None:
                raise click.ClickException(
                    "The `sbx` CLI is required for the 'sbx' sandbox provider. "
                    "Install Docker Sandboxes (https://docs.docker.com/ai/sandboxes/) "
                    "and sign in with `sbx login`."
                )
            self._binary = found
        return self._binary

    def _run_sbx(
        self, args: list[str], *, capture: bool = True, check: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run ``sbx <args>``; capture text output by default."""
        return subprocess.run(
            [self._sbx_binary(), *args],
            capture_output=capture,
            text=True,
            check=check,
        )

    def prepare(self) -> None:
        """
        Local preflight: the ``sbx`` binary must be installed and signed
        in (``sbx login``).

        :raises click.ClickException: When ``sbx`` is missing or the
            login probe (``sbx ls``) fails.
        """
        self._sbx_binary()  # raises the install hint when absent
        probe = self._run_sbx(["ls"])
        if probe.returncode != 0:
            raise click.ClickException(
                "Not signed in to Docker Sandboxes. Run `sbx login` first."
                + (f" ({probe.stderr.strip()})" if probe.stderr.strip() else "")
            )

    def provision(self, name: str) -> str:
        """
        Create a `shell` sbx sandbox bind-mounting the current directory,
        then run the one-time setup step that installs the host runtime
        deps + the Claude Code CLI.

        :param name: Sandbox name (also its id — sbx is name-addressed).
        :returns: The sandbox name.
        :raises click.ClickException: When ``sbx create`` fails.
        """
        workspace = str(Path.cwd())
        args = ["create", "--name", name]
        for kit in self._resolve_kits():
            args += ["--kit", kit]
        template = self._resolve_template()
        if template:
            args += ["-t", template]
        args += ["shell", workspace]

        click.echo(f"▸ Creating sbx sandbox '{name}' (workspace: {workspace})")
        result = self._run_sbx(args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            # Discovery finding: sbx refuses to start any sandbox until a
            # host-global default network policy is set, with a distinctive
            # message. Rewrite it into a single actionable remediation so the
            # user isn't left guessing which `sbx policy` invocation to run.
            if "default network policy" in detail.lower():
                raise click.ClickException(
                    "sbx has no default network policy configured. Run "
                    "`sbx policy set-default balanced` (allows AI services + "
                    "package registries; use `allow-all` if your --server host "
                    "gets blocked), then retry."
                )
            raise click.ClickException(f"sbx sandbox creation failed: {detail}")

        click.echo("  → installing host runtime dependencies")
        setup = self._run_sbx(["exec", "-u", "root", name, "bash", "-lc", _SETUP_COMMAND])
        if setup.returncode != 0:
            raise click.ClickException(
                f"sbx sandbox setup failed on '{name}': "
                f"{setup.stderr.strip() or setup.stdout.strip()}"
            )
        click.echo(f"  → created {name}")
        return name

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox via ``sbx exec`` and capture
        its output (stdout/stderr kept separate).

        :param sandbox_id: Target sandbox name.
        :param command: Shell command (``bash -lc`` applies login PATH).
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: When *check* and the exit is non-zero.
        """
        result = self._run_sbx(["exec", sandbox_id, "bash", "-lc", command])
        for stream in (result.stdout, result.stderr):
            for line in (stream or "").splitlines():
                if line.strip():
                    click.echo(line)
        if check and result.returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {result.returncode}): {command}"
            )
        return RemoteCommandResult(
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via ``sbx cp``.

        :param sandbox_id: Target sandbox name.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path inside the sandbox.
        :raises click.ClickException: When the transfer fails.
        """
        result = self._run_sbx(["cp", str(local_path), f"{sandbox_id}:{remote_path}"])
        if result.returncode != 0:
            raise click.ClickException(
                f"File copy to sandbox '{sandbox_id}' failed: "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Full dependency install of the shipped wheels into the user site.

        Unlike the prebaked-host-image launchers, sbx's default image has
        no baked omnigent, so this installs WITH dependencies (no
        ``--no-deps`` / ``--force-reinstall``). ``--user`` lands the
        install where the bootstrap's PATH-persistence step adds to PATH
        (``$HOME/.local/bin``). ``--break-system-packages`` tolerates a
        PEP-668 externally-managed base python (drop it if the discovery
        task showed PEP 668 is not in force).

        :param remote_tgz_path: Sandbox path of the shipped tarball.
        :returns: Shell command string for :meth:`run`.
        """
        return (
            "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
            f"tar xzf {remote_tgz_path} -C oa-wheels --warning=no-unknown-keyword && "
            "pip install --user --break-system-packages "
            "--no-warn-script-location oa-wheels/*.whl"
        )

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox over an interactive TTY with stdio
        inherited from the local terminal, returning its exit code.

        Passthrough env (resolved from :meth:`_resolve_env`) is injected
        with ``-e``; ``TERM`` is forced (native harnesses spawn tmux,
        which refuses a dumb/unset TERM); ``exec`` replaces the shell so
        the returned code is the command's own. The ``sbx`` process is a
        local child, so Ctrl-C reaches it directly — no remote-kill dance.

        :param sandbox_id: Target sandbox name.
        :param command: Shell command, e.g. ``"omnigent host --server …"``.
        :returns: The command's exit code.
        :raises KeyboardInterrupt: Re-raised when the user detaches.
        """
        argv = [self._sbx_binary(), "exec", "-it"]
        for name, value in self._resolve_env().items():
            argv += ["-e", f"{name}={value}"]
        argv += [sandbox_id, "bash", "-lc", f"TERM=xterm-256color exec {command}"]
        try:
            result = subprocess.run(argv, check=False)
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            raise
        return result.returncode

    def _resolve_template(self) -> str | None:
        """Resolve the create image: constructor wins, then env var."""
        return self._template or os.environ.get(TEMPLATE_ENV_VAR) or None

    def _resolve_kits(self) -> list[str]:
        """Resolve kit references: constructor wins, then env var."""
        if self._kits is not None:
            return list(self._kits)
        raw = os.environ.get(KITS_ENV_VAR, "")
        return [k.strip() for k in raw.replace(",", " ").split() if k.strip()]

    def _resolve_env(self) -> dict[str, str]:
        """Resolve passthrough NAMES to local values; missing fails loud."""
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                n.strip()
                for n in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if n.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sbx env passthrough names '{name}' but it is not set in the "
                    f"local environment — set it (or remove it from {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved
