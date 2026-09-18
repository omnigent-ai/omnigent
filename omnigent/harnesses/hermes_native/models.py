"""Pre-launch model options for the native Hermes harness.

Renders the launch-time picker catalog for a host's Hermes Agent: the default
model from ``~/.hermes/config.yaml`` (``model.default`` + ``model.provider``)
marked as the default choice, plus Hermes' own cached model catalog
(``~/.hermes/cache/model_catalog.json`` — the same file ``hermes model``
maintains) when present. Models from the catalog are qualified
``<provider>/<model_id>``; the configured default is surfaced first.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

_LISTING_TIMEOUT_SECONDS = 10.0
_MAX_CATALOG_MODELS = 400


def hermes_home_path(hermes_home: Path | None = None) -> Path:
    """Resolve the Hermes home directory."""
    configured = os.environ.get("HERMES_HOME")
    return hermes_home or (
        Path(configured).expanduser() if configured else Path.home() / ".hermes"
    )


def _load_yaml(path: Path) -> dict[str, Any] | None:
    try:
        loaded = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def hermes_native_model_options(
    *,
    hermes_home: Path | None = None,
) -> list[dict[str, object]]:
    """Return the pre-launch Hermes model choices for this host.

    The configured default (``model.default`` under ``model.provider``) is
    always present and marked ``default``; Hermes' cached picker catalog adds
    the models ``hermes model`` itself would offer. An absent config or
    catalog yields an empty list (the picker falls back to no selection).
    """
    home = hermes_home_path(hermes_home)
    config = _load_yaml(home / "config.yaml")

    options: dict[str, dict[str, object]] = {}

    if config is not None:
        model_block = config.get("model")
        if isinstance(model_block, dict):
            default_model = str(model_block.get("default") or "").strip()
            default_provider = str(model_block.get("provider") or "").strip()
            if default_model:
                qualified = (
                    f"{default_provider}/{default_model}" if default_provider else default_model
                )
                options[qualified] = {
                    "id": qualified,
                    "model": qualified,
                    "displayName": default_model,
                    "default": True,
                }

    catalog = _load_json(home / "cache" / "model_catalog.json")
    if catalog is not None:
        providers = catalog.get("providers")
        if isinstance(providers, dict):
            for provider_id, payload in providers.items():
                entries = payload.get("models") if isinstance(payload, dict) else None
                if not isinstance(entries, list):
                    continue
                for entry in entries[:_MAX_CATALOG_MODELS]:
                    model_id = (
                        str(entry.get("id") or "").strip() if isinstance(entry, dict) else ""
                    )
                    if not model_id:
                        continue
                    qualified = f"{provider_id}/{model_id}"
                    if qualified in options:
                        continue
                    options[qualified] = {
                        "id": qualified,
                        "model": qualified,
                        "displayName": model_id,
                    }

    return [options[key] for key in sorted(options)]


__all__: list[str] = [
    "hermes_home_path",
    "hermes_native_model_options",
]
