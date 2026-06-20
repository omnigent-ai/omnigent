"""Goose readiness + config reporting for ``omnigent setup``.

Unlike :mod:`omnigent.onboarding.cursor_auth`, Omnigent manages **no** Goose
credentials: Goose owns its own auth via ``goose configure`` (keyring or
``~/.config/goose/config.yaml``). This module is a thin, read-only reporter —
it confirms the ``goose`` binary is installed and surfaces the configured
provider/model so setup can show goose-native as ready (and which model it will
drive) without ever touching Goose's secrets.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import GOOSE_KEY, harness_cli_installed


def goose_cli_installed() -> bool:
    """Return whether the ``goose`` binary is on ``PATH``."""
    return harness_cli_installed(GOOSE_KEY)


def goose_config_path() -> Path:
    """Return Goose's config file path for this process's HOME.

    Honors ``XDG_CONFIG_HOME``; defaults to ``~/.config/goose/config.yaml``.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "goose" / "config.yaml"


@dataclass(frozen=True)
class GooseConfigSummary:
    """What setup needs to know about the local Goose configuration.

    :param installed: ``goose`` binary present on ``PATH``.
    :param provider: Configured ``GOOSE_PROVIDER`` (env override wins over the
        config file), or ``None`` if neither is set.
    :param model: Configured ``GOOSE_MODEL`` (env override wins), or ``None``.
    """

    installed: bool
    provider: str | None
    model: str | None

    @property
    def ready(self) -> bool:
        """Launchable when the binary is present (Goose resolves its own auth)."""
        return self.installed


def _config_value(key: str) -> str | None:
    """Read *key* from the Goose config file (top-level scalar), or ``None``.

    Deliberately a minimal, dependency-light scan: Goose stores ``GOOSE_PROVIDER``
    / ``GOOSE_MODEL`` as top-level YAML scalars. A parse failure or missing file
    returns ``None`` (best-effort reporting, never raises).
    """
    path = goose_config_path()
    try:
        import yaml

        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def goose_config_summary() -> GooseConfigSummary:
    """Summarize the local Goose configuration for setup display.

    The env vars ``GOOSE_PROVIDER`` / ``GOOSE_MODEL`` override the config file
    (matching Goose's own precedence), so a per-shell override is reflected.
    """
    provider = os.environ.get("GOOSE_PROVIDER", "").strip() or _config_value("GOOSE_PROVIDER")
    model = os.environ.get("GOOSE_MODEL", "").strip() or _config_value("GOOSE_MODEL")
    return GooseConfigSummary(
        installed=goose_cli_installed(),
        provider=provider,
        model=model,
    )
