"""
Boat.dev sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Boat <https://boat.dev>`_ sandboxes over the public v1 HTTP API.
No SDK is required; one ``httpx.Client`` per launcher handles all calls.

Platform notes that shape this launcher:

- **API-key auth.** ``BOAT_API_KEY`` is read from the server process
  environment and never injected into the sandbox. ``BOAT_API_URL``
  overrides the API base URL (default ``https://boat.dev/api/v1``).
- **Isolated sandboxes.** Sandboxes are created with ``noEnv`` so Boat
  account secrets and repositories are not inherited, with command
  monitoring and automatic archival disabled.
- **Persistent resume.** Archived or stopped sandboxes are resumed on
  attach, still with ``noEnv``. Terminal states (deleted, cancelled) and a
  snapshot Boat has expired (``409 snapshot_data_expired``, 14 days after
  stop) raise :class:`SandboxGoneError` so the server provisions a replacement.
- **Host package.** The host is installed with ``uv`` into an isolated
  Python 3.12 environment. ``OMNIGENT_BOAT_HOST_PACKAGE`` overrides the
  requirement, which defaults to the server's own version.
- **No local port forwarding** and no interactive CLI bootstrap.
"""

from __future__ import annotations

import base64
import importlib.metadata
import os
import shlex
import time
import uuid
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    ExecModelHostLauncher,
    RemoteCommandResult,
    SandboxGoneError,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

BOAT_API_URL = "https://boat.dev/api/v1"
_ACTIVE = frozenset({"ready", "idle", "running"})
_RESUMABLE = frozenset({"archived", "stopped"})
_TERMINAL = frozenset({"deleted", "cancelled"})
_TIMEOUT = httpx.Timeout(620.0, connect=10.0)


def _api_url() -> str:
    return os.environ.get("BOAT_API_URL", BOAT_API_URL).rstrip("/")


def _host_package() -> str:
    return (
        os.environ.get("OMNIGENT_BOAT_HOST_PACKAGE")
        or f"omnigent=={importlib.metadata.version('omnigent')}"
    )


