"""
Docker Sandbox (``sbx``) launcher.

Implements both the CLI-bootstrap and the server-managed subsets of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for local
`Docker Sandboxes <https://docs.docker.com/ai/sandboxes/>`_ — microVMs
driven by the external ``sbx`` binary. Unlike the cloud launchers
(Daytona, Modal) there is no Python SDK: all transport is ``subprocess``
against ``sbx``.

Platform notes that shape this launcher:

- **Local, with an optional managed mode.** In CLI-bootstrap mode
  (``omnigent sandbox create/connect``) sandboxes run on the machine
  invoking the CLI. In managed mode the server provisions a fresh sbx
  microVM per session from a prebaked image.
- **Own Docker daemon per sandbox.** Each sbx sandbox is a microVM with
  its own Docker daemon, so the in-session agent can run nested Docker
  dev containers safely.
- **Default image + full install (CLI mode).** Sandboxes boot sbx's
  default image (no prebaked omnigent), so :meth:`wheel_install_command`
  does a full dependency install and :meth:`provision` runs a one-time
  setup step to install the host's runtime deps + the Claude Code and
  OpenCode CLIs. Managed mode skips this because the template already
  bakes omnigent.
- **No inbound port forwarding.** ``supports_local_port_forward`` stays
  ``False`` (matching Modal), so the CLI auto-skips the Databricks App
  OAuth step.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

import click

from omnigent.onboarding.harness_install import (
    ANTHROPIC_FAMILY,
    OPENCODE_KEY,
    harness_install_spec,
)
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
)
from omnigent.opencode_zen_credentials import (
    OPENCODE_API_KEY_ENV_VAR,
    resolve_opencode_zen_key,
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
Names, not values: read from the invoking shell at exec time.
``OPENCODE_API_KEY`` additionally falls back to the Omnigent-keychain
OpenCode Zen key when not set locally."""

KITS_ENV_VAR: str = "OMNIGENT_SBX_KITS"
"""Environment variable naming (comma- or whitespace-separated) sbx kit
references applied at provision time (``sbx create --kit``)."""

_CLAUDE_AUTH_DOMAINS: str = "console.anthropic.com:443,platform.claude.ai:443,claude.ai:443"
"""Claude Code subscription-auth endpoints (OAuth authorize + token
refresh). sbx's default-deny network policy blocks them, which makes a
token refresh fail — and on a failed refresh Claude Code wipes the
stored ``~/.claude/.credentials.json`` tokens. Allowed per-sandbox at
provision time so subscription (Pro/Max) auth keeps working."""

_PROXY_MANAGED_SENTINEL: str = "proxy-managed"
"""Literal value Docker Sandboxes injects for its built-in provider key
placeholders (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, …) in every
exec session. Secrets actually registered with ``sbx secret`` get
distinct ``sbx-cs-*`` placeholders instead, so this exact value always
means "no real key behind it" — the gateway never substitutes anything
and harnesses that prefer env keys over their own login (Claude Code)
fail with an invalid-key error."""

_MANAGED_EMPTY_WORKSPACE: Path = Path(tempfile.gettempdir()) / "omnigent-sbx-managed-empty"
"""Throwaway empty directory passed to ``sbx create ... shell`` in managed mode.

The ``sbx`` CLI requires a PATH argument even when the image provides its
own workspace, so we bind-mount this empty directory. It is created on
demand."""


def _server_host_port(server_url: str | None) -> str:
    """Extract ``host:port`` from a URL for sbx network policy allow rules."""
    if not server_url:
        raise click.ClickException("sbx managed mode requires sandbox.server_url")
    parsed = urlparse(server_url)
    host = parsed.hostname
    if not host:
        raise click.ClickException(f"could not parse sandbox.server_url: {server_url}")
    if parsed.port:
        return f"{host}:{parsed.port}"
    default_port = "443" if parsed.scheme == "https" else "80"
    return f"{host}:{default_port}"


# Unsets every env var still holding the unbacked placeholder sentinel
# before the host process starts, so it can't shadow real harness
# credentials (subscription login, apiKeyHelper, `-e`-forwarded keys —
# the latter survive because their values are real, not the sentinel).
_STRIP_PROXY_MANAGED_SNIPPET: str = (
    "while IFS='=' read -r n v; do "
    f'[ "$v" = "{_PROXY_MANAGED_SENTINEL}" ] && unset "$n"; '
    "done < <(env);"
)

