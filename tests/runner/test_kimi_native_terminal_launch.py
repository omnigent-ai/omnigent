"""Regression tests for the runner-owned Kimi terminal launch."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.kimi_native_approval_bridge import (
    BRIDGE_DIR_ENV_VAR,
    ENABLED_ENV_VAR,
)
from omnigent.kimi_native_credentials import (
    KIMI_CODE_HOME_ENV_VAR,
    KIMI_SHARE_DIR_ENV_VAR,
)
from omnigent.runner.native import orchestration
from omnigent.runner.resource_registry import KIMI_NATIVE_TERMINAL_ROLE
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
async def test_kimi_launch_wires_current_state_and_approval_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi 1.49 receives its isolated state, web approval bridge, and forwarder."""
    import omnigent.kimi_native as kimi_native
    import omnigent.kimi_native_bridge as kimi_native_bridge
    import omnigent.kimi_native_forwarder as kimi_native_forwarder

    session_id = "kimi-approval-runtime-session"
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:6767")
    monkeypatch.setattr(kimi_native_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(kimi_native, "resolve_kimi_executable", lambda: "/usr/bin/kimi")

    async def _launch_config(
        **_kwargs: Any,
    ) -> orchestration._PiNativeLaunchConfig:
        return orchestration._PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://127.0.0.1:6767",
            terminal_launch_args=None,
            external_session_id=None,
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", _launch_config)
    forwarder_calls: list[dict[str, Any]] = []

    async def _forwarder(**kwargs: Any) -> None:
        forwarder_calls.append(kwargs)

    monkeypatch.setattr(kimi_native_forwarder, "supervise_kimi_forwarder", _forwarder)
    captured: dict[str, Any] = {}

    class _FakeResourceRegistry:
        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            resource_role: str,
            spec: Any,
        ) -> SessionResourceView:
            captured["terminal_name"] = terminal_name
            captured["session_key"] = session_key
            captured["resource_role"] = resource_role
            captured["spec"] = spec
            return SessionResourceView(
                id="terminal_kimi_main",
                type="terminal",
                session_id=session_id,
                name="kimi:main",
                metadata={
                    "terminal_name": "kimi",
                    "session_key": "main",
                    "running": True,
                },
            )

    try:
        await orchestration._auto_create_kimi_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _session_id, _event: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        for _ in range(20):
            if forwarder_calls:
                break
            await asyncio.sleep(0)
    finally:
        await orchestration._cancel_auto_forwarder_task(session_id)

    bridge_dir = kimi_native_bridge.bridge_dir_for_session_id(session_id)
    spec = captured["spec"]
    assert captured["terminal_name"] == "kimi"
    assert captured["session_key"] == "main"
    assert captured["resource_role"] == KIMI_NATIVE_TERMINAL_ROLE
    assert spec.command == "/usr/bin/kimi"
    assert spec.env[KIMI_SHARE_DIR_ENV_VAR] == str(bridge_dir / "kimi-share")
    assert spec.env[KIMI_CODE_HOME_ENV_VAR] == str(bridge_dir / "kimi-code-home")
    assert spec.env[ENABLED_ENV_VAR] == "1"
    assert spec.env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir)
    assert Path(spec.env["PYTHONPATH"], "sitecustomize.py").is_symlink()
    assert forwarder_calls == [
        {
            "base_url": "http://127.0.0.1:6767",
            "headers": {},
            "session_id": session_id,
            "bridge_dir": bridge_dir,
            "kimi_share_dir": bridge_dir / "kimi-share",
            "legacy_kimi_home": bridge_dir / "kimi-code-home",
            "workspace": str(tmp_path),
            "launch_epoch_ms": forwarder_calls[0]["launch_epoch_ms"],
        }
    ]
