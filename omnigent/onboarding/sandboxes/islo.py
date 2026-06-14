"""
Islo sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Islo <https://islo.dev>`_ sandboxes. The integration talks to the
Islo HTTP API directly through ``httpx`` (already a base Omnigent
dependency), so there is no provider SDK extra to install.

Platform notes that shape this launcher:

- **API-key auth.** ``ISLO_API_KEY`` is exchanged for a short-lived
  session token via ``POST /auth/token``. The token is cached until
  shortly before expiry, mirroring Islo's Go SDK.
- **Prebaked host image.** Like Modal and Daytona, sandboxes boot from
  the official Omnigent host image unless overridden. That keeps
  server-managed launches fast.
- **No local port forwarding.** Islo can run commands and upload files
  through its API, but it does not provide a local-to-sandbox port
  forward for the in-sandbox App OAuth callback. The CLI therefore
  skips that auth step automatically, just as it does for Modal and
  Daytona.
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote, urlencode

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxLauncher,
    host_image_wheel_install_command,
)

API_BASE_URL_ENV_VAR: str = "ISLO_BASE_URL"
"""Optional Islo API base URL override. Defaults to
``https://api.islo.dev``."""

API_KEY_ENV_VAR: str = "ISLO_API_KEY"
"""Islo API key read from the server/CLI process environment."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_ISLO_HOST_IMAGE"
"""Environment variable overriding :data:`DEFAULT_HOST_IMAGE` for Islo
sandboxes."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_ISLO_SANDBOX_ENV"
"""Comma-separated server-process environment variable names injected
into created Islo sandboxes."""

_DEFAULT_BASE_URL = "https://api.islo.dev"
_TOKEN_REFRESH_MARGIN_S = 60.0
_SANDBOX_CPU = 2
_SANDBOX_MEMORY_MB = 4096
_REQUEST_TIMEOUT_S = 30.0


class _IsloAPIError(RuntimeError):
    """Provider-boundary error with a user-facing message."""


