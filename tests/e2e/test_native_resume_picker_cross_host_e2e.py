"""E2E regression tests: bare native ``--resume`` picker vs. other hosts.

Guards the journey where bare ``omnigent claude --server <url> --resume``
against a remote server offered wrapper sessions bound to OTHER hosts,
even though native transcript / workspace state is host-local, so
picking one is a dead end. Two mechanisms underneath, each guarded here:

* the Python SDK's ``SessionListItem`` drops the server's ``host_id``
  field, so the picker cannot filter by host authoritatively; and
* a transient 429 from ``GET /v1/sessions`` escapes the picker as a raw
  SDK traceback instead of bounded retries / a concise CLI error.

The tests drive the REAL user journey: a real ``omnigent server``, two
real host daemons registered as two different hosts (separate ``HOME``\\ s),
one claude-native wrapper session bound to each host through the same
``POST /v1/hosts/{id}/runners`` route the product uses, then the real
``omnigent claude --server <url> --resume`` CLI run as host A with the
picker's line-buffered stdin fallback (``q`` cancels).

Expected behavior (the fix contract):

* session-list rows preserve ``host_id``;
* bare native ``--resume`` filters by wrapper AND invoking host;
* a transient list 429 gets short bounded retries, then a concise CLI
  error — never a raw traceback. Explicit ``--resume <id>`` unchanged.

The suite is hermetic: it boots its own server (dummy LLM key — no turn
ever runs), needs no Claude login (the picker is reached and cancelled
before any TUI launches; ``claude`` / ``tmux`` preflight is satisfied
with stub executables), and cleans up all daemons.
"""

from __future__ import annotations

import io
import json
import os
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

# Worktree root: tests/e2e/<this file> -> parents[2]. Threaded onto every
# subprocess PYTHONPATH so the spawned server / daemons / CLI import THIS
# worktree's code, not a stale editable install.
_REPO_ROOT = Path(__file__).resolve().parents[2]

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 60.0
_CLI_TIMEOUT_S = 240.0

# Env vars that, leaked from a parent omnigent process (or the CI proxy
# env), would mis-route the spawned server / daemons / CLI.
_STRIPPED_ENV_VARS = (
    "http_proxy",
    "https_proxy",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "all_proxy",
    "RUNNER_SERVER_URL",
)
# Whole families stripped by prefix: runner/host identity and provider
# credentials leaked from the parent process would mis-route or
# de-hermeticize the spawned server / daemons / CLI.
_STRIPPED_ENV_PREFIXES = (
    "OMNIGENT_RUNNER_",
    "OMNIGENT_HOST_",
    "OMNIGENT_REMOTE_AUTH_",
    "ANTHROPIC_",
    "DATABRICKS_",
)


