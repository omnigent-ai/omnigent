"""Mocked tests for the DigitalOcean managed-host provider."""

from __future__ import annotations

import base64
import re
from typing import Any

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes.digitalocean import (
    DigitalOceanAPI,
    DigitalOceanAPIError,
    DigitalOceanSandboxLauncher,
)


def _volume(sandbox_id: str = "managed-a1b2c3d4") -> dict[str, Any]:
    name = f"omnigent-workspace-{sandbox_id}"
    return {
        "id": "volume-1",
        "name": name,
        "tags": ["omnigent-managed", name],
        "region": {"slug": "sgp1"},
        "filesystem_type": "ext4",
        "droplet_ids": [],
    }


class FakeAPI:
    """State-recording API seam; it never reaches DigitalOcean."""

    def __init__(self, *, volume: dict[str, Any] | None = None) -> None:
        self.volume = volume
        self.droplets: list[dict[str, Any]] = []
        self.calls: list[tuple[Any, ...]] = []
        self.user_data = ""
        self.attach_error: Exception | None = None
        self.detach_error: Exception | None = None
        self.delete_droplet_error: Exception | None = None

    def find_volume(self, name: str, region: str) -> dict[str, Any] | None:
        self.calls.append(("find_volume", name, region))
        return self.volume

    def create_volume(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_volume", kwargs))
        sandbox_id = str(kwargs["name"]).removeprefix("omnigent-workspace-")
        self.volume = _volume(sandbox_id)
        return self.volume

    def find_droplets(self, tag: str) -> list[dict[str, Any]]:
        self.calls.append(("find_droplets", tag))
        return list(self.droplets)

    def create_droplet(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create_droplet", kwargs))
        self.user_data = str(kwargs["user_data"])
        droplet = {
            "id": 42,
            "name": str(kwargs["name"]),
            "region": {"slug": str(kwargs["region"])},
            "status": "new",
            "tags": list(kwargs["tags"]),
        }
        self.droplets = [droplet]
        return droplet

    def wait_droplet_active(self, droplet_id: int) -> None:
        self.calls.append(("wait_droplet_active", droplet_id))

    def attach_volume(self, volume_id: str, droplet_id: int, region: str) -> None:
        self.calls.append(("attach_volume", volume_id, droplet_id, region))
        if self.attach_error is not None:
            raise self.attach_error
        assert self.volume is not None
        self.volume["droplet_ids"] = [droplet_id]

    def shutdown_droplet(self, droplet_id: int) -> None:
        self.calls.append(("shutdown_droplet", droplet_id))
        for droplet in self.droplets:
            if droplet.get("id") == droplet_id:
                droplet["status"] = "off"

    def detach_volume(self, volume_id: str, droplet_id: int, region: str) -> None:
        self.calls.append(("detach_volume", volume_id, droplet_id, region))
        if self.detach_error is not None:
            raise self.detach_error
        assert self.volume is not None
        self.volume["droplet_ids"] = []

    def delete_droplet(self, droplet_id: int) -> None:
        self.calls.append(("delete_droplet", droplet_id))
        if self.delete_droplet_error is not None:
            raise self.delete_droplet_error
        self.droplets = []

    def delete_volume(self, volume_id: str) -> None:
        self.calls.append(("delete_volume", volume_id))
        self.volume = None


def _launcher(api: FakeAPI) -> DigitalOceanSandboxLauncher:
    return DigitalOceanSandboxLauncher(api=api)  # type: ignore[arg-type]


def _decode_bootstrap(user_data: str) -> str:
    encoded = re.search(r"^    content: (.+)$", user_data, re.MULTILINE)
    assert encoded is not None
    return base64.b64decode(encoded.group(1)).decode()


def test_create_finds_or_creates_volume_then_attaches_to_new_droplet() -> None:
    api = FakeAPI()
    launcher = _launcher(api)
    sandbox_id = launcher.provision("managed-a1b2c3d4")

    workspace = launcher.start_host(
        sandbox_id,
        token="scoped-host-token",
        host_id="a" * 32,
        host_name="managed-a1b2c3d4",
        server_url="https://omnigent.example.com",
        repo_url="https://github.com/example/repo.git",
        repo_name="repo",
    )

    assert workspace == "/workspace/repo"
    assert any(call[0] == "create_volume" for call in api.calls)
    assert ("attach_volume", "volume-1", 42, "sgp1") in api.calls
    script = _decode_bootstrap(api.user_data)
    assert "scoped-host-token" in script
    assert "DIGITALOCEAN_TOKEN" not in script
    assert "OPENAI_API_KEY" not in script
    assert "GIT_TOKEN" not in script
    assert "ghcr.io/omnigent-ai/omnigent-host:latest" in script


def test_suspend_shuts_down_detaches_and_deletes_only_compute() -> None:
    volume = _volume()
    volume["droplet_ids"] = [42]
    tag = "omnigent-sandbox-managed-a1b2c3d4"
    api = FakeAPI(volume=volume)
    api.droplets = [
        {
            "id": 42,
            "name": "omnigent-host-managed-a1b2c3d4",
            "region": {"slug": "sgp1"},
            "status": "active",
            "tags": ["omnigent-managed", tag],
        }
    ]

    _launcher(api).suspend("managed-a1b2c3d4")

    assert ("shutdown_droplet", 42) in api.calls
    assert ("detach_volume", "volume-1", 42, "sgp1") in api.calls
    assert ("delete_droplet", 42) in api.calls
    assert not any(call[0] == "delete_volume" for call in api.calls)
    assert api.volume is volume


def test_partial_suspend_failure_preserves_volume_and_does_not_delete_compute() -> None:
    volume = _volume()
    volume["droplet_ids"] = [42]
    tag = "omnigent-sandbox-managed-a1b2c3d4"
    api = FakeAPI(volume=volume)
    api.detach_error = DigitalOceanAPIError("detach failed")
    api.droplets = [
        {
            "id": 42,
            "name": "omnigent-host-managed-a1b2c3d4",
            "region": {"slug": "sgp1"},
            "status": "active",
            "tags": ["omnigent-managed", tag],
        }
    ]

    with pytest.raises(click.ClickException, match="detach failed"):
        _launcher(api).suspend("managed-a1b2c3d4")

    assert api.volume is volume
    assert not any(call[0] in {"delete_droplet", "delete_volume"} for call in api.calls)


def test_resume_reuses_same_volume_without_creating_compute_early() -> None:
    volume = _volume()
    api = FakeAPI(volume=volume)

    _launcher(api).resume("managed-a1b2c3d4")

    assert api.volume is volume
    assert not any(call[0] in {"create_volume", "create_droplet"} for call in api.calls)


def test_resume_replaces_partial_compute_then_reuses_volume() -> None:
    volume = _volume()
    volume["droplet_ids"] = [41]
    tag = "omnigent-sandbox-managed-a1b2c3d4"
    api = FakeAPI(volume=volume)
    api.droplets = [
        {
            "id": 41,
            "name": "omnigent-host-managed-a1b2c3d4",
            "region": {"slug": "sgp1"},
            "status": "off",
            "tags": ["omnigent-managed", tag],
        }
    ]
    launcher = _launcher(api)

    launcher.resume("managed-a1b2c3d4")
    workspace = launcher.start_host(
        "managed-a1b2c3d4",
        token="fresh-token",
        host_id="c" * 32,
        host_name="managed-a1b2c3d4",
        server_url="https://omnigent.example.com",
    )

    assert workspace == "/workspace"
    assert ("detach_volume", "volume-1", 41, "sgp1") in api.calls
    assert ("delete_droplet", 41) in api.calls
    assert ("attach_volume", "volume-1", 42, "sgp1") in api.calls
    assert "fresh-token" in _decode_bootstrap(api.user_data)


def test_permanent_delete_removes_compute_and_owned_volume() -> None:
    volume = _volume()
    tag = "omnigent-sandbox-managed-a1b2c3d4"
    api = FakeAPI(volume=volume)
    api.droplets = [
        {
            "id": 42,
            "name": "omnigent-host-managed-a1b2c3d4",
            "region": {"slug": "sgp1"},
            "status": "active",
            "tags": ["omnigent-managed", tag],
        }
    ]

    _launcher(api).terminate("managed-a1b2c3d4")

    assert ("delete_droplet", 42) in api.calls
    assert ("delete_volume", "volume-1") in api.calls
    assert api.volume is None


def test_unowned_volume_is_never_used_or_deleted() -> None:
    volume = _volume()
    volume["tags"] = ["somebody-else"]
    api = FakeAPI(volume=volume)

    with pytest.raises(click.ClickException, match="ownership metadata"):
        _launcher(api).terminate("managed-a1b2c3d4")

    assert not any(call[0].startswith("delete_") for call in api.calls)


def test_attach_failure_cleans_up_droplet_but_preserves_volume() -> None:
    volume = _volume()
    api = FakeAPI(volume=volume)
    api.attach_error = DigitalOceanAPIError("attach failed")

    with pytest.raises(click.ClickException, match="attach failed"):
        _launcher(api).start_host(
            "managed-a1b2c3d4",
            token="token",
            host_id="b" * 32,
            host_name="managed-a1b2c3d4",
            server_url="https://omnigent.example.com",
        )

    assert ("delete_droplet", 42) in api.calls
    assert not any(call[0] == "delete_volume" for call in api.calls)
    assert api.volume is volume


def test_attach_and_cleanup_failure_reports_both_and_preserves_volume() -> None:
    volume = _volume()
    api = FakeAPI(volume=volume)
    api.attach_error = DigitalOceanAPIError("attach failed")
    api.delete_droplet_error = DigitalOceanAPIError("delete failed")

    with pytest.raises(click.ClickException, match=r"attach failed.*cleanup.*delete failed"):
        _launcher(api).start_host(
            "managed-a1b2c3d4",
            token="token",
            host_id="d" * 32,
            host_name="managed-a1b2c3d4",
            server_url="https://omnigent.example.com",
        )

    assert api.volume is volume
    assert not any(call[0] == "delete_volume" for call in api.calls)


def test_api_401_is_sanitized_and_does_not_include_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"id": "unauthorized", "message": "bad token"})

    token = "super-secret-digitalocean-token"
    client = httpx.Client(
        base_url="https://api.digitalocean.test/v2",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {token}"},
    )
    api = DigitalOceanAPI(token, client=client, sleep=lambda _seconds: None)

    with pytest.raises(DigitalOceanAPIError) as error:
        api.find_volume("workspace", "sgp1")
    assert "401" in str(error.value)
    assert token not in str(error.value)


