"""DigitalOcean Droplet + Block Storage managed-host launcher.

The stable sandbox id identifies a workspace volume, not a VM.  Compute is a
replaceable generation: suspend deletes the tagged Droplet, resume validates
the volume, and ``start_host`` creates a fresh Droplet that mounts it.
"""

from __future__ import annotations

import base64
import os
import shlex
import time
from collections.abc import Callable
from typing import Any, ClassVar

import click
import httpx

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    SandboxHostLauncher,
    render_host_config_write_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

DIGITALOCEAN_TOKEN_ENV_VAR = "DIGITALOCEAN_TOKEN"
DEFAULT_REGION = "sgp1"
DEFAULT_SIZE = "s-4vcpu-8gb"
DEFAULT_IMAGE = "ubuntu-24-04-x64"
DEFAULT_WORKSPACE_SIZE_GB = 100
DEFAULT_MOUNT_PATH = "/workspace"
DEFAULT_API_URL = "https://api.digitalocean.com/v2"

_MANAGED_TAG = "omnigent-managed"
_ACTION_TIMEOUT_S = 300.0
_DROPLET_TIMEOUT_S = 600.0
_SHUTDOWN_TIMEOUT_S = 120.0
_POWER_OFF_TIMEOUT_S = 60.0
_POLL_INITIAL_S = 1.0
_POLL_MAX_S = 8.0


class DigitalOceanAPIError(RuntimeError):
    """Sanitized error from one DigitalOcean API operation."""


