"""The runner writes the per-session v2 opencode.json before booting ``opencode serve``."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

import omnigent.harnesses.opencode_native.app_server as app_server
import omnigent.harnesses.opencode_native.bridge as bridge
import omnigent.harnesses.opencode_native.provider as provider
import omnigent.runner._entry as runner_entry
import omnigent.runner.native.orchestration as orchestration


class _StopBeforeBoot(Exception):
    pass


class _FakeServer:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    async def start(self) -> None:
        raise _StopBeforeBoot


@pytest.fixture
def launch_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://omnigent.test")
    seeded: list[Path] = []
    monkeypatch.setattr(bridge, "seed_opencode_auth", lambda d: seeded.append(d))
    monkeypatch.setattr(provider, "managed_connect_opencode_config", lambda *a: None)
    monkeypatch.setattr(runner_entry, "_make_auth_token_factory", lambda *a, **k: None)
    monkeypatch.setattr(app_server, "OpenCodeNativeServer", _FakeServer)
    monkeypatch.setattr(
        orchestration, "_native_startup_raw_instructions_from_spec", lambda spec: "Agent rules."
    )

    async def _launch_config(**kwargs: Any) -> Any:
        return orchestration._OpenCodeNativeLaunchConfig(
            workspace=tmp_path,
            policy_server_url="http://omnigent.test",
            terminal_launch_args=[],
            model_override="anthropic/claude-sonnet-4-5",
            external_session_id=None,
            fork_carry_history=False,
        )

    monkeypatch.setattr(orchestration, "_opencode_native_launch_config", _launch_config)
    return {"seeded": seeded}


async def _launch_until_boot(**kwargs: Any) -> Path:
    with pytest.raises(_StopBeforeBoot):
        await orchestration._auto_create_opencode_terminal(
            "conv_launch", cast(Any, object()), lambda *a: None, **kwargs
        )
    return bridge.bridge_dir_for_bridge_id("conv_launch")


async def test_launch_writes_v2_config_with_ask_all_permissions(
    launch_env: dict[str, Any],
) -> None:
    async def _ensure_relay(*args: Any, **kwargs: Any) -> None:
        return None

    bridge_dir = await _launch_until_boot(
        server_client=object(), ensure_comment_relay=_ensure_relay
    )
    xdg = bridge_dir / "xdg-config"
    written = json.loads((xdg / "opencode" / "opencode.json").read_text(encoding="utf-8"))
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert written["mcp"]["servers"]["omnigent"]["codemode"] is False
    assert written["plugins"] == [str(bridge_dir / "omnigent-policy")]
    assert written["instructions"] == [str(xdg / "opencode" / "AGENTS.md")]
    assert (xdg / "opencode" / "AGENTS.md").read_text(encoding="utf-8").strip() == "Agent rules."
    assert written["model"] == "anthropic/claude-sonnet-4-5"
    assert not {"provider", "permission", "plugin"} & set(written)
    assert launch_env["seeded"] == [bridge_dir]


async def test_launch_without_server_still_writes_ask_all_config(
    launch_env: dict[str, Any],
) -> None:
    bridge_dir = await _launch_until_boot()
    written = json.loads(
        (bridge_dir / "xdg-config" / "opencode" / "opencode.json").read_text(encoding="utf-8")
    )
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert "mcp" not in written
    assert "plugins" not in written
