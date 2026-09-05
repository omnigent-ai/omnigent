"""Git credential helper that fetches the session's GitHub token from the server.

Installed in a managed sandbox as git's ``credential.helper`` for
``github.com``. On each HTTPS auth challenge git runs this with ``get`` and the
request on stdin; the helper calls the server's host-facing credential endpoint
(:mod:`omnigent.server.routes.host_credentials`, with ``provider=github``) over
the sandbox's existing authenticated channel and prints back ``username`` /
``password``.

Why this shape:
- For **git**, the **GitHub token is never persisted** in the sandbox — it's
  fetched fresh per git operation and lives only in this short-lived process's
  stdout. (The gh CLI has no per-op credential hook, so
  :func:`configure_host_gh` does materialize the token into gh's ``hosts.yml``
  at launch — a within-sandbox persistence the threat model below already
  admits, matching how ``~/.databrickscfg`` is written — and
  :func:`start_host_gh_refresh` re-writes it on an interval so gh stays fresh
  across the ~8h token expiry the git broker otherwise handles per-op.)
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
import tempfile
import threading
import time
from pathlib import Path

import httpx
import yaml

from omnigent.host.identity import HOST_TOKEN_ENV_VAR as _HOST_TOKEN_ENV_VAR
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER

_TIMEOUT_S = 15.0


def _in_sandbox() -> bool:
    """Whether we're running inside a managed sandbox (host image sets ``IS_SANDBOX=1``).

    The broker host integrations auto-materialize the owner's credentials into
    the host's git / gh config. That's only ever wanted in a managed sandbox — a
    disposable, per-session filesystem. A local ``omnigent host`` shares the
    developer's real ``~/.gitconfig`` / ``~/.config/gh``, so every auto-apply must
    be a no-op there. ``IS_SANDBOX=1`` is baked into the managed host image and
    the k8s pod spec; it is absent on a local host.
    """
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


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
        data = resp.json()
    except ValueError:
        return None
    # Guard non-object JSON (a top-level list/string) so callers' ``data.get(...)``
    # can't raise — keeps the broker fetch's "never raises" contract honest.
    return data if isinstance(data, dict) else None


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
    runs in every executor and holds ``$OMNIGENT_HOST_TOKEN``. When the owner has
    GitHub connected (the per-user Connect model), makes the broker the
    authoritative github.com ``credential.helper`` (fetching the owner's token
    from the server per git op; never written to disk) and sets the commit author
    to them. Best-effort: never raises (git may be absent, or GitHub not
    configured on the server).

    Connected-gated, mirroring :func:`configure_clone_credentials`: a confirmed
    ``connected: false`` means the owner hasn't linked GitHub (a shared-
    ``$GIT_TOKEN`` or local deployment). Installing the broker would reset the
    github.com chain (clearing a shared ``$GIT_TOKEN`` helper) and then decline,
    breaking in-session git — so that path instead **clears** any broker helper a
    prior (inconclusive) clone probe may have installed, restoring the ambient
    helper. Only a connected owner, or an inconclusive probe (``None``,
    fail-closed like the clone), takes over github.com.

    Sandbox-only (see :func:`_in_sandbox`): a no-op outside a managed sandbox, so
    a local ``omnigent host`` never rewrites the developer's real ``~/.gitconfig``.
    """
    if not _in_sandbox():
        return
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return
    data = _fetch(server_url, host_id, token)
    if data is not None and not data.get("connected"):
        # Confirmed not linked: actively clear any broker helper a prior
        # (inconclusive) clone probe installed. Just returning would leave that
        # helper — and the chain reset it wrote — in place, stranding in-session
        # git behind a broker that now declines. Unsetting restores the ambient
        # shared-``$GIT_TOKEN`` helper.
        _git_config("--unset-all", "credential.https://github.com.helper")
        return
    _install_broker_helper(server_url, host_id, token)
    owner = str((data or {}).get("owner") or "")
    if data and data.get("connected") and "@" in owner:
        _git_config("user.email", owner)
        login = data.get("login")
        _git_config("user.name", str(login) if login else owner.split("@", 1)[0])


def _gh_config_dir() -> Path:
    """The gh CLI config dir (``GH_CONFIG_DIR`` override, else ``~/.config/gh``)."""
    override = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "gh"