class DigitalOceanAPI:
    """Small synchronous DigitalOcean v2 API client using the existing HTTP stack."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_API_URL,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(30.0),
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        expected: tuple[int, ...] = (200,),
        **kwargs: Any,
    ) -> dict[str, Any]:
        delay = _POLL_INITIAL_S
        for attempt in range(4):
            try:
                response = self._client.request(method, path, **kwargs)
            except httpx.HTTPError as exc:
                if attempt < 3:
                    self._sleep(delay)
                    delay = min(delay * 2, _POLL_MAX_S)
                    continue
                raise DigitalOceanAPIError(
                    f"DigitalOcean {operation} request failed: {exc}"
                ) from exc
            if response.status_code in expected:
                if response.status_code == 204 or not response.content:
                    return {}
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise DigitalOceanAPIError(
                        f"DigitalOcean {operation} returned invalid JSON"
                    ) from exc
                return payload if isinstance(payload, dict) else {}
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < 3:
                    retry_after = response.headers.get("Retry-After")
                    wait_s = delay
                    if retry_after and retry_after.isdigit():
                        wait_s = min(float(retry_after), 30.0)
                    self._sleep(wait_s)
                    delay = min(delay * 2, _POLL_MAX_S)
                    continue
            message = "unknown error"
            try:
                error = response.json()
                if isinstance(error, dict):
                    message = str(error.get("message") or error.get("id") or message)
            except ValueError:
                pass
            raise DigitalOceanAPIError(
                f"DigitalOcean {operation} failed ({response.status_code}): {message}"
            )
        raise DigitalOceanAPIError(f"DigitalOcean {operation} failed after retries")

    def find_volume(self, name: str, region: str) -> dict[str, Any] | None:
        payload = self._request(
            "GET",
            "/volumes",
            operation=f"find volume {name!r}",
            params={"name": name, "region": region, "per_page": 200},
        )
        volumes = payload.get("volumes")
        if not isinstance(volumes, list):
            return None
        matches = [item for item in volumes if isinstance(item, dict) and item.get("name") == name]
        if len(matches) > 1:
            raise DigitalOceanAPIError(
                f"DigitalOcean find volume {name!r} returned more than one exact match"
            )
        return matches[0] if matches else None

    def create_volume(
        self, *, name: str, region: str, size_gb: int, tags: list[str]
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/volumes",
            operation=f"create volume {name!r}",
            expected=(201,),
            json={
                "name": name,
                "region": region,
                "size_gigabytes": size_gb,
                "filesystem_type": "ext4",
                "filesystem_label": "omnigent",
                "description": "Omnigent persistent managed-host workspace",
                "tags": tags,
            },
        )
        volume = payload.get("volume")
        if not isinstance(volume, dict) or not isinstance(volume.get("id"), str):
            raise DigitalOceanAPIError("DigitalOcean create volume returned no volume id")
        return volume

    def delete_volume(self, volume_id: str) -> None:
        self._request(
            "DELETE",
            f"/volumes/{volume_id}",
            operation=f"delete volume {volume_id}",
            expected=(204, 404),
        )

    def find_droplets(self, tag: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/droplets",
            operation=f"find Droplets tagged {tag!r}",
            params={"tag_name": tag, "per_page": 200},
        )
        droplets = payload.get("droplets")
        return [item for item in droplets or [] if isinstance(item, dict)]

    def create_droplet(
        self,
        *,
        name: str,
        region: str,
        size: str,
        image: str,
        tags: list[str],
        user_data: str,
    ) -> dict[str, Any]:
        payload = self._request(
            "POST",
            "/droplets",
            operation=f"create Droplet {name!r}",
            expected=(202,),
            json={
                "name": name,
                "region": region,
                "size": size,
                "image": image,
                "tags": tags,
                "user_data": user_data,
                "monitoring": True,
                "backups": False,
                "ipv6": False,
                "with_droplet_agent": False,
            },
        )
        droplet = payload.get("droplet")
        if not isinstance(droplet, dict) or not isinstance(droplet.get("id"), int):
            raise DigitalOceanAPIError("DigitalOcean create Droplet returned no Droplet id")
        return droplet

    def get_droplet(self, droplet_id: int) -> dict[str, Any] | None:
        try:
            payload = self._request(
                "GET",
                f"/droplets/{droplet_id}",
                operation=f"get Droplet {droplet_id}",
            )
        except DigitalOceanAPIError as exc:
            if "(404)" in str(exc):
                return None
            raise
        droplet = payload.get("droplet")
        return droplet if isinstance(droplet, dict) else None

    def wait_droplet_active(
        self, droplet_id: int, *, timeout_s: float = _DROPLET_TIMEOUT_S
    ) -> None:
        deadline = time.monotonic() + timeout_s
        delay = _POLL_INITIAL_S
        while time.monotonic() < deadline:
            droplet = self.get_droplet(droplet_id)
            if droplet is None:
                raise DigitalOceanAPIError(
                    f"DigitalOcean wait for Droplet {droplet_id} failed: Droplet disappeared"
                )
            status = droplet.get("status")
            if status == "active":
                return
            if status in {"archive", "off"}:
                raise DigitalOceanAPIError(
                    f"DigitalOcean wait for Droplet {droplet_id} failed: status={status}"
                )
            self._sleep(delay)
            delay = min(delay * 1.5, _POLL_MAX_S)
        raise DigitalOceanAPIError(
            f"DigitalOcean wait for Droplet {droplet_id} timed out after {timeout_s:.0f}s"
        )

    def attach_volume(self, volume_id: str, droplet_id: int, region: str) -> None:
        payload = self._request(
            "POST",
            f"/volumes/{volume_id}/actions",
            operation=f"attach volume {volume_id} to Droplet {droplet_id}",
            expected=(202,),
            json={"type": "attach", "droplet_id": droplet_id, "region": region},
        )
        action = payload.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("id"), int):
            raise DigitalOceanAPIError("DigitalOcean attach volume returned no action id")
        self.wait_action(int(action["id"]), operation=f"attach volume {volume_id}")

    def detach_volume(self, volume_id: str, droplet_id: int, region: str) -> None:
        payload = self._request(
            "POST",
            f"/volumes/{volume_id}/actions",
            operation=f"detach volume {volume_id} from Droplet {droplet_id}",
            expected=(202,),
            json={"type": "detach", "droplet_id": droplet_id, "region": region},
        )
        action = payload.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("id"), int):
            raise DigitalOceanAPIError("DigitalOcean detach volume returned no action id")
        self.wait_action(int(action["id"]), operation=f"detach volume {volume_id}")

    def shutdown_droplet(self, droplet_id: int) -> None:
        self._run_droplet_action(droplet_id, "shutdown", operation="shut down")
        if self._wait_droplet_off(droplet_id, timeout_s=_SHUTDOWN_TIMEOUT_S):
            return
        self._run_droplet_action(droplet_id, "power_off", operation="power off")
        if not self._wait_droplet_off(droplet_id, timeout_s=_POWER_OFF_TIMEOUT_S):
            raise DigitalOceanAPIError(
                f"DigitalOcean wait for Droplet {droplet_id} to power off timed out"
            )

    def _run_droplet_action(self, droplet_id: int, action_type: str, *, operation: str) -> None:
        payload = self._request(
            "POST",
            f"/droplets/{droplet_id}/actions",
            operation=f"{operation} Droplet {droplet_id}",
            expected=(201,),
            json={"type": action_type},
        )
        action = payload.get("action")
        if not isinstance(action, dict) or not isinstance(action.get("id"), int):
            raise DigitalOceanAPIError(f"DigitalOcean {operation} returned no action id")
        self.wait_action(int(action["id"]), operation=f"{operation} Droplet {droplet_id}")

    def _wait_droplet_off(self, droplet_id: int, *, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        delay = _POLL_INITIAL_S
        while time.monotonic() < deadline:
            droplet = self.get_droplet(droplet_id)
            if droplet is None or droplet.get("status") in {"off", "archive"}:
                return True
            self._sleep(delay)
            delay = min(delay * 1.5, _POLL_MAX_S)
        return False

    def wait_action(
        self, action_id: int, *, operation: str, timeout_s: float = _ACTION_TIMEOUT_S
    ) -> None:
        deadline = time.monotonic() + timeout_s
        delay = _POLL_INITIAL_S
        while time.monotonic() < deadline:
            payload = self._request(
                "GET", f"/actions/{action_id}", operation=f"poll {operation} action {action_id}"
            )
            action = payload.get("action")
            status = action.get("status") if isinstance(action, dict) else None
            if status == "completed":
                return
            if status == "errored":
                raise DigitalOceanAPIError(f"DigitalOcean {operation} action {action_id} failed")
            self._sleep(delay)
            delay = min(delay * 1.5, _POLL_MAX_S)
        raise DigitalOceanAPIError(
            f"DigitalOcean {operation} action {action_id} timed out after {timeout_s:.0f}s"
        )

    def delete_droplet(self, droplet_id: int) -> None:
        self._request(
            "DELETE",
            f"/droplets/{droplet_id}",
            operation=f"delete Droplet {droplet_id}",
            expected=(204, 404),
        )


def _workspace_tag(sandbox_id: str) -> str:
    return f"omnigent-workspace-{sandbox_id}"


def _droplet_tag(sandbox_id: str) -> str:
    return f"omnigent-sandbox-{sandbox_id}"


def _volume_name(sandbox_id: str) -> str:
    return f"omnigent-workspace-{sandbox_id}"[:64].rstrip("-")


def _droplet_name(sandbox_id: str) -> str:
    return f"omnigent-host-{sandbox_id}"[:64].rstrip("-")


def _bootstrap_script(
    *,
    volume_name: str,
    image: str,
    mount_path: str,
    token: str,
    host_id: str,
    host_name: str,
    server_url: str,
    repo_url: str | None,
    repo_branch: str | None,
    repo_name: str | None,
    host_config: dict[str, object] | None,
) -> str:
    """Render cloud-init whose only credential is the scoped launch token."""
    storage_root = "/mnt/omnigent"
    config_dir = f"{storage_root}/config"
    workspace_root = f"{storage_root}/workspace"
    requested_workspace = workspace_root
    clone_command = ""
    if repo_url is not None and repo_name is not None:
        requested_workspace = f"{workspace_root}/{repo_name}"
        branch = (
            f"--branch {shlex.quote(repo_branch)} --single-branch "
            if repo_branch is not None
            else ""
        )
        clone_command = (
            f"if [ ! -d {shlex.quote(requested_workspace)}/.git ]; then "
            f"git clone {branch}-- {shlex.quote(repo_url)} "
            f"{shlex.quote(requested_workspace)}; fi\n"
        )
    config_command = render_host_config_write_command(host_config or {})
    container_name = "omnigent-host"
    script = f"""#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends docker.io git ca-certificates
