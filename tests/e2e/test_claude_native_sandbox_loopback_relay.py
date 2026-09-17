"""E2E regression test: claude-native hook relay bind address under a hardened sandbox.

Reproduces the user-reported bug: a ``claude-native`` session running inside a
sandbox backend whose SSRF hardening **unconditionally denies loopback / link-
local / metadata destinations** (OpenShell here, but a standard hardened-sandbox
posture) fails every prompt with a ``policy_denied`` error, because
``omnigent/harnesses/claude_native/bridge.py``'s internal Claude Code hook relay
(``start_tool_relay`` / ``_start_http_ingress``) hardcodes ``127.0.0.1`` as its
bind address. The relay is therefore advertised at ``http://127.0.0.1:<port>``,
so the ``UserPromptSubmit`` policy hook's ``POST /policies/evaluate`` targets a
loopback address the sandbox blocks before its own policy engine is even
consulted. ``post_evaluate_with_retry`` sees the sandbox's ``403`` as a final
4xx and ``_main_evaluate_policy`` converts it into a fail-closed block, so the
prompt is rejected before reaching the model:

    ● UserPromptSubmit operation blocked by hook:
      Omnigent policy evaluation unavailable (could not reach or authenticate to
      the Omnigent server); failing closed for this request.
      Detail: server returned 403: {"detail":"POST 127.0.0.1:38967/policies/evaluate
      not permitted by policy","error":"policy_denied"}

## What this test drives (the real journey, not a code-path poke)

It stands up the REAL components a claude-native prompt submit flows through:

* a real ``omnigent server`` subprocess (the policy authority the relay proxies
  ``/policies/evaluate`` to),
* the production relay started **exactly as the runner starts it** —
  ``prepare_bridge_dir`` + ``start_tool_relay(policy_client=..., session_id=...)``
  (see ``_ensure_comment_relay_started`` in ``omnigent/runner/app.py``),
* the REAL hook subprocess Claude Code spawns on every prompt submit —
  ``python -m omnigent.harnesses.claude_native.hook evaluate-policy`` — reading
  its target from the relay's own ``tool_relay.json``.

The sandbox's loopback-filtering network layer is emulated by an in-process
forward proxy (``_SsrfHardenedProxy``) standing in for the OpenShell sandbox we
cannot provision in CI: it denies loopback / link-local / unspecified / metadata
destinations with OpenShell's exact ``policy_denied`` body and status, and
forwards routable destinations untouched — the two behaviours the ticket's OCSF
log documents. This is a **stand-in** for the reported environment (a real
OpenShell-managed ``omnigent-host``); the mechanism it exercises — the hardcoded
loopback relay bind — is the genuine product defect.

## Polarity (fail now → pass after the fix)

The prompt submit is driven through the SSRF-hardened proxy and the test asserts
the DESIRED behaviour: a normal prompt must **not** be fail-closed-blocked merely
because the relay was bound to an SSRF-blocked loopback address. On the buggy
build the relay advertises ``127.0.0.1`` → the proxy denies the hook's POST →
the hook blocks → **this test FAILS, reproducing the bug**. Once the relay binds
a routable, policy-evaluable address (the ticket's suggested fix), the proxy
forwards the POST, the server returns a real verdict, and the prompt proceeds —
the test passes. A control leg proves the same prompt is allowed with no proxy in
the way, isolating the loopback bind (not the request shape) as the cause.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_sandbox_loopback_relay.py -v
"""

from __future__ import annotations

import asyncio
import io
import ipaddress
import json
import os
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call in this test targets loopback; CI shells often carry an egress
# proxy, so bypass proxy autodetection for the test's own client entirely.
_http = httpx.Client(trust_env=False)

# The server subprocess imports omnigent + the bundled SDKs; in a worktree they
# resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5
# The hook's own retry ladder (fail-closed after the budget) plus the proxy
# round trip; generous for CI.
_HOOK_TIMEOUT_S = 180.0

# The prior Claude session id the hook stamps onto its evaluation request.
_EXTERNAL_SID = "11111111-2222-4333-8444-555566667777"


