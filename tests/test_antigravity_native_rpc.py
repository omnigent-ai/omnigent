"""Tests for the native Antigravity (agy) connect-RPC client.

No live agy is launched here. The OS seams (``lsof`` / ``pgrep``) are
monkeypatched and the HTTP layer is driven through ``httpx.MockTransport`` so
each RPC's URL / headers / body shape is asserted without a real socket.
"""

from __future__ import annotations

import json
import struct
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest

import omnigent.antigravity_native_rpc as rpc

# Realistic two-port agy ``lsof`` output: the LOWER (52548) is the TLS
# connect-RPC port; the higher (52549) is plain HTTP. Mirrors the verified
# spike output (``docs/claude/antigravity-sidecar-spike.md``).
_LSOF_TWO_PORTS = (
    "COMMAND   PID    USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME\n"
    "agy     72753 bryanli    8u  IPv4 0x7a69608b086a898d      0t0  TCP 127.0.0.1:52549 (LISTEN)\n"
    "agy     72753 bryanli    9u  IPv4 0x9fe8e4e3d01a00bd      0t0  TCP 127.0.0.1:52548 (LISTEN)\n"
)

_CONVERSATION_ID = "90468e33-38c3-4e48-ae9f-03c843196227"


# ---------------------------------------------------------------------------
# _parse_loopback_listen_ports
# ---------------------------------------------------------------------------


def test_parse_loopback_listen_ports_sorted_unique() -> None:
    """
    Loopback LISTEN ports parse ascending and de-duplicated.

    The discovery probes lowest-first to find the TLS connect-RPC port, so the
    parser must return the lower port first regardless of ``lsof`` row order.
    """
    assert rpc._parse_loopback_listen_ports(_LSOF_TWO_PORTS) == [52548, 52549]


def test_parse_loopback_listen_ports_ignores_non_loopback() -> None:
    """
    Non-127.0.0.1 listeners (``*:`` / IPv6 / other IPs) are excluded.

    agy binds its control ports on ``127.0.0.1``; a wildcard or LAN listener
    from another process must never be mistaken for agy's port.
    """
    mixed = (
        "node  1 u IPv6 0x0 0t0 TCP *:5001 (LISTEN)\n"
        "x     2 u IPv4 0x0 0t0 TCP 192.168.1.5:8080 (LISTEN)\n"
        "agy   3 u IPv4 0x0 0t0 TCP 127.0.0.1:61000 (LISTEN)\n"
    )
    assert rpc._parse_loopback_listen_ports(mixed) == [61000]


def test_parse_loopback_listen_ports_empty() -> None:
    """
    Empty ``lsof`` output yields no ports.

    A dead/exited agy produces no listeners; the parser must return ``[]`` so
    discovery reports "no port" rather than crashing.
    """
    assert rpc._parse_loopback_listen_ports("") == []


# ---------------------------------------------------------------------------
# discover_language_server_port
# ---------------------------------------------------------------------------


def test_discover_language_server_port_returns_validated_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Discovery returns the lower port that answers ``Heartbeat`` with 200.

    Of agy's two ports only the lower (TLS connect-RPC) answers Heartbeat; the
    discovery must probe lowest-first and return it, not the plain-HTTP port.
    """
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: _LSOF_TWO_PORTS)
    probed: list[int] = []

    def _fake_heartbeat(port: int) -> bool:
        probed.append(port)
        return port == 52548

    monkeypatch.setattr(rpc, "_heartbeat_ok", _fake_heartbeat)
    assert rpc.discover_language_server_port(72753) == 52548
    # Lowest probed first; 52548 validated so the higher port is never probed.
    assert probed == [52548]


def test_discover_language_server_port_skips_failing_lower(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A lower port that fails ``Heartbeat`` is skipped for the next candidate.

    The two ports are not guaranteed exactly adjacent, and the lower could be an
    unrelated listener; discovery must keep probing in ascending order rather
    than give up after the first failure.
    """
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: _LSOF_TWO_PORTS)
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: port == 52549)
    assert rpc.discover_language_server_port(72753) == 52549


def test_discover_language_server_port_none_when_no_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No loopback listeners → ``None``.

    When agy has exited or not yet bound, ``lsof`` returns nothing and discovery
    must yield ``None`` so the executor surfaces a clear error.
    """
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: "")
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: True)
    assert rpc.discover_language_server_port(72753) is None


def test_discover_language_server_port_none_when_no_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Ports exist but none answer ``Heartbeat`` → ``None``.

    Guards against returning a non-connect-RPC port (e.g. only the plain-HTTP
    port is up): without a 200 Heartbeat the port is not the control surface.
    """
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: _LSOF_TWO_PORTS)
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: False)
    assert rpc.discover_language_server_port(72753) is None


# ---------------------------------------------------------------------------
# _heartbeat_ok / _conversation_matches (HTTP via MockTransport)
# ---------------------------------------------------------------------------


def test_heartbeat_ok_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``_heartbeat_ok`` POSTs ``Heartbeat`` and returns True on HTTP 200.

    Asserts the exact connect-RPC URL + JSON content-type the spike verified, so
    a refactor cannot silently change the probe wire shape.
    """
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200, json={"lastExtensionHeartbeat": "2026-06-15T00:00:00Z"})

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(_handler))
    assert rpc._heartbeat_ok(52548) is True
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/Heartbeat"
    )
    assert seen["content_type"] == "application/json"
    assert seen["body"] == b"{}"


def test_heartbeat_ok_false_on_404(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A non-200 Heartbeat (the plain-HTTP port 404s) returns False.

    This is how discovery distinguishes the higher plain-HTTP port from the TLS
    connect-RPC port.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(404)),
    )
    assert rpc._heartbeat_ok(52549) is False


def test_heartbeat_ok_false_on_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A transport/TLS error during Heartbeat returns False, not an exception.

    A port that resets the connection (e.g. wrong protocol) must be treated as
    "not the connect-RPC port" without crashing discovery.
    """

    def _boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(_boom))
    assert rpc._heartbeat_ok(52548) is False


def test_conversation_matches_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``_conversation_matches`` is True when metadata echoes the requested id.

    Asserts it sends ``{"conversationId": ...}`` to GetConversationMetadata and
    accepts a 200 whose ``metadata.rootConversationId`` echoes the id (the real
    agy response shape) — the disambiguation used when several agy run.
    """
    seen: dict[str, object] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"metadata": {"rootConversationId": _CONVERSATION_ID, "projectId": "p1"}}
        )

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(_handler))
    assert rpc._conversation_matches(52548, _CONVERSATION_ID) is True
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/"
        "GetConversationMetadata"
    )
    assert seen["body"] == {"conversationId": _CONVERSATION_ID}


def test_conversation_matches_false_on_id_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A 200 whose metadata echoes a DIFFERENT root id returns False.

    Injection-safety gate: with the /proc/net/tcp fallback the candidate ports
    span every loopback listener, so a port that answers Heartbeat 200 and even
    returns a metadata object — but for another conversation — must be rejected
    before any SendAgentMessage. Only an exact id echo confirms ownership.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={"metadata": {"rootConversationId": "00000000-0000-4000-8000-000000000000"}},
            )
        ),
    )
    assert rpc._conversation_matches(52548, _CONVERSATION_ID) is False


def test_conversation_matches_false_on_error_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A non-200 (unknown conversation) returns False.

    An agy process that does not host this conversation answers HTTP 500
    (``"trajectory not found"``, verified live), so it must not be selected.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(500, json={"code": "unknown"})),
    )
    assert rpc._conversation_matches(52548, _CONVERSATION_ID) is False


def test_conversation_matches_false_without_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A 200 lacking a ``metadata`` object (or its id echo) returns False.

    Guards against treating an empty/odd 200 body — e.g. a non-agy loopback
    service — as a match.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    assert rpc._conversation_matches(52548, _CONVERSATION_ID) is False


# ---------------------------------------------------------------------------
# resolve_language_server_port
# ---------------------------------------------------------------------------


def test_resolve_finds_conversation_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The resolver finds the agy that owns the conversation by validated discovery.

    Among several candidate connect-RPC ports, only the one whose server reports
    this conversation id is selected. Discovery is port-first — every candidate
    is conversation-validated, which is what makes a recycled or foreign port
    safe.
    """
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548, 60000])
    # Only the agy on port 60000 hosts the target conversation.
    monkeypatch.setattr(rpc, "_conversation_matches", lambda port, cid: port == 60000)
    assert rpc.resolve_language_server_port(_CONVERSATION_ID) == 60000


