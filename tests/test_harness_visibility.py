from __future__ import annotations

from omnigent.harness_visibility import (
    dismissed_harness_ids,
    filtered_harness_catalog,
    resolve_catalog_harness_id,
)


def test_dismissed_harness_ids_resolves_aliases_and_ignores_unknown() -> None:
    rows = [
        {"id": "claude-sdk", "label": "Claude SDK"},
        {"id": "codex", "label": "Codex"},
    ]

    dismissed = dismissed_harness_ids(
        {"dismissed_harnesses": ["claude", "not-a-harness"]},
        rows,
    )

    assert dismissed == frozenset({"claude-sdk"})


def test_filtered_harness_catalog_preserves_default_catalog() -> None:
    rows = filtered_harness_catalog({})

    assert any(row["id"] == "claude-sdk" for row in rows)
    assert any(row["id"] == "codex" for row in rows)


def test_filtered_harness_catalog_omits_valid_dismissed_entries() -> None:
    rows = filtered_harness_catalog({"dismissed_harnesses": ["claude-sdk"]})

    ids = {row["id"] for row in rows}
    assert "claude-sdk" not in ids
    assert "codex" in ids


def test_resolve_catalog_harness_id_accepts_aliases() -> None:
    rows = [{"id": "claude-sdk", "label": "Claude SDK"}]

    assert resolve_catalog_harness_id("claude", rows) == "claude-sdk"
    assert resolve_catalog_harness_id("not-a-harness", rows) is None
