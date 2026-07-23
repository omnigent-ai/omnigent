"""Cloudflare Sandbox provider using the Sandbox Bridge HTTP API."""

from __future__ import annotations

import json
import os
import shlex
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator, Iterator, Literal, cast

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    RemoteProcess,
    SandboxCapabilityError,
    SandboxLauncher,
    get_backend as _unused_get_backend,
    host_image_wheel_install_command,
    register_backend as _unused_register_backend,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


# Sandbox Bridge HTTP API endpoints
_BRIDGE_API_VERSION = "v1"


@dataclass
class CloudflareSandboxConfig:
    """Configuration for Cloudflare Sandbox Bridge."""

    bridge_url: str
    api_key: str
    timeout: float = 300.0
    # Warm pool settings
    warm_pool_enabled: bool = False
    warm_pool_size: int = 1


class _CloudflareRemoteProcess(RemoteProcess):
    """Streaming remote process via Cloudflare Sandbox Bridge."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        bridge_url: str,
        api_key: str,
        sandbox_id: str,
        process_id: str,
    ) -> None:
        self._client = client
        self._bridge_url = bridge_url.rstrip("/")
        self._api_key = api_key
        self._sandbox_id = sandbox_id
        self._process_id = process_id
        self._lines_iter: AsyncIterator[str] | None = None
        self._exit_code: int | None = None
        self._closed = False

    @property
    def lines(self) -> AsyncIterator[str]:
        if self._lines_iter is None:
            self._lines_iter = self._stream_lines()
        return self._lines_iter

    async def _stream_lines(self) -> AsyncIterator[str]:
        url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{self._sandbox_id}/process/{self._process_id}/stream"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        async with self._client.stream("GET", url, headers=headers, timeout=None) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                yield line + "\n"

    async def wait(self) -> int:
        if self._exit_code is not None:
            return self._exit_code
        url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{self._sandbox_id}/process/{self._process_id}/wait"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        resp = await self._client.post(url, headers=headers)
        resp.raise_for_status()
        data = resp.json()
        self._exit_code = data.get("exitCode", 0)
        return self._exit_code

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{self._sandbox_id}/process/{self._process_id}"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            await self._client.delete(url, headers=headers)
        except Exception:
            pass


class CloudflareSandboxLauncher(SandboxLauncher):
    """Launch Omnigent hosts in Cloudflare Sandboxes via the Bridge API."""

    provider: str = "cloudflare_sandbox"
    supports_local_port_forward: bool = False
    supports_cli_bootstrap: bool = False
    can_resume: bool = False

    def __init__(
        self,
        *,
        bridge_url: str | None = None,
        api_key: str | None = None,
        image: str | None = None,
        env: list[str] | None = None,
        timeout: float = 300.0,
        warm_pool_enabled: bool = False,
        warm_pool_size: int = 1,
    ) -> None:
        """
        Args:
            bridge_url: Cloudflare Sandbox Bridge Worker URL (e.g., https://sandbox-bridge.your-subdomain.workers.dev)
            api_key: SANDBOX_API_KEY secret for authentication
            image: Docker image for the sandbox (default: official omnigent-host image)
            env: Server environment variable names to inject into sandbox
            timeout: HTTP timeout in seconds
            warm_pool_enabled: Enable warm pool for instant sandbox boot
            warm_pool_size: Number of warm sandboxes to maintain
        """
        self._bridge_url = bridge_url or os.environ.get("OMNIGENT_CLOUDFLARE_SANDBOX_BRIDGE_URL")
        self._api_key = api_key or os.environ.get("OMNIGENT_CLOUDFLARE_SANDBOX_API_KEY")
        self._image = image or DEFAULT_HOST_IMAGE
        self._env_names = env or []
        self._timeout = timeout
        self._warm_pool_enabled = warm_pool_enabled
        self._warm_pool_size = warm_pool_size

        if not self._bridge_url:
            raise click.ClickException(
                "Cloudflare Sandbox Bridge URL not provided. Set OMNIGENT_CLOUDFLARE_SANDBOX_BRIDGE_URL "
                "or pass bridge_url= to the launcher."
            )
        if not self._api_key:
            raise click.ClickException(
                "Cloudflare Sandbox API key not provided. Set OMNIGENT_CLOUDFLARE_SANDBOX_API_KEY "
                "or pass api_key= to the launcher."
            )

        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def _close_client(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def prepare(self) -> None:
        """Verify bridge is reachable and credentials work."""
        import asyncio

        async def _check() -> None:
            client = await self._get_client()
            try:
                url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/openapi.json"
                resp = await client.get(url, headers=self._headers())
                resp.raise_for_status()
            finally:
                await self._close_client()

        asyncio.run(_check())

    def provision(self, name: str) -> str:
        """Create a new sandbox via the Bridge API."""
        import asyncio

        async def _provision() -> str:
            client = await self._get_client()
            try:
                url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox"
                payload: dict[str, Any] = {
                    "image": self._image,
                    "sleepAfter": "30m",
                }
                if self._warm_pool_enabled:
                    payload["warmPool"] = {"size": self._warm_pool_size}
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                sandbox_id = data.get("id")
                if not sandbox_id:
                    raise click.ClickException(f"Sandbox creation succeeded but no ID returned: {data}")
                return sandbox_id
            finally:
                await self._close_client()

        return asyncio.run(_provision())

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
        """Start the Omnigent host process in the sandbox."""
        import asyncio

        async def _start() -> str:
            client = await self._get_client()
            try:
                # Resolve $HOME
                home = await self._run_command(client, sandbox_id, 'printf %s "$HOME"')
                if not home:
                    raise click.ClickException(f"Could not resolve $HOME inside sandbox '{sandbox_id}'")
                workspace = f"{home}/workspace"

                # Create workspace directory
                await self._run_command(client, sandbox_id, f"mkdir -p {shlex.quote(workspace)}")

                # Clone repository if provided
                if repo_url is not None:
                    if on_stage:
                        on_stage("cloning")
                    clone_dir = f"{workspace}/{repo_name}"
                    branch_args = f"--branch {shlex.quote(repo_branch)} --single-branch " if repo_branch else ""
                    await self._run_command(
                        client, sandbox_id, f"git clone {branch_args}-- {shlex.quote(repo_url)} {shlex.quote(clone_dir)}"
                    )
                    workspace = clone_dir

                # Write host config if provided
                if host_config is not None or self.can_resume:
                    if on_stage:
                        on_stage("configuring")
                    from omnigent.onboarding.sandboxes.base import render_host_config_write_command
                    cmd = render_host_config_write_command(host_config or {})
                    await self._run_command(client, sandbox_id, cmd)

                # Prepare environment
                env_vars = {
                    "OMNIGENT_HOST_TOKEN": token,
                    "OMNIGENT_HOST_ID": host_id,
                    "OMNIGENT_HOST_NAME": host_name,
                }
                # Inject server env vars
                for name in self._env_names:
                    value = os.environ.get(name)
                    if value is not None:
                        env_vars[name] = value

                env_prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in env_vars.items())

                # Start host in background
                if on_stage:
                    on_stage("starting")
                cmd = f"{env_prefix} omnigent host --server {shlex.quote(server_url)}"
                await self._run_background_command(client, sandbox_id, cmd)

                return workspace
            finally:
                await self._close_client()

        return asyncio.run(_start())

    async def _run_command(
        self, client: httpx.AsyncClient, sandbox_id: str, command: str, *, check: bool = True
    ) -> RemoteCommandResult:
        url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{sandbox_id}/exec"
        payload = {"command": command, "cwd": "/workspace"}
        resp = await client.post(url, headers=self._headers(), json=payload)
        if check:
            resp.raise_for_status()
        data = resp.json()
        return RemoteCommandResult(
            returncode=data.get("exitCode", 0),
            stdout=data.get("stdout", ""),
            stderr=data.get("stderr", ""),
        )

    async def _run_background_command(
        self, client: httpx.AsyncClient, sandbox_id: str, command: str
    ) -> RemoteCommandResult:
        # Use setsid nohup to background the process
        bg_cmd = f"setsid nohup sh -c {shlex.quote(command)} > /tmp/omnigent-host.log 2>&1 < /dev/null & echo launched"
        return await self._run_command(client, sandbox_id, bg_cmd, check=True)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        import asyncio

        async def _run() -> RemoteCommandResult:
            client = await self._get_client()
            try:
                return await self._run_command(client, sandbox_id, command, check=check)
            finally:
                await self._close_client()

        return asyncio.run(_run())

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        import asyncio

        async def _run_bg() -> RemoteCommandResult:
            client = await self._get_client()
            try:
                return await self._run_background_command(client, sandbox_id, command)
            finally:
                await self._close_client()

        return asyncio.run(_run_bg())

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        import asyncio

        async def _put() -> None:
            client = await self._get_client()
            try:
                # Read file and upload via write-file endpoint
                content = local_path.read_bytes()
                url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{sandbox_id}/files"
                params = {"path": remote_path}
                headers = {"Authorization": f"Bearer {self._api_key}"}
                resp = await client.put(url, headers=headers, params=params, content=content)
                resp.raise_for_status()
            finally:
                await self._close_client()

        asyncio.run(_put())

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        import asyncio

        async def _spawn() -> _CloudflareRemoteProcess:
            client = await self._get_client()
            try:
                url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{sandbox_id}/process"
                payload = {"command": command, "cwd": "/workspace"}
                resp = await client.post(url, headers=self._headers(), json=payload)
                resp.raise_for_status()
                data = resp.json()
                process_id = data.get("processId")
                if not process_id:
                    raise click.ClickException(f"Process spawn failed: {data}")
                return _CloudflareRemoteProcess(client, self._bridge_url, self._api_key, sandbox_id, process_id)
            except Exception:
                await self._close_client()
                raise

        return asyncio.run(_spawn())

    def terminate(self, sandbox_id: str) -> None:
        import asyncio

        async def _term() -> None:
            client = await self._get_client()
            try:
                url = f"{self._bridge_url}/{_BRIDGE_API_VERSION}/sandbox/{sandbox_id}"
                resp = await client.delete(url, headers=self._headers())
                resp.raise_for_status()
            finally:
                await self._close_client()

        asyncio.run(_term())

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        return host_image_wheel_install_command(remote_tgz_path)

    def __del__(self) -> None:
        if self._client is not None:
            import asyncio

            try:
                asyncio.run(self._close_client())
            except Exception:
                pass


def create_launcher(
    *,
    bridge_url: str | None = None,
    api_key: str | None = None,
    image: str | None = None,
    env: list[str] | None = None,
    timeout: float = 300.0,
    warm_pool_enabled: bool = False,
    warm_pool_size: int = 1,
) -> CloudflareSandboxLauncher:
    """Factory for creating a CloudflareSandboxLauncher."""
    return CloudflareSandboxLauncher(
        bridge_url=bridge_url,
        api_key=api_key,
        image=image,
        env=env,
        timeout=timeout,
        warm_pool_enabled=warm_pool_enabled,
        warm_pool_size=warm_pool_size,
    )