def test_resolve_validates_every_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Every resolved port is conversation-checked before it is returned.

    Guards the cross-inject fix: a port that answers Heartbeat but hosts a
    *different* conversation must be rejected, never returned positionally.
    """
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [40000, 52548])
    probed_for_match: list[int] = []

    def _matches(port: int, cid: str) -> bool:
        probed_for_match.append(port)
        return port == 52548  # only this port hosts the conversation

    monkeypatch.setattr(rpc, "_conversation_matches", _matches)
    assert rpc.resolve_language_server_port(_CONVERSATION_ID) == 52548
    # Both candidate ports were conversation-checked, in enumeration order.
    assert probed_for_match == [40000, 52548]


def test_resolve_none_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    No agy hosting the conversation → ``None``.

    The executor turns this into a clear ExecutorError instead of hanging.
    """
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548, 40000])
    monkeypatch.setattr(rpc, "_conversation_matches", lambda port, cid: False)
    assert rpc.resolve_language_server_port(_CONVERSATION_ID) is None


# ---------------------------------------------------------------------------
# conversation_id_owned_by_pid (Finding 2: deterministic by-pid binding)
# ---------------------------------------------------------------------------

_CID_A = "11111111-1111-4111-8111-111111111111"
_CID_B = "22222222-2222-4222-8222-222222222222"


def test_conversation_id_owned_by_pid_returns_the_owned_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A specific pid's port confirms exactly one candidate — that id is returned.

    This is the inverse of ``resolve_language_server_port``: given a known agy
    pid (the one under this session's tmux pane) and several candidate brain-dir
    ids, the candidate THIS pid's connect-RPC server hosts is selected. Two
    concurrent agys have different pids → different ports, so each resolves only
    its own conversation — eliminating the newest-dir guess and the livelock.
    """
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda pid: 52548)
    # This pid's port hosts only _CID_B (a concurrent agy would host _CID_A).
    monkeypatch.setattr(rpc, "_conversation_matches", lambda port, cid: cid == _CID_B)
    assert rpc.conversation_id_owned_by_pid(72753, [_CID_A, _CID_B]) == _CID_B


def test_conversation_id_owned_by_pid_none_when_port_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No resolvable connect-RPC port for the pid → ``None`` (keep polling).

    A still-cold-starting agy has not bound its port yet; the resolver must not
    bind anything and the caller keeps polling rather than guessing. Both port
    sources are stubbed empty: the pid-scoped ``discover_language_server_port``
    AND the ``_candidate_agy_rpc_ports`` fallback (the resolver falls back to
    every live agy port when lsof can't attribute the socket — see the source).
    Stubbing only the former leaves the fallback scanning REAL agy processes,
    so this test would flake on any host/CI runner with a concurrent agy.
    """
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda pid: None)
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", list)
    calls: list[tuple[int, str]] = []

    def _matches(port: int, cid: str) -> bool:
        calls.append((port, cid))
        return True

    monkeypatch.setattr(rpc, "_conversation_matches", _matches)
    assert rpc.conversation_id_owned_by_pid(72753, [_CID_A, _CID_B]) is None
    # No port from either source → nothing to confirm against.
    assert calls == []


def test_conversation_id_owned_by_pid_none_when_no_candidate_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The port resolves but owns none of the candidates → ``None``.

    Defensive: if the in-window candidate set somehow excludes this pid's real
    conversation (e.g. a timing skew), bind nothing rather than mis-bind.
    """
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda pid: 52548)
    monkeypatch.setattr(rpc, "_conversation_matches", lambda port, cid: False)
    assert rpc.conversation_id_owned_by_pid(72753, [_CID_A, _CID_B]) is None


def test_conversation_id_owned_by_pid_refuses_when_multiple_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    More than one candidate confirmed → ``None`` (refuse, do not guess).

    A pid's connect-RPC server can confirm more than one candidate brain-dir id,
    and first-match would then depend on ``candidate_ids`` order and could bind
    the wrong transcript / external_session_id. So the resolver refuses an
    ambiguous match and returns ``None`` (the caller defers to the next poll)
    rather than returning either id — mirroring the forwarder's
    correctness-over-liveness ambiguity refusal.
    """
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda pid: 52548)
    monkeypatch.setattr(rpc, "_conversation_matches", lambda port, cid: True)
    resolved = rpc.conversation_id_owned_by_pid(72753, [_CID_A, _CID_B])
    assert resolved is None
    # Explicitly: neither candidate is bound on an ambiguous confirmation.
    assert resolved != _CID_A
    assert resolved != _CID_B


# ---------------------------------------------------------------------------
# _assert_loopback_url (loopback guard for verify=False clients)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:52548/svc/Method",
        "https://localhost:52548/svc/Method",
        "https://[::1]:52548/svc/Method",
        "https://127.5.6.7:9000/svc/Method",  # entire 127/8 block is loopback
    ],
)
def test_assert_loopback_url_allows_loopback(url: str) -> None:
    """
    Loopback hosts (127/8, ``localhost``, ``::1``) pass the guard silently.

    The connect-RPC clients disable TLS verification, which is only safe on
    loopback; the allow path must cover the hostnames and the whole 127/8 block
    that agy can bind so legitimate requests are never refused.
    """
    rpc._assert_loopback_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://10.0.0.1:1234/svc/Method",
        "https://192.168.1.5:52548/svc/Method",
        "https://example.com:52548/svc/Method",
        "https://8.8.8.8:443/svc/Method",
    ],
)
def test_assert_loopback_url_rejects_non_loopback(url: str) -> None:
    """
    Any non-loopback host is refused with ``ValueError``.

    With ``verify=False`` a non-loopback URL would silently trust any cert, so
    the guard fails loudly rather than letting a MITM succeed.
    """
    with pytest.raises(ValueError, match="non-loopback connect-RPC URL"):
        rpc._assert_loopback_url(url)


