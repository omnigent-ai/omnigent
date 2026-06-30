"""
Databricks workspace credential resolver.

This module provides a single entry point, :func:`resolve_databricks_workspace`,
that returns a populated :class:`WorkspaceCreds` (workspace host and bearer
token) by checking, in order:

1. The ``databricks-sdk``'s :class:`~databricks.sdk.config.Config`
   resolver. This handles every ``auth_type`` in ``~/.databrickscfg``
   (``pat``, ``databricks-cli`` / OAuth-U2M, service-principal OAuth,
   Azure CLI, env-OIDC, metadata-service, etc.) and mints a fresh
   bearer token via ``Config.authenticate()``. Critical for OAuth
   profiles whose cfg sections have NO static ``token`` field.
2. The named profile section in the Databricks config file via
   raw configparser (``~/.databrickscfg`` by default, or the path in
   ``DATABRICKS_CONFIG_FILE`` if that env var is set). Only finds
   creds when both ``host`` and ``token`` are present in plain text.
3. The ``[DEFAULT]`` section of the same config file via raw
   configparser, used either when no profile is requested or when
   the requested profile is missing.

If none of the above yield both a host and a token, the resolver raises
``OSError`` with a message that lists every source it tried.

Note: this resolver intentionally does NOT honor ``OPENAI_BASE_URL`` /
``OPENAI_API_KEY``. Those env vars are used by the *OpenAI* client paths
(serving endpoints, openai-agents harness) and ALREADY contain the full
serving-endpoints URL. Callers of this resolver need the bare workspace
host so they can append their own path (e.g. ``/serving-endpoints``);
reusing a full OpenAI base URL would produce malformed URLs, so we
resolve via the SDK and cfg file only.

The SDK-based path mirrors (but does not import from)
``omnigent/inner/databricks_executor.py:_read_databrickscfg``. The
configparser fallback mirrors ``_read_databrickscfg_file_fallback`` in
the same module. Once the ``inner/`` package is sunset (see
``designs/UNIFICATION.md``), both functions there should be deleted in
favor of calling this helper.

**v1 limitation (documented):** the SDK-minted token is fetched once
per call and then cached in the returned :class:`WorkspaceCreds`. OAuth
access tokens typically expire after ~1 hour. Long-running sessions
will therefore hit expiry mid-stream. v2 will refactor callers to
hold the SDK ``Config`` object and re-call ``authenticate()`` on
demand (which performs refresh-token handling transparently).
"""

from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass
from pathlib import Path

_logger = logging.getLogger(__name__)

# Default location of the Databricks CLI config file when
# ``DATABRICKS_CONFIG_FILE`` is not set in the environment.
DEFAULT_DATABRICKSCFG_PATH: Path = Path.home() / ".databrickscfg"

# Environment variable name recognized by this resolver. Kept as a
# constant so it appears once at the top of the module instead of
# being scattered through the resolution logic.
ENV_DATABRICKS_CONFIG_FILE: str = "DATABRICKS_CONFIG_FILE"

# Section name used by ConfigParser for the implicit default section.
DEFAULT_SECTION: str = "DEFAULT"


@dataclass(frozen=True)
class WorkspaceCreds:
    """
    Resolved Databricks workspace credentials.

    :param host: The workspace host URL with no trailing slash,
        e.g. ``"https://example.databricks.com"``. This is
        the BARE workspace host — callers append their own path
        (e.g. ``/serving-endpoints``) themselves.
    :param token: The bearer token used in the
        ``Authorization: Bearer <token>`` header.
    """

    host: str
    token: str


def _strip_trailing_slash(host: str) -> str:
    """
    Remove any trailing ``/`` characters from a workspace host string.

    Workspace URLs sometimes round-trip through tools that append a
    trailing slash. The Databricks model-serving gateway and the
    OpenAI client both expect no trailing slash on the base URL, so
    we normalize on resolution.

    :param host: A workspace host URL, possibly with one or more
        trailing slashes, e.g. ``"https://example.databricks.com/"``.
    :returns: The same URL with trailing slashes removed,
        e.g. ``"https://example.databricks.com"``.
    """
    return host.rstrip("/")


