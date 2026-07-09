"""Detect AI credentials already present on the machine.

For the ``omnigent setup --no-internal-beta`` first-run experience, this module
discovers credentials a user already has — vendor API keys in the
environment, a logged-in ``claude`` / ``codex`` CLI, or a local Ollama
server — so the setup flow can offer them as one-tap choices instead of
asking the user to paste keys they already have.

Detection is almost entirely pure standard library (``os``, ``socket``,
``pathlib``) and performs no network I/O beyond a single non-blocking
localhost TCP probe for Ollama. The one exception is macOS Claude detection:
Claude Code stores its subscription OAuth in the macOS Keychain (not a file),
so on macOS — and only when the file check comes up empty — Claude detection
falls back to a ``claude auth status`` subprocess (see
:func:`_claude_login_detected`). Linux detection stays purely file-based and
subprocess-free.

The output is a list of :class:`DetectedProvider`, one per credential
found, in a stable priority order (environment keys first, then CLI
logins, then a local server). The caller maps each detection's
:attr:`DetectedProvider.family` to the harness surface it serves.
"""

from __future__ import annotations

import json
import os
import shlex
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import tomllib

from omnigent.env_credentials import getenv_nonempty_with_omnigent_prefix
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GEMINI_FAMILY, OPENAI_FAMILY
from omnigent.onboarding.providers import PROVIDER_ENV_VARS

DetectedKind = Literal["key", "subscription", "local", "cli-config"]

# The detection kinds. ``key`` is a vendor API key from the environment;
# ``subscription`` is a logged-in CLI; ``local`` is a self-hosted endpoint;
# ``cli-config`` is a custom model provider a harness CLI's own config file
# defines (today: a ``[model_providers.X]`` table in ``~/.codex/config.toml``
# that carries its own auth, e.g. the Databricks AI Gateway written by
# ``isaac configure codex``).
KEY_KIND: DetectedKind = "key"
SUBSCRIPTION_KIND: DetectedKind = "subscription"
LOCAL_KIND: DetectedKind = "local"
CLI_CONFIG_KIND: DetectedKind = "cli-config"

# Codex's built-in model provider ids (openai/codex,
# ``codex-rs/model-provider-info/src/lib.rs``). A ``model_provider`` set to
# one of these is not a *custom* provider: ``openai`` is the subscription /
# API-key path (covered by the auth.json detection), ``ollama`` / ``lmstudio``
# are local servers (ollama is covered by the TCP-probe detection), and
# ``amazon-bedrock`` resolves auth from the AWS environment — none of them is
# a config-defined credential this detection should adopt.
_CODEX_BUILTIN_PROVIDERS = frozenset({"openai", "amazon-bedrock", "ollama", "lmstudio"})

# Ollama's default OpenAI-compatible endpoint.
_OLLAMA_HOST = "localhost"
_OLLAMA_PORT = 11434
_OLLAMA_URL = f"http://{_OLLAMA_HOST}:{_OLLAMA_PORT}"

# Timeout (seconds) for the Ollama TCP probe — short so setup stays snappy
# when nothing is listening.
_OLLAMA_PROBE_TIMEOUT = 0.25

# Maps each provider whose env key we surface to the served model family.
# Providers absent here (or mapped to ``None``) are reported with
# ``family=None`` — their key is detected but no harness surface is
# implied. Anthropic serves the ``anthropic`` surface; OpenAI and
# OpenAI-compatible gateways (OpenRouter) serve the ``openai`` surface;
# Gemini serves the ``gemini`` surface (the antigravity-sdk harness drives
# the Gemini SDK directly with a GEMINI_API_KEY), so a detected
# GEMINI_API_KEY is adopted as a ``gemini``-family ``key`` provider.
_ENV_KEY_FAMILY: dict[str, str | None] = {
    "anthropic": ANTHROPIC_FAMILY,
    "openai": OPENAI_FAMILY,
    "openrouter": OPENAI_FAMILY,
    "gemini": GEMINI_FAMILY,
}


