"""Mesos sandbox launcher backed by the mesos-compose HTTP API.

The launcher follows the entrypoint-as-host model used by Kubernetes:
``provision`` reserves a project name, then ``start_host`` submits a generated
compose document whose only service runs ``omnigent host``.  This keeps direct
Mesos scheduler implementation details out of Omnigent and delegates offer,
reconciliation, and task lifecycle handling to mesos-compose.
"""

from __future__ import annotations

import os
import re
import shlex
import time
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import click
import httpx
import yaml

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    SandboxHostLauncher,
    render_host_config_write_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from collections.abc import Callable


HOST_IMAGE_ENV_VAR = "OMNIGENT_MESOS_HOST_IMAGE"
COMPOSE_URL_ENV_VAR = "OMNIGENT_MESOS_COMPOSE_URL"
COMPOSE_USERNAME_ENV_VAR = "OMNIGENT_MESOS_COMPOSE_USERNAME"
COMPOSE_PASSWORD_ENV_VAR = "OMNIGENT_MESOS_COMPOSE_PASSWORD"
TARGET_HOSTNAME_ENV_VAR = "OMNIGENT_MESOS_TARGET_HOSTNAME"
SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_MESOS_SANDBOX_ENV"
ALLOW_INSECURE_TLS_ENV_VAR = "OMNIGENT_MESOS_ALLOW_INSECURE_TLS"

_HOME_DIR = "/mnt/mesos/sandbox"
_SERVICE_NAME = "host"
_TASK_READY_TIMEOUT_S = 90
_TASK_READY_POLL_S = 2.0
_REQUEST_TIMEOUT_S = 15.0
_TERMINATE_ATTEMPTS = 3
_TERMINATE_RETRY_BASE_S = 0.5
_TERMINAL_TASK_STATES = frozenset(
    {
        "TASK_DROPPED",
        "TASK_ERROR",
        "TASK_FAILED",
        "TASK_FINISHED",
        "TASK_GONE",
        "TASK_KILLED",
        "TASK_LOST",
        "TASK_UNKNOWN",
        "TASK_UNREACHABLE",
    }
)
_RESERVED_ENV_NAMES = frozenset(
    {"HOME", "IS_SANDBOX", HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR}
)

# The host can orphan runner children.  This tiny PID-1 wrapper forwards shutdown
# signals and reaps descendants while preserving the host process's exit status.
_REAPER_SRC = """\
import os, signal, subprocess, sys

child = subprocess.Popen(sys.argv[1:])


def forward(signum, _frame):
    try:
        child.send_signal(signum)
    except ProcessLookupError:
        pass


signal.signal(signal.SIGTERM, forward)
signal.signal(signal.SIGINT, forward)
while True:
    try:
        pid, status = os.wait()
    except ChildProcessError:
        break
    if pid == child.pid:
        if os.WIFSIGNALED(status):
            sys.exit(128 + os.WTERMSIG(status))
        sys.exit(os.WEXITSTATUS(status))
"""


def _new_project_name(label: str) -> str:
    """Return a unique mesos-compose project name safe for its URL path."""
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    return f"omnigent-{base[:36]}-{uuid.uuid4().hex[:6]}"


def _render_start_command(
    *,
    server_url: str,
    repo_url: str | None,
    repo_branch: str | None,
    repo_name: str | None,
    host_config: dict[str, object] | None,
) -> str:
    """Build the shell command that prepares the workspace and starts the host."""
    workspace = f"{_HOME_DIR}/workspace"
    script = f"set -e\nmkdir -p {shlex.quote(workspace)}\n"
    if repo_url is not None and repo_name is not None:
        clone_dir = f"{workspace}/{repo_name}"
        branch = (
            f"--branch {shlex.quote(repo_branch)} --single-branch "
            if repo_branch is not None
            else ""
        )
        script += f"git clone {branch}-- {shlex.quote(repo_url)} {shlex.quote(clone_dir)}\n"
    if host_config is not None:
        script += render_host_config_write_command(host_config) + "\n"
    script += (
        f"exec python3 -c {shlex.quote(_REAPER_SRC)} "
        f"omnigent host --server {shlex.quote(server_url)}"
    )
    return script


