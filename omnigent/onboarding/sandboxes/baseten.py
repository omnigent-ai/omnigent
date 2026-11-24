"""Baseten Sandboxes launcher.

The launcher uses the optional :mod:`baseten` Python SDK (``baseten.sandbox``).
Baseten sandboxes run commands through their built-in sandbox API, so the
provider follows Omnigent's exec-model host contract.

Baseten builds sandbox images from any registry image and adds its sandbox API
to them. By default the launcher builds the public Omnigent host image
(:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`, at the tag of
the installed Omnigent release when there is one) into the team once and boots
every sandbox from that build. Deployments can name a fixed Baseten image
instead.
"""

from __future__ import annotations

import contextlib
import math
import os
import queue
import re
import shlex
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from datetime import timedelta
from importlib import metadata
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    ExecModelHostLauncher,
    RemoteCommandResult,
    RemoteProcess,
    host_image_wheel_install_command,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from pathlib import Path

    from baseten.sandbox import Sandbox, SandboxClient, SandboxStream
    from baseten.sandbox import SandboxProcessExecEvent as _ExecEvent


API_KEY_ENV_VAR: str = "BASETEN_API_KEY"
"""Baseten API key that may create sandboxes (a personal or team creator key)."""

TEAM_ID_ENV_VAR: str = "BASETEN_TEAM_ID"
"""Optional team that owns the sandboxes, when the key reaches several teams."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_BASETEN_HOST_IMAGE"
"""Optional Baseten image reference (``<name>:<tag>``) to boot instead of the default."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_BASETEN_SANDBOX_ENV"
"""Comma-separated server environment variable names to inject."""

REGION_ENV_VAR: str = "OMNIGENT_BASETEN_REGION"
"""Optional Baseten region override; unset picks the closest region."""

DEFAULT_IMAGE_PREFIX: str = "omnigent-host-"
"""Name prefix of the Baseten images the launcher builds from the public host image."""

_HOST_IMAGE_REPOSITORY = DEFAULT_HOST_IMAGE.rsplit(":", 1)[0]
# Releases the host image is published for: final, rc, and dated nightly builds.
_RELEASE_VERSION_RE = re.compile(r"\d+\.\d+\.\d+(?:rc\d+|\.dev\d{8})?")
_DEFAULT_MEMORY_MIB = 4096
_COMMAND_TIMEOUT_S = 15 * 60
_COMMAND_KILL_GRACE_S = 30
# How often a caller waiting on a stream wakes up to handle Ctrl-C.
_QUEUE_POLL_S = 1.0
# Put on a stream's queue when its local deadline passes.
_DEADLINE = object()
_DELETE_TIMEOUT_S = 2 * 60
_DEPLOY_TIMEOUT_S = 5 * 60
_IMAGE_BUILD_TIMEOUT_S = 30 * 60
_READINESS_TIMEOUT_S = 90.0
_READINESS_INITIAL_BACKOFF_S = 0.5
_READINESS_MAX_BACKOFF_S = 10.0
_POLL_INTERVAL_S = 1.0
_START_ATTEMPTS = 3
_MAX_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024
_MAX_PARTIAL_LINE_CHARS = 64 * 1024
_DEFAULT_TTL = "24h"
# Baseten rejects a max-age expiration policy shorter than five minutes.
_MIN_TTL_S = 5 * 60
_TOKEN_TTL_SLACK_S = 3600
_TTL_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_TTL_DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?[wdhms])+")
_TTL_TERM_RE = re.compile(r"(\d+(?:\.\d+)?)([wdhms])")
_TERMINAL_PROCESS_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})
_RUNNING_SANDBOX_STATUSES = frozenset({"DEPLOYED"})
_STOPPED_SANDBOX_STATUSES = frozenset(
    {"FAILED", "DELETING", "TERMINATED", "ARCHIVING", "ARCHIVED"}
)
_RETRYABLE_STATUSES = frozenset({425, 429, 500, 502, 503, 504})
_CONTROL_CREDENTIAL_ENV_VARS = frozenset({API_KEY_ENV_VAR})
_CREATE_ATTEMPT_LABEL = "omnigent-create-attempt"
_ENV_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_CLIENTS: dict[tuple[str, str | None], SandboxClient] = {}
_CLIENTS_LOCK = threading.Lock()