systemctl enable --now docker

device={shlex.quote(f"/dev/disk/by-id/scsi-0DO_Volume_{volume_name}")}
for attempt in $(seq 1 180); do
  [ -b "$device" ] && break
  sleep 1
done
[ -b "$device" ] || {{ echo "workspace volume device did not appear" >&2; exit 1; }}
filesystem=$(blkid -o value -s TYPE "$device" || true)
[ "$filesystem" = ext4 ] || {{
  echo "workspace volume is not the expected ext4 filesystem" >&2
  exit 1
}}
mkdir -p {storage_root}
mountpoint -q {storage_root} || mount -o defaults,nofail,discard "$device" {storage_root}
mkdir -p {shlex.quote(config_dir)} {shlex.quote(workspace_root)}
chmod 700 {shlex.quote(config_dir)}
chmod 755 {shlex.quote(workspace_root)}
touch {shlex.quote(config_dir)}/gitconfig {shlex.quote(config_dir)}/git-credentials
chmod 600 {shlex.quote(config_dir)}/gitconfig {shlex.quote(config_dir)}/git-credentials
{clone_command}docker pull {shlex.quote(image)}
docker run --rm \
  -v {shlex.quote(config_dir)}:/root/.omnigent \
  {shlex.quote(image)} sh -lc {shlex.quote(config_command)}
