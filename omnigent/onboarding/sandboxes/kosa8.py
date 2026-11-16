"""
kosa8 sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for
`kosa8 <https://kosa8.com>`_ Cloud sandboxes. A kosa8 sandbox is a microVM:
its own kernel is the isolation boundary, it boots straight from an OCI image,
and it can sleep (memory, processes and disk saved, RAM freed) and wake in
place under the same id.

The launcher talks to kosa8's REST API (``https://api.kosa8.com``) with the
``httpx`` client Omnigent already depends on, so the provider needs no extra
and no SDK.

Notes that shape this launcher:

- **The host image is the VM.** Sandboxes boot the official prebaked host
  image (:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`)
  unless overridden, so the host starts without an install step.
- **Persistent.** Sandboxes are created ``persistent``: every command runs in
  one long-lived container, so ``$HOME``, the injected host config and the
  backgrounded ``omnigent host`` last between commands.
- **Env stays on the host.** The passthrough env (``sandbox.kosa8.env``) is
  set on the sandbox at create. kosa8 keeps the values on the sandbox's host,
  gives them to every command, and never logs or returns them.
- **Streamed exec.** Every command runs over kosa8's streamed exec, which is
  not held to the 90 s limit of a buffered exec, so clones and installs run
  to completion.
- **Sleep and wake.** :meth:`Kosa8SandboxLauncher.stop` sleeps the sandbox and
  :meth:`Kosa8SandboxLauncher.resume` wakes it. A woken sandbox still runs its
  old ``omnigent host`` on a launch token the server has revoked, so
  :meth:`Kosa8SandboxLauncher.start_host` stops it before starting a fresh one.
- **No local port forward.** kosa8 publishes sandbox ports outward (preview
  URLs) but has no local-to-sandbox path for the App OAuth callback, so the
  CLI skips that step, as it does for Modal, Daytona and Islo.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import secrets
import shlex
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxGoneError,
    SandboxLauncher,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
    host_image_wheel_install_command,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxCapabilities

API_KEY_ENV_VAR: str = "KOSA8_API_KEY"
"""kosa8 API key read from the server/CLI process environment."""

API_URL_ENV_VAR: str = "KOSA8_API_URL"
"""Optional kosa8 API base URL override. Defaults to :data:`DEFAULT_API_URL`."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_KOSA8_HOST_IMAGE"
"""Environment variable overriding :data:`DEFAULT_HOST_IMAGE` for kosa8 sandboxes."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_KOSA8_SANDBOX_ENV"
"""Comma-separated server-process environment variable names set on created sandboxes."""

DEFAULT_API_URL: str = "https://api.kosa8.com"

MANAGED_TOKEN_TTL_S: int = 7 * 24 * 3600
"""Launch-token lifetime. kosa8 sandboxes have no lifetime cap (they live until
deleted, asleep or awake), so the bound is policy: 7 days, as for Daytona."""

SUPPORTED_SIZES: tuple[str, ...] = ("2x4", "4x8", "8x16", "16x32")
"""kosa8 sandbox sizes, vCPU x GiB."""

DEFAULT_LABELS: dict[str, str] = {"created-by": "omnigent"}
"""Labels on every sandbox, so ``GET /v1/sandboxes?label=created-by=omnigent``
finds them."""

_MAX_LABELS = 16
_MAX_ENV = 64
_LABEL_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,62}$")
_LABEL_VALUE_MAX_LEN = 256
_SANDBOX_NAME_MAX_LEN = 64
_SNAPSHOT_RE = re.compile(r"^snap_[A-Za-z0-9]{8,40}$")
_REQUEST_TIMEOUT_S = 30.0
# A streamed exec sends a heartbeat frame every 15 s while the command is quiet.
_STREAM_READ_TIMEOUT_S = 120.0
_BOOT_TIMEOUT_S = 600.0
_BOOT_POLL_S = 2.0
_EXEC_READY_TIMEOUT_S = 60.0
_EXEC_READY_POLL_S = 1.0
# The files API takes at most 8 MiB per request; larger files go in parts.
_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024 - 64 * 1024
_RUNNING_STATES = frozenset({"running"})
_ASLEEP_STATES = frozenset({"sleeping", "paused"})
_BOOTING_STATES = frozenset({"creating"})
_GONE_STATES = frozenset({"deleted", "stopped", "host_removed", "host_lost"})
_STOP_PRESERVED_HOST_DAEMON_COMMAND = (
    "pkill -TERM -f '[o]mnigent host' 2>/dev/null; sleep 0.5; "
    "pkill -KILL -f '[o]mnigent host' 2>/dev/null; true"
)
_INSTALL_HINT = "Create an API key at https://app.kosa8.com and set KOSA8_API_KEY."


class _Kosa8APIError(RuntimeError):
    """A kosa8 API call failed; the message is safe to show."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