def test_heartbeat_ok_rejects_non_loopback_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``_heartbeat_ok`` raises ``ValueError`` if the built URL is non-loopback.

    The sync probe shares the loopback guard, so a discovered port that ever
    resolved off-host is refused before the ``verify=False`` client is used.
    """
    monkeypatch.setattr(rpc, "_rpc_url", lambda port, method: "https://10.0.0.1:1234/svc/Method")

    def _unexpected(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request must not be sent for a non-loopback URL")

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(_unexpected))
    with pytest.raises(ValueError, match="non-loopback connect-RPC URL"):
        rpc._heartbeat_ok(52548)


# ---------------------------------------------------------------------------
# interrupt_turn (best-effort, wired OFF — phase 4 task 4)
# ---------------------------------------------------------------------------


async def test_interrupt_turn_is_off_and_issues_no_rpc(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``interrupt_turn`` returns False and sends NO RPC.

    The agy cancel-RPC request contract is unverified (and the forwarder lacks
    agy's internal cascade id), so the interrupt is wired OFF: it must never
    issue a network call. Any attempt to send a request fails the test.
    """

    def _unexpected(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("interrupt_turn must not send an RPC while wired off")

    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", httpx.MockTransport(_unexpected))
    result = await rpc.interrupt_turn(52548, _CONVERSATION_ID)
    assert result is False


async def test_interrupt_turn_never_raises() -> None:
    """``interrupt_turn`` is fail-open: it returns False rather than raising."""
    assert await rpc.interrupt_turn(0, _CONVERSATION_ID) is False


# --- agy pid enumeration: pgrep primary, /proc fallback -------------------


def _completed(stdout: str) -> subprocess.CompletedProcess[str]:
    """A ``CompletedProcess`` stand-in for a stubbed ``pgrep`` run."""
    return subprocess.CompletedProcess(
        args=["pgrep", "-f", "bin/agy"], returncode=0, stdout=stdout
    )


def test_list_agy_pids_parses_pgrep_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_list_agy_pids`` returns the pids ``pgrep`` prints, ignoring noise."""

    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed("1234\n5678\nnot-a-pid\n")

    monkeypatch.setattr(subprocess, "run", _run)
    assert rpc._list_agy_pids() == [1234, 5678]


def test_list_agy_pids_falls_back_to_proc_when_pgrep_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A missing ``pgrep`` (no procps) routes to the /proc scan, not ``[]``.

    Guards the homelab regression: the host image shipped without ``procps``,
    so ``pgrep`` raised ``FileNotFoundError`` and discovery silently found no
    agy, surfacing as "is the agy terminal still open?". The fallback must run.
    """

    def _no_pgrep(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(2, "No such file or directory", "pgrep")

    monkeypatch.setattr(subprocess, "run", _no_pgrep)
    monkeypatch.setattr(rpc, "_list_agy_pids_from_proc", lambda: [4242])
    assert rpc._list_agy_pids() == [4242]


def test_list_agy_pids_falls_back_to_proc_on_pgrep_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ``pgrep`` *timeout* (present but hung) also routes to the /proc scan.

    Pins the contract that the except clause stays broad (``OSError`` AND
    ``SubprocessError``): narrowing it to only the missing-binary case would
    silently re-introduce the empty-list regression whenever pgrep hangs.
    """

    def _timeout(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=["pgrep"], timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    monkeypatch.setattr(rpc, "_list_agy_pids_from_proc", lambda: [9999])
    assert rpc._list_agy_pids() == [9999]


def test_list_agy_pids_from_proc_matches_bin_agy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The /proc scan matches ``bin/agy`` cmdlines and skips everything else."""

    def _write(pid: str, argv: list[str]) -> None:
        proc_dir = tmp_path / pid
        proc_dir.mkdir()
        (proc_dir / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    _write("111", ["/usr/local/bin/agy", "--dangerously-skip-permissions"])
    _write("222", ["node", "/opt/other/server.js"])  # unrelated → skipped
    _write("333", ["/data/.local/bin/agy"])  # installer-default path → matches
    (tmp_path / "not-a-pid").mkdir()  # non-numeric entry → skipped

    monkeypatch.setattr(rpc, "_PROC_FS", str(tmp_path))
    assert sorted(rpc._list_agy_pids_from_proc()) == [111, 333]


def test_list_agy_pids_from_proc_skips_empty_cmdline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A pid with an empty cmdline (kernel thread / zombie) is not matched."""
    (tmp_path / "444").mkdir()
    (tmp_path / "444" / "cmdline").write_bytes(b"")
    monkeypatch.setattr(rpc, "_PROC_FS", str(tmp_path))
    assert rpc._list_agy_pids_from_proc() == []


def test_list_agy_pids_from_proc_empty_when_no_procfs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing ``/proc`` (non-Linux) yields ``[]`` rather than raising."""
    monkeypatch.setattr(rpc, "_PROC_FS", "/no/such/proc/root")
    assert rpc._list_agy_pids_from_proc() == []


# --- /proc/net/tcp loopback port discovery (lsof-attribution fallback) ----


def test_is_loopback_hex_addr() -> None:
    """Only the IPv4 127.0.0.1 hex encoding is loopback (the RPC client is IPv4)."""
    assert rpc._is_loopback_hex_addr("0100007F") is True  # 127.0.0.1 (LE)
    assert rpc._is_loopback_hex_addr("0100007f") is True  # case-insensitive
    assert rpc._is_loopback_hex_addr("00000000000000000000000001000000") is False  # ::1 (IPv6)
    assert rpc._is_loopback_hex_addr("0100A8C0") is False  # 192.168.0.1
    assert rpc._is_loopback_hex_addr("00000000") is False  # 0.0.0.0 (wildcard)
    assert rpc._is_loopback_hex_addr("") is False


def _write_proc_net(tmp_path: Path, tcp: str, tcp6: str | None = None) -> None:
    net = tmp_path / "net"
    net.mkdir()
    (net / "tcp").write_text(tcp)
    if tcp6 is not None:
        (net / "tcp6").write_text(tcp6)


def test_list_loopback_listen_ports_parses_ipv4_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """IPv4 127.0.0.1 LISTEN ports parse; non-loopback, non-LISTEN, and IPv6 are excluded."""
    # tcp rows: 127.0.0.1:6763 LISTEN (keep); 192.168.0.1:8080 LISTEN (not
    # loopback, skip); 127.0.0.1:80 ESTABLISHED (state 01, not LISTEN, skip).
    tcp = (
        "  sl  local_address rem_address   st ...\n"
        "   0: 0100007F:1A6B 00000000:0000 0A 0 0 0\n"
        "   1: 0100A8C0:1F90 00000000:0000 0A 0 0 0\n"
        "   2: 0100007F:0050 0100007F:CAFE 01 0 0 0\n"
    )
    # An ::1 LISTEN row in tcp6 must be IGNORED — agy and the RPC client are IPv4.
    v6_lo = "00000000000000000000000001000000"
    v6_any = "00000000000000000000000000000000"
    tcp6 = f"  sl  local_address ...\n   0: {v6_lo}:30D4 {v6_any}:0000 0A 0\n"
    _write_proc_net(tmp_path, tcp, tcp6)
    monkeypatch.setattr(rpc, "_PROC_FS", str(tmp_path))
    assert rpc._list_loopback_listen_ports() == [6763]


def test_list_loopback_listen_ports_missing_proc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A present /proc/net/tcp parses; a missing /proc (non-Linux) yields []."""
    _write_proc_net(tmp_path, "header\n   0: 0100007F:2710 00000000:0000 0A 0\n")
    monkeypatch.setattr(rpc, "_PROC_FS", str(tmp_path))
    assert rpc._list_loopback_listen_ports() == [10000]
    monkeypatch.setattr(rpc, "_PROC_FS", "/no/such/proc")
    assert rpc._list_loopback_listen_ports() == []


def test_candidate_agy_rpc_ports_uses_lsof_when_attributed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When lsof attributes ports to a pid, /proc/net/tcp is NOT consulted."""
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [72753])
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: _LSOF_TWO_PORTS)
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: port == 52548)  # lower = connect-RPC

    def _must_not_call() -> list[int]:
        raise AssertionError("/proc/net/tcp fallback must not run when lsof attributed ports")

    monkeypatch.setattr(rpc, "_list_loopback_listen_ports", _must_not_call)
    assert rpc._candidate_agy_rpc_ports() == [52548]


def test_candidate_agy_rpc_ports_falls_back_to_proc_net_tcp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When lsof attributes nothing (restricted /proc), enumerate /proc/net/tcp.

    Pins the homelab uid-1000 fix: agy's listening socket is not in its pid's fd
    table, so lsof -p <pid> is empty; discovery must still find the port via the
    network-namespace socket table, Heartbeat-filtered.
    """
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [72753])
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: "")  # lsof attributes nothing
    monkeypatch.setattr(rpc, "_list_loopback_listen_ports", lambda: [6767, 44955, 37479])
    # Only agy's TLS connect-RPC port answers Heartbeat (6767=omnigent, 37479=agy plain-HTTP).
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: port == 44955)
    assert rpc._candidate_agy_rpc_ports() == [44955]


def test_candidate_agy_rpc_ports_no_fallback_when_no_agy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    No agy process → no /proc/net/tcp scan.

    A turn-injection attempt against a dead session must not heartbeat every
    unrelated loopback service: the fallback is gated on agy actually running.
    """
    monkeypatch.setattr(rpc, "_list_agy_pids", list)
    monkeypatch.setattr(rpc, "_run_lsof_listen_ports", lambda pid: "")

    def _must_not_call() -> list[int]:
        raise AssertionError("fallback must not run when no agy is present")

    monkeypatch.setattr(rpc, "_list_loopback_listen_ports", _must_not_call)
    monkeypatch.setattr(rpc, "_heartbeat_ok", lambda port: True)
    assert rpc._candidate_agy_rpc_ports() == []