docker rm -f {container_name} >/dev/null 2>&1 || true
docker run -d --name {container_name} --restart unless-stopped --network host \
  -e {HOST_TOKEN_ENV_VAR}={shlex.quote(token)} \
  -e {HOST_ID_ENV_VAR}={shlex.quote(host_id)} \
  -e {HOST_NAME_ENV_VAR}={shlex.quote(host_name)} \
  -v {shlex.quote(config_dir)}:/root/.omnigent \
  -v {shlex.quote(config_dir)}/gitconfig:/root/.gitconfig \
  -v {shlex.quote(config_dir)}/git-credentials:/root/.git-credentials \
  -v {shlex.quote(workspace_root)}:{shlex.quote(mount_path)} \
  -w {shlex.quote(mount_path + requested_workspace.removeprefix(workspace_root))} \
  {shlex.quote(image)} omnigent host --server {shlex.quote(server_url)}
"""
    encoded = base64.b64encode(script.encode()).decode()
    return "\n".join(
        (
            "#cloud-config",
            "write_files:",
            "  - path: /usr/local/sbin/omnigent-digitalocean-bootstrap",
            "    owner: root:root",
            "    permissions: '0700'",
            "    encoding: b64",
            f"    content: {encoded}",
            "runcmd:",
            "  - [bash, /usr/local/sbin/omnigent-digitalocean-bootstrap]",
            "",
        )
    )


class DigitalOceanSandboxLauncher(SandboxHostLauncher):
    """Managed-only launcher backed by an ephemeral Droplet and durable volume."""

    provider: ClassVar[str] = "digitalocean"
    supports_cli_bootstrap: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        region: str = DEFAULT_REGION,
        size: str = DEFAULT_SIZE,
        image: str = DEFAULT_IMAGE,
        host_image: str = DEFAULT_HOST_IMAGE,
        workspace_size_gb: int = DEFAULT_WORKSPACE_SIZE_GB,
        mount_path: str = DEFAULT_MOUNT_PATH,
        api: DigitalOceanAPI | None = None,
    ) -> None:
        self.region = region
        self.size = size
        self.image = image
        self.host_image = host_image
        self.workspace_size_gb = workspace_size_gb
        self.mount_path = mount_path
        self._api = api

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            resume_stopped=True,
            programmatic_terminate=True,
            suspend_compute=True,
        )

    def _client(self) -> DigitalOceanAPI:
        if self._api is None:
            token = os.environ.get(DIGITALOCEAN_TOKEN_ENV_VAR, "").strip()
            if not token:
                raise click.ClickException(
                    f"{DIGITALOCEAN_TOKEN_ENV_VAR} is required for the DigitalOcean provider"
                )
            self._api = DigitalOceanAPI(token)
        return self._api

    def prepare(self) -> None:
        self._client()

    def provision(self, name: str) -> str:
        sandbox_id = name.lower()
        volume_name = _volume_name(sandbox_id)
        api = self._client()
        try:
            volume = api.find_volume(volume_name, self.region)
            if volume is None:
                api.create_volume(
                    name=volume_name,
                    region=self.region,
                    size_gb=self.workspace_size_gb,
                    tags=[_MANAGED_TAG, _workspace_tag(sandbox_id)],
                )
            else:
                self._validate_volume(volume, sandbox_id)
        except DigitalOceanAPIError as exc:
            raise click.ClickException(str(exc)) from exc
        return sandbox_id

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
        api = self._client()
        volume_name = _volume_name(sandbox_id)
        try:
            volume = api.find_volume(volume_name, self.region)
            if volume is None:
                raise DigitalOceanAPIError(
                    f"DigitalOcean workspace volume {volume_name!r} is missing"
                )
            self._validate_volume(volume, sandbox_id)
            existing = api.find_droplets(_droplet_tag(sandbox_id))
            if existing:
                raise DigitalOceanAPIError(
                    f"DigitalOcean sandbox {sandbox_id!r} already has a Droplet; "
                    "refusing to create a second generation"
                )
            if on_stage is not None and repo_url is not None:
                on_stage("cloning")
            if on_stage is not None:
                on_stage("starting")
            cloud_init = _bootstrap_script(
                volume_name=volume_name,
                image=self.host_image,
                mount_path=self.mount_path,
                token=token,
                host_id=host_id,
                host_name=host_name,
                server_url=server_url,
                repo_url=repo_url,
                repo_branch=repo_branch,
                repo_name=repo_name,
                host_config=host_config,
            )
            droplet = api.create_droplet(
                name=_droplet_name(sandbox_id),
                region=self.region,
                size=self.size,
                image=self.image,
                tags=[_MANAGED_TAG, _droplet_tag(sandbox_id)],
                user_data=cloud_init,
            )
            droplet_id = int(droplet["id"])
            try:
                api.wait_droplet_active(droplet_id)
                api.attach_volume(str(volume["id"]), droplet_id, self.region)
            except Exception as exc:
                try:
                    api.delete_droplet(droplet_id)
                except DigitalOceanAPIError as cleanup_exc:
                    raise DigitalOceanAPIError(
                        f"{exc}; cleanup of Droplet {droplet_id} also failed: {cleanup_exc}"
                    ) from exc
                raise
        except DigitalOceanAPIError as exc:
            raise click.ClickException(str(exc)) from exc
        if repo_url is not None and repo_name is not None:
            return f"{self.mount_path.rstrip('/')}/{repo_name}"
        return self.mount_path

    def suspend(self, sandbox_id: str) -> None:
        """Shut down and delete tagged compute; the workspace volume remains."""
        try:
            api = self._client()
            volume = api.find_volume(_volume_name(sandbox_id), self.region)
            if volume is None:
                raise DigitalOceanAPIError(
                    f"DigitalOcean workspace volume for {sandbox_id!r} is missing"
                )
            self._validate_volume(volume, sandbox_id)
            for droplet in self._managed_droplets(sandbox_id):
                self._destroy_droplet(droplet, volume)
        except DigitalOceanAPIError as exc:
            raise click.ClickException(str(exc)) from exc

    def resume(self, sandbox_id: str) -> None:
        """Validate preserved storage; ``start_host`` creates fresh compute."""
        try:
            volume = self._client().find_volume(_volume_name(sandbox_id), self.region)
            if volume is None:
                raise DigitalOceanAPIError(
                    f"DigitalOcean workspace volume for {sandbox_id!r} is missing"
                )
            self._validate_volume(volume, sandbox_id)
            # A prior resume may have been interrupted after creating compute.
            # Replace that generation so the next start uses its freshly minted
            # launch token rather than stale cloud-init user-data.
            for droplet in self._managed_droplets(sandbox_id):
                self._destroy_droplet(droplet, volume)
        except DigitalOceanAPIError as exc:
            raise click.ClickException(str(exc)) from exc

    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            droplets = self._managed_droplets(sandbox_id)
        except DigitalOceanAPIError:
            return None
        return any(item.get("status") == "active" for item in droplets)

    def terminate(self, sandbox_id: str) -> None:
        """Permanently delete managed compute and the confidently matched volume."""
        api = self._client()
        try:
            volume = api.find_volume(_volume_name(sandbox_id), self.region)
            if volume is not None:
                self._validate_volume(volume, sandbox_id)
            for droplet in self._managed_droplets(sandbox_id):
                self._destroy_droplet(droplet, volume)
            if volume is None:
                return
            api.delete_volume(str(volume["id"]))
        except DigitalOceanAPIError as exc:
            raise click.ClickException(str(exc)) from exc

    def _managed_droplets(self, sandbox_id: str) -> list[dict[str, Any]]:
        tag = _droplet_tag(sandbox_id)
        droplets = self._client().find_droplets(tag)
        managed: list[dict[str, Any]] = []
        for item in droplets:
            tags = item.get("tags") or []
            if _MANAGED_TAG not in tags or tag not in tags:
                continue
            region = item.get("region")
            region_slug = region.get("slug") if isinstance(region, dict) else region
            if item.get("name") != _droplet_name(sandbox_id) or region_slug != self.region:
                raise DigitalOceanAPIError(
                    f"DigitalOcean Droplet {item.get('id', tag)!r} does not carry the "
                    "expected Omnigent ownership metadata; refusing to use or delete it"
                )
            managed.append(item)
        return managed

    def _destroy_droplet(
        self,
        droplet: dict[str, Any],
        volume: dict[str, Any] | None,
    ) -> None:
        droplet_id = droplet.get("id")
        if not isinstance(droplet_id, int):
            raise DigitalOceanAPIError("DigitalOcean managed Droplet returned no numeric id")
        api = self._client()
        if droplet.get("status") not in {"off", "archive"}:
            api.shutdown_droplet(droplet_id)
        if volume is not None and droplet_id in (volume.get("droplet_ids") or []):
            api.detach_volume(str(volume["id"]), droplet_id, self.region)
        api.delete_droplet(droplet_id)

    def _validate_volume(self, volume: dict[str, Any], sandbox_id: str) -> None:
        expected_name = _volume_name(sandbox_id)
        tags = volume.get("tags") or []
        region = volume.get("region")
        region_slug = region.get("slug") if isinstance(region, dict) else region
        if (
            volume.get("name") != expected_name
            or _MANAGED_TAG not in tags
            or _workspace_tag(sandbox_id) not in tags
            or region_slug != self.region
        ):
            raise DigitalOceanAPIError(
                f"DigitalOcean volume {volume.get('id', expected_name)!r} does not carry the "
                "expected Omnigent ownership metadata; refusing to use or delete it"
            )
        filesystem = volume.get("filesystem_type")
        if filesystem != "ext4":
            raise DigitalOceanAPIError(
                f"DigitalOcean volume {volume.get('id', expected_name)!r} uses "
                f"unexpected filesystem {filesystem!r}"
            )


__all__ = [
    "DEFAULT_IMAGE",
    "DEFAULT_MOUNT_PATH",
    "DEFAULT_REGION",
    "DEFAULT_SIZE",
    "DEFAULT_WORKSPACE_SIZE_GB",
    "DIGITALOCEAN_TOKEN_ENV_VAR",
    "DigitalOceanAPI",
    "DigitalOceanAPIError",
    "DigitalOceanSandboxLauncher",
]