def parse_ttl_seconds(ttl: str | None = None) -> int:
    """
    Resolve a sandbox max-age duration to whole seconds.

    The ``ttl`` is an age from creation, not an idle timeout: Baseten deletes
    the sandbox this long after creation regardless of activity.

    :param ttl: A duration in units ``w``, ``d``, ``h``, ``m``, or ``s``, alone
        or combined (``30m``, ``24h``, ``7d``, ``1h30m``). ``None`` resolves
        the provider default (``24h``).
    :returns: The sandbox lifetime in seconds.
    :raises ValueError: When the duration is malformed or non-positive.
    """
    text = _DEFAULT_TTL if ttl is None else ttl.strip().lower()
    if not _TTL_DURATION_RE.fullmatch(text):
        raise ValueError(f"expected a duration like '30m', '24h', '7d', or '2w'; got {ttl!r}")
    seconds = math.ceil(
        sum(float(value) * _TTL_UNIT_SECONDS[unit] for value, unit in _TTL_TERM_RE.findall(text))
    )
    if seconds < _MIN_TTL_S:
        raise ValueError(f"expected at least 5m, the shortest age Baseten accepts; got {ttl!r}")
    return seconds


def managed_token_ttl_s(ttl: str | None = None) -> int:
    """
    Launch-token TTL for the managed path, derived from (and always above)
    the sandbox max age so the token outlives the sandbox Baseten deletes
    across tunnel reconnects.

    :param ttl: The configured ``sandbox.baseten.ttl``, or ``None`` for the
        provider default.
    :returns: The token lifetime in seconds.
    """
    return parse_ttl_seconds(ttl) + _TOKEN_TTL_SLACK_S


def default_source_image() -> str:
    """
    The public host image the default Baseten image is built from.

    It is the installed Omnigent release's tag of the host image, so managed
    hosts run the server's own version, or ``:latest`` for a source checkout
    whose version has no published image.
    """
    try:
        version = metadata.version("omnigent")
    except metadata.PackageNotFoundError:
        return DEFAULT_HOST_IMAGE
    if _RELEASE_VERSION_RE.fullmatch(version):
        return f"{_HOST_IMAGE_REPOSITORY}:v{version}"
    return DEFAULT_HOST_IMAGE


def default_image_name(source_image: str) -> str:
    """The Baseten image name for one host image tag, e.g. ``omnigent-host-v0-24-0``."""
    tag = source_image.rsplit(":", 1)[1] if ":" in source_image else "latest"
    return DEFAULT_IMAGE_PREFIX + (re.sub(r"[^a-z0-9]+", "-", tag.lower()).strip("-") or "latest")


def _ensure_sdk() -> None:
    """Fail with an install hint when the optional SDK is absent."""
    try:
        import baseten.sandbox  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The Baseten SDK is required for the 'baseten' sandbox provider. "
            "Install it with `pip install 'omnigent[baseten]'`, then set "
            f"{API_KEY_ENV_VAR} to a Baseten API key that can create sandboxes."
        ) from exc


def _client() -> SandboxClient:
    """One shared SDK client per key and team: it caches the minted sandbox token."""
    _ensure_sdk()
    api_key = os.environ.get(API_KEY_ENV_VAR, "").strip()
    if not api_key:
        raise click.ClickException(
            f"Set {API_KEY_ENV_VAR} to a Baseten API key that can create sandboxes "
            f"(a personal key or a team creator key), and {TEAM_ID_ENV_VAR} when the "
            "key reaches more than one team."
        )
    team_id = os.environ.get(TEAM_ID_ENV_VAR, "").strip() or None
    with _CLIENTS_LOCK:
        client = _CLIENTS.get((api_key, team_id))
        if client is None:
            from baseten.sandbox import SandboxClient

            client = SandboxClient(api_key=api_key, team_id=team_id)
            _CLIENTS[(api_key, team_id)] = client
        return client


def _status(exc: BaseException) -> int | None:
    """The HTTP status of an SDK API error, else ``None``."""
    status = getattr(exc, "status", None)
    return status if isinstance(status, int) else None


def _is_not_found(exc: BaseException) -> bool:
    return _status(exc) == 404


def _is_retryable(exc: BaseException) -> bool:
    """Whether the sandbox is still starting or waking from standby."""
    status = _status(exc)
    if status is not None:
        return status in _RETRYABLE_STATUSES
    import httpx

    return isinstance(exc, httpx.TransportError)