def test_conversation_id_owned_by_pid_falls_back_when_lsof_blind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The forwarder's by-pid binding survives a /proc where lsof can't see the port.

    discover_language_server_port returns None (lsof blind); the candidate is
    still confirmed against every live agy connect-RPC port — correct because the
    conversation id is globally unique.
    """
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda pid: None)
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [44955])
    monkeypatch.setattr(
        rpc, "_conversation_matches", lambda port, cid: cid == _CID_A and port == 44955
    )
    assert rpc.conversation_id_owned_by_pid(72753, [_CID_A, _CID_B]) == _CID_A


# ---------------------------------------------------------------------------
# get_trajectory_steps / cancel_cascade_steps (unary RPC, Task 2)
# ---------------------------------------------------------------------------


def test_get_trajectory_steps_posts_cascade_id_and_returns_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``get_trajectory_steps`` POSTs ``{"cascadeId": ...}`` to
    ``GetCascadeTrajectorySteps`` and returns the ``steps`` list from the
    response body.

    Asserts the exact URL, JSON body, and parsed list shape so a refactor
    cannot silently change the wire contract consumed by the read driver.
    """
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = req.content
        return httpx.Response(
            200, json={"steps": [{"stepIndex": 0, "status": "CORTEX_STEP_STATUS_DONE"}]}
        )

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    steps = rpc.get_trajectory_steps(52548, "conv-uuid")
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/"
        "GetCascadeTrajectorySteps"
    )
    body = seen["body"]
    assert isinstance(body, (bytes, bytearray))
    assert json.loads(body) == {"cascadeId": "conv-uuid"}
    assert steps[0]["stepIndex"] == 0


def test_get_trajectory_steps_raises_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A non-2xx response from ``GetCascadeTrajectorySteps`` raises
    ``httpx.HTTPStatusError``.

    Pins the "non-2xx raises, not fail-open" contract: the Task 6 read driver
    is responsible for retry/backoff, and a 500 must propagate rather than
    silently returning an empty list.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(500, json={"code": "unknown"})),
    )
    with pytest.raises(httpx.HTTPStatusError):
        rpc.get_trajectory_steps(52548, "conv-uuid")


def test_cancel_cascade_steps_true_on_200(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    ``cancel_cascade_steps`` POSTs ``{"cascadeId": ...}`` to
    ``CancelCascadeSteps`` and returns ``True`` when the server responds 200.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    assert rpc.cancel_cascade_steps(52548, "conv-uuid") is True


def test_cancel_cascade_steps_false_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A transport error during ``cancel_cascade_steps`` returns ``False``.

    Cancel is best-effort: a connection reset (e.g. agy already exited) must
    not propagate an exception to the executor, which treats ``False`` as a
    soft no-op.
    """

    def raise_transport(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(raise_transport))
    assert rpc.cancel_cascade_steps(52548, "conv-uuid") is False


# ---------------------------------------------------------------------------
# handle_user_interaction (unary RPC, Task 3)
# ---------------------------------------------------------------------------


def test_handle_user_interaction_nests_traj_and_step_inside_interaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``handle_user_interaction`` POSTs a body where ``trajectoryId`` and
    ``stepIndex`` are nested inside ``interaction``, not at the top level.

    The proto-JSON encoding drops top-level extras, so the ``trajectoryId`` and
    ``stepIndex`` MUST be inside the ``interaction`` key, alongside the payload
    variant dict. Asserts the exact wire body so a refactor cannot silently
    break the contract consumed by the interaction bridge.
    """
    seen: dict[str, bytes] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = req.content
        return httpx.Response(200, json={})

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    rpc.handle_user_interaction(
        52548,
        "c",
        trajectory_id="t",
        step_index=14,
        payload={"permission": {"allow": True}},
    )
    assert json.loads(seen["body"]) == {
        "cascadeId": "c",
        "interaction": {"trajectoryId": "t", "stepIndex": 14, "permission": {"allow": True}},
    }


def test_handle_user_interaction_raises_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A non-2xx response from ``HandleCascadeUserInteraction`` raises
    ``AntigravityRpcError`` carrying the raw response body text.

    The body text is load-bearing: the Task 8 interaction bridge detects the
    overloaded ``"input not registered for step N"`` string to handle the
    race-condition case where agy has not yet registered the interaction. The
    exception must carry the body, not httpx's generic message.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(500, json={"message": "input not registered for step 14"})
        ),
    )
    with pytest.raises(rpc.AntigravityRpcError) as exc_info:
        rpc.handle_user_interaction(
            52548,
            "c",
            trajectory_id="t",
            step_index=14,
            payload={"permission": {"allow": True}},
        )
    assert "input not registered" in str(exc_info.value)


def test_handle_user_interaction_raises_rpc_error_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A transport failure from ``HandleCascadeUserInteraction`` raises
    ``AntigravityRpcError``, NOT a raw ``httpx.HTTPError``.

    The Task 8 bridge catches ``AntigravityRpcError`` as the single failure
    surface for all delivery problems; a raw transport error bypassing that
    type would crash the bridge. Asserts the unified failure surface contract.
    """

    def raise_transport(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(raise_transport))
    with pytest.raises(rpc.AntigravityRpcError, match="transport error"):
        rpc.handle_user_interaction(
            52548,
            "c",
            trajectory_id="t",
            step_index=0,
            payload={"permission": {"allow": True}},
        )


# ---------------------------------------------------------------------------
# send_user_cascade_message (unary RPC, Task T-A)
# ---------------------------------------------------------------------------


def test_send_user_cascade_message_posts_exact_nested_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``send_user_cascade_message`` POSTs a body with ``items[].text`` and
    ``cascadeConfig.plannerConfig.planModel`` to ``SendUserCascadeMessage``.

    The design specifies EXACT shapes: text MUST be in ``items[0].text`` (a list
    of objects, NOT a flat "message"), and the model MUST be at
    ``cascadeConfig.plannerConfig.planModel`` (omitting it causes agy to error
    "neither PlanModel nor RequestedModel specified"). Asserts the full wire body
    so a refactor cannot silently break either constraint.
    """
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["content_type"] = req.headers.get("content-type")
        return httpx.Response(200, json={})

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    rpc.send_user_cascade_message(
        52548,
        "conv-uuid",
        "hello from omnigent",
        plan_model="gemini-2.5-pro",
    )
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/"
        "SendUserCascadeMessage"
    )
    assert seen["content_type"] == "application/json"
    assert seen["body"] == {
        "cascadeId": "conv-uuid",
        "items": [{"text": "hello from omnigent"}],
        "cascadeConfig": {"plannerConfig": {"planModel": "gemini-2.5-pro"}},
    }


def test_send_user_cascade_message_returns_none_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200 response causes ``send_user_cascade_message`` to return without raising.

    The turn-send is fire-and-forget from the caller's perspective; the only
    signal needed is "agy accepted" (200) vs "error" (raise). No exception on
    200 mirrors ``handle_user_interaction``'s contract.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, json={})),
    )
    rpc.send_user_cascade_message(52548, "c", "hi", plan_model="m")  # must not raise


def test_send_user_cascade_message_raises_rpc_error_on_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-2xx response raises ``AntigravityRpcError`` carrying the raw body text.

    Mirrors ``handle_user_interaction``: the body is load-bearing so the executor
    can surface model/validation error messages (e.g. "neither PlanModel nor
    RequestedModel specified") without wrapping them in a generic string.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(
                500, json={"message": "neither PlanModel nor RequestedModel specified"}
            )
        ),
    )
    with pytest.raises(rpc.AntigravityRpcError) as exc_info:
        rpc.send_user_cascade_message(52548, "c", "hi", plan_model="")
    assert "PlanModel" in str(exc_info.value)


