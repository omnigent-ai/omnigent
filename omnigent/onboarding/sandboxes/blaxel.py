"""Blaxel sandbox launcher.

The launcher uses the optional :mod:`blaxel` Python SDK. Blaxel sandboxes
execute commands through their built-in sandbox API, so the provider follows
Omnigent's exec-model host contract.

The standard ``blaxel/omnigent-host`` image combines the Omnigent host runtime
with Blaxel's ``sandbox-api`` binary. Deployments can override that default with
a fixed Blaxel image tag, like other managed sandbox providers.
"""

from __future__ import annotations

import codecs
import contextlib
import io
import math
import os
import re
import shlex
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import quote

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    ExecModelHostLauncher,
    RemoteCommandResult,
    RemoteProcess,
    host_image_wheel_install_command,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from pathlib import Path

    from blaxel.core import SyncSandboxInstance


DEFAULT_BLAXEL_HOST_IMAGE: str = "blaxel/omnigent-host:latest"
"""Public Blaxel Hub image containing the Omnigent host runtime."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_BLAXEL_HOST_IMAGE"
"""Optional override for the Blaxel-compatible Omnigent host image."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_BLAXEL_SANDBOX_ENV"
"""Comma-separated server environment variable names to inject."""

REGION_ENV_VAR: str = "OMNIGENT_BLAXEL_REGION"
"""Optional Blaxel region override."""

_DEFAULT_MEMORY_MIB = 4096
_COMMAND_TIMEOUT_S = 15 * 60
_DELETE_TIMEOUT_S = 2 * 60
_READINESS_TIMEOUT_S = 90.0
_READINESS_INITIAL_BACKOFF_S = 0.5
_READINESS_MAX_BACKOFF_S = 30.0
_PROCESS_POLL_INTERVAL_S = 1.0
_STREAM_POLL_INTERVAL_S = 0.1
_STREAM_NETWORK_CHUNK_BYTES = 8 * 1024
_STREAM_LINE_BUFFER_BYTES = 64 * 1024
_MAX_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024
_STREAM_BUFFER_BYTES = _MAX_PROCESS_OUTPUT_BYTES
_UTF8_COUNT_CHARS = 16 * 1024
_STREAM_CLOSE_TIMEOUT_S = 1.0
_DEFAULT_TTL = "24h"
_TOKEN_TTL_SLACK_S = 3600
_TTL_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}
_TTL_DURATION_RE = re.compile(r"(?:\d+(?:\.\d+)?[wdhms])+")
_TTL_TERM_RE = re.compile(r"(\d+(?:\.\d+)?)([wdhms])")
_TERMINAL_PROCESS_STATUSES = frozenset({"completed", "failed", "killed", "stopped"})
_RUNNING_SANDBOX_STATUSES = frozenset({"deployed"})
_STOPPED_SANDBOX_STATUSES = frozenset(
    {"deactivated", "deactivating", "deleting", "failed", "terminated", "terminating"}
)
_CONTROL_CREDENTIAL_ENV_VARS = frozenset({"BL_API_KEY", "BL_CLIENT_CREDENTIALS"})
_CREATE_ATTEMPT_LABEL = "omnigent-create-attempt"
_ENV_ASSIGNMENT_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")


def parse_ttl_seconds(ttl: str | None = None) -> int:
    """
    Resolve a Blaxel sandbox max-age duration to whole seconds.

    Blaxel's ``ttl`` is an age from creation, not an idle timeout: the
    sandbox is deleted this long after creation regardless of activity.

    :param ttl: A Blaxel duration in units ``w``, ``d``, ``h``, ``m``, or
        ``s``, alone or combined (``30m``, ``24h``, ``7d``, ``1h30m``).
        ``None`` resolves the provider default (``24h``).
    :returns: The sandbox lifetime in seconds.
    :raises ValueError: When the duration is malformed or non-positive.
    """
    text = _DEFAULT_TTL if ttl is None else ttl.strip().lower()
    if not _TTL_DURATION_RE.fullmatch(text):
        raise ValueError(f"expected a duration like '30m', '24h', '7d', or '2w'; got {ttl!r}")
    seconds = math.ceil(
        sum(float(value) * _TTL_UNIT_SECONDS[unit] for value, unit in _TTL_TERM_RE.findall(text))
    )
    if seconds <= 0:
        raise ValueError(f"expected a positive duration; got {ttl!r}")
    return seconds


def managed_token_ttl_s(ttl: str | None = None) -> int:
    """
    Launch-token TTL for the managed path, derived from (and always above)
    the sandbox max age so the token outlives the sandbox Blaxel reaps
    across tunnel reconnects.

    :param ttl: The configured ``sandbox.blaxel.ttl``, or ``None`` for the
        provider default.
    :returns: The token lifetime in seconds.
    """
    return parse_ttl_seconds(ttl) + _TOKEN_TTL_SLACK_S