def _start_never_reached(exc: BaseException) -> bool:
    """Whether a failed process start is known not to have reached the sandbox."""
    status = _status(exc)
    if status is not None:
        return status in {425, 429, 503}
    import httpx

    return isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout))


def _stop_attempt(sandbox: Sandbox, name: str) -> bool:
    """Kill a process start that may have run; whether it is known to be stopped."""
    try:
        sandbox.process.kill(identifier=name)
    except Exception as exc:
        return _status(exc) in {404, 409}
    return True


def _safe_provider_error(exc: BaseException, secret_values: Sequence[str] = ()) -> str:
    """Render an SDK error without echoing the API key or injected values."""
    message = str(exc) or type(exc).__name__
    for secret in (os.environ.get(API_KEY_ENV_VAR), *secret_values):
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _split_env_prefix(command: str) -> tuple[dict[str, str], str]:
    """Move leading shell assignments into a process environment mapping."""
    env: dict[str, str] = {}
    cursor = 0
    while match := _ENV_ASSIGNMENT_RE.match(command, cursor):
        name = match.group(1)
        end = match.end()
        quote: str | None = None
        escaped = False
        while end < len(command):
            char = command[end]
            if escaped:
                escaped = False
            elif quote == "'":
                if char == "'":
                    quote = None
            elif quote == '"':
                if char == '"':
                    quote = None
                elif char == "\\":
                    escaped = True
            elif char in {"'", '"'}:
                quote = char
            elif char == "\\":
                escaped = True
            elif char.isspace():
                break
            end += 1
        assignment = command[cursor:end]
        try:
            tokens = shlex.split(assignment)
        except ValueError as exc:
            raise click.ClickException(
                "background command has an invalid environment prefix"
            ) from exc
        if len(tokens) != 1:
            raise click.ClickException("background command has an invalid environment prefix")
        _parsed_name, value = tokens[0].split("=", 1)
        env[name] = value
        cursor = end
        while cursor < len(command) and command[cursor].isspace():
            cursor += 1
    if not env:
        return {}, command
    if cursor == len(command):
        raise click.ClickException("background command contains environment assignments only")
    return env, command[cursor:]


class _BasetenSandboxNotFound(click.ClickException):
    """Internal marker for an absent sandbox record."""


class _OutputLimitExceeded(RuntimeError):
    """Remote process output crossed the provider safety bound."""


def _fuller(streamed: str, recorded: str | None) -> str:
    """The longer of the streamed output and Baseten's recorded output."""
    return recorded if recorded is not None and len(recorded) > len(streamed) else streamed


def _timeout_message(timeout_s: int) -> str:
    return f"Remote command timed out after {timeout_s} seconds and was killed."


def _exit_code(process: object) -> int:
    """A finished process's exit code; a missing one counts as failure unless it completed."""
    status = str(getattr(process, "status", "") or "").lower()
    code = getattr(process, "exit_code", None)
    if code is None:
        return 0 if status == "completed" else 1
    return int(code)


class _ExecStream:
    """One streamed process: its output chunks, then its exit, bounded in size."""

    def __init__(
        self,
        launcher: BasetenSandboxLauncher,
        sandbox_id: str,
        sandbox: Sandbox,
        name: str,
        stream: SandboxStream[_ExecEvent],
        *,
        max_output_bytes: int = _MAX_PROCESS_OUTPUT_BYTES,
    ) -> None:
        self._launcher = launcher
        self._sandbox_id = sandbox_id
        self._sandbox = sandbox
        self.name = name
        self._stream = stream
        self._max_output_bytes = max_output_bytes
        self._output_bytes = 0
        self.exit_code: int | None = None
        # Baseten reports "killed" for a process stopped through the API,
        # which includes its own kill when the exec timeout runs out.
        self.status: str | None = None
        # The output Baseten recorded for the process, from its exit event.
        self.exit_stdout: str | None = None
        self.exit_stderr: str | None = None

    def chunks(self) -> Iterator[tuple[str, str]]:
        """Yield ``(stream, text)`` pairs until the process exits, then set :attr:`exit_code`."""
        from baseten.sandbox import SandboxProcessExecEventExit

        try:
            for event in self._stream:
                if isinstance(event, SandboxProcessExecEventExit):
                    self.exit_code = _exit_code(event.process)
                    self.status = str(getattr(event.process, "status", "") or "").lower()
                    self.exit_stdout = getattr(event.process, "stdout", None)
                    self.exit_stderr = getattr(event.process, "stderr", None)
                    return
                text = event.text
                self._output_bytes += len(text.encode("utf-8"))
                if self._output_bytes > self._max_output_bytes:
                    raise _OutputLimitExceeded(
                        f"remote process output exceeded {self._max_output_bytes} bytes"
                    )
                yield event.stream, text
        except Exception as exc:
            failure = self.kill()
            detail = f" Remote kill also failed: {failure}." if failure else ""
            raise click.ClickException(
                f"Baseten process output stream failed on sandbox '{self._sandbox_id}'; "
                "the remote process was killed: "
                f"{_safe_provider_error(exc, tuple(self._launcher._secret_values))}.{detail}"
            ) from exc
        finally:
            self.close()

    def kill(self) -> str | None:
        """Kill the process by name; return a sanitized failure, if any."""
        try:
            self._sandbox.process.kill(identifier=self.name)
        except Exception as exc:
            if _status(exc) in {404, 409}:
                return None
            return _safe_provider_error(exc, tuple(self._launcher._secret_values))
        return None

    def close(self) -> None:
        """Release the connection; the process keeps running."""
        with contextlib.suppress(Exception):
            self._stream.close()