@dataclass(frozen=True)
class DetectedProvider:
    """A credential found on the machine during ambient detection.

    :param name: The provider/source name, e.g. ``"anthropic"``,
        ``"openai"``, ``"openrouter"``, ``"gemini"``, ``"claude"``,
        ``"codex"``, or ``"ollama"``.
    :param kind: How the credential authenticates — ``"key"`` (an API key
        in the environment), ``"subscription"`` (a logged-in CLI), or
        ``"local"`` (a self-hosted endpoint).
    :param family: The model family this credential serves
        (``"anthropic"`` / ``"openai"`` / ``"gemini"``), or ``None`` when the
        credential is detected but maps to no omnigent harness surface.
    :param source: A human-readable descriptor of where the credential
        comes from, e.g. ``"$ANTHROPIC_API_KEY"``, ``"claude CLI login"``,
        or ``"http://localhost:11434"``.
    :param model_provider: For ``kind="cli-config"`` only: the custom
        provider id the CLI's config file selects, e.g. ``"Databricks"``
        (the ``model_provider`` key in ``~/.codex/config.toml``). ``None``
        for other kinds.
    :param display_name: For ``kind="cli-config"`` only: the provider's
        human display name from its config table (``name = "Databricks AI
        Gateway"``), falling back to :attr:`model_provider` when the table
        names none. ``None`` for other kinds.
    """

    name: str
    kind: DetectedKind
    family: str | None
    source: str
    model_provider: str | None = None
    display_name: str | None = None


def _claude_credentials_path() -> Path:
    """Return the path to the Claude CLI's stored login credentials.

    Honors ``$HOME`` (and thus ``monkeypatch.setenv("HOME", ...)``) so the
    check can be redirected in tests.

    :returns: Path to ``~/.claude/.credentials.json``.
    """
    return Path(os.path.expanduser("~")) / ".claude" / ".credentials.json"


def _codex_auth_path() -> Path:
    """Return the path to the Codex CLI's stored login credentials.

    Honors ``$HOME`` so the check can be redirected in tests.

    :returns: Path to ``~/.codex/auth.json``.
    """
    return Path(os.path.expanduser("~")) / ".codex" / "auth.json"


def codex_auth_has_credential(auth_path: Path) -> bool:
    """Return whether a Codex ``auth.json`` carries a usable stored credential.

    The Codex CLI persists login state in ``auth.json`` as an ``AuthDotJson``
    object whose fields are *all* optional, so an empty ``{}`` — or a file left
    behind after a logout — parses cleanly while representing **no** usable
    login. Treating mere file existence as "logged in" let such a stale file
    masquerade as a subscription provider, which then shadowed a real
    configured credential and dropped Codex to its own login screen at run time
    (the bug this guards against).

    A file counts as a real login when it parses as a JSON object and carries
    at least one credential the Codex CLI can authenticate with, mirroring
    Codex's own ``AuthDotJson`` shape (``openai/codex``,
    ``codex-rs/login/src/{auth/storage,token_data}.rs``):

    - ``OPENAI_API_KEY`` — a non-empty string (``auth_mode: "apikey"``);
    - ``tokens.access_token`` or ``tokens.refresh_token`` — a non-empty string
      (``auth_mode: "chatgpt"``; a refresh token alone suffices because the
      CLI mints a fresh access token from it);
    - ``personal_access_token`` — a non-empty string (enterprise / external
      token integrations).

    The check is purely local (no network), so it cannot detect a
    *present-but-expired* OAuth access token — but the Codex CLI refreshes
    those itself from the refresh token. Its job is to reject the empty /
    logged-out / malformed cases.

    :param auth_path: Path to the Codex ``auth.json`` to inspect, e.g.
        ``Path("~/.codex/auth.json").expanduser()``.
    :returns: ``True`` when the file carries a usable credential; ``False``
        when it is missing, unreadable, not valid JSON, not a JSON object, or
        carries no credential field.
    """
    try:
        raw = auth_path.read_text(encoding="utf-8")
    except OSError:
        # Missing or unreadable file — no login.
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Malformed JSON — treat as no usable login rather than crash.
        return False
    if not isinstance(data, dict):
        return False
    # apikey mode: a baked-in OpenAI API key.
    api_key = data.get("OPENAI_API_KEY")
    if isinstance(api_key, str) and api_key.strip():
        return True
    # chatgpt / OAuth mode: an access token, or a refresh token the CLI can
    # exchange for a fresh access token.
    tokens = data.get("tokens")
    if isinstance(tokens, dict):
        for field in ("access_token", "refresh_token"):
            value = tokens.get(field)
            if isinstance(value, str) and value.strip():
                return True
    # Enterprise / external-token integrations.
    personal_access_token = data.get("personal_access_token")
    if isinstance(personal_access_token, str) and personal_access_token.strip():
        return True
    return False


