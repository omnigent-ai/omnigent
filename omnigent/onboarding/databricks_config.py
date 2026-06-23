"""Helpers for resolving local Databricks profile metadata."""

from __future__ import annotations

import configparser
import importlib.util
import logging
from pathlib import Path
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"


def normalize_workspace_url(raw: str) -> str:
    """Reduce a Databricks workspace URL to its bare ``scheme://host`` origin.

    Users routinely paste the URL straight from a browser address bar, which
    carries a path and query the workspace host does not — e.g.
    ``https://my-ws.cloud.databricks.com/browse?o=1234567890``. Both the
    ``~/.databrickscfg`` profile host and ``ucode configure --workspaces``
    need the bare origin: the Databricks CLI keys its OAuth token cache by
    host, so a path-laden value resolves to "no access token" and
    ``ucode configure`` then exits non-zero.

    :param raw: A workspace URL, possibly carrying a path/query/fragment
        and/or a trailing slash, e.g.
        ``"https://my-ws.cloud.databricks.com/browse?o=1"``.
    :returns: ``scheme://host`` with no path, query, fragment, or trailing
        slash (e.g. ``"https://my-ws.cloud.databricks.com"``). When *raw* has
        no parseable scheme+host (e.g. a bare ``"host/path"`` with no scheme),
        the input is returned trimmed of surrounding whitespace and a trailing
        slash — matching the prior ``rstrip("/")`` behavior so callers that
        pre-add a scheme never regress.
    """
    parsed = urlparse(raw.strip())
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return raw.strip().rstrip("/")


# The install command surfaced wherever a Databricks flow is gated on the
# `databricks` extra (the add-provider menu, `setup --internal-beta`).
# Matches the README's canonical `uv tool install` path. Dev clones use
# `uv sync --extra databricks` instead, but the tool install is the path
# end users actually took. The repo URL sits on its own line: the slug
# differs per distribution, and inlining it into the hint string would
# make the line's width — and therefore its ruff formatting — depend on
# which slug a checkout carries.
_SOURCE_REPO_URL = "https://github.com/omnigent-ai/omnigent.git"
DATABRICKS_EXTRA_INSTALL_HINT = (
    f'uv tool install --force "omnigent[databricks] @ git+{_SOURCE_REPO_URL}"'
)


def databricks_sdk_installed() -> bool:
    """Return whether ``databricks-sdk`` (the ``databricks`` extra) is present.

    The SDK is not part of the default install — it ships in the
    ``databricks`` (and ``all``) extras. The ``kind: databricks`` provider
    path needs it to mint workspace OAuth tokens at runtime
    (:mod:`omnigent.runtime.credentials.databricks`), so onboarding flows
    gate the Databricks option on this check and surface
    :data:`DATABRICKS_EXTRA_INSTALL_HINT` when it fails.

    Uses :func:`importlib.util.find_spec` so the check never pays the cost
    of actually importing the SDK.

    :returns: ``True`` when ``databricks.sdk`` is importable.
    """
    try:
        return importlib.util.find_spec("databricks.sdk") is not None
    except ModuleNotFoundError:
        # find_spec("databricks.sdk") imports the parent `databricks`
        # namespace package first; when even that is absent it raises
        # instead of returning None.
        return False


# Fallback Claude model for the Databricks AI gateway when neither the spec
# nor the workspace's ucode state names one. Must be a ``databricks-*``
# endpoint name — the gateway rejects Anthropic-direct ids like the CLI's
# own ``opus[1m]`` default.
DATABRICKS_CLAUDE_DEFAULT_MODEL = "databricks-claude-opus-4-8"


def list_databricks_profiles() -> list[str]:
    """Return the profile section names declared in ``~/.databrickscfg``.

    Used by ``omnigent setup --no-internal-beta`` to offer the user a pick-list
    when adding a ``kind: databricks`` provider, so they don't have to
    recall the exact profile name.

    :returns: Section names, e.g. ``["oss", "DEFAULT"]``. The ``DEFAULT``
        section is included only when it actually carries keys. Empty when
        the file is missing or unparseable.
    """
    if not _DATABRICKSCFG_PATH.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(_DATABRICKSCFG_PATH)
    except configparser.Error as exc:
        _logger.debug("Could not parse %s: %s", _DATABRICKSCFG_PATH, exc)
        return []
    sections = [s for s in parser.sections() if s != "DEFAULT"]
    if parser.defaults():
        sections.append("DEFAULT")
    return sections


def get_workspace_url_for_profile(profile: str) -> str | None:
    """Return the workspace host for a ``~/.databrickscfg`` profile.

    Reads the INI-style ``~/.databrickscfg`` directly with
    :mod:`configparser`, then falls back to Omnigent' built-in setup
    profile metadata for legacy names.

    :param profile: Profile section name, e.g. ``"<your-profile>"`` or
        ``"DEFAULT"``.
    :returns: The ``host`` value for the profile, stripped of trailing slash,
        or ``None`` when the profile cannot be resolved.
    """
    if _DATABRICKSCFG_PATH.exists():
        cfg = configparser.ConfigParser()
        try:
            cfg.read(_DATABRICKSCFG_PATH)
        except configparser.Error as exc:
            _logger.debug("Could not parse %s: %s", _DATABRICKSCFG_PATH, exc)
        else:
            host = None
            if cfg.has_section(profile):
                try:
                    host = cfg.get(profile, "host")
                except configparser.NoOptionError:
                    host = None
            elif profile.lower() == cfg.default_section.lower():
                host = cfg.defaults().get("host")
            if host:
                return host.rstrip("/")

    try:
        from omnigent.onboarding.internal_beta import DEFAULT_PROFILES
    except ModuleNotFoundError:
        # The internal-beta catalog is intentionally absent from the OSS
        # build; without it there are no bundled-profile fallbacks.
        return None

    for spec in DEFAULT_PROFILES:
        if spec.name == profile:
            return spec.host.rstrip("/")
    return None
