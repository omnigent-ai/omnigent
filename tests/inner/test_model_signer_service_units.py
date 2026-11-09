"""Unit tests for the signer process's pure pieces (``model_signer_service``).

The relay's TLS/socket path and the strict HTTP parsers are covered by the
protocol and process tests; this file covers what those cannot reach
cheaply: the fake provider's Responses payloads, config loading, the
owned-directory cleanup guard, the supervisor's stdin command handling, and
``main()``'s exit mapping.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.inner import model_signer_service as svc
from omnigent.inner.model_auth import PROVIDER_AUTH_REQUIRED, ProviderAuthRequired
from omnigent.inner.model_egress import FrozenModelRoute

_ROUTE = FrozenModelRoute(method="POST", host="api.example.test", path="/v1/responses")


# ── Credential stub ────────────────────────────────────────────────────────


async def test_static_credential_returns_its_token_and_ignores_invalidation() -> None:
    cred = svc._StaticCredential("tok")
    assert await cred.token_for_request() == "tok"
    cred.invalidate_after_unauthorized()
    assert await cred.token_for_request() == "tok"


# ── Fake provider payloads ─────────────────────────────────────────────────


def test_fake_non_streaming_payload_carries_the_marker_only_when_authorized() -> None:
    body, ctype = svc._fake_responses_payload(payload={"model": "m1"}, authorized=True)
    assert ctype == b"application/json"
    doc = json.loads(body)
    assert doc["model"] == "m1"
    assert doc["output"][0]["content"][0]["text"] == svc._E2E_MARKER

    body, _ = svc._fake_responses_payload(payload={"model": "m1"}, authorized=False)
    assert json.loads(body)["output"] == []

    body, _ = svc._fake_responses_payload(payload=["not", "a", "dict"], authorized=True)
    assert json.loads(body)["model"] == "fake"


def test_fake_streaming_payload_is_a_responses_sse_sequence() -> None:
    body, ctype = svc._fake_responses_payload(
        payload={"model": "m2", "stream": True}, authorized=True
    )
    assert ctype == b"text/event-stream"
    frames = [f for f in body.decode().split("\n\n") if f]
    events = [json.loads(f.split("\ndata: ", 1)[1]) for f in frames]
    types = [e["type"] for e in events]
    assert types[0] == "response.created"
    assert "response.output_text.delta" in types
    assert types[-1] == "response.completed"
    delta = next(e for e in events if e["type"] == "response.output_text.delta")
    assert delta["delta"] == svc._E2E_MARKER
    completed = events[-1]["response"]
    assert completed["model"] == "m2"
    assert completed["output"][0]["content"][0]["text"] == svc._E2E_MARKER


# ── Config loading ─────────────────────────────────────────────────────────


def _config_fd(payload: Any, tmp_path: Path) -> int:
    """Write *payload* to a file and return a read fd positioned at its start."""
    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    path = tmp_path / "signer-config.json"
    path.write_bytes(raw)
    return os.open(path, os.O_RDONLY)


def _config(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "binding_id": svc._TEST_BINDING,
        "endpoint": "https://api.example.test/v1",
        "routes": [{"method": "POST", "host": "api.example.test", "path": "/v1/responses"}],
    }
    base.update(overrides)
    return base


def test_load_config_accepts_the_test_binding(tmp_path: Path) -> None:
    binding, route, profile = svc._load_config(_config_fd(_config(), tmp_path))
    assert binding == svc._TEST_BINDING
    assert route == _ROUTE
    assert profile is None


def test_load_config_accepts_the_ucode_binding_with_a_profile(tmp_path: Path) -> None:
    binding, route, profile = svc._load_config(
        _config_fd(_config(binding_id=svc._UCODE_BINDING, auth_profile="work"), tmp_path)
    )
    assert binding == svc._UCODE_BINDING
    assert route.host == "api.example.test"
    assert profile == "work"


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (b"x" * (svc._MAX_CONFIG_BYTES + 1), "too large"),
        (["not", "dict"], "fields are invalid"),
        (_config(extra=1), "fields are invalid"),
        (_config(binding_id="unknown-binding"), "fields are invalid|unavailable"),
        (_config(binding_id=svc._UCODE_BINDING), "fields are invalid"),
        (_config(routes=[]), "exactly one route"),
        (_config(routes=[{"method": "POST"}]), "route fields are invalid"),
        (_config(endpoint="http://api.example.test/v1"), "exact POST trusted"),
        (_config(endpoint="https://other.test/v1"), "exact POST trusted"),
        (_config(endpoint="https://user@api.example.test/v1"), "exact POST trusted"),
        (_config(endpoint="https://api.example.test:8443/v1"), "exact POST trusted"),
        (_config(endpoint="https://api.example.test/v1?x=1"), "exact POST trusted"),
        (_config(endpoint="https://api.example.test/"), "exact POST trusted"),
        (
            _config(
                routes=[{"method": "GET", "host": "api.example.test", "path": "/v1/responses"}]
            ),
            "exact POST trusted",
        ),
        (
            _config(
                routes=[{"method": "POST", "host": "api.example.test", "path": "/v2/responses"}]
            ),
            "exact POST trusted",
        ),
    ],
    ids=[
        "too-large",
        "not-a-dict",
        "extra-key",
        "unknown-binding",
        "ucode-without-profile",
        "no-routes",
        "route-missing-keys",
        "http-endpoint",
        "host-mismatch",
        "userinfo",
        "non-443-port",
        "query",
        "root-path",
        "get-route",
        "path-mismatch",
    ],
)
def test_load_config_rejections(payload: Any, why: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=why):
        svc._load_config(_config_fd(payload, tmp_path))


# ── Port probe and owned-directory cleanup ─────────────────────────────────


def test_pick_relay_port_returns_a_bindable_loopback_port() -> None:
    port = svc._pick_relay_port()
    assert 1024 <= port <= 65535
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", port))


def test_remove_owned_directory_only_removes_the_exact_directory(tmp_path: Path) -> None:
    svc._remove_owned_directory(None, None)  # nothing to do
    svc._remove_owned_directory(tmp_path / "missing", (1, 2))  # vanished already

    owned = tmp_path / "owned"
    owned.mkdir()
    (owned / "file").write_text("x")
    st = owned.lstat()
    identity = (st.st_dev, st.st_ino)

    # Same path, different inode (someone replaced it) → left alone.
    svc._remove_owned_directory(owned, (st.st_dev, st.st_ino + 1))
    assert owned.exists()
    # A file at the path is never treated as the owned directory.
    plain = tmp_path / "plain"
    plain.write_text("x")
    pst = plain.lstat()
    svc._remove_owned_directory(plain, (pst.st_dev, pst.st_ino))
    assert plain.exists()

    svc._remove_owned_directory(owned, identity)
    assert not owned.exists()


# ── Supervisor and main() ──────────────────────────────────────────────────


class _SilentReader:
    """Stands in for ``threading.Thread``: the stdin reader never delivers a command."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        pass


