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