def _write_gh_hosts(login: str, token: str) -> bool:
    """Materialize the owner's ``github.com`` auth into gh's ``hosts.yml``.

    The gh CLI does not consult git's ``credential.helper`` for its own API
    calls (``gh api`` / ``gh pr`` / ``gh issue``); it reads ``oauth_token`` from
    ``hosts.yml`` (or ``GH_TOKEN``).

    Writes **only the** ``github.com`` **key, merging** into any existing
    document so a user's other hosts survive (``hosts.yml`` is a multi-host map —
    a GitHub Enterprise entry, a second account). This matters because the host's
    ``GH_CONFIG_DIR`` is not always a throwaway sandbox one: a local ``omnigent
    host`` resolves it to the real ``~/.config/gh``, where truncating would
    destroy the developer's config on every refresh. Mirrors the Databricks
    config writer in :mod:`omnigent.onboarding.setup`, which mutates one section
    of ``~/.databrickscfg`` and writes the rest back rather than truncating.

    Written via a fresh ``0600`` temp file + :func:`os.replace`, so the token is
    never briefly world-readable and a concurrent ``gh`` read never sees a
    partial file; :func:`yaml.safe_dump` quotes values correctly whatever they
    contain.

    NB deliberately no ``gh auth setup-git``: that would register gh as a git
    credential helper and compete with the per-user broker, which must stay
    authoritative for github.com so git ops fetch the token fresh per op.

    :returns: ``True`` when written; ``False`` on any filesystem error.
    """
    hosts_path = _gh_config_dir() / "hosts.yml"
    # Merge into the existing document so other hosts / accounts are preserved. A
    # missing, unreadable, or non-mapping file is treated as empty (never a
    # failure) — the worst case is starting a fresh map.
    hosts: dict = {}
    with contextlib.suppress(OSError, yaml.YAMLError):
        loaded = yaml.safe_load(hosts_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            hosts = loaded
    entry = hosts.get("github.com")
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"oauth_token": token, "user": login, "git_protocol": "https"})
    hosts["github.com"] = entry

    tmp: str | None = None
    try:
        hosts_path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file 0600 (owner-only, umask-independent), so the
        # credential never has a world-readable window; os.replace swaps it in
        # atomically, so a concurrent gh read never sees a partial file.
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


def configure_host_gh(server_url: str, host_id: str) -> bool:
    """Authenticate the sandbox's gh CLI as the session owner.

    Companion to :func:`configure_host_git` (which wires git's per-op broker):
    the gh CLI has no per-op credential hook, so — like the Databricks config
    writer in :mod:`omnigent.onboarding.setup` materializes ``~/.databrickscfg``
    — the owner's brokered GitHub token is materialized into ``hosts.yml`` at
    host startup. The token is a short-lived, server-refreshed user token; it is
    re-fetched on each host launch, so a long-lived session should relaunch to
    refresh it.

    Best-effort: a no-op when the host token is absent, the broker is
    unreachable, or the owner hasn't linked GitHub (any ambient gh auth is left
    untouched). Never raises.

    :returns: ``True`` when gh was authenticated; ``False`` otherwise.
    """
    if not _in_sandbox():
        return False
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if not data or not data.get("connected") or not data.get("token"):
        return False
    gh_token = str(data["token"])
    login = str(data.get("login") or data.get("username") or "x-access-token")
    return _write_gh_hosts(login, gh_token)


# How often the background refresher re-materializes gh's hosts.yml, in seconds.
# NB the name deliberately avoids a TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL segment:
# the managed-sandbox launcher rejects env-passthrough names that look like a
# credential (they would land in the Pod spec/etcd), and this is a plain integer.
_GH_REFRESH_INTERVAL_ENV_VAR: str = "OMNIGENT_GH_REFRESH_INTERVAL_S"
_GH_REFRESH_DEFAULT_S: int = 1800


def _gh_refresh_interval_s() -> int:
    """Resolve the gh-token refresh interval (env override, else 30 min).

    Non-positive disables the refresher.
    """
    raw = (os.environ.get(_GH_REFRESH_INTERVAL_ENV_VAR) or "").strip()
    if not raw:
        return _GH_REFRESH_DEFAULT_S
    try:
        return int(raw)
    except ValueError:
        return _GH_REFRESH_DEFAULT_S


def start_host_gh_refresh(server_url: str, host_id: str) -> threading.Thread | None:
    """Keep the gh CLI's ``hosts.yml`` token fresh over a long-lived host.

    Unlike git (whose per-op broker helper re-fetches the server-refreshed
    token on every operation), the gh CLI reads a **static** ``hosts.yml``, so
    the token :func:`configure_host_gh` writes at startup goes stale when the
    GitHub App user token expires (~8h) and the server rotates it — a
    long-running session would then hit ``gh api`` 401s. This best-effort daemon
    thread re-materializes ``hosts.yml`` on an interval well under the token
    lifetime, so gh stays authenticated for the life of the host. (An
    agent-sandbox resume already refreshes it by re-running host startup; this
    covers a session that stays live without ever suspending.)

    :returns: The started daemon thread, or ``None`` when disabled (a
        non-positive interval, or outside a managed sandbox) — mainly for tests.
    """
    if not _in_sandbox():
        return None
    interval = _gh_refresh_interval_s()
    if interval <= 0:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            with contextlib.suppress(Exception):
                configure_host_gh(server_url, host_id)

    thread = threading.Thread(target=_loop, name="gh-token-refresh", daemon=True)
    thread.start()
    return thread


def configure_clone_credentials(server_url: str, host_id: str) -> bool:
    """Wire the per-user broker for the initial workspace clone.

    Called by the managed-sandbox ``workspace-prep`` init container BEFORE it
    clones the workspace repo. Like :func:`configure_host_git`, this is
    connected-gated: it keeps the ambient credential chain — notably the image's
    shared ``$GIT_TOKEN`` helper — when the owner is *definitively* not linked, so
    a shared-token clone still works for them.

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
