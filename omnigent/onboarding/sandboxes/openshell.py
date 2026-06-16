"""
NVIDIA OpenShell sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `NVIDIA OpenShell <https://github.com/NVIDIA/openshell>`_ sandboxes.
OpenShell provides AI-agent sandboxes via Docker containers exposed
through a FastAPI REST API. The integration talks to the OpenShell HTTP
API directly through ``httpx`` (already a base Omnigent dependency), so
there is no provider SDK extra to install.

Platform notes that shape this launcher:

- **Self-hosted.** OpenShell runs on the user's own infrastructure via
  Docker, making it suitable for on-prem deployments where cloud
  providers (Modal, Daytona, E2B) are unavailable.
- **API-driven.** All operations use OpenShell's REST API: sandbox
  create/delete for lifecycle, command execution for running code, and
  file upload/download for wheel shipping.
- **No local port forwarding.** OpenShell does not provide a
  local-to-sandbox port forward for the in-sandbox App OAuth callback.
  The CLI therefore skips that auth step automatically.
"""

from __future__ import annotations

import os
import re
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
    host_image_wheel_install_command,
)

BASE_URL_ENV_VAR: str = "OPENSHELL_BASE_URL"
"""OpenShell server base URL, e.g. ``http://localhost:8000``."""

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_OPENSHELL_HOST_IMAGE"
"""Environment variable overriding :data:`DEFAULT_HOST_IMAGE` for
OpenShell sandboxes."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_OPENSHELL_SANDBOX_ENV"
"""Comma-separated server-process environment variable names injected
into created OpenShell sandboxes."""

_DEFAULT_SANDBOX_CPU = 2
_DEFAULT_SANDBOX_MEMORY_MB = 4096
_REQUEST_TIMEOUT_S = 30.0
_PROVISION_TIMEOUT_S = 300.0


class _OpenShellAPIError(RuntimeError):
    """Provider-boundary error with a user-facing message."""


class _OpenShellClient:
    """Small synchronous OpenShell HTTP API client."""

    def __init__(self, *, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=_REQUEST_TIMEOUT_S)

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._client.close()

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a sandbox and return the response object."""
        return self._request_json("POST", "/sandboxes", json=payload, timeout=_PROVISION_TIMEOUT_S)

    def execute(self, sandbox_id: str, command: str, timeout: int = 300) -> dict[str, Any]:
        """Execute a command in a sandbox and return the result."""
        return self._request_json(
            "POST",
            f"/sandboxes/{_url_component(sandbox_id)}/execute",
            json={"command": command, "timeout": timeout},
            timeout=max(float(timeout) + 10.0, _REQUEST_TIMEOUT_S),
        )

    def upload_file(
        self, sandbox_id: str, local_path: Path, remote_path: str
    ) -> None:
        """Upload one file to an absolute path in the sandbox."""
        with local_path.open("rb") as file_obj:
            files = {"file": (local_path.name, file_obj, "application/octet-stream")}
            self._request(
                "POST",
                f"/sandboxes/{_url_component(sandbox_id)}/upload",
                files=files,
                data={"path": remote_path},
            )

    def get_status(self, sandbox_id: str) -> dict[str, Any]:
        """Get the status of a sandbox."""
        return self._request_json(
            "GET", f"/sandboxes/{_url_component(sandbox_id)}/status"
        )

    def delete_sandbox(self, sandbox_id: str) -> None:
        """Delete a sandbox. Missing sandboxes are treated as gone."""
        try:
            self._request("DELETE", f"/sandboxes/{_url_component(sandbox_id)}")
        except _OpenShellAPIError as exc:
            if "HTTP 404" not in str(exc):
                raise

    def _request_json(
        self, method: str, endpoint: str, timeout: float | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        response = self._request(method, endpoint, timeout=timeout, **kwargs)
        try:
            data = response.json()
        except ValueError as exc:
            raise _OpenShellAPIError(
                f"openshell {method} {endpoint} returned invalid JSON"
            ) from exc
        if not isinstance(data, dict):
            raise _OpenShellAPIError(
                f"openshell {method} {endpoint} returned a non-object response"
            )
        return data

    def _request(
        self, method: str, endpoint: str, timeout: float | None = None, **kwargs: Any
    ) -> httpx.Response:
        url = self._url(endpoint)
        effective_timeout = timeout if timeout is not None else _REQUEST_TIMEOUT_S
        try:
            response = self._client.request(
                method, url, timeout=effective_timeout, **kwargs
            )
        except httpx.HTTPError as exc:
            raise _OpenShellAPIError(
                f"openshell {method} {endpoint} failed: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise self._response_error(method, endpoint, response)
        return response

    def _url(self, endpoint: str) -> str:
        return self._base_url + endpoint

    def _response_error(
        self, method: str, endpoint: str, response: httpx.Response
    ) -> _OpenShellAPIError:
        try:
            text = response.text
        except httpx.ResponseNotRead:
            text = response.read().decode("utf-8", errors="replace")
        snippet = text.strip()[:1024]
        detail = f": {snippet}" if snippet else ""
        return _OpenShellAPIError(
            f"openshell {method} {endpoint} failed with HTTP {response.status_code}{detail}"
        )


class OpenShellSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for NVIDIA OpenShell sandboxes.

    All primitives use OpenShell's REST API: sandbox create/delete for
    lifecycle, command execution for running code, and file upload for
    wheel shipping.
    """

    provider: ClassVar[str] = "openshell"
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        base_url: str | None = None,
        cpu: int | None = None,
        memory_mb: int | None = None,
    ) -> None:
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._base_url = base_url
        self._cpu = cpu
        self._memory_mb = memory_mb
        self._client: _OpenShellClient | None = None

    def prepare(self) -> None:
        """Verify OpenShell server URL is available."""
        if not (self._base_url or os.environ.get(BASE_URL_ENV_VAR)):
            raise click.ClickException(
                "No OpenShell server configured. Set OPENSHELL_BASE_URL to "
                "the OpenShell server address (e.g. http://localhost:8000)."
            )

    def provision(self, name: str) -> str:
        """Create a new OpenShell sandbox from the host image."""
        resolved_ref = (
            self._image_ref
            or os.environ.get(HOST_IMAGE_ENV_VAR)
            or DEFAULT_HOST_IMAGE
        )
        payload: dict[str, Any] = {
            "image": resolved_ref,
            "name": name,
        }
        cpu = self._cpu or _DEFAULT_SANDBOX_CPU
        memory = self._memory_mb or _DEFAULT_SANDBOX_MEMORY_MB
        payload["cpu"] = cpu
        payload["memory_mb"] = memory
        env_vars = self._resolve_sandbox_env()
        if env_vars:
            payload["env"] = env_vars
        click.echo(f"▸ Creating OpenShell sandbox from {resolved_ref}")
        try:
            sandbox = self._openshell().create_sandbox(payload)
        except _OpenShellAPIError as exc:
            raise click.ClickException(
                f"OpenShell sandbox creation failed: {exc}"
            ) from exc
        sandbox_id = sandbox.get("id") or sandbox.get("sandbox_id") or sandbox.get("name")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise click.ClickException(
                "OpenShell sandbox creation returned no sandbox identifier"
            )
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """Validate access to an existing OpenShell sandbox."""
        click.echo(f"▸ Reusing existing OpenShell sandbox '{sandbox_id}'")
        try:
            self._openshell().get_status(sandbox_id)
        except _OpenShellAPIError as exc:
            raise click.ClickException(
                f"Could not attach to OpenShell sandbox '{sandbox_id}': {exc}"
            ) from exc

    def keep_alive(self, sandbox_id: str) -> None:
        """No idle auto-stop management is exposed by the OpenShell API."""
        click.echo(
            f"  → OpenShell sandbox '{sandbox_id}' remains active until destroyed"
        )

    def run(
        self, sandbox_id: str, command: str, *, check: bool = True
    ) -> RemoteCommandResult:
        """Run a shell command in the sandbox and capture its output."""
        try:
            result = self._openshell().execute(sandbox_id, command)
        except _OpenShellAPIError as exc:
            raise click.ClickException(
                f"Remote command failed on OpenShell sandbox '{sandbox_id}': {exc}"
            ) from exc
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        exit_code = result.get("exit_code", 1)
        if stdout:
            click.echo(stdout, nl=False)
        if stderr:
            click.echo(stderr, nl=False, err=True)
        if check and exit_code != 0:
            raise click.ClickException(
                f"Remote command failed on OpenShell sandbox '{sandbox_id}' "
                f"(exit {exit_code}): {command}"
            )
        return RemoteCommandResult(
            returncode=exit_code, stdout=stdout, stderr=stderr
        )

    def put(
        self, sandbox_id: str, local_path: Path, remote_path: str
    ) -> None:
        """Copy a local file into the sandbox."""
        try:
            self._openshell().upload_file(sandbox_id, local_path, remote_path)
        except _OpenShellAPIError as exc:
            raise click.ClickException(
                f"File upload to OpenShell sandbox '{sandbox_id}' failed: {exc}"
            ) from exc

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        """Remote command that overlays shipped wheels onto the host image."""
        return host_image_wheel_install_command(remote_tgz_path)

    def terminate(self, sandbox_id: str) -> None:
        """Delete a sandbox, releasing its compute."""
        try:
            self._openshell().delete_sandbox(sandbox_id)
        finally:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _openshell(self) -> _OpenShellClient:
        if self._client is None:
            base_url = self._base_url or os.environ.get(BASE_URL_ENV_VAR)
            if not base_url:
                raise click.ClickException(
                    "No OpenShell server configured. Set OPENSHELL_BASE_URL to "
                    "the OpenShell server address (e.g. http://localhost:8000)."
                )
            self._client = _OpenShellClient(base_url=base_url)
        return self._client

    def _resolve_sandbox_env(self) -> dict[str, str]:
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(
                    SANDBOX_ENV_PASSTHROUGH_ENV_VAR, ""
                ).split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.openshell.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved


def _url_component(value: str) -> str:
    return quote(value, safe="")
