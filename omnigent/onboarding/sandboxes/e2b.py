"""
E2B sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `E2B <https://e2b.dev>`_ sandboxes on top of the official ``e2b``
Python SDK. Same posture as the Modal, Daytona, and CoreWeave launchers:
the SDK is an optional dependency (``pip install 'omnigent[e2b]'``)
imported lazily, so the provider can be listed and the module probed
without it.

Supports both server-managed hosts (``host_type="managed"`` sessions)
and the CLI bootstrap flow. The one unsupported primitive is
``forward_local_port``: E2B only exposes sandbox ports OUTWARD via a
public URL (``sandbox.get_host(port)``) — there is no local→sandbox
path — so the in-sandbox Databricks App OAuth flow doesn't apply and
managed hosts authenticate with a server-minted launch token instead.

Notes that shape this launcher:

- **Templates, not registry images.** Unlike every other launcher, E2B
  cannot boot an arbitrary registry image at create time — it boots from
  a pre-built E2B *template*. The Omnigent host image must therefore be
  built into an E2B template out-of-band (``e2b template build`` from the
  host Dockerfile; see ``deploy/e2b/README.md``) and the launcher's
  ``template`` field names that template — it is NOT a
  ``ghcr.io/...`` reference. The wheel-overlay path
  (:meth:`wheel_install_command`) still applies because the template is
  built FROM the same host image (omnigent ``0.1.0`` baked).
- **Hard lifetime cap, no idle-stop disable.** E2B sandboxes carry a
  single timeout (default 300 s, account-max 24 h on Pro / 1 h on Hobby)
  with no "never expire" option. :meth:`provision` requests the 24 h max;
  :meth:`keep_alive` can only re-extend a live sandbox to that max. A
  managed host outliving the cap relies on the dead-sandbox relaunch path
  (same posture as Modal's 24 h cap).
- **API-key auth.** ``E2B_API_KEY`` is read from the CLI/server process
  environment by the SDK, 12-factor — like the other providers' keys.
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import shlex
import threading
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from e2b import CommandHandle, Sandbox


# ── Constants ──────────────────────────────────────────

API_KEY_ENV_VAR: str = "E2B_API_KEY"
"""E2B API key, read from the CLI/server process environment by the SDK.
Create one at https://e2b.dev/dashboard."""

