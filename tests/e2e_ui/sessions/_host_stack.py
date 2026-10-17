"""Polling helpers for browser scenarios that drive a real host daemon."""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx


def wait_until(predicate: Callable[[], bool], *, timeout: float = 60.0) -> None:
    """Poll ``predicate`` every 200 ms until it holds or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"condition was not met within {timeout}s")


def list_hosts(client: httpx.Client) -> list[dict]:
    """Return the host rows from ``GET /v1/hosts``."""
    response = client.get("/v1/hosts")
    response.raise_for_status()
    return response.json()["hosts"]


def wait_for_online_host(client: httpx.Client, *, timeout: float = 60.0) -> str:
    """Return the id of the one host once it reports ``online``."""
    online: list[dict] = []

    def one_online() -> bool:
        online[:] = [host for host in list_hosts(client) if host["status"] == "online"]
        return len(online) == 1

    wait_until(one_online, timeout=timeout)
    return online[0]["host_id"]