def _codex_config_path() -> Path:
    """Return the path to the Codex CLI's user config file.

    Honors ``$HOME`` so the check can be redirected in tests.

    :returns: Path to ``~/.codex/config.toml``.
    """
    return Path(os.path.expanduser("~")) / ".codex" / "config.toml"


@dataclass(frozen=True)
class CodexConfigProvider:
    """A custom, auth-carrying model provider found in ``~/.codex/config.toml``.

    :param provider_id: The ``model_provider`` id the config selects, i.e.
        the key under ``[model_providers.<id>]``, e.g. ``"Databricks"``.
    :param display_name: The provider table's ``name`` field, e.g.
        ``"Databricks AI Gateway"``; falls back to :attr:`provider_id` when
        the table names none.
    """

    provider_id: str
    display_name: str


def _provider_table_has_self_contained_auth(table: dict[str, object]) -> bool:
    """Return whether a Codex ``[model_providers.X]`` table carries its own auth.

    "Self-contained" means the Codex CLI can authenticate against the
    provider from the config table alone — no ``auth.json`` login and no
    environment variable that omnigents would have to thread into the codex
    subprocess. Mirrors Codex's own provider auth fields (``openai/codex``,
    ``codex-rs/model-provider-info/src/lib.rs``):

    - ``[X.auth]`` — a token-printing command (``command`` + ``args``), the
      shape ``isaac configure codex`` writes (``jq`` reading a cached
      Databricks Model Serving token);
    - ``experimental_bearer_token`` — an inline static bearer token;
    - ``[X.aws]`` — AWS SigV4 request signing (Bedrock-style);
    - ``http_headers`` containing an ``Authorization`` header — a static
      auth header.

    Deliberately **excluded** (each with a reason):

    - ``env_key`` / ``env_http_headers`` — auth via an environment
      variable. The codex executor launches the CLI with a scrubbed
      environment (``_clean_codex_env``), so an arbitrary env var would not
      reach the subprocess and the provider would 401 at run time despite
      detecting as "configured". Supporting these needs an env-passthrough
      design first.
    - ``requires_openai_auth = true`` — the provider rides the ChatGPT
      login, which is exactly the ``auth.json`` subscription detection's
      territory (checked separately by the caller).

    :param table: The raw ``[model_providers.X]`` mapping parsed from
        ``config.toml``, e.g. ``{"name": "Databricks AI Gateway",
        "base_url": "...", "auth": {"command": "jq", ...}}``.
    :returns: ``True`` when the table carries self-contained auth.
    """
    auth = table.get("auth")
    if isinstance(auth, dict):
        command = auth.get("command")
        if isinstance(command, str) and command.strip():
            return True
    bearer = table.get("experimental_bearer_token")
    if isinstance(bearer, str) and bearer.strip():
        return True
    if isinstance(table.get("aws"), dict):
        return True
    headers = table.get("http_headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if (
                isinstance(key, str)
                and key.lower() == "authorization"
                and isinstance(value, str)
                and value.strip()
            ):
                return True
    return False


def _effective_codex_model_provider(config: dict[str, object]) -> str | None:
    """Resolve the effective default ``model_provider`` id from a Codex config.

    Mirrors Codex's own profile-merge resolution (``openai/codex``,
    ``codex-rs/core/src/config``): the active profile's ``model_provider``
    (top-level ``profile = "name"`` selecting ``[profiles.name]``) wins over
    the top-level ``model_provider``.

    :param config: The parsed ``config.toml`` mapping, e.g.
        ``{"model_provider": "Databricks", "model_providers": {...}}``.
    :returns: The effective provider id, e.g. ``"Databricks"``, or ``None``
        when neither the active profile nor the top level sets one (Codex
        then defaults to the built-in ``openai`` provider).
    """
    provider_id: object = config.get("model_provider")
    active_profile = config.get("profile")
    if isinstance(active_profile, str) and active_profile.strip():
        profiles = config.get("profiles")
        if isinstance(profiles, dict):
            profile_table = profiles.get(active_profile)
            if isinstance(profile_table, dict) and isinstance(
                profile_table.get("model_provider"), str
            ):
                provider_id = profile_table["model_provider"]
    if isinstance(provider_id, str) and provider_id.strip():
        return provider_id
    return None


