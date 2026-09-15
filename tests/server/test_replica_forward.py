"""Unit tests for the replica advertise-URL derivation.

The advertised URL is what peer replicas dial to forward a mis-routed
session request, so a wrong derivation either disables the self-heal
(``None``) or forwards to an address nobody serves.
"""

from __future__ import annotations

import pytest

from omnigent.server.replica_forward import (
    REPLICA_ADVERTISE_URL_ENV,
    derive_replica_advertise_url,
)


@pytest.fixture(autouse=True)
def _clear_advertise_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate every test from an ambient operator override.

    :param monkeypatch: Pytest patcher (auto-reverted per test).
    """
    monkeypatch.delenv(REPLICA_ADVERTISE_URL_ENV, raising=False)


def test_env_override_wins_and_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator override beats derivation, minus a trailing slash."""
    monkeypatch.setenv(REPLICA_ADVERTISE_URL_ENV, "https://replica-3.mesh.internal/")
    assert derive_replica_advertise_url("0.0.0.0", 8000) == "https://replica-3.mesh.internal"


def test_concrete_bind_host_is_used_verbatim() -> None:
    """A non-wildcard bind address is reachable as-is."""
    assert derive_replica_advertise_url("10.4.5.6", 8123) == "http://10.4.5.6:8123"


def test_loopback_bind_host_is_kept() -> None:
    """Loopback stays loopback — multi-replica-on-one-machine (and the
    single local server, where forwarding never triggers) both resolve
    correctly through it."""
    assert derive_replica_advertise_url("127.0.0.1", 9001) == "http://127.0.0.1:9001"


def test_wildcard_bind_derives_primary_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wildcard bind advertises the routable primary IP instead."""
    monkeypatch.setattr("omnigent.server.replica_forward._primary_local_ip", lambda: "10.9.8.7")
    assert derive_replica_advertise_url("0.0.0.0", 8000) == "http://10.9.8.7:8000"


def test_wildcard_bind_without_route_disables_forwarding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No derivable address means no advertised URL (forwarding stays off)."""
    monkeypatch.setattr("omnigent.server.replica_forward._primary_local_ip", lambda: None)
    assert derive_replica_advertise_url("0.0.0.0", 8000) is None


def test_bare_ipv6_bind_host_is_bracketed() -> None:
    """A bare IPv6 literal must be bracketed to form a valid URL."""
    assert derive_replica_advertise_url("fd00::7", 8000) == "http://[fd00::7]:8000"
