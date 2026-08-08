"""
Databricks Sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Databricks Sandbox
<https://docs.databricks.com/aws/en/compute/serverless/sandbox>`_ —
SSH-accessible, microVM-isolated personal development environments.

Unlike every other provider in this package, the transport is not a
Python SDK: it is the **public** ``databricks sandbox`` CLI surface
(``create`` / ``delete`` / ``list`` / ``start`` / ``stop`` / ``status`` /
``ssh`` / ``config``), which went GA when the Databricks CLI renamed
``databricks lakebox`` to ``databricks sandbox``
(https://github.com/databricks/cli/pull/5487). There is therefore no
optional Python extra to install and no internal dependency — an
operator who can run ``databricks sandbox ssh`` by hand can run this
provider. Commands are built as argv lists and executed WITHOUT a shell
locally, so a sandbox id or path never reaches a local shell.

Platform notes that shape this launcher:

- **Real SSH means real port forwarding.** ``databricks sandbox ssh <id>
  -- -L <port>:localhost:<port>`` passes flags straight through to
  ``ssh``, so this is the one exec-model provider in the tree with
  :attr:`supports_local_port_forward` ``True``. Modal, Daytona, and Islo
  all lack a local→sandbox path and must skip the in-sandbox App OAuth
  flow; this provider does not.
- **But the transport allocates no remote PTY.** ``-- -tt <cmd>`` was
  verified against CLI v1.11.0 to still run the remote command with
  ``tty`` reporting "not a tty". :meth:`stream_exec` therefore ignores
  its ``pty`` argument, and :meth:`exec_foreground` runs without a
  controlling terminal. CLIs that suppress output when not attached to a
  terminal — including the ``databricks auth login`` step of the
  in-sandbox App OAuth flow — may degrade. See the module's open
  questions in the accompanying issue.
- **Stop/start with persistent storage.** Sandboxes auto-stop on idle and
  restart with their disk intact, so a dormant managed host is resumed in
  place under the SAME sandbox id (:attr:`can_resume` ``True``) rather
  than reprovisioned. That matters more here than elsewhere: Databricks
  Sandbox enforces a per-user cap on sandbox count, so a
  provision-per-session deployment would eventually hit the ceiling.
- **No image selection and no create-time env injection.** The CLI's
  ``create`` takes only ``--name``; the sandbox image is Databricks'
  own (Ubuntu 24.04, user ``sandbox-agent``) and already ships git,
  tmux, python3, uv, node, and the coding-harness CLIs, so the generic
  :meth:`~omnigent.onboarding.sandboxes.base.ExecModelHostLauncher.start_host`
  bootstrap works unmodified. There is deliberately no ``env``
  passthrough knob: with no create-time env API, the only way to inject
  credentials would be to prefix them onto the remote argv of every
  exec, which exposes them in the sandbox's process table.
- **PEP 668 image.** The sandbox's Python is externally managed
  (``/usr/lib/python3.12/EXTERNALLY-MANAGED``) and its site-packages are
  not writable by ``sandbox-agent``, so :meth:`wheel_install_command`
  passes ``--break-system-packages`` and lets pip fall back to a user
  install, which precedes ``dist-packages`` on ``sys.path``.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from typing import ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

# ── Constants ──────────────────────────────────────────

DEFAULT_CLI_BINARY: str = "databricks"
"""Name of the Databricks CLI executable resolved on ``PATH``."""

INSTALL_HINT: str = (
    "The Databricks CLI is required for the 'databricks' sandbox provider. "
    "Install it (https://docs.databricks.com/aws/en/dev-tools/cli/install) — "
    "v0.270.0 or newer, which is where `databricks sandbox` landed — then run "
    "`databricks auth login --host https://<workspace>` and "
    "`databricks sandbox register`."
)

AUTH_HINT: str = (
    "The Databricks CLI is installed but could not list sandboxes. "
    "Authenticate with `databricks auth login --host https://<workspace>` "
    "(or set DATABRICKS_HOST/DATABRICKS_TOKEN), then register this machine "
    "for sandbox SSH with `databricks sandbox register`."
)

# `databricks sandbox create` blocks until the microVM reports Running.
# A cold create takes tens of seconds; the ceiling covers a slow region
# without hanging a managed launch forever.
_CREATE_TIMEOUT_S: float = 600.0

# `databricks sandbox start` documents its own 10-minute ceiling for a
# stopped sandbox to reach Running; match it rather than cutting it short.
_START_TIMEOUT_S: float = 660.0

# Blocking remote commands (`run`, `put`). The bootstrap's longest single
# command is the wheel install, which is seconds on a warm sandbox; the
# ceiling covers a slow git clone of a large repository without letting a
# wedged SSH session hang a managed launch indefinitely.
_REMOTE_COMMAND_TIMEOUT_S: float = 1800.0

# Short control-plane calls (list / status / config / delete). Generous
# enough for a slow workspace round-trip, short enough that a wedged
# control plane surfaces as an error instead of a stalled launch.
_CONTROL_TIMEOUT_S: float = 120.0

# How long `forward_local_port` waits for ssh to bind the local port
# before giving up. Live measurement was ~2 s; the ceiling covers a
# stopped sandbox that `ssh` auto-starts on connection.
_FORWARD_BIND_TIMEOUT_S: float = 120.0
_FORWARD_POLL_INTERVAL_S: float = 0.25

# Grace period for the port-forward child to exit on SIGTERM before it
# is killed outright.
_FORWARD_TERMINATE_TIMEOUT_S: float = 5.0

# The line `ssh -v` prints once a `-L` listener is actually bound, e.g.
# "debug1: Local forwarding listening on 127.0.0.1 port 8022.". Stable
# across OpenSSH releases and the only non-invasive readiness signal
# available — see `DatabricksSandboxLauncher._await_forward` for why
# probing the port instead is wrong in both possible directions. Built
# per-port so a transcript can never match a different forward.
_FORWARD_READY_TEMPLATE: str = "Local forwarding listening on 127.0.0.1 port {port}"

# Substrings that mean the forward will never come up, so the wait can
# fail immediately instead of burning the full readiness timeout.
#
# `ExitOnForwardFailure=yes` would be the tidier mechanism, but the
# Databricks CLI re-quotes the arguments it forwards to ssh and mangles
# `-o Key=Value` in BOTH spellings — verified live on v1.11.0, where
# `-o ExitOnForwardFailure=yes` yields "Bad configuration option:
# 'exitonforwardfailure / invalid quotes" and the single-token
# `-oExitOnForwardFailure=yes` silently loses `-N`. Watching ssh's own
# failure lines gets the same fail-fast without an `-o` flag.
_FORWARD_FAILURE_MARKERS: tuple[str, ...] = (
    "cannot listen to port",
    "bind: ",
    "could not request local forwarding",
)

# Substrings that mark an `ssh -v` line as explaining a failure, used to
# lift the useful lines out of verbose handshake chatter.
_FORWARD_ERROR_MARKERS: tuple[str, ...] = (
    "address already in use",
    "bind:",
    "cannot listen",
    "permission denied",
    "connection refused",
    "connection closed",
    "could not request local forwarding",
    "ssh: ",
)

# Status strings the control plane reports, lower-cased.
_RUNNING_STATUS: str = "running"
_STOPPED_STATUSES: frozenset[str] = frozenset({"stopped", "stopping", "starting", "pending"})

# Substrings that mark a control-plane response as "this sandbox does not
# exist". Matched case-insensitively against combined output so a delete
# of an already-gone sandbox is idempotent.
_NOT_FOUND_MARKERS: tuple[str, ...] = ("not found", "does not exist", "no such sandbox")


def _drain_forward_output(
    process: subprocess.Popen[str],
    transcript: list[str],
    settled: threading.Event,
    state: dict[str, bool],
    port: int,
) -> None:
    """
    Drain an ``ssh -v`` forward child's output for its whole lifetime.

    Runs on a daemon thread. Draining is not optional: ``ssh -v`` keeps
    logging as channels open and close, and an unread pipe eventually
    fills and wedges the tunnel. Readiness and hard failure are both
    recognized here, as a side effect of the drain.

    :param process: The forwarding child, text-mode with stderr merged
        into ``stdout``.
    :param transcript: Appended to, so the caller can quote ``ssh``'s
        own words in an error.
    :param settled: Set once the outcome is known either way, so the
        waiter wakes immediately instead of polling to its deadline.
    :param state: Mutated with ``{"listening": True}`` on success or
        ``{"failed": True}`` when ssh reports it cannot listen.
    :param port: The local port whose readiness line to match, so a
        transcript can never satisfy a different forward.
    """
    stream = process.stdout
    if stream is None:
        return
    ready_marker = _FORWARD_READY_TEMPLATE.format(port=port)
    try:
        for line in stream:
            transcript.append(line)
            if ready_marker in line:
                state["listening"] = True
                settled.set()
            elif any(marker in line.lower() for marker in _FORWARD_FAILURE_MARKERS):
                state["failed"] = True
                settled.set()
    finally:
        stream.close()


def _forward_failure_detail(transcript: list[str]) -> str:
    """
    Summarize an ``ssh -v`` transcript for a forward error message.

    Prefers the lines that explain a failure (``ssh``'s own errors and
    bind complaints) over the verbose handshake chatter, falling back to
    the tail of the transcript when nothing matches.

    :param transcript: Collected output lines.
    :returns: A short, single-line explanation.
    """
    interesting = [
        line.strip()
        for line in transcript
        if any(marker in line.lower() for marker in _FORWARD_ERROR_MARKERS)
    ]
    lines = interesting or [line.strip() for line in transcript[-3:] if line.strip()]
    return " / ".join(lines) or "<no output>"


def _looks_missing(output: str) -> bool:
    """
    Return whether CLI output reports the sandbox as nonexistent.

    :param output: Combined stdout/stderr from a failed control-plane call.
    :returns: ``True`` when the failure is a not-found, e.g. a delete
        racing another cleanup path.
    """
    lowered = output.lower()
    return any(marker in lowered for marker in _NOT_FOUND_MARKERS)


class _DatabricksRemoteProcess(RemoteProcess):
    """
    :class:`RemoteProcess` over a ``databricks sandbox ssh`` child.

    stderr is merged into stdout because the caller consumes one line
    stream; the blocking :meth:`DatabricksSandboxLauncher.run` keeps the
    two streams separate instead.
    """

    def __init__(self, process: subprocess.Popen[str], command: str) -> None:
        """
        Wrap an already-spawned child.

        :param process: The ``Popen`` handle, text-mode, ``stdout=PIPE``
            with stderr merged in.
        :param command: The remote command, for error messages.
        """
        self._process = process
        self._command = command
        self._lines: Iterator[str] | None = None

    @property
    def lines(self) -> Iterator[str]:
        """
        Line iterator over the child's combined output.

        Cached so a caller can consume a few lines, do other work, and
        resume where it left off — the contract
        :class:`~omnigent.onboarding.sandboxes.base.RemoteProcess`
        requires for the OAuth flow.
        """
        if self._lines is None:
            stream = self._process.stdout
            self._lines = iter(()) if stream is None else iter(stream)
        return self._lines

    def wait(self) -> int:
        """
        Block until the remote command exits.

        :returns: The remote command's exit code, which
            ``databricks sandbox ssh`` propagates verbatim.
        """
        return self._process.wait()

    def close(self) -> None:
        """Terminate the child if it is still running, then reap it."""
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=_FORWARD_TERMINATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if self._process.stdout is not None:
            self._process.stdout.close()


class DatabricksSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Databricks Sandbox environments.

    Every primitive shells out to the public ``databricks sandbox`` CLI
    with an argv list (never ``shell=True``): ``create`` / ``delete`` /
    ``start`` / ``status`` / ``config`` for lifecycle, and ``ssh <id> --
    <cmd>`` for all transport. The remote command rides as a single argv
    element, which the CLI hands to ``ssh`` and the sandbox's login shell
    interprets — so ``run("…", "a; b")`` behaves like a shell line, and
    callers quote remote paths themselves as the base contract requires.
    """

    provider: ClassVar[str] = "databricks"
    # `databricks sandbox ssh <id> -- -L …` forwards flags to ssh, so a
    # local→sandbox bridge for the App OAuth callback genuinely works.
    supports_local_port_forward: ClassVar[bool] = True
    # Sandboxes stop on idle and restart with their disk intact.
    can_resume: ClassVar[bool] = True

    @property
    def capabilities(self) -> SandboxCapabilities:
        """
        Feature flags, declared against what was verified on CLI v1.11.0.

        ``streaming_exec`` is ``True`` but cannot honor a PTY request —
        see the module docstring — so the in-sandbox App OAuth flow it
        backs is the least-proven path in this launcher.
        """
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=True,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=True,
            foreground_exec=True,
        )

    def __init__(
        self,
        *,
        cli_path: str | None = None,
        profile: str | None = None,
        idle_timeout: str | None = None,
        no_autostop: bool = True,
    ) -> None:
        """
        Initialize the launcher.

        :param cli_path: Databricks CLI executable to invoke, e.g.
            ``"/opt/databricks/bin/databricks"`` — the server's
            ``sandbox.databricks.cli_path`` config. ``None`` resolves
            :data:`DEFAULT_CLI_BINARY` on ``PATH``.
        :param profile: ``~/.databrickscfg`` profile passed as ``-p`` to
            every call, e.g. ``"sandbox-sp"`` for a service-principal
            M2M profile. ``None`` uses the CLI's own resolution
            (``DATABRICKS_*`` env, then the ``DEFAULT`` profile).
        :param idle_timeout: Per-sandbox idle timeout applied at
            provision, e.g. ``"4h"``. Ignored while *no_autostop* is
            ``True``, which is the default.
        :param no_autostop: Exempt provisioned sandboxes from idle
            auto-stop. Defaults to ``True`` because a managed host must
            survive arbitrary idle gaps between turns; set ``False`` to
            let *idle_timeout* (or the workspace default) apply.
        """
        self._cli_path = cli_path or DEFAULT_CLI_BINARY
        self._profile = profile
        self._idle_timeout = idle_timeout
        self._no_autostop = no_autostop

    # ── CLI plumbing ────────────────────────────────────

    def _argv(self, *args: str) -> list[str]:
        """
        Build the argv for one ``databricks sandbox`` invocation.

        The profile flag is appended AFTER the subcommand's own
        arguments but is never allowed past a ``--`` separator, so
        ``ssh`` remote arguments stay untouched. Callers that need a
        ``--`` pass it themselves via :meth:`_ssh_argv`.

        :param args: Subcommand and its arguments, e.g.
            ``("status", "sb-1", "-o", "json")``.
        :returns: The full argv list, ready for :func:`subprocess.run`.
        """
        argv = [self._cli_path, "sandbox", *args]
        if self._profile is not None:
            argv += ["-p", self._profile]
        return argv

    def _ssh_argv(self, sandbox_id: str, *remote: str) -> list[str]:
        """
        Build the argv for ``databricks sandbox ssh <id> -- <remote…>``.

        :param sandbox_id: Target sandbox id, e.g.
            ``"spanking-pitta-1649"``.
        :param remote: Everything after ``--`` — ssh flags and/or the
            remote command as a single element.
        :returns: The full argv list.
        """
        return [*self._argv("ssh", sandbox_id), "--", *remote]

    def _cli(
        self,
        argv: list[str],
        *,
        timeout: float = _CONTROL_TIMEOUT_S,
        action: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """
        Run one CLI invocation and normalize its failures.

        :param argv: Full argv from :meth:`_argv` / :meth:`_ssh_argv`.
        :param timeout: Seconds to wait before killing the child.
        :param action: Human phrase for error messages, e.g.
            ``"create a Databricks Sandbox"``.
        :param check: When ``True``, raise on a non-zero exit.
        :returns: The completed process, with text-mode output captured.
        :raises click.ClickException: When the CLI is missing, times
            out, or (with *check*) exits non-zero.
        """
        try:
            completed = subprocess.run(  # argv list, no shell
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(INSTALL_HINT) from exc
        except subprocess.TimeoutExpired as exc:
            raise click.ClickException(
                f"Timed out after {timeout:.0f}s trying to {action}."
            ) from exc
        if check and completed.returncode != 0:
            raise click.ClickException(
                f"Could not {action} (exit {completed.returncode}): "
                f"{_combined(completed).strip() or '<no output>'}"
            )
        return completed

    def _cli_json(self, argv: list[str], *, timeout: float, action: str) -> dict[str, object]:
        """
        Run a CLI invocation whose stdout is a JSON object.

        :param argv: Full argv, which must already request JSON output.
        :param timeout: Seconds to wait before killing the child.
        :param action: Human phrase for error messages.
        :returns: The decoded object.
        :raises click.ClickException: On CLI failure or unparseable output.
        """
        completed = self._cli(argv, timeout=timeout, action=action)
        try:
            payload = json.loads(completed.stdout)
        except ValueError as exc:
            raise click.ClickException(
                f"Could not {action}: the Databricks CLI returned "
                f"unparseable JSON ({completed.stdout.strip()[:200]!r})."
            ) from exc
        if not isinstance(payload, dict):
            raise click.ClickException(
                f"Could not {action}: expected a JSON object from the "
                f"Databricks CLI, got {type(payload).__name__}."
            )
        return payload

    # ── Lifecycle ───────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: the ``databricks`` CLI must be on ``PATH`` and
        authenticated against a workspace.

        ``databricks sandbox list`` proves both in one call — it is
        read-only, it fails when no credentials resolve, and it fails
        when the CLI predates the ``sandbox`` command group.

        :raises click.ClickException: When the CLI is missing (with the
            install hint) or unauthenticated (with the login hint).
        """
        if shutil.which(self._cli_path) is None:
            raise click.ClickException(INSTALL_HINT)
        completed = self._cli(
            self._argv("list", "-o", "json"),
            action="list Databricks Sandboxes",
            check=False,
        )
        if completed.returncode != 0:
            raise click.ClickException(
                f"{AUTH_HINT}\n\nThe CLI reported: {_combined(completed).strip() or '<no output>'}"
            )

    def provision(self, name: str) -> str:
        """
        Create a new Databricks Sandbox and return its id.

        ``create`` blocks until the microVM reports Running, so the id
        this returns is immediately usable. The auto-stop policy is
        applied right after creation (see :meth:`keep_alive`) because
        ``create`` itself exposes no policy flag.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Recorded as the sandbox's display name; the returned id is
            the canonical reference.
        :returns: The sandbox id, e.g. ``"spanking-pitta-1649"``.
        :raises click.ClickException: If provisioning fails or the CLI
            returns no id.
        """
        click.echo(f"▸ Creating Databricks Sandbox '{name}'")
        payload = self._cli_json(
            self._argv("create", "--name", name, "--json"),
            timeout=_CREATE_TIMEOUT_S,
            action="create a Databricks Sandbox",
        )
        sandbox_id = payload.get("sandboxId")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise click.ClickException(
                f"Databricks Sandbox creation returned no 'sandboxId' — got {payload!r}."
            )
        click.echo(f"  → created {sandbox_id}")
        self.keep_alive(sandbox_id)
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing sandbox, starting it if stopped.

        Databricks Sandboxes stop on idle and restart with their disk
        intact, so — like Daytona and unlike Modal — attach revives a
        stopped sandbox rather than rejecting it.

        :param sandbox_id: The sandbox to attach to.
        :raises click.ClickException: When the sandbox does not exist or
            cannot be started.
        """
        click.echo(f"▸ Reusing existing Databricks Sandbox '{sandbox_id}'")
        if self._status(sandbox_id) != _RUNNING_STATUS:
            self.resume(sandbox_id)

    def resume(self, sandbox_id: str) -> None:
        """
        Start a stopped sandbox in place, keeping its id and disk.

        Starting an already-running sandbox is a documented no-op, so
        this is safe to call unconditionally.

        :param sandbox_id: The sandbox to start.
        :raises click.ClickException: If the start fails or times out.
        """
        click.echo(f"▸ Starting Databricks Sandbox '{sandbox_id}'")
        self._cli(
            self._argv("start", sandbox_id),
            timeout=_START_TIMEOUT_S,
            action=f"start Databricks Sandbox '{sandbox_id}'",
        )
        click.echo(f"  → running {sandbox_id}")

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether the control plane reports the sandbox as running.

        :param sandbox_id: The sandbox to inspect.
        :returns: ``True`` when Running, ``False`` for the known
            not-running states, ``None`` for anything unrecognized (so
            callers keep their existing liveness behavior).
        """
        status = self._status(sandbox_id)
        if status == _RUNNING_STATUS:
            return True
        if status in _STOPPED_STATUSES:
            return False
        return None

    def _status(self, sandbox_id: str) -> str:
        """
        Return the lower-cased status string for a sandbox.

        :param sandbox_id: The sandbox to inspect.
        :returns: e.g. ``"running"``, ``"stopped"``, or ``""`` when the
            control plane reported no status field.
        :raises click.ClickException: If the status call fails.
        """
        payload = self._cli_json(
            self._argv("status", sandbox_id, "-o", "json"),
            timeout=_CONTROL_TIMEOUT_S,
            action=f"read the status of Databricks Sandbox '{sandbox_id}'",
        )
        status = payload.get("status")
        return status.lower() if isinstance(status, str) else ""

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Apply the configured auto-stop policy so the host survives idle
        gaps between turns.

        Soft-fail per the launcher contract: a rejected setting warns
        rather than aborting the bootstrap — the sandbox is usable, it
        just may reap itself on idle.

        :param sandbox_id: The sandbox to configure.
        """
        if self._no_autostop:
            flags = ["--no-autostop"]
            description = "idle auto-stop disabled"
        elif self._idle_timeout is not None:
            flags = ["--idle-timeout", self._idle_timeout]
            description = f"idle timeout set to {self._idle_timeout}"
        else:
            return
        completed = self._cli(
            self._argv("config", sandbox_id, *flags),
            action=f"configure Databricks Sandbox '{sandbox_id}'",
            check=False,
        )
        if completed.returncode != 0:
            click.echo(
                f"  → warning: could not configure auto-stop on '{sandbox_id}' "
                f"({_combined(completed).strip() or 'no output'}); the sandbox "
                "may stop after its idle timeout.",
                err=True,
            )
            return
        click.echo(f"  → {description}")

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox, releasing its compute and disk.

        ``--auto-approve`` is mandatory: ``delete`` prompts interactively
        otherwise, which would hang a server-side teardown. Idempotent
        from the caller's perspective — a sandbox that is already gone is
        treated as success, since the desired end state holds.

        :param sandbox_id: The sandbox to delete.
        :raises click.ClickException: When the delete fails for any
            reason other than the sandbox not existing.
        """
        completed = self._cli(
            self._argv("delete", sandbox_id, "--auto-approve"),
            action=f"delete Databricks Sandbox '{sandbox_id}'",
            check=False,
        )
        if completed.returncode != 0 and not _looks_missing(_combined(completed)):
            raise click.ClickException(
                f"Could not delete Databricks Sandbox '{sandbox_id}' "
                f"(exit {completed.returncode}): "
                f"{_combined(completed).strip() or '<no output>'}"
            )

    # ── Transport ───────────────────────────────────────

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        The remote transport keeps stdout and stderr separate, so both
        are reported faithfully rather than merged into ``stdout`` the
        way the SDK-backed providers must.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely; quote remote
            paths yourself, per the base contract.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout and stderr.
        :raises click.ClickException: If *check* is ``True`` and the
            command exits non-zero.
        """
        completed = self._cli(
            self._ssh_argv(sandbox_id, command),
            timeout=_REMOTE_COMMAND_TIMEOUT_S,
            action=f"run a command on Databricks Sandbox '{sandbox_id}'",
            check=False,
        )
        for line in completed.stdout.splitlines():
            if line.strip():
                click.echo(line)
        for line in completed.stderr.splitlines():
            if line.strip():
                click.echo(line, err=True)
        if check and completed.returncode != 0:
            raise click.ClickException(
                f"Remote command failed on Databricks Sandbox '{sandbox_id}' "
                f"(exit {completed.returncode}): {command}"
            )
        return RemoteCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox.

        ``databricks sandbox ssh`` passes local stdin through to the
        remote process, so the transfer is a plain ``cat > <path>`` fed
        from the local file — no scp invocation and no dependency on the
        gateway exposing an sftp subsystem. Verified against CLI v1.11.0.

        :param sandbox_id: Target sandbox.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path on the sandbox,
            e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the file cannot be read or the
            transfer fails.
        """
        argv = self._ssh_argv(sandbox_id, f"cat > {shlex.quote(remote_path)}")
        try:
            with local_path.open("rb") as handle:
                completed = subprocess.run(  # argv list, no shell
                    argv,
                    stdin=handle,
                    capture_output=True,
                    text=True,
                    timeout=_REMOTE_COMMAND_TIMEOUT_S,
                    check=False,
                )
        except FileNotFoundError as exc:
            # Either the local file or the CLI itself is missing; the
            # local file is the likelier caller error, so name it.
            raise click.ClickException(
                f"Could not copy '{local_path}' into Databricks Sandbox '{sandbox_id}': {exc}"
            ) from exc
        except OSError as exc:
            raise click.ClickException(
                f"Could not read '{local_path}' to copy into Databricks "
                f"Sandbox '{sandbox_id}': {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise click.ClickException(
                f"Timed out copying '{local_path}' into Databricks Sandbox '{sandbox_id}'."
            ) from exc
        if completed.returncode != 0:
            raise click.ClickException(
                f"File upload to Databricks Sandbox '{sandbox_id}' failed "
                f"(exit {completed.returncode}): "
                f"{_combined(completed).strip() or '<no output>'}"
            )

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its output line by line.

        ``pty`` is accepted for interface compatibility and **ignored**:
        passing ``-tt`` through ``databricks sandbox ssh`` was verified
        not to allocate a remote terminal on CLI v1.11.0. Commands that
        suppress output when not on a TTY will behave accordingly.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: Ignored — see above.
        :returns: A handle streaming the process's combined output.
        :raises click.ClickException: When the CLI is not installed.
        """
        del pty
        argv = self._ssh_argv(sandbox_id, command)
        try:
            process = subprocess.Popen(  # argv list, no shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(INSTALL_HINT) from exc
        return _DatabricksRemoteProcess(process, command)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox with local stdio inherited, blocking
        until it exits.

        No remote PTY is allocated (see the module docstring), so
        ``TERM`` is exported explicitly to give tmux-spawning harnesses a
        usable value rather than an empty one. Ctrl-C propagates to the
        child ``ssh``, which tears the remote command down.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises click.ClickException: When the CLI is not installed.
        """
        argv = self._ssh_argv(sandbox_id, f"TERM=xterm-256color exec {command}")
        try:
            completed = subprocess.run(argv, check=False)  # argv list, no shell
        except FileNotFoundError as exc:
            raise click.ClickException(INSTALL_HINT) from exc
        except KeyboardInterrupt:
            click.echo("\n  → detaching; the remote command was signalled")
            raise
        return completed.returncode

    def forward_local_port(self, sandbox_id: str, port: int) -> AbstractContextManager[None]:
        """
        Forward ``127.0.0.1:<port>`` into the sandbox over SSH.

        ``databricks sandbox ssh <id> -- …`` hands the flags straight to
        ``ssh``. Two details are load-bearing, each fixing a failure seen
        live against CLI v1.11.0:

        - **Both endpoints are pinned to ``127.0.0.1``**, never
          ``localhost``. Unpinned, ``ssh`` binds the local port on BOTH
          ``::1`` and ``127.0.0.1``, and the sandbox side resolves
          ``localhost`` to whichever family it prefers — so a callback
          server bound only to IPv4 loopback can be missed entirely.
        - **Readiness comes from ``ssh -v`` itself**, not from probing the
          port. See :meth:`_await_forward`.

        Only bare flags are passed: the Databricks CLI re-quotes what it
        forwards to ``ssh`` and mangles ``-o Key=Value`` (see
        :data:`_FORWARD_FAILURE_MARKERS`), so the equivalent of
        ``ExitOnForwardFailure`` is done by watching ``ssh``'s own
        "cannot listen to port" output instead of setting the option.

        The context manager does not yield until ``ssh`` reports the
        forward listening, so a caller that immediately opens a browser
        at it cannot race the bind.

        :param sandbox_id: Target sandbox.
        :param port: Local + remote loopback port to bridge, e.g. ``8022``.
        :returns: Context manager holding the forward open.
        :raises click.ClickException: When the CLI is missing, the child
            exits early, or the forward is not established in time.
        """
        return self._forward_local_port(sandbox_id, port)

    @contextmanager
    def _forward_local_port(self, sandbox_id: str, port: int) -> Iterator[None]:
        """
        Implement :meth:`forward_local_port`.

        Split out so the public method can carry the documented
        ``AbstractContextManager`` return type rather than a generator.
        """
        argv = self._ssh_argv(
            sandbox_id,
            "-v",
            "-N",
            "-L",
            f"127.0.0.1:{port}:127.0.0.1:{port}",
        )
        try:
            process = subprocess.Popen(  # argv list, no shell
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise click.ClickException(INSTALL_HINT) from exc
        # ssh keeps logging for the life of the session; an unread pipe
        # would fill and wedge it, so one daemon thread drains stdout
        # for the whole lifetime and the readiness wait just watches
        # what it collects.
        transcript: list[str] = []
        settled = threading.Event()
        state: dict[str, bool] = {}
        reader = threading.Thread(
            target=_drain_forward_output,
            args=(process, transcript, settled, state, port),
            name="databricks-forward-reader",
            daemon=True,
        )
        reader.start()
        try:
            self._await_forward(process, settled, state, transcript, port, sandbox_id)
            yield
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=_FORWARD_TERMINATE_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            reader.join(timeout=_FORWARD_TERMINATE_TIMEOUT_S)

    @staticmethod
    def _await_forward(
        process: subprocess.Popen[str],
        settled: threading.Event,
        state: dict[str, bool],
        transcript: list[str],
        port: int,
        sandbox_id: str,
    ) -> None:
        """
        Block until ``ssh`` reports the local forward listening.

        Readiness is taken from ``ssh -v``'s own
        ``"Local forwarding listening on …"`` line rather than from
        probing the port, because BOTH probe styles are wrong here:

        - A **connect** probe traverses the tunnel and opens a real TCP
          session against whatever listens on the sandbox side. The
          in-sandbox App OAuth callback server accepts exactly one
          connection, so the probe consumes the very request the caller
          is waiting for. Observed live: the forward reported healthy and
          the caller's own connection then hung until timeout.
        - A **bind** probe races ``ssh`` for the port. When the probe
          happens to hold it at the moment ``ssh`` binds, ``ssh`` loses
          and the tunnel is dead while the port still looks taken.
          Observed live at roughly one run in three.

        Watching ``ssh``'s own output has neither failure mode: it
        touches nothing, and it reports the state ``ssh`` actually
        reached.

        :param process: The forwarding child, polled so an early exit is
            reported as itself rather than as a readiness timeout.
        :param settled: Set by the reader thread once the outcome is known.
        :param state: Carries ``listening`` / ``failed`` from the reader.
        :param transcript: Lines collected so far, for error messages.
        :param port: Local loopback port expected to bind.
        :param sandbox_id: Target sandbox, for error messages.
        :raises click.ClickException: When ``ssh`` reports it cannot
            listen, the child exits early, or the forward is not reported
            within :data:`_FORWARD_BIND_TIMEOUT_S`.
        """
        deadline = time.monotonic() + _FORWARD_BIND_TIMEOUT_S
        while time.monotonic() < deadline:
            if settled.wait(timeout=_FORWARD_POLL_INTERVAL_S):
                if state.get("listening"):
                    return
                raise click.ClickException(
                    f"Port forward to Databricks Sandbox '{sandbox_id}' could not "
                    f"listen on 127.0.0.1:{port}: {_forward_failure_detail(transcript)}"
                )
            if process.poll() is not None:
                raise click.ClickException(
                    f"Port forward to Databricks Sandbox '{sandbox_id}' exited "
                    f"before listening on 127.0.0.1:{port} "
                    f"(exit {process.returncode}): "
                    f"{_forward_failure_detail(transcript)}"
                )
        raise click.ClickException(
            f"Port forward to Databricks Sandbox '{sandbox_id}' did not report "
            f"listening on 127.0.0.1:{port} within "
            f"{_FORWARD_BIND_TIMEOUT_S:.0f}s: {_forward_failure_detail(transcript)}"
        )

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that installs locally-built wheels over the
        sandbox's preinstalled omnigent.

        Diverges from
        :func:`~omnigent.onboarding.sandboxes.base.host_image_wheel_install_command`
        because the Databricks Sandbox image is not the Omnigent host
        image: its interpreter is PEP 668 externally-managed and its
        ``dist-packages`` are root-owned, so ``pip`` needs
        ``--break-system-packages`` and falls back to a user install.
        User site precedes ``dist-packages`` on ``sys.path``, so the
        overlay wins at import time.

        ``--force-reinstall`` is required for the same reason as the host
        image: the baked omnigent shares a version with the local build,
        so pip would otherwise consider it satisfied and silently skip.
        ``--no-deps`` keeps the overlay to the local wheels alone.

        :param remote_tgz_path: Sandbox path of the shipped tarball, e.g.
            ``"/tmp/oa-wheels.tgz"``.
        :returns: Shell command string for :meth:`run`.
        """
        quoted = shlex.quote(remote_tgz_path)
        return (
            "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
            f"tar xzf {quoted} -C oa-wheels --warning=no-unknown-keyword && "
            "pip install --quiet --break-system-packages --force-reinstall "
            "--no-deps --no-warn-script-location oa-wheels/*.whl"
        )


def _combined(completed: subprocess.CompletedProcess[str]) -> str:
    """
    Join a completed process's captured streams for error reporting.

    :param completed: The finished process.
    :returns: stdout and stderr concatenated, in that order.
    """
    return f"{completed.stdout or ''}{completed.stderr or ''}"