def _ensure_sdk() -> None:
    """Fail with an install hint when the optional SDK is absent."""
    try:
        import blaxel  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The Blaxel SDK is required for the 'blaxel' sandbox provider. "
            "Install it with `pip install 'omnigent[blaxel]'`, then set "
            "BL_WORKSPACE and BL_API_KEY or install the Blaxel CLI and run `bl login`."
        ) from exc


def _status_value(value: object) -> str:
    """Normalize SDK enum and string statuses."""
    resolved = getattr(value, "value", value)
    return str(resolved or "").lower()


def _sandbox_label(handle: object, key: str) -> str | None:
    """Read one SDK metadata label without depending on its generated type."""
    metadata = getattr(handle, "metadata", None)
    labels = getattr(metadata, "labels", None)
    if labels is None:
        return None
    if hasattr(labels, "to_dict"):
        labels = labels.to_dict()
    if not isinstance(labels, dict):
        return None
    value = labels.get(key)
    return str(value) if value is not None else None


def _is_retryable_readiness_error(exc: BaseException) -> bool:
    """Return whether Blaxel says the sandbox API is still starting."""
    data = getattr(exc, "data", None)
    if isinstance(data, dict):
        provider_error = data.get("error")
        if isinstance(provider_error, dict):
            if provider_error.get("retryable") is True:
                return True
            if provider_error.get("code") == "WORKLOAD_UNAVAILABLE":
                return True

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {425, 429, 500, 502, 503, 504}


def _wait_for_sandbox_api(handle: object) -> None:
    """Wait for the sandbox API with the provider's bounded backoff guidance."""
    deadline = time.monotonic() + _READINESS_TIMEOUT_S
    delay = _READINESS_INITIAL_BACKOFF_S
    while True:
        try:
            handle.fs.ls("/")  # type: ignore[attr-defined]
            return
        except Exception as exc:
            remaining = deadline - time.monotonic()
            if not _is_retryable_readiness_error(exc) or remaining <= 0:
                raise
            time.sleep(min(delay, remaining))
            delay = min(max(delay * 2, _READINESS_INITIAL_BACKOFF_S), _READINESS_MAX_BACKOFF_S)


def _safe_provider_error(exc: Exception, secret_values: Sequence[str] = ()) -> str:
    """Render an SDK error without echoing configured control credentials."""
    message = str(exc) or type(exc).__name__
    for name in ("BL_API_KEY", "BL_CLIENT_CREDENTIALS"):
        secret = os.environ.get(name)
        if secret:
            message = message.replace(secret, "[redacted]")
    for secret in secret_values:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _bounded_utf8_size(values: Sequence[str], limit: int) -> int:
    """Count UTF-8 bytes with bounded temporary allocations, capped at limit + 1."""
    total = 0
    for value in values:
        for offset in range(0, len(value), _UTF8_COUNT_CHARS):
            chunk_size = len(value[offset : offset + _UTF8_COUNT_CHARS].encode("utf-8"))
            if chunk_size > limit - total:
                return limit + 1
            total += chunk_size
    return total


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


class _BlaxelSandboxNotFound(click.ClickException):
    """Internal marker for an absent sandbox record."""


class _OutputLimitExceeded(RuntimeError):
    """Remote process output crossed the provider safety bound."""


class _BlaxelLogParser:
    """Parse the sandbox-api log stream without an unbounded line buffer."""

    def __init__(
        self,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
        *,
        max_line_bytes: int = _STREAM_LINE_BUFFER_BYTES,
        max_total_bytes: int = _MAX_PROCESS_OUTPUT_BYTES,
    ) -> None:
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._max_line_bytes = max_line_bytes
        self._max_total_bytes = max_total_bytes
        self._buffer = bytearray()
        # sandbox-api prefixes each writer write, not each embedded line. Keep
        # the last explicit stream until a later prefix switches it.
        self._current_kind = "stdout"
        self._output_bytes = 0
        decoder = codecs.getincrementaldecoder("utf-8")
        self._decoders = {
            "stdout": decoder(errors="replace"),
            "stderr": decoder(errors="replace"),
        }

    def feed(self, chunk: bytes) -> None:
        """Consume one bounded network chunk in linear time."""
        self._buffer.extend(chunk)
        offset = 0
        while offset < len(self._buffer):
            newline = self._buffer.find(b"\n", offset)
            if newline >= 0 and newline - offset < self._max_line_bytes:
                segment = bytes(self._buffer[offset:newline])
                offset = newline + 1
                if segment.endswith(b"\r"):
                    segment = segment[:-1]
                self._emit_segment(segment, terminated=True)
                continue
            if len(self._buffer) - offset < self._max_line_bytes:
                break
            segment_end = offset + self._max_line_bytes
            segment = bytes(self._buffer[offset:segment_end])
            offset = segment_end
            self._emit_segment(segment, terminated=False)
        if offset:
            # Delete the consumed prefix once per network chunk; deleting for
            # each short line would make newline-heavy output quadratic.
            del self._buffer[:offset]

    def finish(self) -> None:
        """Flush a final unterminated fragment when the HTTP stream closes."""
        if self._buffer:
            segment = bytes(self._buffer)
            self._buffer.clear()
            self._emit_segment(segment, terminated=False)
        for kind, decoder in self._decoders.items():
            if tail := decoder.decode(b"", final=True):
                callback = self._on_stderr if kind == "stderr" else self._on_stdout
                callback(tail)

    def _emit_segment(self, segment: bytes, *, terminated: bool) -> None:
        payload = segment
        if segment.startswith(b"stdout:"):
            self._current_kind = "stdout"
            payload = segment[7:]
        elif segment.startswith(b"stderr:"):
            self._current_kind = "stderr"
            payload = segment[7:]
        elif segment.startswith(b"[keepalive]"):
            return
        kind = self._current_kind
        if terminated:
            payload += b"\n"
        next_total = self._output_bytes + len(payload)
        if next_total > self._max_total_bytes:
            raise _OutputLimitExceeded(
                f"remote process output exceeded {self._max_total_bytes} bytes"
            )
        self._output_bytes = next_total
        if not payload:
            return
        callback = self._on_stderr if kind == "stderr" else self._on_stdout
        if text := self._decoders[kind].decode(payload, final=False):
            callback(text)


