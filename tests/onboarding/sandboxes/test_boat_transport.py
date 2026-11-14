"""Boat HTTP transport contract against a local API stand-in.

The stand-in speaks the documented HTTP contract on a real socket, and the
launcher reaches it through ``BOAT_API_URL`` exactly as it would reach Boat.
It covers the happy path the server drives: parse ``sandbox.provider: boat``,
provision and install the host, keep alive, resume after archival, replace a
deleted sandbox, and delete. No Boat account or API key is needed::

    pytest tests/onboarding/sandboxes/test_boat_transport.py -v
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.boat import BoatSandboxLauncher
from omnigent.server.managed_hosts import BOAT_MANAGED_TOKEN_TTL_S, parse_sandbox_config

if TYPE_CHECKING:
    from collections.abc import Iterator

_KEY = "boat-e2e-key"


class _FakeBoat(BaseHTTPRequestHandler):
    """Minimal Boat v1 API: sandbox CRUD, resume and commands."""

    sandboxes: ClassVar[dict[str, dict[str, Any]]] = {}
    commands: ClassVar[list[str]] = []

    def log_message(self, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any] | None = None) -> None:
        data = json.dumps(payload or {"ok": True}).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self) -> None:
        if self.headers.get("Authorization") != f"Bearer {_KEY}":
            self._send(401, {"ok": False, "code": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        parts = self.path.removeprefix("/api/v1/").split("/")
        if parts == ["sandboxes"] and self.command == "POST":
            sandbox_id = f"bx_{len(self.sandboxes) + 1}"
            self.sandboxes[sandbox_id] = {"id": sandbox_id, "state": "ready", "create": body}
            self._send(202, {"ok": True, "sandbox": self.sandboxes[sandbox_id]})
            return
        sandbox = self.sandboxes.get(parts[1]) if len(parts) > 1 else None
        if sandbox is None or sandbox["state"] == "gone":
            self._send(404, {"ok": False, "code": "not_found"})
            return
        tail = parts[2:]
        if not tail and self.command == "GET":
            self._send(200, {"ok": True, "sandbox": sandbox})
        elif not tail and self.command == "PATCH":
            sandbox.update(body)
            self._send(200, {"ok": True, "sandbox": sandbox})
        elif not tail and self.command == "DELETE":
            assert self.headers.get("X-Ascii-Confirm-Delete") == sandbox["id"]
            sandbox["state"] = "gone"
            self._send(200)
        elif tail == ["resume"]:
            sandbox["state"] = "ready"
            self._send(202)
        elif tail == ["commands"]:
            self.commands.append(body["command"])
            self._send(200, {"ok": True, "exitCode": 0, "stdout": "", "stderr": ""})
        else:
            self._send(404, {"ok": False, "code": "not_found"})

    do_GET = do_POST = do_PATCH = do_DELETE = _handle


@pytest.fixture
def boat_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[type[_FakeBoat]]:
    _FakeBoat.sandboxes = {}
    _FakeBoat.commands = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeBoat)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("BOAT_API_KEY", _KEY)
    monkeypatch.setenv("BOAT_API_URL", f"http://127.0.0.1:{server.server_port}/api/v1")
    monkeypatch.setenv("OMNIGENT_BOAT_HOST_PACKAGE", "omnigent==0.0.0")
    try:
        yield _FakeBoat
    finally:
        server.shutdown()
        server.server_close()


def test_boat_managed_host_lifecycle(boat_api: type[_FakeBoat]) -> None:
    config = parse_sandbox_config({"provider": "boat", "server_url": "https://omni.example"})
    assert config is not None and config.managed_launch_supported
    assert config.default.token_ttl_s == BOAT_MANAGED_TOKEN_TTL_S
    launcher = config.default.launcher_factory()
    assert isinstance(launcher, BoatSandboxLauncher)

    sandbox_id = launcher.provision("managed-e2e")
    created = boat_api.sandboxes[sandbox_id]
    assert created["create"] == {"noEnv": True, "ttlSeconds": None, "monitoring": False}
    assert created["name"] == "managed-e2e"
    assert any("omnigent==0.0.0" in command for command in boat_api.commands)
    assert launcher.is_running(sandbox_id)
    assert launcher.keep_alive(sandbox_id) is True

    created["state"] = "archived"
    launcher.attach(sandbox_id)
    assert created["state"] == "ready"

    created["state"] = "deleted"
    with pytest.raises(SandboxGoneError):
        launcher.attach(sandbox_id)

    created["state"] = "ready"
    launcher.terminate(sandbox_id)
    assert not launcher.is_running(sandbox_id)
    launcher.terminate(sandbox_id)
