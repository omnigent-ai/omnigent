"""GitHub's credential facet: git auth for github.com and the gh CLI's ``hosts.yml``.

The gh CLI does not consult git's ``credential.helper`` for its own API calls (``gh api``,
``gh pr``, ``gh issue``); it reads ``oauth_token`` from ``hosts.yml`` (or ``GH_TOKEN``). So
the facet materializes the owner's brokered token there, and the refresher re-writes it before
the GitHub App user token expires (about 8 hours).

NB deliberately no ``gh auth setup-git``: that would register gh as a git credential helper
and compete with the per-user broker, which must stay authoritative for github.com so git ops
fetch the token fresh per op.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path
from typing import Any

import yaml

from omnigent.git_credential import CredentialFacet, broker_hosts
from omnigent.git_providers import Instances

_PROVIDER_ID = "github"
_GIT_HOST = "github.com"
_API_HOSTS = frozenset({"api.github.com"})
# git's username for a GitHub token when the broker names none.
_TOKEN_USERNAME = "x-access-token"
# The account keys of a hosts.yml host entry that hold or name the credential.
_ACCOUNT_KEYS = ("oauth_token", "user")

# The name avoids a TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL segment: the sandbox launcher rejects
# env-passthrough names that look like a credential, and this is a plain integer.
REFRESH_INTERVAL_ENV_VAR = "OMNIGENT_GH_REFRESH_INTERVAL_S"
REFRESH_DEFAULT_S = 1800


def _gh_config_dir(home: Path | None = None) -> Path:
    """Return the gh CLI config dir.

    ``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``, else ``<home>/.config/gh``. *home*
    defaults to :meth:`Path.home`, which is read only when neither override is set.
    """
    override = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return Path(override)
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return Path(xdg_config_home) / "gh"
    return (Path.home() if home is None else home) / ".config" / "gh"


def _cli_login(cred: dict[str, Any]) -> str:
    """Return the gh ``user`` for a broker credential, preferring the owner's GitHub login."""
    return str(cred.get("login") or cred.get("username") or _TOKEN_USERNAME)


def _read_hosts(hosts_path: Path) -> dict[Any, Any]:
    """Read gh's multi-host ``hosts.yml``.

    A missing, unreadable, or non-mapping file is treated as empty (never a failure); the worst
    case is starting a fresh map.
    """
    with contextlib.suppress(OSError, yaml.YAMLError):
        loaded = yaml.safe_load(hosts_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            return loaded
    return {}


def _replace_hosts(hosts_path: Path, hosts: dict[Any, Any]) -> bool:
    """Replace gh's ``hosts.yml`` with *hosts*; :func:`yaml.safe_dump` quotes every value.

    :returns: ``True`` when written; ``False`` on any filesystem error.
    """
    tmp: str | None = None
    try:
        hosts_path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file 0600 whatever the umask, and os.replace swaps it in
        # atomically, so the token is never world-readable and gh never reads a partial file.
        fd, tmp = tempfile.mkstemp(dir=hosts_path.parent, prefix=".hosts.", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(hosts, handle, default_flow_style=False, sort_keys=True)
        os.replace(tmp, hosts_path)
    except (OSError, yaml.YAMLError):
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


def _write_hosts_entry(hosts_path: Path, login: str, token: str) -> bool:
    """Merge the owner's ``github.com`` auth into gh's ``hosts.yml``.

    Writes only the ``github.com`` key and merges it into the existing document, so a user's
    other hosts survive (``hosts.yml`` is a multi-host map: a GitHub Enterprise entry, a second
    account). The host's ``GH_CONFIG_DIR`` is not always a throwaway sandbox one: a local
    ``omnigent host`` resolves it to the real ``~/.config/gh``, where truncating would destroy
    the developer's config on every refresh.

    :returns: ``True`` when written; ``False`` on any filesystem error.
    """
    hosts = _read_hosts(hosts_path)
    entry = hosts.get(_GIT_HOST)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"oauth_token": token, "user": login, "git_protocol": "https"})
    hosts[_GIT_HOST] = entry
    return _replace_hosts(hosts_path, hosts)


def _clear_hosts_entry(hosts_path: Path) -> None:
    """Remove the ``github.com`` token and user from gh's ``hosts.yml``, keeping the rest.

    gh 2.40 and later also copies the active account's token under ``users.<login>``, so that
    account goes too; other accounts, keys, and hosts stay.
    """
    hosts = _read_hosts(hosts_path)
    entry = hosts.get(_GIT_HOST)
    if not isinstance(entry, dict) or not any(key in entry for key in _ACCOUNT_KEYS):
        return
    accounts = entry.get("users")
    if isinstance(accounts, dict):
        accounts.pop(entry.get("user"), None)
        if not accounts:
            del entry["users"]
    for key in _ACCOUNT_KEYS:
        entry.pop(key, None)
    if not entry:
        del hosts[_GIT_HOST]
    _replace_hosts(hosts_path, hosts)


class GitHubCredential:
    """github.com, plus GitHub Enterprise instance hosts once the broker lists them."""

    def refresh_interval_s(self) -> int:
        """Return the ``hosts.yml`` refresh interval: the env override, else 30 minutes.

        A non-positive value disables the refresher; a value that is not an integer uses the
        default.
        """
        raw = (os.environ.get(REFRESH_INTERVAL_ENV_VAR) or "").strip()
        if not raw:
            return REFRESH_DEFAULT_S
        try:
            return int(raw)
        except ValueError:
            return REFRESH_DEFAULT_S

    def hosts(self, instances: Instances) -> frozenset[str]:
        """Return github.com plus the configured GitHub instance hosts."""
        configured = (host.lower() for host in instances.hosts_for(_PROVIDER_ID) if host)
        return frozenset({_GIT_HOST, *configured})

    def api_hosts(self) -> frozenset[str]:
        """Return the GitHub REST API host."""
        return _API_HOSTS

    def git_username(self, cred: dict[str, Any]) -> str:
        """Return the broker's username, else ``x-access-token``."""
        return str(cred.get("username") or _TOKEN_USERNAME)

    def write_cli_config(self, cred: dict[str, Any], home: Path) -> bool:
        """Write the owner's token into gh's ``hosts.yml`` under ``github.com``.

        Skipped when the broker lists hosts without github.com, so a token for another host
        never lands under github.com.
        """
        token = cred.get("token")
        listed = broker_hosts(cred)
        if not token or (listed is not None and _GIT_HOST not in listed):
            return False
        hosts_path = _gh_config_dir(home) / "hosts.yml"
        return _write_hosts_entry(hosts_path, _cli_login(cred), str(token))

    def clear_cli_config(self, home: Path) -> None:
        """Remove the ``github.com`` token that :meth:`write_cli_config` writes."""
        _clear_hosts_entry(_gh_config_dir(home) / "hosts.yml")


CREDENTIAL: CredentialFacet = GitHubCredential()