def _databrickscfg_path() -> Path:
    """
    Return the path to the Databricks config file.

    Honors the ``DATABRICKS_CONFIG_FILE`` environment variable, falling
    back to ``~/.databrickscfg`` when the env var is unset or empty.

    :returns: The :class:`pathlib.Path` to read the config file from.
        The file may or may not exist; callers must check.
    """
    override = os.environ.get(ENV_DATABRICKS_CONFIG_FILE)
    if override:
        return Path(override)
    return DEFAULT_DATABRICKSCFG_PATH


class _SectionAbsent(Exception):
    """
    Raised by :func:`_read_section` when a NAMED section does not
    exist in the cfg file at all.

    Distinguishing "section absent" (this exception — fail loud when
    a named profile was explicitly requested) from "section present
    but invalid" (:class:`_SectionPresentButInvalid`) closes the
    silent-fallback-to-``[DEFAULT]`` bug: a typo in a configured
    profile name would otherwise silently resolve against whatever
    ``[DEFAULT]`` points to — a completely different workspace.
    """


class _SectionPresentButInvalid(Exception):
    """
    Raised by :func:`_read_section` when a named section EXISTS in
    the cfg file but is missing ``host`` or ``token``.

    Distinguishing "section absent" (:class:`_SectionAbsent` — fail
    loud when a profile was explicitly named) from "section present
    but invalid" (this exception — also fail loud) prevents the
    silent-fallback bug where a malformed named profile would send
    the caller to a different workspace via ``[DEFAULT]``.
    """


def _read_section(config: configparser.ConfigParser, section: str) -> WorkspaceCreds | None:
    """
    Read ``host`` and ``token`` from a named section of a parsed config.

    Three outcomes for named (non-DEFAULT) sections:

    - Section absent → raises :class:`_SectionAbsent`.
    - Section present and complete → returns :class:`WorkspaceCreds`.
    - Section present but missing ``host`` or ``token`` → raises
      :class:`_SectionPresentButInvalid`.

    For ``DEFAULT``: absent or incomplete → returns ``None`` (no
    further fallback exists).

    :param config: A :class:`configparser.ConfigParser` already loaded
        from a ``.databrickscfg`` file.
    :param section: The section name to read, e.g. ``"dev"`` or
        ``"DEFAULT"``.
    :returns: A :class:`WorkspaceCreds` when the section exists AND
        has both fields, or ``None`` for an absent/incomplete
        ``[DEFAULT]``.
    :raises _SectionAbsent: When a named section does not exist.
    :raises _SectionPresentButInvalid: When the section exists but
        is missing ``host`` or ``token``.
    """
    # ConfigParser exposes DEFAULT via .defaults(); named sections via
    # __getitem__. Read via the appropriate API to avoid accidentally
    # inheriting DEFAULT keys into named sections.
    if section == DEFAULT_SECTION:
        values: dict[str, str] = dict(config.defaults())
        # An empty DEFAULT (no host/token) is "absent" from the
        # caller's perspective — nothing to fall back to FROM, since
        # DEFAULT is itself the fallback. Don't raise.
        if not values:
            return None
    else:
        if section not in config:
            raise _SectionAbsent(section)
        # Read ONLY the named section's own keys, NOT the keys
        # inherited from [DEFAULT]. Without this, configparser would
        # silently merge DEFAULT into the named section's view and
        # a malformed profile (e.g. ``[dev]`` with host but no
        # token) would inherit DEFAULT's token, sending the caller
        # to a different workspace than they asked for. The
        # private ``_sections`` mapping holds the per-section
        # parsed dict; the public API has no equivalent that
        # excludes DEFAULT inheritance.
        own = config._sections.get(section, {})  # type: ignore[attr-defined]
        values = {str(k): str(v) for k, v in own.items()}

    host = values.get("host")
    token = values.get("token")
    if host and token:
        return WorkspaceCreds(host=_strip_trailing_slash(host), token=token)
    # Section exists but is missing one or both required fields.
    # Named sections raise so the caller fails loud (no silent
    # fallback to a different workspace via [DEFAULT]). DEFAULT
    # falls through to None — there's nowhere further to fall back.
    if section == DEFAULT_SECTION:
        return None
    missing = [k for k in ("host", "token") if not values.get(k)]
    raise _SectionPresentButInvalid(
        f"section [{section}] is missing required field(s) {missing}; "
        "this looks like a malformed profile rather than a missing one. "
        "Fix the section or remove it so resolution can fall back to "
        "[DEFAULT]."
    )