class _Kosa8GoneError(_Kosa8APIError):
    """kosa8 definitively reports the sandbox as absent."""


def validate_labels(labels: dict[str, str]) -> dict[str, str]:
    """
    Check labels against kosa8's rules.

    :param labels: Labels to set, e.g. ``{"team": "agents"}``.
    :returns: The same labels.
    :raises ValueError: When a key or value breaks kosa8's rules or there are
        too many (``created-by`` counts toward the 16 kosa8 allows).
    """
    if not isinstance(labels, dict):
        raise ValueError("labels must be a mapping of key to value")
    if len({**DEFAULT_LABELS, **labels}) > _MAX_LABELS:
        raise ValueError(f"at most {_MAX_LABELS} labels, created-by included")
    for key, value in labels.items():
        if not isinstance(key, str) or not _LABEL_KEY_RE.match(key):
            raise ValueError(
                f"label key {key!r} must be 1-63 letters, digits, '.', '_', '/' or '-', "
                "starting with a letter or digit"
            )
        if (
            not isinstance(value, str)
            or len(value) > _LABEL_VALUE_MAX_LEN
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in value)
        ):
            raise ValueError(
                f"label {key!r} must be a string of at most {_LABEL_VALUE_MAX_LEN} "
                "characters without control characters"
            )
    return labels