TEMPLATE_ENV_VAR: str = "OMNIGENT_E2B_TEMPLATE"
"""Environment variable overriding :data:`DEFAULT_E2B_TEMPLATE` — the
NAME (or id) of the pre-built E2B template the Omnigent host image was
built into (``e2b template build``). NOT a registry image reference:
E2B boots from templates, not arbitrary images."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_E2B_SANDBOX_ENV"
"""Comma-separated server-process environment variable NAMES whose
values are injected into every sandbox this launcher creates — typically
the harness LLM credentials (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``,
gateway base URLs, …) and ``GIT_TOKEN`` that the in-sandbox host forwards
to runners. Names, not values: the values are read from the server's own
environment at provision time, so secrets never live in config files.
The server's managed-host config (``sandbox.e2b.env``) takes precedence
when set."""

DEFAULT_E2B_TEMPLATE: str = "omnigent-host"
"""Default E2B template name. Matches the ``--name`` the
``deploy/e2b/README.md`` walkthrough builds the host template with, so a
deployment that followed the guide works without extra config. Override
with the ``template`` field or :data:`TEMPLATE_ENV_VAR`."""

MAX_LIFETIME_ENV_VAR: str = "OMNIGENT_E2B_MAX_LIFETIME_S"
"""Environment variable overriding the requested sandbox lifetime in
seconds (default :data:`_DEFAULT_MAX_LIFETIME_S`, 24 h). E2B caps lifetime
per account — 24 h on Pro, 1 h on Hobby — and the SDK default is only
300 s with no never-expire option, so the timeout is always passed
explicitly. E2B *rejects* (does not clamp) a request above the account
cap, so :meth:`E2BSandboxLauncher.provision` retries clamped to the cap;
set this to the account maximum to skip that retry."""

_DEFAULT_MAX_LIFETIME_S: int = 24 * 60 * 60  # E2B Pro-plan maximum
# Fallback cap used when E2B rejects the requested lifetime but its error
# doesn't state the limit — E2B's Hobby maximum (1 h).
_HOBBY_FALLBACK_LIFETIME_S: int = 60 * 60
# Token TTL slack: the managed launch-token must outlive the sandbox so a
# live sandbox can re-authenticate its tunnel across reconnects.
_TOKEN_TTL_SLACK_S: int = 3600

# Matches E2B's "Timeout cannot be greater than N hours" 400 rejection so
# provision() can retry clamped to the account's actual cap.
_TIMEOUT_REJECTED_RE = re.compile(r"greater than\s+(\d+)\s*hour", re.IGNORECASE)

# E2B caps each command at 60 s by default; 0 disables that per-command
# limit (the SDK documents "Using 0 will not limit the command connection
# time"). A wheel install or git clone must not be killed mid-run.
_COMMAND_NO_TIMEOUT: int = 0

# No _SANDBOX_CPU / _MEMORY constants: E2B bakes resources into the template
# at build time, not at Sandbox.create() (see deploy/e2b/README.md).


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


def _lifetime_cap_from_error(message: str) -> int | None:
    """
    Extract the account's max lifetime (seconds) from E2B's
    timeout-too-large 400 rejection.

    :param message: The SDK exception's string form, e.g.
        ``"400: Timeout cannot be greater than 1 hours"``.
    :returns: The cap in seconds (``3600`` for the example), or ``None``
        when the error is not that rejection (so the caller re-raises it).
    """
    match = _TIMEOUT_REJECTED_RE.search(message)
    if match:
        return int(match.group(1)) * 3600
    if "Timeout cannot be greater than" in message:
        return _HOBBY_FALLBACK_LIFETIME_S
    return None


def _is_missing_template_error(message: str) -> bool:
    """
    Whether an E2B creation error indicates a missing/unknown template.

    A template that was never built (or is misnamed) surfaces as a plain
    ``SandboxException`` — ``"404: template '<name>' not found"`` — NOT a
    ``TemplateException`` (which the SDK reserves for an incompatible/old
    template). This is the most common first-run failure, so the launcher
    detects it to point the operator at the build step.

    :param message: The SDK exception's string form.
    :returns: ``True`` for a missing-template error.
    """
    low = message.lower()
    return "template" in low and ("not found" in low or "404" in low)


def _template_build_hint(template: str, exc: Exception) -> click.ClickException:
    """
    Build the error that points the operator at the E2B template build
    step — for both a missing template and an incompatible one.

    :param template: The template name that failed to resolve.
    :param exc: The underlying SDK exception (appended verbatim).
    :returns: The launcher-contract error to raise.
    """
    return click.ClickException(
        f"E2B sandbox creation failed: template '{template}' is unavailable or "
        "incompatible. Build the Omnigent host image into an E2B template first "
        "(`e2b template build` — see deploy/e2b/README.md), or set the correct "
        f"template via sandbox.e2b.template / {TEMPLATE_ENV_VAR}. ({exc})"
    )


def _ensure_sdk() -> None:
    """
    Verify the E2B SDK is importable, with an install hint when not.

    Called at the top of every launcher entry point because the SDK is
    an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``e2b`` package is not
        installed.
    """
    try:
        import e2b  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The E2B SDK is required for the 'e2b' sandbox provider. "
            "Install it with `pip install 'omnigent[e2b]'`, then set "
            "E2B_API_KEY (create a key at https://e2b.dev/dashboard)."
        ) from exc


def _echo_lines(stream: str, *, err: bool = False) -> None:
    """
    Echo a captured remote output stream line-by-line, dropping
    pure-whitespace lines.

    :param stream: Captured stdout or stderr from a remote command.
    :param err: When ``True``, write to stderr (used for the captured
        stderr stream).
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line, err=err)