def test_send_user_cascade_message_raises_rpc_error_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A transport failure raises ``AntigravityRpcError``, NOT raw ``httpx.HTTPError``.

    The executor catches ``AntigravityRpcError`` as the single failure surface;
    a raw transport error bypassing that type would crash the executor. Mirrors
    ``handle_user_interaction``'s transport-error contract.
    """

    def raise_transport(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(raise_transport))
    with pytest.raises(rpc.AntigravityRpcError, match="transport error"):
        rpc.send_user_cascade_message(52548, "c", "hi", plan_model="m")


# ---------------------------------------------------------------------------
# start_cascade (unary RPC, Task 11a cold-start bootstrap)
# ---------------------------------------------------------------------------


def test_start_cascade_posts_cascade_id_and_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``start_cascade`` POSTs ``{"cascadeId": ..., "source": ...}`` to ``StartCascade``.

    The runner cold-starts the conversation so its turn-1 has a real cascade id
    instead of waiting for the TUI to lazily mint one. ``source`` is the only
    required field; the runner PROVIDES the cascadeId so it owns the id. Asserts
    the full wire body so a refactor cannot drop either field or change the
    default source enum.
    """
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        seen["content_type"] = req.headers.get("content-type")
        return httpx.Response(200, json={"cascadeId": "runner-uuid"})

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    rpc.start_cascade(52548, "runner-uuid")
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/StartCascade"
    )
    assert seen["content_type"] == "application/json"
    assert seen["body"] == {
        "cascadeId": "runner-uuid",
        "source": "CORTEX_TRAJECTORY_SOURCE_CLI",
    }


def test_start_cascade_returns_none_on_200(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 200 response causes ``start_cascade`` to return without raising.

    The bootstrap only needs "agy created the cascade" (200) vs "error" (raise);
    no return value is consumed (the runner already holds the id it sent). Mirrors
    ``send_user_cascade_message``'s 200 contract.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, json={"cascadeId": "c"})),
    )
    rpc.start_cascade(52548, "c")  # must not raise


def test_start_cascade_raises_rpc_error_on_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-2xx response raises ``AntigravityRpcError`` carrying the raw body text.

    Mirrors ``send_user_cascade_message``: the body is the message (NOT
    ``raise_for_status()``) so the runner can surface agy's error verbatim rather
    than a generic status string.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(500, json={"message": "cascade boom"})),
    )
    with pytest.raises(rpc.AntigravityRpcError) as exc_info:
        rpc.start_cascade(52548, "c")
    assert "cascade boom" in str(exc_info.value)


def test_start_cascade_raises_rpc_error_on_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A transport failure raises ``AntigravityRpcError``, NOT a raw ``httpx.HTTPError``.

    The runner bootstrap catches ``AntigravityRpcError`` as the single failure
    surface; a raw transport error bypassing that type would crash the launch.
    Mirrors ``send_user_cascade_message``'s transport-error contract.
    """

    def raise_transport(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(raise_transport))
    with pytest.raises(rpc.AntigravityRpcError, match="transport error"):
        rpc.start_cascade(52548, "c")


def test_start_cascade_custom_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A caller-provided ``source`` overrides the default enum in the POST body.

    Keeps the source a parameter (not a hardcoded constant) so a future agy enum
    rename is a one-line caller change, not an edit to the RPC helper.
    """
    seen: dict[str, object] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={})

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    rpc.start_cascade(52548, "c", source="CORTEX_TRAJECTORY_SOURCE_OTHER")
    assert seen["body"] == {"cascadeId": "c", "source": "CORTEX_TRAJECTORY_SOURCE_OTHER"}


# ---------------------------------------------------------------------------
# get_available_models (unary RPC, Task T-A)
# ---------------------------------------------------------------------------


def test_get_available_models_posts_empty_body_and_returns_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``get_available_models`` POSTs ``{}`` to ``GetAvailableModels`` and returns
    the parsed response body (the full catalog dict).

    The catalog shape is ``{"models": {<key>: {"model", "displayName", ...}}}``.
    Asserts the URL and wire body so a refactor cannot silently change the method
    or add unexpected request fields.
    """
    seen: dict[str, object] = {}
    catalog = {
        "models": {
            "gemini-2.5-pro": {
                "model": "gemini-2.5-pro",
                "displayName": "Gemini 2.5 Pro",
                "recommended": True,
                "supportsThinking": True,
                "thinkingBudget": 8192,
            }
        }
    }

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json=catalog)

    monkeypatch.setattr(rpc, "_HTTP_TRANSPORT", httpx.MockTransport(handler))
    result = rpc.get_available_models(52548)
    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/GetAvailableModels"
    )
    assert seen["body"] == {}
    assert result == catalog


def test_get_available_models_raises_on_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A non-2xx response from ``GetAvailableModels`` raises ``httpx.HTTPStatusError``.

    Mirrors ``get_trajectory_steps``' error contract: a hard read failure raises
    rather than silently returning ``{}``, so the executor gets a clear signal
    that model resolution failed.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(500, json={"code": "unknown"})),
    )
    with pytest.raises(httpx.HTTPStatusError):
        rpc.get_available_models(52548)


# ---------------------------------------------------------------------------
# stream_agent_state_updates (connect server-stream, Task T-C)
# ---------------------------------------------------------------------------
#
# These tests drive the connect-protocol server-stream client over a STREAMING
# ``httpx.MockTransport`` response (a custom ``httpx.AsyncByteStream`` whose
# chunks are hand-crafted framed bytes). The framing is load-bearing: a bug in
# reassembly corrupts or drops live token deltas, so the tests pin the request
# envelope and exercise the three reassembly hazards — multiple frames in one
# chunk, one frame split across chunks, and the trailer terminating the stream.


def _frame(flag: int, payload: bytes) -> bytes:
    """
    Build one connect-protocol frame: ``[flag: 1B][length: 4B BE][payload]``.

    Mirrors the wire format the client must parse. ``flag == 0x00`` is a data
    message (payload is the JSON ``update`` object); ``flag & 0x02`` marks the
    end-of-stream trailer.

    :param flag: The 1-byte frame flag (e.g. ``0x00`` data, ``0x02`` trailer).
    :param payload: The frame payload bytes (JSON for a data frame).
    :returns: The fully framed bytes ready to concatenate into a stream.
    """
    return bytes([flag]) + struct.pack(">I", len(payload)) + payload


def _data_frame(obj: dict[str, object]) -> bytes:
    """Build a ``flag==0x00`` data frame whose payload is ``obj`` as JSON."""
    return _frame(0x00, json.dumps(obj).encode("utf-8"))


class _ByteChunks(httpx.AsyncByteStream):
    """
    An ``httpx.AsyncByteStream`` that yields a fixed list of byte chunks.

    Used to drive ``stream_agent_state_updates`` over ``MockTransport`` with
    EXACT control of chunk boundaries (``aiter_bytes`` preserves them), so the
    split-frame reassembly path can be exercised deterministically.
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


async def _collect(port: int, conversation_id: str) -> list[dict[str, object]]:
    """Drive the async generator to completion, collecting its yielded dicts."""
    return [update async for update in rpc.stream_agent_state_updates(port, conversation_id)]