def _find_free_port() -> int:
    """Grab an ephemeral loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no ambient proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for a spawned process.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError as exc:  # server still booting
            last = repr(exc)
        time.sleep(_POLL_S)
    raise AssertionError(f"server never became healthy at {url}; last: {last}")


# ---------------------------------------------------------------------------
# The sandbox stand-in: a forward proxy applying OpenShell-style SSRF hardening.
# ---------------------------------------------------------------------------
def _is_always_blocked(host: str) -> bool:
    """Whether *host* is in the class a hardened sandbox blocks unconditionally.

    Loopback / link-local / unspecified / cloud-metadata — the exact class the
    ticket's OCSF log names ("SSRF hardening — loopback/link-local/unspecified/
    metadata"), which OpenShell will not even *offer* to add to policy.

    :param host: Destination host from the proxied request line.
    :returns: ``True`` when the destination is unconditionally blocked.
    """
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host in ("localhost", "metadata.google.internal")
    return ip.is_loopback or ip.is_link_local or ip.is_unspecified or host == "169.254.169.254"


class _SsrfHardenedProxy:
    """An in-process forward proxy standing in for the OpenShell sandbox layer.

    Denies loopback/link-local/metadata destinations with OpenShell's exact
    ``403 {"error":"policy_denied"}`` body; forwards everything else untouched.
    Records each denial so the test can surface the reproduced OCSF signature.
    """

    def __init__(self) -> None:
        self.denials: list[str] = []
        self.forwards: list[str] = []
        proxy = self

        class _Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                target = urlsplit(self.path)  # absolute-form (proxy) request line
                host = target.hostname or ""
                port = target.port or 80
                if _is_always_blocked(host):
                    body = json.dumps(
                        {
                            "detail": f"{self.command} {host}:{port}{target.path} "
                            "not permitted by policy",
                            "error": "policy_denied",
                        }
                    ).encode()
                    proxy.denials.append(
                        f"[ocsf] HTTP:{self.command} [MED] DENIED -> "
                        f"{self.command} {self.path} [reason:endpoint {host}:{port} "
                        "is not allowed by any policy] | [sandbox] Skipped proposal "
                        "for always-blocked destination (SSRF hardening — "
                        f"loopback/link-local/unspecified/metadata) host={host} port={port}"
                    )
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # Routable destination: normal policy-evaluable traffic -> forward.
                #
                # A real OpenShell-managed sandbox routes the host's routable IP
                # to the relay socket. This CI netns has ONLY loopback, so we
                # model that routing by rewriting the (non-blocked) advertised
                # host to the loopback the relay socket actually lives on,
                # preserving the advertised port. The buggy build never reaches
                # this branch (it advertises 127.0.0.1, which is denied above);
                # any fix that advertises a non-SSRF-blocked host on the relay's
                # real port is forwarded here and reaches the policy server.
                fwd_url = f"http://127.0.0.1:{port}{target.path}"
                if target.query:
                    fwd_url += f"?{target.query}"
                proxy.forwards.append(f"{self.command} {self.path} -> {fwd_url}")
                length = int(self.headers.get("Content-Length") or 0)
                payload = self.rfile.read(length) if length else b""
                fwd_headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower() not in ("host", "proxy-connection", "content-length")
                }
                with httpx.Client(trust_env=False, timeout=_HOOK_TIMEOUT_S) as client:
                    upstream = client.request(
                        self.command, fwd_url, content=payload, headers=fwd_headers
                    )
                self.send_response(upstream.status_code)
                for k, v in upstream.headers.items():
                    if k.lower() in ("content-length", "transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(upstream.content)))
                self.end_headers()
                self.wfile.write(upstream.content)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

            def log_message(self, *args: object) -> None:  # silence access log
                del args

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        host, port = self._httpd.server_address[0], self._httpd.server_address[1]
        return f"http://{host}:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
def _start_server(tmp_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    """Spawn a real ``omnigent server`` on a free loopback port; return it + base URL."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'db.sqlite'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        env=_localhost_env({}),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
    return proc, base_url


def _create_session(base_url: str) -> str:
    """Register a minimal inline agent and return its session id.

    A session row is all the relay's ``/policies/evaluate`` proxy needs: it is
    the sole enforcement point the ``UserPromptSubmit`` hook reaches, so the
    journey is exercised without booting a Claude CLI or a runner.
    """
    cfg = {
        "name": "relay-bind-repro",
        "prompt": "You are a test agent.",
        "executor": {"harness": "openai-agents", "model": "gpt-4o-mini"},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(cfg).encode()
        info = tarfile.TarInfo("relay-bind-repro.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def _run_prompt_submit_hook(
    bridge_dir: Path, extra_env: dict[str, str]
) -> subprocess.CompletedProcess[bytes]:
    """Run the REAL ``UserPromptSubmit`` hook subprocess Claude Code spawns.

    :param bridge_dir: The relay's bridge directory (holds ``tool_relay.json``).
    :param extra_env: Env overrides (e.g. the SSRF-proxy ``HTTP_PROXY``).
    :returns: The completed hook process (its stdout is the harness verdict).
    """
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": "hello from the sandbox",
            "session_id": _EXTERNAL_SID,
            "cwd": str(bridge_dir),
        }
    ).encode()
    env = _localhost_env(extra_env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent.harnesses.claude_native.hook",
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ],
        input=payload,
        capture_output=True,
        timeout=_HOOK_TIMEOUT_S,
        env=env,
    )