class _E2BRemoteProcess(RemoteProcess):
    """
    Thread-backed :class:`RemoteProcess` over an E2B background command.

    E2B delivers a background command's output through ``on_stdout`` /
    ``on_stderr`` callbacks rather than a readable stream, so a worker
    thread drives ``CommandHandle.wait`` with both callbacks feeding one
    queue — the queue is the combined-output stream the
    :class:`RemoteProcess` contract wants.
    """

    def __init__(self, handle: CommandHandle) -> None:
        """
        Wrap a running background command handle.

        :param handle: Handle returned by
            ``sandbox.commands.run(..., background=True)``.
        """
        self._handle = handle
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._returncode: int | None = None
        self._error: BaseException | None = None
        # Materialize the iterator once so repeated `lines` reads resume
        # the same stream (the RemoteProcess contract).
        self._line_iter: Iterator[str] = self._iter_lines()
        self._thread = threading.Thread(target=self._run, name="e2b-remote-process", daemon=True)
        self._thread.start()

    @property
    def lines(self) -> Iterator[str]:
        """
        Iterator over the command's combined stdout/stderr lines (same
        object on every access).

        :returns: Line iterator draining the shared output queue.
        """
        return self._line_iter

    def wait(self) -> int:
        """
        Block until the command finishes and return its exit code.

        :returns: The command's exit code.
        :raises click.ClickException: When the command failed to run at
            the transport level (as opposed to merely exiting non-zero).
        """
        self._thread.join()
        if self._error is not None:
            raise click.ClickException(str(self._error)) from self._error
        return self._returncode if self._returncode is not None else 1

    def close(self) -> None:
        """
        Terminate the command if it is still running.

        Unlike Modal / Islo (whose handles expose no kill), E2B's
        ``CommandHandle.kill`` really stops the remote process, so this
        is a true teardown. Idempotent and best-effort: a process that
        already exited is left alone, and close() never raises.
        """
        with contextlib.suppress(Exception):
            self._handle.kill()

    def _run(self) -> None:
        """Drive the handle to completion, feeding output into the queue."""
        from e2b import CommandExitException

        try:
            result = self._handle.wait(on_stdout=self._enqueue, on_stderr=self._enqueue)
            self._returncode = result.exit_code
        except CommandExitException as exc:
            # A non-zero exit is a normal outcome the caller inspects, not
            # a transport error — CommandExitException IS a CommandResult,
            # so carry its exit code rather than raising.
            self._returncode = exc.exit_code
        except Exception as exc:
            # Any other failure is a transport error surfaced from wait();
            # catch Exception (not BaseException) so KeyboardInterrupt /
            # SystemExit still propagate. The finally below always runs, so
            # the sentinel is queued regardless.
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


