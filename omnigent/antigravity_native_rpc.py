"""connect-RPC client for a running native Antigravity (agy) process.

This is a **discovery / control helper** for
:mod:`omnigent.antigravity_native_forwarder` (the read path that tails agy's
transcript). It does NOT deliver turns: web/mobile turns are typed into the agy
TUI over tmux (see :func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`
and the executor), because a turn delivered over agy's ``SendAgentMessage`` RPC
is recorded as a ``SYSTEM_MESSAGE`` ("not actually sent by the user"), not a
``USER_INPUT`` — so the forwarder (which mirrors user turns from ``USER_INPUT``)
would never commit the user's message. This module instead provides the two
read-side capabilities the forwarder needs over agy's connect-RPC surface:
**conversation-ownership discovery** and a (currently wired-off) **turn
interrupt**.

How agy exposes a control surface (verified end-to-end; see
``docs/claude/antigravity-sidecar-spike.md``):

* A running ``agy`` process opens **two** ephemeral ``127.0.0.1`` TCP LISTEN
  ports. The **lower** one is a TLS HTTP/2 **connect-RPC** server hosting
  ``exa.language_server_pb.LanguageServerService``; the higher one is a plain
  HTTP surface that 404s. The TLS cert is self-signed, so the client uses
  ``verify=False``.
* The ports are ephemeral and not configurable
  (``ANTIGRAVITY_SIDECAR_WEB_PORT`` is a sidecar-plugin no-op), so they are
  discovered from the loopback socket table — ``lsof`` per agy pid, falling back
  to ``/proc/net/tcp`` on hosts where ``lsof`` cannot attribute the socket.
* Ownership probe: ``POST .../GetConversationMetadata`` with REQUEST body
  ``{"conversationId": "<id>"}`` returns HTTP 200 whose RESPONSE echoes that id at
  ``metadata.rootConversationId`` for a hosted conversation, and HTTP 500
  ("trajectory not found") for an unknown one — so the forwarder can confirm which
  live agy owns a brain dir before binding it. (Request and response shapes
  differ: the id is sent flat as ``conversationId`` and echoed nested under
  ``metadata``.)

Port discovery is **port-first**: it enumerates candidate loopback connect-RPC
ports (see :func:`_candidate_agy_rpc_ports`) and, for each, checks whether its
server reports the target ``conversation_id`` via ``GetConversationMetadata`` —
so the right port is found even when several agy instances run. There is no pid
key: agy is launched under ``tmux_start_on_attach`` (CLI) and does not exist at
launch, and on some hosts agy's listening socket is owned by a backend that is
neither the agy pid nor ``lsof``-attributable. The conversation-ownership check
is what makes a discovered port trustworthy, rejecting a recycled/foreign port
that hosts a different agy.

Everything that touches the OS (``lsof``, process enumeration) or the network
(httpx) is funnelled through small module-level seams so the unit tests can mock
them without real subprocesses or sockets.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import subprocess
from collections.abc import Iterable
from urllib.parse import urlparse

import httpx

_logger = logging.getLogger(__name__)

# connect-RPC service + methods on agy's TLS port.
_LS_SERVICE = "exa.language_server_pb.LanguageServerService"
_METHOD_HEARTBEAT = "Heartbeat"
_METHOD_GET_CONVERSATION_METADATA = "GetConversationMetadata"
# Turn-cancel methods. The NAMES are verified present in the agy 1.0.8 binary
# (``strings`` → ``LanguageServerService/{CancelCascadeInvocation,
# CancelCascadeSteps,ForceStopCascadeTree}``), but their request CONTRACTS are
# NOT verified: the proto field tags show ``CancelCascadeInvocationRequest`` and
# friends key on an internal ``cascade_id`` / ``invocation_id`` (agy's per-turn
# identifiers) that the transcript forwarder does NOT have — it only knows the
# *conversation* id, not the live cascade/invocation id — and the stop semantics
# are unconfirmed on a live process. The best-effort interrupt
# (:func:`interrupt_turn`) is therefore wired OFF by default; see its TODO.
_METHOD_FORCE_STOP_CASCADE_TREE = "ForceStopCascadeTree"
_METHOD_GET_CASCADE_TRAJECTORY_STEPS = "GetCascadeTrajectorySteps"
_METHOD_CANCEL_CASCADE_STEPS = "CancelCascadeSteps"
_METHOD_HANDLE_CASCADE_USER_INTERACTION = "HandleCascadeUserInteraction"

_LOOPBACK = "127.0.0.1"

# Hostnames that are unconditionally loopback. Any other host is checked
# numerically via :func:`ipaddress.ip_address` in :func:`_assert_loopback_url`.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Timeout for the liveness/validation probes used during discovery (Heartbeat +
# GetConversationMetadata), kept tight so scanning several candidate ports stays
# fast.
_PROBE_TIMEOUT_S = 2.0

# ``lsof`` flags: ``-a`` ANDs the filters (without it ``-p`` and ``-i`` are ORed
# and the pid filter is ignored), ``-nP`` skips name/port resolution (fast),
# ``-p`` restricts to the pid, ``-iTCP -sTCP:LISTEN`` selects TCP listeners.
_LSOF_TIMEOUT_S = 5.0

# Root of the Linux proc filesystem, scanned by ``_list_agy_pids_from_proc`` when
# ``pgrep`` is unavailable, and by ``_list_loopback_listen_ports`` for the socket
# table. A module constant so tests can repoint it at a fake tree without
# monkeypatching ``os``.
_PROC_FS = "/proc"

# Upper bound on how many loopback listeners ``_candidate_agy_rpc_ports`` will
# Heartbeat-probe in the ``/proc/net/tcp`` fallback, so a host with an unusual
# number of loopback services cannot make turn injection block for
# ``N * _PROBE_TIMEOUT_S``. Far above any real host's loopback listener count
# (the agy host pods have ~3); a breach is logged, never silent.
_MAX_FALLBACK_PROBE_PORTS = 32

# Cap on how many conversation ids are echoed into the ambiguity-refusal warning
# in ``conversation_id_owned_by_pid`` so a pathological candidate set cannot blow
# up a log line.
_MAX_LOGGED_AMBIGUOUS_IDS = 10


def _assert_loopback_url(url: str) -> None:
    """
    Refuse any connect-RPC URL whose host is not loopback.

    The connect-RPC clients disable TLS verification (agy's cert is self-signed),
    which is only safe because the endpoint is loopback-only. The port is
    discovered dynamically per session, so this guards every request against a
    URL that ever resolved to a non-loopback host — there, ``verify=False`` would
    silently trust any cert.

    :param url: Full request URL, e.g. ``"https://127.0.0.1:52548/svc/Method"``.
    :returns: None.
    :raises ValueError: When the URL's host is not a loopback address.
    """
    host = urlparse(url).hostname or ""
    if host in _LOOPBACK_HOSTS:
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError(f"refusing non-loopback connect-RPC URL (verify is disabled): {url!r}")


def _rpc_url(port: int, method: str) -> str:
    """
    Build the connect-RPC URL for a LanguageServerService method.

    :param port: agy connect-RPC (TLS) port, e.g. ``52548``.
    :param method: RPC method name, e.g. ``"SendAgentMessage"``.
    :returns: Full ``https://127.0.0.1:<port>/<service>/<method>`` URL.
    """
    return f"https://{_LOOPBACK}:{port}/{_LS_SERVICE}/{method}"


# httpx transport seam. ``None`` (production) lets httpx use its real loopback
# TLS transport with cert verification disabled (agy's cert is self-signed and
# the endpoint is loopback-only). Tests set this to an ``httpx.MockTransport``
# to assert the URL / headers / body of each RPC without a real socket.
#
# The sync seam backs the live discovery probes (Heartbeat +
# GetConversationMetadata). The async seam is reserved for :func:`interrupt_turn`'s
# future POST — that function is async (the forwarder ``await``s it) but is wired
# OFF pending request-contract verification, so ``_async_client`` has no live
# caller yet; the async seam is exercised by its guard test, which asserts no
# async RPC fires while the interrupt is off.
_HTTP_TRANSPORT: httpx.BaseTransport | None = None
_ASYNC_HTTP_TRANSPORT: httpx.AsyncBaseTransport | None = None


def _sync_client(timeout: float) -> httpx.Client:
    """
    Build a sync httpx client for a connect-RPC probe.

    Cert verification is disabled because agy's loopback cert is self-signed;
    this is safe only because every request URL is checked by
    :func:`_assert_loopback_url` before it is sent, so the client never trusts an
    unverified cert from a non-loopback host.

    :param timeout: Per-request timeout in seconds.
    :returns: An ``httpx.Client`` with cert verification disabled (loopback,
        self-signed) and the test transport when one is installed.
    """
    return httpx.Client(verify=False, timeout=timeout, transport=_HTTP_TRANSPORT)


def _async_client(timeout: float) -> httpx.AsyncClient:
    """
    Build an async httpx client for a connect-RPC call.

    Reserved for :func:`interrupt_turn`'s future POST (see the transport-seam
    note above); it has no live caller while the interrupt is wired off. Cert
    verification is disabled because agy's loopback cert is self-signed; this is
    safe only because every request URL is checked by :func:`_assert_loopback_url`
    before it is sent, so the client never trusts an unverified cert from a
    non-loopback host.

    :param timeout: Per-request timeout in seconds.
    :returns: An ``httpx.AsyncClient`` with cert verification disabled
        (loopback, self-signed) and the test transport when one is installed.
    """
    return httpx.AsyncClient(verify=False, timeout=timeout, transport=_ASYNC_HTTP_TRANSPORT)


def _run_lsof_listen_ports(pid: int) -> str:
    """
    Return raw ``lsof`` output listing a pid's TCP LISTEN sockets.

    Isolated as a seam so tests can stub the subprocess. A non-zero exit (e.g.
    the process is gone) or a missing ``lsof`` yields ``""`` rather than raising
    — discovery treats "no ports" the same as "lsof unavailable".

    :param pid: agy process id, e.g. ``72753``.
    :returns: ``lsof`` stdout, or ``""`` on any failure.
    """
    try:
        completed = subprocess.run(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        _logger.warning("lsof failed for agy pid=%s", pid, exc_info=True)
        return ""
    return completed.stdout


def _parse_loopback_listen_ports(lsof_output: str) -> list[int]:
    """
    Parse ascending unique ``127.0.0.1`` LISTEN ports from ``lsof`` output.

    Only IPv4 loopback (``127.0.0.1:<port>``) listeners are considered — agy
    binds its connect-RPC + HTTP ports there. The NAME column looks like
    ``127.0.0.1:52548 (LISTEN)``.

    :param lsof_output: Raw ``lsof -iTCP -sTCP:LISTEN`` stdout.
    :returns: Sorted, de-duplicated loopback port numbers (lowest first).
    """
    ports: set[int] = set()
    for line in lsof_output.splitlines():
        for token in line.split():
            prefix = f"{_LOOPBACK}:"
            if not token.startswith(prefix):
                continue
            port_text = token[len(prefix) :]
            if port_text.isdigit():
                ports.add(int(port_text))
    return sorted(ports)


# /proc/net/tcp local-address column is the little-endian hex of the bound IPv4
# address. 127.0.0.1 -> bytes [7F,00,00,01] -> u32 0x0100007F -> "0100007F".
# Matched case-insensitively against this exact form (agy binds the loopback host
# on IPv4, never a 127.0.0.0/8 alias).
_LOOPBACK_HEX_V4 = "0100007F"


def _is_loopback_hex_addr(addr_hex: str) -> bool:
    """
    Return whether a ``/proc/net/tcp`` local-address hex column is IPv4 loopback.

    :param addr_hex: The hex local address, e.g. ``"0100007F"`` (127.0.0.1).
    :returns: ``True`` for the IPv4 ``127.0.0.1`` encoding only.
    """
    return addr_hex.upper() == _LOOPBACK_HEX_V4


def _list_loopback_listen_ports() -> list[int]:
    """
    Return all IPv4 ``127.0.0.1`` TCP LISTEN ports from ``/proc/net/tcp``.

    The robust fallback for :func:`_candidate_agy_rpc_ports` on hosts where
    ``lsof -p <pid>`` cannot attribute a listening socket to its owning pid —
    e.g. the uid-1000 k8s pods where agy 1.0.10 holds its connect-RPC listener
    in a backend the agy process does not own as a file descriptor, so neither
    ``lsof`` nor a ``/proc/<pid>/fd`` scan finds it. The kernel's
    network-namespace socket table (``/proc/net/tcp``) lists every listener
    regardless of fd ownership and needs no ptrace/fd permission.

    IPv4 only: agy binds ``127.0.0.1`` and the connect-RPC client
    (:func:`_rpc_url`) dials ``127.0.0.1``, so an IPv6 ``::1`` listener would be
    unreachable regardless — enumerating it would only add a dead probe. Parses
    state ``0A`` (``TCP_LISTEN``). A missing table (non-Linux) yields ``[]``.

    :returns: Sorted, de-duplicated loopback LISTEN port numbers.
    """
    ports: set[int] = set()
    try:
        with open(os.path.join(_PROC_FS, "net", "tcp"), "rb") as handle:
            raw = handle.read().decode("ascii", "replace")
    except OSError:
        return []  # non-Linux, or no /proc
    for line in raw.splitlines()[1:]:  # row 0 is the column header
        fields = line.split()
        if len(fields) < 4 or fields[3] != "0A":  # 0A == TCP_LISTEN
            continue
        addr_hex, _sep, port_hex = fields[1].partition(":")
        if not _is_loopback_hex_addr(addr_hex):
            continue
        try:
            ports.add(int(port_hex, 16))
        except ValueError:
            continue
    return sorted(ports)


def _heartbeat_ok(port: int) -> bool:
    """
    Return whether a port answers the connect-RPC ``Heartbeat`` with HTTP 200.

    This is the canonical "is this the TLS connect-RPC port" probe: agy's lower
    port returns 200 for ``Heartbeat {}``; the higher plain-HTTP port does not
    (it 404s, or the TLS handshake fails). Any transport/TLS error counts as
    "not it".

    :param port: Candidate loopback port, e.g. ``52548``.
    :returns: ``True`` only when ``Heartbeat`` returns HTTP 200.
    """
    url = _rpc_url(port, _METHOD_HEARTBEAT)
    _assert_loopback_url(url)
    try:
        with _sync_client(_PROBE_TIMEOUT_S) as client:
            response = client.post(
                url,
                headers={"Content-Type": "application/json"},
                content=b"{}",
            )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def _conversation_matches(port: int, conversation_id: str) -> bool:
    """
    Return whether the agy on ``port`` owns ``conversation_id``.

    Used to pick the right port when several agy instances are running (or no
    pid was captured). For an id it hosts, agy's ``GetConversationMetadata``
    returns HTTP 200 with a ``metadata`` object that **echoes the resolved id**
    as ``metadata.rootConversationId`` (verified live); for an id it does not
    host it returns HTTP 500 (``"trajectory not found"``).

    The id echo — not merely a 200 with *some* metadata — is what makes the
    port-first candidate set (which, in the ``/proc/net/tcp`` fallback, spans
    every loopback listener, not just agy's) safe to write to: a non-agy service
    that happened to answer ``Heartbeat`` 200, or an agy hosting a *different*
    conversation, cannot echo this exact id and is rejected before any
    ``SendAgentMessage``. Fails closed (returns ``False``) on any shape it does
    not recognize.

    :param port: Validated connect-RPC port, e.g. ``52548``.
    :param conversation_id: agy conversation id to look for, e.g.
        ``"90468e33-..."``.
    :returns: ``True`` only when the server confirms it hosts ``conversation_id``
        (200 + ``metadata.rootConversationId == conversation_id``).
    """
    url = _rpc_url(port, _METHOD_GET_CONVERSATION_METADATA)
    _assert_loopback_url(url)
    try:
        with _sync_client(_PROBE_TIMEOUT_S) as client:
            response = client.post(
                url,
                headers={"Content-Type": "application/json"},
                content=json.dumps({"conversationId": conversation_id}).encode("utf-8"),
            )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        body = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    metadata = body.get("metadata")
    if not isinstance(metadata, dict):
        return False
    return metadata.get("rootConversationId") == conversation_id


def get_trajectory_steps(port: int, cascade_id: str) -> list[dict[str, object]]:
    """
    Return the trajectory steps for ``cascade_id`` from the agy on ``port``.

    POSTs ``{"cascadeId": cascade_id}`` to ``GetCascadeTrajectorySteps`` and
    returns the ``steps`` list from the response body (empty list when the key
    is absent). Used by the read driver to poll incremental step progress.

    :param port: Validated connect-RPC port, e.g. ``52548``.
    :param cascade_id: agy cascade id (equal to the conversation id) to query.
    :returns: List of step dicts, each containing at least ``stepIndex`` and
        ``status`` (may be empty when no steps have been recorded yet).
    :raises httpx.HTTPError: On transport errors or non-2xx responses; the
        Task 6 read driver is responsible for retry/backoff. Intentionally NOT
        fail-open (unlike ``_conversation_matches`` which is a discovery probe);
        a non-2xx here is a hard read failure, not a "not found" signal.
    :raises ValueError: When the 2xx response body is not valid JSON (body is
        decoded; the Task 6 driver catches broadly).
    """
    url = _rpc_url(port, _METHOD_GET_CASCADE_TRAJECTORY_STEPS)
    _assert_loopback_url(url)
    with _sync_client(_PROBE_TIMEOUT_S) as client:
        response = client.post(
            url,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"cascadeId": cascade_id}).encode("utf-8"),
        )
    # Raises httpx.HTTPStatusError (subclass of httpx.HTTPError) on non-2xx so
    # the body is never decoded on error paths (which may not be JSON).
    response.raise_for_status()
    body = response.json()  # ValueError on non-JSON 200 propagates (documented)
    steps = body.get("steps") if isinstance(body, dict) else None
    return list(steps) if isinstance(steps, list) else []


def cancel_cascade_steps(port: int, cascade_id: str) -> bool:
    """
    Request cancellation of the active cascade steps for ``cascade_id``.

    POSTs ``{"cascadeId": cascade_id}`` to ``CancelCascadeSteps`` and returns
    ``True`` when the server responds with a non-error HTTP status (< 400).
    Fails open (returns ``False``) on any transport error so the executor can
    treat it as best-effort.

    :param port: Validated connect-RPC port, e.g. ``52548``.
    :param cascade_id: agy cascade id (equal to the conversation id) to cancel.
    :returns: ``True`` when the server accepted the cancel (HTTP < 400),
        ``False`` on any error or rejection.
    """
    url = _rpc_url(port, _METHOD_CANCEL_CASCADE_STEPS)
    _assert_loopback_url(url)
    try:
        with _sync_client(_PROBE_TIMEOUT_S) as client:
            response = client.post(
                url,
                headers={"Content-Type": "application/json"},
                content=json.dumps({"cascadeId": cascade_id}).encode("utf-8"),
            )
    except Exception:  # deliberate fail-open: ssl.SSLError etc. outside httpx hierarchy
        return False
    return response.status_code < 400


class AntigravityRpcError(Exception):
    """
    Raised by :func:`handle_user_interaction` when the server returns a
    non-2xx status.

    The raw response body text is the exception message so callers can detect
    the overloaded ``"input not registered for step N"`` string that agy
    returns when the interaction has not yet been registered for the step
    (a race the Task 8 bridge must retry on).
    """


def handle_user_interaction(
    port: int,
    cascade_id: str,
    *,
    trajectory_id: str,
    step_index: int,
    payload: dict[str, object],
) -> None:
    """
    Deliver an interaction answer (question response / approval) to agy.

    POSTs ``{"cascadeId": cascade_id, "interaction": {"trajectoryId":
    trajectory_id, "stepIndex": step_index, **payload}}`` to
    ``HandleCascadeUserInteraction``. The ``trajectoryId`` and ``stepIndex``
    are nested inside ``interaction`` because the proto-JSON encoding drops
    top-level extras — they must be co-located with the payload variant dict.

    ``cascade_id`` is identical to the conversation id (agy uses the same
    UUID for both).

    :param port: Validated connect-RPC port, e.g. ``52548``.
    :param cascade_id: agy cascade id (equal to the conversation id) to
        address.
    :param trajectory_id: agy trajectory id identifying the active trajectory.
    :param step_index: Step index the interaction targets.
    :param payload: Variant dict, e.g. ``{"permission": {"allow": True}}`` or
        ``{"askQuestion": {...}}``.
    :raises AntigravityRpcError: On transport errors (e.g. connection refused)
        or any HTTP status >= 400. Transport errors are wrapped so the bridge
        has one exception type to catch, regardless of whether the failure was
        on the wire or at the application layer. On non-2xx, the raw response
        body text is the message (NOT ``raise_for_status()``) so callers can
        detect the overloaded ``"input not registered for step N"`` string.
    """
    url = _rpc_url(port, _METHOD_HANDLE_CASCADE_USER_INTERACTION)
    _assert_loopback_url(url)
    body: dict[str, object] = {
        "cascadeId": cascade_id,
        "interaction": {"trajectoryId": trajectory_id, "stepIndex": step_index, **payload},
    }
    try:
        with _sync_client(_PROBE_TIMEOUT_S) as client:
            response = client.post(
                url,
                headers={"Content-Type": "application/json"},
                content=json.dumps(body).encode("utf-8"),
            )
    except httpx.HTTPError as e:
        raise AntigravityRpcError(f"transport error contacting agy: {e}") from e
    if response.status_code >= 400:
        raise AntigravityRpcError(response.text)


def _list_agy_pids() -> list[int]:
    """
    Return pids of running ``agy`` processes (best-effort).

    Isolated as a seam so tests can stub it. Matches the agy binary path
    (``.../bin/agy``) to avoid matching unrelated commands that merely mention
    "agy". Prefers ``pgrep -f`` (portable across Linux + macOS); when ``pgrep``
    is unavailable — e.g. a minimal container image without ``procps`` — falls
    back to a ``/proc`` cmdline scan (:func:`_list_agy_pids_from_proc`) so
    discovery still works rather than silently yielding no candidates (which
    surfaces to the user as the misleading "is the agy terminal still open?").

    :returns: Candidate agy pids, newest-not-guaranteed order.
    """
    try:
        completed = subprocess.run(
            ["pgrep", "-f", r"bin/agy"],
            capture_output=True,
            text=True,
            timeout=_LSOF_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # ``pgrep`` missing (no procps), hung past its timeout, or otherwise
        # unusable — fall back to a /proc scan instead of giving up, so a turn
        # can still be injected. ``exc_info`` records the real cause.
        _logger.debug("pgrep failed; falling back to /proc scan for agy pids", exc_info=True)
        return _list_agy_pids_from_proc()
    pids: list[int] = []
    for line in completed.stdout.split():
        if line.isdigit():
            pids.append(int(line))
    return pids


def _list_agy_pids_from_proc() -> list[int]:
    """
    Enumerate agy pids by scanning ``/proc/<pid>/cmdline`` (no ``pgrep`` needed).

    The Linux-only fallback for :func:`_list_agy_pids`. Mirrors ``pgrep -f
    bin/agy``: a process matches when its full (NUL-joined) command line
    contains ``bin/agy``, which the launcher always satisfies because
    :func:`omnigent.antigravity_native_launch.agy_binary_path` resolves to an
    absolute ``.../bin/agy`` path. Unreadable or vanished ``/proc`` entries are
    skipped; a missing ``/proc`` (non-Linux) yields ``[]``.

    :returns: Candidate agy pids.
    """
    pids: list[int] = []
    try:
        entries = os.listdir(_PROC_FS)
    except OSError:
        return []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join(_PROC_FS, entry, "cmdline"), "rb") as handle:
                cmdline = handle.read()
        except OSError:
            # Process exited between listdir and open, or cmdline is unreadable.
            continue
        if b"bin/agy" in cmdline.replace(b"\0", b" "):
            pids.append(int(entry))
    return pids


def discover_language_server_port(pid: int) -> int | None:
    """
    Resolve the connect-RPC (TLS) port for a known agy pid.

    ``lsof``-es the pid's ``127.0.0.1`` TCP LISTEN ports and returns the first
    (lowest) one that answers ``Heartbeat`` with HTTP 200 — agy's lower port is
    the TLS connect-RPC surface and the higher one is plain HTTP that fails the
    probe. Probing lowest-first means the connect-RPC port is found without
    assuming the two ports are exactly adjacent.

    :param pid: agy process id, e.g. ``72753``.
    :returns: The validated connect-RPC port, or ``None`` when the process has
        no loopback listeners or none answer ``Heartbeat`` (e.g. agy has exited
        or has not finished binding).
    """
    ports = _parse_loopback_listen_ports(_run_lsof_listen_ports(pid))
    for port in ports:
        if _heartbeat_ok(port):
            _logger.debug("agy connect-RPC port resolved: pid=%s port=%s", pid, port)
            return port
    return None


def _candidate_agy_rpc_ports() -> list[int]:
    """
    Return every live agy connect-RPC port, validated by ``Heartbeat``.

    Primary path: ``lsof`` each running agy pid's loopback LISTEN ports (precise
    — scopes to agy). Fallback: when ``lsof`` attributes no ports — a restricted
    ``/proc`` where agy's listening socket is not in its pid's fd table (verified
    on uid-1000 k8s pods, where agy 1.0.10 holds the listener in a backend the
    agy process does not own as an fd) — enumerate every loopback LISTEN port
    from ``/proc/net/tcp`` via :func:`_list_loopback_listen_ports`, which needs
    no fd/ptrace access.

    The fallback fires only when agy IS running but lsof saw none of its ports —
    not when no agy is running at all — so a turn-injection attempt against a
    dead session does not heartbeat every unrelated loopback service. The scan is
    also capped at :data:`_MAX_FALLBACK_PROBE_PORTS` (lowest-first), with the drop
    logged, to bound the probe count on a host with many loopback listeners.

    Either way the candidates are ``Heartbeat``-filtered, so only agy's TLS
    connect-RPC port(s) survive (agy's plain-HTTP port and unrelated loopback
    listeners fail the probe). Callers additionally confirm conversation
    ownership before injecting, so a stray non-agy port can never be written to.

    :returns: Sorted connect-RPC ports that answer ``Heartbeat`` with HTTP 200.
    """
    agy_pids = _list_agy_pids()
    ports: set[int] = set()
    for pid in agy_pids:
        ports.update(_parse_loopback_listen_ports(_run_lsof_listen_ports(pid)))
    if agy_pids and not ports:
        loopback = _list_loopback_listen_ports()
        if len(loopback) > _MAX_FALLBACK_PROBE_PORTS:
            _logger.warning(
                "agy port discovery fallback: %d loopback listeners exceed the "
                "%d-probe cap; probing the lowest %d only",
                len(loopback),
                _MAX_FALLBACK_PROBE_PORTS,
                _MAX_FALLBACK_PROBE_PORTS,
            )
            loopback = loopback[:_MAX_FALLBACK_PROBE_PORTS]
        ports.update(loopback)
    return [port for port in sorted(ports) if _heartbeat_ok(port)]


def conversation_id_owned_by_pid(pid: int, candidate_ids: Iterable[str]) -> str | None:
    """
    Return which candidate conversation id a specific agy pid owns.

    The deterministic counterpart to :func:`resolve_language_server_port`: that
    function answers "which port owns this *known* id"; this one answers "which
    of these candidate ids does *this* process own", binding discovery to a
    specific agy pid (e.g. the one running under this session's tmux pane).

    agy exposes no method to *list* its conversation id, only
    ``GetConversationMetadata`` which confirms a given id. So this resolves the
    pid's own connect-RPC port via :func:`discover_language_server_port` and asks
    that port to confirm each candidate (``GetConversationMetadata`` returns
    metadata only for an id that server hosts), eliminating the newest-dir guess,
    the cross-launch ambiguity, and the resulting livelock.

    Correctness over liveness, mirroring the forwarder's
    ``_discover_conversation_id``: a pid's connect-RPC server can confirm more
    than one candidate brain-dir id, so every candidate is tested and the result
    is bound only when *exactly one* matches. Zero or multiple matches return
    ``None`` (the multi-match case is refused rather than guessed, since
    first-match would depend on ``candidate_ids`` order and could bind the wrong
    transcript / external_session_id).

    On a host where ``lsof`` cannot attribute the socket to *pid* (restricted
    ``/proc``), the resolve falls back to checking the candidates against EVERY
    live agy connect-RPC port rather than just *pid*'s. Because the conversation
    id is globally unique this never mis-binds; but two concurrent same-host
    sessions sharing the HOME-global brain dir can put both their ids in
    ``candidate_ids``, in which case both are confirmed (by different ports) and
    the call refuses (returns ``None``) instead of guessing — the forwarder then
    retries. So the fallback trades the pid-scoped *liveness* of the binding for
    the same safety, never correctness. (The executor's write path,
    :func:`resolve_language_server_port`, is unaffected: it already holds the
    target conversation id, so a single port resolves unambiguously.)

    :param pid: agy process id whose conversation to resolve, e.g. ``72753``.
    :param candidate_ids: agy conversation ids to test (e.g. the in-window
        brain-dir names).
    :returns: The candidate id this pid's connect-RPC server confirms it hosts
        when exactly one matches; ``None`` when the port cannot be resolved (agy
        not bound yet / exited), no candidate matches, or — refusing to guess —
        more than one candidate matches.
    """
    candidates = list(candidate_ids)
    # Prefer the pid-scoped port (lsof — precise). When lsof cannot attribute the
    # socket to the pid (restricted /proc; see _candidate_agy_rpc_ports), fall
    # back to every live agy connect-RPC port: the conversation id is globally
    # unique, so a candidate is confirmed only by the port that actually hosts
    # it — the binding stays correct without the pid scoping.
    scoped = discover_language_server_port(pid)
    ports = [scoped] if scoped is not None else _candidate_agy_rpc_ports()
    if not ports:
        _logger.debug("agy pid=%s has no resolvable connect-RPC port yet", pid)
        return None
    matched = [
        candidate
        for candidate in candidates
        if any(_conversation_matches(port, candidate) for port in ports)
    ]
    if len(matched) == 1:
        _logger.info(
            "agy conversation resolved by pid ownership: pid=%s ports=%s conversation=%s",
            pid,
            ports,
            matched[0],
        )
        return matched[0]
    if not matched:
        _logger.debug(
            "agy pid=%s (ports=%s) owns none of the %d candidate conversation ids",
            pid,
            ports,
            len(candidates),
        )
        return None
    _logger.warning(
        "agy pid=%s (ports=%s) confirmed %d candidate conversation ids; refusing "
        "to guess which it owns: %s",
        pid,
        ports,
        len(matched),
        matched[:_MAX_LOGGED_AMBIGUOUS_IDS],
    )
    return None


def resolve_language_server_port(conversation_id: str) -> int | None:
    """
    Resolve agy's connect-RPC port for a conversation by validated discovery.

    Enumerates candidate agy connect-RPC ports (:func:`_candidate_agy_rpc_ports`
    — ``lsof`` per agy pid, or ``/proc/net/tcp`` where ``lsof`` cannot attribute
    the socket to a pid) and returns the first that owns ``conversation_id`` via
    ``GetConversationMetadata``. With one agy this is unambiguous; with several
    the conversation check picks the one actually hosting this conversation.

    Discovery is port-first (not pid-first): agy is launched under
    ``tmux_start_on_attach`` so the launcher never captures a pid, and on some
    hosts agy's listening socket is owned by a backend that is neither the agy
    pid nor ``lsof``-attributable — so a pid is not a reliable key. The
    ``GetConversationMetadata`` ownership check is what makes a port safe to use:
    a recycled/foreign port (a different live agy) is rejected because it does
    not host ``conversation_id``.

    :param conversation_id: agy conversation id the turn targets, e.g.
        ``"90468e33-..."``. Used to disambiguate when multiple agy processes
        run.
    :returns: A validated connect-RPC port that hosts ``conversation_id``, or
        ``None`` when no running agy could be resolved.
    """
    for port in _candidate_agy_rpc_ports():
        if _conversation_matches(port, conversation_id):
            _logger.info(
                "agy connect-RPC port resolved by conversation match: port=%s conversation=%s",
                port,
                conversation_id,
            )
            return port
    _logger.warning(
        "could not resolve an agy connect-RPC port for conversation=%s",
        conversation_id,
    )
    return None


async def interrupt_turn(port: int, conversation_id: str) -> bool:
    """
    Best-effort interrupt of an in-flight agy turn via connect-RPC (FAIL-OPEN).

    Intended to back the post-hoc audit's "decline → stop the turn" on a policy
    DENY/ASK (see :mod:`omnigent.antigravity_native_audit`). It is **best-effort
    and fail-open**: the audit warning is always surfaced regardless of whether
    this succeeds, and the offending tool has already run (agy writes a step only
    at ``DONE``), so a cancel can at most stop *subsequent* tools in the same
    turn — it never prevents the violation.

    .. warning:: **Wired OFF — request contract unverified.**
       agy 1.0.8 exposes ``ForceStopCascadeTree`` / ``CancelCascadeInvocation`` /
       ``CancelCascadeSteps`` on the connect-RPC surface (method names verified in
       the binary), but the request CONTRACT is **not** verified: the proto field
       tags show these key on an internal ``cascade_id`` / ``invocation_id`` —
       agy's per-turn identifiers, which are NOT exposed in the transcript and
       which this forwarder does not hold (it only knows the *conversation* id).
       The stop semantics are also unconfirmed against a live process. Until the
       request shape (and the cascade-id source) is verified end-to-end, this
       function does not issue an RPC: it logs and returns ``False`` so the gated
       caller treats the interrupt as unavailable and relies on the audit warning
       alone.

       TODO(antigravity-interrupt): verify ``ForceStopCascadeTreeRequest`` (does
       it accept the conversation id, or does it require a cascade id obtained
       from ``GetCascadeTrajectorySteps`` / a metadata RPC?) on a live agy, then
       implement the POST (a loopback connect-RPC call like the
       ``GetConversationMetadata`` probe above) and flip
       ``_INTERRUPT_ON_AUDIT_DENY`` (forwarder) to opt-in.

    :param port: agy connect-RPC (TLS) port, e.g. ``52548``.
    :param conversation_id: agy conversation id whose turn should be stopped.
    :returns: ``True`` when agy accepted the cancel; ``False`` when the interrupt
        is unavailable (currently always, pending contract verification) or the
        call failed. Never raises — fail-open.
    """
    # Intentionally not issuing an RPC: the request contract is unverified and
    # the forwarder lacks agy's internal cascade/invocation id. Returning False
    # keeps the caller fail-open (the audit warning still surfaces). The method
    # constant is referenced so the verified name is not dead and a future
    # implementation has the anchor.
    del port  # unused until the contract is verified (see warning above)
    _logger.debug(
        "agy turn interrupt requested but unavailable (RPC %s contract unverified); "
        "relying on the audit warning only: conversation=%s",
        _METHOD_FORCE_STOP_CASCADE_TREE,
        conversation_id,
    )
    return False