class _IsloClient:
    """Small synchronous Islo HTTP API client."""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = httpx.Client(timeout=_REQUEST_TIMEOUT_S)
        self._token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a sandbox and return the response object."""
        return self._request_json("POST", "/sandboxes/", json=payload)

    def get_sandbox(self, name: str) -> dict[str, Any]:
        """Fetch a sandbox by name."""
        return self._request_json("GET", f"/sandboxes/{_url_component(name)}")

    def delete_sandbox(self, name: str) -> None:
        """Delete a sandbox by name. Missing sandboxes are treated as gone."""
        try:
            self._request("DELETE", f"/sandboxes/{_url_component(name)}")
        except _IsloAPIError as exc:
            if "HTTP 404" not in str(exc):
                raise

    def upload_file(self, name: str, local_path: Path, remote_path: str) -> None:
        """Upload one file to an absolute path in the sandbox."""
        params = urlencode({"path": remote_path})
        endpoint = f"/sandboxes/{_url_component(name)}/files?{params}"
        with local_path.open("rb") as file_obj:
            files = {"file": (local_path.name, file_obj, "application/octet-stream")}
            self._request("POST", endpoint, files=files)

    def exec_stream(
        self,
        name: str,
        command: Sequence[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        on_stdout: Callable[[str], None] | None = None,
        on_stderr: Callable[[str], None] | None = None,
    ) -> int:
        """Execute a command and stream SSE stdout/stderr callbacks."""
        body: dict[str, Any] = {"command": list(command)}
        if workdir is not None:
            body["workdir"] = workdir
        if env:
            body["env"] = env
        headers = self._auth_headers()
        headers["Accept"] = "text/event-stream"
        url = self._url(f"/sandboxes/{_url_component(name)}/exec/stream")
        try:
            with self._client.stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=None,
            ) as response:
                if response.status_code >= 400:
                    raise self._response_error("POST", url, response)
                return _parse_exec_sse(
                    response.iter_lines(),
                    on_stdout=on_stdout,
                    on_stderr=on_stderr,
                )
        except httpx.HTTPError as exc:
            raise _IsloAPIError(f"islo exec stream failed: {exc}") from exc

    def _request_json(self, method: str, endpoint: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, endpoint, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise _IsloAPIError(f"islo {method} {endpoint} returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise _IsloAPIError(f"islo {method} {endpoint} returned a non-object response")
        return data

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> httpx.Response:
        url = self._url(endpoint)
        headers = kwargs.pop("headers", None) or {}
        headers = {**headers, **self._auth_headers()}
        try:
            response = self._client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise _IsloAPIError(f"islo {method} {endpoint} failed: {exc}") from exc
        if response.status_code >= 400:
            raise self._response_error(method, endpoint, response)
        return response

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._session_token()}"}

    def _session_token(self) -> str:
        now = time.time()
        if self._token is not None and now < self._token_expires_at:
            return self._token
        try:
            response = self._client.post(
                self._url("/auth/token"),
                json={"access_key": self._api_key},
                timeout=_REQUEST_TIMEOUT_S,
            )
        except httpx.HTTPError as exc:
            raise _IsloAPIError(f"islo token exchange failed: {exc}") from exc
        if response.status_code >= 400:
            raise self._response_error("POST", "/auth/token", response)
        try:
            data = response.json()
        except ValueError as exc:
            raise _IsloAPIError("islo token exchange returned invalid JSON") from exc
        token = data.get("session_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise _IsloAPIError("islo token exchange response missing session_token")
        max_age = data.get("cookie_max_age", 0) if isinstance(data, dict) else 0
        ttl = (
            max(float(max_age) - _TOKEN_REFRESH_MARGIN_S, 0.0)
            if isinstance(max_age, (int, float))
            else 0.0
        )
        self._token = token
        self._token_expires_at = now + ttl
        return token

    def _url(self, endpoint: str) -> str:
        return self._base_url + endpoint

    def _response_error(
        self, method: str, endpoint: str, response: httpx.Response
    ) -> _IsloAPIError:
        try:
            text = response.text
        except httpx.ResponseNotRead:
            text = response.read().decode("utf-8", errors="replace")
        snippet = text.strip()[:1024]
        detail = f": {snippet}" if snippet else ""
        return _IsloAPIError(
            f"islo {method} {endpoint} failed with HTTP {response.status_code}{detail}"
        )


class _IsloRemoteProcess(RemoteProcess):
    """Thread-backed :class:`RemoteProcess` over Islo exec streaming."""

    def __init__(self, client: _IsloClient, sandbox_id: str, command: str) -> None:
        self._client = client
        self._sandbox_id = sandbox_id
        self._command = command
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._returncode: int | None = None
        self._error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="islo-remote-process", daemon=True)
        self._thread.start()

    @property
    def lines(self) -> Iterator[str]:
        """Iterator over combined stdout/stderr lines."""
        while True:
            item = self._lines.get()
            if item is None:
                return
            yield item

    def wait(self) -> int:
        """Block until the remote exec finishes and return its exit code."""
        self._thread.join()
        if self._error is not None:
            raise click.ClickException(str(self._error)) from self._error
        return self._returncode if self._returncode is not None else 1

    def close(self) -> None:
        """Best-effort cleanup; Islo exec streams do not expose a kill handle."""
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
            self._lines.put(None)

    def _enqueue(self, text: str) -> None:
        for line in text.splitlines(keepends=True):
            self._lines.put(line)
        if text and not text.endswith(("\n", "\r")):
            self._lines.put("\n")


class IsloSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for Islo sandboxes.

    All primitives use Islo's HTTP API: sandbox create/delete for
    lifecycle, exec streaming for commands, and file upload for wheel
    shipping.
    """

    provider: ClassVar[str] = "islo"
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        base_url: str | None = None,
        gateway_profile: str | None = None,
        snapshot_name: str | None = None,
        workdir: str | None = None,
        vcpus: int | None = None,
        memory_mb: int | None = None,
        disk_gb: int | None = None,
    ) -> None:
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._base_url = base_url
        self._gateway_profile = gateway_profile
        self._snapshot_name = snapshot_name
        self._workdir = workdir
        self._vcpus = vcpus
        self._memory_mb = memory_mb
        self._disk_gb = disk_gb
        self._client: _IsloClient | None = None

    def prepare(self) -> None:
        """Verify Islo credentials are available."""
        if not os.environ.get(API_KEY_ENV_VAR):
            raise click.ClickException(
                "No Islo credentials found. Create an API key at "
                "https://islo.dev and set ISLO_API_KEY."
            )

    def provision(self, name: str) -> str:
        """Create a new Islo sandbox from the host image."""
        resolved_ref = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        sandbox_name = _new_sandbox_name(name)
        payload: dict[str, Any] = {
            "name": sandbox_name,
            "image": resolved_ref,
            "vcpus": self._vcpus or _SANDBOX_CPU,
            "memory_mb": self._memory_mb or _SANDBOX_MEMORY_MB,
            "init": {"type": "minimal"},
        }
        env_vars = self._resolve_sandbox_env()
        if env_vars:
            payload["env"] = env_vars
        if self._workdir:
            payload["workdir"] = self._workdir
        if self._gateway_profile:
            payload["gateway_profile"] = self._gateway_profile
        if self._snapshot_name:
            payload["snapshot_name"] = self._snapshot_name
        if self._disk_gb is not None:
            payload["disk_gb"] = self._disk_gb
        click.echo(f"▸ Creating Islo sandbox '{sandbox_name}' from {resolved_ref}")
        try:
            sandbox = self._islo().create_sandbox(payload)
        except _IsloAPIError as exc:
            raise click.ClickException(f"Islo sandbox creation failed: {exc}") from exc
        created_name = sandbox.get("name")
        if not isinstance(created_name, str) or not created_name:
            raise click.ClickException("Islo sandbox creation returned no sandbox name")
        click.echo(f"  → created {created_name}")
        return created_name

    def attach(self, sandbox_id: str) -> None:
        """Validate access to an existing Islo sandbox."""
        click.echo(f"▸ Reusing existing Islo sandbox '{sandbox_id}'")
        try:
            self._islo().get_sandbox(sandbox_id)
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Could not attach to Islo sandbox '{sandbox_id}': {exc}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """No local keep-alive setting is exposed by the Islo API."""
        click.echo(f"  → Islo sandbox '{sandbox_id}' remains active until deleted")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run a shell command in the sandbox and capture its output."""
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []

        def _stdout(text: str) -> None:
            stdout_chunks.append(text)
            if text:
                click.echo(text, nl=False)

        def _stderr(text: str) -> None:
            stderr_chunks.append(text)
            if text:
                click.echo(text, nl=False, err=True)

        try:
            returncode = self._islo().exec_stream(
                sandbox_id,
                ["bash", "-lc", command],
                on_stdout=_stdout,
                on_stderr=_stderr,
            )
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Remote command failed to execute on Islo sandbox '{sandbox_id}': {exc}"
            ) from exc
        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on Islo sandbox '{sandbox_id}' "
                f"(exit {returncode}): {command}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Copy a local file into the sandbox."""
        try:
            self._islo().upload_file(sandbox_id, local_path, remote_path)
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"File upload to Islo sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        """Spawn a command in the sandbox and stream combined output."""
        del pty
        return _IsloRemoteProcess(self._islo(), sandbox_id, command)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Run *command* in the sandbox, echoing output until it exits."""
        process = self.stream_exec(sandbox_id, f"TERM=xterm-256color exec {command}", pty=True)
        try:
            for line in process.lines:
                click.echo(line, nl=False)
            return process.wait()
        except KeyboardInterrupt:
            click.echo("\n  → detached; Islo exec streams do not expose a remote kill handle")
            raise

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Remote command that overlays shipped wheels onto the host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """Delete a sandbox, releasing its compute."""
        try:
            self._islo().delete_sandbox(sandbox_id)
        finally:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _islo(self) -> _IsloClient:
        if self._client is None:
            api_key = os.environ.get(API_KEY_ENV_VAR)
            if not api_key:
                raise click.ClickException(
                    "No Islo credentials found. Create an API key at "
                    "https://islo.dev and set ISLO_API_KEY."
                )
            base_url = self._base_url or os.environ.get(API_BASE_URL_ENV_VAR) or _DEFAULT_BASE_URL
            self._client = _IsloClient(base_url=base_url, api_key=api_key)
        return self._client

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
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.islo.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved


def _url_component(value: str) -> str:
    return quote(value, safe="")


def _new_sandbox_name(label: str) -> str:
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    return f"omnigent-{base[:40]}-{uuid.uuid4().hex[:6]}"


def _parse_exec_sse(
    lines: Iterator[str],
    *,
    on_stdout: Callable[[str], None] | None,
    on_stderr: Callable[[str], None] | None,
) -> int:
    exit_code = 1
    seen_exit = False
    event = ""
    data: list[str] = []

    def flush() -> None:
        nonlocal event, data, exit_code, seen_exit
        if not event and not data:
            return
        payload = "\n".join(data)
        if event == "stdout" and on_stdout is not None:
            on_stdout(payload)
        elif event == "stderr" and on_stderr is not None:
            on_stderr(payload)
        elif event == "exit":
            try:
                exit_code = int(payload.strip())
            except ValueError as exc:
                raise _IsloAPIError(f"islo exec stream invalid exit event {payload!r}") from exc
            seen_exit = True
        event = ""
        data = []

    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            flush()
            continue
        if line.startswith(":"):
            continue
        field, sep, value = line.partition(":")
        if sep:
            value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
    flush()
    if not seen_exit:
        raise _IsloAPIError("islo exec stream ended without exit event")
    return exit_code