class _BasetenRemoteProcess(RemoteProcess):
    """Combined output of one streamed process, line by line."""

    def __init__(self, exec_stream: _ExecStream) -> None:
        self._exec = exec_stream
        self._source = self._read_lines()
        self._backlog: deque[str] = deque()
        self._lines: Iterator[str] | None = None
        self._closed = False

    @property
    def lines(self) -> Iterator[str]:
        """Return the one persistent iterator over combined output."""
        if self._lines is None:
            self._lines = self._iter_lines()
        return self._lines

    def _read_lines(self) -> Iterator[str]:
        pending = ""
        for _stream, text in self._exec.chunks():
            pending += text
            while (newline := pending.find("\n")) >= 0:
                yield pending[: newline + 1]
                pending = pending[newline + 1 :]
            if len(pending) >= _MAX_PARTIAL_LINE_CHARS:
                yield pending
                pending = ""
        if pending:
            yield pending

    def _iter_lines(self) -> Iterator[str]:
        while not self._closed:
            if self._backlog:
                yield self._backlog.popleft()
                continue
            try:
                line = next(self._source)
            except StopIteration:
                return
            yield line

    def wait(self) -> int:
        """Wait for the exit, keeping unread output (bounded) for :attr:`lines`."""
        if self._exec.exit_code is None:
            self._backlog.extend(self._source)
        return self._exec.exit_code if self._exec.exit_code is not None else 1

    def close(self) -> None:
        """Kill the remote process if it still runs and stop the stream. Idempotent."""
        if self._closed:
            return
        self._closed = True
        if self._exec.exit_code is None:
            self._exec.kill()
        self._exec.close()


