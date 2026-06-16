"""
Docker sandbox launcher for server-managed Omnigent hosts.

Implements the managed subset of :class:`SandboxLauncher` for a local
Docker daemon: the server spawns one ``omnigent-host`` container per
managed session as a sibling of the server container (Docker-out-of-
Docker via a mounted ``/var/run/docker.sock``). The container dials the
server back over the existing host tunnel exactly like every other
provider, so the managed launch flow in
:mod:`omnigent.server.managed_hosts` is unchanged.

Managed-only: only ``prepare`` / ``provision`` / ``run`` / ``terminate``
are implemented. The CLI bootstrap primitives (``put`` / ``stream_exec``
/ wheels / local port forward) are intentionally absent —
:attr:`supports_cli_bootstrap` is ``False`` so the CLI fails fast with a
pointer to ``host_type="managed"`` instead of a mid-flow capability error.

The ``docker`` SDK is an optional dependency (``pip install
'omnigent[docker]'``, baked into the official Compose server image)
imported lazily, so the provider can be listed and this module imported
without it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from docker import DockerClient

HOST_IMAGE_ENV_VAR = "OMNIGENT_DOCKER_HOST_IMAGE"
SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_DOCKER_SANDBOX_ENV"
DEFAULT_NETWORK = "omnigent-sbx"

# Container labels stamped at provision time. ``host_name`` is a host
# DISPLAY name, never a session id — the reaper keys on label presence +
# the host store, not on this value.
DOCKER_MANAGED_LABEL = "omnigent.managed"
DOCKER_PROVIDER_LABEL = "omnigent.provider"
DOCKER_HOST_NAME_LABEL = "omnigent.host_name"
DOCKER_CREATED_AT_LABEL = "omnigent.created_at"


def _ensure_sdk() -> None:
    """Verify the docker SDK is importable, with an install hint when not."""
    try:
        import docker  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The Docker SDK is required for the 'docker' sandbox provider. "
            "Install it with `pip install 'omnigent[docker]'`, or use the "
            "official managed Docker Compose image (which bakes it in)."
        ) from exc


def docker_client() -> DockerClient:
    """Return a Docker SDK client, mapping setup failures to ClickException."""
    _ensure_sdk()
    import docker

    try:
        return docker.from_env()
    except Exception as exc:
        raise click.ClickException(
            f"Could not connect to the Docker daemon: {exc}. For Compose, mount "
            "/var/run/docker.sock into the server and set DOCKER_GID so the "
            "non-root server user can read it (see deploy/docker/README.md)."
        ) from exc


class DockerSandboxLauncher(SandboxLauncher):
    """:class:`SandboxLauncher` for a local Docker daemon (managed subset)."""

    provider: ClassVar[str] = "docker"
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        env: Sequence[str] | None = None,
        network: str | None = None,
        resources: Mapping[str, object] | None = None,
        security: Mapping[str, object] | None = None,
    ) -> None:
        """
        :param image: Image to provision from; ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` then the official host image.
        :param env: Server-process env var NAMES injected into every
            sandbox; ``None`` resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param network: Docker network sandboxes join (the segmented
            sandbox network); ``None`` falls back to :data:`DEFAULT_NETWORK`.
        :param resources: ``containers.run`` resource kwargs, e.g.
            ``{"mem_limit": "4g", "nano_cpus": 2_000_000_000,
            "pids_limit": 512}``.
        :param security: ``containers.run`` security kwargs, e.g.
            ``{"security_opt": ["no-new-privileges:true"],
            "cap_drop": ["ALL"]}`` — set from the A2 spike outcome.
        """
        self._image_ref = image
        self._env_names = tuple(env) if env is not None else None
        self._network = network or DEFAULT_NETWORK
        self._resources = dict(resources or {})
        self._security = dict(security or {})

    def _client(self) -> DockerClient:
        """Return a Docker client (a fresh handle per call is fine here)."""
        return docker_client()

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """Resolve env var NAMES to inject, reading values from server env."""
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
                    f"sandbox env passthrough names '{name}' but it is not set in the "
                    "server's environment — set it or remove it from sandbox.docker.env "
                    f"/ {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            resolved[name] = value
        return resolved

    def prepare(self) -> None:
        """Preflight: the SDK must be installed and the daemon reachable."""
        client = self._client()
        try:
            client.ping()
        except Exception as exc:
            raise click.ClickException(
                "Docker daemon is not reachable from the Omnigent server. For "
                "Compose, mount /var/run/docker.sock and set DOCKER_GID so the "
                "non-root server user can access it (see deploy/docker/README.md)."
            ) from exc

    def provision(self, name: str) -> str:
        """Create a detached host container and return its id."""
        image = self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE
        labels = {
            DOCKER_MANAGED_LABEL: "1",
            DOCKER_PROVIDER_LABEL: self.provider,
            DOCKER_HOST_NAME_LABEL: name,
            DOCKER_CREATED_AT_LABEL: str(int(time.time())),
        }
        kwargs: dict[str, object] = {
            "detach": True,
            "network": self._network,
            "environment": self._resolve_sandbox_env() or None,
            "labels": labels,
            "init": True,
            **self._resources,
            **self._security,
        }
        click.echo(f"▸ Creating Docker sandbox '{name}' from {image}")
        try:
            container = self._client().containers.run(image, ["sleep", "infinity"], **kwargs)
        except Exception as exc:
            raise click.ClickException(f"Docker container creation failed: {exc}") from exc
        container_id = getattr(container, "id", None)
        if not isinstance(container_id, str) or not container_id:
            raise click.ClickException("Docker container creation returned no container id")
        click.echo(f"  → created {container_id}")
        return container_id

    def _get_container(self, container_id: str) -> Any:
        """Resolve a container handle, mapping a missing one to ClickException."""
        try:
            return self._client().containers.get(container_id)
        except Exception as exc:
            raise click.ClickException(
                f"Docker sandbox '{container_id}' not found — it may have been "
                f"stopped or reaped: {exc}"
            ) from exc

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """Run ``bash -lc <command>`` in the container; capture rc/out/err."""
        container = self._get_container(sandbox_id)
        try:
            result = container.exec_run(["bash", "-lc", command], demux=True)
        except Exception as exc:
            raise click.ClickException(
                f"Remote command failed to execute in Docker container "
                f"'{sandbox_id}': {exc}"
            ) from exc
        output = result.output
        stdout_b, stderr_b = output if isinstance(output, tuple) else (output, b"")
        stdout = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr = (stderr_b or b"").decode("utf-8", errors="replace")
        if check and result.exit_code != 0:
            raise click.ClickException(
                f"Remote command failed in Docker container '{sandbox_id}' "
                f"(exit {result.exit_code}): {command}"
            )
        return RemoteCommandResult(returncode=result.exit_code, stdout=stdout, stderr=stderr)

    def terminate(self, sandbox_id: str) -> None:
        """Force-remove the container; an already-gone container is success."""
        _ensure_sdk()
        import docker

        try:
            self._client().containers.get(sandbox_id).remove(force=True)
        except docker.errors.NotFound:
            return  # Already gone — desired end state holds.
        except Exception as exc:
            raise click.ClickException(
                f"Failed to remove Docker container '{sandbox_id}': {exc}"
            ) from exc
