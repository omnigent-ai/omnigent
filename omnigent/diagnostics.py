"""Read-only environment snapshot for ``omnigent diagnose``.

A deliberately small, secret-free snapshot for bug reports: the version of the
CLI and (when a server is reachable) the server, plus the OS/runtime and the
server's auth mode. Reporting both versions makes skew visible — the same reason
the session-info popover shows ``server · host``.

:func:`collect_snapshot` returns a JSON-serializable dict. The CLI version and
OS are always local. The **server version and auth mode are read from the server
itself** via the unauthed ``GET /v1/info`` endpoint when a server URL is
provided (``omnigent diagnose --server``), so they reflect the *actual* remote
server rather than local guesses. When no server is reachable, ``auth_source``
falls back to what this machine's environment *would* boot a server in, marked
by ``auth_source_origin`` so the two are never confused.

Invariant: **no secrets.** Only versions, OS, and the coarse auth mode
(accounts | single_user | oidc | header) are reported — never keys, tokens, or
config bodies. ``auth_source`` doubles as the OSS-vs-managed signal (there is no
explicit install marker).
"""

from __future__ import annotations

import platform
import re
from typing import Any


def _redact_url(url: str | None) -> str | None:
    """Strip userinfo and query/fragment from a URL so it's safe to print.

    A ``--server`` value could carry embedded basic-auth (``https://user:pass@h``)
    or a query token; the snapshot is meant to be pasted into a bug report, so
    keep only scheme + host + port + path.

    Operates on the raw string with no ``urlsplit`` — that would (a) raise
    ``ValueError`` on a malformed IPv6 URL, leaving a fallback that leaks the
    credentials, and (b) misparse scheme-less ``user:pass@host`` inputs. String
    surgery instead scrubs uniformly regardless of shape:

    1. cut everything from the first ``?`` or ``#`` (query + fragment);
    2. drop a ``user:pass@`` prefix from the authority (the part before the
       first ``/`` after any scheme), preserving IPv6 ``[...]`` brackets and
       host casing.
    """
    if not url:
        return url

    # 1. Drop query + fragment (works on well-formed and scheme-less inputs).
    without_qf = re.split(r"[?#]", url, maxsplit=1)[0]

    # 2. Split an optional ``scheme://`` prefix, then strip userinfo from the
    #    authority (up to the next ``/``). Also handles a scheme-less authority.
    m = re.match(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)?(?P<rest>.*)", without_qf, re.DOTALL)
    assert m is not None  # the pattern always matches
    scheme = m.group("scheme") or ""
    rest = m.group("rest")

    authority, sep, path = rest.partition("/")
    authority = authority.rsplit("@", 1)[-1]  # drop ``user:pass@`` if present
    return f"{scheme}{authority}{sep}{path}"


def _local_version() -> str:
    """The installed CLI/package version (same constant the runtime imports)."""
    from omnigent.version import VERSION

    return VERSION


def _local_auth_source() -> str:
    """The auth mode this machine's env would boot a server in (no network)."""
    try:
        from omnigent.server.auth import resolve_auth_source

        return resolve_auth_source()
    except Exception:  # noqa: BLE001
        return "unknown"


def _fetch_server_info(server_url: str, *, timeout: float) -> dict[str, Any] | None:
    """Fetch the server's ``/v1/info`` (unauthed). ``None`` when unreachable.

    A snapshot for a bug report must degrade, never hang or raise, so any
    transport/HTTP failure returns ``None``.
    """
    try:
        import httpx

        from omnigent.chat import _remote_headers

        base = server_url.rstrip("/")
        resp = httpx.get(
            f"{base}/v1/info",
            headers=_remote_headers(server_url=base, host_id=None),
            timeout=timeout,
            trust_env=False,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — an unreachable/misbehaving server is not an error here
        return None
    return None


def _auth_source_from_info(info: dict[str, Any]) -> str:
    """Derive the coarse auth mode from ``/v1/info`` fields.

    ``/v1/info`` exposes booleans/urls rather than a raw source string, so map
    them back to a coarse vocabulary:

    - ``accounts_enabled`` → ``"accounts"`` (built-in username/password login).
    - otherwise ``single_user`` → ``"single_user"`` (a local one-user runtime).
      This branch comes before ``header`` because a local single-user server and
      a multi-user header-auth deploy BOTH report ``accounts_enabled=false`` /
      ``login_url=null``; ``single_user`` is the only field that tells them apart
      (see ``/v1/info`` in ``omnigent/server/app.py``).
    - otherwise a ``login_url`` is present → ``"oidc"`` (an external IdP owns the
      login redirect).
    - otherwise → ``"header"`` (a trusted upstream proxy injects identity — the
      managed-deployment posture, and the OSS-vs-managed signal).
    """
    if info.get("accounts_enabled"):
        return "accounts"
    if info.get("single_user"):
        return "single_user"
    if info.get("login_url"):
        return "oidc"
    return "header"


def collect_snapshot(*, server_url: str | None = None, timeout: float = 5.0) -> dict[str, Any]:
    """Collect the read-only diagnostics snapshot.

    :param server_url: When given and reachable, the server version and auth
        mode are read from its ``/v1/info`` endpoint. When ``None`` (or
        unreachable), ``server_version`` is ``None`` and ``auth_source`` falls
        back to the local environment (see ``auth_source_origin``).
    :param timeout: Per-request timeout (seconds) for the server probe.
    :returns: A JSON-serializable dict::

            {
              "cli_version": "0.8.0.dev0",
              "server_version": "0.8.0.dev0" | null,
              "server_url": "http://..." | null,
              "auth_source": "accounts" | "single_user" | "oidc" | "header" | "unknown",
              "auth_source_origin": "server" | "local-env",
              "os": "macOS-...",
              "python": "3.12.13"
            }

        Contains no secrets.
    """
    server_version: str | None = None
    auth_source = _local_auth_source()
    auth_source_origin = "local-env"

    if server_url:
        info = _fetch_server_info(server_url, timeout=timeout)
        if info is not None:
            version = info.get("server_version")
            server_version = str(version) if version else None
            auth_source = _auth_source_from_info(info)
            auth_source_origin = "server"

    return {
        "cli_version": _local_version(),
        "server_version": server_version,
        "server_url": _redact_url(server_url),
        "auth_source": auth_source,
        "auth_source_origin": auth_source_origin,
        "os": platform.platform(),
        "python": platform.python_version(),
    }


__all__ = ["collect_snapshot"]