def test_api_rate_limit_retries_with_bounded_backoff() -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"message": "rate limit"},
                headers={"Retry-After": "1"},
            )
        return httpx.Response(200, json={"volumes": []})

    client = httpx.Client(
        base_url="https://api.digitalocean.test/v2", transport=httpx.MockTransport(handler)
    )
    api = DigitalOceanAPI("token", client=client, sleep=sleeps.append)

    assert api.find_volume("workspace", "sgp1") is None
    assert attempts == 2
    assert sleeps == [1.0]


def test_shutdown_posts_action_and_waits_for_completion() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/droplets/42/actions"):
            return httpx.Response(201, json={"action": {"id": 7, "status": "in-progress"}})
        if request.url.path.endswith("/actions/7"):
            return httpx.Response(200, json={"action": {"id": 7, "status": "completed"}})
        return httpx.Response(200, json={"droplet": {"id": 42, "status": "off"}})

    client = httpx.Client(
        base_url="https://api.digitalocean.test/v2", transport=httpx.MockTransport(handler)
    )
    api = DigitalOceanAPI("token", client=client, sleep=lambda _seconds: None)

    api.shutdown_droplet(42)

    assert [(request.method, request.url.path) for request in requests] == [
        ("POST", "/v2/droplets/42/actions"),
        ("GET", "/v2/actions/7"),
        ("GET", "/v2/droplets/42"),
    ]
    assert requests[0].read() == b'{"type":"shutdown"}'


def test_api_timeout_retries_then_reports_operation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = httpx.Client(
        base_url="https://api.digitalocean.test/v2", transport=httpx.MockTransport(handler)
    )
    api = DigitalOceanAPI("token", client=client, sleep=lambda _seconds: None)

    with pytest.raises(DigitalOceanAPIError, match="find volume"):
        api.find_volume("workspace", "sgp1")


def test_droplet_poll_timeout_is_bounded() -> None:
    client = httpx.Client(
        base_url="https://api.digitalocean.test/v2",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"droplet": {"id": 42, "status": "new"}})
        ),
    )
    api = DigitalOceanAPI("token", client=client, sleep=lambda _seconds: None)

    with pytest.raises(DigitalOceanAPIError, match="timed out"):
        api.wait_droplet_active(42, timeout_s=0)