def sandbox_name_for(label: str) -> str:
    """
    Build a kosa8 sandbox name from a human label.

    :param label: The launcher's label, e.g. ``"managed-a1b2c3d4"``.
    :returns: ``omnigent-<label>-<6 hex>``, within kosa8's 64-character
        ``[A-Za-z0-9._-]`` rule, e.g. ``"omnigent-managed-a1b2c3d4-3f9c0a"``.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    suffix = secrets.token_hex(3)
    room = _SANDBOX_NAME_MAX_LEN - len("omnigent--") - len(suffix)
    return f"omnigent-{base[:room].rstrip('-')}-{suffix}"


class _Kosa8Client:
    """Small synchronous client for the kosa8 Cloud REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}", "User-Agent": "omnigent"},
            timeout=_REQUEST_TIMEOUT_S,
            transport=transport,
        )

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise _Kosa8APIError(f"could not reach kosa8: {exc}") from exc
        if response.is_error:
            raise _error_for(response)
        if not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        return _sandbox_of(self._request("POST", "/v1/sandboxes", json=payload))

    def get_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return _sandbox_of(self._request("GET", f"/v1/sandboxes/{_url(sandbox_id)}"))

    def delete_sandbox(self, sandbox_id: str) -> None:
        self._request("DELETE", f"/v1/sandboxes/{_url(sandbox_id)}")

    def sleep_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return _sandbox_of(self._request("POST", f"/v1/sandboxes/{_url(sandbox_id)}/sleep"))

    def wake_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return _sandbox_of(self._request("POST", f"/v1/sandboxes/{_url(sandbox_id)}/wake"))

    def update_sandbox(self, sandbox_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return _sandbox_of(
            self._request("PATCH", f"/v1/sandboxes/{_url(sandbox_id)}", json=payload)
        )

    def write_file(self, sandbox_id: str, path: str, data: bytes) -> None:
        self._request(
            "PUT",
            f"/v1/sandboxes/{_url(sandbox_id)}/files",
            params={"path": path},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )

    def exec_stream(
        self,
        sandbox_id: str,
        argv: list[str],
        *,
        on_stdout: Callable[[str], None],
        on_stderr: Callable[[str], None],
    ) -> int:
        """
        Run *argv* over kosa8's streamed exec and return its exit code.

        :raises _Kosa8APIError: When the exec cannot start, or the stream ends
            with an error frame or without a final frame.
        """
        timeout = httpx.Timeout(_REQUEST_TIMEOUT_S, read=_STREAM_READ_TIMEOUT_S)
        try:
            with self._http.stream(
                "POST",
                f"/v1/sandboxes/{_url(sandbox_id)}/exec",
                json={"cmd": argv, "stream": True},
                timeout=timeout,
            ) as response:
                if response.is_error:
                    response.read()
                    raise _error_for(response)
                return _parse_exec_frames(
                    response.iter_lines(), on_stdout=on_stdout, on_stderr=on_stderr
                )
        except httpx.HTTPError as exc:
            raise _Kosa8APIError(f"exec stream to kosa8 failed: {exc}") from exc


def _parse_exec_frames(
    lines: Iterator[str],
    *,
    on_stdout: Callable[[str], None],
    on_stderr: Callable[[str], None],
) -> int:
    """
    Read kosa8's NDJSON exec frames and return the exit code.

    Frames are ``{"stream": "stdout"|"stderr", "data": <base64>}`` (an empty
    one with ``heartbeat: true`` while the command is quiet), then
    ``{"done": true, "exit": <code>}`` or ``{"done": true, "error": <text>}``.
    """
    decoders = {"stdout": _Utf8Stream(on_stdout), "stderr": _Utf8Stream(on_stderr)}
    for raw in lines:
        if not raw.strip():
            continue
        try:
            frame = json.loads(raw)
        except ValueError as exc:
            raise _Kosa8APIError(
                f"kosa8 exec stream sent an unreadable frame: {raw[:200]!r}"
            ) from exc
        if frame.get("done"):
            for decoder in decoders.values():
                decoder.flush()
            if frame.get("error"):
                code = frame.get("code")
                raise _Kosa8APIError(
                    f"command did not finish: {frame['error']}", code=code if code else None
                )
            exit_code = frame.get("exit")
            return int(exit_code) if isinstance(exit_code, int) else 1
        decoder = decoders.get(str(frame.get("stream")))
        data = frame.get("data")
        if decoder is not None and isinstance(data, str) and data:
            decoder.feed(base64.b64decode(data))
    raise _Kosa8APIError("kosa8 exec stream ended without a final frame")


class _Utf8Stream:
    """Decode a byte stream as UTF-8 across frame boundaries."""

    def __init__(self, sink: Callable[[str], None]) -> None:
        import codecs

        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._sink = sink

    def feed(self, data: bytes) -> None:
        text = self._decoder.decode(data)
        if text:
            self._sink(text)

    def flush(self) -> None:
        text = self._decoder.decode(b"", final=True)
        if text:
            self._sink(text)


def _error_for(response: httpx.Response) -> _Kosa8APIError:
    """Build the error for a kosa8 error answer (``{"error": code, "message": text}``)."""
    code: str | None = None
    message = response.reason_phrase or "request failed"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = body.get("error") if isinstance(body.get("error"), str) else None
        if isinstance(body.get("message"), str):
            message = body["message"]
    text = f"{message} (HTTP {response.status_code}{f', {code}' if code else ''})"
    error_cls = _Kosa8GoneError if response.status_code == 404 else _Kosa8APIError
    return error_cls(text, status=response.status_code, code=code)


def _sandbox_of(data: dict[str, Any]) -> dict[str, Any]:
    sandbox = data.get("sandbox")
    if not isinstance(sandbox, dict) or not isinstance(sandbox.get("id"), str):
        raise _Kosa8APIError("kosa8 answered without a sandbox")
    return sandbox


def _url(value: str) -> str:
    return quote(value, safe="")


class _Kosa8RemoteProcess(RemoteProcess):
    """Thread-backed :class:`RemoteProcess` over kosa8's streamed exec."""

    def __init__(self, client: _Kosa8Client, sandbox_id: str, command: str) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._command = command
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._pending = ""
        self._returncode: int | None = None
        self._error: BaseException | None = None
        self._iterator = self._iter_lines()
        self._thread = threading.Thread(target=self._run, name="kosa8-remote-process", daemon=True)
        self._thread.start()

    @property
    def lines(self) -> Iterator[str]:
        """Iterator over combined stdout/stderr lines (the same one on every access)."""
        return self._iterator

    def _iter_lines(self) -> Iterator[str]:
        while True:
            item = self._lines.get()
            if item is None:
                return
            yield item

    def wait(self) -> int:
        """Block until the remote command finishes and return its exit code."""
        self._thread.join()
        if self._error is not None:
            raise click.ClickException(str(self._error)) from self._error
        return self._returncode if self._returncode is not None else 1

    def close(self) -> None:
        """Best effort: a streamed kosa8 exec has no kill handle of its own."""
        return

    def _run(self) -> None:
        try:
            self._returncode = self._client.exec_stream(
                self._sandbox_id,
                ["bash", "-lc", self._command],
                on_stdout=self._enqueue,
                on_stderr=self._enqueue,
            )
        except BaseException as exc:
            self._error = exc
        finally:
            if self._pending:
                self._lines.put(f"{self._pending}\n")
            self._lines.put(None)

    def _enqueue(self, text: str) -> None:
        self._pending += text
        *complete, self._pending = self._pending.split("\n")
        for line in complete:
            self._lines.put(f"{line}\n")


class Kosa8SandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for kosa8 Cloud sandboxes, over kosa8's REST API.

    ``POST /v1/sandboxes`` and ``DELETE`` for lifecycle, streamed
    ``POST …/exec`` for commands (``bash -lc`` so login PATH applies),
    ``PUT …/files`` for file shipping, and ``…/sleep`` / ``…/wake`` for stop
    and resume.
    """

    provider: ClassVar[str] = "kosa8"
    supports_local_port_forward: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    @property
    def capabilities(self) -> SandboxCapabilities:
        # start_host only adds a step before the base implementation, which
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
            snapshot_restore=True,
            git_clone_options=True,
        )

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        size: str | None = None,
        region: str | None = None,
        snapshot: str | None = None,
        base_url: str | None = None,
        labels: dict[str, str] | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: OCI image the sandbox boots, e.g.
            ``"ghcr.io/me/omnigent-host:latest"``. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR`, then :data:`DEFAULT_HOST_IMAGE`.
        :param env: Names of server-process environment variables set on every
            sandbox, e.g. ``["ANTHROPIC_API_KEY", "GIT_TOKEN"]``. ``None``
            resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`, then none.
        :param size: kosa8 size, one of :data:`SUPPORTED_SIZES`. ``None`` boots
            the image at 2 vCPU and 4 GiB.
        :param region: kosa8 region, e.g. ``"eu-hel1"`` or ``"eu"``. ``None``
            uses the org's default region.
        :param snapshot: kosa8 snapshot (``snap_…``) to create sandboxes from
            instead of booting the image. The snapshot's image, size and
            region apply.
        :param base_url: kosa8 API URL. ``None`` resolves
            :data:`API_URL_ENV_VAR`, then :data:`DEFAULT_API_URL`.
        :param labels: kosa8 labels added to every sandbox, on top of
            :data:`DEFAULT_LABELS`.
        :raises ValueError: When *size* is not a kosa8 size, *snapshot* is
            malformed or combined with *image*, *size* or *region*, or a
            label breaks kosa8's rules.
        """
        if size is not None and size not in SUPPORTED_SIZES:
            raise ValueError(f"size must be one of {', '.join(SUPPORTED_SIZES)} (got {size!r})")
        if snapshot is not None:
            if not _SNAPSHOT_RE.match(snapshot):
                raise ValueError(
                    f"snapshot must be a kosa8 snapshot id (snap_…), got {snapshot!r}"
                )
            clashing = [
                key
                for key, value in (("image", image), ("size", size), ("region", region))
                if value is not None
            ]
            if clashing:
                raise ValueError(
                    f"snapshot cannot be combined with {', '.join(clashing)}: a sandbox made "
                    "from a snapshot keeps its image, size and region"
                )
        self.image = image
        self.env = tuple(env) if env is not None else None
        self.size = size
        self.region = region
        self.snapshot = snapshot
        self.base_url = base_url
        self.labels = {**DEFAULT_LABELS, **validate_labels(labels or {})}
        self._transport = _transport
        self._client: _Kosa8Client | None = None

    # ── client and config ──────────────────────────────

    def _kosa8(self) -> _Kosa8Client:
        if self._client is None:
            api_key = os.environ.get(API_KEY_ENV_VAR)
            if not api_key:
                raise click.ClickException(f"No kosa8 credentials found. {_INSTALL_HINT}")
            base_url = self.base_url or os.environ.get(API_URL_ENV_VAR) or DEFAULT_API_URL
            self._client = _Kosa8Client(
                base_url=base_url, api_key=api_key, transport=self._transport
            )
        return self._client

    def _resolved_image(self) -> str:
        return self.image or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE

    def _resolve_sandbox_env(self) -> dict[str, str]:
        if self.env is not None:
            names: Sequence[str] = self.env
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        if len(names) > _MAX_ENV:
            raise click.ClickException(
                f"kosa8 sandboxes take at most {_MAX_ENV} env vars; {len(names)} are configured"
            )
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.kosa8.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    # ── lifecycle ──────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: a kosa8 API key must be set.

        :raises click.ClickException: When ``KOSA8_API_KEY`` is not set.
        """
        if not os.environ.get(API_KEY_ENV_VAR):
            raise click.ClickException(f"No kosa8 credentials found. {_INSTALL_HINT}")

    def provision(self, name: str) -> str:
        """
        Create a sandbox, wait until it runs and accepts commands.

        Idle sleep is off (``sleep_after_s: 0``); the managed-session machinery
        owns the sandbox's lifetime and sleeps it through :meth:`stop`. A
        sandbox that fails to boot is deleted before the error propagates,
        since no caller holds its id yet.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :returns: The kosa8 sandbox id, e.g. ``"sbx_4Qm2…"``.
        :raises click.ClickException: If creation or the boot fails.
        """
        client = self._kosa8()
        sandbox_name = sandbox_name_for(name)
        payload: dict[str, Any] = {
            "name": sandbox_name,
            "labels": self.labels,
            "sleep_after_s": 0,
        }
        env = self._resolve_sandbox_env()
        if env:
            payload["env"] = env
        if self.snapshot is not None:
            payload["snapshot"] = self.snapshot
            source = f"snapshot {self.snapshot}"
        else:
            image = self._resolved_image()
            payload.update({"image": image, "persistent": True})
            if self.size is not None:
                payload["size"] = self.size
            if self.region is not None:
                payload["region"] = self.region
            source = image
        click.echo(f"▸ Creating kosa8 sandbox '{sandbox_name}' from {source}")
        try:
            sandbox = client.create_sandbox(payload)
        except _Kosa8APIError as exc:
            raise click.ClickException(f"kosa8 sandbox creation failed: {exc}") from exc
        sandbox_id = str(sandbox["id"])
        try:
            self._wait_until_running(sandbox)
            self._wait_for_exec(sandbox_id)
        except BaseException as exc:
            self._delete_quietly(sandbox_id)
            if isinstance(exc, _Kosa8APIError):
                raise click.ClickException(
                    f"kosa8 sandbox '{sandbox_name}' did not start: {exc}"
                ) from exc
            raise
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def _wait_until_running(self, sandbox: dict[str, Any]) -> None:
        """
        Poll a booting sandbox until it runs.

        :param sandbox: The sandbox as last read.
        :raises _Kosa8APIError: When it ends in ``error`` or a gone state, or
            is still booting after :data:`_BOOT_TIMEOUT_S`.
        """
        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        while True:
            state = str(sandbox.get("state") or "")
            if state in _RUNNING_STATES:
                return
            if state == "error" or state in _GONE_STATES:
                reason = sandbox.get("last_error") or "no reason given"
                raise _Kosa8APIError(f"sandbox is {state}: {reason}")
            if time.monotonic() >= deadline:
                raise _Kosa8APIError(
                    f"sandbox still {state or 'unknown'} after {int(_BOOT_TIMEOUT_S)}s"
                )
            time.sleep(_BOOT_POLL_S)
            sandbox = self._kosa8().get_sandbox(str(sandbox["id"]))

    def _wait_for_exec(self, sandbox_id: str) -> None:
        """
        Run a no-op until the sandbox accepts commands.

        A sandbox booted from an image answers ``409 starting`` for a moment
        after it reads as running.

        :raises _Kosa8APIError: When exec still fails after
            :data:`_EXEC_READY_TIMEOUT_S`, or fails for a reason waiting
            cannot fix.
        """
        deadline = time.monotonic() + _EXEC_READY_TIMEOUT_S
        while True:
            try:
                self._kosa8().exec_stream(
                    sandbox_id, ["true"], on_stdout=lambda _: None, on_stderr=lambda _: None
                )
                return
            except _Kosa8APIError as exc:
                if exc.status not in (409, 502, 503) or time.monotonic() >= deadline:
                    raise
                time.sleep(_EXEC_READY_POLL_S)

    def _delete_quietly(self, sandbox_id: str) -> None:
        try:
            self._kosa8().delete_sandbox(sandbox_id)
        except _Kosa8APIError as exc:
            click.echo(
                f"  → warning: could not delete kosa8 sandbox '{sandbox_id}' after a "
                f"failed launch: {exc}",
                err=True,
            )

    def _wake(self, sandbox_id: str) -> None:
        """
        Bring a sandbox to ``running`` in place.

        :raises _Kosa8GoneError: When the sandbox no longer exists.
        :raises _Kosa8APIError: When it cannot be brought back.
        """
        client = self._kosa8()
        sandbox = client.get_sandbox(sandbox_id)
        state = str(sandbox.get("state") or "")
        if state in _GONE_STATES:
            raise _Kosa8GoneError(f"kosa8 sandbox '{sandbox_id}' is {state}")
        if state in _ASLEEP_STATES:
            click.echo(f"  → waking sandbox (state: {state})")
            sandbox = client.wake_sandbox(sandbox_id)
        elif state not in _RUNNING_STATES and state not in _BOOTING_STATES:
            raise _Kosa8APIError(
                f"kosa8 sandbox '{sandbox_id}' is in state {state!r}; "
                "it cannot be resumed in place."
            )
        self._wait_until_running(sandbox)

    def attach(self, sandbox_id: str) -> None:
        """
        Validate access to an existing sandbox, waking it if it sleeps.

        :raises click.ClickException: When the sandbox does not exist or
            cannot be brought back.
        """
        click.echo(f"▸ Reusing existing kosa8 sandbox '{sandbox_id}'")
        try:
            self._wake(sandbox_id)
        except _Kosa8GoneError as exc:
            raise click.ClickException(
                f"kosa8 sandbox '{sandbox_id}' not found — it may have been deleted."
            ) from exc
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"Could not attach to kosa8 sandbox '{sandbox_id}': {exc}"
            ) from exc

    def resume(self, sandbox_id: str) -> None:
        """
        Wake a sleeping sandbox in place: memory, processes and disk come back.

        :raises SandboxGoneError: When the sandbox no longer exists.
        :raises click.ClickException: When the wake fails.
        """
        click.echo(f"▸ Resuming kosa8 sandbox '{sandbox_id}'")
        try:
            self._wake(sandbox_id)
        except _Kosa8GoneError as exc:
            raise SandboxGoneError(str(exc)) from exc
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"Could not resume kosa8 sandbox '{sandbox_id}': {exc}"
            ) from exc

    def stop(self, sandbox_id: str) -> None:
        """
        Sleep the sandbox: kosa8 saves memory, processes and disk and frees
        its RAM, so it bills snapshot storage only until :meth:`resume`.

        An already sleeping sandbox counts as stopped.

        :raises SandboxGoneError: When the sandbox no longer exists.
        :raises click.ClickException: When kosa8 rejects the sleep.
        """
        client = self._kosa8()
        try:
            state = str(client.get_sandbox(sandbox_id).get("state") or "")
            if state in _GONE_STATES:
                raise SandboxGoneError(f"kosa8 sandbox '{sandbox_id}' is {state}")
            if state in _ASLEEP_STATES:
                return
            click.echo(f"▸ Sleeping kosa8 sandbox '{sandbox_id}'")
            client.sleep_sandbox(sandbox_id)
        except _Kosa8GoneError as exc:
            raise SandboxGoneError(str(exc)) from exc
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"Could not stop kosa8 sandbox '{sandbox_id}': {exc}"
            ) from exc

    def is_running(self, sandbox_id: str) -> bool | None:
        """
        Return whether kosa8 reports the sandbox as running.

        :returns: ``True`` when running, ``False`` when asleep or gone,
            ``None`` while booting or when the status could not be read.
        """
        try:
            state = str(self._kosa8().get_sandbox(sandbox_id).get("state") or "")
        except _Kosa8GoneError:
            return False
        except _Kosa8APIError:
            return None
        if state in _RUNNING_STATES:
            return True
        if state in _BOOTING_STATES:
            return None
        return False

    def keep_alive(self, sandbox_id: str) -> bool:
        """
        Keep idle sleep off, so the host survives quiet periods.

        :returns: ``True`` when kosa8 accepted the setting, ``False`` when it
            was rejected (reported as a warning).
        """
        try:
            self._kosa8().update_sandbox(sandbox_id, {"sleep_after_s": 0})
        except _Kosa8APIError as exc:
            click.echo(
                f"  → warning: could not turn off idle sleep on kosa8 sandbox "
                f"'{sandbox_id}' ({exc}); it may sleep when idle.",
                err=True,
            )
            return False
        return True

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete the sandbox. A sandbox that no longer exists counts as deleted.

        :raises click.ClickException: When kosa8 rejects the delete.
        """
        try:
            self._kosa8().delete_sandbox(sandbox_id)
        except _Kosa8GoneError:
            pass
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"Could not delete kosa8 sandbox '{sandbox_id}': {exc}"
            ) from exc

    # ── host ───────────────────────────────────────────

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
        Stop any host a wake brought back, then start one with a fresh token.

        A woken sandbox resumes with its old ``omnigent host`` still running on
        a launch token the server has revoked.
        """
        self.run(sandbox_id, _STOP_PRESERVED_HOST_DAEMON_COMMAND, check=False)
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

    # ── transport ──────────────────────────────────────

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the sandbox and capture its output.

        :raises click.ClickException: If the command could not be executed,
            or *check* is ``True`` and it exited non-zero.
        """
        stdout: list[str] = []
        stderr: list[str] = []
        try:
            returncode = self._kosa8().exec_stream(
                sandbox_id,
                ["bash", "-lc", command],
                on_stdout=stdout.append,
                on_stderr=stderr.append,
            )
        except _Kosa8GoneError as exc:
            raise SandboxGoneError(
                f"kosa8 sandbox '{sandbox_id}' no longer exists: {exc}"
            ) from exc
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"Remote command failed to execute on kosa8 sandbox '{sandbox_id}': {exc}"
            ) from exc
        out, err = "".join(stdout), "".join(stderr)
        if out:
            click.echo(out, nl=False)
        if err:
            click.echo(err, nl=False, err=True)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on kosa8 sandbox '{sandbox_id}' "
                f"(exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=out, stderr=err)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """
        Copy a local file into the sandbox.

        The files API takes at most 8 MiB per request, so a larger file goes
        up in parts that a command then joins.

        :raises click.ClickException: If the transfer fails.
        """
        data = local_path.read_bytes()
        client = self._kosa8()
        try:
            if len(data) <= _UPLOAD_CHUNK_BYTES:
                client.write_file(sandbox_id, remote_path, data)
                return
            parts = []
            for index, start in enumerate(range(0, len(data), _UPLOAD_CHUNK_BYTES)):
                part = f"{remote_path}.part{index:04d}"
                client.write_file(sandbox_id, part, data[start : start + _UPLOAD_CHUNK_BYTES])
                parts.append(part)
        except _Kosa8APIError as exc:
            raise click.ClickException(
                f"File upload to kosa8 sandbox '{sandbox_id}' failed: {exc}"
            ) from exc
        quoted = " ".join(shlex.quote(part) for part in parts)
        self.run(
            sandbox_id,
            f"cat {quoted} > {shlex.quote(remote_path)} && rm -f {quoted}",
        )

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """
        Spawn a command in the sandbox and stream its combined output.

        kosa8's exec has no PTY, so *pty* is ignored.
        """
        del pty
        return _Kosa8RemoteProcess(self._kosa8(), sandbox_id, command)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """
        Run *command* in the sandbox, echoing its output until it exits;
        Ctrl-C kills the remote process and re-raises.

        The remote command records its pid first, so the interrupt path can
        signal it with a second exec.
        """
        run_dir, pidfile = foreground_pidfile()
        remote = f"{foreground_record_prefix(pidfile)}TERM=xterm-256color exec {command}"
        process = self.stream_exec(sandbox_id, remote)
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
        """Remote command that overlays shipped wheels onto the host image."""
        return host_image_wheel_install_command(remote_tgz_path)
