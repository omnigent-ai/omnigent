"""
Declaw sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Declaw <https://declaw.ai>`_ secure microVM sandboxes on top of the
official ``declaw`` Python SDK. Same posture as the E2B / Modal / Daytona
/ CoreWeave launchers: the SDK is an optional dependency (``pip install
'omnigent[declaw]'``) imported lazily, so the provider can be listed and
the module probed without it.

Supports both server-managed hosts (``host_type="managed"`` sessions) and
the CLI bootstrap flow. The one unsupported primitive is
``forward_local_port``: Declaw has no local→sandbox path, so the App OAuth
flow doesn't apply — managed hosts authenticate with a server-minted
launch token instead (same posture as E2B / CoreWeave).

What sets this launcher apart from every other provider is that Declaw is
not just compute — it is a **security plane**. :meth:`provision` attaches a
Declaw ``SecurityPolicy`` (PII redaction, prompt-injection defense, network
egress policy, credential vault, governance/OPA custom policy, audit) to the
sandbox. That policy is built entirely from Declaw's own SDK config classes
(:meth:`_build_security_policy`); Omnigent's local sandbox policy layer
(``omnigent.inner``) is not involved.

Notes that shape this launcher:

- **Templates, not registry images.** Like E2B, Declaw boots from a named
  *template* baked ahead of time, not an arbitrary ``ghcr.io/...`` image.
  The Omnigent host image must be built into a Declaw template out-of-band
  (see ``deploy/declaw/README.md``); :data:`DEFAULT_DECLAW_TEMPLATE` names
  it. The wheel-overlay path (:meth:`wheel_install_command`) still applies
  because the template is built FROM the prebaked host image.
- **Secure-by-default.** With no ``security`` config, the sandbox gets a
  *balanced* policy: PII redaction + audit on, the injection cascade
  configured but scanning no domains yet (opt-in per domain), so it never
  blocks the agent's own model endpoint. Operators dial it up via the
  ``sandbox.declaw.security`` block (managed) or
  :data:`SECURITY_MODE_ENV_VAR` (CLI).
- **API-key auth.** ``DECLAW_API_KEY`` (and optional ``DECLAW_DOMAIN``) are
  read from the CLI/server process environment by the SDK, 12-factor — like
  the other providers' keys.
"""

from __future__ import annotations

import os
import queue
import shlex
import threading
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import click

from omnigent.inner import ui
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from declaw import Sandbox, SecurityPolicy
    from declaw.sandbox_sync.commands.commands import Commands


# ── Constants ──────────────────────────────────────────

API_KEY_ENV_VAR: str = "DECLAW_API_KEY"
"""Declaw API key, read from the CLI/server process environment by the SDK.
An optional ``DECLAW_DOMAIN`` is read by the SDK the same way (12-factor)."""

TEMPLATE_ENV_VAR: str = "OMNIGENT_DECLAW_TEMPLATE"
"""Environment variable overriding :data:`DEFAULT_DECLAW_TEMPLATE` — the
NAME of the Declaw template the Omnigent host image was built into (see
``deploy/declaw/README.md``). NOT a registry image reference."""

DEFAULT_DECLAW_TEMPLATE: str = "omnigent-host"
"""Default Declaw template name — matches the ``--name`` the
``deploy/declaw/README.md`` walkthrough builds the host template with."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_DECLAW_SANDBOX_ENV"
"""Comma-separated server-process environment variable NAMES whose values
are injected into every sandbox this launcher creates — typically the
harness LLM credentials (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, …) and
``GIT_TOKEN``. Names, not values: the values are read from the server's own
environment at provision time, so secrets never live in config files. The
server's managed-host config (``sandbox.declaw.env``) takes precedence when
set. For secrets that must never enter the VM at all, use Declaw vault refs
(``sandbox.declaw.vault_refs``) instead."""

MAX_LIFETIME_ENV_VAR: str = "OMNIGENT_DECLAW_MAX_LIFETIME_S"
"""Environment variable overriding the requested sandbox lifetime in
seconds (default :data:`_DEFAULT_MAX_LIFETIME_S`, 24 h). Declaw caps the
lifetime per tier; set this to your tier's maximum for long-lived hosts."""