_VENV_DIR: str = "$HOME/.omnigent-venv"
"""In-sandbox venv omnigent is installed into (remote shell syntax, not
a Python path — expanded by the sandbox's own shell). A real venv, not
a ``--user`` install against the bare system Python: every native-harness
hook command runs as ``python3 -I ...``, and isolated mode (``-I``)
disables the user site-packages directory, so a ``--user`` install is
invisible to those hook subprocesses even though plain ``python3 -c
"import omnigent"`` finds it fine. A venv's site-packages aren't subject
to that exclusion."""

# npm specs come from harness_install.py so this setup tracks the same version
# pins `omnigent setup` installs (e.g. OpenCode's `~1.17.7` range).
_claude_spec = harness_install_spec(ANTHROPIC_FAMILY)
_opencode_spec = harness_install_spec(OPENCODE_KEY)
assert _claude_spec is not None and _opencode_spec is not None

# One-time root setup after `sbx create`: install the host runtime deps
# (python/git/tmux/node/npm) and the Claude Code + OpenCode CLIs the harnesses
# gate on. Idempotent — already-present tools are skipped.
_SETUP_COMMAND: str = (
    "set -e; "
    "if command -v apt-get >/dev/null 2>&1; then "
    "export DEBIAN_FRONTEND=noninteractive; pkgs=''; "
    'command -v python3 >/dev/null 2>&1 || pkgs="$pkgs python3"; '
    'command -v pip3 >/dev/null 2>&1 || pkgs="$pkgs python3-pip"; '
    'python3 -c "import venv" >/dev/null 2>&1 || pkgs="$pkgs python3-venv"; '
    'command -v git >/dev/null 2>&1 || pkgs="$pkgs git"; '
    'command -v tmux >/dev/null 2>&1 || pkgs="$pkgs tmux"; '
    'command -v node >/dev/null 2>&1 || pkgs="$pkgs nodejs"; '
    'command -v npm >/dev/null 2>&1 || pkgs="$pkgs npm"; '
    'if [ -n "$pkgs" ]; then apt-get update && '
    "apt-get install -y --no-install-recommends $pkgs; fi; fi; "
    f"command -v {_claude_spec.binary} >/dev/null 2>&1 || "
    f"npm install -g {_claude_spec.package}; "
    f"command -v {_opencode_spec.binary} >/dev/null 2>&1 || "
    f"npm install -g {_opencode_spec.package}"
)


