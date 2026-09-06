"""Persistent cookie-secret resolution for the ``accounts`` auth provider.

A user-set ``OMNIGENT_ACCOUNTS_COOKIE_SECRET`` always wins —
it's the right knob for HA deploys where every instance must
mint cookies with the same key. When nothing's set, this module
auto-generates a 32-byte secret on first boot and persists it
to a ``0600`` file in the server's data directory. Subsequent
boots read the same value, so sessions survive restarts.

Lives separate from :class:`AccountsConfig` on purpose:
``AccountsConfig.from_env()`` keeps its strict "every required
env var is present" contract (good for prod misconfig surface),
and this module is the CLI-layer ergonomic that supplies a
default *before* the config is read.

Caller pattern:

    secret_hex = load_or_generate_cookie_secret(data_dir)
    os.environ.setdefault("OMNIGENT_ACCOUNTS_COOKIE_SECRET", secret_hex)
    # …then call create_auth_provider() / AccountsConfig.from_env()
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

# Filename used to persist the auto-generated cookie secret inside
# the server's data directory. Hidden-ish prefix on purpose so a
# user `ls`-ing the data dir doesn't think it's a normal artifact.
_SECRET_FILENAME = "accounts-cookie-secret"


def load_or_generate_cookie_secret(data_dir: str | os.PathLike[str]) -> str:
    """Return a 64-hex-char cookie secret, persisting on first boot.

    Search order:

    1. ``OMNIGENT_ACCOUNTS_COOKIE_SECRET`` env var — operator-set,
       always wins. Returned unchanged for the caller to forward
       to :meth:`AccountsConfig.from_env`.
    2. ``<data_dir>/accounts-cookie-secret`` — auto-generated on a
       previous boot. Read verbatim.
    3. Fresh ``secrets.token_hex(32)`` — generated, written to the
       same path with ``0o600`` mode, returned.

    Parent dir is created with ``0o700`` if missing. File mode is
    re-applied on every write so a pre-existing file doesn't
    leak via a too-permissive umask.

    :param data_dir: Where the server keeps its persistent state.
    :returns: 64-character hex string.
    """
    env_value = os.environ.get("OMNIGENT_ACCOUNTS_COOKIE_SECRET")
    if env_value:
        return env_value

    path = Path(data_dir) / _SECRET_FILENAME
    if path.exists():
        existing = path.read_text().strip()
        if existing:
            return existing

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fresh = secrets.token_hex(32)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        # fchmod is POSIX-only; os.open(..., 0o600) already sets the right
        # bits at creation time, so the call is belt-and-suspenders on POSIX
        # and a no-op skip on Windows (where it would crash).
        if hasattr(os, "fchmod"):
            os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
        os.write(fd, fresh.encode("ascii") + b"\n")
    finally:
        os.close(fd)
    logger.info(
        "accounts: minted new cookie secret at %s "
        "(set OMNIGENT_ACCOUNTS_COOKIE_SECRET to share across instances)",
        path,
    )
    return fresh