def codex_config_custom_provider(config_path: Path) -> CodexConfigProvider | None:
    """Detect a custom, auth-carrying default model provider in a Codex config.

    ``isaac configure codex`` (and similar enterprise tooling) configures
    Codex by writing ``~/.codex/config.toml`` only: a custom
    ``[model_providers.X]`` table whose ``[X.auth]`` command prints a
    bearer token, selected via a top-level ``model_provider = "X"``. No
    ``auth.json`` is ever written, so the subscription detection sees
    nothing — this is the detection for that state.

    A config counts when its **effective default provider** is a custom
    (non-built-in) ``[model_providers.X]`` table with self-contained auth
    (see :func:`_provider_table_has_self_contained_auth`). The effective
    provider mirrors Codex's own resolution (``openai/codex``,
    ``codex-rs/core/src/config``): the active profile's ``model_provider``
    (top-level ``profile = "name"`` selecting ``[profiles.name]``) wins
    over the top-level ``model_provider``; absent both, Codex defaults to
    the built-in ``openai`` provider and there is nothing to detect.

    Purely local and structural: parses one TOML file, runs nothing, and
    never validates that the auth command actually yields a live token —
    like the ``auth.json`` check, its job is to reject "not configured,"
    not to prove the credential will authenticate.

    :param config_path: Path to the Codex ``config.toml`` to inspect, e.g.
        ``Path("~/.codex/config.toml").expanduser()``.
    :returns: The detected provider, or ``None`` when the file is missing /
        malformed, the effective provider is built-in or unset, its table
        is absent, or the table carries no self-contained auth.
    """
    try:
        raw = config_path.read_bytes()
    except OSError:
        # Missing or unreadable file — nothing configured.
        return None
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        # Malformed TOML — treat as not configured rather than crash setup.
        return None

    provider_id = _effective_codex_model_provider(config)
    if provider_id is None or provider_id in _CODEX_BUILTIN_PROVIDERS:
        return None

    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    table = providers.get(provider_id)
    if not isinstance(table, dict):
        # The config selects a provider it never defines — codex itself
        # would fail; nothing usable to adopt.
        return None
    if table.get("requires_openai_auth") is True:
        # Rides the ChatGPT login — the auth.json subscription detection's
        # territory, not a config-defined credential.
        return None
    if not _provider_table_has_self_contained_auth(table):
        return None

    name = table.get("name")
    display_name = name if isinstance(name, str) and name.strip() else provider_id
    return CodexConfigProvider(provider_id=provider_id, display_name=display_name)


@dataclass(frozen=True)
class CodexConfigTransport:
    """The base URL + auth command from a Codex ``[model_providers.X]`` table.

    The runtime-routing counterpart of :class:`CodexConfigProvider` (which
    only carries the id / display name for the setup menu). This reads the
    fields a harness needs to actually talk to the provider — the ones
    ``isaac configure codex`` writes for the Databricks AI Gateway.

    :param base_url: The provider table's ``base_url``, e.g.
        ``"https://<workspace>.ai-gateway.cloud.databricks.com/codex/v1"``.
    :param auth_command: A single shell command string that prints a bearer
        token to stdout, reconstructed from the table's ``[X.auth]``
        ``command`` + ``args`` (e.g. ``"jq -r .access_token /path/token.json"``).
        ``None`` when the table carries no ``[X.auth]`` token command (e.g. it
        authenticates via a static header or AWS SigV4 instead).
    """

    base_url: str
    auth_command: str | None


