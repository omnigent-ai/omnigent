"""Detect Google Antigravity (``agy``) OAuth credentials for ``antigravity-native``.

The ``agy`` CLI authenticates via a browser OAuth flow on first interactive run
— it has no ``agy login`` / ``agy auth status`` subcommand. *Where* and *how* it
persists the resulting token is **platform-specific**:

- macOS through agy 1.0.10 writes ``~/.gemini/oauth_creds.json`` — a flat
  OAuth2 object (verified live against agy 1.0.10)::

      {"access_token": "ya29.…", "refresh_token": "1//0g…",
       "expiry_date": …, "id_token": "eyJ…", "scope": "…", "token_type": "Bearer"}

- Linux writes ``~/.gemini/antigravity-cli/antigravity-oauth-token`` — the OAuth
  object **nested under** ``token`` (verified live against agy 1.0.10)::

      {"auth_method": "oauth",
       "token": {"access_token": "ya29.…", "refresh_token": "1//0g…",
                 "token_type": "Bearer", "expiry": "…"}}

- macOS on agy 1.1.7+ writes **no token file at all** — the OAuth credential
  goes into the **Keychain**. A signed-in Mac therefore has neither file above,
  and a file-only check locks the user out of a harness they can actually run.

Detection is file-first with a macOS-only CLI fallback, mirroring
:func:`omnigent.onboarding.ambient._claude_login_detected` (Claude Code has the
same Keychain split). A non-empty ``access_token`` / ``refresh_token`` string —
flat (macOS) or nested under ``token`` (Linux) — counts as a completed login.
When no file carries one, macOS asks the CLI itself via
:func:`omnigent.onboarding.harness_install.harness_cli_logged_in`, which runs
``agy models`` (exit 0 only when signed in) and so reads the credential wherever
agy stored it, Keychain included. Linux stays purely file-based and
subprocess-free: the token file is accurate there, so the fallback would add
cost while weakening a signal that already works.

The fallback asks the CLI rather than inspecting
``~/.gemini/antigravity-cli/settings.json`` because omnigent's own CLI launch
path creates that file under the real home before agy ever starts
(:func:`omnigent.antigravity_native_bridge.ensure_agy_feedback_survey_disabled`).
Its presence proves only that omnigent ran, not that anyone signed in, so
keying on it would leave the credential gate permanently satisfied. Nothing
omnigent writes can make ``agy models`` exit 0.

Like :func:`omnigent.onboarding.ambient.codex_auth_has_credential` and
:func:`omnigent.onboarding.ambient.claude_auth_has_credential`, the file check
cannot detect server-side revocation — its job is to reject the "no-credential /
file-empty / file-corrupt" cases. On the rare machine carrying *both* files —
e.g. a home directory migrated across OSes — a stale token could read as
logged-in; that is benign (agy re-drives OAuth on launch) and ``agy models``
remains the authoritative check.

This module feeds a readiness path that must never raise, so every probe turns
failure — an unreadable home, a missing ``agy`` binary, a hung or non-zero
``agy models`` — into ``False``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from omnigent.onboarding import harness_install
from omnigent.onboarding.provider_config import GEMINI_FAMILY

_GEMINI_DIR: Path = Path(os.path.expanduser("~")) / ".gemini"

# Where ``agy`` writes its OAuth token after the first interactive sign-in. The
# location is platform-specific (verified against agy 1.0.10), so detection
# checks both: macOS uses ``oauth_creds.json``; Linux uses
# ``antigravity-cli/antigravity-oauth-token``.
DEFAULT_GEMINI_OAUTH_CREDS: Path = _GEMINI_DIR / "oauth_creds.json"
LINUX_GEMINI_OAUTH_TOKEN: Path = _GEMINI_DIR / "antigravity-cli" / "antigravity-oauth-token"
GEMINI_OAUTH_CRED_PATHS: tuple[Path, ...] = (DEFAULT_GEMINI_OAUTH_CREDS, LINUX_GEMINI_OAUTH_TOKEN)

# The OAuth fields whose non-empty presence proves a completed sign-in. agy
# stores them flat (macOS) or nested under ``token`` (Linux); detection looks in
# both places.
_OAUTH_CRED_FIELDS: tuple[str, ...] = ("access_token", "refresh_token")


def _file_carries_token(path: Path) -> bool:
    """Return whether a single creds file parses as JSON carrying a usable token.

    Accepts both agy token shapes: the OAuth fields flat at the top level
    (macOS) or nested under a ``token`` object (Linux).

    :param path: Path to a candidate credential file.
    :returns: ``True`` when *path* reads as a JSON object with a non-empty
        ``access_token`` / ``refresh_token`` string (flat or under ``token``);
        ``False`` when missing, unreadable, non-UTF-8, not JSON, not an object,
        or token-less.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # OSError: missing / unreadable file. UnicodeDecodeError: the file
        # exists but holds non-UTF-8 bytes (a corrupt creds file) — treat it as
        # "no usable credential" rather than letting the decode crash.
        return False
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False
    if not isinstance(data, dict):
        return False
    # macOS stores the OAuth fields flat; Linux nests them under ``token``.
    nested = data.get("token")
    sources = [data, nested] if isinstance(nested, dict) else [data]
    for source in sources:
        for field in _OAUTH_CRED_FIELDS:
            value = source.get(field)
            if isinstance(value, str) and value.strip():
                return True
    return False


