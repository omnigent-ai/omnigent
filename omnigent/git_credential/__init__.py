"""Git credential helper that fetches the session owner's forge token from the server.

A git provider plugs in with a credential facet: the ``credential`` module named by its
descriptor exposes ``CREDENTIAL``, a :class:`CredentialFacet`. The host installs this helper as
git's ``credential.https://<host>.helper`` for the hosts a facet serves. On each HTTPS auth
challenge git runs the helper with ``get`` and the request on stdin; the helper picks the facet
that serves the request's host, fetches the owner's credential from the server's host-facing
broker (:mod:`omnigent.server.routes.host_credentials`) at
``/v1/hosts/{host_id}/credentials/{provider_id}``, and prints ``username`` / ``password``.

Host rules: :meth:`CredentialFacet.hosts` names the hosts a facet can serve, and the broker
response lists the git hosts its credential is for in ``hosts`` (for example
``["github.com"]``). The helper vends a token, and is installed, only for a host in both sets.
When the response has no ``hosts`` list (an older server), only the provider's default hosts
qualify, so a github.com token never reaches a configured GitHub Enterprise host. Clearing
removes only this helper's own entries; a helper the user configured is never changed.

Why this shape:
- For **git**, the forge **token is never persisted** in the sandbox: it is fetched fresh per
  git operation and lives only in this short-lived process's stdout. A provider CLI with no
  per-op credential hook (the gh CLI) instead gets the token written into its own config at
  host startup and again on an interval (:func:`configure_host_credentials`,
  :func:`start_credential_refresh`), a within-sandbox persistence the threat model admits.
- It is **executor-agnostic**: every executor starts the host with a server URL and a launch
  token, so nothing provider-specific is injected per executor. The host bakes the server URL,
  host id, and launch token into the helper invocation, so the helper works whether or not the
  process running git inherited the runner's env.

Threat model: this removes the forge token from disk and env, not the ability of in-sandbox
code to request one. Any process that can read the launch token (git config, argv) can call the
endpoint and obtain the owner's credential for its lifetime; teardown stops future fetches but
does not revoke a fetched token. The trust boundary is the sandbox, not this helper.

Run as: ``git credential.helper`` →
``python -m omnigent.git_credential --server <url> --host-id <id> --host-token <tok>``.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import logging
import os
import shlex
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from omnigent.git_providers import EnvInstances, Instances, load_facet, providers
from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

_logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0
_MODULE = "omnigent.git_credential"
# The broker's 404 detail for a provider it does not broker. Any other 404 is inconclusive.
_UNKNOWN_PROVIDER_DETAIL = "unknown credential provider"
# Commands of this helper, including the deprecated GitHub-only module, as installed in git.
_OWN_HELPER_PREFIXES = (
    f"!python3 -m {_MODULE} --server ",
    f"!python3 -m {_MODULE}_github --server ",
)
# The same commands as a ``git config`` value pattern (a POSIX extended regex).
_OWN_HELPER_PATTERN = r"^!python3 -m omnigent\.git_credential(_github)? --server "

_GitConfig = Callable[..., None]


class CredentialFacet(Protocol):
    """A provider's part of the git credential helper, exposed as its facet's ``CREDENTIAL``.

    ``cred`` is the broker's JSON for the provider: ``connected`` and ``token``, plus the
    optional ``username``, ``hosts``, and ``expires_at`` that older servers omit.
    """

    def refresh_interval_s(self) -> int:
        """Return the seconds between CLI config refreshes; zero or less disables them."""
        ...

    def hosts(self, instances: Instances) -> frozenset[str]:
        """Return the lower-cased git hosts this facet can serve over https."""
        ...

    def api_hosts(self) -> frozenset[str]:
        """Return the lower-cased API hosts the credential is valid for.

        Unused today; a later egress credential proxy can use it.
        """
        ...

    def git_username(self, cred: dict[str, Any]) -> str:
        """Return the git ``username`` that goes with the broker's token."""
        ...

    def write_cli_config(self, cred: dict[str, Any], home: Path) -> bool:
        """Write the credential into the provider CLI's config under *home*.

        :returns: ``True`` when written; ``False`` when skipped or on a filesystem error.
        """
        ...

    def clear_cli_config(self, home: Path) -> None:
        """Remove what :meth:`write_cli_config` writes under *home*, keeping everything else.

        The setup and refresh flows never call it: they cannot tell an entry this helper
        wrote from one the user wrote.
        """
        ...