def _call_sdk_authenticate(profile: str | None) -> WorkspaceCreds | None:
    """
    Call ``databricks-sdk``'s ``Config.authenticate()`` once and unpack
    the result.

    Wrapping the SDK interaction in its own helper keeps
    :func:`_try_resolve_via_sdk` short and isolates the import-error /
    ValueError handling so the caller can stay focused on policy
    (which sources to try, in what order).

    :param profile: The profile name to authenticate against, or
        ``None`` to let the SDK decide.
    :returns: A :class:`WorkspaceCreds` with a freshly-minted bearer,
        or ``None`` if the SDK could not produce one (import failed,
        config invalid, non-Bearer auth scheme).
    """
    try:
        from databricks.sdk.config import Config
    except ImportError as exc:
        # Pinned dep missing = real env bug, not routine auth failure.
        _logger.warning(
            "databricks-sdk is not importable: %s — OAuth profiles will be invisible "
            "to the resolver, falling through to configparser path.",
            exc,
        )
        return None

    # ``None`` means "let the SDK decide" (env var / DEFAULT section).
    sdk_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")
    try:
        cfg = Config(profile=sdk_profile)
        headers = cfg.authenticate()
    except ValueError as exc:
        # INFO (not WARNING): expired tokens raise here. WARNING would
        # surface via root's lastResort handler to stderr, drowning the
        # clean ClickException. INFO still lands in cli-*.log.
        _logger.info(
            "databricks-sdk Config(profile=%r).authenticate() failed: %s — "
            "falling through to configparser path.",
            sdk_profile,
            exc,
            exc_info=True,
        )
        return None

    host = cfg.host
    auth = headers.get("Authorization")
    if not host or not auth or not auth.startswith("Bearer "):
        # Non-Bearer auth schemes (Basic, etc.) are unsupported.
        return None
    return WorkspaceCreds(
        host=_strip_trailing_slash(host),
        token=auth.removeprefix("Bearer "),
    )


def _try_resolve_via_sdk(profile: str | None) -> WorkspaceCreds | None:
    """
    Try to resolve credentials via ``databricks-sdk``'s ``Config``.

    The SDK's ``Config(profile=...).authenticate()`` covers every
    ``auth_type`` in ``~/.databrickscfg`` (``pat``, ``databricks-cli`` /
    OAuth-U2M, service-principal OAuth, Azure CLI, env-OIDC, metadata-
    service, etc.) and always returns a freshly-minted bearer. For
    OAuth profiles whose cfg has no static ``token`` field, this is
    the ONLY path that yields a usable token — the raw configparser
    fallback returns ``None`` for those.

    Returns ``None`` (rather than raising) on any SDK-level failure so
    the caller can fall through to the configparser path. This matches
    the inner/ legacy behavior — exotic setups that predate the SDK's
    support matrix continue to work.

    :param profile: The Databricks config profile name to authenticate
        against, e.g. ``"<profile-name>"``. ``None`` lets the SDK use its
        own resolution order (``DATABRICKS_CONFIG_PROFILE`` env var,
        then ``DEFAULT``).
    :returns: A :class:`WorkspaceCreds` with a freshly-minted bearer
        token, or ``None`` if the SDK could not resolve credentials
        for the requested profile.
    """
    return _call_sdk_authenticate(profile)


def _try_resolve_from_cfg(profile: str | None, cfg_path: Path) -> WorkspaceCreds | None:
    """
    Try to resolve credentials from a ``.databrickscfg`` file.

    Resolution within the file:

    1. If ``profile`` is provided and that named section has both
       ``host`` and ``token``, return those values.
    2. If the named section is ABSENT, :class:`_SectionAbsent`
       propagates — the caller (:func:`resolve_databricks_workspace`)
       raises ``OSError`` immediately rather than silently falling back
       to ``[DEFAULT]`` (which could be a different workspace).
    3. If the named section is PRESENT but missing required fields,
       :class:`_SectionPresentButInvalid` propagates — same fail-loud
       treatment.
    4. If ``profile`` is ``None``, skip straight to ``[DEFAULT]``.

    :param profile: The profile name to look up, e.g. ``"dev"``.
        ``None`` means "go straight to ``[DEFAULT]``".
    :param cfg_path: The path to the config file. If this path does
        not exist on disk, the function returns ``None``.
    :returns: A :class:`WorkspaceCreds` populated from ``[DEFAULT]``
        (when ``profile`` is ``None``), or ``None`` if ``[DEFAULT]``
        also had no usable creds (or the file is absent).
    :raises _SectionAbsent: When a named profile was requested but
        its section does not exist in the file.
    :raises _SectionPresentButInvalid: When the requested named
        section exists but is malformed.
    """
    if not cfg_path.exists():
        return None

    config = configparser.ConfigParser()
    config.read(cfg_path)

    if profile is not None:
        # _read_section raises _SectionPresentButInvalid when the
        # named profile exists but is malformed; let it propagate.
        named = _read_section(config, profile)
        if named is not None:
            return named

    return _read_section(config, DEFAULT_SECTION)