def gemini_auth_has_credential(creds_path: Path | None = None) -> bool:
    """Return whether ``agy`` has a usable OAuth login on this machine.

    With *creds_path* unset, checks every known platform location
    (:data:`GEMINI_OAUTH_CRED_PATHS`) and returns ``True`` if any carries a
    usable token — so a logged-in user is recognized on both macOS
    (``oauth_creds.json``) and Linux
    (``antigravity-cli/antigravity-oauth-token``). When no file carries a token
    and the host is macOS, falls back to asking the CLI itself
    (:func:`omnigent.onboarding.harness_install.harness_cli_logged_in`, which
    runs ``agy models``), because agy 1.1.7+ keeps the credential in the
    Keychain and writes no token file. With *creds_path* set, checks only that
    file — the caller named the signal it wants, so no CLI fallback runs.

    A file counts as a completed login when it parses as a JSON object with a
    non-empty ``access_token`` / ``refresh_token`` string, flat or nested under
    ``token``. The file check cannot detect server-side revocation; the macOS
    CLI fallback can.

    Never raises — an unreadable file or home directory, a missing ``agy``
    binary, and a hung or failing ``agy models`` all read as ``False``.

    :param creds_path: A specific credential file to check; ``None`` checks all
        of :data:`GEMINI_OAUTH_CRED_PATHS`, then the macOS CLI fallback.
    :returns: ``True`` when a usable Gemini credential is present — a token file
        on any platform, or a Keychain credential seen through the macOS CLI
        fallback. ``False`` when every checked file is missing, unreadable,
        non-UTF-8, not JSON, not an object, or token-less, and the fallback
        either does not apply (non-macOS, or an explicit *creds_path*) or
        reports no login.
    """
    paths = (creds_path,) if creds_path is not None else GEMINI_OAUTH_CRED_PATHS
    if any(_file_carries_token(path) for path in paths):
        return True
    if creds_path is not None or sys.platform != "darwin":
        return False
    # agy 1.1.7+ on macOS keeps OAuth in the Keychain and writes no token file,
    # so only the CLI can see the login. Resolved through the module so a test
    # can monkeypatch it without this call site caching the old function.
    try:
        return harness_install.harness_cli_logged_in(GEMINI_FAMILY)
    except (OSError, ValueError, subprocess.SubprocessError):
        # Readiness must never raise: a probe that cannot run is "no credential".
        return False


def gemini_login_detected() -> bool:
    """Return whether a usable ``agy`` OAuth credential is present on this machine.

    Thin wrapper over :func:`gemini_auth_has_credential` across all known
    platform locations (:data:`GEMINI_OAUTH_CRED_PATHS`). Used by the readiness
    layer to check whether the ``antigravity-native`` harness has a Google
    subscription credential. This is a hard launch gate, not a hint — a wrong
    ``True`` lets a runner spawn that dies on its first turn — so the signal has
    to prove a credential rather than merely suggest one.

    :returns: ``True`` when a token file (macOS ``~/.gemini/oauth_creds.json``,
        Linux ``~/.gemini/antigravity-cli/antigravity-oauth-token``) carries a
        usable credential, or — on macOS only, where agy 1.1.7+ stores OAuth in
        the Keychain — ``agy models`` reports a signed-in CLI; ``False``
        otherwise.
    """
    return gemini_auth_has_credential()
