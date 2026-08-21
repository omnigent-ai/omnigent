"""Tests for the shared on-disk model-catalog store (model-flows design §1.2)."""

from __future__ import annotations

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