def codex_config_provider_transport(
    config_path: Path, provider_id: str
) -> CodexConfigTransport | None:
    """Read the base URL + auth command for one Codex ``[model_providers.X]``.

    A harness that pinned a ``cli-config`` provider (e.g. pi-native routing the
    user's Databricks AI Gateway) needs the *transport* — where to send
    requests and how to authenticate — not just the id. This parses the named
    ``[model_providers.<provider_id>]`` table out of ``config.toml`` and returns
    its ``base_url`` plus a shell command (rebuilt from ``[X.auth]``
    ``command`` + ``args``) that prints a bearer token.

    Purely local and structural (parses one TOML file, runs nothing). Returns
    ``None`` — rather than raising — for every "can't resolve" case so a caller
    can fall back gracefully without crashing a launch.

    :param config_path: Path to the Codex ``config.toml`` to inspect, e.g.
        ``Path("~/.codex/config.toml").expanduser()``.
    :param provider_id: The ``[model_providers.X]`` id to read, e.g.
        ``"Databricks"``.
    :returns: The :class:`CodexConfigTransport`, or ``None`` when the file is
        missing / malformed, the table is absent, or it declares no
        ``base_url``.
    """
    try:
        raw = config_path.read_bytes()
    except OSError:
        return None
    try:
        config = tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None

    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        return None
    table = providers.get(provider_id)
    if not isinstance(table, dict):
        return None
    base_url = table.get("base_url")
    if not isinstance(base_url, str) or not base_url.strip():
        return None

    auth_command: str | None = None
    auth = table.get("auth")
    if isinstance(auth, dict):
        command = auth.get("command")
        args = auth.get("args")
        if isinstance(command, str) and command.strip():
            parts = [command]
            if isinstance(args, list):
                parts.extend(str(arg) for arg in args)
            # shlex.join produces a single shell-safe string Pi can run as a
            # "!command" apiKey (it shell-quotes the token file path etc.).
            auth_command = shlex.join(parts)
    return CodexConfigTransport(base_url=base_url.strip(), auth_command=auth_command)


def codex_config_detection() -> DetectedProvider | None:
    """Return the ``cli-config`` detection for ``~/.codex/config.toml``, if any.

    The single constructor for this detection — used by
    :func:`detect_providers` and by callers that need to identify the
    detection on its own (e.g. the launch path checking whether the host
    config's custom default provider was dismissed by a Remove).

    :returns: A ``kind="cli-config"`` :class:`DetectedProvider` (stable name
        ``codex-<slug>``, e.g. ``"codex-databricks"``), or ``None`` when the
        config carries no custom, self-contained-auth default provider (see
        :func:`codex_config_custom_provider`).
    """
    codex_config = codex_config_custom_provider(_codex_config_path())
    if codex_config is None:
        return None
    return DetectedProvider(
        name=f"codex-{_slug(codex_config.provider_id)}",
        kind=CLI_CONFIG_KIND,
        family=OPENAI_FAMILY,
        source=f"~/.codex/config.toml provider {codex_config.provider_id!r}",
        model_provider=codex_config.provider_id,
        display_name=codex_config.display_name,
    )


