"""File-backed admin roster for the OSS auth providers.

A plaintext file (default ``<data_dir>/admins``) lists identities that
should be granted admin rights. It is consulted at each login and
promotes a matching user to ``is_admin=True`` in the database. This is
the *primary* admin mechanism for the ``oidc`` provider — OIDC has no
other admin signal (the IdP doesn't tell us who is an operator), so
without this every OIDC user would be a non-admin member forever. It
also applies to the ``accounts`` provider for consistency (an operator
can add a teammate's username to the file rather than minting an admin
invite).

Design decisions (locked with the product owner):

- **File only.** No env-var roster. The file is editable at runtime
  without a redeploy — in the bundled Docker stack it lives on the
  persistent ``/data`` volume alongside the other operator-editable
  files (e.g. ``allowed_domains``).
- **Additive promotion only.** A listed identity is promoted on login.
  Removing an identity from the file NEVER demotes them — demotion is a
  separate, explicit admin action. This makes the file safe to edit:
  you cannot accidentally lock the deploy out of its bootstrap admin by
  forgetting to list them.
- **mtime-cached.** The file is re-read only when its modification time
  changes, so the hot path (cookie validation does not touch it; only
  the comparatively rare login event does) avoids redundant I/O while
  still picking up edits without a restart.

File format: one identity per line, lowercased on read. ``#`` starts a
comment (inline or whole-line); blank lines are ignored. Example::

    # Omnigent admins
    alice@example.com
    bob@example.com   # founder

A missing or unreadable file yields an empty roster (no error) — the
admin list is optional, and an auth path must never fail because the
operator hasn't created the file.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)


def resolve_data_dir() -> Path:
    """Resolve the directory that holds OSS server-side state files.

    Co-locates the admin list and the allowed-domains file on a single
    mounted volume so all operator-editable state lives together.
    Resolution:

    1. The parent directory of ``OMNIGENT_ADMIN_CREDENTIALS_PATH`` if
       that env var is set (Docker compose points it at
       ``/data/admin-credentials`` purely to anchor the data dir at
       ``/data``; no credentials file is written there). The name is
       retained for compatibility — see #2832 for the rename.
    2. ``~/.omnigent`` for a laptop deploy.

    :returns: The resolved data directory. Not created here — callers
        that only read tolerate a missing directory.
    """
    explicit_creds = os.environ.get("OMNIGENT_ADMIN_CREDENTIALS_PATH", "").strip()
    if explicit_creds:
        return Path(explicit_creds).parent
    return Path.home() / ".omnigent"


def resolve_admin_list_path() -> Path:
    """Resolve the path to the admin-list file.

    :returns: ``OMNIGENT_ADMIN_LIST_PATH`` if set, else
        ``<data_dir>/admins`` (see :func:`resolve_data_dir`).
    """
    explicit = os.environ.get("OMNIGENT_ADMIN_LIST_PATH", "").strip()
    if explicit:
        return Path(explicit)
    return resolve_data_dir() / "admins"


class MtimeCachedIdentitySet:
    """A set of lowercased tokens loaded from a plaintext file.

    Re-reads the file only when its modification time (nanosecond
    precision) changes. Used for both the admin list and the
    allowed-domains file — both are "one token per line, ``#``
    comments, lowercased" files with the same optional-and-must-not-
    fail-auth semantics.

    :param path: Path to the backing file. The file need not exist;
        a missing file is treated as an empty set.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._cached: frozenset[str] = frozenset()
        # mtime_ns of the last successful load, or ``None`` when the
        # file was absent. ``_loaded`` distinguishes "never loaded"
        # from "loaded an absent file" so the first call always reads.
        self._mtime_ns: int | None = None
        self._loaded = False

    def _refresh(self) -> None:
        """Reload from disk if the file changed since the last read."""
        try:
            stat = self._path.stat()
        except FileNotFoundError:
            # Absent file → empty set. Re-checked every call (cheap)
            # so creating the file later takes effect immediately.
            self._cached = frozenset()
            self._mtime_ns = None
            self._loaded = True
            return

        if self._loaded and self._mtime_ns == stat.st_mtime_ns:
            return  # unchanged since last load

        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            # Unreadable (permissions, race with a writer) — fail open
            # to empty rather than breaking the login path. Logged so
            # a misconfigured file is diagnosable.
            logger.warning("admin/domain file %s unreadable: %s", self._path, exc)
            self._cached = frozenset()
            self._mtime_ns = stat.st_mtime_ns
            self._loaded = True
            return

        tokens: set[str] = set()
        for raw_line in text.splitlines():
            token = raw_line.split("#", 1)[0].strip().lower()
            if token:
                tokens.add(token)
        self._cached = frozenset(tokens)
        self._mtime_ns = stat.st_mtime_ns
        self._loaded = True

    def contains(self, value: str) -> bool:
        """Whether ``value`` (case-insensitively) is in the file.

        :param value: The token to test, e.g. ``"Alice@Example.com"``.
        :returns: ``True`` if the lowercased value is present.
        """
        self._refresh()
        return value.strip().lower() in self._cached

    def snapshot(self) -> frozenset[str]:
        """Return the current set, refreshing from disk first.

        :returns: A frozenset of the lowercased tokens currently in
            the file (empty if the file is absent/unreadable).
        """
        self._refresh()
        return self._cached


