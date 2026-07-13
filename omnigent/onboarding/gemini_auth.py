"""Detect Google Antigravity (``agy``) OAuth credentials for ``antigravity-native``.

The ``agy`` CLI authenticates via a browser OAuth flow on first interactive run
— it has no ``agy login`` / ``agy auth status`` subcommand. *Where* and *how* it
persists the resulting token is **platform-specific** (verified live against agy
1.0.10):

- macOS writes ``~/.gemini/oauth_creds.json`` — a flat OAuth2 object::

      {"access_token": "ya29.…", "refresh_token": "1//0g…",
       "expiry_date": …, "id_token": "eyJ…", "scope": "…", "token_type": "Bearer"}

- Linux writes ``~/.gemini/antigravity-cli/antigravity-oauth-token`` — the OAuth
  object **nested under** ``token``::

      {"auth_method": "oauth",
       "token": {"access_token": "ya29.…", "refresh_token": "1//0g…",
                 "token_type": "Bearer", "expiry": "…"}}

Detection is file-based and subprocess-free: it checks **both** locations and
treats a non-empty ``access_token`` / ``refresh_token`` string — flat (macOS) or
nested under ``token`` (Linux) — as a completed login, so a logged-in user is
recognized on either platform. Like
:func:`omnigent.onboarding.ambient.codex_auth_has_credential` and
:func:`omnigent.onboarding.ambient.claude_auth_has_credential`, it cannot detect
server-side revocation — its only job is to reject the "no-credential /
file-empty / file-corrupt" cases. The readiness layer uses this as a fast path;
when a caller needs a live, revocation-aware verdict (robust to any future
change of the token path/shape)
:func:`omnigent.onboarding.harness_install.harness_cli_logged_in` asks the CLI
itself by running ``agy models`` (exit 0 only when signed in). On the rare
machine carrying *both* files — e.g. a home directory migrated across OSes — a
stale token could read as logged-in; that is benign (agy re-drives OAuth on
launch) and ``agy models`` is the authoritative check.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

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
    (``antigravity-cli/antigravity-oauth-token``). With *creds_path* set, checks
    only that file.

    A file counts as a completed login when it parses as a JSON object with a
    non-empty ``access_token`` / ``refresh_token`` string, flat or nested under
    ``token``. This cannot detect server-side revocation — for that, see
    :func:`omnigent.onboarding.harness_install.harness_cli_logged_in`.

    :param creds_path: A specific credential file to check; ``None`` checks all
        of :data:`GEMINI_OAUTH_CRED_PATHS`.
    :returns: ``True`` when a usable Gemini credential is present; ``False``
        when every checked file is missing, unreadable, non-UTF-8, not JSON, not
        an object, or token-less.
    """
    paths = (creds_path,) if creds_path is not None else GEMINI_OAUTH_CRED_PATHS
    return any(_file_carries_token(path) for path in paths)


def gemini_login_detected() -> bool:
    """Return whether a usable ``agy`` OAuth credential is present on this machine.

    Thin wrapper over :func:`gemini_auth_has_credential` across all known
    platform locations (:data:`GEMINI_OAUTH_CRED_PATHS`). Used by the readiness
    layer to check whether the ``antigravity-native`` harness has a Google
    subscription credential without spawning any subprocess.

    :returns: ``True`` when agy's token (macOS ``~/.gemini/oauth_creds.json`` or
        Linux ``~/.gemini/antigravity-cli/antigravity-oauth-token``) carries a
        usable credential; ``False`` otherwise.
    """
    return gemini_auth_has_credential()