def _stream_transport(chunks: list[bytes], seen: dict[str, object]) -> httpx.MockTransport:
    """
    Build a ``MockTransport`` that records the request and replies with a stream.

    Captures the request URL / content-type / body bytes into ``seen`` (so the
    connect envelope can be asserted) and returns a 200 streaming response whose
    body is ``chunks`` (hand-crafted frames).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["content_type"] = request.headers.get("content-type")
        seen["body"] = request.content
        return httpx.Response(200, stream=_ByteChunks(chunks))

    return httpx.MockTransport(handler)


async def test_stream_agent_state_updates_request_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The POST body is a single connect-enveloped frame and the content-type is
    ``application/connect+json``.

    The request body MUST be ``[0x00][BE-uint32 len][{"conversationId": ...}]``
    (a connect-enveloped message), not a bare JSON object — agy's connect-stream
    endpoint rejects an unframed body. Asserts the exact URL, header, and the
    byte-level envelope so a refactor cannot silently drop the framing.
    """
    seen: dict[str, object] = {}
    trailer = _frame(0x02, b"")
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport([trailer], seen))
    await _collect(52548, _CONVERSATION_ID)

    assert seen["url"] == (
        "https://127.0.0.1:52548/exa.language_server_pb.LanguageServerService/"
        "StreamAgentStateUpdates"
    )
    assert seen["content_type"] == "application/connect+json"
    body = seen["body"]
    assert isinstance(body, (bytes, bytearray))
    # Envelope: flag byte 0x00, then a 4-byte big-endian length, then the payload.
    assert body[0] == 0x00
    payload_len = struct.unpack(">I", body[1:5])[0]
    payload = body[5:]
    assert payload_len == len(payload)
    assert json.loads(payload) == {"conversationId": _CONVERSATION_ID}


async def test_stream_agent_state_updates_yields_data_frames_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stream of several data frames + a trailer yields the parsed dicts in order.

    The happy path: each ``flag==0x00`` frame's JSON payload is yielded as a
    dict, in arrival order, and the trailer ends iteration cleanly.
    """
    updates: list[dict[str, object]] = [
        {"mainTrajectoryUpdate": {"stepsUpdate": {"steps": [{"stepIndex": 0}]}}},
        {"mainTrajectoryUpdate": {"stepsUpdate": {"steps": [{"stepIndex": 1}]}}},
        {"conversationId": _CONVERSATION_ID},
    ]
    chunks = [_data_frame(u) for u in updates] + [_frame(0x02, b"")]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    assert await _collect(52548, _CONVERSATION_ID) == updates


async def test_stream_agent_state_updates_reassembles_split_and_packed_frames(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Frames split across chunks AND multiple frames in one chunk reassemble.

    The critical reassembly case. The transport yields chunk boundaries that do
    NOT line up with frame boundaries: frame A is split across two chunks, while
    frames B and C arrive packed together in a single chunk. The client must
    maintain a byte buffer and emit exactly A, B, C — never assuming one chunk
    equals one frame.
    """
    frame_a = _data_frame({"n": "a", "pad": "x" * 50})
    frame_b = _data_frame({"n": "b"})
    frame_c = _data_frame({"n": "c"})
    trailer = _frame(0x02, b"")

    split = len(frame_a) // 2
    chunks = [
        frame_a[:split],  # first half of A (header may itself be split here)
        frame_a[split:] + frame_b,  # rest of A, then B fully — boundary mid-chunk
        frame_c + trailer,  # C and the trailer packed together
    ]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    assert await _collect(52548, _CONVERSATION_ID) == [
        {"n": "a", "pad": "x" * 50},
        {"n": "b"},
        {"n": "c"},
    ]


async def test_stream_agent_state_updates_header_split_across_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A 5-byte frame header itself split across chunks reassembles correctly.

    The buffer must not attempt to read the header until it holds the full 5
    bytes (1 flag + 4 length). Here the first chunk carries only 2 bytes of the
    header, so the loop must await more before parsing.
    """
    frame = _data_frame({"hello": "world"})
    chunks = [frame[:2], frame[2:], _frame(0x02, b"")]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    assert await _collect(52548, _CONVERSATION_ID) == [{"hello": "world"}]


async def test_stream_agent_state_updates_trailer_ends_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A trailer frame (``flag & 0x02``) stops iteration even if more bytes follow.

    The end-of-stream trailer is authoritative: any data frame after it must NOT
    be yielded. Guards against over-reading a stream that agy has signalled
    complete.
    """
    chunks = [
        _data_frame({"n": "first"}),
        _frame(0x02, b'{"someTrailer": true}'),  # clean trailer (no error → stop)
        _data_frame({"n": "after-trailer-must-be-ignored"}),
    ]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    assert await _collect(52548, _CONVERSATION_ID) == [{"n": "first"}]


async def test_stream_agent_state_updates_raises_on_trailer_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A trailer carrying a connect ``error`` object raises ``AntigravityRpcError``.

    In connect streaming a mid-stream server failure is reported in the TRAILER
    PAYLOAD as ``{"error": {...}}`` — NOT via HTTP status (200 + headers were
    already flushed). Treating that trailer as a clean stop would silently
    truncate the turn for the reader, so the framing layer must surface it: the
    generator yields the data frames that DID arrive, then raises with the error
    message in the exception text.
    """
    chunks = [
        _data_frame({"n": "first"}),
        _data_frame({"n": "second"}),
        _frame(0x02, b'{"error": {"code": "internal", "message": "cascade exploded"}}'),
    ]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    collected: list[dict[str, object]] = []
    with pytest.raises(rpc.AntigravityRpcError, match="cascade exploded"):
        async for update in rpc.stream_agent_state_updates(52548, _CONVERSATION_ID):
            collected.append(update)
    # The data frames before the error trailer were still delivered in order.
    assert collected == [{"n": "first"}, {"n": "second"}]


async def test_stream_agent_state_updates_empty_trailer_is_clean_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An empty trailer payload (``b""``) — and a ``{}`` trailer — end the stream
    cleanly.

    Only a non-empty ``error`` object is an error; the common case is a trailer
    with no payload (or an empty object), which is a normal end-of-stream and
    must NOT raise.
    """
    chunks = [_data_frame({"n": "only"}), _frame(0x02, b"")]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    assert await _collect(52548, _CONVERSATION_ID) == [{"n": "only"}]

    chunks_empty_obj = [_data_frame({"n": "only"}), _frame(0x02, b"{}")]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks_empty_obj, {}))
    assert await _collect(52548, _CONVERSATION_ID) == [{"n": "only"}]


async def test_stream_agent_state_updates_raises_on_compressed_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A compressed frame (``flag & 0x01``) raises rather than mis-decoding.

    agy sends uncompressed frames; a compressed flag would mean the JSON decode
    is wrong. The client must raise a clear error instead of feeding compressed
    bytes to ``json.loads`` (which would surface as a confusing decode error).
    """
    chunks = [_frame(0x01, b"\x1f\x8b compressed junk")]
    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", _stream_transport(chunks, {}))
    with pytest.raises(rpc.AntigravityRpcError, match="compress"):
        await _collect(52548, _CONVERSATION_ID)


async def test_stream_agent_state_updates_rejects_non_loopback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A non-loopback stream URL is refused before the ``verify=False`` client runs.

    Shares the loopback guard with the unary RPCs: a discovered port that ever
    resolved off-host must be refused, since ``verify=False`` would otherwise
    silently trust any cert.
    """
    monkeypatch.setattr(rpc, "_rpc_url", lambda port, method: "https://10.0.0.1:1234/svc/Method")

    def _unexpected(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("request must not be sent for a non-loopback URL")

    monkeypatch.setattr(rpc, "_ASYNC_HTTP_TRANSPORT", httpx.MockTransport(_unexpected))
    with pytest.raises(ValueError, match="non-loopback connect-RPC URL"):
        await _collect(52548, _CONVERSATION_ID)


# ---------------------------------------------------------------------------
# Functional-RPC timeout: the four functional calls use _RPC_CALL_TIMEOUT_S, the
# discovery probes keep the tight _PROBE_TIMEOUT_S
# ---------------------------------------------------------------------------


def _capture_sync_client_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> list[float]:
    """Spy on ``_sync_client`` to record every per-request timeout it is built with.

    Returns the list the spy appends to; the real client is still constructed so
    the RPC's MockTransport request/response path runs unchanged.
    """
    timeouts: list[float] = []
    real_sync_client = rpc._sync_client

    def _spy(timeout: float) -> httpx.Client:
        timeouts.append(timeout)
        return real_sync_client(timeout)

    monkeypatch.setattr(rpc, "_sync_client", _spy)
    return timeouts


def test_functional_rpcs_use_rpc_call_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The four functional RPCs build their client with ``_RPC_CALL_TIMEOUT_S``.

    A tight discovery-probe timeout (2s) misused here would raise a transport
    ``TimeoutException`` against a momentarily busy agy; the caller does not retry
    — the interaction bridge only retries on the "input not registered" substring
    (a timed-out human-answer delivery would be permanently abandoned) and the
    cold-start does NOT retry ``StartCascade`` (a slow ack would deadlock the
    placeholder conversation id). The functional calls therefore get the generous
    ``_RPC_CALL_TIMEOUT_S`` headroom.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(lambda r: httpx.Response(200, json={"steps": []})),
    )

    timeouts = _capture_sync_client_timeouts(monkeypatch)
    rpc.get_trajectory_steps(52548, "c")
    rpc.cancel_cascade_steps(52548, "c")
    rpc.handle_user_interaction(
        52548, "c", trajectory_id="t", step_index=0, payload={"permission": {"allow": True}}
    )
    rpc.send_user_cascade_message(52548, "c", "hi", plan_model="MODEL_X")
    rpc.start_cascade(52548, "c")

    assert timeouts == [rpc._RPC_CALL_TIMEOUT_S] * 5


