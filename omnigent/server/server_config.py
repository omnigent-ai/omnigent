"""Server-side YAML config for the non-CLI entrypoints.

The ``omnigent server`` CLI already takes ``-c/--config`` and reads a
YAML file (see ``omnigent/cli.py``). The hosted entrypoints —
``deploy/docker/entrypoint.py`` and ``deploy/databricks/src/app.py`` —
don't go through that CLI; they build the app directly from env vars.
This module gives those entrypoints the *same* config-file experience a
laptop gets from ``-c``, so a deployment can keep most of its settings
(admins, allowed domains, policy modules, artifact location, host/port,
database URI) in one file on the persistent volume instead of a pile of
env vars.

**Secrets stay in the environment, not this file.** ``DATABASE_URL``,
the session cookie secret, and the OIDC client secret are injected by
compose / ``bootstrap.sh`` / the platform — keeping them out of a
mounted YAML is deliberate (12-factor; the file is operator-editable
and often world-readable on the box). This config holds non-secret
*settings* only.

Resolution order for the config path:

1. ``OMNIGENT_CONFIG`` env var, if set (explicit path).
2. ``<data_dir>/config.yaml`` if it exists — ``<data_dir>`` is the same
   directory the admin list / credentials use (``/data`` in the Docker
   stack, ``~/.omnigent`` on a laptop; see
   :func:`omnigent.server.admin_list.resolve_data_dir`).
3. Otherwise ``None`` — no file, pure env config (back-compat: existing
   env-only deploys keep working unchanged).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

from omnigent.server.admin_list import resolve_data_dir

logger = logging.getLogger(__name__)


def resolve_config_path() -> Path | None:
    """Resolve the server config file path, or ``None`` if there is none.

    :returns: ``OMNIGENT_CONFIG`` if set; else ``<data_dir>/config.yaml``
        when that file exists; else ``None``.
    """
    explicit = os.environ.get("OMNIGENT_CONFIG", "").strip()
    if explicit:
        return Path(explicit)
    default = resolve_data_dir() / "config.yaml"
    return default if default.is_file() else None


def load_server_config() -> dict[str, Any]:
    """Load the resolved server config file into a dict.

    :returns: The parsed mapping, or an empty dict when no config file is
        resolved. A present-but-unreadable / malformed file logs a
        warning and returns ``{}`` rather than crashing startup — the
        entrypoint then falls back to env + defaults.
    """
    path = resolve_config_path()
    if path is None:
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("server config %s unreadable/invalid: %s — falling back to env", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("server config %s is not a mapping — ignoring", path)
        return {}
    logger.info("loaded server config from %s", path)
    return data


def config_str_list(value: Any) -> list[str]:
    """Coerce a config value into a list of non-empty strings.

    Accepts a YAML list (``["a", "b"]``) or a single scalar (``"a"``);
    anything else yields an empty list. Used for ``admins`` /
    ``allowed_domains`` so a one-entry value doesn't have to be a list.

    :param value: The raw config value, e.g. ``["alice@example.com"]``.
    :returns: A list of stripped, non-empty strings.
    """
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    return [str(item).strip() for item in items if str(item).strip()]


def _config_positive_int(key: str, default: int) -> int:
    """Read a positive-int setting from the server config, else *default*.

    A missing, non-numeric, or non-positive value falls back to *default*
    rather than crashing — the config file is operator-editable and a typo
    should degrade to the safe built-in limit, not take the server down.

    :param key: Top-level config key, e.g. ``"copy_max_files"``.
    :param default: Value used when the key is absent or invalid.
    :returns: The configured positive int, or *default*.
    """
    raw = load_server_config().get(key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("server config %s=%r is not an int — using default %d", key, raw, default)
        return default
    if value <= 0:
        logger.warning(
            "server config %s=%d is not positive — using default %d", key, value, default
        )
        return default
    return value


def copy_file_count_limit() -> int:
    """Max number of files a single copy-at-spawn request may copy.

    Config key ``copy_max_files``; defaults to
    :data:`omnigent.runtime.content_resolver.MAX_COPY_FILES`.
    """
    from omnigent.runtime.content_resolver import MAX_COPY_FILES

    return _config_positive_int("copy_max_files", MAX_COPY_FILES)


def copy_total_bytes_limit() -> int:
    """Max summed byte size a single copy-at-spawn request may copy.

    Config key ``copy_max_total_bytes``; defaults to
    :data:`omnigent.runtime.content_resolver.MAX_COPY_TOTAL_BYTES`.
    """
    from omnigent.runtime.content_resolver import MAX_COPY_TOTAL_BYTES

    return _config_positive_int("copy_max_total_bytes", MAX_COPY_TOTAL_BYTES)