SECURITY_MODE_ENV_VAR: str = "OMNIGENT_DECLAW_SECURITY_MODE"
"""Environment variable selecting the secure-by-default injection posture
for the CLI bootstrap path (where there is no ``sandbox.declaw.security``
block). One of ``strict`` / ``balanced`` / ``permissive`` / ``agentic-tool``
/ ``data-egress-sensitive``. Default :data:`_DEFAULT_SECURITY_MODE`."""

_DEFAULT_MAX_LIFETIME_S: int = 24 * 60 * 60
# Token TTL slack: the managed launch token must outlive the sandbox so a
# live sandbox can re-authenticate its tunnel across reconnects.
_TOKEN_TTL_SLACK_S: int = 3600
# Foreground-command cap. Declaw's commands.run defaults to 60 s; a wheel
# install or git clone must not be killed mid-run, so run() raises it. The
# managed `omnigent host` is detached via run_background (setsid nohup) and
# is not bound by this.
_RUN_TIMEOUT_S: int = 30 * 60
_DEFAULT_SECURITY_MODE: str = "balanced"
# The set of accepted `security` keys (mode / governance_pack / pii /
# injection_defense / network / audit / … / policy_ref / inline_rego) is
# validated structurally in server/managed_hosts.py; the launcher maps
# whatever it receives, so it stays lenient here.


def resolve_max_lifetime_s() -> int:
    """
    Resolve the requested sandbox lifetime in seconds.

    :data:`MAX_LIFETIME_ENV_VAR` overrides the 24 h default.

    :returns: The lifetime to request at sandbox creation.
    :raises click.ClickException: When the env override is not a number.
    """
    raw = os.environ.get(MAX_LIFETIME_ENV_VAR)
    if raw is None:
        return _DEFAULT_MAX_LIFETIME_S
    try:
        return int(float(raw))
    except ValueError as exc:
        raise click.ClickException(f"{MAX_LIFETIME_ENV_VAR} must be a number of seconds") from exc


def managed_token_ttl_s() -> int:
    """
    Launch-token TTL for the managed path, derived from (and always above)
    the sandbox lifetime so the token outlives the sandbox across tunnel
    reconnects.

    :returns: The token lifetime in seconds.
    """
    return resolve_max_lifetime_s() + _TOKEN_TTL_SLACK_S


def _ensure_sdk() -> None:
    """
    Verify the Declaw SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is an
    optional dependency — the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When the ``declaw`` package is not installed.
    """
    try:
        import declaw  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The Declaw SDK is required for the 'declaw' sandbox provider. "
            "Install it with `pip install 'omnigent[declaw]'`, then set "
            "DECLAW_API_KEY (and optionally DECLAW_DOMAIN)."
        ) from exc


