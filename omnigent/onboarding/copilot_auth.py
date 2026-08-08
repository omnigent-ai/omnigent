"""GitHub Copilot token storage for ``omnigent setup`` and the runtime.

Copilot is deliberately outside the anthropic/openai provider-family + gateway
machinery (see :func:`omnigent.runtime.workflow._build_copilot_spawn_env`): the
GitHub Copilot SDK (``github-copilot-sdk``) talks only to GitHub's Copilot
backend, authenticated by a **GitHub token** — never the Databricks AI gateway.
It therefore has no ``providers:`` family entry, but a user should still be able
to register a Copilot token once through ``omnigent setup`` rather than
exporting it in every shell.

This module is that home. The token is stored exactly like the api-key
providers' secrets — in the omnigent secret store (OS keychain, else a ``0600``
JSON file; see :mod:`omnigent.onboarding.secrets`) — and referenced from a
dedicated top-level ``copilot:`` block in ``~/.omnigent/config.yaml``::

    copilot:
      github_token_ref: keychain:copilot   # or env:GH_TOKEN
      gh_host: https://bmw.ghe.com         # optional; omit for github.com

The reference is resolved with the same :func:`resolve_secret` resolver the
provider families use. A dedicated block (rather than the shared global
``auth:`` block) is required because ``auth:`` is the *gateway* credential the
SDK harnesses inherit when their spec declares no auth — a Copilot token parked
there would be mis-consumed by claude-sdk / codex / pi / openai-agents.

Accepted token types mirror what the Copilot CLI/SDK honors: a fine-grained PAT
(``github_pat_``) with the "Copilot Requests" permission, or an OAuth token from
the GitHub CLI (``gho_``) / Copilot CLI app. Classic PATs (``ghp_``) are NOT
accepted by Copilot.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

from omnigent.errors import OmnigentError
from omnigent.onboarding.extra_install import extra_install_command
from omnigent.onboarding.provider_config import load_config, resolve_secret

# The secret-store name (and thus ``keychain:<name>``) under which a Copilot
# GitHub token is stored — stable so the setup flow and the resolver agree.
COPILOT_SECRET_NAME = "copilot"

# The dedicated top-level config block and the field that references the token.
COPILOT_CONFIG_KEY = "copilot"
_TOKEN_REF_FIELD = "github_token_ref"
_TOKEN_FIELD = "github_token"
_GH_HOST_FIELD = "gh_host"

# The env var the Copilot CLI/SDK reads to point token validation and the
# Copilot backend at a GitHub Enterprise host. Deliberately NOT ``GH_HOST``:
# that would also retarget every ``gh`` invocation the agent makes.
COPILOT_GH_HOST_ENV_VAR = "COPILOT_GH_HOST"

# Ambient GitHub-token env vars, in the precedence the Copilot CLI/SDK honors.
COPILOT_TOKEN_ENV_VARS = ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")

# Token-shape prefixes Copilot accepts. The check is deliberately *soft* — a
# user may force a non-matching value through — so a future prefix change can
# never lock anyone out of their own token. Classic ``ghp_`` PATs are excluded
# because Copilot rejects them.
_GITHUB_TOKEN_PREFIXES = ("github_pat_", "gho_", "ghu_", "ghs_")

# The Copilot CLI's state directory override (the CLI's own convention) and the
# managed config file inside it that records the logged-in user.
COPILOT_HOME_ENV_VAR = "COPILOT_HOME"
_CLI_CONFIG_FILENAME = "config.json"
_CLI_LOGIN_FIELD = "lastLoggedInUser"


def _copilot_cli_config_path() -> Path:
    """Return the Copilot CLI's managed config file path.

    Honors the CLI's ``COPILOT_HOME`` override; defaults to ``~/.copilot``.

    :returns: Path to ``config.json`` inside the Copilot CLI home.
    """
    home = os.environ.get(COPILOT_HOME_ENV_VAR, "").strip()
    base = Path(home) if home else Path.home() / ".copilot"
    return base / _CLI_CONFIG_FILENAME


def copilot_cli_logged_in() -> bool:
    """Return whether the Copilot CLI has a logged-in user on this machine.

    With no GitHub token configured anywhere, the SDK leaves the CLI's
    auto-login on (``use_logged_in_user``) and the runtime authenticates as
    the user from ``copilot login``; the credential also carries its GitHub
    host, which makes it the working auth source for GitHub Enterprise
    data-residency seats (``copilot login --host <tenant>.ghe.com``). The
    login itself lives in the OS keychain, but the CLI records the identity
    in its managed ``config.json``; that marker is what this probe reads.

    The file is JSONC (the CLI writes ``//`` line comments), so comment
    lines are stripped before parsing. Any unreadable or unparseable state
    reads as "not logged in"; this feeds a readiness probe, never an
    error path.

    :returns: ``True`` when a logged-in user is recorded.
    """
    try:
        raw = _copilot_cli_config_path().read_text(encoding="utf-8")
    except OSError:
        return False
    cleaned = re.sub(r"^\s*//.*$", "", raw, flags=re.M)
    try:
        doc = json.loads(cleaned)
    except ValueError:
        return False
    if not isinstance(doc, dict):
        return False
    user = doc.get(_CLI_LOGIN_FIELD)
    return isinstance(user, dict) and bool(user.get("login"))


def looks_like_github_copilot_token(value: str) -> bool:
    """Return whether *value* has the shape of a Copilot-capable GitHub token.

    :param value: A pasted/typed candidate token, e.g. ``"gho_AbC123"`` or
        ``"github_pat_..."``.
    :returns: ``True`` when *value* starts with a known Copilot-capable prefix
        (a fine-grained PAT or an OAuth token); ``False`` for an empty string or
        a classic ``ghp_`` PAT (which Copilot rejects).
    """
    return value.startswith(_GITHUB_TOKEN_PREFIXES)


# The OPTIONAL pip extra that ships the Copilot SDK (``github-copilot-sdk``,
# imported as ``copilot``) — not in the default install, so the ``copilot:``
# token can be set with no SDK present. Setup surfaces the command verbatim when
# the extra is missing. Mirrors cursor's ``CURSOR_EXTRA`` / antigravity's
# ``ANTIGRAVITY_EXTRA``. The name carries literal brackets — markup-rendered
# surfaces must escape it.
COPILOT_EXTRA = "copilot"


def copilot_sdk_installed() -> bool:
    """Return whether the Copilot SDK (the optional extra) is importable.

    The executor imports it lazily on the first turn
    (:mod:`omnigent.inner.copilot_executor`), so a token can be set with no SDK;
    setup uses this to detect that and offer to install it. The
    ``github-copilot-sdk`` package is imported as ``copilot``. Mirrors
    :func:`omnigent.onboarding.cursor_auth.cursor_sdk_installed` /
    :func:`omnigent.onboarding.antigravity_auth.antigravity_sdk_installed`:
    :func:`importlib.util.find_spec` avoids importing the heavy SDK, and the
    guard catches the ``ModuleNotFoundError`` it raises when a parent package is
    absent.

    :returns: ``True`` when ``copilot`` is importable.
    """
    try:
        return importlib.util.find_spec("copilot") is not None
    except ModuleNotFoundError:
        # Guard like the cursor/antigravity checks: find_spec can raise (not
        # return None) when a parent package is absent.
        return False


def copilot_install_command() -> list[str]:
    """Return the argv that installs the ``copilot`` extra into this env.

    Delegates to :func:`~omnigent.onboarding.extra_install.extra_install_command`
    which detects ``uv tool`` / ``uv`` / ``pip`` installs automatically.

    :returns: The install argv.
    """
    return extra_install_command(COPILOT_EXTRA)


def install_copilot_sdk() -> bool:
    """Install the ``copilot`` extra; return whether the SDK is now present.

    Shells out to :func:`copilot_install_command` and re-checks
    :func:`copilot_sdk_installed`; pip/uv output is not captured so failures are
    visible. Mirrors :func:`omnigent.onboarding.cursor_auth.install_cursor_sdk`.

    :returns: ``True`` when ``copilot`` is importable after the attempt;
        ``False`` if the process failed to spawn, timed out, or the SDK is still
        absent.
    """
    try:
        subprocess.run(copilot_install_command(), check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Invalidate import caches so a just-installed package is seen without
    # restarting the process.
    importlib.invalidate_caches()
    return copilot_sdk_installed()


def copilot_github_token_ref(config: dict[str, object] | None = None) -> str | None:
    """Return the configured Copilot GitHub-token secret reference, if any.

    Reads the dedicated ``copilot:`` block of the global config. Both the
    ``github_token_ref`` (``keychain:`` / ``env:``) and an inline ``github_token``
    (``$VAR`` / literal) shapes are accepted so a hand-edited config works too;
    ``github_token_ref`` wins when both are present.

    :param config: A pre-loaded config mapping; ``None`` loads
        ``~/.omnigent/config.yaml`` via :func:`load_config`.
    :returns: The secret reference, e.g. ``"keychain:copilot"`` or
        ``"env:GH_TOKEN"``, or ``None`` when no Copilot token is configured.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(COPILOT_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    ref = block.get(_TOKEN_REF_FIELD) or block.get(_TOKEN_FIELD)
    return ref if isinstance(ref, str) and ref else None


def resolve_copilot_github_token(config: dict[str, object] | None = None) -> str | None:
    """Resolve the configured Copilot GitHub token to its plaintext value, softly.

    Looks up the ``copilot:`` block's secret reference and resolves it via
    :func:`resolve_secret`. Unlike :func:`resolve_secret`, this **never raises**:
    a missing block or an unresolvable reference (deleted keychain entry, unset
    env var) returns ``None`` so the caller — the copilot spawn-env builder and
    the setup readout — can fall back to an inherited ``GH_TOKEN`` instead of
    crashing a run.

    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: The plaintext GitHub token, or ``None`` when none is configured or
        it cannot be resolved.
    """
    ref = copilot_github_token_ref(config)
    if ref is None:
        return None
    try:
        return resolve_secret(ref)
    except OmnigentError:
        return None


def copilot_github_token_configured(config: dict[str, object] | None = None) -> bool:
    """Return whether a usable Copilot GitHub token is configured.

    ``True`` only when the ``copilot:`` block names a reference **and** it
    resolves — a dangling reference reads as not-configured so the setup readout
    never claims a credential the runtime can't actually use.

    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: ``True`` when a Copilot GitHub token is configured and resolvable.
    """
    return resolve_copilot_github_token(config) is not None


def copilot_github_token_settings(ref: str) -> dict[str, object]:
    """Build the ``{"copilot": {...}}`` settings dict that records *ref*.

    Handed to :func:`omnigent.cli._save_global_config` with the ``copilot:``
    block deep-merged, so an already-configured ``gh_host`` survives a token
    change (and vice versa).

    :param ref: The secret reference to record, e.g. ``"keychain:copilot"`` or
        ``"env:GH_TOKEN"``.
    :returns: ``{"copilot": {"github_token_ref": ref}}``.
    """
    return {COPILOT_CONFIG_KEY: {_TOKEN_REF_FIELD: ref}}


def normalize_gh_host(value: str) -> str:
    """Canonicalize a typed GitHub Enterprise host value.

    Trims whitespace and trailing slashes; an effectively empty input becomes
    ``""``, which callers treat as "use github.com" (the field is then not
    written at all).

    :param value: The raw user input, e.g. ``" https://bmw.ghe.com/ "``.
    :returns: The canonical host string, or ``""`` when empty.
    """
    return value.strip().rstrip("/")


def looks_like_gh_host(value: str) -> bool:
    """Return whether *value* has the expected GHE host shape, softly.

    The Copilot CLI was probed with a full ``https://<tenant>.ghe.com``
    origin; other shapes (a bare hostname, an explicit path) are unverified
    rather than known-broken, so this check is *soft*: setup warns and lets
    the user force the value through, mirroring the token-shape check. A
    ``github.com``/``api.`` value is flagged too: the field exists precisely
    for the non-default host.

    :param value: A normalized (see :func:`normalize_gh_host`) candidate.
    :returns: ``True`` when the value looks like a ``https://`` origin
        without a path and does not point at github.com.
    """
    if not value.startswith("https://"):
        return False
    rest = value.removeprefix("https://")
    if not rest or "/" in rest:
        return False
    host = rest.split(":", 1)[0].lower()
    return host != "github.com" and not host.startswith("api.")


def copilot_gh_host(config: dict[str, object] | None = None) -> str | None:
    """Return the configured GitHub Enterprise host for Copilot, if any.

    :param config: A pre-loaded config mapping; ``None`` loads the global
        config.
    :returns: The configured host origin, e.g. ``"https://bmw.ghe.com"``, or
        ``None`` when Copilot authenticates against github.com.
    """
    cfg = load_config() if config is None else config
    block = cfg.get(COPILOT_CONFIG_KEY)
    if not isinstance(block, dict):
        return None
    host = block.get(_GH_HOST_FIELD)
    if not isinstance(host, str):
        return None
    normalized = normalize_gh_host(host)
    return normalized or None


def copilot_gh_host_settings(host: str) -> dict[str, object]:
    """Build the ``{"copilot": {...}}`` settings dict that records *host*.

    Handed to :func:`omnigent.cli._save_global_config` with the ``copilot:``
    block deep-merged, so the token reference survives a host change.

    :param host: The normalized host origin to record.
    :returns: ``{"copilot": {"gh_host": host}}``.
    """
    return {COPILOT_CONFIG_KEY: {_GH_HOST_FIELD: host}}


def _copilot_block_without(
    config: dict[str, object], fields: tuple[str, ...]
) -> dict[str, object] | None:
    """The ``{"copilot": {...}}`` settings dict minus *fields*, or ``None``.

    ``None`` signals "nothing would remain"; the caller then unsets the whole
    ``copilot:`` block instead of writing an empty one. Deep-merge cannot
    delete a nested key, so removals are written as a full-block replace.
    """
    block = config.get(COPILOT_CONFIG_KEY)
    remaining = (
        {key: value for key, value in block.items() if key not in fields}
        if isinstance(block, dict)
        else {}
    )
    return {COPILOT_CONFIG_KEY: remaining} if remaining else None


def copilot_token_removed_settings(config: dict[str, object]) -> dict[str, object] | None:
    """Settings that drop the token fields but keep the rest (e.g. ``gh_host``).

    :param config: The pre-loaded global config.
    :returns: A full-block replacement for :func:`_save_global_config`, or
        ``None`` when the whole ``copilot:`` block should be unset.
    """
    return _copilot_block_without(config, (_TOKEN_REF_FIELD, _TOKEN_FIELD))


def copilot_gh_host_removed_settings(config: dict[str, object]) -> dict[str, object] | None:
    """Settings that drop ``gh_host`` but keep the token reference.

    :param config: The pre-loaded global config.
    :returns: A full-block replacement for :func:`_save_global_config`, or
        ``None`` when the whole ``copilot:`` block should be unset.
    """
    return _copilot_block_without(config, (_GH_HOST_FIELD,))
