"""
Islo sandbox launcher.

Implements :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
for `Islo <https://islo.dev>`_ sandboxes. The integration talks to the
Islo Python SDK for lifecycle, auth refresh, and blocking execs. A small
raw HTTP path remains for the SSE stream and file upload endpoints because
Omnigent's bootstrap needs interactive streaming output.

Platform notes that shape this launcher:

- **API-key auth.** ``ISLO_API_KEY`` is passed to the SDK, which exchanges
  it for short-lived session tokens and refreshes them automatically.
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
import shlex
import threading
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import quote

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
_SANDBOX_CPU = 2
_SANDBOX_MEMORY_MB = 4096
DEFAULT_IDLE_PAUSE_AFTER_S = 15 * 60
_REQUEST_TIMEOUT_S = 30.0
_STREAM_TIMEOUT_S = None
_RUNNING_STATUSES = frozenset({"running", "ready"})
_RESUMABLE_STATUSES = frozenset({"paused", "stopped"})
_NON_RESUMABLE_STATUSES = frozenset({"deleted", "deleting", "failed", "error"})
_INSTALL_HINT = (
    "The Islo sandbox provider requires the optional Islo SDK. Install it with "
    "`pip install 'omnigent[islo]'` or `uv tool install 'omnigent[islo]'`."
)

# Claude credentials a user injects via sandbox env passthrough that must win
# over the gateway ``apiKeyHelper`` Islo pre-seeds into every sandbox. When one
# is present we strip the seeded helper (see
# :meth:`IsloSandboxLauncher._clear_seeded_api_key_helper`).
_USER_CLAUDE_CRED_ENV_VARS = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

# In-sandbox script that removes a seeded ``apiKeyHelper`` from Claude Code's
# settings. Best effort: a sandbox without the settings file is left untouched.
_CLEAR_API_KEY_HELPER_SCRIPT = """\
import json, os
path = os.path.expanduser("~/.claude/settings.json")
try:
    with open(path) as handle:
        settings = json.load(handle)
except (FileNotFoundError, ValueError):
    raise SystemExit(0)
if isinstance(settings, dict) and settings.pop("apiKeyHelper", None) is not None:
    with open(path, "w") as handle:
        json.dump(settings, handle, indent=2)
"""

_STOP_PRESERVED_HOST_DAEMON_SCRIPT = """\
import os, signal, subprocess, time

self_pids = {os.getpid(), os.getppid()}
try:
    output = subprocess.check_output(["ps", "-eo", "pid=,args="], text=True)
except Exception as exc:
    print(f"could not inspect process table: {exc}")
    raise SystemExit(0)

targets = []
for line in output.splitlines():
    parts = line.strip().split(None, 1)
    if len(parts) != 2:
        continue
    try:
        pid = int(parts[0])
    except ValueError:
        continue
    args = parts[1]
    if pid in self_pids:
        continue
    if "omnigent host" in args:
        targets.append(pid)

for pid in targets:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        print(f"could not terminate preserved omnigent host pid {pid}: {exc}")

