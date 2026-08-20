"""Local health probes for ``omnigent diagnose``.

:mod:`omnigent.diagnostics` answers *what is installed* (CLI/server version,
OS, auth mode). This module answers *why is it not working* — the three local
signals that actually discriminate between the auth failure modes a user hits,
read straight from the artifacts the runtime already writes:

- **credential**: how many ``~/.databrickscfg`` profiles match the workspace
  host — a count, never a verdict (see :func:`_credential_health` for why a
  verdict would be wrong). It earns its place because the resolver's final error
  names the duplicate even when a duplicate is not the cause, so the count tells
  a reader whether that message is describing the config or the failure.
- **host log**: whether the host tunnel is stalled mid-connect (a server-side
  refusal, where restarting locally cannot help) and how often the server
  restarted the tunnel under it.
- **runner log**: whether the runner received a bearer from the host, whether
  it fell back to a self-refreshing credential after that bearer lapsed, and
  how often a refresh produced nothing. Together these say "recovered by
  itself" versus "stuck without a credential" — the distinction that decides
  whether any local action is warranted.

Invariant: **no secrets**, matching :mod:`omnigent.diagnostics`. Only counts,
booleans, byte sizes, and a fixed vocabulary of classification strings are
reported — never profile names, hosts, tokens, log lines, or config bodies.

Every probe degrades to ``None`` (section absent) or ``"unknown"`` rather than
raising: a snapshot for a bug report must survive a half-broken machine, which
is exactly when it is collected. Logs are read through two bounded windows
(:data:`_LOG_HEAD_BYTES`, :data:`_LOG_TAIL_BYTES`) rather than whole, because a
runner log reaches hundreds of megabytes when a transport failure loops.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

# A log is read from both ends, never whole: a runner log reaches hundreds of
# megabytes when a transport failure loops (#4885), and an unbounded read would
# defeat the point of a quick snapshot.
#
# The two windows answer different questions, and using only one gets the answer
# wrong. Recurring failures are counted from the TAIL, because a stuck process
# appends the same line forever and only recent history matters. One-shot
# startup facts ("was a bearer handed over at launch?") are looked for in the
# HEAD, because they are written once in the first few lines and scroll out of
# any tail window on a long-lived session — a tail-only read reports "no bearer"
# for a runner that was in fact handed one.
_LOG_TAIL_BYTES = 256 * 1024
_LOG_HEAD_BYTES = 16 * 1024

# Log markers. These are strings the runtime itself emits, not a third-party
# library's formatting, so they do not drift when a dependency changes its
# message layout. The one exception is noted on the field it feeds.
_MARKER_BEARER_HANDOFF = "using host-provided bearer"
_MARKER_BEARER_REJECTED = "host bootstrap bearer rejected"
_MARKER_REFRESH_EMPTY = "Databricks token refresh returned no token"
# Emitted by databricks-sdk, not by us: the clearest evidence that the runner
# reached a self-refreshing credential. Counted, never required.
_MARKER_SDK_AUTH = "Using Databricks CLI authentication"
_MARKER_TUNNEL_CONNECTING = "Connecting to wss"
_MARKER_SERVICE_RESTART = "received 1012"

CredentialProbe = Literal["ok", "unavailable", "not-applicable"]


def _log_windows(path: Path) -> tuple[str, str]:
    """Return ``(head, tail)`` text windows of *path*, guaranteed not to overlap.

    A log short enough to read whole comes back entirely as *head*, with *tail*
    empty. That keeps counting simple: a caller may count a marker across both
    windows without double-counting a short file, and may test presence in
    *head* alone for a fact written once at startup.

    :param path: File to read.
    :returns: ``(head, tail)`` decoded text (errors replaced). ``("", "")`` when
        the file is unreadable.
    """
    head_limit = _LOG_HEAD_BYTES
    tail_limit = _LOG_TAIL_BYTES
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size <= head_limit + tail_limit:
                return fh.read(head_limit + tail_limit).decode("utf-8", errors="replace"), ""
            head = fh.read(head_limit).decode("utf-8", errors="replace")
            fh.seek(size - tail_limit)
            tail = fh.read(tail_limit).decode("utf-8", errors="replace")
            return head, tail
    except OSError:
        return "", ""


def _log_facts(path: Path, head: str, tail: str) -> dict[str, Any]:
    """Return the size/window/idle facts every log section reports.

    ``window_bytes`` states how much of the file the counters in that section
    were computed from, so a count can never be mistaken for a lifetime total.
    ``idle_seconds`` is how long ago the file was last written, which is what
    separates "stuck" from "busy": a marker on the last line means something
    different at 0 seconds (in progress) than at 300 (it stopped there).

    :param path: The log file the windows came from.
    :param head: Head window text.
    :param tail: Tail window text.
    :returns: ``{"size_bytes", "window_bytes", "idle_seconds"}``.
    """
    import time

    try:
        stat = path.stat()
        size_bytes = stat.st_size
        idle_seconds = max(0, int(time.time() - stat.st_mtime))
    except OSError:
        size_bytes = 0
        idle_seconds = 0
    return {
        "size_bytes": size_bytes,
        "window_bytes": len(head.encode("utf-8", errors="replace"))
        + len(tail.encode("utf-8", errors="replace")),
        "idle_seconds": idle_seconds,
    }


def _newest_log(*destinations: str) -> Path | None:
    """Return the most recently modified ``*.log`` across log destinations.

    :param destinations: Destination directory names under the logs root, e.g.
        ``"runner"``, ``"host-runner"``.
    :returns: The newest log path, or ``None`` when none exists or the logs
        root is unreadable.
    """
    try:
        from omnigent.process_logging import logs_root

        root = logs_root()
    except Exception:  # noqa: BLE001 — a snapshot must not fail on import/env
        return None
    newest: Path | None = None
    newest_mtime = -1.0
    for destination in destinations:
        directory = root / destination
        try:
            candidates = list(directory.glob("*.log"))
        except OSError:
            continue
        for candidate in candidates:
            try:
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = candidate, mtime
    return newest


def _is_databricks_host(server_url: str | None) -> bool:
    """Whether *server_url* looks like a Databricks-fronted deployment.

    The credential probe is Databricks-specific, so it is skipped (reported as
    ``not-applicable``) for OSS/self-hosted servers rather than guessing.

    Covers all three clouds: ``*.cloud.databricks.com`` (AWS, and GCP via
    ``*.gcp.databricks.com``), ``*.azuredatabricks.net`` (Azure), and
    ``*.databricksapps.com`` (a Databricks App fronting the server).

    :param server_url: Server URL, or ``None``.
    :returns: ``True`` when the host looks like a Databricks workspace.
    """
    if not server_url:
        return False
    return any(
        suffix in server_url
        for suffix in (".databricks.com", ".azuredatabricks.net", ".databricksapps.com")
    )


def _workspace_host(server_url: str) -> str:
    """Return the ``scheme://host`` prefix of *server_url*.

    The Omnigent server sits on a path (``/api/2.0/omnigent``) under the
    workspace host, but credentials are keyed by the workspace host alone.

    :param server_url: Server URL.
    :returns: ``scheme://host`` with any path, query, or fragment removed.
    """
    match = re.match(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)?(?P<rest>[^/?#]*)", server_url)
    assert match is not None  # the pattern always matches
    return f"{match.group('scheme') or ''}{match.group('rest')}"


def _profiles_for_host(host: str) -> list[str]:
    """Return the ``~/.databrickscfg`` profile names whose host is *host*.

    Delegates to the runtime's own matcher so the count reflects what the
    credential chain actually sees. A separate function (rather than an inline
    import) keeps it substitutable in tests, the same way
    :func:`omnigent.diagnostics._fetch_server_info` is.

    :param host: Workspace host, e.g. ``"https://example.cloud.databricks.com"``.
    :returns: Matching profile names, in config-file order.
    :raises Exception: When the matcher is unavailable (no ``databricks``
        extra) or the config cannot be read; the caller classifies that.
    """
    from omnigent.inner.databricks_executor import _databrickscfg_profiles_for_host

    return _databrickscfg_profiles_for_host(host)


def _credential_health(server_url: str | None) -> dict[str, Any]:
    """Count the ``~/.databrickscfg`` profiles matching *server_url*'s workspace.

    Reports **only the count**, deliberately. It is tempting to turn the count
    into a verdict, but every such verdict would be wrong, because
    ``_resolve_databricks_auth_for_host`` tries each matching profile in turn and
    falls back to a host-keyed lookup when none authenticates:

    - two or more matches is **not** a failure — the resolver authenticates the
      candidates one by one and takes the first that works;
    - exactly one match is **not** success — that profile's credential may be
      expired;
    - zero matches is **not** "unauthenticated" — the host-keyed CLI lookup is
      still tried.

    No authentication is attempted here either: that would shell out to the
    Databricks CLI once per candidate and can block for seconds.

    The count earns its place for one specific reason. When every matching
    profile fails to authenticate, the resolver's final error is
    ``"<a> and <b> match <host> in ~/.databrickscfg. Use --profile"``, which
    names the duplicate even though the duplicate is not the cause. Seeing
    "2 profiles match" next to that error tells a reader the message is
    describing the config, not the failure.

    :param server_url: Server URL, or ``None``.
    :returns: ``{"backend", "profiles_matching_host", "probe"}``. ``probe`` is
        ``"ok"`` when the matcher ran, ``"unavailable"`` when it could not (no
        ``databricks`` extra, unreadable config), and ``"not-applicable"`` for a
        non-Databricks deployment — it describes **this probe**, never the
        credential.
    """
    if not _is_databricks_host(server_url):
        return {
            "backend": "other" if server_url else "unknown",
            "profiles_matching_host": None,
            "probe": "not-applicable",
        }
    assert server_url is not None  # guarded by _is_databricks_host
    try:
        matches = _profiles_for_host(_workspace_host(server_url))
    except Exception:  # noqa: BLE001 — no databricks extra, unreadable cfg, …
        return {
            "backend": "databricks",
            "profiles_matching_host": None,
            "probe": "unavailable",
        }
    return {
        "backend": "databricks",
        "profiles_matching_host": len(matches),
        "probe": "ok",
    }


def _host_log_health() -> dict[str, Any] | None:
    """Summarize the newest host log.

    ``stalled_on_connect`` says only that the connect attempt is the last thing
    in the log — it is not a claim that the tunnel is down. Read it together with
    ``idle_seconds``: the connect line is written *before* the attempt, so at a
    low idle time it just means a connect is in flight, while at a high one it is
    the fingerprint of a server-side refusal, where restarting locally cannot
    help. Its negation says nothing either — a log can end on any line.

    ``service_restarts`` counts tunnel drops the server initiated (WebSocket close
    1012), which are normal and self-healing.

    :returns: ``{"size_bytes", "window_bytes", "idle_seconds",
        "stalled_on_connect", "service_restarts"}``, or ``None`` when no host log
        exists.
    """
    path = _newest_log("host")
    if path is None:
        return None
    head, tail = _log_windows(path)
    last_lines = [line for line in (tail or head).splitlines() if line.strip()]
    stalled = bool(last_lines) and _MARKER_TUNNEL_CONNECTING in last_lines[-1]
    return {
        **_log_facts(path, head, tail),
        "stalled_on_connect": stalled,
        "service_restarts": head.count(_MARKER_SERVICE_RESTART)
        + tail.count(_MARKER_SERVICE_RESTART),
    }


def _runner_log_health() -> dict[str, Any] | None:
    """Summarize the newest runner log.

    Read the three counters as a **pair plus a fallback**, not individually:

    ============================  ==================  ==========================
    ``refresh_empty``             ``sdk_fallback``    reading
    ============================  ==================  ==========================
    ``0``                         ``0``               nothing went wrong recently
    ``> 0``                       ``> 0``             lapsed, then recovered
    ``> 0``                       ``0``               stuck without a credential
    ============================  ==================  ==========================

    ``bearer_handoff`` is separate: it says whether the host handed a bearer over
    at launch. Its absence points at the host rather than the credential.

    The counters describe the **recent window** (the tail), deliberately, not the
    session's lifetime. A runner that lapsed and recovered hours ago, then went
    stuck, must not read as healthy because of that old recovery; and one that is
    quiet now reports zeros either way. ``bearer_handoff`` is looked for in the
    head instead, because it is logged once at launch and scrolls out of any tail
    window on a long-lived session.

    The pair stays reliable across the window boundary because a lapse and the
    fallback that answers it are seconds apart, and therefore adjacent in the
    file: either both land in the window or neither does. A recovery seen without
    the lapse that caused it would mean the boundary fell between them, which the
    two markers being adjacent rules out in practice.

    :returns: ``{"size_bytes", "window_bytes", "idle_seconds",
        "bearer_handoff", "bearer_rejected", "sdk_fallback", "refresh_empty"}``,
        or ``None`` when no runner log exists.
    """
    path = _newest_log("runner", "host-runner")
    if path is None:
        return None
    head, tail = _log_windows(path)
    # A file short enough to read whole comes back entirely as ``head``; then the
    # whole file *is* the recent window.
    recent = tail or head
    return {
        **_log_facts(path, head, tail),
        "bearer_handoff": _MARKER_BEARER_HANDOFF in head,
        "bearer_rejected": recent.count(_MARKER_BEARER_REJECTED),
        "sdk_fallback": recent.count(_MARKER_SDK_AUTH),
        "refresh_empty": recent.count(_MARKER_REFRESH_EMPTY),
    }


def collect_health(*, server_url: str | None = None) -> dict[str, Any]:
    """Collect the local health section of the diagnostics snapshot.

    :param server_url: Server URL used to pick the workspace host for the
        credential probe. When ``None`` the credential section reports
        ``not-applicable``.
    :returns: A JSON-serializable dict::

            {
              "credential": {
                "backend": "databricks" | "other" | "unknown",
                "profiles_matching_host": 1 | null,
                "probe": "ok" | "unavailable" | "not-applicable"
              } | null,
              "host_log":   {...} | null,
              "runner_log": {...} | null
            }

        Contains no secrets.
    """
    return {
        "credential": _safe(lambda: _credential_health(server_url)),
        "host_log": _safe(_host_log_health),
        "runner_log": _safe(_runner_log_health),
    }


def _safe(probe: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """Run *probe*, turning any failure into an absent section.

    The "never raises" guarantee is structural rather than a promise each probe
    has to keep on its own: a snapshot is collected precisely when the machine
    is misbehaving, so a probe that trips must cost a ``null`` section and
    nothing more.

    :param probe: Zero-argument probe returning a section mapping or ``None``.
    :returns: The probe's result, or ``None`` when it raised.
    """
    try:
        return probe()
    except Exception:  # noqa: BLE001 — a broken probe must not break the snapshot
        return None


__all__ = ["collect_health"]
