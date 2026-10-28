"""Choosing the host a remote-server session's runner launches on."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from omnigent_client import LaunchHost, OmnigentError, SessionsNamespace

import omnigent.host.identity as identity
from omnigent.remote_launch import (
    LaunchHostError,
    LocalHostCannotLaunchError,
    RemoteLaunchHostError,
    resolve_remote_launch_host,
)


def _sessions(hosts: list[LaunchHost], seen: dict[str, object]) -> SimpleNamespace:
    async def _list_launch_hosts(**kwargs: object) -> list[LaunchHost]:
        seen.update(kwargs)
        return hosts

    return SimpleNamespace(list_launch_hosts=_list_launch_hosts)


def _local_host(monkeypatch: pytest.MonkeyPatch, host_id: str | None) -> None:
    local = SimpleNamespace(host_id=host_id, name="me.local") if host_id else None
    monkeypatch.setattr(identity, "load_host_identity_if_present", lambda *_a, **_kw: local)


def _resolve(hosts: list[LaunchHost], seen: dict[str, object] | None = None) -> str | None:
    return asyncio.run(
        resolve_remote_launch_host(
            _sessions(hosts, seen if seen is not None else {}),  # type: ignore[arg-type]
            harness="claude-sdk",
        )
    )


def test_uses_this_machine_when_it_is_a_connected_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_host(monkeypatch, "host_local")
    seen: dict[str, object] = {}

    hosts = [LaunchHost("host_other", "devbox.local"), LaunchHost("host_local", "me.local")]

    assert _resolve(hosts, seen) == "host_local"
    assert seen["harness"] == "claude-sdk"


def test_no_connected_host_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_host(monkeypatch, "host_local")

    assert _resolve([]) is None


@pytest.mark.parametrize("local_id", [None, "host_local"])
def test_only_other_machines_connected_names_the_launchable_ones(
    monkeypatch: pytest.MonkeyPatch, local_id: str | None
) -> None:
    """Another machine can't use this directory as its workspace, so don't launch there."""
    _local_host(monkeypatch, local_id)
    hosts = [
        LaunchHost("host_a", "devbox.local"),
        LaunchHost("host_b", "ci.local"),
        LaunchHost("host_c", "old.local", launchable=False, unavailable_reason="binary-missing"),
    ]

    with pytest.raises(RemoteLaunchHostError) as excinfo:
        _resolve(hosts)

    assert excinfo.value.host_names == ["devbox.local", "ci.local"]
    message = str(excinfo.value)
    assert "devbox.local" in message and "ci.local" in message and "old.local" not in message
    assert "host --server" in message


def test_other_machines_that_cannot_run_the_agent_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _local_host(monkeypatch, None)
    hosts = [LaunchHost("host_a", "devbox.local", launchable=False, unavailable_reason="x")]

    assert _resolve(hosts) is None


def test_this_machine_connected_but_unable_to_run_the_harness_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Don't claim no host is connected when this machine's host is, but lacks the harness."""
    _local_host(monkeypatch, "host_local")
    hosts = [
        LaunchHost(
            "host_local", "me.local", launchable=False, unavailable_reason="binary-missing"
        ),
        LaunchHost("host_other", "devbox.local"),
    ]

    with pytest.raises(LocalHostCannotLaunchError) as excinfo:
        _resolve(hosts)

    message = str(excinfo.value)
    assert isinstance(excinfo.value, LaunchHostError)
    assert "claude-sdk" in message and "binary-missing" in message
    assert "devbox.local" not in message


def test_host_names_are_escaped_in_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    _local_host(monkeypatch, None)

    with pytest.raises(RemoteLaunchHostError) as excinfo:
        _resolve([LaunchHost("host_a", "evil\x1b]0;pwned\x07")])

    assert "\x1b" not in str(excinfo.value)
    assert "\\x1b" in str(excinfo.value)


@pytest.mark.asyncio
async def test_unsupported_host_listing_route_falls_back_to_no_host() -> None:
    """An older server without the hosts route retains the no-host fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/hosts"
        return httpx.Response(404, json={"detail": "Not Found"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert (
            await resolve_remote_launch_host(
                SessionsNamespace(http, "http://srv"), harness="claude-sdk"
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_host_listing_failure_reports_route_and_http_status(status_code: int) -> None:
    """SDK HTTP failures must not become advice to connect an absent host."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/hosts"
        return httpx.Response(status_code, json={"detail": "lookup failed"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(LaunchHostError) as excinfo:
            await resolve_remote_launch_host(SessionsNamespace(http, "http://srv"), harness=None)

    message = str(excinfo.value)
    assert "GET /v1/hosts" in message
    assert f"HTTP {status_code}" in message
    assert "host --server" not in message
    assert isinstance(excinfo.value.__cause__, OmnigentError)


def test_host_listing_failure_without_http_status_remains_an_error() -> None:
    """An unclassified SDK error is not evidence of an empty host list."""

    async def broken(**_kwargs: object) -> list[LaunchHost]:
        raise OmnigentError("lookup failed")

    sessions = SimpleNamespace(list_launch_hosts=broken)
    with pytest.raises(LaunchHostError, match="GET /v1/hosts failed"):
        asyncio.run(
            resolve_remote_launch_host(sessions, harness=None)  # type: ignore[arg-type]
        )
