"""Git credential helper that fetches the session's GitHub token from the server.

Installed in a managed sandbox as git's ``credential.helper`` for
``github.com``. On each HTTPS auth challenge git runs this with ``get`` and the
request on stdin; the helper calls the server's host-facing credential endpoint
(:mod:`omnigent.server.routes.host_credentials`, with ``provider=github``) over
the sandbox's existing authenticated channel and prints back ``username`` /
``password``.

Why this shape:
- The **GitHub token is never persisted** in the sandbox — it's fetched fresh
  per git operation and lives only in this short-lived process's stdout.
- It is **executor-agnostic**: every executor already starts the host with a
  server URL + launch token, so nothing GitHub-specific is injected per
  executor. The host bakes the endpoint coordinates (server URL, host id, launch
  token) into the ``credential.helper`` invocation, so the helper works
  regardless of whether the process running git inherited the runner's env.

The launch token *does* live in the git config (a lesser, host-scoped,
expiring credential already present in this disposable sandbox); the user's
GitHub token does not.

Threat model: this removes the GitHub token from disk/env, not the ability of
in-sandbox code to request one. Any process that can read the launch token (git
config / argv) can call the endpoint and obtain the owner's full-scope user
token for its lifetime; teardown stops future fetches but does not revoke an
already-fetched token. The trust boundary is the sandbox, not this helper.

Run as: ``git credential.helper`` →
``python -m omnigent.git_credential_github --server <url> --host-id <id>
--host-token <tok>``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shlex
import subprocess
import sys

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR as _HOST_TOKEN_ENV_VAR
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER

_TIMEOUT_S = 15.0


def _read_git_request() -> dict[str, str]:
    """Parse git's ``key=value`` credential request from stdin (blank-line terminated)."""
    fields: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _credential_url(server: str, host_id: str) -> str:
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/github"


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    """Fetch the credential endpoint JSON, or ``None`` on any failure."""
    try:
        resp = httpx.get(
            _credential_url(server, host_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def _fetch_credential(server: str, host_id: str, host_token: str) -> tuple[str, str] | None:
    """Fetch ``(username, token)`` from the server, or ``None`` if unavailable."""
    data = _fetch(server, host_id, host_token)
    if not data or not data.get("connected") or not data.get("token"):
        return None
    return str(data.get("username") or "x-access-token"), str(data["token"])


def _git_config(*args: str) -> None:
    """Run ``git config --global`` best-effort (never raises; git may be absent)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "config", "--global", *args],
            check=False,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )


def _install_broker_helper(server_url: str, host_id: str, token: str) -> None:
    """Make the per-user broker the sole github.com credential helper.

    Bakes the (lesser, expiring) launch token into the helper invocation so it
    works regardless of whether the agent's process inherited the env; the GitHub
    token itself is fetched per-op and never stored. Resets the github.com helper
    chain first: git accumulates credential helpers across scopes and runs them in
    parse order (system → global), stopping at the first that returns a full
    credential. A managed image installs a wider-scope helper that answers
    github.com from a shared ``$GIT_TOKEN``; parsed before this --global entry it
    would vend first and silently bypass the per-user broker. An empty value
    clears the inherited chain for github.com, so only the broker remains.

    Idempotent by ``--replace-all``: the workspace-prep init container and the
    host both wire the broker into the same shared ~/.gitconfig (and the host
    re-runs on every resume). A plain set can't overwrite the 2+ values that
    leaves, so without ``--replace-all`` broker entries would pile up.
    """
    helper = (
        "!python3 -m omnigent.git_credential_github "
        f"--server {shlex.quote(server_url)} --host-id {shlex.quote(host_id)} "
        f"--host-token {shlex.quote(token)}"
    )
    _git_config("--replace-all", "credential.https://github.com.helper", "")
    _git_config("--add", "credential.https://github.com.helper", helper)


def configure_host_git(server_url: str, host_id: str) -> None:
    """Configure git in a managed sandbox to use the GitHub credential broker.

    Called by ``omnigent host`` at startup — executor-agnostic, since the host
    runs in every executor and holds ``$OMNIGENT_HOST_TOKEN``. Makes the broker
    the authoritative github.com ``credential.helper`` (fetching the owner's
    token from the server per git op; never written to disk) and, when the owner
    has GitHub connected, sets the commit author to them. Best-effort: never
    raises (git may be absent, or GitHub not configured on the server).
    """
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return
    _install_broker_helper(server_url, host_id, token)
    data = _fetch(server_url, host_id, token) or {}
    owner = str(data.get("owner") or "")
    if data.get("connected") and "@" in owner:
        _git_config("user.email", owner)
        login = data.get("login")
        _git_config("user.name", str(login) if login else owner.split("@", 1)[0])


def configure_clone_credentials(server_url: str, host_id: str) -> bool:
    """Wire the per-user broker for the initial workspace clone.

    Called by the managed-sandbox ``workspace-prep`` init container BEFORE it
    clones the workspace repo. Unlike :func:`configure_host_git` (which makes the
    broker authoritative for the running host unconditionally), this keeps the
    ambient credential chain — notably the image's shared ``$GIT_TOKEN`` helper —
    only when the owner is *definitively* not linked, so a shared-token clone
    still works for them.

    Fails closed on an ambiguous probe: :func:`_fetch` returns ``None`` on any
    transient fault (timeout, non-200, bad JSON), which is indistinguishable from
    "not linked". Treating that as unlinked would silently clone a *linked*
    owner's private repo under the shared image identity, defeating the per-user
    contract. So only a **successful** ``connected: false`` keeps the shared
    fallback; a connected owner — or an unresolved probe — installs the broker,
    so the clone authenticates per-user or fails visibly instead of quietly
    falling back to the shared token. Best-effort: never raises.

    :returns: ``True`` when the broker was wired (owner connected, or the probe
        was inconclusive); ``False`` only when the owner is confirmed not linked.
    """
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if data is not None and not data.get("connected"):
        return False
    _install_broker_helper(server_url, host_id, token)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--host-token", required=True)
    # git passes the operation (get/store/erase) as the first positional arg.
    parser.add_argument("operation", nargs="?", default="get")
    args, _ = parser.parse_known_args(argv)

    # Only ``get`` returns credentials; ``store``/``erase`` are no-ops (nothing
    # is persisted — the token is re-fetched next time).
    if args.operation != "get":
        return 0

    request = _read_git_request()
    # Fail closed: only vend for github.com over https. A missing/empty host, a
    # different host, or a non-https protocol declines (return 0, no output) so
    # git falls through to the next helper — the brokered token can never leak
    # to another host even if this is ever wired as a global credential helper.
    if request.get("host") != "github.com" or request.get("protocol") != "https":
        return 0

    cred = _fetch_credential(args.server, args.host_id, args.host_token)
    if cred is None:
        return 0  # decline; git tries the next helper / prompts
    username, token = cred
    sys.stdout.write(f"username={username}\npassword={token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