time.sleep(0.5)
for pid in targets:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        continue
    except PermissionError:
        continue
    try:
        os.kill(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass

if targets:
    print(f"stopped preserved omnigent host daemon(s): {', '.join(map(str, targets))}")
"""


class _IsloAPIError(RuntimeError):
    """Provider-boundary error with a user-facing message."""


@dataclass(frozen=True)
class _IsloSDK:
    """Lazy-loaded SDK symbols used by the launcher."""

    islo_cls: type[Any]
    api_error_cls: type[Exception]
    exec_and_wait_sync: Callable[..., Any]


def _load_islo_sdk() -> _IsloSDK:
    """
    Import the optional Islo SDK at use time.

    The launcher module is importable without ``omnigent[islo]`` installed;
    users only need the extra when they actually select the Islo provider.
    """
    try:
        from islo import Islo
        from islo.core.api_error import ApiError
        from islo.custom.exec import exec_and_wait_sync
    except ImportError as exc:
        raise click.ClickException(_INSTALL_HINT) from exc
    return _IsloSDK(
        islo_cls=Islo,
        api_error_cls=ApiError,
        exec_and_wait_sync=exec_and_wait_sync,
    )


class _IsloClient:
    """Small synchronous adapter around the Islo Python SDK."""

    def __init__(self, *, base_url: str, api_key: str) -> None:
        sdk = _load_islo_sdk()
        self._api_error_cls = sdk.api_error_cls
        self._exec_and_wait_sync = sdk.exec_and_wait_sync
        self._client = sdk.islo_cls(
            api_key=api_key,
            base_url=base_url.rstrip("/"),
            timeout=_REQUEST_TIMEOUT_S,
        )

    def close(self) -> None:
        """Close the SDK's underlying HTTP connection pool when possible."""
        try:
            self._client._client_wrapper.httpx_client.httpx_client.close()
        except AttributeError:
            return

    def create_sandbox(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a sandbox and return the response object."""
        try:
            sandbox = self._client.sandboxes.create_sandbox(**payload)
        except Exception as exc:
            raise self._sdk_error("create sandbox", exc) from exc
        return _object_dict(sandbox)

    def get_sandbox(self, name: str) -> dict[str, Any]:
        """Fetch a sandbox by name."""
        try:
            sandbox = self._client.sandboxes.get_sandbox(name)
        except Exception as exc:
            raise self._sdk_error(f"get sandbox '{name}'", exc) from exc
        return _object_dict(sandbox)

    def delete_sandbox(self, name: str) -> None:
        """Delete a sandbox by name. Missing sandboxes are treated as gone."""
        try:
            self._client.sandboxes.delete_sandbox(name)
        except Exception as exc:
            if self._is_not_found(exc):
                return
            raise self._sdk_error(f"delete sandbox '{name}'", exc) from exc

    def resume_sandbox(self, name: str) -> dict[str, Any]:
        """Resume a paused/stopped sandbox and return the refreshed object."""
        try:
            sandbox = self._client.sandboxes.resume_sandbox(name)
        except Exception as exc:
            raise self._sdk_error(f"resume sandbox '{name}'", exc) from exc
        return _object_dict(sandbox)

    def upload_file(self, name: str, local_path: Path, remote_path: str) -> None:
        """Upload one file to an absolute path in the sandbox."""
        wrapper = self._client._client_wrapper
        try:
            with local_path.open("rb") as file_obj:
                response = wrapper.httpx_client.request(
                    f"sandboxes/{_url_component(name)}/files",
                    base_url=wrapper.get_environment().compute,
                    method="POST",
                    params={"path": remote_path},
                    files={"file": (local_path.name, file_obj, "application/octet-stream")},
                )
        except (OSError, httpx.HTTPError) as exc:
            raise _IsloAPIError(f"islo file upload failed: {exc}") from exc
        if response.status_code >= 400:
            raise self._response_error("POST", f"/sandboxes/{name}/files", response)

    def exec(
        self,
        name: str,
        command: Sequence[str],
        *,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> tuple[int, str, str]:
        """Execute a command through the SDK helper and wait for completion."""
        try:
            result = self._exec_and_wait_sync(
                self._client,
                name,
                list(command),
                workdir=workdir,
                env=env,
            )
        except Exception as exc:
            raise self._sdk_error(f"exec in sandbox '{name}'", exc) from exc
        return (
            int(getattr(result, "exit_code", -1)),
            str(getattr(result, "stdout", "") or ""),
            str(getattr(result, "stderr", "") or ""),
        )

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
        wrapper = self._client._client_wrapper
        headers = wrapper.get_headers()
        headers["Accept"] = "text/event-stream"
        compute_url = wrapper.get_environment().compute.rstrip("/")
        url = f"{compute_url}/sandboxes/{_url_component(name)}/exec/stream"
        try:
            with wrapper.httpx_client.httpx_client.stream(
                "POST",
                url,
                headers=headers,
                json=body,
                timeout=_STREAM_TIMEOUT_S,
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

    def _is_not_found(self, exc: BaseException) -> bool:
        return isinstance(exc, self._api_error_cls) and getattr(exc, "status_code", None) == 404

    def _sdk_error(self, action: str, exc: BaseException) -> _IsloAPIError:
        if isinstance(exc, _IsloAPIError):
            return exc
        if isinstance(exc, self._api_error_cls):
            status = getattr(exc, "status_code", None)
            body = getattr(exc, "body", None)
            status_text = f" with HTTP {status}" if status is not None else ""
            detail = f": {body}" if body else ""
            return _IsloAPIError(f"islo {action} failed{status_text}{detail}")
        return _IsloAPIError(f"islo {action} failed: {exc}")


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
    can_resume: ClassVar[bool] = True

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
        idle_pause_after_s: int | None = DEFAULT_IDLE_PAUSE_AFTER_S,
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
        self._idle_pause_after_s = idle_pause_after_s
        self._client: _IsloClient | None = None

    def prepare(self) -> None:
        """Verify Islo credentials are available."""
        if not os.environ.get(API_KEY_ENV_VAR):
            raise click.ClickException(
                "No Islo credentials found. Create an API key at "
                "https://islo.dev and set ISLO_API_KEY."
            )
        _load_islo_sdk()

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
        if self._idle_pause_after_s is not None:
            payload["lifecycle"] = {
                "pause_after_idle": self._idle_pause_after_s,
                "auto_resume": "never",
            }
        click.echo(f"▸ Creating Islo sandbox '{sandbox_name}' from {resolved_ref}")
        try:
            sandbox = self._islo().create_sandbox(payload)
        except _IsloAPIError as exc:
            raise click.ClickException(f"Islo sandbox creation failed: {exc}") from exc
        created_name = sandbox.get("name")
        if not isinstance(created_name, str) or not created_name:
            raise click.ClickException("Islo sandbox creation returned no sandbox name")
        click.echo(f"  → created {created_name}")
        self._clear_seeded_api_key_helper(created_name, env_vars)
        return created_name

    def _clear_seeded_api_key_helper(self, sandbox_id: str, env_vars: dict[str, str]) -> None:
        """
        Strip Islo's gateway ``apiKeyHelper`` when the user injected their
        own Claude credential.

        Islo pre-seeds ``~/.claude/settings.json`` with an ``apiKeyHelper``
        that resolves, through Islo's gateway, to a connected provider
        integration. Claude Code prefers that helper over a
        ``CLAUDE_CODE_OAUTH_TOKEN`` / ``ANTHROPIC_API_KEY`` in the
        environment, so a user who brings their own credential through
        sandbox env passthrough would be silently overridden. When such a
        credential is among the injected vars, remove the seeded helper so
        the user's credential is the sole auth path. Best effort: a sandbox
        with no seeded settings file is left untouched, and a failed strip
        warns rather than aborting the launch.
        """
        if not any(name in env_vars for name in _USER_CLAUDE_CRED_ENV_VARS):
            return
        click.echo(
            "  → clearing Islo's seeded apiKeyHelper so your injected "
            "Claude credential takes precedence"
        )
        try:
            self.run(
                sandbox_id,
                f"python3 -c {shlex.quote(_CLEAR_API_KEY_HELPER_SCRIPT)}",
                check=False,
            )
        except click.ClickException as exc:
            click.echo(f"  → warning: could not clear seeded apiKeyHelper: {exc}", err=True)

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Stop any memory-preserved host daemon, then start with a fresh token."""
        self._stop_preserved_host_daemon(sandbox_id)
        return super().start_host(
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=server_url,
            repo_url=repo_url,
            repo_branch=repo_branch,
            repo_name=repo_name,
            on_stage=on_stage,
        )

    def _stop_preserved_host_daemon(self, sandbox_id: str) -> None:
        """
        Best-effort cleanup for Islo's memory-preserving pause/resume.

        A paused VM can resume with the old ``omnigent host`` process still
        alive and carrying a stale launch token. Stop it before the shared
        startup path launches a fresh daemon.
        """
        try:
            self.run(
                sandbox_id,
                f"python3 -c {shlex.quote(_STOP_PRESERVED_HOST_DAEMON_SCRIPT)}",
                check=False,
            )
        except click.ClickException as exc:
            click.echo(f"  → warning: could not stop preserved omnigent host: {exc}", err=True)

    def attach(self, sandbox_id: str) -> None:
        """Validate access to an existing Islo sandbox."""
        click.echo(f"▸ Reusing existing Islo sandbox '{sandbox_id}'")
        try:
            self._islo().get_sandbox(sandbox_id)
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Could not attach to Islo sandbox '{sandbox_id}': {exc}"
            ) from exc

    def resume(self, sandbox_id: str) -> None:
        """Resume a paused/stopped Islo sandbox in place."""
        click.echo(f"▸ Resuming Islo sandbox '{sandbox_id}'")
        try:
            sandbox = self._islo().get_sandbox(sandbox_id)
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Could not resume Islo sandbox '{sandbox_id}': {exc}"
            ) from exc
        status = str(sandbox.get("status") or "").lower()
        if status in _RUNNING_STATUSES:
            click.echo(f"  → sandbox '{sandbox_id}' is already running")
            return
        if status in _RESUMABLE_STATUSES:
            try:
                self._islo().resume_sandbox(sandbox_id)
            except _IsloAPIError as exc:
                raise click.ClickException(
                    f"Could not resume Islo sandbox '{sandbox_id}': {exc}"
                ) from exc
            click.echo(f"  → resumed {sandbox_id}")
            return
        if status in _NON_RESUMABLE_STATUSES:
            raise click.ClickException(
                f"Islo sandbox '{sandbox_id}' is {status}; it cannot be resumed in place."
            )
        raise click.ClickException(
            f"Islo sandbox '{sandbox_id}' is in unknown state {status!r}; "
            "it cannot be safely resumed in place."
        )

    def is_running(self, sandbox_id: str) -> bool | None:
        """Return whether Islo currently reports the sandbox as running."""
        try:
            sandbox = self._islo().get_sandbox(sandbox_id)
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Could not inspect Islo sandbox '{sandbox_id}': {exc}"
            ) from exc
        status = str(sandbox.get("status") or "").lower()
        if status in _RUNNING_STATUSES:
            return True
        if status in _RESUMABLE_STATUSES or status in _NON_RESUMABLE_STATUSES:
            return False
        return None

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
            returncode, stdout, stderr = self._islo().exec(
                sandbox_id,
                ["bash", "-lc", command],
            )
        except _IsloAPIError as exc:
            raise click.ClickException(
                f"Remote command failed to execute on Islo sandbox '{sandbox_id}': {exc}"
            ) from exc
        _stdout(stdout)
        _stderr(stderr)
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


def _object_dict(value: Any) -> dict[str, Any]:
    """Normalize SDK pydantic models and test fakes into plain dicts."""
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        data = value.model_dump()
        if isinstance(data, dict):
            return data
    if hasattr(value, "dict"):
        data = value.dict()
        if isinstance(data, dict):
            return data
    raise _IsloAPIError("islo SDK returned a non-object response")


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
