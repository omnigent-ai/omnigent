"""Live agy 1.2.x connect-RPC CSRF regression e2e.

Launches the REAL ``agy`` CLI in a private tmux pane using the native
Antigravity harness's own launch assembly
(:func:`omnigent.harnesses.antigravity_native.launch.build_agy_launch`), then
drives omnigent's own connect-RPC client
(:mod:`omnigent.harnesses.antigravity_native.rpc`) against it.

The bug: agy >= 1.2 gates its local connect-RPC endpoint behind a CSRF token.
A client that does not know the token gets
``401 {"code":"unauthenticated","message":"missing CSRF token"}`` on every
method. Because that 401 is at the transport/auth layer, it happens before any
Google sign-in or turn, and it starves the whole native path: ``_heartbeat_ok``
fails, port discovery finds nothing, and the runner cold-start swallows the 401
as an empty model catalog, times out, and leaves the ``agy_conv_*`` placeholder
— so the RPC reader never binds the real cascade and a WAITING permission
prompt never surfaces as an elicitation in the web UI.

The fixed behaviour: the launcher seeds agy with omnigent's shared CSRF token
(hidden ``--csrf_token`` flag) and every RPC echoes it back, so the probes and
calls below succeed. On an unfixed tree the launch assembly emits a bare agy
argv and the client sends no token, so these tests FAIL with the CSRF 401; on a
fixed tree they PASS. They key on the CSRF signature — not on a populated
catalog — so they stay valid without Google sign-in: Heartbeat is an
unauthenticated liveness probe, and a post-fix auth-required error would not
carry a CSRF message.

Prerequisites (else skipped):
    - ``agy`` on PATH at version >= 1.2 (the versions that added the CSRF gate).
    - ``tmux`` on PATH.

Usage::

    python -m pytest tests/e2e/test_antigravity_native_agy_csrf_e2e.py -v --timeout=300
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.antigravity_native import rpc
from omnigent.harnesses.antigravity_native.launch import agy_binary_path, build_agy_launch

_CSRF_MARKER = "csrf token"
_PORT_BIND_TIMEOUT_S = 90.0


def _agy_path_or_skip() -> str:
    try:
        return agy_binary_path()
    except RuntimeError:
        pytest.skip("agy CLI not installed; native Antigravity CSRF e2e cannot run")


def _agy_supports_csrf_gate_or_skip(agy: str) -> None:
    """Skip unless agy is >= 1.2 (the versions carrying the connect-RPC CSRF gate)."""
    try:
        out = subprocess.run([agy, "--version"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        pytest.skip(f"could not read agy version: {exc}")
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out)
    if not m:
        pytest.skip(f"could not parse agy version from {out!r}")
    major, minor = int(m.group(1)), int(m.group(2))
    if (major, minor) < (1, 2):
        pytest.skip(f"agy {major}.{minor} predates the connect-RPC CSRF gate; bug N/A")


def _tls_rpc_port(agy_pid: int) -> int | None:
    """Return the agy pid's TLS connect-RPC port, independent of the CSRF gate.

    Cannot use ``_heartbeat_ok``: that gates on HTTP 200, which the CSRF bug
    denies. The TLS connect-RPC port is instead the loopback listener that
    answers omnigent's ``verify=False`` client with an HTTP response at all; the
    sibling plain-HTTP port fails the TLS handshake (``ConnectError``).
    """
    for port in rpc._pid_listen_ports(agy_pid):
        url = rpc._rpc_url(port, rpc._METHOD_HEARTBEAT)
        try:
            with rpc._sync_client(5.0) as client:
                client.post(url, headers={"Content-Type": "application/json"}, content=b"{}")
        except httpx.HTTPError:
            continue
        return port
    return None


@pytest.fixture(scope="module")
def live_agy_rpc_port() -> Iterator[int]:
    """Launch a real agy exactly as the harness does; yield its connect-RPC port.

    The argv comes from the harness's own :func:`build_agy_launch`, so the
    launch matches the product path on both trees: a fixed tree seeds agy with
    omnigent's CSRF token, an unfixed tree launches the bare binary. ``$HOME``
    is redirected to a scratch dir for the whole module so agy stays off any
    real ``~/.gemini`` credential and the shared token file the launcher mints
    never touches the developer's real ``~/.omnigent``.
    """
    agy = _agy_path_or_skip()
    _agy_supports_csrf_gate_or_skip(agy)
    if shutil.which("tmux") is None:
        pytest.skip("tmux not on PATH; native Antigravity terminal cannot be launched")

    scratch = Path(tempfile.mkdtemp(prefix="agycsrf-", dir="/tmp"))
    sock = scratch / "tmux.sock"
    home_patch = pytest.MonkeyPatch()
    agy_pid: int | None = None
    port: int | None = None
    try:
        home_patch.setenv("HOME", str(scratch))
        argv, _env_overrides = build_agy_launch(conversation_id=None, model=None, resume=False)
        subprocess.run(
            [
                "tmux",
                "-S",
                str(sock),
                "new-session",
                "-d",
                "-s",
                "main",
                "-x",
                "200",
                "-y",
                "50",
                f"env HOME={shlex.quote(str(scratch))} {shlex.join(argv)}",
            ],
            check=True,
            timeout=30,
        )

        deadline = time.monotonic() + _PORT_BIND_TIMEOUT_S
        while time.monotonic() < deadline:
            pane_pid = rpc._pane_pid(sock, "main")
            if pane_pid is not None:
                agy_pid = rpc._agy_pid_in_pane_subtree(pane_pid)
                if agy_pid is not None:
                    port = _tls_rpc_port(agy_pid)
                    if port is not None:
                        break
            time.sleep(2)
        if port is None:
            pytest.skip("agy did not bind a loopback connect-RPC port in time")
        yield port
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], check=False, timeout=15)
        if agy_pid is not None:
            subprocess.run(["kill", "-TERM", str(agy_pid)], check=False, timeout=10)
        home_patch.undo()
        shutil.rmtree(scratch, ignore_errors=True)


def test_heartbeat_probe_succeeds(live_agy_rpc_port: int) -> None:
    """The canonical port-liveness probe must pass against a live agy."""
    assert rpc._heartbeat_ok(live_agy_rpc_port) is True, (
        "rpc._heartbeat_ok() is False against a live agy — the CSRF gate makes "
        "Heartbeat 401, so port discovery and cold-start can never find agy"
    )


def test_cold_start_port_discovery_finds_live_agy(live_agy_rpc_port: int) -> None:
    """The cold-start's Heartbeat-gated port scan must include the live agy port."""
    candidates = rpc._candidate_agy_rpc_ports()
    assert live_agy_rpc_port in candidates, (
        f"live agy port {live_agy_rpc_port} not in candidate RPC ports {candidates}; "
        "a CSRF 401 fails the Heartbeat==200 gate, so _cold_start_agy_conversation "
        "never resolves a port and leaves the agy_conv_* placeholder"
    )


def test_get_available_models_not_rejected_over_csrf(live_agy_rpc_port: int) -> None:
    """get_available_models (the call cold-start makes) must clear the CSRF gate.

    Keyed on the CSRF signature only: a post-fix auth-required error without
    Google sign-in would be acceptable; a 401 naming the CSRF token (missing OR
    invalid) is the bug.
    """
    try:
        rpc.get_available_models(live_agy_rpc_port)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text.lower()
        assert not (exc.response.status_code == 401 and _CSRF_MARKER in body), (
            "cold-start's get_available_models() was rejected over the CSRF token "
            f"(HTTP {exc.response.status_code}: {exc.response.text!r}); this 401 is "
            "swallowed as catalog={} so cold-start times out and never mints agy's cascade"
        )
