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
from dataclasses import dataclass
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


# ── Manager webhook (OMN-104) ──────────────────────────


class ManagerWebhookConfigError(RuntimeError):
    """Raised when ``manager_webhook`` config is invalid — fails server startup closed."""


@dataclass(frozen=True)
class ManagerWebhookConfig:
    """
    Resolved ``manager_webhook`` config block.

    :param enabled: Whether the dispatcher should attempt delivery. The
        dispatcher task itself always starts (see
        ``omnigent/server/manager_webhook_dispatcher.py``); this only gates
        whether it claims rows, so flipping it via config reload needs no
        server restart.
    :param endpoint: HTTPS URL to POST lifecycle events to. Required (and
        HTTPS-enforced, see :func:`manager_webhook_config`) when *enabled*.
    :param key_id: Opaque key identifier sent as ``X-Omnigent-Key-Id`` so the
        receiver can select the right verification secret during rotation.
    """

    enabled: bool
    endpoint: str | None
    key_id: str | None


def manager_webhook_config() -> ManagerWebhookConfig:
    """
    Resolve the ``manager_webhook`` config block.

    Non-secret settings only — the signing secret is env-var only (see
    ``omnigent/server/manager_webhook_signing.py``), never this file, per
    this module's own secrets-stay-in-the-environment convention.

    Fails closed at config-load time (raises rather than warns) when
    *enabled* but the endpoint is missing or not HTTPS, mirroring how other
    security-relevant settings in this codebase refuse to boot on
    misconfiguration rather than silently running insecurely. The
    ``allow_insecure_dev_endpoint: true`` escape hatch is an explicit,
    local-dev-only override (e.g. ``http://localhost:...`` against a fake
    receiver in a dev loop).

    :returns: The resolved config. ``enabled=False`` (the default) when the
        block is absent — existing session-state correctness is unaffected
        either way; this only controls whether the dispatcher delivers.
    :raises ManagerWebhookConfigError: If *enabled* but misconfigured
        (missing endpoint, or a non-HTTPS endpoint without the explicit
        dev override).
    """
    raw = load_server_config().get("manager_webhook") or {}
    if not isinstance(raw, dict):
        raise ManagerWebhookConfigError("server config manager_webhook must be a mapping")
    enabled = bool(raw.get("enabled", False))
    endpoint_raw = raw.get("endpoint")
    endpoint = str(endpoint_raw).strip() if endpoint_raw else None
    key_id_raw = raw.get("key_id")
    key_id = str(key_id_raw).strip() if key_id_raw else None
    allow_insecure = bool(raw.get("allow_insecure_dev_endpoint", False))
    if enabled:
        if not endpoint:
            raise ManagerWebhookConfigError(
                "manager_webhook.enabled is true but manager_webhook.endpoint is not set"
            )
        if not endpoint.startswith("https://") and not (
            allow_insecure and endpoint.startswith("http://")
        ):
            raise ManagerWebhookConfigError(
                f"manager_webhook.endpoint {endpoint!r} must be HTTPS "
                "(set allow_insecure_dev_endpoint: true to override for local dev only)"
            )
    return ManagerWebhookConfig(enabled=enabled, endpoint=endpoint, key_id=key_id)