class BasetenSandboxLauncher(ExecModelHostLauncher):
    """Run Omnigent hosts in Baseten sandboxes."""

    provider: ClassVar[str] = "baseten"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=True,
            foreground_exec=True,
            git_clone_options=super().capabilities.git_clone_options,
        )

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        region: str | None = None,
        memory_mb: int | None = None,
        ttl: str | None = None,
    ) -> None:
        """Configure image, environment, region, memory, and deletion TTL.

        Sandboxes default to a 24-hour maximum age when *ttl* is omitted, and
        to 4096 MiB of memory, which Baseten pairs with 2 vCPUs.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._region = region
        self._memory_mb = memory_mb
        self._ttl = ttl
        self._resolved_image: str | None = None
        self._sandboxes: dict[str, Sandbox] = {}
        self._secret_values: set[str] = set()

    def _secrets(self, *extra: str) -> tuple[str, ...]:
        return (*self._secret_values, *extra)

    def _configured_image(self) -> str | None:
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or None

    def _resolve_region(self) -> str | None:
        return self._region or os.environ.get(REGION_ENV_VAR) or None

    def _resolve_sandbox_env(self) -> dict[str, str]:
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
            if name in _CONTROL_CREDENTIAL_ENV_VARS:
                raise click.ClickException(
                    f"sandbox env passthrough must not inject the Baseten API key {name}; "
                    "keep provider credentials in the Omnigent server process only."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server environment — set it or remove it from "
                    f"sandbox.baseten.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            self._secret_values.add(value)
            resolved[name] = value
        return resolved

    def _ensure_default_image(self, client: SandboxClient) -> str:
        """Build the public host image into the team on first use; return its reference."""
        from baseten.sandbox import ImageBuilder

        source = default_source_image()
        name = default_image_name(source)
        try:
            status = client.images.get_info(name=name).status
        except Exception as exc:
            if not _is_not_found(exc):
                raise
            status = None
        timeout = timedelta(seconds=_IMAGE_BUILD_TIMEOUT_S)
        if status in {"UPLOADING", "BUILDING"}:
            click.echo(f"▸ Waiting for Baseten image '{name}' to finish building")
            client.images.wait_built(name=name, timeout=timeout)
        elif status != "BUILT":
            click.echo(f"▸ Building Baseten image '{name}' from {source} (first use only)")
            client.images.push(
                name=name, builder=ImageBuilder.from_registry(source), timeout=timeout
            )
        return f"{name}:latest"

    def _image(self) -> str:
        if self._resolved_image is None:
            configured = self._configured_image()
            if configured is not None:
                self._resolved_image = configured
            else:
                try:
                    self._resolved_image = self._ensure_default_image(_client())
                except click.ClickException:
                    raise
                except Exception as exc:
                    raise click.ClickException(
                        "Could not prepare the Omnigent host image on Baseten: "
                        f"{_safe_provider_error(exc, self._secrets())}"
                    ) from exc
        return self._resolved_image

    def _resolve(self, sandbox_id: str) -> Sandbox:
        handle = self._sandboxes.get(sandbox_id)
        if handle is not None:
            return handle
        client = _client()
        try:
            info = client.get_info(name=sandbox_id)
        except Exception as exc:
            if _is_not_found(exc):
                raise _BasetenSandboxNotFound(
                    f"Baseten sandbox '{sandbox_id}' was not found; it may have "
                    "expired or been deleted."
                ) from exc
            raise click.ClickException(
                f"Could not access Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets())}"
            ) from exc
        if info.status in _STOPPED_SANDBOX_STATUSES:
            raise _BasetenSandboxNotFound(
                f"Baseten sandbox '{sandbox_id}' is {info.status.lower()}; it may have "
                "expired or been deleted."
            )
        handle = client.get(info=info)
        self._sandboxes[sandbox_id] = handle
        return handle

    def _wait_deployed(self, client: SandboxClient, name: str) -> Sandbox:
        """Wait until the sandbox is deployed and its sandbox API answers."""
        deadline = time.monotonic() + _DEPLOY_TIMEOUT_S
        while True:
            try:
                info = client.get_info(name=name)
            except Exception as exc:
                # The SDK does not retry control-plane reads; ride out a
                # transient error until the deadline.
                if not _is_retryable(exc) or time.monotonic() >= deadline:
                    raise
                time.sleep(_POLL_INTERVAL_S)
                continue
            if info.status == "DEPLOYED" and info.url:
                break
            if info.status in _STOPPED_SANDBOX_STATUSES:
                raise click.ClickException(f"Baseten sandbox '{name}' is {info.status}")
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"Baseten sandbox '{name}' is still {info.status} after {_DEPLOY_TIMEOUT_S}s"
                )
            time.sleep(_POLL_INTERVAL_S)
        sandbox = client.get(info=info)
        self._wait_for_sandbox_api(sandbox)
        return sandbox

    @staticmethod
    def _wait_for_sandbox_api(sandbox: Sandbox) -> None:
        """Wait, with bounded backoff, for the sandbox API to answer (also wakes standby)."""
        deadline = time.monotonic() + _READINESS_TIMEOUT_S
        delay = _READINESS_INITIAL_BACKOFF_S
        while True:
            try:
                sandbox.process.list()
                return
            except Exception as exc:
                remaining = deadline - time.monotonic()
                if not _is_retryable(exc) or remaining <= 0:
                    raise
                time.sleep(min(delay, remaining))
                delay = min(delay * 2, _READINESS_MAX_BACKOFF_S)

    def prepare(self) -> None:
        """Verify the SDK and key, and make sure the host image is built."""
        client = _client()
        self._secret_values.add(os.environ.get(API_KEY_ENV_VAR, ""))
        self._secret_values.discard("")
        try:
            # Confirms the key can mint a sandbox token for the team.
            client.images.list_library()
        except Exception as exc:
            raise click.ClickException(
                "Baseten rejected the sandbox credentials. Use a personal or team creator "
                f"API key in {API_KEY_ENV_VAR}, and set {TEAM_ID_ENV_VAR} to a team the key "
                f"belongs to: {_safe_provider_error(exc, self._secrets())}"
            ) from exc
        self._image()

    def provision(self, name: str) -> str:
        """Create a sandbox from the host image and wait until its API answers."""
        from baseten.sandbox import (
            SandboxEnvValue,
            SandboxExpirationPolicyMaxAge,
            SandboxLifecycle,
        )

        client = _client()
        image = self._image()
        create_attempt = uuid.uuid4().hex
        env = self._resolve_sandbox_env()
        try:
            max_age = timedelta(seconds=parse_ttl_seconds(self._ttl))
        except ValueError as exc:
            raise click.ClickException(f"Invalid Baseten sandbox ttl: {exc}") from exc
        click.echo(f"▸ Creating Baseten sandbox '{name}'")
        created = False
        try:
            client.create(
                name=name,
                image=image,
                memory=self._memory_mb or _DEFAULT_MEMORY_MIB,
                region=self._resolve_region(),
                envs={key: SandboxEnvValue(value=value, secret=True) for key, value in env.items()}
                or None,
                labels={"managed-by": "omnigent", _CREATE_ATTEMPT_LABEL: create_attempt},
                lifecycle=SandboxLifecycle(
                    expiration_policies=[SandboxExpirationPolicyMaxAge(after=max_age)],
                ),
            )
            created = True
            sandbox = self._wait_deployed(client, name)
        except Exception as exc:
            cleanup_failure = self._cleanup_failed_create(
                client, name, create_attempt, created=created, exc=exc
            )
            raise click.ClickException(
                "Baseten sandbox creation failed: "
                f"{_safe_provider_error(exc, self._secrets())}.{cleanup_failure}"
            ) from exc
        self._sandboxes[name] = sandbox
        click.echo(f"  → created {name}")
        return name

    def _cleanup_failed_create(
        self,
        client: SandboxClient,
        name: str,
        create_attempt: str,
        *,
        created: bool,
        exc: BaseException,
    ) -> str:
        """Delete what a failed create left, but only a sandbox this attempt made."""
        status = _status(exc)
        try:
            if not created:
                if status is not None and status < 500:
                    return ""
                # A transport or 5xx failure can commit remotely and lose the
                # response. A fixed CLI name may belong to an earlier sandbox,
                # so delete only one carrying this attempt's label.
                info = client.get_info(name=name)
                if info.labels.get(_CREATE_ATTEMPT_LABEL) != create_attempt:
                    return ""
            self.terminate(name)
        except Exception as cleanup_exc:
            if _is_not_found(cleanup_exc):
                return ""
            return (
                f" Cleanup also failed for sandbox '{name}': "
                f"{_safe_provider_error(cleanup_exc, self._secrets())}"
            )
        return ""

    def attach(self, sandbox_id: str) -> None:
        """Validate an existing sandbox and wake its sandbox API."""
        click.echo(f"▸ Reusing existing Baseten sandbox '{sandbox_id}'")
        try:
            self._wait_for_sandbox_api(self._resolve(sandbox_id))
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not attach to Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets())}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """Explain that the host process keeps the sandbox off standby."""
        del sandbox_id
        click.echo("  → the Omnigent host process keeps the Baseten sandbox active")

    def _stream(
        self,
        sandbox_id: str,
        command: str,
        *,
        name: str,
        timeout_s: int,
        keep_alive: bool = False,
        env: dict[str, str] | None = None,
    ) -> _ExecStream:
        """Start a command and return its output stream, retrying a sandbox that is waking."""
        sandbox = self._resolve(sandbox_id)
        last: Exception | None = None
        for attempt in range(_START_ATTEMPTS):
            attempt_name = name if attempt == 0 else f"{name}-{attempt}"
            try:
                stream = sandbox.process.exec_stream(
                    command=command,
                    name=attempt_name,
                    env=env,
                    timeout=timedelta(seconds=timeout_s),
                    keep_alive=keep_alive or None,
                )
            except Exception as exc:
                last = exc
                # A lost response does not mean the start failed. Unless the
                # request never reached the sandbox, stop this attempt, and
                # retry only once it is known not to be running.
                safe = _start_never_reached(exc) or _stop_attempt(sandbox, attempt_name)
                if not (_is_retryable(exc) and safe) or attempt == _START_ATTEMPTS - 1:
                    break
                time.sleep(_READINESS_INITIAL_BACKOFF_S * (2**attempt))
                continue
            return _ExecStream(self, sandbox_id, sandbox, attempt_name, stream)
        assert last is not None
        raise click.ClickException(
            f"Remote command could not start on Baseten sandbox '{sandbox_id}': "
            f"{_safe_provider_error(last, self._secrets(*(env or {}).values()))}"
        ) from last

    def _consume(
        self,
        exec_stream: _ExecStream,
        on_output: Callable[[str, str], None],
        *,
        timeout_s: int | None,
    ) -> int:
        """Read a stream to its exit; kill the process on timeout or Ctrl-C.

        Baseten kills the process when *timeout_s* runs out. A local deadline,
        a grace period later, backs that up for a sandbox that stops
        answering: the SDK stream cannot be closed from another thread, so it
        is read on a daemon thread that the caller can stop waiting on.
        """
        items: queue.SimpleQueue[object] = queue.SimpleQueue()

        def _read() -> None:
            try:
                for chunk in exec_stream.chunks():
                    items.put(chunk)
            except BaseException as exc:  # handed to the caller below
                items.put(exc)
            else:
                items.put(None)

        # After a local deadline the reader may stay blocked on its connection
        # until Baseten closes it; it is a daemon and holds no caller state.
        threading.Thread(target=_read, name=f"baseten-{exec_stream.name}", daemon=True).start()
        watchdog: threading.Timer | None = None
        if timeout_s is not None:
            watchdog = threading.Timer(timeout_s + _COMMAND_KILL_GRACE_S, items.put, (_DEADLINE,))
            watchdog.daemon = True
            watchdog.start()

        def _pump() -> None:
            while True:
                try:
                    item = items.get(timeout=_QUEUE_POLL_S)
                except queue.Empty:
                    continue  # wake up so Ctrl-C is handled promptly
                if item is None:
                    return
                if item is _DEADLINE and timeout_s is not None:
                    failure = exec_stream.kill()
                    detail = f" Remote kill also failed: {failure}." if failure else ""
                    raise click.ClickException(_timeout_message(timeout_s) + detail)
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, tuple):
                    on_output(*item)

        try:
            # In its own frame: CPython 3.12 can raise a Ctrl-C at this loop's
            # backward jump past a handler in the same function.
            _pump()
        except KeyboardInterrupt:
            click.echo("\n  → interrupted; stopping the remote process")
            if failure := exec_stream.kill():
                click.echo(f"  ! remote process cleanup failed: {failure}", err=True)
            raise
        finally:
            if watchdog is not None:
                watchdog.cancel()
        # Baseten's own timeout kill ends the stream with an ordinary exit
        # event (exit -1, status "killed").
        if timeout_s is not None and exec_stream.status == "killed":
            raise click.ClickException(_timeout_message(timeout_s))
        if exec_stream.exit_code is None:
            raise click.ClickException("Baseten process output ended before the command exited.")
        return exec_stream.exit_code

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run a bounded command and capture stdout and stderr."""
        exec_stream = self._stream(
            sandbox_id,
            command,
            name=f"omni-command-{uuid.uuid4().hex[:12]}",
            timeout_s=_COMMAND_TIMEOUT_S,
        )
        stdout: list[str] = []
        stderr: list[str] = []

        def _collect(stream_name: str, text: str) -> None:
            (stderr if stream_name == "stderr" else stdout).append(text)

        returncode = self._consume(exec_stream, _collect, timeout_s=_COMMAND_TIMEOUT_S)
        # A process that ends before streaming starts is replayed one line per
        # event without newlines; Baseten's record from the exit event keeps
        # them. The streamed text wins when that record is cut short.
        out = _fuller("".join(stdout), exec_stream.exit_stdout)
        err = _fuller("".join(stderr), exec_stream.exit_stderr)
        for output in (out, err):
            if output:
                click.echo(output, nl=False)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on Baseten sandbox '{sandbox_id}' (exit {returncode})."
            )
        return RemoteCommandResult(returncode=returncode, stdout=out, stderr=err)

    def run_background(
        self,
        sandbox_id: str,
        command: str,
        *,
        log_path: str = "/tmp/omnigent-host.log",
    ) -> RemoteCommandResult:
        """Start a supervised host that keeps the sandbox off standby until its TTL."""
        env, clean_command = _split_env_prefix(command)
        supervised = supervise_host_command(clean_command)
        remote = f"exec sh -c {shlex.quote(supervised)} > {shlex.quote(log_path)} 2>&1"
        sandbox = self._resolve(sandbox_id)
        try:
            process = sandbox.process.exec(
                command=remote,
                name=f"omnigent-host-{uuid.uuid4().hex[:12]}",
                env=env or None,
                # Zero: no time limit. Keep-alive holds standby off while it runs.
                timeout=timedelta(0),
                keep_alive=True,
                wait_for_completion=False,
            )
        except Exception as exc:
            raise click.ClickException(
                f"Omnigent host process failed to start on Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets(*env.values()))}"
            ) from exc
        if str(getattr(process, "status", "")).lower() in _TERMINAL_PROCESS_STATUSES:
            raise click.ClickException(
                f"Omnigent host process failed to start on Baseten sandbox '{sandbox_id}'."
            )
        return RemoteCommandResult(returncode=0, stdout="launched\n", stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Copy one binary-safe file through the sandbox filesystem API."""
        try:
            self._resolve(sandbox_id).fs.write_bytes(
                path=remote_path, content=local_path.read_bytes()
            )
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not copy a file to Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets())}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Spawn a command and stream its combined output line by line.

        Baseten's process API streams stdout and stderr and allocates no PTY,
        so the *pty* flag is unused and output is combined either way.

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: Accepted for the contract; unused (see above).
        :returns: Handle streaming the process's combined output.
        :raises click.ClickException: When the command cannot be started.
        """
        del pty  # unused (see docstring)
        exec_stream = self._stream(
            sandbox_id,
            command,
            name=f"omni-stream-{uuid.uuid4().hex[:12]}",
            # No time limit: the OAuth login this backs can outlast a normal command.
            timeout_s=0,
            keep_alive=True,
            env={"TERM": "xterm-256color"},
        )
        return _BasetenRemoteProcess(exec_stream)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Stream a foreground command and kill it on local cancellation."""
        exec_stream = self._stream(
            sandbox_id,
            command,
            name=f"omni-foreground-{uuid.uuid4().hex[:12]}",
            timeout_s=0,
            keep_alive=True,
            env={"TERM": "xterm-256color"},
        )

        def _echo(_stream_name: str, text: str) -> None:
            click.echo(text, nl=False)

        return self._consume(exec_stream, _echo, timeout_s=None)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Overlay local wheels onto the prebuilt Omnigent host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """Delete a sandbox and wait until Baseten reports it gone. Idempotent."""
        self._sandboxes.pop(sandbox_id, None)
        client = _client()
        try:
            client.delete(name=sandbox_id)
        except Exception as exc:
            if _is_not_found(exc):
                return
            raise click.ClickException(
                f"Could not delete Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets())}"
            ) from exc
        deadline = time.monotonic() + _DELETE_TIMEOUT_S
        status = "DELETING"
        while time.monotonic() < deadline:
            try:
                status = client.get_info(name=sandbox_id).status
            except Exception as exc:
                if _is_not_found(exc):
                    return
                if _is_retryable(exc):
                    time.sleep(_POLL_INTERVAL_S)
                    continue
                raise click.ClickException(
                    f"Could not finish deleting Baseten sandbox '{sandbox_id}': "
                    f"{_safe_provider_error(exc, self._secrets())}"
                ) from exc
            if status == "TERMINATED":
                return
            time.sleep(_POLL_INTERVAL_S)
        raise click.ClickException(
            f"Baseten sandbox '{sandbox_id}' did not terminate within "
            f"{_DELETE_TIMEOUT_S} seconds; last status: {status}."
        )

    def is_running(self, sandbox_id: str) -> bool | None:
        """Report whether the sandbox is deployed, stopped, or (``None``) in between."""
        self._sandboxes.pop(sandbox_id, None)
        try:
            info = _client().get_info(name=sandbox_id)
        except Exception as exc:
            if _is_not_found(exc):
                return False
            raise click.ClickException(
                f"Could not read Baseten sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, self._secrets())}"
            ) from exc
        if info.status in _RUNNING_SANDBOX_STATUSES:
            return True
        if info.status in _STOPPED_SANDBOX_STATUSES:
            return False
        return None