def _build_resolution_error_message(profile: str | None, cfg_path: Path) -> str:
    """
    Format the OSError message listing every credential source that
    was tried.

    Extracted so :func:`resolve_databricks_workspace` stays focused on
    the resolution chain itself.

    :param profile: The profile that was requested (or ``None``).
    :param cfg_path: The cfg-file path that was checked.
    :returns: The full error message listing every source.
    """
    profile_clause = (
        f"profile [{profile}] in {cfg_path}" if profile is not None else "(no profile requested)"
    )
    return (
        "Could not resolve Databricks workspace credentials. Tried: "
        "(1) databricks-sdk Config(profile="
        f"{profile!r}).authenticate(), "
        f"(2) {profile_clause}, "
        f"(3) [DEFAULT] section in {cfg_path}."
    )


# TODO: once inner/ is sunset (designs/UNIFICATION.md), delete
# inner/databricks_executor.py:_read_databrickscfg and have it
# call this helper instead.
def resolve_databricks_workspace(profile: str | None) -> WorkspaceCreds:
    """
    Resolve Databricks workspace credentials.

    Resolution order:

    1. ``databricks-sdk`` ``Config(profile=...).authenticate()`` —
       handles every ``auth_type`` including OAuth ``databricks-cli``
       profiles.
    2. The named *profile* section in the Databricks config file via
       raw configparser (only resolves when both ``host`` and ``token``
       are in plain text — does NOT cover OAuth profiles). If the
       named section does not exist, ``OSError`` is raised immediately
       — no silent fallback to ``[DEFAULT]``.
    3. The ``[DEFAULT]`` section of the Databricks config file via raw
       configparser (only when *profile* is ``None``).
    4. ``OSError`` listing every source tried.

    The config file path is ``$DATABRICKS_CONFIG_FILE`` when that env
    var is set, otherwise ``~/.databrickscfg``.

    Trailing slashes on the host are stripped on every code path so
    callers can append their own path (e.g. ``/serving-endpoints``)
    without further normalization.

    :param profile: The Databricks config profile to look up,
        e.g. ``"<profile-name>"``. Pass ``None`` to skip directly to
        the ``[DEFAULT]`` section.
    :returns: A :class:`WorkspaceCreds` whose ``host`` and ``token``
        are both non-empty.
    :raises OSError: When no source yielded both a host and a token.
        The error message names every source that was checked so the
        caller can debug their environment.
    """
    from_sdk = _try_resolve_via_sdk(profile)
    if from_sdk is not None:
        return from_sdk

    # For the configparser path, honor DATABRICKS_CONFIG_PROFILE when no
    # explicit profile was passed. Without this, a typo'd --profile (which
    # _propagate_profile_to_environment exports to the env var) is caught by
    # the SDK path (Config raises ValueError → returns None) but then silently
    # bypassed by the configparser path, which sees profile=None and falls
    # straight through to [DEFAULT].
    effective_profile = profile or os.environ.get("DATABRICKS_CONFIG_PROFILE")

    cfg_path = _databrickscfg_path()
    try:
        from_cfg = _try_resolve_from_cfg(effective_profile, cfg_path)
    except _SectionAbsent:
        # Named profile explicitly requested but not present in the
        # file — fail loud rather than silently routing to [DEFAULT]
        # (a different workspace). This is the typo-in-profile guard.
        raise OSError(
            f"Databricks profile [{effective_profile}] not found in {cfg_path}. "
            "Check that the section name matches exactly (case-sensitive) "
            "and that the file exists."
        ) from None
    except _SectionPresentButInvalid as exc:
        raise OSError(
            f"Databricks profile [{effective_profile}] in {cfg_path} is malformed: {exc}"
        ) from exc
    if from_cfg is not None:
        return from_cfg

    raise OSError(_build_resolution_error_message(effective_profile, cfg_path))
