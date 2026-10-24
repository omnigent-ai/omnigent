"""
boxd sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `boxd <https://boxd.sh>`_ machines on top of the official ``boxd``
Python SDK. A boxd machine is a hosted KVM microVM: a full Ubuntu computer
with its own persistent disk that boots in about a second, sleeps in place
and wakes in milliseconds. boxd is not Boxlite
(:mod:`omnigent.onboarding.sandboxes.boxlite`), the embeddable micro-VM
runtime that runs boxes on the server host itself or a self-hosted pool:
boxd is a managed cloud service reached over its public API with an API key.

The SDK is an optional dependency (``pip install 'omnigent[boxd]'``)
imported lazily, so the provider can be listed and the module probed
without it.

Supports both server-managed hosts (``host_type="managed"`` sessions) and
the CLI bootstrap flow. The one unsupported primitive is
``forward_local_port``: boxd publishes machine ports outward
(``https://<machine>.boxd.sh``) but has no local-to-machine path for the
in-sandbox App OAuth callback, so managed hosts authenticate with a
server-minted launch token instead.

Notes that shape this launcher:

- **No registry images: a bootstrap, or a snapshot.** Machines boot boxd's
  Ubuntu image, which already carries git, tmux, Node, uv, Docker, Claude
  Code and Codex. :meth:`BoxdSandboxLauncher.provision` installs the rest of
  what the official host image bakes (``omnigent``, ``bubblewrap``, the
  ``GIT_TOKEN`` credential helper). A boxd snapshot with those in place
  (``sandbox.boxd.snapshot``) skips that step and restores in milliseconds;
  ``deploy/boxd/README.md`` shows how to save one.
- **Persistent, no lifetime cap.** A machine lives until it is deleted, so
  the launch-token bound is policy (7 days, as for Daytona). A machine whose
  host has gone quiet hibernates after boxd's network-idle timeout, keeping
  disk and memory, and :meth:`BoxdSandboxLauncher.resume` brings a
  hibernated, standby or stopped machine back under the same id.
- **Always isolated.** Machines are created with ``isolated=True``: no
  in-machine boxd CLI, metadata endpoint, integrations or agent logins, so
  code an agent runs cannot act on the operator's boxd account.
- **Env rides every command.** Environment set at machine creation does not
  reach exec sessions, so the passthrough env is sent with each command (the
  backgrounded host included) rather than written to the machine's disk.
- **API-key auth.** The SDK reads ``BOXD_API_KEY`` (or ``BOXD_TOKEN``) from
  the CLI/server process environment, like the other providers' keys.
  ``BOXD_ORG`` (or ``sandbox.boxd.org``) picks the organization.
"""

from __future__ import annotations

import codecs
import contextlib
import logging
import os
import re
import secrets
import shlex
import time
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    RemoteProcess,
    SandboxGoneError,
    SandboxLauncher,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
    stop_preserved_host_daemon_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from boxd import Boxd, ExecStream, Machine

    from omnigent.onboarding.sandboxes.types import RepoWorkspace


_logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────

API_KEY_ENV_VARS: tuple[str, ...] = ("BOXD_API_KEY", "BOXD_TOKEN")
"""Credential environment variables the boxd SDK reads, in the order it
prefers them after explicit arguments. Mint a key with ``boxd auth keys
create`` or in the console at https://app.boxd.sh."""

ORG_ENV_VAR: str = "BOXD_ORG"
"""Organization machines are created in when ``sandbox.boxd.org`` is unset.
Unset means the API key's own organization."""

SNAPSHOT_ENV_VAR: str = "OMNIGENT_BOXD_SNAPSHOT"
"""Environment variable naming the boxd snapshot to create machines from
when ``sandbox.boxd.snapshot`` is unset (see ``deploy/boxd/README.md``)."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_BOXD_SANDBOX_ENV"
"""Comma-separated server-process environment variable NAMES whose values
are sent with every command this launcher runs in a machine, typically the
harness LLM credentials and ``GIT_TOKEN``. The server's managed-host config
(``sandbox.boxd.env``) takes precedence when set."""

MANAGED_TOKEN_TTL_S: int = 7 * 24 * 3600
"""Launch-token lifetime. Machines have no lifetime cap, so the bound is
policy: the same 7 days as Daytona and Islo."""

SUPPORTED_VCPUS: tuple[int, ...] = (1, 2, 4, 8)
"""boxd's machine sizes, named by vCPU count; memory follows (4, 8, 16 and
32 GiB). Omitting the size takes the organization's default."""