class AdminList:
    """The admin roster: a static set (from config) unioned with a file.

    The ``extra`` set is the canonical, declarative source — admins
    listed in the server config (``admins:``). The file
    (``<data_dir>/admins``) is an optional supplement that's editable at
    runtime without a restart (mtime-cached). An identity in *either* is
    an admin.

    :param path: Path to the admin-list file (need not exist).
    :param extra: Static admin identities from config, e.g. from
        ``admins:`` in the server YAML. Lowercased on construction.
    """

    def __init__(self, path: Path, extra: frozenset[str] = frozenset()) -> None:
        self.path = path
        self._set = MtimeCachedIdentitySet(path)
        self._extra = frozenset(e.strip().lower() for e in extra if e.strip())

    def is_admin(self, identity: str) -> bool:
        """Whether ``identity`` is listed as an admin (config or file).

        :param identity: User identifier — an email in OIDC mode, a
            username in accounts mode, e.g. ``"alice@example.com"``.
        :returns: ``True`` if the (lowercased) identity is in the config
            set or the file.
        """
        return identity.strip().lower() in self._extra or self._set.contains(identity)


def load_admin_list(extra: frozenset[str] = frozenset()) -> AdminList:
    """Construct the :class:`AdminList` at the resolved default path.

    :param extra: Static admin identities from the server config's
        ``admins:`` key, unioned with the file.
    :returns: An :class:`AdminList` bound to :func:`resolve_admin_list_path`.
    """
    return AdminList(resolve_admin_list_path(), extra=extra)


class AdminFlagStore(Protocol):
    """The slice of a store needed to read and set a user's admin flag.

    Both :class:`~omnigent.stores.permission_store.PermissionStore`
    and :class:`~omnigent.server.accounts_store.SqlAlchemyAccountStore`
    satisfy this — they read and write the same ``users.is_admin``
    column. The promotion helper takes this Protocol so it works for
    both the OIDC path (PermissionStore) and the accounts path
    (AccountStore) without either importing the other.
    """

    def is_admin(self, user_id: str) -> bool:
        """Return whether ``user_id`` currently has the admin flag."""
        ...

    def set_admin(self, user_id: str, is_admin: bool) -> None:
        """Set the admin flag on an existing ``user_id`` row."""
        ...


def promote_if_listed(admin_list: AdminList, store: AdminFlagStore, user_id: str) -> bool:
    """Promote ``user_id`` to admin if the file lists them (additive).

    Idempotent and additive: only ever sets the flag to ``True``, and
    only when the user is listed but not already admin. Never demotes.
    Call this right after a successful login, once the user row is
    known to exist (so :meth:`AdminFlagStore.set_admin`'s ``UPDATE``
    matches a row).

    :param admin_list: The roster to consult.
    :param store: The store backing ``users.is_admin`` for this
        provider (PermissionStore for OIDC, AccountStore for accounts).
    :param user_id: The just-authenticated identity, e.g.
        ``"alice@example.com"``.
    :returns: ``True`` if this call promoted the user, ``False`` if
        they were not listed or were already admin.
    """
    if not admin_list.is_admin(user_id):
        return False
    if store.is_admin(user_id):
        return False
    store.set_admin(user_id, True)
    logger.info("admin_list: promoted %s to admin", user_id)
    return True