class _BlaxelLogStream:
    """Observable, bounded parser around sandbox-api's streaming log endpoint."""

    def __init__(
        self,
        process_api: Any,
        identifier: str,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
        *,
        max_output_bytes: int = _MAX_PROCESS_OUTPUT_BYTES,
        on_error: Callable[[Exception], str | None] | None = None,
    ) -> None:
        self._process_api = process_api
        self._identifier = identifier
        self._on_stdout = on_stdout
        self._on_stderr = on_stderr
        self._max_output_bytes = max_output_bytes
        self._on_error = on_error
        self._closed = threading.Event()
        self._close_started = threading.Event()
        self._done = threading.Event()
        self._error: Exception | None = None
        self._error_cleanup_attempted = False
        self._error_cleanup_failure: str | None = None
        self._active_lock = threading.Lock()
        self._client: httpx.Client | None = None
        self._response: httpx.Response | None = None
        self._thread = threading.Thread(
            target=self._run,
            name=f"blaxel-log-{identifier}",
            daemon=True,
        )
        self._thread.start()

    @property
    def done(self) -> bool:
        """Whether the reader reached EOF, failed, or was closed."""
        return self._done.is_set()

    @property
    def error(self) -> Exception | None:
        """Return the reader failure instead of losing it in a daemon thread."""
        return self._error

    @property
    def error_cleanup_attempted(self) -> bool:
        """Whether the reader itself attempted remote-process cleanup."""
        return self._error_cleanup_attempted

    @property
    def error_cleanup_failure(self) -> str | None:
        """Return a sanitized reader-thread cleanup failure, when present."""
        return self._error_cleanup_failure

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the reader and report whether it finished."""
        return self._done.wait(timeout)

    def close(self) -> None:
        """Close the live response so an idle no-timeout read unblocks."""
        if self._close_started.is_set():
            return
        self._close_started.set()
        self._closed.set()
        with self._active_lock:
            response = self._response
            client = self._client
        if response is not None:
            with contextlib.suppress(Exception):
                response.close()
        if client is not None:
            with contextlib.suppress(Exception):
                client.close()
        self._thread.join(timeout=_STREAM_CLOSE_TIMEOUT_S)

    def _run(self) -> None:
        parser = _BlaxelLogParser(
            self._on_stdout,
            self._on_stderr,
            max_total_bytes=self._max_output_bytes,
        )
        try:
            client = self._process_api.get_client()
            # Claim the client only while still open. A close racing a slow
            # get_client() must prevent activation, not lose the only handle to
            # a later no-read-timeout request.
            with self._active_lock:
                if self._closed.is_set():
                    with contextlib.suppress(Exception):
                        client.close()
                    return
                self._client = client
            # sandbox-api emits keepalives every 30s. The SDK's implicit 5s
            # read timeout drops a healthy quiet process, so reads stay open.
            client.timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
            with client:
                path = f"/process/{quote(self._identifier, safe='')}/logs/stream"
                with client.stream("GET", path) as response:
                    with self._active_lock:
                        self._response = response
                    self._process_api.handle_response_error(response)
                    for chunk in response.iter_bytes(chunk_size=_STREAM_NETWORK_CHUNK_BYTES):
                        if self._closed.is_set():
                            break
                        parser.feed(chunk)
                    if not self._closed.is_set():
                        parser.finish()
        except Exception as exc:
            if not self._closed.is_set():
                self._error = exc
                if self._on_error is not None:
                    self._error_cleanup_attempted = True
                    try:
                        self._error_cleanup_failure = self._on_error(exc)
                    except Exception as cleanup_exc:
                        self._error_cleanup_failure = str(cleanup_exc)
        finally:
            with self._active_lock:
                self._response = None
                self._client = None
            self._done.set()


def _open_process_log_stream(
    process_api: Any,
    identifier: str,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
    *,
    max_output_bytes: int = _MAX_PROCESS_OUTPUT_BYTES,
    on_error: Callable[[Exception], str | None] | None = None,
) -> _BlaxelLogStream:
    """Open the local fixed stream implementation (a narrow test seam)."""
    return _BlaxelLogStream(
        process_api,
        identifier,
        on_stdout,
        on_stderr,
        max_output_bytes=max_output_bytes,
        on_error=on_error,
    )


class _LineSink:
    """Byte-bounded stream buffer that fails instead of blocking its reader."""

    def __init__(self, max_bytes: int = _STREAM_BUFFER_BYTES) -> None:
        self._max_bytes = max_bytes
        self._buffered_bytes = 0
        self._buffer = io.StringIO()
        self._lock = threading.Lock()
        self._closed = False

    def feed(self, text: str) -> None:
        """Accept one SDK-style line (kept for direct contract tests)."""
        self.feed_chunk(f"{text}\n")

    def feed_chunk(self, text: str) -> None:
        """Buffer a chunk or fail immediately when the byte budget is exhausted."""
        size = len(text.encode("utf-8"))
        with self._lock:
            if self._closed:
                return
            if size > self._max_bytes - self._buffered_bytes:
                raise _OutputLimitExceeded(
                    f"remote process output buffer exceeded {self._max_bytes} bytes"
                )
            self._buffer.write(text)
            self._buffered_bytes += size

    def drain(self) -> list[str]:
        """Drain buffered text as one bounded chunk without per-line allocation."""
        with self._lock:
            text = self._buffer.getvalue()
            self._buffer.seek(0)
            self._buffer.truncate(0)
            self._buffered_bytes = 0
        return [text] if text else []

    def close(self) -> None:
        """Discard future output after local cancellation."""
        with self._lock:
            self._closed = True


class _BlaxelRemoteProcess(RemoteProcess):
    """Stream one bounded Blaxel process without cumulative-log polling."""

    def __init__(
        self,
        launcher: BlaxelSandboxLauncher,
        handle: SyncSandboxInstance,
        identifier: str,
        sink: _LineSink,
        log_stream: _BlaxelLogStream,
    ) -> None:
        self._launcher = launcher
        self._handle = handle
        self._identifier = identifier
        self._sink = sink
        self._log_stream = log_stream
        self._lines: Iterator[str] | None = None
        self._prefetched = io.StringIO()
        self._exit_code: int | None = None
        self._closed = False

    @property
    def lines(self) -> Iterator[str]:
        """Return the one persistent iterator over combined output."""
        if self._lines is None:
            self._lines = self._iter_lines()
        return self._lines

    def _current(self) -> object:
        try:
            return self._handle.process.get(self._identifier)
        except Exception as exc:
            raise click.ClickException(
                "Could not read Blaxel process state: "
                f"{_safe_provider_error(exc, tuple(self._launcher._secret_values))}"
            ) from exc

    def _raise_stream_error(self, exc: Exception) -> None:
        if self._log_stream.error_cleanup_attempted:
            kill_failure = self._log_stream.error_cleanup_failure
        else:
            kill_failure = self._launcher._kill_process(self._handle, self._identifier)
        detail = f" Remote kill also failed: {kill_failure}." if kill_failure else ""
        self._log_stream.close()
        raise click.ClickException(
            "Blaxel process output stream failed; the remote process was killed: "
            f"{_safe_provider_error(exc, tuple(self._launcher._secret_values))}.{detail}"
        ) from exc

    def _finish(self) -> int:
        if self._exit_code is not None:
            return self._exit_code
        if error := self._log_stream.error:
            self._raise_stream_error(error)
        current = self._current()
        status = _status_value(getattr(current, "status", ""))
        if status not in _TERMINAL_PROCESS_STATUSES:
            self._raise_stream_error(
                RuntimeError("the log endpoint closed before the process reached a terminal state")
            )
        raw_exit_code = getattr(current, "exit_code", None)
        self._exit_code = int(
            raw_exit_code if raw_exit_code is not None else (0 if status == "completed" else 1)
        )
        self._log_stream.close()
        return self._exit_code

    def _take_prefetched(self) -> str:
        prefetched = self._prefetched.getvalue()
        self._prefetched.seek(0)
        self._prefetched.truncate(0)
        return prefetched

    def _iter_lines(self) -> Iterator[str]:
        while not self._closed:
            if prefetched := self._take_prefetched():
                yield from io.StringIO(prefetched)
                continue
            buffered = self._sink.drain()
            if buffered:
                yield from io.StringIO(buffered[0])
                continue
            if self._log_stream.done:
                final_chunks = self._sink.drain()
                if final_chunks:
                    yield from io.StringIO(final_chunks[0])
                self._finish()
                return
            self._log_stream.wait(_STREAM_POLL_INTERVAL_S)

    def wait(self) -> int:
        """Wait for completion while retaining bounded output for later reads."""
        if self._exit_code is not None:
            return self._exit_code
        while not self._log_stream.done:
            self._prefetched.writelines(self._sink.drain())
            self._log_stream.wait(_STREAM_POLL_INTERVAL_S)
        self._prefetched.writelines(self._sink.drain())
        return self._finish()

    def close(self) -> None:
        """Kill the remote process and stop the stream. Idempotent."""
        if self._closed:
            return
        self._closed = True
        self._sink.close()
        if self._exit_code is None:
            if not self._log_stream.error_cleanup_attempted:
                self._launcher._kill_process(self._handle, self._identifier)
            elif self._log_stream.error_cleanup_failure is not None:
                # The reader already tried; retry only when that attempt failed.
                self._launcher._kill_process(self._handle, self._identifier)
        self._log_stream.close()


class BlaxelSandboxLauncher(ExecModelHostLauncher):
    """Run Omnigent hosts in Blaxel sandboxes."""

    provider: ClassVar[str] = "blaxel"

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

        Sandboxes default to a 24-hour maximum age when *ttl* is omitted.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._region = region
        self._memory_mb = memory_mb
        self._ttl = ttl
        self._sandboxes: dict[str, SyncSandboxInstance] = {}
        self._secret_values: set[str] = set()

    def _resolve_image(self) -> str:
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_BLAXEL_HOST_IMAGE

    def _resolve_region(self) -> str | None:
        return self._region or os.environ.get(REGION_ENV_VAR) or os.environ.get("BL_REGION")

    def _resolve_sandbox_env(self) -> list[dict[str, str]]:
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: list[dict[str, str]] = []
        for name in names:
            if name in _CONTROL_CREDENTIAL_ENV_VARS:
                raise click.ClickException(
                    f"sandbox env passthrough must not inject Blaxel control credential {name}; "
                    "keep provider credentials in the Omnigent server process only."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server environment — set it or remove it from "
                    f"sandbox.blaxel.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            self._secret_values.add(value)
            resolved.append({"name": name, "value": value})
        return resolved

    def _resolve(self, sandbox_id: str) -> SyncSandboxInstance:
        _ensure_sdk()
        handle = self._sandboxes.get(sandbox_id)
        if handle is not None:
            return handle
        from blaxel.core import SyncSandboxInstance
        from blaxel.core.sandbox import SandboxAPIError

        try:
            handle = SyncSandboxInstance.get(sandbox_id)
        except SandboxAPIError as exc:
            if exc.status_code == 404:
                raise _BlaxelSandboxNotFound(
                    f"Blaxel sandbox '{sandbox_id}' was not found; it may have "
                    "expired or been deleted."
                ) from exc
            raise click.ClickException(
                f"Could not access Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        except Exception as exc:
            raise click.ClickException(
                f"Could not access Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        self._sandboxes[sandbox_id] = handle
        return handle

    def prepare(self) -> None:
        """Verify the SDK, image, workspace, and control credentials."""
        _ensure_sdk()
        self._resolve_image()
        from blaxel.core.authentication import get_credentials

        credentials = get_credentials()
        if (
            credentials is None
            or not credentials.workspace
            or not (
                credentials.api_key or credentials.client_credentials or credentials.device_code
            )
        ):
            raise click.ClickException(
                "No complete Blaxel credentials were found. Set BL_WORKSPACE and "
                "BL_API_KEY, or install the Blaxel CLI and run `bl login` for a "
                "workspace."
            )
        for name in (
            "api_key",
            "client_credentials",
            "refresh_token",
            "access_token",
            "device_code",
        ):
            if value := getattr(credentials, name, None):
                self._secret_values.add(str(value))

    def provision(self, name: str) -> str:
        """Create a sandbox from the configured Blaxel host image."""
        _ensure_sdk()
        from blaxel.core import SyncSandboxInstance
        from blaxel.core.sandbox import SandboxAPIError

        image = self._resolve_image()
        create_attempt = uuid.uuid4().hex
        payload: dict[str, object] = {
            "name": name,
            "image": image,
            "memory": self._memory_mb or _DEFAULT_MEMORY_MIB,
            "labels": {
                "managed-by": "omnigent",
                _CREATE_ATTEMPT_LABEL: create_attempt,
            },
            "ttl": self._ttl or _DEFAULT_TTL,
        }
        env = self._resolve_sandbox_env()
        region = self._resolve_region()
        if env:
            payload["envs"] = env
        if region:
            payload["region"] = region
        click.echo(f"▸ Creating Blaxel sandbox '{name}'")
        handle: SyncSandboxInstance | None = None
        sandbox_id = name
        try:
            handle = SyncSandboxInstance.create(payload)
            sandbox_id = str(handle.metadata.name or name)
            _wait_for_sandbox_api(handle)
        except Exception as exc:
            cleanup_failure = ""
            status_code = exc.status_code if isinstance(exc, SandboxAPIError) else None
            ambiguous_create = status_code is None or status_code >= 500
            try:
                if handle is not None:
                    self.terminate(sandbox_id)
                elif ambiguous_create:
                    # A transport/5xx failure can commit remotely and lose the
                    # response. Resolve the deterministic name, but delete only
                    # a resource carrying this attempt's nonce: a fixed CLI name
                    # may already belong to an earlier invocation.
                    candidate = SyncSandboxInstance.get(name)
                    if _sandbox_label(candidate, _CREATE_ATTEMPT_LABEL) == create_attempt:
                        self.terminate(name)
            except SandboxAPIError as cleanup_exc:
                if cleanup_exc.status_code != 404:
                    cleanup_failure = (
                        f" Cleanup also failed for sandbox '{sandbox_id}': "
                        f"{_safe_provider_error(cleanup_exc, tuple(self._secret_values))}"
                    )
            except Exception as cleanup_exc:
                cleanup_failure = (
                    f" Cleanup also failed for sandbox '{sandbox_id}': "
                    f"{_safe_provider_error(cleanup_exc, tuple(self._secret_values))}"
                )
            raise click.ClickException(
                "Blaxel sandbox creation failed: "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}."
                f"{cleanup_failure}"
            ) from exc
        self._sandboxes[sandbox_id] = handle
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """Validate an existing sandbox and wake its sandbox API."""
        click.echo(f"▸ Reusing existing Blaxel sandbox '{sandbox_id}'")
        try:
            _wait_for_sandbox_api(self._resolve(sandbox_id))
        except click.ClickException:
            raise
        except Exception as exc:
            raise click.ClickException(
                f"Could not attach to Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """Explain that the host process owns Blaxel keep-alive."""
        del sandbox_id
        click.echo("  → the Omnigent host process keeps the Blaxel sandbox active")

    @staticmethod
    def _process_identifier(process: object, fallback_name: str) -> str:
        return str(
            getattr(process, "pid", None) or getattr(process, "name", None) or fallback_name
        )

    def _start_process(
        self,
        sandbox_id: str,
        command: str,
        *,
        name: str,
        env: dict[str, str] | None = None,
        keep_alive: bool = False,
        timeout_s: int | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
        max_output_bytes: int = _MAX_PROCESS_OUTPUT_BYTES,
    ) -> tuple[SyncSandboxInstance, object, str, _BlaxelLogStream | None]:
        handle = self._resolve(sandbox_id)
        request: dict[str, object] = {
            "name": name,
            "command": command,
            "wait_for_completion": False,
        }
        if env:
            request["env"] = env
        if timeout_s is not None:
            request["timeout"] = timeout_s
        if keep_alive:
            request["keep_alive"] = True
            request["timeout"] = 0
        try:
            process = handle.process.exec(request)
        except Exception as exc:
            raise click.ClickException(
                f"Remote command could not start on Blaxel sandbox "
                f"'{sandbox_id}': "
                f"{_safe_provider_error(exc, (*self._secret_values, *(env or {}).values()))}"
            ) from exc
        identifier = self._process_identifier(process, name)
        log_stream: _BlaxelLogStream | None = None
        if on_stdout is not None or on_stderr is not None:
            stdout_callback = on_stdout or (lambda _text: None)
            stderr_callback = on_stderr or (lambda _text: None)

            def _kill_on_stream_error(_exc: Exception) -> str | None:
                return self._kill_process(handle, identifier)

            try:
                log_stream = _open_process_log_stream(
                    handle.process,
                    identifier,
                    stdout_callback,
                    stderr_callback,
                    max_output_bytes=max_output_bytes,
                    on_error=_kill_on_stream_error,
                )
            except Exception as exc:
                kill_failure = self._kill_process(handle, identifier)
                detail = f" Remote kill also failed: {kill_failure}." if kill_failure else ""
                raise click.ClickException(
                    "Could not open the Blaxel process output stream; the remote "
                    "process was killed: "
                    f"{_safe_provider_error(exc, tuple(self._secret_values))}.{detail}"
                ) from exc
        return handle, process, identifier, log_stream

    def _kill_process(self, handle: SyncSandboxInstance, identifier: str) -> str | None:
        try:
            handle.process.kill(identifier)
        except Exception as exc:
            return _safe_provider_error(exc, tuple(self._secret_values))
        return None

    def _wait_for_process(
        self,
        handle: SyncSandboxInstance,
        identifier: str,
        log_stream: _BlaxelLogStream,
        *,
        timeout_s: float | None,
    ) -> object:
        deadline = time.monotonic() + timeout_s if timeout_s is not None else None
        try:
            while not log_stream.done:
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        assert timeout_s is not None
                        kill_failure = self._kill_process(handle, identifier)
                        detail = (
                            f" Remote kill also failed: {kill_failure}." if kill_failure else ""
                        )
                        raise click.ClickException(
                            f"Remote command timed out after {int(timeout_s)} seconds and was "
                            f"killed.{detail}"
                        )
                    log_stream.wait(min(_STREAM_POLL_INTERVAL_S, remaining))
                else:
                    log_stream.wait(_STREAM_POLL_INTERVAL_S)
            if error := log_stream.error:
                if log_stream.error_cleanup_attempted:
                    kill_failure = log_stream.error_cleanup_failure
                else:
                    kill_failure = self._kill_process(handle, identifier)
                detail = f" Remote kill also failed: {kill_failure}." if kill_failure else ""
                raise click.ClickException(
                    "Blaxel process output stream failed; the remote process was killed: "
                    f"{_safe_provider_error(error, tuple(self._secret_values))}.{detail}"
                ) from error
            try:
                current = handle.process.get(identifier)
            except Exception as exc:
                kill_failure = self._kill_process(handle, identifier)
                detail = f" Remote kill also failed: {kill_failure}." if kill_failure else ""
                raise click.ClickException(
                    "Could not read final Blaxel process state; the remote process was killed: "
                    f"{_safe_provider_error(exc, tuple(self._secret_values))}.{detail}"
                ) from exc
            status = _status_value(getattr(current, "status", ""))
            if status not in _TERMINAL_PROCESS_STATUSES:
                kill_failure = self._kill_process(handle, identifier)
                detail = f" Remote kill also failed: {kill_failure}." if kill_failure else ""
                raise click.ClickException(
                    "Blaxel process output ended before the command completed; the remote "
                    f"process was killed.{detail}"
                )
            return current
        except KeyboardInterrupt:
            click.echo("\n  → interrupted; stopping the remote process")
            if kill_failure := self._kill_process(handle, identifier):
                click.echo(f"  ! remote process cleanup failed: {kill_failure}", err=True)
            raise
        finally:
            log_stream.close()

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run a bounded command and capture stdout and stderr."""
        name = f"omni-command-{uuid.uuid4().hex[:12]}"

        def _discard_progress(_text: str) -> None:
            pass

        handle, _process, identifier, log_stream = self._start_process(
            sandbox_id,
            command,
            name=name,
            timeout_s=_COMMAND_TIMEOUT_S,
            on_stdout=_discard_progress,
            on_stderr=_discard_progress,
        )
        assert log_stream is not None
        current = self._wait_for_process(
            handle,
            identifier,
            log_stream,
            timeout_s=_COMMAND_TIMEOUT_S,
        )
        # The stream is the primary cap: its parser kills the process on
        # overflow. The final state is re-read only for the authoritative
        # stdout/stderr split, which the stream cannot recover for writes with
        # embedded newlines; the size check below is a backstop.
        stdout = str(getattr(current, "stdout", "") or "")
        stderr = str(getattr(current, "stderr", "") or "")
        output_bytes = _bounded_utf8_size(
            (stdout, stderr),
            _MAX_PROCESS_OUTPUT_BYTES,
        )
        if output_bytes > _MAX_PROCESS_OUTPUT_BYTES:
            raise click.ClickException(
                f"Remote command output exceeded {_MAX_PROCESS_OUTPUT_BYTES} bytes."
            )
        for output in (stdout, stderr):
            if output:
                click.echo(output, nl=False)
        status = _status_value(getattr(current, "status", ""))
        raw_exit_code = getattr(current, "exit_code", None)
        returncode = int(
            raw_exit_code if raw_exit_code is not None else (0 if status == "completed" else 1)
        )
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on Blaxel sandbox '{sandbox_id}' (exit {returncode})."
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def run_background(
        self,
        sandbox_id: str,
        command: str,
        *,
        log_path: str = "/tmp/omnigent-host.log",
    ) -> RemoteCommandResult:
        """Start a supervised host kept alive within the sandbox's TTL."""
        env, clean_command = _split_env_prefix(command)
        supervised = supervise_host_command(clean_command)
        remote = f"exec sh -c {shlex.quote(supervised)} > {shlex.quote(log_path)} 2>&1"
        name = f"omnigent-host-{uuid.uuid4().hex[:12]}"
        _handle, process, _identifier, _log_stream = self._start_process(
            sandbox_id,
            remote,
            name=name,
            env=env,
            keep_alive=True,
        )
        status = _status_value(getattr(process, "status", ""))
        if status in _TERMINAL_PROCESS_STATUSES:
            raise click.ClickException(
                f"Omnigent host process failed to start on Blaxel sandbox '{sandbox_id}'."
            )
        return RemoteCommandResult(returncode=0, stdout="launched\n", stderr="")

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Copy one binary-safe file through the Blaxel filesystem API."""
        try:
            self._resolve(sandbox_id).fs.write_binary(remote_path, local_path.read_bytes())
        except Exception as exc:
            raise click.ClickException(
                f"Could not copy a file to Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Spawn a command and stream its combined output line by line.

        Blaxel's process API exposes stdout and stderr on one bounded HTTP
        stream and allocates no PTY, so the *pty* flag is unused. Output is
        combined either way (mirrors the E2B and Islo launchers).

        :param sandbox_id: Target sandbox.
        :param command: Shell command to execute remotely.
        :param pty: Accepted for the contract; unused (see above).
        :returns: Handle streaming the process's combined output.
        :raises click.ClickException: When the command cannot be started.
        """
        del pty  # unused (see docstring)
        sink = _LineSink()
        name = f"omni-stream-{uuid.uuid4().hex[:12]}"
        remote = f"TERM=xterm-256color exec sh -c {shlex.quote(command)}"
        handle, _process, identifier, log_stream = self._start_process(
            sandbox_id,
            remote,
            name=name,
            # Blaxel caps a normal exec; the OAuth login this backs runs longer.
            keep_alive=True,
            on_stdout=sink.feed_chunk,
            on_stderr=sink.feed_chunk,
        )
        assert log_stream is not None
        return _BlaxelRemoteProcess(self, handle, identifier, sink, log_stream)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Stream a foreground command and kill it on local cancellation."""
        name = f"omni-foreground-{uuid.uuid4().hex[:12]}"
        remote = f"TERM=xterm-256color exec sh -c {shlex.quote(command)}"

        def _echo_chunk(text: str) -> None:
            click.echo(text, nl=False)

        handle, _process, identifier, log_stream = self._start_process(
            sandbox_id,
            remote,
            name=name,
            keep_alive=True,
            on_stdout=_echo_chunk,
            on_stderr=_echo_chunk,
        )
        assert log_stream is not None
        current = self._wait_for_process(
            handle,
            identifier,
            log_stream,
            timeout_s=None,
        )
        status = _status_value(getattr(current, "status", ""))
        raw_exit_code = getattr(current, "exit_code", None)
        return int(
            raw_exit_code if raw_exit_code is not None else (0 if status == "completed" else 1)
        )

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Overlay local wheels onto the prebuilt Omnigent host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """Delete a sandbox and purge its terminal tombstone."""
        _ensure_sdk()
        from blaxel.core import SyncSandboxInstance
        from blaxel.core.sandbox import SandboxAPIError

        self._sandboxes.pop(sandbox_id, None)
        try:
            # The SDK installs delete through a runtime descriptor.
            deleted = SyncSandboxInstance.delete(sandbox_id)  # type: ignore[attr-defined]
        except SandboxAPIError as exc:
            if exc.status_code == 404:
                return
            raise click.ClickException(
                f"Could not delete Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        except Exception as exc:
            raise click.ClickException(
                f"Could not delete Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc

        purge_attempted = False
        deadline = time.monotonic() + _DELETE_TIMEOUT_S
        try:
            while True:
                status = _status_value(getattr(deleted, "status", ""))
                if status == "terminated" and not purge_attempted:
                    deleted = SyncSandboxInstance.delete(  # type: ignore[attr-defined]
                        sandbox_id
                    )
                    purge_attempted = True
                    continue
                try:
                    deleted = SyncSandboxInstance.get(sandbox_id)
                except SandboxAPIError as exc:
                    if exc.status_code == 404:
                        return
                    raise
                if time.monotonic() >= deadline:
                    raise click.ClickException(
                        f"Blaxel sandbox '{sandbox_id}' did not disappear within "
                        f"{_DELETE_TIMEOUT_S} seconds; last status: "
                        f"{_status_value(getattr(deleted, 'status', 'unknown')) or 'unknown'}."
                    )
                time.sleep(_PROCESS_POLL_INTERVAL_S)
        except click.ClickException:
            raise
        except SandboxAPIError as exc:
            if exc.status_code == 404:
                return
            raise click.ClickException(
                f"Could not finish deleting Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        except Exception as exc:
            raise click.ClickException(
                f"Could not finish deleting Blaxel sandbox '{sandbox_id}': "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        finally:
            self._sandboxes.pop(sandbox_id, None)

    def is_running(self, sandbox_id: str) -> bool | None:
        """Report whether the sandbox record is non-terminal."""
        self._sandboxes.pop(sandbox_id, None)
        try:
            handle = self._resolve(sandbox_id)
        except _BlaxelSandboxNotFound:
            return False
        status = _status_value(handle.status)
        if status in _RUNNING_SANDBOX_STATUSES:
            return True
        if status in _STOPPED_SANDBOX_STATUSES:
            return False
        return None