# Distinguishes the sandbox host from the operator's own boxd machines in
# `boxd machine list`; the random suffix keeps relaunches from colliding.
_MACHINE_NAME_PREFIX: str = "omnigent"
_MACHINE_NAME_MAX_LEN: int = 63

_READY_TIMEOUT_S: float = 120.0
# A machine started from `stopped` can report ready a moment before its agent
# accepts exec sessions, so readiness ends with a probe exec, retried briefly.
_EXEC_READY_TIMEOUT_S: float = 60.0
_EXEC_READY_POLL_S: float = 0.5

# PTY geometry for foreground/streaming execs; an unset size gives the
# remote terminal zero columns, which tmux-spawning harnesses reject.
_PTY_COLS: int = 200
_PTY_ROWS: int = 50

# Same credential helper the official host image installs system-wide: it
# answers `git credential get` from GIT_TOKEN / GIT_USERNAME in the env.
_GIT_CREDENTIAL_HELPER: str = (
    '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; '
    'printf "username=%s\\npassword=%s\\n" "${GIT_USERNAME:-x-access-token}" '
    '"$GIT_TOKEN"; }; f'
)

# apt is chatty on stderr even when quiet; its log is shown only on failure.
_APT_LOG: str = "/tmp/omnigent-bootstrap-apt.log"

_MACHINE_GONE_STATUSES: frozenset[str] = frozenset({"destroyed"})
_MACHINE_ASLEEP_STATUSES: frozenset[str] = frozenset({"suspended", "hibernated"})
_MACHINE_SETTLING_STATUSES: frozenset[str] = frozenset({"pending", "starting", "migrating"})


def _ensure_sdk() -> None:
    """
    Verify the boxd SDK is importable, with an install hint when not.

    :raises click.ClickException: When the ``boxd`` package is not installed.
    """
    try:
        import boxd  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "The boxd SDK is required for the 'boxd' sandbox provider. "
            "Install it with `pip install 'omnigent[boxd]'`, then set "
            "BOXD_API_KEY (create a key at https://app.boxd.sh)."
        ) from exc


def default_omnigent_spec() -> str:
    """
    The package spec the in-machine host is installed from by default.

    A release server pins its own version so host and server match; a
    development build takes the latest published release, the same posture
    as the ``:latest`` host image tracking main.

    :returns: A pip requirement, e.g. ``"omnigent==0.19.1"`` or ``"omnigent"``.
    """
    from packaging.version import Version

    from omnigent.version import VERSION

    if Version(VERSION).is_prerelease:
        return "omnigent"
    return f"omnigent=={VERSION}"


def host_bootstrap_command(omnigent_spec: str) -> str:
    """
    Remote script that installs what the official host image bakes and
    boxd's base image lacks.

    Idempotent: each step is skipped when its binary is already present, so
    a machine restored from a prepared snapshot pays only the checks. The
    omnigent install and the apt install run in parallel.

    :param omnigent_spec: pip requirement for ``uv tool install``, e.g.
        ``"omnigent==0.19.1"``.
    :returns: A POSIX shell script for :meth:`BoxdSandboxLauncher.run`.
    """
    return "\n".join(
        [
            "set -e",
            "install_host() {",
            "  command -v omnigent >/dev/null 2>&1 && return 0",
            f"  uv tool install --quiet --python 3.12 {shlex.quote(omnigent_spec)}",
            "}",
            "install_bwrap() {",
            "  command -v bwrap >/dev/null 2>&1 && return 0",
            "  { sudo apt-get update -qq &&",
            "    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq bubblewrap",
            f"  }} >{_APT_LOG} 2>&1 || {{ cat {_APT_LOG} >&2; return 1; }}",
            "}",
            "install_host & host_pid=$!",
            "install_bwrap & bwrap_pid=$!",
            'wait "$host_pid"',
            'wait "$bwrap_pid"',
            "git config --global --replace-all credential.helper "
            f"{shlex.quote(_GIT_CREDENTIAL_HELPER)}",
        ]
    )