class SbxSandboxLauncher(SandboxLauncher):
    """:class:`SandboxLauncher` for local Docker Sandboxes via ``sbx``."""

    provider: ClassVar[str] = "sbx"
    supports_cli_bootstrap: ClassVar[bool] = True
    # sbx microVMs expose no local->sandbox port-forward path, so the
    # Databricks App OAuth flow is unsupported (CLI auto-skips it).
    supports_local_port_forward: ClassVar[bool] = False
    wheel_build_index_url: ClassVar[str | None] = None
    # sbx sandboxes idle-stop but retain their filesystem, so a dormant
    # managed host can be revived under the same sandbox id.
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        template: str | None = None,
        env: Sequence[str] | None = None,
        kits: Sequence[str] | None = None,
        server_url: str | None = None,
    ) -> None:
        """
        :param template: Container image for ``sbx create -t``, or
            ``None`` to resolve :data:`TEMPLATE_ENV_VAR` / sbx default.
        :param env: Local env-var NAMES forwarded into the host process,
            or ``None`` to resolve :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param kits: sbx kit references applied at create, or ``None`` to
            resolve :data:`KITS_ENV_VAR`.
        :param server_url: Public URL the in-sandbox host dials back to.
            When set, the launcher operates in **managed** mode (server
            provisions the box). When unset, it operates in CLI-bootstrap
            mode (``omnigent sandbox create/connect``).
        """
        self._template = template
        self._env_names = tuple(env) if env is not None else None
        self._kits = tuple(kits) if kits is not None else None
        self._server_url = server_url
        self._binary: str | None = None

    @property
    def _managed(self) -> bool:
        """True when this launcher is driven by the server's managed flow."""
        return self._server_url is not None

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

    def _run_sbx(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run ``sbx <args>``, capturing text output."""
        return subprocess.run(
            [self._sbx_binary(), *args],
            capture_output=True,
            text=True,
        )

    def _exec_env_args(self) -> list[str]:
        """
        Build ``sbx exec -e NAME=VALUE`` args for configured env in managed mode.

        CLI-bootstrap mode returns an empty list so :meth:`run` stays
        unchanged for the local ``omnigent sandbox create/connect`` path.
        """
        if not self._managed:
            return []
        args: list[str] = []
        for name, value in self._resolve_env().items():
            args += ["-e", f"{name}={value}"]
        return args

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
        Create a `shell` sbx sandbox.

        CLI-bootstrap mode bind-mounts the current directory and runs a
        one-time setup step. Managed mode creates from a prebaked template,
        bind-mounts an empty directory (the CLI requires a PATH), and opens
        egress to the Omnigent server.

        :param name: Sandbox name (also its id — sbx is name-addressed).
        :returns: The sandbox name.
        :raises click.ClickException: When ``sbx create`` fails.
        """
        if self._managed:
            return self._provision_managed(name)
        return self._provision_cli(name)

    def _provision_cli(self, name: str) -> str:
        """CLI-bootstrap provision: cwd bind-mount + runtime setup."""
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
            # sbx refuses to start any sandbox until a host-global default
            # network policy exists; rewrite that into an actionable remediation.
            if "default network policy" in detail.lower():
                raise click.ClickException(
                    "sbx has no default network policy configured. Run "
                    "`sbx policy set-default balanced` (allows AI services + "
                    "package registries; use `allow-all` if your --server host "
                    "gets blocked), then retry."
                )
            raise click.ClickException(f"sbx sandbox creation failed: {detail}")

        # Best-effort: without these rules subscription auth still works
        # until the first token refresh, and API-key users don't need
        # them at all — so an sbx build lacking `--sandbox` scoping must
        # not fail the provision.
        policy = self._run_sbx(
            ["policy", "allow", "network", "--sandbox", name, _CLAUDE_AUTH_DOMAINS]
        )
        if policy.returncode != 0:
            click.echo(
                "  → warning: could not allow Claude auth domains "
                f"({policy.stderr.strip() or policy.stdout.strip()}); "
                "subscription token refresh may be blocked — run "
                f"`sbx policy allow network --sandbox {name} {_CLAUDE_AUTH_DOMAINS}`"
            )

        click.echo("  → installing host runtime dependencies")
        setup = self._run_sbx(["exec", "-u", "root", name, "bash", "-lc", _SETUP_COMMAND])
        if setup.returncode != 0:
            raise click.ClickException(
                f"sbx sandbox setup failed on '{name}': "
                f"{setup.stderr.strip() or setup.stdout.strip()}"
            )
        click.echo(f"  → created {name}")
        return name

    def _provision_managed(self, name: str) -> str:
        """
        Managed provision: create from the prebaked template, no setup step.

        Bind-mounts an empty server-side directory (the CLI requires a
        PATH), skips the runtime setup (the template already carries
        omnigent), and opens egress to the Omnigent server so the in-box
        host can register.
        """
        template = self._resolve_template()
        if not template:
            raise click.ClickException(
                "sbx managed mode requires a template image — set "
                "'sandbox.sbx.template' in the server config."
            )
        _MANAGED_EMPTY_WORKSPACE.mkdir(parents=True, exist_ok=True)
        args = ["create", "--name", name]
        for kit in self._resolve_kits():
            args += ["--kit", kit]
        args += ["-t", template, "shell", str(_MANAGED_EMPTY_WORKSPACE)]

        click.echo(f"▸ Creating managed sbx sandbox '{name}'")
        result = self._run_sbx(args)
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            if "default network policy" in detail.lower():
                raise click.ClickException(
                    "sbx has no default network policy configured. Run "
                    "`sbx policy set-default balanced` (allows AI services + "
                    "package registries; use `allow-all` if your --server host "
                    "gets blocked), then retry."
                )
            raise click.ClickException(f"sbx sandbox creation failed: {detail}")

        self._allow_managed_egress(name)
        click.echo(f"  → created {name}")
        return name

    def _allow_managed_egress(self, name: str) -> None:
        """Best-effort allow the in-box host to reach the server + Claude auth."""
        server_host_port = _server_host_port(self._server_url)
        domains = f"{server_host_port},{_CLAUDE_AUTH_DOMAINS}"
        policy = self._run_sbx(["policy", "allow", "network", "--sandbox", name, domains])
        if policy.returncode != 0:
            click.echo(
                "  → warning: could not allow managed egress "
                f"({policy.stderr.strip() or policy.stdout.strip()}); "
                "the host may not be able to register."
            )

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox via ``sbx exec`` and capture
        its output (stdout/stderr kept separate).

        In managed mode, configured env vars are injected as
        ``sbx exec -e NAME=VALUE`` so the in-box host receives harness
        credentials. CLI-bootstrap mode keeps the legacy argv.

        :param sandbox_id: Target sandbox name.
        :param command: Shell command (``bash -lc`` applies login PATH).
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: When *check* and the exit is non-zero.
        """
        result = self._run_sbx(
            ["exec", *self._exec_env_args(), sandbox_id, "bash", "-lc", command]
        )
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

        The first ``sbx cp`` into a freshly-provisioned sandbox can flake
        with a truncated-tar extract; the copy transport isn't always
        warmed up by the time ``provision``'s setup step (apt + npm
        installs) finishes, and how long that takes varies with the
        host's network — a short fixed retry budget isn't always enough.
        An immediate retry clears it once the transport is up, and
        ``cp`` is idempotent. Only that transient is retried — a
        permanent error (bad path, missing sandbox) fails fast.

        :param sandbox_id: Target sandbox name.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path inside the sandbox.
        :raises click.ClickException: When the copy fails.
        """
        attempts = 6
        for attempt in range(1, attempts + 1):
            result = self._run_sbx(["cp", str(local_path), f"{sandbox_id}:{remote_path}"])
            if result.returncode == 0:
                return
            detail = result.stderr.strip() or result.stdout.strip()
            low = detail.lower()
            transient = "unexpected eof" in low or "tar extract failed" in low
            if not transient or attempt == attempts:
                raise click.ClickException(f"File copy to sandbox '{sandbox_id}' failed: {detail}")
            time.sleep(3)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Full dependency install of the shipped wheels into :data:`_VENV_DIR`.

        Unlike the prebaked-host-image launchers, sbx's default image has
        no baked omnigent, so this installs WITH dependencies (no
        ``--no-deps`` / ``--force-reinstall``). It lands in a dedicated
        venv rather than ``--user`` site-packages — see :data:`_VENV_DIR`
        for why a ``--user`` install breaks native-harness hook commands.
        The venv is created on first install and reused after (``pip
        install`` alone is idempotent already). ``--pre`` is required:
        omnigent depends on ``opentelemetry-instrumentation-fastapi``,
        which has never cut a non-beta release, so a plain ``pip
        install`` excludes every version that satisfies the pin and
        fails outright — confirmed live against a real sbx sandbox,
        where the prebaked-image launchers' ``--no-deps`` install never
        exercises this dependency at all. The final line appends the
        venv's ``bin/`` to ``PATH`` (idempotent), mirroring the generic
        bootstrap's ``~/.local/bin`` persistence step for the launchers
        that use it.

        :param remote_tgz_path: Sandbox path of the shipped tarball.
        :returns: Shell command string for :meth:`run`.
        """
        return (
            "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
            f"tar xzf {remote_tgz_path} -C oa-wheels --warning=no-unknown-keyword && "
            f'[ -x "{_VENV_DIR}/bin/python3" ] || python3 -m venv "{_VENV_DIR}" && '
            f'"{_VENV_DIR}/bin/pip" install --quiet --pre '
            "--no-warn-script-location oa-wheels/*.whl && "
            "for f in ~/.bashrc ~/.bash_profile; do "
            f'grep -q "{_VENV_DIR}/bin" "$f" 2>/dev/null || '
            f'echo "export PATH={_VENV_DIR}/bin:\\$PATH" >> "$f"; '
            "done"
        )

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox over an interactive TTY with stdio
        inherited from the local terminal, returning its exit code.

        Passthrough env (resolved from :meth:`_resolve_env`) is injected
        with ``-e``; Docker's unbacked ``proxy-managed`` placeholder keys
        are unset first (see :data:`_PROXY_MANAGED_SENTINEL`); ``TERM``
        is forced (native harnesses spawn tmux,
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
        argv += [
            sandbox_id,
            "bash",
            "-lc",
            f"{_STRIP_PROXY_MANAGED_SNIPPET} TERM=xterm-256color exec {command}",
        ]
        try:
            result = subprocess.run(argv, check=False)
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            raise
        return result.returncode

    def _sandbox_exists(self, sandbox_id: str) -> bool:
        """Return whether a sandbox named *sandbox_id* is listed."""
        result = self._run_sbx(["ls", "--json"])
        if result.returncode != 0:
            raise click.ClickException(
                f"Could not list sbx sandboxes: {result.stderr.strip() or result.stdout.strip()}"
            )
        try:
            parsed = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            parsed = {}
        # Current sbx CLI wraps the list in {"sandboxes": [...]}; older builds
        # returned a bare list. Accept both so tests and legacy CLIs keep working.
        entries = parsed if isinstance(parsed, list) else parsed.get("sandboxes", [])
        return any(entry.get("name") == sandbox_id for entry in entries)

    def resume(self, sandbox_id: str) -> None:
        """
        Verify a managed sandbox still exists so the next ``exec`` wakes it.

        sbx retains the sandbox filesystem across idle-stop and auto-starts
        on the next ``exec``, so ``start_host`` will bring the compute back.
        """
        if not self._sandbox_exists(sandbox_id):
            raise click.ClickException(f"sbx sandbox '{sandbox_id}' not found — cannot resume")
        click.echo(f"  → sandbox '{sandbox_id}' exists; exec will auto-start it")

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is present (``sbx exec``
        auto-starts a stopped one, so no explicit start is needed).

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When it does not exist.
        """
        click.echo(f"▸ Reusing existing sbx sandbox '{sandbox_id}'")
        if not self._sandbox_exists(sandbox_id):
            raise click.ClickException(
                f"sbx sandbox '{sandbox_id}' not found — create one with "
                "`omnigent sandbox create --provider sbx`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Informational: issues no sbx call. Discovery confirmed sbx DOES
        idle-stop a sandbox ~30s after its last session disconnects, but
        there is no "disable idle-stop" knob to set here. The host stays
        up the way it must anyway — the bootstrap's ``connect`` step runs
        ``omnigent host`` in the FOREGROUND via ``sbx exec``, which holds
        a session attached for the host's whole lifetime. A sandbox that
        is merely created but not yet connected may idle-stop; that is
        harmless because ``sbx exec`` auto-starts a stopped sandbox on the
        next call (``run`` during ship, ``exec_foreground`` on connect).

        :param sandbox_id: Unused; present to satisfy the contract.
        """
        click.echo(
            "  → sbx idle-stops a sandbox ~30s after its last session "
            "disconnects; `connect` holds one open. A stopped sandbox is "
            "auto-started on the next exec."
        )

    def terminate(self, sandbox_id: str) -> None:
        """
        Remove a sandbox via ``sbx rm -f``. Idempotent: an already-gone
        sandbox is treated as success.

        :param sandbox_id: The sandbox to remove.
        :raises click.ClickException: When removal of a present sandbox
            fails.
        """
        if not self._sandbox_exists(sandbox_id):
            return
        result = self._run_sbx(["rm", "-f", sandbox_id])
        if result.returncode != 0:
            raise click.ClickException(
                f"Could not remove sbx sandbox '{sandbox_id}': "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )

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
            if value is None and name == OPENCODE_API_KEY_ENV_VAR:
                # The Zen key may live in Omnigent's keychain rather than the
                # local environment — resolve it before failing loud.
                zen = resolve_opencode_zen_key()
                if zen is not None:
                    value = zen[1]
            if value is None:
                raise click.ClickException(
                    f"sbx env passthrough names '{name}' but it is not set in the "
                    f"local environment — set it (or remove it from "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved
