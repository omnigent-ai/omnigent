"""Tests for Docker managed-sandbox orphan reaping."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from omnigent.server.docker_sandbox_reaper import reap_docker_sandboxes_once


@dataclass
class _Container:
    id: str
    labels: dict[str, str]
    removed: bool = False

    def remove(self, force: bool = False) -> None:
        assert force is True
        self.removed = True


@dataclass
class _Containers:
    containers: list[_Container]
    seen_filters: dict | None = None

    def list(self, all: bool = False, filters: dict | None = None):
        self.seen_filters = filters
        return self.containers


@dataclass
class _Client:
    containers: _Containers


@dataclass
class _HostStore:
    ids: set[str] = field(default_factory=set)

    def list_managed_sandbox_ids(self, provider: str) -> set[str]:
        assert provider == "docker"
        return set(self.ids)


def test_reaper_removes_only_unbacked_containers_past_grace() -> None:
    old = str(int(time.time()) - 3600)
    backed = _Container("backed", {"omnigent.created_at": old})
    orphan = _Container("orphan", {"omnigent.created_at": old})
    recent = _Container("recent", {"omnigent.created_at": str(int(time.time()))})
    client = _Client(_Containers([backed, orphan, recent]))
    store = _HostStore({"backed"})

    removed = reap_docker_sandboxes_once(client=client, host_store=store, grace_s=60)

    assert removed == ["orphan"]
    assert backed.removed is False
    assert orphan.removed is True
    assert recent.removed is False
    # Reaper must filter on the managed+docker labels.
    assert client.containers.seen_filters == {
        "label": ["omnigent.managed=1", "omnigent.provider=docker"]
    }


def test_reaper_handles_missing_created_at_label_as_zero() -> None:
    orphan = _Container("orphan", {})  # no created_at → treated as epoch 0 (past grace)
    client = _Client(_Containers([orphan]))
    store = _HostStore(set())

    removed = reap_docker_sandboxes_once(client=client, host_store=store, grace_s=60)

    assert removed == ["orphan"]
    assert orphan.removed is True


def test_reaper_swallows_remove_errors() -> None:
    class _Boom(_Container):
        def remove(self, force: bool = False) -> None:
            raise RuntimeError("locked")

    boom = _Boom("boom", {"omnigent.created_at": "0"})
    client = _Client(_Containers([boom]))
    store = _HostStore(set())

    # A remove failure is logged, not raised, and the id is not reported.
    removed = reap_docker_sandboxes_once(client=client, host_store=store, grace_s=60)
    assert removed == []