def _decision(hook_stdout: bytes) -> dict[str, object]:
    """Parse the hook's JSON stdout ("" = no opinion / allow)."""
    text = hook_stdout.decode().strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def test_claude_native_prompt_survives_loopback_filtering_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt submit must not fail-closed once the sandbox opt-in is set.

    Models the documented remediation: an SSRF-hardened sandbox integrator
    sets ``OMNIGENT_BRIDGE_BIND_HOST=0.0.0.0`` so the relay advertises the
    host's routable address. Fails on the buggy build (the relay ignored the
    setting and advertised ``127.0.0.1`` → the sandbox stand-in denies the
    hook's POST → the prompt is blocked); passes once the relay honours the
    opt-in and advertises a routable, policy-evaluable address the sandbox
    forwards.
    """
    from omnigent.harnesses.claude_native.bridge import (
        prepare_bridge_dir,
        start_tool_relay,
        write_active_session_id,
    )

    # The sandbox integrator's opt-in: bind all interfaces and advertise the
    # host's real routable address. The SSRF stand-in forwards that routable
    # destination (rewriting it to the relay's loopback socket, modelling how
    # a real sandbox routes it), while the control leg reaches it directly.
    monkeypatch.setenv("OMNIGENT_BRIDGE_BIND_HOST", "0.0.0.0")
    monkeypatch.delenv("OMNIGENT_BRIDGE_PORT_POOL", raising=False)

    proxy = _SsrfHardenedProxy()
    server_proc: subprocess.Popen[bytes] | None = None
    relay = None
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    policy_client = httpx.AsyncClient(trust_env=False)
    try:
        server_proc, base_url = _start_server(tmp_path)
        session_id = _create_session(base_url)
        policy_client.base_url = httpx.URL(base_url)

        # Start the relay EXACTLY as the runner does (policy_client + session_id
        # → it proxies POST /policies/evaluate to the real server).
        bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)

        async def _noop_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
            del name, arguments
            return {}

        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=_noop_tool,
            loop=loop,
            policy_client=policy_client,
            session_id=session_id,
        )
        write_active_session_id(bridge_dir, session_id)

        relay_info = json.loads((bridge_dir / "tool_relay.json").read_text())
        relay_host = urlsplit(relay_info["url"]).hostname or ""

        # --- Control leg: same prompt, no sandbox filter -> chain is healthy. ---
        control = _run_prompt_submit_hook(bridge_dir, extra_env={})
        control_decision = _decision(control.stdout)
        assert control_decision.get("decision") != "block", (
            "Control leg (no SSRF filter) unexpectedly blocked the prompt — the "
            "server/relay chain is unhealthy, so the filtered leg would prove "
            f"nothing. hook stdout={control.stdout.decode()!r} "
            f"stderr={control.stderr.decode()!r}"
        )

        # --- The bug leg: same prompt inside the loopback-filtering sandbox. ---
        filtered = _run_prompt_submit_hook(
            bridge_dir,
            extra_env={
                "HTTP_PROXY": proxy.url,
                "http_proxy": proxy.url,
                # The whole point: the sandbox filters loopback too, so the
                # relay's 127.0.0.1 target is NOT proxy-bypassed.
                "NO_PROXY": "",
                "no_proxy": "",
            },
        )
        decision = _decision(filtered.stdout)

        assert decision.get("decision") != "block", (
            "Bug reproduced: the claude-native hook relay bound to a "
            f"loopback address ({relay_host}) — advertised as {relay_info['url']} — "
            "so inside a sandbox that filters loopback the UserPromptSubmit hook's "
            "POST /policies/evaluate is denied and the prompt is blocked fail-closed. "
            f"hook decision={decision!r}\n"
            f"hook stderr={filtered.stderr.decode().strip()!r}\n"
            f"sandbox denials={proxy.denials!r}"
        )
    finally:
        if relay is not None:
            relay.close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        asyncio.run(policy_client.aclose())
        _terminate(server_proc)
        proxy.close()