def _subprocess_env(home: Path | None = None) -> dict[str, str]:
    """Build a clean environment for a spawned server / daemon / CLI.

    :param home: Per-role ``HOME`` (isolates ``~/.omnigent`` host identity
        and daemon records), or ``None`` to keep the test process home.
    :returns: Environment dict for ``subprocess``.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _STRIPPED_ENV_VARS and not k.startswith(_STRIPPED_ENV_PREFIXES)
    }
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    extra = (
        _REPO_ROOT,
        _REPO_ROOT / "sdks" / "python-client",
        _REPO_ROOT / "sdks" / "ui",
    )
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [*(str(p) for p in extra), *([existing] if existing else [])]
    )
    if home is not None:
        env["HOME"] = str(home)
        # The host daemon's lifecycle lock is keyed by data_dir()/daemons,
        # and data_dir() prefers OMNIGENT_DATA_DIR over $HOME. The test
        # session sets one shared OMNIGENT_DATA_DIR (see tests/conftest.py),
        # so without a per-role override both daemons — same server URL —
        # collide on one lock and the second exits before coming online.
        # Pin the data dir under this role's HOME to match its identity.
        env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    return env


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _client() -> httpx.Client:
    # trust_env=False: never route loopback through the CI egress proxy.
    return httpx.Client(trust_env=False, timeout=30.0)


def _python() -> str:
    import sys

    return sys.executable


def _wrapper_bundle() -> bytes:
    """Bundle the exact terminal-first spec ``omnigent claude`` ships."""
    from omnigent.claude_native import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat
        # translator (the spec has no spec_version) — same convention as
        # the ``_create_native_claude_session`` e2e_ui fixture.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _create_wrapper_session(server_url: str, title: str) -> str:
    """Create a claude-native wrapper session (labels as the CLI stamps)."""
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    with _client() as c:
        resp = c.post(
            f"{server_url}/v1/sessions",
            data={"metadata": json.dumps({"labels": labels})},
            files={"bundle": ("claude-native-ui.tar.gz", _wrapper_bundle(), "application/gzip")},
            timeout=120.0,
        )
        resp.raise_for_status()
        session_id = str(resp.json()["session_id"])
        # A distinctive title makes the picker rows (and any failure
        # output) self-describing.
        c.patch(f"{server_url}/v1/sessions/{session_id}", json={"title": title})
    return session_id


@dataclass
class _CrossHostEnv:
    """Booted server + two registered hosts, one wrapper session each."""

    server_url: str
    host_a_id: str
    host_b_id: str
    home_a: Path
    home_b: Path
    session_a: str  # bound to host A
    session_b: str  # bound to host B
    stub_bin: Path  # PATH dir with `claude` / `tmux` stubs for preflight
    procs: list[subprocess.Popen]


def _boot_server(root: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    (root / "server").mkdir(parents=True, exist_ok=True)
    log = open(root / "server" / "server.log", "w")  # noqa: SIM115 — Popen lifetime
    env = _subprocess_env()
    # Dummy key: sessions are created but no turn ever runs.
    env["OPENAI_API_KEY"] = "dummy"
    proc = subprocess.Popen(
        [
            _python(),
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{root}/server/db.sqlite",
            "--artifact-location",
            str(root / "server" / "artifacts"),
        ],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(root),
    )
    with _client() as c:
        deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "server died:\n" + (root / "server" / "server.log").read_text()[-4000:]
                )
            try:
                if c.get(f"{url}/health").status_code == 200:
                    return proc, url
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(
        "server never healthy:\n" + (root / "server" / "server.log").read_text()[-4000:]
    )


def _spawn_host(root: Path, name: str, server_url: str) -> tuple[subprocess.Popen, str, Path]:
    """Register a real host daemon under an isolated ``HOME``."""
    home = root / f"home-{name}"
    (home / ".omnigent").mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": f"picker-e2e-{name}"}})
    )
    log = open(root / f"host-{name}.log", "w")  # noqa: SIM115 — Popen lifetime
    proc = subprocess.Popen(
        [_python(), "-m", "omnigent.host._daemon_entry", "--server", server_url],
        env=_subprocess_env(home),
        stdout=log,
        stderr=subprocess.STDOUT,
        cwd=str(home),
    )
    with _client() as c:
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                r = c.get(f"{server_url}/v1/hosts/{host_id}")
                if r.status_code == 200 and r.json().get("status") == "online":
                    return proc, host_id, home
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
    raise RuntimeError(
        f"host {name} never online:\n" + (root / f"host-{name}.log").read_text()[-4000:]
    )


def _bind_to_host(server_url: str, session_id: str, host_id: str, workspace: Path) -> None:
    """Bind the session to the host — persists ``host_id`` on the row."""
    workspace.mkdir(parents=True, exist_ok=True)
    with _client() as c:
        resp = c.post(
            f"{server_url}/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(workspace)},
            timeout=90.0,
        )
        resp.raise_for_status()


def _write_stub_bin(root: Path) -> Path:
    """Stub ``claude`` / ``tmux`` so the CLI's preflight passes.

    The picker journey cancels before any TUI launches, so the stubs are
    only ever ``shutil.which``-probed — this keeps the test independent of
    a Claude login / tmux install.
    """
    stub_bin = root / "stub-bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    for name in ("claude", "tmux"):
        stub = stub_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub_bin


def _run_resume_picker_cli(
    server_url: str, env_home: Path, stub_bin: Path
) -> subprocess.CompletedProcess:
    """Run the real ``omnigent claude --server <url> --resume`` as one host.

    stdin is a pipe, so the picker takes its line-buffered fallback; ``q``
    cancels at the first rendered page. The rendered page (rows and ids)
    goes to stderr.
    """
    env = _subprocess_env(env_home)
    env["PATH"] = f"{stub_bin}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        [_python(), "-m", "omnigent.cli", "claude", "--server", server_url, "--resume"],
        env=env,
        input="q\n",
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_S,
        cwd=str(env_home),
    )


@pytest.fixture(scope="module")
def cross_host_env(tmp_path_factory: pytest.TempPathFactory) -> _CrossHostEnv:
    """Server + host A + host B, one claude-native session bound to each."""
    root = tmp_path_factory.mktemp("cross_host_picker")
    procs: list[subprocess.Popen] = []
    try:
        server_proc, url = _boot_server(root)
        procs.append(server_proc)
        host_a_proc, host_a, home_a = _spawn_host(root, "hostA", url)
        procs.append(host_a_proc)
        host_b_proc, host_b, home_b = _spawn_host(root, "hostB", url)
        procs.append(host_b_proc)

        session_a = _create_wrapper_session(url, "session-on-host-A")
        _bind_to_host(url, session_a, host_a, home_a / "wsA")
        session_b = _create_wrapper_session(url, "session-on-host-B")
        _bind_to_host(url, session_b, host_b, home_b / "wsB")

        env = _CrossHostEnv(
            server_url=url,
            host_a_id=host_a,
            host_b_id=host_b,
            home_a=home_a,
            home_b=home_b,
            session_a=session_a,
            session_b=session_b,
            stub_bin=_write_stub_bin(root),
            procs=procs,
        )
        yield env
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()


def test_sdk_session_list_preserves_host_id(cross_host_env: _CrossHostEnv) -> None:
    """``client.sessions.list`` rows must carry the server's ``host_id``.

    The server's ``GET /v1/sessions`` returns ``host_id`` per row (it is
    part of ``omnigent.server.schemas.SessionListItem``); the SDK model
    must not drop it — it is the only authoritative signal the native
    resume picker has for host-local filtering.
    """
    import asyncio

    from omnigent_client import OmnigentClient

    url = cross_host_env.server_url

    async def _list():
        async with OmnigentClient(base_url=url) as client:
            return await client.sessions.list(limit=50, order="desc")

    rows = asyncio.run(_list())
    with _client() as c:
        raw = {
            row["id"]: row.get("host_id")
            for row in c.get(f"{url}/v1/sessions", params={"limit": 50}).json()["data"]
        }
    assert raw[cross_host_env.session_a] == cross_host_env.host_a_id
    assert raw[cross_host_env.session_b] == cross_host_env.host_b_id

    by_id = {row.id: row for row in rows}
    for session_id in (cross_host_env.session_a, cross_host_env.session_b):
        row = by_id[session_id]
        got = getattr(row, "host_id", None)
        assert got == raw[session_id], (
            f"SDK SessionListItem for {session_id} dropped host_id "
            f"(server said {raw[session_id]!r}, SDK exposed {got!r}) — the "
            "native resume picker cannot filter by host without it."
        )


def test_bare_resume_picker_excludes_other_hosts_sessions(
    cross_host_env: _CrossHostEnv,
) -> None:
    """Bare ``--resume`` on host A must not offer host B's session.

    Native transcript and workspace state are host-local, so a row bound
    to another host is a dead end in this picker. The picker page renders
    each row's conversation id, so asserting on ids is exact.
    """
    cp = _run_resume_picker_cli(
        cross_host_env.server_url, cross_host_env.home_a, cross_host_env.stub_bin
    )
    out = cp.stdout + "\n" + cp.stderr
    assert "Traceback (most recent call last)" not in out, (
        f"CLI crashed instead of showing the picker:\n{out[-4000:]}"
    )
    assert cross_host_env.session_a in out, (
        "picker never rendered host A's own session — the journey did not "
        f"reach the picker page:\n{out[-4000:]}"
    )
    assert cross_host_env.session_b not in out, (
        "bare --resume picker on host A offered a session bound to host B "
        f"({cross_host_env.session_b} / host {cross_host_env.host_b_id}); "
        "native resume state is host-local, so cross-host rows are dead "
        f"ends and must be filtered out:\n{out[-4000:]}"
    )


class _Flaky429Proxy(ThreadingHTTPServer):
    """Reverse proxy that 429s ``GET /v1/sessions`` a fixed number of times.

    Everything else (health, hosts, daemon tunnel bootstrap HTTP) is
    relayed to the real server, so the CLI journey is untouched except
    for the injected transient rate-limit — the fault named in the bug.
    """

    daemon_threads = True

    def __init__(self, upstream: str, fail_times: int):
        self.upstream = upstream
        self.remaining_429 = fail_times
        self.lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), _ProxyHandler)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence request logging
        pass

    def _relay(self) -> None:
        server: _Flaky429Proxy = self.server  # type: ignore[assignment]
        if self.command == "GET" and self.path.split("?")[0] == "/v1/sessions":
            with server.lock:
                inject = server.remaining_429 > 0
                if inject:
                    server.remaining_429 -= 1
            if inject:
                body = json.dumps(
                    {"error": {"message": "rate limited", "code": "rate_limited"}}
                ).encode()
                self.send_response(429)
                self.send_header("Content-Type", "application/json")
                self.send_header("Retry-After", "1")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        length = int(self.headers.get("Content-Length") or 0)
        payload = self.rfile.read(length) if length else None
        headers = {
            k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")
        }
        with httpx.Client(trust_env=False, timeout=60.0) as c:
            upstream = c.request(
                self.command,
                f"{server.upstream}{self.path}",
                content=payload,
                headers=headers,
            )
        self.send_response(upstream.status_code)
        hop_by_hop = ("transfer-encoding", "connection", "content-encoding", "content-length")
        for k, v in upstream.headers.items():
            if k.lower() in hop_by_hop:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(upstream.content)))
        self.end_headers()
        self.wfile.write(upstream.content)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _relay


def test_transient_429_on_session_list_is_not_a_traceback(
    cross_host_env: _CrossHostEnv,
) -> None:
    """A single transient 429 on the picker's list call must be absorbed.

    The fix contract: short bounded retries, then (only if the 429
    persists) a concise CLI error. With a once-only 429 the retry
    succeeds, so the journey must reach the picker page — and a raw
    Python / SDK traceback must never surface either way.
    """
    proxy = _Flaky429Proxy(cross_host_env.server_url, fail_times=1)
    thread = threading.Thread(target=proxy.serve_forever, daemon=True)
    thread.start()
    try:
        cp = _run_resume_picker_cli(proxy.url, cross_host_env.home_a, cross_host_env.stub_bin)
        out = cp.stdout + "\n" + cp.stderr
        assert "Traceback (most recent call last)" not in out, (
            "a transient 429 from GET /v1/sessions escaped the resume "
            f"picker as a raw SDK traceback:\n{out[-5000:]}"
        )
        assert cross_host_env.session_a in out, (
            "the picker was never reached after a single transient 429 — "
            "the list call must be retried (bounded) before failing:\n"
            f"{out[-4000:]}"
        )
    finally:
        proxy.shutdown()
        thread.join(timeout=5)