def test_discovery_probes_keep_probe_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """The discovery/probe RPCs keep the tight ``_PROBE_TIMEOUT_S``.

    Scanning several candidate ports must stay fast, so ``Heartbeat`` /
    ``GetConversationMetadata`` (during discovery) / ``GetAvailableModels`` keep
    the short probe budget — they were NOT widened to the functional timeout.
    """
    monkeypatch.setattr(
        rpc,
        "_HTTP_TRANSPORT",
        httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"metadata": {"rootConversationId": "c"}, "models": {}}
            )
        ),
    )

    timeouts = _capture_sync_client_timeouts(monkeypatch)
    rpc._heartbeat_ok(52548)
    rpc._conversation_matches(52548, "c")
    rpc.get_available_models(52548)

    assert timeouts == [rpc._PROBE_TIMEOUT_S] * 3


# ---------------------------------------------------------------------------
# _pane_pid
# ---------------------------------------------------------------------------


def _fake_tmux_pane_pid(stdout: str, *, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a ``tmux display-message`` CompletedProcess for stubbing."""
    return subprocess.CompletedProcess(
        args=["tmux"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_pane_pid_parses_display_message(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pane pid is read from ``tmux display-message -p #{pane_pid}``."""
    captured: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return _fake_tmux_pane_pid("72753\n")

    monkeypatch.setattr(rpc.subprocess, "run", _fake_run)
    assert rpc._pane_pid(Path("/tmp/agy/tmux.sock"), "main") == 72753
    # The exact tmux invocation: socket-scoped, the pane target, ``#{pane_pid}``.
    assert captured == [
        [
            "tmux",
            "-S",
            "/tmp/agy/tmux.sock",
            "display-message",
            "-p",
            "-t",
            "main",
            "#{pane_pid}",
        ]
    ]


def test_pane_pid_none_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead pane (non-zero tmux exit) yields ``None`` so the caller falls back."""
    monkeypatch.setattr(
        rpc.subprocess,
        "run",
        lambda *_a, **_k: _fake_tmux_pane_pid("", returncode=1),
    )
    assert rpc._pane_pid(Path("/tmp/agy/tmux.sock"), "main") is None


def test_pane_pid_none_on_nonnumeric_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-numeric tmux output (e.g. an error string) yields ``None``."""
    monkeypatch.setattr(
        rpc.subprocess,
        "run",
        lambda *_a, **_k: _fake_tmux_pane_pid("can't find pane\n"),
    )
    assert rpc._pane_pid(Path("/tmp/agy/tmux.sock"), "main") is None


def test_pane_pid_none_when_tmux_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``tmux`` binary (OSError) yields ``None``, never raising."""

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("tmux")

    monkeypatch.setattr(rpc.subprocess, "run", _boom)
    assert rpc._pane_pid(Path("/tmp/agy/tmux.sock"), "main") is None


# ---------------------------------------------------------------------------
# _agy_pid_in_pane_subtree
# ---------------------------------------------------------------------------


def test_agy_pid_in_pane_subtree_uses_pane_pid_when_it_is_agy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pane process IS agy (the ``exec agy`` case), the pane pid is used.

    The CLI / non-sandbox launch ``exec``s agy in the pane, so the pane pid is
    agy's own pid — no descendant walk needed.
    """
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [72753, 99999])
    # _child_pids must NOT be consulted when the pane pid itself is agy.
    monkeypatch.setattr(
        rpc,
        "_child_pids",
        lambda _pid: (_ for _ in ()).throw(AssertionError("should not walk children")),
    )
    assert rpc._agy_pid_in_pane_subtree(72753) == 72753


def test_agy_pid_in_pane_subtree_finds_agy_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the pane runs a wrapper (sandbox launcher), agy is a descendant.

    The sandbox launch makes the pane process the launcher → bwrap → ... → agy,
    so the agy pid is found by walking the pane pid's subtree and intersecting
    with the live agy pids.
    """
    # Tree: pane 100 -> 200 (bwrap) -> 300 (agy). agy pids = [300].
    children = {100: [200], 200: [300], 300: []}
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [300])
    monkeypatch.setattr(rpc, "_child_pids", lambda pid: children.get(pid, []))
    assert rpc._agy_pid_in_pane_subtree(100) == 300


def test_agy_pid_in_pane_subtree_none_when_no_agy_in_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No agy anywhere in the pane subtree yields ``None`` (caller falls back)."""
    children = {100: [200], 200: []}
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [999])  # agy elsewhere, not in tree
    monkeypatch.setattr(rpc, "_child_pids", lambda pid: children.get(pid, []))
    assert rpc._agy_pid_in_pane_subtree(100) is None


def test_agy_pid_in_pane_subtree_tolerates_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pathological parent cycle in the process table terminates (no hang)."""
    # 100 -> 200 -> 100 (cycle); agy is not in the tree.
    children = {100: [200], 200: [100]}
    monkeypatch.setattr(rpc, "_list_agy_pids", lambda: [300])
    monkeypatch.setattr(rpc, "_child_pids", lambda pid: children.get(pid, []))
    assert rpc._agy_pid_in_pane_subtree(100) is None


# ---------------------------------------------------------------------------
# _child_pids
# ---------------------------------------------------------------------------


def test_child_pids_parses_pgrep_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Direct children are parsed from ``pgrep -P`` output."""
    captured: list[list[str]] = []

    def _fake_run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="200\n201\n", stderr="")

    monkeypatch.setattr(rpc.subprocess, "run", _fake_run)
    assert rpc._child_pids(100) == [200, 201]
    assert captured == [["pgrep", "-P", "100"]]