def _slug(value: str) -> str:
    """Slugify a provider id into a config-friendly provider entry name part.

    :param value: A Codex provider id, e.g. ``"Databricks"`` or
        ``"My Proxy"``.
    :returns: A lowercase, hyphenated slug, e.g. ``"databricks"`` or
        ``"my-proxy"``; ``"provider"`` when nothing alphanumeric survives.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in value.lower()).strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    # A provider id with no alphanumerics at all would otherwise produce an
    # empty entry name; "provider" keeps the synthesized name well-formed.
    return slug or "provider"


def claude_auth_has_credential(creds_path: Path) -> bool:
    """Return whether a Claude ``.credentials.json`` carries a usable login.

    The mirror of :func:`codex_auth_has_credential` for Claude Code, which
    stores its subscription OAuth credentials under a ``claudeAiOauth`` object
    (verified against a live file: ``accessToken``, ``refreshToken``,
    ``expiresAt`` as epoch **milliseconds**, ``scopes``, ``subscriptionType``).

    The access token is short-lived (~hours) and the CLI silently refreshes it
    from ``refreshToken``, so "expired" is the normal steady state — gating on
    ``expiresAt`` alone would flag a perfectly good login every time the token
    rolls over. A login therefore counts as usable when it parses and has a
    non-empty ``accessToken`` that is **either renewable** (a non-empty
    ``refreshToken``) **or** not yet expired (``expiresAt`` in the future). This
    rejects the empty / logged-out / malformed cases (matching the codex helper)
    without false-flagging a stale-but-refreshable token.

    Like the codex helper this is purely local (no network): it cannot detect a
    server-side *revocation* — only the harness's own login attempt can. Its job
    is to reject "no usable login," not to prove the token will authenticate.

    :param creds_path: Path to ``.credentials.json``, e.g.
        ``Path("~/.claude/.credentials.json").expanduser()``.
    :returns: ``True`` when the file carries a usable (present + renewable or
        unexpired) subscription login; ``False`` when missing, unreadable, not
        valid JSON, not an object, or carrying no usable credential.
    """
    try:
        raw = creds_path.read_text(encoding="utf-8")
    except OSError:
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    access_token = oauth.get("accessToken")
    if not (isinstance(access_token, str) and access_token.strip()):
        return False
    # Renewable: a refresh token means the CLI can mint a fresh access token
    # even if the current one has expired.
    refresh_token = oauth.get("refreshToken")
    if isinstance(refresh_token, str) and refresh_token.strip():
        return True
    # No refresh token — the access token must still be unexpired to be usable.
    expires_at = oauth.get("expiresAt")  # epoch milliseconds
    if isinstance(expires_at, (int, float)) and not isinstance(expires_at, bool):
        return expires_at > time.time() * 1000
    return False


def _claude_login_detected() -> bool:
    """Return whether a usable Claude Code subscription login is present.

    File-first, with a macOS Keychain fallback. Claude Code stores its OAuth
    credential in ``~/.claude/.credentials.json`` on Linux but in the **macOS
    Keychain** on macOS — so the fast, no-subprocess file check
    (:func:`claude_auth_has_credential`) is accurate on Linux yet silently
    misses a real, working subscription on a Mac, where that file does not
    exist. That gap is why ``configure harnesses`` failed to auto-detect a
    Claude subscription that the same machine could sign in to without a web
    login (the CLI had already cached the credential in the Keychain).

    To close the gap without slowing the common path, this checks the file
    first and, only on macOS and only when the file check comes up empty, falls
    back to the authoritative CLI status check
    (:func:`omnigent.onboarding.harness_install.harness_cli_logged_in`, which
    runs ``claude auth status`` — the command that reads wherever the CLI
    actually stored the credential, Keychain included). The fallback costs one
    subprocess and runs only on macOS when the file is absent (the normal macOS
    case), so Linux detection stays purely file-based and subprocess-free.

    The fallback is a no-op when the ``claude`` binary is not on ``PATH``
    (``harness_cli_logged_in`` returns ``False`` there), so a Keychain
    credential is detected only when the CLI that wrote it is still installed.

    :returns: ``True`` when a usable Claude subscription login is present — via
        the credentials file on any platform, or via the macOS Keychain through
        the CLI status fallback; ``False`` otherwise.
    """
    if claude_auth_has_credential(_claude_credentials_path()):
        return True
    if sys.platform == "darwin":
        # macOS stores the Claude OAuth token in the Keychain, not the file
        # checked above. Ask the CLI itself (``claude auth status`` reads the
        # Keychain) rather than reimplement Keychain access here. Lazy import
        # keeps this module's import graph light and pays nothing off-macOS.
        from omnigent.onboarding.harness_install import harness_cli_logged_in

        return harness_cli_logged_in(ANTHROPIC_FAMILY)
    return False


def _ollama_reachable() -> bool:
    """Return whether a local Ollama server accepts TCP connections.

    Performs a single short-timeout connect to ``localhost:11434``. Isolated
    in its own helper so tests can monkeypatch it without real network I/O.

    :returns: ``True`` when ``localhost:11434`` accepts a TCP connection,
        ``False`` on refusal, timeout, or any socket error.
    """
    try:
        with socket.create_connection((_OLLAMA_HOST, _OLLAMA_PORT), timeout=_OLLAMA_PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def detect_providers() -> list[DetectedProvider]:
    """Detect credentials already present on the machine.

    Checks, in a stable priority order:

    1. Vendor API keys in the environment, via
       :data:`omnigent.onboarding.providers.PROVIDER_ENV_VARS`. Only
       variables that are set and non-empty are reported, in the order they
       appear in ``PROVIDER_ENV_VARS``.
    2. A logged-in Claude CLI — ``~/.claude/.credentials.json`` carries a
       usable login (see :func:`claude_auth_has_credential`), or, on macOS,
       the credential lives in the Keychain and ``claude auth status`` reports
       a login (see :func:`_claude_login_detected`).
    3. A custom, auth-carrying model provider in ``~/.codex/config.toml``
       (see :func:`codex_config_custom_provider`) — e.g. the Databricks AI
       Gateway provider that ``isaac configure codex`` writes. Ordered
       before the codex login check so the auto-default matches Codex's own
       resolution (config.toml's default provider beats auth.json).
    4. A logged-in Codex CLI (``~/.codex/auth.json`` exists *and* carries a
       usable credential — see :func:`codex_auth_has_credential`).
    5. A reachable local Ollama (``localhost:11434`` TCP-connectable).

    No network I/O is performed except the single Ollama probe (see
    :func:`_ollama_reachable`). On macOS, a ``claude auth status`` subprocess
    may run as the Claude Keychain fallback (see :func:`_claude_login_detected`).

    :returns: One :class:`DetectedProvider` per credential found, in the
        priority order above. Empty when nothing is detected.
    """
    detected: list[DetectedProvider] = []

    # 1. Environment API keys.
    for provider, env_var in PROVIDER_ENV_VARS.items():
        # Only surface providers we can map to a family decision; other
        # PROVIDER_ENV_VARS entries (mistral, groq, ...) are not part of
        # the model-selection surface yet.
        if provider not in _ENV_KEY_FAMILY:
            continue
        resolved = getenv_nonempty_with_omnigent_prefix(env_var)
        if resolved is None:
            continue
        actual_env_var, _value = resolved
        detected.append(
            DetectedProvider(
                name=provider,
                kind=KEY_KIND,
                family=_ENV_KEY_FAMILY[provider],
                source=f"${actual_env_var}",
            )
        )

    # 1b. Claude Code on Vertex AI — the CLI reads three env vars for GCP auth.
    # Detected separately from plain API keys because Vertex uses GCP ADC
    # (no Anthropic key), so none of the PROVIDER_ENV_VARS entries cover it.
    _vertex_truthy = ("1", "true", "yes")
    if (
        os.environ.get("CLAUDE_CODE_USE_VERTEX", "").strip().lower() in _vertex_truthy
        and os.environ.get("ANTHROPIC_VERTEX_PROJECT_ID", "").strip()
        and os.environ.get("CLOUD_ML_REGION", "").strip()
    ):
        detected.append(
            DetectedProvider(
                name="vertex-claude",
                kind=KEY_KIND,
                family=ANTHROPIC_FAMILY,
                source="$CLAUDE_CODE_USE_VERTEX",
            )
        )

    # 2. Claude CLI login. Like codex (below), existence alone is not enough —
    #    an empty / logged-out ``.credentials.json`` carries no usable login.
    #    On macOS the credential lives in the Keychain rather than the file, so
    #    detection falls back to the CLI's own status check there. See
    #    ``_claude_login_detected`` / ``claude_auth_has_credential``.
    if _claude_login_detected():
        detected.append(
            DetectedProvider(
                name="claude",
                kind=SUBSCRIPTION_KIND,
                family=ANTHROPIC_FAMILY,
                source="claude CLI login",
            )
        )

    # 3. A custom model provider in ~/.codex/config.toml (e.g. the
    #    Databricks AI Gateway written by ``isaac configure codex``, which
    #    writes config.toml only — never auth.json — so the login check
    #    below cannot see it). Ordered BEFORE the codex login check so that
    #    on a machine with both, the auto-default matches what a plain
    #    ``codex`` invocation does: config.toml's default model_provider
    #    wins over auth.json.
    codex_config_det = codex_config_detection()
    if codex_config_det is not None:
        detected.append(codex_config_det)

    # 4. Codex CLI login. Existence alone is not enough — an empty or
    #    logged-out ``auth.json`` carries no usable credential, and adopting it
    #    as a subscription would plant a phantom default that shadows a real
    #    configured credential and strands Codex at its own login screen. See
    #    ``codex_auth_has_credential``.
    if codex_auth_has_credential(_codex_auth_path()):
        detected.append(
            DetectedProvider(
                name="codex",
                kind=SUBSCRIPTION_KIND,
                family=OPENAI_FAMILY,
                source="codex CLI login",
            )
        )

    # 5. Local Ollama.
    if _ollama_reachable():
        detected.append(
            DetectedProvider(
                name="ollama",
                kind=LOCAL_KIND,
                family=OPENAI_FAMILY,
                source=_OLLAMA_URL,
            )
        )

    return detected
