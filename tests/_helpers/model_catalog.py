"""Seed an offline MLflow model catalog for one spawned ``omnigent server``: the suite
disables catalog lookups, so a test needing a priceable model seeds a private cache dir
and hands the returned env to its own server subprocess only."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.onboarding import providers

# The catalog cache ignores ``XDG_CACHE_HOME`` on macOS, where a seeded catalog
# would land in the developer's real cache.
offline_catalog_isolated = pytest.mark.skipif(
    sys.platform == "darwin",
    reason="the model-catalog cache cannot be redirected on macOS",
)


def seed_offline_catalog(
    cache_dir: Path, provider: str, models: dict[str, dict[str, Any]]
) -> dict[str, str]:
    """Write a *provider* catalog under *cache_dir*; return the env a server needs to read it."""
    catalog = {"schema_version": "1.0", "models": models}
    source_url = providers._catalog_source_url(provider)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("XDG_CACHE_HOME", str(cache_dir))
        providers._write_disk_catalog(
            providers._DiskCatalogEntry(
                catalog=catalog, source_url=source_url, fetched_at=time.time()
            ),
            provider,
        )
        assert providers._read_disk_catalog(provider, source_url) is not None, (
            f"seeded {provider} catalog did not round-trip through the disk cache"
        )
    return {"XDG_CACHE_HOME": str(cache_dir), "OMNIGENT_DISABLE_CATALOG_LOOKUP": "0"}