def boxd_wheel_install_command(remote_tgz_path: str) -> str:
    """
    Remote command that installs the shipped local wheels as the host.

    boxd machines have no baked omnigent environment to overlay, so the
    wheels go into a fresh ``uv tool`` environment with their dependencies
    resolved from PyPI. ``--find-links`` lets the omnigent wheel resolve the
    client and UI SDK wheels built alongside it at the same dev version.

    :param remote_tgz_path: Machine path of the shipped tarball, e.g.
        ``"/tmp/oa-wheels.tgz"``.
    :returns: Shell command string for :meth:`BoxdSandboxLauncher.run`.
    """
    return (
        "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels && "
        f"tar xzf {shlex.quote(remote_tgz_path)} -C oa-wheels --warning=no-unknown-keyword && "
        "uv tool install --quiet --force --python 3.12 --find-links oa-wheels "
        "oa-wheels/omnigent-*.whl"
    )


def machine_name_for(label: str) -> str:
    """
    Derive a unique, valid boxd machine name from an Omnigent sandbox label.

    boxd names are lowercase letters, digits and hyphens, at most 63
    characters, unique per organization, and double as the machine's public
    hostname, so the label is slugged and given a random suffix.

    :param label: Human-readable sandbox label, e.g. ``"managed-a1b2c3d4"``.
    :returns: A machine name, e.g. ``"omnigent-managed-a1b2c3d4-3f9a1c"``.
    """
    suffix = secrets.token_hex(3)
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
    if slug.startswith(f"{_MACHINE_NAME_PREFIX}-"):
        slug = slug[len(_MACHINE_NAME_PREFIX) + 1 :]
    budget = _MACHINE_NAME_MAX_LEN - len(_MACHINE_NAME_PREFIX) - len(suffix) - 2
    slug = slug[:budget].strip("-")
    parts = [_MACHINE_NAME_PREFIX, slug, suffix] if slug else [_MACHINE_NAME_PREFIX, suffix]
    return "-".join(parts)


def _as_text(output: bytes | str) -> str:
    """
    Normalize captured exec output to text.

    :param output: ``ExecResult.stdout`` or ``.stderr``; ``str`` under the
        SDK's default UTF-8 encoding.
    :returns: The output as ``str``.
    """
    return output if isinstance(output, str) else output.decode("utf-8", errors="replace")


def _echo_lines(stream: str, *, err: bool = False) -> None:
    """
    Echo a captured remote output stream line by line, dropping blank lines.

    :param stream: Captured stdout or stderr from a remote command.
    :param err: When ``True``, write to stderr.
    """
    for line in stream.splitlines():
        if line.strip():
            click.echo(line, err=err)


class _BoxdRemoteProcess(RemoteProcess):
    """
    :class:`RemoteProcess` over a boxd ``ExecStream``.

    Iterating the SDK stream yields merged output chunks, so this only has to
    decode them incrementally and cut them into lines.
    """

    def __init__(self, stream: ExecStream) -> None:
        """
        Wrap an open exec stream.

        :param stream: Stream returned by ``machines.stream_exec``.
        """
        self._stream = stream
        self._error: Exception | None = None
        # Materialized once so repeated `lines` reads resume the same stream.
        self._line_iter: Iterator[str] = self._iter_lines()

    @property
    def lines(self) -> Iterator[str]:
        """
        Iterator over the command's combined output lines (same object on
        every access).

        :returns: Line iterator over the stream.
        """
        return self._line_iter

    def wait(self) -> int:
        """
        Drain the stream and return the command's exit code.

        :returns: The exit code, or ``1`` when the stream ended without one.
        :raises click.ClickException: When the stream failed at the transport
            level rather than the command exiting non-zero.
        """
        for _ in self._line_iter:
            pass
        if self._error is not None:
            raise click.ClickException(str(self._error)) from self._error
        exit_code = self._stream.exit_code
        return exit_code if exit_code is not None else 1

    def close(self) -> None:
        """Close the stream. Idempotent and best-effort: never raises."""
        with contextlib.suppress(Exception):
            self._stream.close()

    def _iter_lines(self) -> Iterator[str]:
        """Yield newline-terminated lines decoded from the stream's chunks."""
        from boxd import BoxdError

        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        try:
            for chunk in self._stream:
                pending += decoder.decode(chunk)
                *complete, pending = pending.split("\n")
                for line in complete:
                    yield f"{line}\n"
        except BoxdError as exc:
            self._error = exc
        pending += decoder.decode(b"", final=True)
        if pending:
            yield f"{pending}\n"


class BoxdSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for boxd machines, over the ``boxd`` SDK.

    All transport rides the SDK and addresses machines by id:
    ``machines.create`` / ``delete`` for lifecycle, ``machines.exec`` for
    commands (``bash -lc`` so login PATH applies), ``machines.files.upload``
    for file shipping, ``machines.stream_exec`` for streaming and the
    foreground attach, and ``wake`` / ``start`` for resume.
    """

    provider: ClassVar[str] = "boxd"
    # boxd publishes machine ports outward only; there is no local→machine
    # path for the App OAuth callback port.
    supports_local_port_forward: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    @property
    def capabilities(self) -> SandboxCapabilities:
        # start_host only adds steps around the base implementation, which
        # still clones through materialize_workspace and honors clone options.
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=True,
            foreground_exec=True,
            git_clone_options=True,
        )

    def __init__(
        self,
        *,
        snapshot: str | None = None,
        env: Sequence[str] | None = None,
        org: str | None = None,
        vcpu: int | None = None,
        omnigent_spec: str | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param snapshot: Optional boxd snapshot to create machines from, e.g.
            ``"omnigent-host"`` (the server's ``sandbox.boxd.snapshot``).
            ``None`` resolves :data:`SNAPSHOT_ENV_VAR` and falls back to
            boxd's base image plus :func:`host_bootstrap_command`.
        :param env: Optional names of server-process environment variables
            sent with every command, e.g. ``["ANTHROPIC_API_KEY", "GIT_TOKEN"]``
            (``sandbox.boxd.env``). ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` and falls back to none.
        :param org: Optional boxd organization slug (``sandbox.boxd.org``).
            ``None`` resolves :data:`ORG_ENV_VAR`, then the key's own org.
        :param vcpu: Optional machine size by vCPU count, one of
            :data:`SUPPORTED_VCPUS`. ``None`` takes the org's default size.
        :param omnigent_spec: Optional pip requirement the host is installed
            from on a fresh machine, e.g. ``"omnigent==0.19.1"``. ``None``
            uses :func:`default_omnigent_spec`.
        :raises ValueError: When *vcpu* is not a boxd size, or is combined
            with *snapshot* (a machine keeps its snapshot's size).
        """
        if vcpu is not None and vcpu not in SUPPORTED_VCPUS:
            sizes = ", ".join(str(size) for size in SUPPORTED_VCPUS)
            raise ValueError(f"vcpu must be one of {sizes} (got {vcpu!r})")
        if vcpu is not None and snapshot is not None:
            raise ValueError(
                "vcpu cannot be combined with snapshot: a machine keeps its snapshot's size"
            )
        if omnigent_spec is not None and not omnigent_spec.strip():
            raise ValueError("omnigent_spec must be a non-empty pip requirement")
        self.snapshot = snapshot
        self.env = tuple(env) if env is not None else None
        self.org = org
        self.vcpu = vcpu
        self.omnigent_spec = omnigent_spec
        self._client: Boxd | None = None

    def _boxd(self) -> Boxd:
        """
        Return the (lazily created) boxd API client.

        :returns: The shared client, authenticated from the environment.
        :raises click.ClickException: When the SDK is not installed.
        """
        if self._client is None:
            _ensure_sdk()
            from boxd import Boxd

            self._client = Boxd()
        return self._client

    def _resolved_org(self) -> str | None:
        """The organization to create machines in, or ``None`` for the key's own."""
        return self.org or os.environ.get(ORG_ENV_VAR) or None

    def _resolved_snapshot(self) -> str | None:
        """The snapshot to create machines from, or ``None`` for the base image."""
        return self.snapshot or os.environ.get(SNAPSHOT_ENV_VAR) or None

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env sent with every command.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies.
        Values come from the server's own environment, and a configured name
        that is unset there fails loud rather than surfacing later as an
        opaque harness auth failure. ``IS_SANDBOX=1`` marks the machine as a
        managed sandbox, as the official host image does.

        :returns: Name → value mapping for ``exec(env=…)``.
        :raises click.ClickException: When a configured name is not set in
            the server process environment.
        """
        if self.env is not None:
            names: Sequence[str] = self.env
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved = {"IS_SANDBOX": "1"}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.boxd.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """
        Local preflight: the boxd SDK must be installed and a credential set.

        :raises click.ClickException: When the SDK is missing or neither
            ``BOXD_API_KEY`` nor ``BOXD_TOKEN`` is set.
        """
        _ensure_sdk()
        if not any(os.environ.get(name) for name in API_KEY_ENV_VARS):
            raise click.ClickException(
                "No boxd credentials found. Create an API key with "
                "`boxd auth keys create omnigent` (or at https://app.boxd.sh) "
                "and set BOXD_API_KEY."
            )

    def provision(self, name: str) -> str:
        """
        Create a boxd machine, wait until it runs, and bootstrap the host.

        Auto-suspend is disabled at creation; the managed-session machinery
        owns the machine's lifetime. A machine that fails to become ready or
        to bootstrap is deleted before the error propagates, since no caller
        holds its id yet.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``,
            from which the machine name derives (:func:`machine_name_for`).
        :returns: The boxd machine id (a UUID string).
        :raises click.ClickException: If creation, readiness or the
            bootstrap fails.
        """
        client = self._boxd()
        # _boxd owns the missing-SDK preflight, so this import can only succeed.
        from boxd import AuthenticationError, BoxdError

        machine_name = machine_name_for(name)
        snapshot = self._resolved_snapshot()
        org = self._resolved_org()
        source = f"snapshot '{snapshot}'" if snapshot else "the boxd base image"
        click.echo(f"▸ Creating boxd machine '{machine_name}' from {source}")
        try:
            if snapshot is not None:
                # A snapshot restore keeps the snapshot's size and takes no vcpu.
                machine = client.machines.create(
                    machine_name,
                    from_snapshot=snapshot,
                    org=org,
                    isolated=True,
                    auto_suspend_timeout=0,
                )
            else:
                machine = client.machines.create(
                    machine_name,
                    org=org,
                    isolated=True,
                    auto_suspend_timeout=0,
                    vcpu=self.vcpu,
                )
        except AuthenticationError as exc:
            raise click.ClickException(
                f"boxd authentication failed — check BOXD_API_KEY. ({exc})"
            ) from exc
        except BoxdError as exc:
            raise click.ClickException(f"boxd machine creation failed: {exc}") from exc
        machine_id = str(machine.id)
        try:
            client.machines.wait_until_ready(machine_id, timeout=_READY_TIMEOUT_S)
            self._wait_for_exec(machine_id)
            click.echo("  → bootstrapping the omnigent host")
            self.run(machine_id, host_bootstrap_command(self._omnigent_spec()))
        except (BoxdError, TimeoutError) as exc:
            self._delete_quietly(machine_id)
            raise click.ClickException(
                f"boxd machine '{machine_name}' did not become ready: {exc}"
            ) from exc
        except BaseException:
            self._delete_quietly(machine_id)
            raise
        click.echo(f"  → created {machine_id} (https://{machine_name}.boxd.sh)")
        return machine_id

    def _omnigent_spec(self) -> str:
        """The pip requirement a fresh machine installs the host from."""
        return self.omnigent_spec or default_omnigent_spec()

    def _delete_quietly(self, machine_id: str) -> None:
        """
        Best-effort delete for a machine no caller knows about yet.

        :param machine_id: The machine to delete.
        """
        try:
            self._boxd().machines.delete(machine_id)
        except Exception as exc:
            _logger.warning(
                "could not delete boxd machine %s after a failed launch: %s", machine_id, exc
            )

    def _wake(self, machine: Machine) -> None:
        """
        Bring a persisted machine to ``running`` without replacing its disk.

        :param machine: The machine record from ``machines.get``.
        :raises SandboxGoneError: When the machine has been destroyed.
        :raises click.ClickException: When the machine cannot be resumed in
            place (e.g. ``failed``).
        """
        client = self._boxd()
        status = str(machine.status)
        if status == "running":
            return
        if status in _MACHINE_GONE_STATUSES:
            raise SandboxGoneError(f"boxd machine '{machine.id}' no longer exists")
        if status in _MACHINE_ASLEEP_STATUSES:
            click.echo(f"  → waking machine (state: {status})")
            client.machines.wake(machine.id)
        elif status == "stopped":
            click.echo("  → starting machine (state: stopped)")
            client.machines.start(machine.id)
        elif status not in _MACHINE_SETTLING_STATUSES:
            raise click.ClickException(
                f"boxd machine '{machine.id}' is in state {status!r}; "
                "it cannot be resumed in place."
            )
        client.machines.wait_until_ready(machine.id, timeout=_READY_TIMEOUT_S)
        self._wait_for_exec(machine.id)

    def _wait_for_exec(self, machine_id: str) -> None:
        """
        Probe the machine with a no-op exec until it accepts one.

        :param machine_id: The machine to probe.
        :raises boxd.BoxdError: When exec still fails after
            :data:`_EXEC_READY_TIMEOUT_S`, or fails for a reason waiting cannot
            fix (missing machine, rejected credentials).
        """
        client = self._boxd()
        from boxd import AuthenticationError, BoxdError, NotFoundError, PermissionDeniedError

        deadline = time.monotonic() + _EXEC_READY_TIMEOUT_S
        while True:
            try:
                client.machines.exec(machine_id, ["true"])
                return
            except (AuthenticationError, NotFoundError, PermissionDeniedError):
                raise
            except BoxdError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(_EXEC_READY_POLL_S)

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing machine, waking or starting it.

        :param sandbox_id: The machine to attach to (id or name).
        :raises click.ClickException: When the machine does not exist or
            cannot be brought back.
        """
        client = self._boxd()
        from boxd import BoxdError, NotFoundError

        click.echo(f"▸ Reusing existing boxd machine '{sandbox_id}'")
        try:
            self._wake(client.machines.get(sandbox_id))
        except NotFoundError as exc:
            raise click.ClickException(
                f"boxd machine '{sandbox_id}' not found — it may have been deleted."
            ) from exc
        except (BoxdError, TimeoutError) as exc:
            raise click.ClickException(
                f"Could not attach to boxd machine '{sandbox_id}': {exc}"
            ) from exc

    def resume(self, sandbox_id: str) -> None:
        """
        Wake a hibernated or standby machine, or start a stopped one, in place.

        :param sandbox_id: The machine to resume.
        :raises SandboxGoneError: When the machine no longer exists.
        :raises click.ClickException: When the resume fails.
        """
        client = self._boxd()
        from boxd import BoxdError, NotFoundError

        click.echo(f"▸ Resuming boxd machine '{sandbox_id}'")
        try:
            self._wake(client.machines.get(sandbox_id))
        except NotFoundError as exc:
            raise SandboxGoneError(f"boxd machine '{sandbox_id}' no longer exists") from exc
        except (BoxdError, TimeoutError) as exc:
            raise click.ClickException(
                f"Could not resume boxd machine '{sandbox_id}': {exc}"
            ) from exc

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether boxd reports the machine as running.

        A sleeping machine reports ``False``: its host tunnel is down until
        something wakes it. Transport errors report ``None`` (unknown), so a
        provider blip never fails the caller's message path.

        :param sandbox_id: The machine to inspect.
        :returns: ``True`` when running, ``False`` when asleep, stopped or
            gone, ``None`` when the status could not be read.
        """
        client = self._boxd()
        from boxd import BoxdError, NotFoundError

        try:
            status = str(client.machines.get(sandbox_id).status)
        except NotFoundError:
            return False
        except BoxdError as exc:
            _logger.warning("could not read boxd machine %s status: %s", sandbox_id, exc)
            return None
        if status == "running":
            return True
        if status in _MACHINE_SETTLING_STATUSES:
            return None
        return False

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Keep auto-suspend disabled so the host survives idle gaps.

        Machines have no lifetime cap, and :meth:`provision` already creates
        them with auto-suspend off; this re-asserts it for attached machines
        created elsewhere. Soft-fail per the launcher contract.

        :param sandbox_id: The machine to configure.
        """
        client = self._boxd()
        from boxd import BoxdError

        try:
            client.machines.set_auto_suspend_timeout(sandbox_id, 0)
        except BoxdError as exc:
            click.echo(
                f"  → warning: could not disable auto-suspend on boxd machine "
                f"'{sandbox_id}' ({exc}); it may go to standby when idle.",
                err=True,
            )

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
        """
        Stop any host daemon a wake preserved, then start one with a fresh token.

        A machine woken from standby or hibernation resumes with its old
        ``omnigent host`` still running on a revoked token.
        """
        self.run(sandbox_id, stop_preserved_host_daemon_command(), check=False)
        return super().start_host(
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=server_url,
            repos=repos,
            host_config=host_config,
            on_stage=on_stage,
        )

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the machine and capture its output.

        ``bash -lc`` wraps the command so login PATH applies. No timeout is
        set, so installs and clones run to completion.

        :param sandbox_id: Target machine.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the command could not be executed,
            or *check* is ``True`` and it exited non-zero.
        """
        client = self._boxd()
        from boxd import BoxdError

        env = self._resolve_sandbox_env()
        try:
            result = client.machines.exec(sandbox_id, ["bash", "-lc", command], env=env)
        except BoxdError as exc:
            # A stopped or deleted machine must surface its provider reason
            # through the launcher contract, not as a raw SDK exception.
            raise click.ClickException(
                f"Remote command failed to execute on boxd machine '{sandbox_id}': {exc}"
            ) from exc
        stdout, stderr = _as_text(result.stdout), _as_text(result.stderr)
        _echo_lines(stdout)
        _echo_lines(stderr, err=True)
        if check and result.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on boxd machine '{sandbox_id}' "
                f"(exit {result.exit_code}): {command}"
            )
        return RemoteCommandResult(returncode=result.exit_code, stdout=stdout, stderr=stderr)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the machine (the SDK chunks large uploads).

        :param sandbox_id: Target machine.
        :param local_path: Local file to read.
        :param remote_path: Absolute destination path, e.g. ``"/tmp/oa-wheels.tgz"``.
        :raises click.ClickException: If the transfer fails.
        """
        client = self._boxd()
        from boxd import BoxdError

        try:
            client.machines.files.upload(sandbox_id, remote_path, local_path.read_bytes())
        except BoxdError as exc:
            raise click.ClickException(
                f"File upload to boxd machine '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the machine and stream its combined output.

        :param sandbox_id: Target machine.
        :param command: Shell command to execute remotely.
        :param pty: When ``True``, allocate a remote PTY.
        :returns: Handle over the streaming process.
        :raises click.ClickException: When the command cannot be started.
        """
        client = self._boxd()
        from boxd import BoxdError

        env = self._resolve_sandbox_env()
        try:
            stream = client.machines.stream_exec(
                sandbox_id,
                command=["bash", "-lc", command],
                env=env,
                tty=pty,
                cols=_PTY_COLS if pty else 0,
                rows=_PTY_ROWS if pty else 0,
            )
        except BoxdError as exc:
            raise click.ClickException(
                f"Could not start a streaming command on boxd machine '{sandbox_id}': {exc}"
            ) from exc
        return _BoxdRemoteProcess(stream)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the machine over a PTY, echoing its output until it
        exits; Ctrl-C kills the remote process and re-raises.

        The remote command records its pid first (``echo $$`` then ``exec``
        keeps the pid across the swap), so the interrupt path can signal it
        with a second exec whether or not closing the stream reaches it.
        ``TERM`` is forced to ``xterm-256color`` because native harnesses
        spawn tmux, which refuses to start under a dumb terminal.

        :param sandbox_id: Target machine.
        :param command: Shell command to execute remotely, e.g.
            ``"omnigent host --server https://…"``.
        :returns: The remote command's exit code.
        :raises KeyboardInterrupt: Re-raised after killing the remote process.
        """
        run_dir, pidfile = foreground_pidfile()
        remote = f"{foreground_record_prefix(pidfile)}TERM=xterm-256color exec {command}"
        process = self.stream_exec(sandbox_id, remote, pty=True)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            returncode = process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detaching; stopping the remote process")
            process.close()
            self.run(sandbox_id, foreground_kill_command(pidfile), check=False)
            raise
        self.run(sandbox_id, f"rm -rf {shlex.quote(run_dir)}", check=False)
        return returncode

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """
        Remote command that installs the shipped wheels as the host — see
        :func:`boxd_wheel_install_command`.

        :param remote_tgz_path: Machine path of the shipped tarball.
        :returns: Shell command string for :meth:`run`.
        """
        return boxd_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a machine, releasing its compute and disk.

        Idempotent: a machine that no longer exists is treated as success.

        :param sandbox_id: The machine to delete.
        :raises click.ClickException: When boxd rejects the delete for any
            other reason (callers in the managed teardown are best-effort).
        """
        client = self._boxd()
        from boxd import BoxdError, NotFoundError

        try:
            client.machines.delete(sandbox_id)
        except NotFoundError:
            pass  # already gone — success
        except BoxdError as exc:
            raise click.ClickException(
                f"Could not delete boxd machine '{sandbox_id}': {exc}"
            ) from exc