@dataclass(frozen=True)
class _Facet:
    """A loaded credential facet with its provider's id and default hosts."""

    provider_id: str
    default_hosts: frozenset[str]
    credential: CredentialFacet


def _in_sandbox() -> bool:
    """Whether we're running inside a managed sandbox (host image sets ``IS_SANDBOX=1``).

    The broker host integrations auto-materialize the owner's credentials into the host's git
    and CLI config. That's only ever wanted in a managed sandbox — a disposable, per-session
    filesystem. A local ``omnigent host`` shares the developer's real ``~/.gitconfig`` and
    ``~/.config/gh``, so every auto-apply must be a no-op there. ``IS_SANDBOX=1`` is baked into
    the managed host image and the k8s pod spec; it is absent on a local host.
    """
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _host_token() -> str:
    """Return the host's launch token from the environment, or ``""``."""
    return (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()


def _home() -> Path | None:
    """Return the user's home directory, or ``None`` when it cannot be determined."""
    try:
        return Path.home()
    except RuntimeError:
        return None


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


def _credential_url(server: str, host_id: str, provider_id: str) -> str:
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/{provider_id}"


def _fetch(server: str, host_id: str, host_token: str, provider_id: str) -> dict[str, Any] | None:
    """Fetch the broker's JSON for *provider_id*, or ``None`` if the result is inconclusive.

    Only the server's explicit no-provider 404 maps to ``connected: false``.
    A generic 404 from a proxy or missing route must not enable shared credentials.
    """
    try:
        resp = httpx.get(
            _credential_url(server, host_id, provider_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code not in (200, 404):
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    if resp.status_code == 404:
        if data.get("detail") != _UNKNOWN_PROVIDER_DETAIL:
            return None
        return {"connected": False}
    return data


def _confirmed_unlinked(data: Mapping[str, Any] | None) -> bool:
    """Whether a broker probe says for certain that the owner has not linked the provider.

    ``None`` is inconclusive, so callers keep the broker authoritative (fail closed).
    """
    return data is not None and not data.get("connected")


def _vendable(data: Mapping[str, Any] | None) -> bool:
    """Whether a broker probe carries a token for a connected owner."""
    return bool(data and data.get("connected") and data.get("token"))


def broker_hosts(cred: Mapping[str, Any]) -> frozenset[str] | None:
    """Return the lower-cased git hosts the broker lists in ``hosts``, or ``None``.

    Older servers omit ``hosts``; a value that is not a list counts as omitted.
    """
    listed = cred.get("hosts")
    if not isinstance(listed, list):
        return None
    return frozenset(entry.lower() for entry in listed if isinstance(entry, str))


def _allowed_hosts(
    data: Mapping[str, Any] | None, default_hosts: frozenset[str]
) -> frozenset[str]:
    """Return the hosts the broker vouches for: its ``hosts`` list, else the default hosts."""
    listed = None if data is None else broker_hosts(data)
    return default_hosts if listed is None else listed


def _git_config(*args: str) -> None:
    """Run ``git config --global`` best-effort (never raises; git may be absent)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "config", "--global", *args],
            check=False,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )


def _git_config_values(key: str) -> list[str]:
    """Return every ``--global`` value of *key*; none when it is unset or git cannot run."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--null", "--get-all", key],
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return result.stdout.split("\0")[:-1]


def _helper_key(host: str) -> str:
    return f"credential.https://{host}.helper"


def _helper_command(server_url: str, host_id: str, token: str, module: str = _MODULE) -> str:
    """Return the ``credential.helper`` value that runs *module* against the broker.

    The launch token is baked in so the helper works whether or not the process running git
    inherited the env; the forge token itself is fetched per operation and never stored.
    """
    return (
        f"!python3 -m {module} "
        f"--server {shlex.quote(server_url)} --host-id {shlex.quote(host_id)} "
        f"--host-token {shlex.quote(token)}"
    )


def _install_helper(host: str, command: str, git_config: _GitConfig | None = None) -> None:
    """Make *command* the only git credential helper for https requests to *host*.

    Resets the host's helper chain first: git accumulates credential helpers across scopes and
    runs them in parse order (system → global), stopping at the first that returns a full
    credential. A managed image installs a wider-scope helper that answers from a shared
    ``$GIT_TOKEN``; parsed before this --global entry it would vend first and silently bypass
    the per-user broker. An empty value clears the inherited chain, so only the broker remains.

    Idempotent by ``--replace-all``: the host re-runs this on every resume, and a plain set
    can't overwrite the two values an earlier run left, so broker entries would pile up.
    """
    run = _git_config if git_config is None else git_config
    key = _helper_key(host)
    run("--replace-all", key, "")
    run("--add", key, command)


def _clear_helper(host: str) -> None:
    """Remove this helper's entries for *host*, and the chain reset installed with them.

    Helpers the user configured stay as they are. The reset (an empty value) goes only when an
    entry of this helper is present, because :func:`_install_helper` wrote them together.
    """
    key = _helper_key(host)
    if not any(value.startswith(_OWN_HELPER_PREFIXES) for value in _git_config_values(key)):
        return
    _git_config("--unset-all", key, _OWN_HELPER_PATTERN)
    _git_config("--unset-all", key, "^$")


def _set_commit_identity(
    data: Mapping[str, Any] | None, git_config: _GitConfig | None = None
) -> bool:
    """Attribute commits to a connected owner whose id is an email address.

    :returns: Whether the identity was set.
    """
    owner = str((data or {}).get("owner") or "")
    if not data or not data.get("connected") or "@" not in owner:
        return False
    run = _git_config if git_config is None else git_config
    run("user.email", owner)
    login = data.get("login")
    run("user.name", str(login) if login else owner.split("@", 1)[0])
    return True


def _credential_facets() -> list[_Facet]:
    """Load the credential facet of every registered provider, in registration order.

    A facet that fails to load is logged and skipped, so one broken provider cannot stop the
    others.
    """
    loaded: list[_Facet] = []
    for descriptor in providers():
        try:
            credential = load_facet(descriptor.id, "credential")
            defaults = frozenset(host.lower() for host in descriptor.default_hosts)
        except Exception:  # noqa: BLE001 — a broken provider must not stop the others
            _logger.warning(
                "Git credential facet of provider %s failed to load; skipping it",
                descriptor.id,
                exc_info=True,
            )
            continue
        if credential is not None:
            loaded.append(_Facet(descriptor.id, defaults, credential))
    return loaded


def _facet_hosts(facet: _Facet, instances: Instances) -> frozenset[str]:
    """Return the facet's git hosts, lower-cased."""
    return frozenset(host.lower() for host in facet.credential.hosts(instances) if host)


def _served_hosts(
    facet: _Facet, hosts: frozenset[str], data: Mapping[str, Any] | None
) -> frozenset[str]:
    """Return the facet hosts that get this helper for a broker probe.

    A confirmed not-linked owner gets none. An inconclusive probe (``None``) vouches for the
    default hosts only, so the broker stays authoritative there (fail closed).
    """
    if _confirmed_unlinked(data):
        return frozenset()
    return hosts & _allowed_hosts(data, facet.default_hosts)


def _facet_for_host(host: str) -> _Facet | None:
    """Return the first credential facet whose hosts include *host*, or ``None``."""
    instances = EnvInstances()
    for facet in _credential_facets():
        try:
            if host in _facet_hosts(facet, instances):
                return facet
        except Exception:  # noqa: BLE001 — a broken facet must not stop the others
            _logger.warning(
                "Git credential facet of provider %s failed to list its hosts",
                facet.provider_id,
                exc_info=True,
            )
    return None


def _wire_git(
    server_url: str,
    host_id: str,
    token: str,
    facet: _Facet,
    instances: Instances,
    *,
    clear_others: bool,
) -> tuple[dict[str, Any] | None, frozenset[str]]:
    """Probe the broker for one provider and install this helper on the hosts it serves.

    :param clear_others: Also remove this helper's entries from the facet's other hosts.
    :returns: The probe (the broker JSON, or ``None`` when inconclusive) and the served hosts.
    """
    data = _fetch(server_url, host_id, token, facet.provider_id)
    hosts = _facet_hosts(facet, instances)
    served = _served_hosts(facet, hosts, data)
    command = _helper_command(server_url, host_id, token)
    for host in sorted(served):
        _install_helper(host, command)
    if clear_others:
        for host in sorted(hosts - served):
            _clear_helper(host)
    return data, served


def _write_cli_config(facet: _Facet, data: dict[str, Any] | None) -> bool:
    """Write a connected owner's token into the facet's CLI config under the home directory."""
    home = _home()
    if data is None or not _vendable(data) or home is None:
        return False
    return facet.credential.write_cli_config(data, home)


def configure_host_credentials(server_url: str, host_id: str) -> None:
    """Point git and each provider CLI in a managed sandbox at the per-user broker.

    Called by ``omnigent host`` at startup: executor-agnostic, since the host runs in every
    executor and holds ``$OMNIGENT_HOST_TOKEN``. One broker probe per credential facet decides:

    - The hosts the facet serves (see the module docstring's host rules) get this helper as
      their only https credential helper, which fetches the owner's token per git op.
    - The facet's other hosts lose this helper's entries, so a confirmed not-linked owner
      (a shared-``$GIT_TOKEN`` or local deployment) falls back to the ambient helper. Helpers
      the user configured stay unchanged.
    - A connected owner whose id is an email address becomes the commit author (the first such
      provider wins), and the facet writes its CLI config (gh's ``hosts.yml`` for GitHub).

    An inconclusive probe keeps the broker on the default hosts (fail closed, like the clone).
    Sandbox-only (see :func:`_in_sandbox`) and best-effort: never raises.
    """
    if not _in_sandbox():
        return
    token = _host_token()
    if not token:
        return
    instances = EnvInstances()
    identity_set = False
    for facet in _credential_facets():
        try:
            data, _served = _wire_git(
                server_url, host_id, token, facet, instances, clear_others=True
            )
            identity_set = identity_set or _set_commit_identity(data)
            _write_cli_config(facet, data)
        except Exception as exc:  # noqa: BLE001 — one provider's failure must not stop the others
            # No traceback: its frames hold the launch token and the broker reply, which a
            # formatter that prints frame locals would log.
            _logger.warning(
                "Git credential setup for provider %s failed: %s: %s",
                facet.provider_id,
                type(exc).__name__,
                exc,
            )


def configure_clone_credentials(server_url: str, host_id: str) -> bool:
    """Wire the per-user broker for the initial workspace clone.

    Called before a managed sandbox clones its workspace repos. Connected-gated like
    :func:`configure_host_credentials`: a provider whose owner is confirmed not linked keeps
    the ambient chain (notably the image's shared ``$GIT_TOKEN`` helper), so a shared-token
    clone still works for them. An inconclusive probe (timeout, 5xx, bad JSON) installs the
    broker on the default hosts anyway: treating it as not linked would silently clone a linked
    owner's private repo under the shared identity. Unlike host setup, this never removes a
    helper and is not sandbox-gated. A facet that raises propagates, so the clone fails visibly
    instead of falling back to the shared token.

    :returns: ``True`` when the broker was wired for at least one host; ``False`` when no
        provider's broker was wired (owner confirmed not linked, or no launch token).
    """
    token = _host_token()
    if not token:
        return False
    instances = EnvInstances()
    wired = False
    for facet in _credential_facets():
        _data, served = _wire_git(server_url, host_id, token, facet, instances, clear_others=False)
        wired = wired or bool(served)
    return wired


def _start_refresh_thread(
    interval: int, refresh: Callable[[], object], name: str
) -> threading.Thread | None:
    """Run *refresh* every *interval* seconds on a daemon thread, ignoring its errors.

    :returns: The started thread, or ``None`` when *interval* is not positive.
    """
    if interval <= 0:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            with contextlib.suppress(Exception):
                refresh()

    thread = threading.Thread(target=_loop, name=name, daemon=True)
    thread.start()
    return thread


def _refresh_cli_config(server_url: str, host_id: str, facet: _Facet) -> bool:
    """Re-fetch one provider's credential and re-write its CLI config (one refresher tick)."""
    if not _in_sandbox():
        return False
    token = _host_token()
    if not token:
        return False
    return _write_cli_config(facet, _fetch(server_url, host_id, token, facet.provider_id))


def start_credential_refresh(server_url: str, host_id: str) -> list[threading.Thread]:
    """Keep each provider CLI's token fresh over a long-lived host.

    git's helper re-fetches the server-refreshed token on every operation, but a CLI such as gh
    reads a static config, so the token :func:`configure_host_credentials` writes at startup
    goes stale when the forge token expires and the server rotates it. One best-effort daemon
    thread per credential facet re-writes the config every
    :meth:`CredentialFacet.refresh_interval_s` seconds, whether or not the startup write
    succeeded: each tick re-fetches, so a broker blip at startup cannot strand a connected owner.

    :returns: The started threads; none outside a managed sandbox, and none for a facet whose
        interval is not positive.
    """
    if not _in_sandbox():
        return []
    threads: list[threading.Thread] = []
    for facet in _credential_facets():
        try:
            interval = facet.credential.refresh_interval_s()
        except Exception:  # noqa: BLE001 — one provider's failure must not stop the others
            _logger.warning(
                "Git credential facet of provider %s has no refresh interval",
                facet.provider_id,
                exc_info=True,
            )
            continue
        thread = _start_refresh_thread(
            interval,
            functools.partial(_refresh_cli_config, server_url, host_id, facet),
            name=f"{facet.provider_id}-credential-refresh",
        )
        if thread is not None:
            threads.append(thread)
    return threads


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--host-token", required=True)
    # git passes the operation (get/store/erase) as the first positional arg.
    parser.add_argument("operation", nargs="?", default="get")
    args, _ = parser.parse_known_args(argv)
    return args


def _format_answer(username: str, token: str) -> str:
    """Return a credential in git's credential helper format."""
    return f"username={username}\npassword={token}\n"


def _lookup(args: argparse.Namespace) -> tuple[str, str] | None:
    """Return ``(username, token)`` for the git request on stdin, or ``None`` to decline."""
    request = _read_git_request()
    host = (request.get("host") or "").lower()
    if not host or request.get("protocol") != "https":
        return None
    facet = _facet_for_host(host)
    if facet is None:
        return None
    data = _fetch(args.server, args.host_id, args.host_token, facet.provider_id)
    if data is None or not _vendable(data):
        return None
    if host not in _allowed_hosts(data, facet.default_hosts):
        return None
    return facet.credential.git_username(data), str(data["token"])


def main(argv: list[str] | None = None) -> int:
    """Answer one git credential request from the broker.

    Only ``get`` returns a credential; ``store`` and ``erase`` are no-ops, because nothing is
    persisted. A request declines (returns 0 with no output, so git tries the next helper)
    unless it is https, names a host a credential facet serves, and the broker vends a token
    for that host under the module docstring's host rules. So the brokered token can never leak
    to another host, even if this is ever wired as a global credential helper.
    """
    args = _parse_args(argv)
    if args.operation != "get":
        return 0
    out = sys.stdout
    # git parses stdout, so anything a facet or a log handler prints goes to stderr instead.
    with contextlib.redirect_stdout(sys.stderr):
        cred = _lookup(args)
    if cred is not None:
        out.write(_format_answer(*cred))
    return 0
