"""Tests for the shared on-disk model-catalog store (model-flows design §1.2)."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from omnigent import model_catalog_store as store

_ROWS = [
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"},
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": True,
    },
]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))


def test_write_then_read_round_trips_verbatim() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_fingerprint_mismatch_is_a_miss_never_a_close_hit() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc124") is None
    assert store.read_catalog("codex-native", "abc123") is None


def test_damaged_file_reads_as_a_miss() -> None:
    path = store.catalog_path("claude-native", "abc123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert store.read_catalog("claude-native", "abc123") is None


def test_rows_without_ids_are_dropped_on_read() -> None:
    store.write_catalog("claude-native", "abc123", [*_ROWS, {"displayName": "no id"}])
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_default_row_and_membership_helpers() -> None:
    assert store.default_row(_ROWS) == _ROWS[1]
    assert store.default_row([_ROWS[0]]) is None
    assert store.catalog_contains(_ROWS, "sonnet")
    assert store.catalog_contains(_ROWS, "claude-opus-4-8[1m]")
    assert not store.catalog_contains(_ROWS, "haiku")


def test_catalog_age_reports_and_misses() -> None:
    assert store.catalog_age_s("claude-native", "abc123") is None
    store.write_catalog("claude-native", "abc123", _ROWS)
    age = store.catalog_age_s("claude-native", "abc123")
    assert age is not None and age >= 0.0


def _age_entry(harness: str, fingerprint: str, age_s: float) -> None:
    """
    Backdate a stored catalog file's mtime by *age_s* seconds.
    """
    path = store.catalog_path(harness, fingerprint)
    old = time.time() - age_s
    os.utime(path, (old, old))


def test_catalog_is_stale_truth_table() -> None:
    assert store.catalog_is_stale("claude-native", "abc123") is False
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.catalog_is_stale("claude-native", "abc123") is False
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    assert store.catalog_is_stale("claude-native", "abc123") is True


async def test_ensure_catalog_fresh_hit_never_probes() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return [{"id": "new"}]

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    assert probes == []


async def test_ensure_catalog_stale_hit_serves_now_and_refreshes_in_background() -> None:
    """
    A stale entry still answers instantly; the re-probe converges the store.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    refreshed = [{"id": "sonnet", "model": "claude-sonnet-6", "isDefault": True}]
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return refreshed

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    task = store._inflight.get(("claude-native", "abc123"))
    assert task is not None, "a stale hit must kick a background refresh"
    await task
    assert probes == [1]
    assert store.read_catalog("claude-native", "abc123") == refreshed
    assert store.catalog_is_stale("claude-native", "abc123") is False
    # The refreshed entry is fresh again: the next read is a plain hit.
    assert await store.ensure_catalog("claude-native", "abc123", _probe) == refreshed
    assert probes == [1]


async def test_ensure_catalog_stale_refresh_failure_keeps_serving() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)

    async def _probe() -> list[dict[str, object]]:
        raise OSError("provider unreachable")

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    task = store._inflight.get(("claude-native", "abc123"))
    assert task is not None
    await task
    # The stale rows keep serving; nothing crashed and nothing was clobbered.
    assert store.read_catalog("claude-native", "abc123") == _ROWS
    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS


async def test_reprobe_catalog_joins_the_background_probe_and_persists() -> None:
    """
    An awaited refresh reuses the stale hit's in-flight probe and returns its rows.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)
    _age_entry("claude-native", "abc123", store.CATALOG_STALE_AFTER_S + 60)
    refreshed = [{"id": "haiku", "model": "claude-haiku-4-5", "isDefault": True}]
    probes: list[int] = []

    async def _probe() -> list[dict[str, object]]:
        probes.append(1)
        return refreshed

    assert await store.ensure_catalog("claude-native", "abc123", _probe) == _ROWS
    assert await store.reprobe_catalog("claude-native", "abc123", _probe) == refreshed
    assert probes == [1], "the awaited refresh must join the probe already in flight"
    assert store.read_catalog("claude-native", "abc123") == refreshed
    assert store.catalog_is_stale("claude-native", "abc123") is False


async def test_reprobe_catalog_failure_returns_none_and_keeps_serving() -> None:
    """
    A failed awaited refresh reports ``None`` and leaves the stored rows alone.
    """
    store.write_catalog("claude-native", "abc123", _ROWS)

    async def _probe() -> list[dict[str, object]]:
        raise OSError("provider unreachable")

    assert await store.reprobe_catalog("claude-native", "abc123", _probe) is None
    assert store.read_catalog("claude-native", "abc123") == _ROWS


# ── binary_identity ──────────────────────────────────────


def test_binary_identity_is_none_without_a_resolvable_command() -> None:
    """An unresolvable binary keys exactly as it did before this facet.

    Returning ``None`` keeps the fingerprint stable, so a probe that
    cannot name its executable never invalidates a stored catalog.
    """
    assert store.binary_identity(None) is None
    assert store.binary_identity("") is None
    assert store.binary_identity("/nonexistent/claude") is None


def test_binary_identity_follows_a_version_symlink_to_its_target(tmp_path: Path) -> None:
    """Two links to one release share an identity; a new release does not.

    Claude Code keeps each release in its own directory and moves a
    symlink, so the resolved target is what changes on upgrade.
    """
    old_release = tmp_path / "2.1.247"
    new_release = tmp_path / "2.1.250"
    old_release.write_text("old")
    new_release.write_text("newer build")
    link = tmp_path / "claude"
    link.symlink_to(old_release)

    before = store.binary_identity(str(link))
    assert before is not None
    assert before[0] == str(old_release.resolve())

    link.unlink()
    link.symlink_to(new_release)
    after = store.binary_identity(str(link))

    assert after is not None
    assert after != before


def test_binary_identity_notices_a_binary_replaced_in_place(tmp_path: Path) -> None:
    """Installs that overwrite one path still register as a new binary."""
    binary = tmp_path / "claude"
    binary.write_text("old")
    before = store.binary_identity(str(binary))

    binary.write_text("a longer, newer build")
    os.utime(binary, (0, 0))
    after = store.binary_identity(str(binary))

    assert before is not None
    assert after is not None
    assert after != before


def test_binary_identity_resolves_a_bare_name_through_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare name is looked up on PATH, the way a launch spawns it."""
    binary = tmp_path / "claude"
    binary.write_text("build")
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))

    identity = store.binary_identity("claude")

    assert identity is not None
    assert identity[0] == str(binary.resolve())
