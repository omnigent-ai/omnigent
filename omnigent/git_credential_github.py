"""Deprecated GitHub-only entry point of the git credential helper.

@deprecated, remove in 0.19.0: use :mod:`omnigent.git_credential`, which serves every git
provider with a credential facet. Workspace-prep scripts rendered by older servers import this
module and replace its private hooks, and a sandbox image can run a different version than the
server, so every name here keeps its signature and its GitHub-only behavior. Each one delegates
to :mod:`omnigent.git_credential` and reaches the other names here through this module at call
time, so a caller that replaces one of them still changes what the others do.

Run as: ``git credential.helper`` →
``python -m omnigent.git_credential_github --server <url> --host-id <id> --host-token <tok>``.
"""

from __future__ import annotations

import subprocess  # noqa: F401 - callers patch subprocess.run through this module
import sys
import threading
import time  # noqa: F401 - callers patch time.sleep through this module
from pathlib import Path

import httpx  # noqa: F401 - callers patch httpx.get through this module

from omnigent import git_credential as _generic
from omnigent.git_credential import github as _github

# Callers read these two names through this module.
from omnigent.host.identity import HOST_TOKEN_ENV_VAR as _HOST_TOKEN_ENV_VAR  # noqa: F401
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER  # noqa: F401

_PROVIDER_ID = "github"
_GITHUB_HOST = "github.com"
_MODULE = "omnigent.git_credential_github"

# @deprecated, remove in 0.19.0: these constants mirror omnigent.git_credential.
_TIMEOUT_S = _generic._TIMEOUT_S
_GH_REFRESH_INTERVAL_ENV_VAR: str = _github.REFRESH_INTERVAL_ENV_VAR
_GH_REFRESH_DEFAULT_S: int = _github.REFRESH_DEFAULT_S


def _in_sandbox() -> bool:
    """@deprecated, remove in 0.19.0. Whether this runs in a managed sandbox (``IS_SANDBOX=1``)."""
    return _generic._in_sandbox()


def _read_git_request() -> dict[str, str]:
    """@deprecated, remove in 0.19.0. Parse git's credential request from stdin."""
    return _generic._read_git_request()


def _credential_url(server: str, host_id: str) -> str:
    """@deprecated, remove in 0.19.0. Return the broker URL of the owner's GitHub credential."""
    return _generic._credential_url(server, host_id, _PROVIDER_ID)


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    """@deprecated, remove in 0.19.0. Fetch the GitHub broker JSON, or ``None`` if inconclusive.

    Only the server's explicit no-provider 404 maps to ``connected: false``.
    """
    return _generic._fetch(server, host_id, host_token, _PROVIDER_ID)


def _fetch_credential(server: str, host_id: str, host_token: str) -> tuple[str, str] | None:
    """@deprecated, remove in 0.19.0. Fetch ``(username, token)``, or ``None`` if unavailable."""
    data = _fetch(server, host_id, host_token)
    if data is None or not _generic._vendable(data):
        return None
    return _github.CREDENTIAL.git_username(data), str(data["token"])


def _git_config(*args: str) -> None:
    """@deprecated, remove in 0.19.0. Run ``git config --global`` best-effort; never raises."""
    _generic._git_config(*args)


def _install_broker_helper(server_url: str, host_id: str, token: str) -> None:
    """@deprecated, remove in 0.19.0. Make this module the only github.com credential helper.

    Resets the github.com helper chain, then adds the helper, both through :func:`_git_config`.
    """
    command = _generic._helper_command(server_url, host_id, token, module=_MODULE)
    _generic._install_helper(_GITHUB_HOST, command, git_config=_git_config)


def configure_host_git(server_url: str, host_id: str) -> None:
    """@deprecated, remove in 0.19.0: use ``omnigent.git_credential.configure_host_credentials``.

    In a managed sandbox, make the broker the github.com credential helper and attribute
    commits to a connected owner. A confirmed not-linked owner instead loses every global
    github.com helper, which restores the ambient one. Best-effort: never raises.
    """
    if not _in_sandbox():
        return
    token = _generic._host_token()
    if not token:
        return
    data = _fetch(server_url, host_id, token)
    if _generic._confirmed_unlinked(data):
        _git_config("--unset-all", _generic._helper_key(_GITHUB_HOST))
        return
    _install_broker_helper(server_url, host_id, token)
    _generic._set_commit_identity(data, git_config=_git_config)


def _gh_config_dir() -> Path:
    """@deprecated, remove in 0.19.0. Return the gh CLI config dir (``GH_CONFIG_DIR`` first)."""
    return _github._gh_config_dir()


def _write_gh_hosts(login: str, token: str) -> bool:
    """@deprecated, remove in 0.19.0. Merge github.com auth into gh's ``hosts.yml``.

    Written 0600 and atomically, keeping the other hosts in the file.

    :returns: ``True`` when written; ``False`` on any filesystem error.
    """
    return _github._write_hosts_entry(_gh_config_dir() / "hosts.yml", login, token)


def configure_host_gh(server_url: str, host_id: str) -> bool:
    """@deprecated, remove in 0.19.0: use ``omnigent.git_credential.configure_host_credentials``.

    In a managed sandbox, write a connected owner's brokered token into gh's ``hosts.yml``.
    Best-effort: never raises.

    :returns: ``True`` when gh was authenticated; ``False`` otherwise.
    """
    if not _in_sandbox():
        return False
    token = _generic._host_token()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if data is None or not _generic._vendable(data):
        return False
    return _write_gh_hosts(_github._cli_login(data), str(data["token"]))


def _gh_refresh_interval_s() -> int:
    """@deprecated, remove in 0.19.0. Return the gh refresh interval (env, else 30 min)."""
    return _github.CREDENTIAL.refresh_interval_s()


def start_host_gh_refresh(server_url: str, host_id: str) -> threading.Thread | None:
    """@deprecated, remove in 0.19.0: use ``omnigent.git_credential.start_credential_refresh``.

    Start a daemon thread that re-runs :func:`configure_host_gh` every interval.

    :returns: The thread, or ``None`` outside a managed sandbox or for a non-positive interval.
    """
    if not _in_sandbox():
        return None
    return _generic._start_refresh_thread(
        _gh_refresh_interval_s(),
        lambda: configure_host_gh(server_url, host_id),
        name="gh-token-refresh",
    )


def configure_clone_credentials(server_url: str, host_id: str) -> bool:
    """@deprecated, remove in 0.19.0: use ``omnigent.git_credential.configure_clone_credentials``.

    Wire the broker for github.com before the workspace clone unless the owner is confirmed
    not linked; an inconclusive probe wires it anyway (fail closed). Best-effort: never raises.

    :returns: ``True`` when the broker was wired; ``False`` only when the owner is confirmed
        not linked or there is no launch token.
    """
    token = _generic._host_token()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if _generic._confirmed_unlinked(data):
        return False
    _install_broker_helper(server_url, host_id, token)
    return True


def main(argv: list[str] | None = None) -> int:
    """@deprecated, remove in 0.19.0: use ``omnigent.git_credential.main``.

    Answer a git credential request for github.com over https; anything else declines.
    """
    args = _generic._parse_args(argv)
    if args.operation != "get":
        return 0
    request = _read_git_request()
    # Fail closed: a missing host, another host, or a non-https protocol declines.
    if request.get("host") != _GITHUB_HOST or request.get("protocol") != "https":
        return 0
    cred = _fetch_credential(args.server, args.host_id, args.host_token)
    if cred is None:
        return 0
    sys.stdout.write(_generic._format_answer(*cred))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