def test_child_pids_falls_back_to_proc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When ``pgrep`` is missing, children come from ``/proc/<child>/stat`` PPid.

    Exercises the comm-with-spaces-and-parens edge: the PPid is read relative to
    the LAST ``)`` so a process named ``(a) b)`` does not corrupt field offsets.
    """

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(rpc.subprocess, "run", _boom)

    # Fake /proc: child 200 has PPid 100; 201 has a paren-laden comm but PPid 100;
    # 999 has PPid 1 (not a child); "self"/non-numeric entries are ignored.
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "200").mkdir()
    (proc / "200" / "stat").write_text("200 (agy) S 100 200 200 0 -1 0\n", encoding="ascii")
    (proc / "201").mkdir()
    (proc / "201" / "stat").write_text("201 (weird ) name) S 100 201 0\n", encoding="ascii")
    (proc / "999").mkdir()
    (proc / "999" / "stat").write_text("999 (init) S 1 999 0\n", encoding="ascii")
    (proc / "self").mkdir()  # non-numeric entry must be skipped
    monkeypatch.setattr(rpc, "_PROC_FS", str(proc))

    assert sorted(rpc._child_pids(100)) == [200, 201]


def test_child_pids_proc_empty_when_no_procfs(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing ``/proc`` (non-Linux) yields ``[]`` from the fallback."""

    def _boom(*_a: object, **_k: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("pgrep")

    monkeypatch.setattr(rpc.subprocess, "run", _boom)
    monkeypatch.setattr(rpc, "_PROC_FS", "/nonexistent-proc-xyz")
    assert rpc._child_pids(100) == []


# ---------------------------------------------------------------------------
# resolve_pane_agy_rpc_port
# ---------------------------------------------------------------------------


def test_resolve_pane_agy_rpc_port_scopes_to_pane_agy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pane's own agy pid drives ``discover_language_server_port``.

    This is the cross-bind fix: the port comes from THIS session's pane agy, not
    from the lowest of all agy ports on the host.
    """
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda pid: 300)
    discovered_for: list[int] = []

    def _fake_discover(pid: int) -> int:
        discovered_for.append(pid)
        return 61000

    monkeypatch.setattr(rpc, "discover_language_server_port", _fake_discover)
    assert rpc.resolve_pane_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") == 61000
    assert discovered_for == [300]  # scoped to the pane's agy, not a host-wide scan


def test_resolve_pane_agy_rpc_port_none_when_no_pane_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No resolvable pane pid → ``None`` (the caller uses the candidates fallback)."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: None)
    monkeypatch.setattr(
        rpc,
        "_agy_pid_in_pane_subtree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("unreachable")),
    )
    assert rpc.resolve_pane_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") is None


def test_resolve_pane_agy_rpc_port_none_when_no_agy_in_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live pane with no agy in its subtree → ``None`` (fallback)."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda _pid: None)
    monkeypatch.setattr(
        rpc,
        "discover_language_server_port",
        lambda _pid: (_ for _ in ()).throw(AssertionError("unreachable")),
    )
    assert rpc.resolve_pane_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") is None


def test_resolve_pane_agy_rpc_port_none_when_port_not_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """agy resolved but not yet bound (discover returns None) → ``None``."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda _pid: 300)
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda _pid: None)
    assert rpc.resolve_pane_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") is None


# ---------------------------------------------------------------------------
# resolve_pane_agy_rpc_port_state (3-state)
# ---------------------------------------------------------------------------


def test_resolve_pane_state_found_with_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """State 1: our agy is in the pane subtree and its port resolves."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda _pid: 300)
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda _pid: 61000)
    state = rpc.resolve_pane_agy_rpc_port_state(Path("/tmp/agy/tmux.sock"), "main")
    assert state == rpc.PaneAgyResolution(agy_found=True, port=61000)


def test_resolve_pane_state_found_without_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """State 2: our agy IS in the subtree but its port is not lsof-attributable.

    ``agy_found`` MUST be ``True`` so the cold-start treats the candidate fallback
    as safe (agy is up in this pane; restricted-/proc one-agy-pod).
    """
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda _pid: 300)
    monkeypatch.setattr(rpc, "discover_language_server_port", lambda _pid: None)
    state = rpc.resolve_pane_agy_rpc_port_state(Path("/tmp/agy/tmux.sock"), "main")
    assert state == rpc.PaneAgyResolution(agy_found=True, port=None)


def test_resolve_pane_state_no_agy_when_no_pane_pid(monkeypatch: pytest.MonkeyPatch) -> None:
    """State 3 (no pane pid): ``agy_found`` is ``False`` so the caller keeps polling."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: None)
    monkeypatch.setattr(
        rpc,
        "_agy_pid_in_pane_subtree",
        lambda _pid: (_ for _ in ()).throw(AssertionError("unreachable")),
    )
    state = rpc.resolve_pane_agy_rpc_port_state(Path("/tmp/agy/tmux.sock"), "main")
    assert state == rpc.PaneAgyResolution(agy_found=False, port=None)


def test_resolve_pane_state_no_agy_when_not_in_subtree(monkeypatch: pytest.MonkeyPatch) -> None:
    """State 3 (agy not exec'd yet): ``agy_found`` is ``False`` (CLI early-poll)."""
    monkeypatch.setattr(rpc, "_pane_pid", lambda socket_path, target: 100)
    monkeypatch.setattr(rpc, "_agy_pid_in_pane_subtree", lambda _pid: None)
    monkeypatch.setattr(
        rpc,
        "discover_language_server_port",
        lambda _pid: (_ for _ in ()).throw(AssertionError("unreachable")),
    )
    state = rpc.resolve_pane_agy_rpc_port_state(Path("/tmp/agy/tmux.sock"), "main")
    assert state == rpc.PaneAgyResolution(agy_found=False, port=None)


# ---------------------------------------------------------------------------
# resolve_cold_start_agy_rpc_port (3-state dispatch)
# ---------------------------------------------------------------------------


def test_cold_start_port_uses_scoped_when_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """State 1: a resolved pane-scoped port wins over a lower foreign candidate."""
    monkeypatch.setattr(
        rpc,
        "resolve_pane_agy_rpc_port_state",
        lambda _sock, _tgt: rpc.PaneAgyResolution(agy_found=True, port=61000),
    )
    monkeypatch.setattr(
        rpc,
        "_candidate_agy_rpc_ports",
        lambda: (_ for _ in ()).throw(AssertionError("scoped port resolved; no candidate scan")),
    )
    assert rpc.resolve_cold_start_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") == 61000


def test_cold_start_port_keeps_polling_when_no_agy_in_pane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State 3 (THE FIX): pane present, NO agy yet, FOREIGN candidate up → None.

    The CLI ``tmux_start_on_attach`` early-poll window: our agy is not ``exec``-ed
    yet. A foreign agy is the only candidate (52548). The resolver MUST return
    ``None`` (keep polling) and NOT bind the foreign candidate — the durable
    cross-bind this whole change prevents.
    """
    monkeypatch.setattr(
        rpc,
        "resolve_pane_agy_rpc_port_state",
        lambda _sock, _tgt: rpc.PaneAgyResolution(agy_found=False, port=None),
    )
    monkeypatch.setattr(
        rpc,
        "_candidate_agy_rpc_ports",
        lambda: (_ for _ in ()).throw(
            AssertionError("must NOT consult candidates when our agy is not up yet")
        ),
    )
    assert rpc.resolve_cold_start_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") is None


def test_cold_start_port_falls_back_when_agy_found_but_no_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """State 2: our agy IS up in the pane but its port is not lsof-attributable.

    Restricted-/proc one-agy-per-pod: agy exists here so the lone candidate is
    ours — the candidate fallback is safe and necessary.
    """
    monkeypatch.setattr(
        rpc,
        "resolve_pane_agy_rpc_port_state",
        lambda _sock, _tgt: rpc.PaneAgyResolution(agy_found=True, port=None),
    )
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548])
    assert rpc.resolve_cold_start_agy_rpc_port(Path("/tmp/agy/tmux.sock"), "main") == 52548


def test_cold_start_port_no_pane_uses_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    """No pane supplied (remote runner) → lowest candidate; pane resolver untouched."""
    monkeypatch.setattr(
        rpc,
        "resolve_pane_agy_rpc_port_state",
        lambda _sock, _tgt: (_ for _ in ()).throw(
            AssertionError("no pane → pane resolver must not be consulted")
        ),
    )
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", lambda: [52548, 61000])
    assert rpc.resolve_cold_start_agy_rpc_port(None, None) == 52548


def test_cold_start_port_no_pane_none_when_no_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No pane and no candidates yet → ``None`` (keep polling)."""
    monkeypatch.setattr(rpc, "_candidate_agy_rpc_ports", list)
    assert rpc.resolve_cold_start_agy_rpc_port(None, None) is None
