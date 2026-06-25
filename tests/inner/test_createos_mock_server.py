"""End-to-end tests for the createos provider against a mock control plane.

Unlike test_createos_os_env.py (which stubs `_Http` wholesale), these stand up
a real local HTTP server implementing the six createos endpoints and drive the
provider through its real `httpx`-based `_Http` client. This covers the paths
the unit tests skip: JSend unwrapping, `_poll_until_running`, the file
download/upload wire format, the exec envelope, bearer auth, and DELETE-on-close.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from omnigent.inner import createos_os_env as mod
from omnigent.inner.datamodel import OSEnvSpec
from omnigent.inner.os_env import create_os_environment


class _MockState:
    """Per-server scenario knobs + recorded traffic."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}
        self.exec_bodies: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.auth_seen: list[str] = []
        # Number of "pending" polls before a sandbox reports "running".
        self.pending_polls = 0
        self._poll_count = 0
        # exec result the mock returns (echo-the-command by default).
        self.exec_result: dict[str, Any] | None = None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # silence test server noise
        pass

    @property
    def state(self) -> _MockState:
        return self.server.state  # type: ignore[attr-defined]

    def _send_json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _jsend(self, data: Any, code: int = 200) -> None:
        self._send_json(code, {"status": "success", "data": data})

    def _parts(self) -> tuple[list[str], dict[str, list[str]]]:
        parsed = urlsplit(self.path)
        return parsed.path.strip("/").split("/"), parse_qs(parsed.query)

    def _record_auth(self) -> None:
        self.state.auth_seen.append(self.headers.get("Authorization", ""))

    def _read_body_bytes(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def do_POST(self) -> None:
        self._record_auth()
        parts, _ = self._parts()
        body = self._read_body_bytes()
        if parts == ["v1", "sandboxes"]:
            self._jsend({"id": "sbx_mock"})
            return
        if len(parts) == 4 and parts[:2] == ["v1", "sandboxes"] and parts[3] == "exec":
            payload = json.loads(body) if body else {}
            self.state.exec_bodies.append(payload)
            if self.state.exec_result is not None:
                result = self.state.exec_result
            else:
                # Echo the resolved command back as stdout (exit 0).
                cmd = " ".join(payload.get("args", []))
                result = {"stdout": cmd, "stderr": "", "exit_code": 0}
            self._jsend({"result": result, "exec_ms": 3})
            return
        self.send_error(404)

    def do_GET(self) -> None:
        self._record_auth()
        parts, query = self._parts()
        # GET /v1/sandboxes/:id  → status (poll)
        if len(parts) == 3 and parts[:2] == ["v1", "sandboxes"]:
            self.state._poll_count += 1
            status = "pending" if self.state._poll_count <= self.state.pending_polls else "running"
            self._jsend({"status": status})
            return
        # GET /v1/sandboxes/:id/files?path=…  → raw bytes
        if len(parts) == 4 and parts[3] == "files":
            path = query["path"][0]
            if path not in self.state.files:
                self.send_error(404)
                return
            data = self.state.files[path]
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        self._record_auth()
        parts, query = self._parts()
        if len(parts) == 4 and parts[3] == "files":
            self.state.files[query["path"][0]] = self._read_body_bytes()
            self._send_json(200, {"status": "success", "data": {}})
            return
        self.send_error(404)

    def do_DELETE(self) -> None:
        self._record_auth()
        parts, _ = self._parts()
        if len(parts) == 3 and parts[:2] == ["v1", "sandboxes"]:
            self.state.deleted.append(parts[2])
            self.send_response(204)
            self.end_headers()
            return
        self.send_error(404)


@pytest.fixture
def mock_createos() -> Iterator[tuple[str, _MockState]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.state = _MockState()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", server.state  # type: ignore[attr-defined]
    finally:
        server.shutdown()
        server.server_close()


def _make_env(base_url: str) -> mod.CreateosOSEnvironment:
    return create_os_environment(
        OSEnvSpec(
            type="createos",
            cwd="/root",
            createos_base_url=base_url,
            createos_api_key="sk-mock",
        )
    )  # type: ignore[return-value]


async def test_provision_file_roundtrip_and_auth(
    mock_createos: tuple[str, _MockState],
) -> None:
    """Real httpx path: provision, write/read/edit a file, bearer auth sent."""
    base_url, state = mock_createos
    env = _make_env(base_url)
    try:
        w = await env.write("notes.txt", "alpha\nbeta\n")
        assert w["ok"] is True
        assert state.files["/root/notes.txt"] == b"alpha\nbeta\n"

        r = await env.read("notes.txt")
        assert r["ok"] is True
        assert r["content"] == "alpha\nbeta\n"
        assert r["total_lines"] == 2

        e = await env.edit("notes.txt", old_text="beta", new_text="gamma")
        assert e["ok"] is True
        assert state.files["/root/notes.txt"] == b"alpha\ngamma\n"
    finally:
        env.close()

    # Every request carried the bearer token.
    assert state.auth_seen
    assert all(h == "Bearer sk-mock" for h in state.auth_seen)


async def test_read_missing_file_maps_404(mock_createos: tuple[str, _MockState]) -> None:
    base_url, _ = mock_createos
    env = _make_env(base_url)
    try:
        r = await env.read("ghost.txt")
        assert "error" in r
        assert "not found" in r["error"].lower()
    finally:
        env.close()


async def test_shell_exec_envelope_and_cwd(mock_createos: tuple[str, _MockState]) -> None:
    """Provider wraps the command under cwd and parses the JSend exec envelope."""
    base_url, state = mock_createos
    env = _make_env(base_url)
    try:
        res = await env.shell("echo hi")
        assert res["exit_code"] == 0
        # Echoed back by the mock = the resolved argv.
        assert res["stdout"] == "-c cd /root && echo hi"
        body = state.exec_bodies[0]
        assert body["cmd"] == "bash"
        assert body["args"] == ["-c", "cd /root && echo hi"]
    finally:
        env.close()


async def test_shell_nonzero_exit_maps_error(mock_createos: tuple[str, _MockState]) -> None:
    base_url, state = mock_createos
    state.exec_result = {"stdout": "", "stderr": "boom", "exit_code": 2}
    env = _make_env(base_url)
    try:
        res = await env.shell("false")
        assert res["exit_code"] == 2
        assert "status 2" in res["error"]
        assert "boom" in res["error"]
    finally:
        env.close()


def test_poll_waits_for_running(
    mock_createos: tuple[str, _MockState], monkeypatch: pytest.MonkeyPatch
) -> None:
    """create_sync polls until the sandbox reports 'running'."""
    base_url, state = mock_createos
    state.pending_polls = 2
    monkeypatch.setattr(mod, "_POLL_INTERVAL_S", 0.01)
    env = _make_env(base_url)
    try:
        # Provisioning returned only after the 'pending' polls elapsed.
        assert state._poll_count >= 3
        assert env._sandbox_id == "sbx_mock"
    finally:
        env.close()


def test_close_destroys_sandbox(mock_createos: tuple[str, _MockState]) -> None:
    base_url, state = mock_createos
    env = _make_env(base_url)
    env.close()
    assert state.deleted == ["sbx_mock"]
    # Idempotent: a second close issues no further DELETE.
    env.close()
    assert state.deleted == ["sbx_mock"]