class E2BSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for E2B sandboxes, over the ``e2b`` SDK.

    All transport rides the SDK: ``Sandbox.create`` / ``connect`` /
    ``kill`` for lifecycle, ``sandbox.commands.run`` for commands (a
    ``bash -lc`` wrap applies login PATH), ``sandbox.files.write`` for
    file shipping, and a background command for the foreground attach.
    Handles are cached per sandbox id to avoid a server round-trip on
    every primitive.
    """

    provider: ClassVar[str] = "e2b"
    # E2B exposes sandbox ports OUTWARD only (get_host(port) → public
    # URL); there is no local→sandbox path for the App OAuth callback.
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(self, *, template: str | None = None, env: Sequence[str] | None = None) -> None:
        """
        Initialize the launcher.

        :param template: Optional E2B template NAME (or id) to provision
            sandboxes from — the server's managed-host
            ``sandbox.e2b.template`` config. This is an E2B template the
            Omnigent host image was built into (``e2b template build``),
            NOT a registry image reference. ``None`` resolves
            :data:`TEMPLATE_ENV_VAR` and falls back to
            :data:`DEFAULT_E2B_TEMPLATE`.
        :param env: Optional names of server-process environment
            variables to inject into every sandbox, e.g.
            ``["OPENAI_API_KEY", "GIT_TOKEN"]`` — the server's
            managed-host ``sandbox.e2b.env`` config. ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
            and falls back to no injected env.
        """
        self._template_ref = template
        self._env_names = tuple(env) if env is not None else None
        self._sandboxes: dict[str, Sandbox] = {}

    def _resolved_template(self) -> str:
        """
        Resolve the E2B template name: explicit constructor value wins,
        then :data:`TEMPLATE_ENV_VAR`, then :data:`DEFAULT_E2B_TEMPLATE`.

        :returns: The template name to pass to ``Sandbox.create``.
        """
        return self._template_ref or os.environ.get(TEMPLATE_ENV_VAR) or DEFAULT_E2B_TEMPLATE

    def _resolve(self, sandbox_id: str) -> Sandbox:
        """
        Return the cached handle for *sandbox_id*, connecting on first
        use.

        :param sandbox_id: E2B sandbox id, e.g. ``"i1a2b3c4..."``.
        :returns: The sandbox handle.
        :raises click.ClickException: When the SDK is not installed or
            the sandbox does not exist (e.g. reaped after its timeout).
        """
        # The CLI connect flow reaches primitives without a prepare()
        # preflight — keep the missing-SDK error the friendly install hint.
        _ensure_sdk()
        from e2b import Sandbox
        from e2b.exceptions import NotFoundException

        handle = self._sandboxes.get(sandbox_id)
        if handle is None:
            try:
                handle = Sandbox.connect(sandbox_id)
            except NotFoundException as exc:
                raise click.ClickException(
                    f"E2B sandbox '{sandbox_id}' not found — it may have passed its "
                    "lifetime cap. Managed sessions provision a replacement on the "
                    "next message; for a CLI host create a fresh one with "
                    "`omnigent sandbox create --provider e2b`."
                ) from exc
            self._sandboxes[sandbox_id] = handle
        return handle

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created sandboxes.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a credential the deployment
        never provided; silently launching without it would surface much
        later as an opaque harness auth failure).

        :returns: Name → value mapping for ``Sandbox.create(envs=…)``.
        :raises click.ClickException: When a configured name is not set
            in the server process environment.
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
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.e2b.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """
        Local preflight: the E2B SDK must be installed and an API key
        available.

        :raises click.ClickException: When the SDK is missing or
            ``E2B_API_KEY`` is not set.
        """
        _ensure_sdk()
        if not os.environ.get(API_KEY_ENV_VAR):
            raise click.ClickException(
                "No E2B credentials found. Create an API key at "
                "https://e2b.dev/dashboard and set E2B_API_KEY."
            )

    def provision(self, name: str) -> str:
        """
        Create a new E2B sandbox from the host template.

        The sandbox is created at the requested lifetime
        (:func:`resolve_max_lifetime_s`, default E2B's 24 h Pro maximum);
        the SDK default is only 300 s and there is no never-expire option,
        so the timeout is passed explicitly. E2B *rejects* a request above
        the account cap (e.g. a Hobby account's 1 h), so creation retries
        once clamped to that cap. The sandbox lives until the managed-
        session machinery terminates it or the timeout lapses (a managed
        host outliving the cap relies on the dead-sandbox relaunch path).

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Recorded as sandbox metadata; the returned id is the
            canonical reference.
        :returns: The E2B sandbox id.
        :raises click.ClickException: If provisioning fails (including a
            template that has not been built yet).
        """
        _ensure_sdk()
        template = self._resolved_template()
        env_vars = self._resolve_sandbox_env()
        click.echo(f"▸ Creating E2B sandbox '{name}' from template '{template}'")
        sandbox = self._create_sandbox(template, resolve_max_lifetime_s(), name, env_vars)
        sandbox_id = sandbox.sandbox_id
        self._sandboxes[sandbox_id] = sandbox
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def _create_sandbox(
        self, template: str, timeout: int, name: str, env_vars: dict[str, str]
    ) -> Sandbox:
        """
        Create one sandbox, retrying once clamped to the account's cap if
        E2B rejects the requested lifetime as too large.

        :param template: E2B template name to boot from.
        :param timeout: Requested lifetime in seconds.
        :param name: Human-readable label (recorded as metadata).
        :param env_vars: Resolved env vars to inject (empty for none).
        :returns: The created sandbox handle.
        :raises click.ClickException: If creation fails for any reason
            other than a clampable timeout rejection.
        """
        from e2b import Sandbox
        from e2b.exceptions import AuthenticationException, SandboxException, TemplateException

        metadata = {"omnigent-name": name}
        try:
            return Sandbox.create(
                template=template, timeout=timeout, metadata=metadata, envs=env_vars or None
            )
        except AuthenticationException as exc:
            # A bad/expired key (HTTP 401) raises AuthenticationException,
            # which does NOT extend SandboxException — catch it explicitly so
            # it surfaces as a credential hint instead of escaping raw.
            raise click.ClickException(
                "E2B authentication failed — check E2B_API_KEY (create a key at "
                f"https://e2b.dev/dashboard). ({exc})"
            ) from exc
        except TemplateException as exc:
            # TemplateException is reserved for an incompatible/old template,
            # not a missing one (that's a generic SandboxException, below).
            raise _template_build_hint(template, exc) from exc
        except SandboxException as exc:
            cap = _lifetime_cap_from_error(str(exc))
            if cap is not None and cap < timeout:
                pass  # account-cap rejection → fall through to the clamp-retry
            elif _is_missing_template_error(str(exc)):
                # The most common first-run failure: the host image was never
                # built into an E2B template (E2B returns "404: template … not
                # found" as a plain SandboxException). Point at the build step.
                raise _template_build_hint(template, exc) from exc
            else:
                # SDK boundary: surface the provider's reason (quota, …) as the
                # launcher-contract error type so the managed-launch 502 carries
                # it verbatim, not a generic message.
                raise click.ClickException(f"E2B sandbox creation failed: {exc}") from exc
        # The requested lifetime exceeds this account's maximum (e.g. a
        # Hobby account's 1 h cap vs the 24 h default) and E2B rejected it
        # rather than clamping — retry once at the cap.
        click.secho(
            f"  → requested {timeout // 3600}h lifetime exceeds this E2B account's "
            f"maximum ({cap // 3600}h); retrying clamped to it (set "
            f"{MAX_LIFETIME_ENV_VAR} to request a specific lifetime).",
            fg="yellow",
        )
        try:
            return Sandbox.create(
                template=template, timeout=cap, metadata=metadata, envs=env_vars or None
            )
        except SandboxException as exc:
            raise click.ClickException(f"E2B sandbox creation failed: {exc}") from exc

    def attach(self, sandbox_id: str) -> None:
        """
        Validate that an existing sandbox is still running.

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When the sandbox is missing or has
            terminated (E2B sandboxes are reaped at their timeout).
        """
        click.echo(f"▸ Reusing existing E2B sandbox '{sandbox_id}'")
        handle = self._resolve(sandbox_id)
        if not handle.is_running():
            raise click.ClickException(
                f"E2B sandbox '{sandbox_id}' is not running (it may have passed its "
                "lifetime cap). Create a fresh one with "
                "`omnigent sandbox create --provider e2b`."
            )

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Re-extend the sandbox timeout to the requested lifetime.

        E2B exposes no idle-autostop to disable and no never-expire
        option, so the best available keep-alive is to set the timeout to
        the requested maximum (:func:`resolve_max_lifetime_s`), measured
        from now. Soft-fail per the launcher contract: a rejected setting
        (e.g. a Hobby account whose max is below the request) warns rather
        than aborting the bootstrap.

        :param sandbox_id: The sandbox to configure.
        """
        from e2b.exceptions import SandboxException

        lifetime = resolve_max_lifetime_s()
        handle = self._resolve(sandbox_id)
        try:
            handle.set_timeout(lifetime)
        except SandboxException as exc:
            click.secho(
                f"  → warning: could not extend the lifetime of '{sandbox_id}' "
                f"({exc}); the sandbox will stop at its current timeout.",
                fg="yellow",
            )
        else:
            # set_timeout accepts an over-cap request without raising (unlike
            # create, which rejects it — verified live on a capped account),
            # so report the REQUEST rather than claim a grant.
            click.echo(
                f"  → requested a {lifetime // 3600}h lifetime extension "
                "(capped at the account maximum; E2B has no idle-stop disable)."
            )

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        ``bash -lc`` wraps the command so login PATH applies. E2B caps
        each command at 60 s by default; the per-command timeout is
        disabled here so installs / clones aren't killed mid-run.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the command could not be
            executed, or *check* is ``True`` and it exited non-zero.
        """
        from e2b import CommandExitException
        from e2b.exceptions import SandboxException

        handle = self._resolve(sandbox_id)
        wrapped = f"bash -lc {shlex.quote(command)}"
        try:
            result = handle.commands.run(wrapped, timeout=_COMMAND_NO_TIMEOUT)
            returncode, stdout, stderr = result.exit_code, result.stdout, result.stderr
        except CommandExitException as exc:
            # E2B raises on non-zero exit; the exception IS a CommandResult,
            # so read its captured output/code rather than treating it as a
            # transport failure (which would break check=False callers).
            returncode, stdout, stderr = exc.exit_code, exc.stdout, exc.stderr
        except SandboxException as exc:
            # SDK boundary: a stopped/deleted sandbox or daemon outage must
            # surface its provider reason through the launcher contract.
            raise click.ClickException(
                f"Remote command failed to execute on sandbox '{sandbox_id}': {exc}"
            ) from exc
        _echo_lines(stdout)
        _echo_lines(stderr, err=True)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on sandbox '{sandbox_id}' (exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox via the SDK's filesystem API.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        from e2b.exceptions import SandboxException

        handle = self._resolve(sandbox_id)
        try:
            handle.files.write(remote_path, local_path.read_bytes())
        except SandboxException as exc:
            raise click.ClickException(
                f"File upload to sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its combined output
        line by line.

        E2B background commands deliver stdout and stderr through
        separate callbacks; the wrapping :class:`_E2BRemoteProcess`
        routes both into one queue, so the *pty* flag is unused — the
        output is already combined either way (mirrors the Islo
        launcher).

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: Accepted for the contract; unused (see above).
        :returns: Handle over the streaming process.
        :raises click.ClickException: When the command cannot be started.
        """
        del pty  # unused (see docstring)
        from e2b.exceptions import SandboxException

        handle = self._resolve(sandbox_id)
        try:
            # timeout=0 disables the SDK's default 60s per-command cap — this
            # backs exec_foreground, which holds `omnigent host` open for the
            # whole session; the cap would otherwise tear it down after 60s.
            process = handle.commands.run(
                f"bash -lc {shlex.quote(command)}",
                background=True,
                timeout=_COMMAND_NO_TIMEOUT,
            )
        except SandboxException as exc:
            raise click.ClickException(
                f"Could not start a streaming command on sandbox '{sandbox_id}': {exc}"
            ) from exc
        return _E2BRemoteProcess(process)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output to the local
        terminal until it exits; Ctrl-C kills the remote process and
        re-raises.

        ``TERM`` is forced to ``xterm-256color`` for the same reason as
        the other launchers: native harnesses spawn tmux, which refuses
        to start under a dumb/unset TERM. ``exec`` replaces the wrapping
        shell so the streamed command's own exit code is reported.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote
            process when the user detaches with Ctrl-C.
        """
        process = self.stream_exec(sandbox_id, f"TERM=xterm-256color exec {command}", pty=True)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            # E2B's command handle exposes a real kill, so this genuinely
            # tears the remote process down (unlike Modal / Islo).
            process.close()
            raise

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that overlays the shipped wheels onto the host
        template — see
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        for the flag rationale. Applies because the E2B template is built
        FROM the prebaked host image (omnigent ``0.1.0`` baked).

        :param remote_tgz_path: Sandbox path of the shipped tarball,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Kill a sandbox, releasing its compute.

        Idempotent from the caller's perspective: a sandbox that no
        longer exists (already killed or aged past its timeout) is
        treated as success — the desired end state holds.

        :param sandbox_id: The sandbox to kill.
        """
        _ensure_sdk()
        from e2b import Sandbox
        from e2b.exceptions import NotFoundException

        try:
            # Static kill resolves the id directly (no need to connect a
            # cached handle first); returns False for an already-gone
            # sandbox, which is the desired end state.
            Sandbox.kill(sandbox_id)
        except NotFoundException:
            pass  # already gone — success
        finally:
            self._sandboxes.pop(sandbox_id, None)
