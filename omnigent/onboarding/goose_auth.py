"""Goose readiness + config reporting for ``omnigent setup``.

Unlike :mod:`omnigent.onboarding.cursor_auth`, Omnigent manages **no** Goose
credentials: Goose owns its own auth via ``goose configure`` (keyring or
``~/.config/goose/config.yaml``). This module is a thin, read-only reporter —
it confirms the ``goose`` binary is installed and surfaces the configured
provider/model so setup can show Goose as ready (and which model it will drive)
without ever touching Goose's secrets.

Detection prefers Goose's **own** config resolution via ``goose info -v`` (which
prints a ``goose Configuration:`` block with ``GOOSE_PROVIDER`` / ``GOOSE_MODEL``
once a provider is configured, and omits it when not). That is authoritative
across platforms and config formats — ``goose configure`` may store provider/key
in the keyring and a config file whose exact path/shape differs by OS, so reading
``goose info -v`` is more reliable than hand-parsing ``config.yaml`` (the latter
remains a best-effort fallback when the binary can't be run).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from omnigent.onboarding.harness_install import GOOSE_KEY, harness_cli_installed

#: Override for the goose binary path (mirrors omnigent.goose_native).
_GOOSE_PATH_ENV = "OMNIGENT_GOOSE_PATH"
#: ``goose info -v`` is local-only (no network), so a short timeout is ample.
_INFO_TIMEOUT_S = 10.0


def goose_cli_installed() -> bool:
    """Return whether the ``goose`` binary is on ``PATH``."""
    return harness_cli_installed(GOOSE_KEY)


def _goose_binary() -> str | None:
    """Resolve the goose executable (``OMNIGENT_GOOSE_PATH`` override, else PATH)."""
    override = os.environ.get(_GOOSE_PATH_ENV, "").strip()
    if override:
        return override
    return shutil.which("goose")


def goose_info_config() -> tuple[str | None, str | None]:
    """Return ``(provider, model)`` from ``goose info -v``, or ``(None, None)``.

    Parses the ``goose Configuration:`` section Goose prints for its *own*
    resolved config (env overrides + config file + defaults). Returns
    ``(None, None)`` when the binary is absent, the command fails/times out, or
    no provider is configured (Goose omits the ``GOOSE_PROVIDER`` line then), so
    a caller can cleanly distinguish "configured" from "not".
    """
    binary = _goose_binary()
    if binary is None:
        return None, None
    try:
        proc = subprocess.run(
            [binary, "info", "-v"],
            capture_output=True,
            text=True,
            timeout=_INFO_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if proc.returncode != 0:
        return None, None
    provider: str | None = None
    model: str | None = None
    in_config = False
    for raw in proc.stdout.splitlines():
        stripped = raw.strip()
        if stripped.lower().startswith("goose configuration"):
            in_config = True
            continue
        if not in_config:
            continue
        if stripped.startswith("GOOSE_PROVIDER:"):
            provider = stripped.split(":", 1)[1].strip() or None
        elif stripped.startswith("GOOSE_MODEL:"):
            model = stripped.split(":", 1)[1].strip() or None
    return provider, model


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

    Resolution order, most authoritative first:

    1. ``GOOSE_PROVIDER`` / ``GOOSE_MODEL`` env (an explicit per-shell override),
    2. ``goose info -v`` — Goose's own resolved config (handles the keyring /
       config-file path + format across platforms; see :func:`goose_info_config`),
    3. a best-effort top-level scan of ``config.yaml`` (fallback when the binary
       can't be run, e.g. in unit tests).

    Using ``goose info -v`` is why a provider set via ``goose configure`` is now
    detected even when it isn't a plain top-level ``GOOSE_PROVIDER`` scalar in the
    file we'd guess at.
    """
    info_provider, info_model = goose_info_config()
    provider = (
        os.environ.get("GOOSE_PROVIDER", "").strip()
        or info_provider
        or _config_value("GOOSE_PROVIDER")
    )
    model = os.environ.get("GOOSE_MODEL", "").strip() or info_model or _config_value("GOOSE_MODEL")
    return GooseConfigSummary(
        installed=goose_cli_installed(),
        provider=provider,
        model=model,
    )