async def _supervised(
    monkeypatch: pytest.MonkeyPatch, stdin: bytes | None, run_result: Any
) -> int:
    """Run ``_run_supervised`` with stdin scripted and ``_run`` replaced.

    ``stdin=None`` means no command ever arrives (the reader thread is a
    no-op), so the only way out is the run itself finishing.
    """
    if stdin is None:
        # Patch the module's handle only, never ``threading`` itself: the event
        # loop's executor threads must keep working for every other test.
        monkeypatch.setattr(svc, "threading", SimpleNamespace(Thread=_SilentReader))
    else:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin)))

    async def _fake_run(config_fd: int) -> int:
        if isinstance(run_result, int):
            return run_result
        await asyncio.sleep(3600)  # never finishes: the command must win
        raise AssertionError("unreachable")

    monkeypatch.setattr(svc, "_run", _fake_run)
    return await svc._run_supervised(0)


async def test_supervisor_returns_the_run_result_when_it_finishes_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _supervised(monkeypatch, None, 7) == 7


async def test_supervisor_shutdown_command_cancels_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _supervised(monkeypatch, b"shutdown\n", None) == 0


async def test_supervisor_closed_stdin_cancels_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # EOF on stdin (the runner died) reads as b"" → clean shutdown.
    assert await _supervised(monkeypatch, b"", None) == 0


async def test_supervisor_unknown_command_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _supervised(monkeypatch, b"explode\n", None) == 2


def test_main_requires_config_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.argv", ["signer"])
    with pytest.raises(SystemExit) as exc:
        svc.main()
    assert exc.value.code == 2


def test_main_maps_auth_required_to_a_structured_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _auth_required(config_fd: int) -> int:
        raise ProviderAuthRequired.for_authority("https://api.example.test", "work")

    monkeypatch.setattr("sys.argv", ["signer", "--config-fd", "3"])
    monkeypatch.setattr(svc, "_run_supervised", _auth_required)
    assert svc.main() == 1
    assert json.loads(capsys.readouterr().out.strip()) == {
        "status": "error",
        "code": PROVIDER_AUTH_REQUIRED,
    }


def test_main_keeps_other_failures_opaque(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _boom(config_fd: int) -> int:
        raise RuntimeError("/private/path leaked?")

    monkeypatch.setattr("sys.argv", ["signer", "--config-fd", "3"])
    monkeypatch.setattr(svc, "_run_supervised", _boom)
    assert svc.main() == 1
    assert capsys.readouterr().out == ""


def test_main_returns_the_supervisor_result(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _ok(config_fd: int) -> int:
        assert config_fd == 9
        return 0

    monkeypatch.setattr("sys.argv", ["signer", "--config-fd", "9"])
    monkeypatch.setattr(svc, "_run_supervised", _ok)
    assert svc.main() == 0