class BoatSandboxLauncher(ExecModelHostLauncher):
    """Run managed Omnigent hosts on Boat; credentials stay on the server."""

    provider: ClassVar[str] = "boat"
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(self) -> None:
        super().__init__()
        self._http: httpx.Client | None = None

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(timeout=_TIMEOUT)
        return self._http

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            managed_launch=True,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=True,
            multi_repo=True,
            git_clone_options=True,
        )

    def prepare(self) -> None:
        """Require a service API key before provisioning."""
        if not os.environ.get("BOAT_API_KEY"):
            raise click.ClickException(
                "Set BOAT_API_KEY from https://boat.dev/dashboard?tab=api-keys."
            )

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        missing_ok: bool = False,
    ) -> dict[str, Any]:
        self.prepare()
        deadline = time.monotonic() + 30
        while True:
            try:
                response = self._client().request(
                    method,
                    _api_url() + path,
                    headers={
                        "Authorization": f"Bearer {os.environ['BOAT_API_KEY']}",
                        **(headers or {}),
                    },
                    json=body,
                )
            except httpx.RequestError as exc:
                raise click.ClickException("Boat API request failed; check connectivity.") from exc
            if not _safe_to_retry(response) or time.monotonic() >= deadline:
                break
            time.sleep(2)
        if response.status_code == 404:
            if missing_ok:
                return {}
            raise SandboxGoneError("Boat sandbox no longer exists.")
        if response.status_code == 409 and _error_code(response) == " (snapshot_data_expired)":
            # Boat deleted the stopped sandbox's disk; no resume can ever succeed.
            raise SandboxGoneError("Boat sandbox snapshot data expired.")
        if response.is_error:
            # Provider bodies may contain credentials or remote command text.
            # Surface only Boat's machine readable error code.
            raise click.ClickException(
                f"Boat API returned HTTP {response.status_code}{_error_code(response)}."
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise click.ClickException("Boat API returned invalid JSON.") from exc
        if not isinstance(data, dict) or data.get("ok") is False:
            raise click.ClickException("Boat API returned an unsuccessful response.")
        return data

    def _state(self, sandbox_id: str) -> str:
        state = str(self._request("GET", self._path(sandbox_id))["sandbox"]["state"])
        if state in _TERMINAL:
            raise SandboxGoneError(f"Boat sandbox is {state}.")
        return state

    def _path(self, sandbox_id: str) -> str:
        return f"/sandboxes/{quote(sandbox_id, safe='')}"

    def _wait_ready(self, sandbox_id: str) -> None:
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            state = self._state(sandbox_id)
            if state in _ACTIVE:
                return
            if state in _RESUMABLE or state == "error":
                raise click.ClickException(f"Boat sandbox is {state}; cannot launch host.")
            time.sleep(2)
        raise click.ClickException("Timed out waiting for Boat sandbox to become ready.")

    def provision(self, name: str) -> str:
        """Create an isolated sandbox and install the host; clean up failed setup."""
        created = self._request(
            "POST",
            "/sandboxes",
            body={"noEnv": True, "ttlSeconds": None, "monitoring": False},
            headers={"Idempotency-Key": str(uuid.uuid4())},
        )
        sandbox_id = str(created["sandbox"]["id"])
        try:
            self._wait_ready(sandbox_id)
            self._request("PATCH", self._path(sandbox_id), body={"name": name})
            package = shlex.quote(_host_package())
            self.run(
                sandbox_id,
                "uv venv --python 3.12 /home/user/.omnigent-venv && "
                f"uv pip install --python /home/user/.omnigent-venv/bin/python {package} && "
                "sudo ln -sf /home/user/.omnigent-venv/bin/omnigent /usr/local/bin/omnigent",
            )
        except BaseException:
            try:
                self.terminate(sandbox_id)
            except Exception:
                click.echo(
                    f"Boat cleanup failed; delete sandbox {sandbox_id} in the dashboard.", err=True
                )
            raise
        return sandbox_id

    def attach(self, sandbox_id: str) -> None:
        """Resume an archived sandbox or wait for an active one."""
        state = self._state(sandbox_id)
        if state in _RESUMABLE:
            self.resume(sandbox_id)
        else:
            self._wait_ready(sandbox_id)

    def resume(self, sandbox_id: str) -> None:
        """Restore the persistent sandbox filesystem."""
        state = self._state(sandbox_id)
        if state not in _ACTIVE:
            # Restate noEnv so account secrets are withheld on resume too (one-way at Boat).
            self._request(
                "POST",
                self._path(sandbox_id) + "/resume",
                body={"noEnv": True, "ttlSeconds": None},
            )
        self._wait_ready(sandbox_id)

    def keep_alive(self, sandbox_id: str) -> bool:
        """Disable automatic archival; warn and return False on failure."""
        try:
            self._request("PATCH", self._path(sandbox_id), body={"ttlSeconds": None})
        except click.ClickException as exc:
            click.echo(f"Warning: Boat keep alive failed: {exc.format_message()}", err=True)
            return False
        return True

    def is_running(self, sandbox_id: str) -> bool:
        data = self._request("GET", self._path(sandbox_id), missing_ok=True)
        return data.get("sandbox", {}).get("state") in _ACTIVE

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Execute shell commands with a bounded timeout and separate output streams."""
        data = self._request(
            "POST",
            self._path(sandbox_id) + "/commands",
            body={"command": command, "timeoutSeconds": 600},
        )
        code = data.get("exitCode")
        if data.get("timedOut") or code is None:
            raise click.ClickException("Boat command timed out or returned no exit status.")
        result = RemoteCommandResult(
            returncode=int(code),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
        )
        if check and result.returncode:
            raise click.ClickException(f"Boat command failed (exit {result.returncode}).")
        return result

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        """Upload binary files, using signed URLs for files above the inline limit."""
        content = local_path.read_bytes()
        path = self._path(sandbox_id) + "/files"
        if len(content) <= 5 * 1024 * 1024:
            self._request(
                "PUT",
                path,
                body={
                    "path": remote_path,
                    "content": base64.b64encode(content).decode(),
                    "encoding": "base64",
                },
            )
            return
        data = self._request("POST", path + "/upload-url", body={"path": remote_path})
        try:
            response = httpx.put(data["url"], content=content, timeout=620)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise click.ClickException("Boat file upload failed.") from exc

    def terminate(self, sandbox_id: str) -> None:
        """Delete the sandbox, treating an already absent sandbox as success."""
        self._request(
            "DELETE",
            self._path(sandbox_id),
            headers={"X-Ascii-Confirm-Delete": sandbox_id},
            missing_ok=True,
        )


def _error_code(response: httpx.Response) -> str:
    """Return Boat's error code suffix without echoing the response body."""
    try:
        data = response.json()
    except ValueError:
        return ""
    code = data.get("code") if isinstance(data, dict) else None
    if isinstance(code, str) and code.replace("_", "").isalnum() and len(code) <= 64:
        return f" ({code})"
    return ""


def _safe_to_retry(response: httpx.Response) -> bool:
    """Retry only when Boat guarantees that a remote command did not run."""
    if response.status_code not in {409, 502}:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    if response.status_code == 409:
        return data.get("code") == "sandbox_starting"
    if data.get("code") != "sandbox_direct_failed":
        return False
    error = data.get("error")
    if not isinstance(error, dict):
        return data.get("message") == "sandbox_restoring"
    details = error.get("details")
    return (isinstance(details, dict) and details.get("retryable") is True) or (
        error.get("message") == "sandbox_restoring"
    )
