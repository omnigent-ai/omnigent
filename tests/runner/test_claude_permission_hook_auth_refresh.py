"""The runner keeps the Claude permission hook's header snapshot on a live bearer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.runner.native import orchestration


async def test_refresh_writes_each_new_runner_bearer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each renewed runner bearer reaches ``permission_hook.json`` once, past a failed mint."""
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = bridge.prepare_bridge_dir(
        "conv_refresh", bridge_id="bridge_refresh", workspace=tmp_path
    )
    bridge.build_hook_settings(
        bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer launch"},
    )
    tokens = iter(["renewed", "renewed", RuntimeError("mint failed"), "renewed-again"])
    written: list[str] = []
    update = bridge.update_permission_hook_auth_headers

    def _recording_update(path: Path, headers: dict[str, str]) -> bool:
        written.append(headers["Authorization"])
        return update(path, headers)

    monkeypatch.setattr(bridge, "update_permission_hook_auth_headers", _recording_update)

    def _next_token() -> str:
        token = next(tokens, "renewed-again")
        if isinstance(token, Exception):
            raise token
        return token

    task = asyncio.create_task(
        orchestration._refresh_claude_permission_hook_auth(
            bridge_dir=bridge_dir,
            server_url="http://127.0.0.1:8787",
            auth_token_factory=_next_token,
            refresh_interval_s=0.01,
        )
    )
    try:
        for _ in range(200):
            if len(written) >= 2:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    assert written == ["Bearer renewed", "Bearer renewed-again"]
    snapshot = bridge.read_permission_hook_config(bridge_dir)["ap_auth_headers"]
    assert snapshot["Authorization"] == "Bearer renewed-again"