def build_compose_manifest(
    *,
    image: str,
    sandbox_id: str,
    host_id: str,
    host_name: str,
    token: str,
    server_url: str,
    env_literals: Mapping[str, str],
    target_hostname: str | None,
    repo_url: str | None = None,
    repo_branch: str | None = None,
    repo_name: str | None = None,
    host_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """Build the mesos-compose document for one managed Omnigent host."""
    environment = {
        "HOME": _HOME_DIR,
        "IS_SANDBOX": "1",
        HOST_ID_ENV_VAR: host_id,
        HOST_NAME_ENV_VAR: host_name,
        HOST_TOKEN_ENV_VAR: token,
        **env_literals,
    }
    service: dict[str, object] = {
        "image": image,
        "command": _render_start_command(
            server_url=server_url,
            repo_url=repo_url,
            repo_branch=repo_branch,
            repo_name=repo_name,
            host_config=host_config,
        ),
        "environment": environment,
        "hostname": host_name,
        "container_name": sandbox_id,
        "container_type": "docker",
        "network_mode": "BRIDGE",
        "pull_policy": "always",
        "restart": "no",
        "shell": True,
        "deploy": {
            "replicas": 1,
            "resources": {"limits": {"cpus": 1.0, "memory": 1024}},
        },
    }
    if target_hostname is not None:
        deploy = service["deploy"]
        assert isinstance(deploy, dict)
        deploy["placement"] = {
            "constraints": [f"node.hostname=={target_hostname}"],
        }
    return {"version": "3.9", "services": {_SERVICE_NAME: service}}


class MesosSandboxLauncher(SandboxHostLauncher):
    """Managed host launcher using a running mesos-compose framework."""

    provider: ClassVar[str] = "mesos"
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False
    requires_durable_cleanup: ClassVar[bool] = True

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        compose_url: str | None = None,
        username: str | None = None,
        verify_ssl: bool | None = None,
        target_hostname: str | None = None,
    ) -> None:
        super().__init__()
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._compose_url = compose_url
        self._username = username
        self._verify_ssl = verify_ssl
        self._target_hostname = target_hostname

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
        )

    def _resolve_image(self) -> str:
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE

    def _resolve_compose_url(self) -> str:
        url = self._compose_url or os.environ.get(COMPOSE_URL_ENV_VAR)
        if not url:
            raise click.ClickException(
                "The mesos-compose URL is required for the 'mesos' sandbox provider; "
                "set sandbox.mesos.compose_url or OMNIGENT_MESOS_COMPOSE_URL."
            )
        return url.rstrip("/")

    def _resolve_credentials(self) -> tuple[str, str] | None:
        username = self._username or os.environ.get(COMPOSE_USERNAME_ENV_VAR)
        password = os.environ.get(COMPOSE_PASSWORD_ENV_VAR)
        if bool(username) != bool(password):
            raise click.ClickException(
                "mesos-compose authentication requires a username and the "
                f"{COMPOSE_PASSWORD_ENV_VAR} environment variable"
            )
        return (username, password) if username is not None and password is not None else None

    def _resolve_transport(self) -> tuple[str, tuple[str, str] | None, bool]:
        """Validate the compose endpoint before constructing an authenticated client."""
        url = self._resolve_compose_url()
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise click.ClickException("mesos-compose URL must be an absolute HTTP(S) URL")
        credentials = self._resolve_credentials()
        if credentials is not None and parsed.scheme != "https":
            raise click.ClickException("mesos-compose Basic authentication requires HTTPS")
        verify = True if self._verify_ssl is None else self._verify_ssl
        insecure_opt_in = os.environ.get(ALLOW_INSECURE_TLS_ENV_VAR, "").lower() in {
            "1",
            "true",
            "yes",
        }
        if credentials is not None and not verify and not insecure_opt_in:
            raise click.ClickException(
                "disabling mesos-compose TLS verification with authentication requires "
                f"{ALLOW_INSECURE_TLS_ENV_VAR}=1"
            )
        return url, credentials, verify

    def _resolve_target_hostname(self) -> str | None:
        return self._target_hostname or os.environ.get(TARGET_HOSTNAME_ENV_VAR)

    def _resolve_sandbox_env(self) -> dict[str, str]:
        if self._env_names is None:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        else:
            names = list(self._env_names)
        resolved: dict[str, str] = {}
        for name in names:
            if name in _RESERVED_ENV_NAMES:
                raise click.ClickException(f"sandbox env name '{name}' is reserved by Omnigent")
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server environment"
                )
            resolved[name] = value
        return resolved

    def _request(self, method: str, path: str, **kwargs: object) -> httpx.Response:
        compose_url, credentials, verify = self._resolve_transport()
        client = httpx.Client(
            base_url=compose_url,
            auth=credentials,
            verify=verify,
            timeout=_REQUEST_TIMEOUT_S,
        )
        try:
            response = client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise click.ClickException(
                f"mesos-compose request {method} {path} failed: {exc}"
            ) from exc
        finally:
            client.close()
        if response.status_code == 401:
            raise click.ClickException("mesos-compose rejected authentication (HTTP 401)")
        if method == "DELETE" and response.status_code == 404:
            return response
        if response.status_code >= 400:
            raise click.ClickException(
                f"mesos-compose request {method} {path} failed with HTTP {response.status_code}"
            )
        return response

    def prepare(self) -> None:
        """Verify that mesos-compose is reachable and serves the v0 API."""
        response = self._request("GET", "/api/compose/versions")
        if "/api/compose/v0" not in response.text:
            raise click.ClickException(
                "mesos-compose did not advertise the required /api/compose/v0 API"
            )

    def provision(self, name: str) -> str:
        """Reserve a unique mesos-compose project name before token registration."""
        return _new_project_name(name)

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
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Submit the host service to mesos-compose and wait for TASK_RUNNING."""
        workspace = f"{_HOME_DIR}/workspace"
        clone_dir = f"{workspace}/{repo_name}" if repo_url is not None and repo_name else None
        if on_stage is not None:
            on_stage("starting")
        manifest = build_compose_manifest(
            image=self._resolve_image(),
            sandbox_id=sandbox_id,
            host_id=host_id,
            host_name=host_name,
            token=token,
            server_url=server_url,
            env_literals=self._resolve_sandbox_env(),
            target_hostname=self._resolve_target_hostname(),
            repo_url=repo_url,
            repo_branch=repo_branch,
            repo_name=repo_name,
            host_config=host_config,
        )
        payload = yaml.safe_dump(manifest, sort_keys=False)
        self._request(
            "PUT",
            f"/api/compose/v0/{sandbox_id}",
            content=payload,
            headers={"Content-Type": "application/yaml"},
        )
        self._wait_for_task_running(sandbox_id)
        click.echo(f"  → mesos-compose project '{sandbox_id}' is running the host service")
        return clone_dir or workspace

    def _wait_for_task_running(self, sandbox_id: str) -> None:
        deadline = time.monotonic() + _TASK_READY_TIMEOUT_S
        last_state = "not listed"
        task_prefix = f"{sandbox_id}_{_SERVICE_NAME}."
        while True:
            response = self._request("GET", "/api/compose/v0/tasks")
            try:
                tasks = response.json()
            except ValueError as exc:
                raise click.ClickException(
                    "mesos-compose returned invalid JSON for its task list"
                ) from exc
            if tasks is None:
                tasks = []
            if not isinstance(tasks, list):
                raise click.ClickException("mesos-compose returned an invalid task list")
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                task_id = str(task.get("TaskID", ""))
                task_name = str(task.get("task_name", task.get("TaskName", "")))
                if not (
                    task_id.startswith(task_prefix)
                    or task_name.endswith(f":{sandbox_id}:{_SERVICE_NAME}")
                ):
                    continue
                last_state = str(task.get("State", "unknown"))
                if last_state == "TASK_RUNNING":
                    return
                if last_state in _TERMINAL_TASK_STATES:
                    raise click.ClickException(
                        f"mesos-compose task for project '{sandbox_id}' entered {last_state}"
                    )
            if time.monotonic() >= deadline:
                raise click.ClickException(
                    f"mesos-compose task for project '{sandbox_id}' did not reach TASK_RUNNING "
                    f"within {_TASK_READY_TIMEOUT_S}s (last state: {last_state})"
                )
            time.sleep(_TASK_READY_POLL_S)

    def terminate(self, sandbox_id: str) -> None:
        """Kill the host service, retrying transient compose API failures."""
        path = f"/api/compose/v0/{sandbox_id}/{_SERVICE_NAME}"
        for attempt in range(_TERMINATE_ATTEMPTS):
            try:
                self._request("DELETE", path)
                return
            except click.ClickException:
                if attempt == _TERMINATE_ATTEMPTS - 1:
                    raise
                time.sleep(_TERMINATE_RETRY_BASE_S * (2**attempt))

    def is_running(self, sandbox_id: str) -> bool | None:
        """Report whether this project's host task is currently running."""
        response = self._request("GET", "/api/compose/v0/tasks")
        try:
            tasks = response.json() or []
        except ValueError:
            return None
        task_prefix = f"{sandbox_id}_{_SERVICE_NAME}."
        return any(
            isinstance(task, dict)
            and str(task.get("TaskID", "")).startswith(task_prefix)
            and task.get("State") == "TASK_RUNNING"
            for task in tasks
        )
