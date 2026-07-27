"""Tests for the Kimi 1.49 in-process approval bridge."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import omnigent.kimi_native_approval_bridge as bridge


class _FakeRuntime:
    """Small ApprovalRuntime stand-in exposing Kimi's public seams."""

    def __init__(self) -> None:
        self.subscribers: list[Any] = []
        self.resolutions: list[tuple[str, str, str]] = []

    def subscribe(self, callback: Any) -> str:
        self.subscribers.append(callback)
        return "subscription"

    def resolve(self, request_id: str, response: str, feedback: str = "") -> bool:
        self.resolutions.append((request_id, response, feedback))
        return True

    def publish(self, event: object) -> None:
        for callback in self.subscribers:
            callback(event)


def _request(*, status: str = "pending") -> SimpleNamespace:
    display = SimpleNamespace(
        model_dump=lambda **_kwargs: {"type": "shell", "command": "touch should-not-exist"}
    )
    return SimpleNamespace(
        id="approval-123",
        tool_call_id="tool-456",
        sender="Shell",
        action="execute",
        description="Run a shell command",
        display=[display],
        status=status,
        feedback="",
    )


def test_materialize_sitecustomize_uses_owner_scoped_bridge(tmp_path: Path) -> None:
    env = bridge.materialize_kimi_approval_bridge(tmp_path / "bridge")

    startup_dir = Path(env["PYTHONPATH"])
    startup_file = startup_dir / "sitecustomize.py"
    assert startup_file.is_symlink()
    assert startup_file.resolve() == Path(bridge.__file__).resolve()
    assert env[bridge.ENABLED_ENV_VAR] == "1"
    assert env[bridge.BRIDGE_DIR_ENV_VAR] == str(tmp_path / "bridge")
    assert startup_dir.stat().st_mode & 0o077 == 0


def test_web_payload_preserves_kimi_request_identity_and_display() -> None:
    payload = bridge.approval_payload(_request())

    assert payload == {
        "elicitation_id": "elicit_kimi_approval-123",
        "agent": "Kimi",
        "policy_name": "kimi_native_permission",
        "operation_type": "execute",
        "message": "Run a shell command",
        "content_preview": json.dumps(
            {
                "tool_call_id": "tool-456",
                "sender": "Shell",
                "display": [{"type": "shell", "command": "touch should-not-exist"}],
            },
            sort_keys=True,
        ),
    }


def test_web_payload_bounds_tool_preview() -> None:
    request = _request()
    request.display[0].model_dump = lambda **_kwargs: {
        "type": "shell",
        "command": "x" * 4_096,
    }

    payload = bridge.approval_payload(request)

    assert len(str(payload["content_preview"])) == 1024


@pytest.mark.parametrize(
    ("web_verdict", "expected_response"),
    [
        pytest.param("accept", "approve", id="web-accept"),
        pytest.param("decline", "reject", id="web-decline"),
        pytest.param("cancel", "reject", id="web-cancel"),
        pytest.param(None, None, id="unresolved-keeps-native-modal"),
    ],
)
@pytest.mark.asyncio
async def test_runtime_request_is_resolved_only_from_concrete_web_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    web_verdict: str | None,
    expected_response: str | None,
) -> None:
    monkeypatch.setattr(
        bridge,
        "request_web_verdict",
        lambda _bridge_dir, _request: web_verdict,
    )
    runtime_type = type(f"Runtime_{web_verdict}", (_FakeRuntime,), {})
    bridge.patch_approval_runtime(runtime_type, tmp_path)
    runtime = runtime_type()
    request = _request()

    runtime.publish(SimpleNamespace(kind="request_created", request=request))
    await bridge.wait_for_bridge_tasks(runtime)

    if expected_response is None:
        assert runtime.resolutions == []
    else:
        assert runtime.resolutions == [("approval-123", expected_response, "Resolved in Omnigent")]


@pytest.mark.asyncio
async def test_terminal_resolution_releases_pending_web_card(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    released: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        bridge,
        "post_external_resolution",
        lambda bridge_dir, request_id: released.append((bridge_dir, request_id)),
    )
    runtime_type = type("Runtime_terminal_resolution", (_FakeRuntime,), {})
    bridge.patch_approval_runtime(runtime_type, tmp_path)
    runtime = runtime_type()

    runtime.publish(SimpleNamespace(kind="request_resolved", request=_request(status="resolved")))
    await bridge.wait_for_bridge_tasks(runtime)

    assert released == [(tmp_path, "approval-123")]


def test_web_transport_failure_returns_no_verdict(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = {
        "ap_server_url": "http://127.0.0.1:6767",
        "ap_auth_headers": {},
        "session_id": "session-1",
    }
    (tmp_path / "hook_config.json").write_text(json.dumps(config), encoding="utf-8")

    def _unreachable(*_args: object, **_kwargs: object) -> object:
        raise OSError("offline")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", _unreachable)

    assert bridge.request_web_verdict(tmp_path, _request()) is None
