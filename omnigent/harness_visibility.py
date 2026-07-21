"""User-configurable visibility filtering for harness catalog rows."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from omnigent.config import load_global_config
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import harness_catalog

DISMISSED_HARNESSES_CONFIG_KEY = "dismissed_harnesses"


def _as_str_list(value: Any) -> list[str]:  # type: ignore[explicit-any]
    """Coerce a YAML scalar/list into stripped string values."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def catalog_harness_ids(rows: Sequence[Mapping[str, Any]] | None = None) -> frozenset[str]:  # type: ignore[explicit-any]
    """Return the harness ids currently present in the web catalog."""
    catalog_rows = rows if rows is not None else harness_catalog()
    return frozenset(str(row.get("id", "")).strip() for row in catalog_rows if row.get("id"))


def resolve_catalog_harness_id(
    harness_id: str,
    rows: Sequence[Mapping[str, Any]] | None = None,  # type: ignore[explicit-any]
) -> str | None:
    """Resolve a user-supplied harness id/alias to a visible catalog id.

    :param harness_id: Harness id or accepted alias, e.g. ``"claude"``.
    :param rows: Optional catalog rows to validate against.
    :returns: The catalog id to persist, or ``None`` if the value is not a
        known web-catalog harness.
    """
    requested = harness_id.strip()
    if not requested:
        return None
    ids = catalog_harness_ids(rows)
    if requested in ids:
        return requested
    canonical = canonicalize_harness(requested)
    if canonical in ids:
        return canonical
    return None


def dismissed_harness_ids(
    config: Mapping[str, Any] | None = None,  # type: ignore[explicit-any]
    rows: Sequence[Mapping[str, Any]] | None = None,  # type: ignore[explicit-any]
) -> frozenset[str]:
    """Return valid dismissed harness catalog ids from config.

    Unknown config entries are ignored so a typo or stale value never breaks the
    ``GET /v1/harnesses`` response.
    """
    cfg = load_global_config() if config is None else config
    resolved: set[str] = set()
    for value in _as_str_list(cfg.get(DISMISSED_HARNESSES_CONFIG_KEY)):
        catalog_id = resolve_catalog_harness_id(value, rows)
        if catalog_id is not None:
            resolved.add(catalog_id)
    return frozenset(resolved)


def filtered_harness_catalog(config: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:  # type: ignore[explicit-any]
    """Return web-catalog rows after applying ``dismissed_harnesses``."""
    rows = harness_catalog()
    dismissed = dismissed_harness_ids(config, rows)
    if not dismissed:
        return rows
    return [row for row in rows if row.get("id") not in dismissed]


__all__ = [
    "DISMISSED_HARNESSES_CONFIG_KEY",
    "catalog_harness_ids",
    "dismissed_harness_ids",
    "filtered_harness_catalog",
    "resolve_catalog_harness_id",
]