def _echo_lines(stream: str, *, err: bool = False) -> None:
    """
    Echo a captured remote output stream line-by-line, dropping
    pure-whitespace lines.

    :param stream: Captured stdout or stderr from a remote command.
    :param err: When ``True``, write to stderr.
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line, err=err)


def _as_mapping(value: object) -> dict[str, Any]:
    """Return *value* as a plain dict when it is a mapping, else ``{}``."""
    return dict(value) if isinstance(value, Mapping) else {}


def _normalize_network(value: object) -> dict[str, Any]:
    """
    Normalize a ``security.network`` block to Declaw's ``NetworkPolicy`` shape.

    Accepts the ergonomic ``allow`` / ``deny`` aliases (matching the
    documented config) and maps them to Declaw's ``allow_out`` / ``deny_out``.
    """
    out = _as_mapping(value)
    if "allow" in out and "allow_out" not in out:
        out["allow_out"] = out.pop("allow")
    if "deny" in out and "deny_out" not in out:
        out["deny_out"] = out.pop("deny")
    return out


class _DeclawRemoteProcess(RemoteProcess):
    """
    Thread-backed :class:`RemoteProcess` over a Declaw streaming command.

    Declaw's real-time output rides ``commands.run_stream`` (SSE), which
    blocks the calling thread until the process exits while invoking
    ``on_stdout`` / ``on_stderr`` per line. A worker thread drives it with
    both callbacks feeding one queue — the queue is the combined-output
    stream the :class:`RemoteProcess` contract wants.
    """

    def __init__(self, commands: Commands, command: str) -> None:
        """
        Wrap a streaming command.

        :param commands: The sandbox's ``commands`` module.
        :param command: Shell command to stream (already ``bash -lc`` wrapped).
        """
        self._commands = commands
        self._command = command
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._returncode: int | None = None
        self._error: BaseException | None = None
        # Materialize the iterator once so repeated `lines` reads resume the
        # same stream (the RemoteProcess contract).
        self._line_iter: Iterator[str] = self._iter_lines()
        self._thread = threading.Thread(
            target=self._run, name="declaw-remote-process", daemon=True
        )
        self._thread.start()

    @property
    def lines(self) -> Iterator[str]:
        """Iterator over the command's combined output lines (stable object)."""
        return self._line_iter

    def wait(self) -> int:
        """
        Block until the command finishes and return its exit code.

        :returns: The command's exit code.
        :raises click.ClickException: When the command failed to run at the
            transport level.
        """
        self._thread.join()
        if self._error is not None:
            raise click.ClickException(str(self._error)) from self._error
        return self._returncode if self._returncode is not None else 1

    def close(self) -> None:
        """
        Best-effort teardown.

        DEVIATION from the base contract: Declaw's real-time stream
        (``run_stream`` over SSE) exposes no remote pid, so a still-running
        process cannot be force-killed from here — it is reaped when the
        sandbox is terminated. Unreachable for the App-OAuth flow (gated off
        for Declaw via ``supports_local_port_forward=False``); for
        :meth:`exec_foreground` (CLI connect) the user's Ctrl-C abandons the
        daemon thread and the remote host stops with the sandbox. Mirrors the
        CoreWeave launcher's documented deviation.
        """

    def _run(self) -> None:
        """Drive the stream to completion, feeding output into the queue."""
        try:
            # timeout = the sandbox lifetime so a long-lived `omnigent host`
            # isn't cut by the SDK's default 60 s per-command read timeout.
            result = self._commands.run_stream(
                self._command,
                on_stdout=self._enqueue,
                on_stderr=self._enqueue,
                timeout=resolve_max_lifetime_s(),
            )
            self._returncode = result.exit_code
        # Catch Exception (not BaseException) so KeyboardInterrupt/SystemExit
        # still propagate; transport errors are surfaced through wait().
        except Exception as exc:
            self._error = exc
        finally:
            self._lines.put(None)

    def _iter_lines(self) -> Iterator[str]:
        """Yield queued output lines until the terminating sentinel."""
        while True:
            item = self._lines.get()
            if item is None:
                return
            yield item

    def _enqueue(self, text: str) -> None:
        """Split a callback chunk into newline-terminated lines and queue them."""
        for line in text.splitlines(keepends=True):
            self._lines.put(line)
        if text and not text.endswith(("\n", "\r")):
            self._lines.put("\n")


class DeclawSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Declaw sandboxes, over the ``declaw`` SDK.

    All transport rides the SDK: ``Sandbox.create`` / ``connect`` / ``kill``
    for lifecycle, ``sandbox.commands.run`` for commands (a ``bash -lc``
    wrap applies login PATH), ``sandbox.files.write`` for file shipping, and
    ``commands.run_stream`` for the foreground attach. Handles are cached per
    sandbox id to avoid a server round-trip on every primitive.
    """

    provider: ClassVar[str] = "declaw"
    # Declaw exposes no local→sandbox path; managed hosts authenticate with
    # the server-minted launch token instead of the App OAuth callback.
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        template: str | None = None,
        env: Sequence[str] | None = None,
        security: Mapping[str, Any] | None = None,
        vault_refs: Mapping[str, str] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param template: Optional Declaw template NAME to provision sandboxes
            from — the server's ``sandbox.declaw.template`` config. ``None``
            resolves :data:`TEMPLATE_ENV_VAR` then :data:`DEFAULT_DECLAW_TEMPLATE`.
        :param env: Optional names of server-process environment variables to
            inject into every sandbox — the server's ``sandbox.declaw.env``
            config. ``None`` resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param security: Optional Declaw security config (``sandbox.declaw.
            security``) — a mapping of curated knobs (``mode``,
            ``governance_pack``, ``pii``, ``injection_defense``, ``network``,
            ``audit``, …) plus a ``policy_ref`` / ``inline_rego`` escape hatch.
            ``None``/empty → the balanced secure-by-default policy.
        :param vault_refs: Optional Declaw vault references
            (``sandbox.declaw.vault_refs``) mapping an in-sandbox env var name
            to a stored vault secret name — the secret value never enters the
            VM. Used for harness LLM credentials (the Declaw-native alternative
            to plaintext ``env`` passthrough).
        """
        self._template_ref = template
        self._env_names = tuple(env) if env is not None else None
        self._security = dict(security) if security else None
        self._vault_refs = dict(vault_refs) if vault_refs else None
        self._sandboxes: dict[str, Sandbox] = {}

    # ── helpers ────────────────────────────────────────

    def _resolved_template(self) -> str:
        """Resolve the template name: constructor > env > default."""
        return self._template_ref or os.environ.get(TEMPLATE_ENV_VAR) or DEFAULT_DECLAW_TEMPLATE

    def _resolve(self, sandbox_id: str) -> Sandbox:
        """
        Return the cached handle for *sandbox_id*, connecting on first use.

        :raises click.ClickException: When the SDK is not installed or the
            sandbox does not exist (e.g. killed or aged past its lifetime).
        """
        _ensure_sdk()
        from declaw import Sandbox
        from declaw.exceptions import NotFoundException

        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            try:
                handle = Sandbox.connect(sandbox_id)
            except NotFoundException as exc:
                raise click.ClickException(
                    f"Declaw sandbox '{sandbox_id}' not found — it may have been "
                    "killed or passed its lifetime. Managed sessions provision a "
                    "replacement on the next message; for a CLI host create a fresh "
                    "one with `omnigent sandbox create --provider declaw`."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies.
        Values come from the server's own environment — a configured name
        that is unset there fails loud.

        :returns: Name → value mapping for ``Sandbox.create(envs=…)``.
        :raises click.ClickException: When a configured name is not set in the
            server process environment.
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in the "
                    "server's environment — set it (or remove it from "
                    f"sandbox.declaw.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def _build_security_policy(self) -> SecurityPolicy:
        """
        Build the Declaw ``SecurityPolicy`` for created sandboxes.

        With no config (``self._security`` empty), returns the balanced
        secure-by-default policy: PII redaction + audit on, the injection
        cascade configured but scanning no domains (so it never blocks the
        agent's own model endpoint). The CLI path can pick the posture via
        :data:`SECURITY_MODE_ENV_VAR`.

        With a config mapping, maps the curated knobs to Declaw's own config
        classes — ``mode`` enables the full injection cascade, the rest layer
        on declaw-shaped sub-blocks, and ``governance_pack`` / ``policy_ref`` /
        ``inline_rego`` drive the OPA custom policy (the escape hatch). Fields
        omitted from the block take Declaw's defaults (so omit the whole block
        for the balanced default).
        """
        from declaw import (
            AuditConfig,
            CodeSecurityConfig,
            ContentGateConfig,
            CustomPolicyConfig,
            InjectionDefenseConfig,
            NetworkPolicy,
            PIIConfig,
            SecurityPolicy,
            ToxicityConfig,
        )

        if not self._security:
            mode = os.environ.get(SECURITY_MODE_ENV_VAR, _DEFAULT_SECURITY_MODE)
            policy = SecurityPolicy.full_injection_defense(mode=mode)
            policy.pii = PIIConfig(enabled=True, action="redact")
            return policy

        sec = dict(self._security)
        mode = sec.pop("mode", None)
        agent_policy = sec.pop("agent_policy", None)
        governance_pack = sec.pop("governance_pack", None)
        policy_ref = sec.pop("policy_ref", None)
        inline_rego = sec.pop("inline_rego", None)
        inline_modules = sec.pop("inline_modules", None)
        inj_dict = _as_mapping(sec.get("injection_defense"))

        if mode:
            judge = inj_dict.get("judge")
            judge_policy = judge.get("policy") if isinstance(judge, Mapping) else None
            policy = SecurityPolicy.full_injection_defense(
                mode=mode,
                agent_policy=agent_policy or judge_policy,
                action=inj_dict.get("action", "block"),
                domains=inj_dict.get("domains"),
                threshold=inj_dict.get("threshold", 0.95),
            )
        else:
            policy = SecurityPolicy()

        if "pii" in sec:
            policy.pii = PIIConfig.from_dict(_as_mapping(sec["pii"]))
        if "injection_defense" in sec and not mode:
            policy.injection_defense = InjectionDefenseConfig.from_dict(inj_dict)
        if "network" in sec:
            policy.network = NetworkPolicy.from_dict(_normalize_network(sec["network"]))
        if "audit" in sec:
            audit = sec["audit"]
            policy.audit = (
                AuditConfig.from_dict(audit) if isinstance(audit, Mapping) else bool(audit)
            )
        if "toxicity" in sec:
            policy.toxicity = ToxicityConfig.from_dict(_as_mapping(sec["toxicity"]))
        if "code_security" in sec:
            policy.code_security = CodeSecurityConfig.from_dict(_as_mapping(sec["code_security"]))
        if "content_gate" in sec:
            policy.content_gate = ContentGateConfig.from_dict(_as_mapping(sec["content_gate"]))

        if governance_pack or policy_ref or inline_rego or inline_modules:
            custom = policy.custom_policy or CustomPolicyConfig()
            custom.enabled = True
            ref = policy_ref or governance_pack
            if ref:
                custom.policy_ref = ref
            if inline_rego:
                custom.inline_rego = inline_rego
            if inline_modules:
                custom.inline_modules = list(inline_modules)
            policy.custom_policy = custom

        return policy

    def _template_build_hint(self, template: str, exc: Exception) -> click.ClickException:
        """Build the error pointing the operator at the Declaw template build step."""
        return click.ClickException(
            f"Declaw sandbox creation failed: template '{template}' is unavailable. "
            "Build the Omnigent host image into a Declaw template first (see "
            "deploy/declaw/README.md), or set the correct template via "
            f"sandbox.declaw.template / {TEMPLATE_ENV_VAR}. ({exc})"
        )

    # ── lifecycle ──────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: the Declaw SDK must be installed and an API key set.

        :raises click.ClickException: When the SDK is missing or
            ``DECLAW_API_KEY`` is not set.
        """
        _ensure_sdk()
        if not os.environ.get(API_KEY_ENV_VAR):
            raise click.ClickException(
                "No Declaw credentials found — set DECLAW_API_KEY (and optionally "
                "DECLAW_DOMAIN). Create a key in the Declaw dashboard."
            )

    def provision(self, name: str) -> str:
        """
        Create a new Declaw sandbox from the host template, wrapped in the
        resolved ``SecurityPolicy``.

        :param name: Human-readable label, recorded as sandbox metadata; the
            returned id is the canonical reference.
        :returns: The Declaw sandbox id.
        :raises click.ClickException: If provisioning fails (including a
            template that has not been built yet).
        """
        _ensure_sdk()
        from declaw import Sandbox
        from declaw.exceptions import (
            AuthenticationException,
            NotFoundException,
            SandboxException,
            TemplateException,
        )

        template = self._resolved_template()
        lifetime = resolve_max_lifetime_s()
        env_vars = self._resolve_sandbox_env()
        policy = self._build_security_policy()
        click.echo(f"▸ Creating Declaw sandbox '{name}' from template '{template}'")
        try:
            sandbox = Sandbox.create(
                template=template,
                timeout=lifetime,
                metadata={"omnigent-name": name},
                envs=env_vars or None,
                vault_refs=self._vault_refs or None,
                security=policy,
                allow_internet_access=True,
            )
        except AuthenticationException as exc:
            raise click.ClickException(
                f"Declaw authentication failed — check DECLAW_API_KEY. ({exc})"
            ) from exc
        except (TemplateException, NotFoundException) as exc:
            raise self._template_build_hint(template, exc) from exc
        except SandboxException as exc:
            raise click.ClickException(f"Declaw sandbox creation failed: {exc}") from exc
        sandbox_id: str = sandbox.sandbox_id
        self._sandboxes[sandbox_id] = sandbox
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is still running.

        :raises click.ClickException: When the sandbox is missing or stopped.
        """
        click.echo(f"▸ Reusing existing Declaw sandbox '{sandbox_id}'")
        handle = self._resolve(sandbox_id)
        if not handle.is_running():
            raise click.ClickException(
                f"Declaw sandbox '{sandbox_id}' is not running (it may have been "
                "killed or passed its lifetime). Create a fresh one with "
                "`omnigent sandbox create --provider declaw`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Re-extend the sandbox timeout to the requested lifetime.

        Soft-fail per the launcher contract: a rejected setting warns rather
        than aborting the bootstrap.
        """
        from declaw.exceptions import SandboxException

        lifetime = resolve_max_lifetime_s()
        handle = self._resolve(sandbox_id)
        try:
            handle.set_timeout(lifetime)
        except SandboxException as exc:
            ui.console.print(
                f"  → warning: could not extend the lifetime of '{sandbox_id}' "
                f"({exc}); the sandbox will stop at its current timeout.",
                style="omni.warning",
                markup=False,
            )
        else:
            click.echo(f"  → extended sandbox lifetime to {lifetime // 3600}h")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        ``bash -lc`` wraps the command so login PATH applies. The per-command
        timeout is raised to :data:`_RUN_TIMEOUT_S` so installs / clones
        aren't killed mid-run.

        :raises click.ClickException: If the command could not be executed, or
            *check* is ``True`` and it exited non-zero.
        """
        from declaw.exceptions import SandboxException

        handle = self._resolve(sandbox_id)
        wrapped = f"bash -lc {shlex.quote(command)}"
        try:
            result = handle.commands.run(
                wrapped, timeout=_RUN_TIMEOUT_S, request_timeout=_RUN_TIMEOUT_S
            )
        except SandboxException as exc:
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        _echo_lines(result.stdout)
        _echo_lines(result.stderr, err=True)
        if check and result.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' "
                f"(exit {result.exit_code}): {command}"
            )
        return RemoteCommandResult(
            returncode=result.exit_code, stdout=result.stdout, stderr=result.stderr
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the SDK's filesystem API.

        :raises click.ClickException: If the transfer fails.
        """
        from declaw.exceptions import FileUploadException, SandboxException

        handle = self._resolve(sandbox_id)
        try:
            handle.files.write(remote_path, local_path.read_bytes())
        except (FileUploadException, SandboxException) as exc:
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its combined output line by
        line.

        Declaw streams via ``commands.run_stream`` (SSE); the wrapping
        :class:`_DeclawRemoteProcess` routes stdout + stderr into one queue,
        so the *pty* flag is unused — the output is already combined.

        :raises click.ClickException: When the command cannot be started.
        """
        del pty  # unused (see docstring)
        handle = self._resolve(sandbox_id)
        return _DeclawRemoteProcess(handle.commands, f"bash -lc {shlex.quote(command)}")

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output to the local terminal
        until it exits; Ctrl-C detaches.

        ``TERM`` is forced to ``xterm-256color`` (native harnesses spawn tmux,
        which refuses to start under a dumb/unset TERM). ``exec`` replaces the
        wrapping shell so the streamed command's own exit code is reported.

        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after detaching when the user
            interrupts with Ctrl-C.
        """
        process = self.stream_exec(sandbox_id, f"TERM=xterm-256color exec {command}", pty=True)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo(
                "\n  → detaching; the remote host keeps running until the sandbox is killed"
            )
            process.close()
            raise

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the host template
        — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`.
        Applies because the Declaw template is built FROM the prebaked host
        image.
        """
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Kill a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no longer
        exists is treated as success. Unlike E2B's static kill, Declaw's
        ``kill`` is an instance method, so terminate connects (or reuses the
        cached handle) first.
        """
        _ensure_sdk()
        from declaw import Sandbox
        from declaw.exceptions import NotFoundException, SandboxException

        try:
            handle = self._sandboxes.get(sandbox_id) or Sandbox.connect(sandbox_id)
            handle.kill(wait=True)
        except NotFoundException:
            pass  # already gone — the desired end state holds
        except SandboxException as exc:
            raise click.ClickException(
                f"Failed to kill Declaw sandbox '{sandbox_id}': {exc}"
            ) from exc
        finally:
            self._sandboxes.pop(sandbox_id, None)
