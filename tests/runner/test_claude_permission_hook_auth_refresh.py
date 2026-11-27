"""The runner keeps the Claude permission hook's header snapshot on a live bearer."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native.settings_generation import (
    ClaudeSettingsRequest,
    request_claude_bridge_deletion,
)
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

    def _recording_update(
        path: Path,
        headers: dict[str, str],
        *,
        credential_binding: bridge.ClaudeBridgeCredentialBinding | None = None,
    ) -> bool:
        written.append(headers["Authorization"])
        return update(path, headers, credential_binding=credential_binding)

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


@pytest.mark.parametrize(("retained", "reactivate"), [(False, False), (True, False), (True, True)])
async def test_inflight_auth_refresh_cannot_restore_a_deleted_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, retained: bool, reactivate: bool
) -> None:
    monkeypatch.setattr(bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "root")
    bridge_dir = bridge.prepare_bridge_dir("conv_refresh", workspace=tmp_path)
    bridge.build_hook_settings(
        bridge_dir,
        ap_server_url="https://example.com",
        ap_auth_headers={"Authorization": "Bearer synthetic-launch"},
    )
    request = ClaudeSettingsRequest.create(bridge_dir) if retained else None
    binding = bridge.read_bridge_credential_binding(bridge_dir)
    assert binding is not None
    mint_started = threading.Event()
    finish_mint = threading.Event()
    calls = []

    def mint() -> str:
        calls.append(True)
        mint_started.set()
        assert finish_mint.wait(5), "the component did not release its inert mint"
        return "synthetic-stale-renewal"

    task = asyncio.create_task(
        orchestration._refresh_claude_permission_hook_auth(
            bridge_dir=bridge_dir,
            server_url="https://example.com",
            auth_token_factory=mint,
            refresh_interval_s=0,
            credential_binding=binding,
        )
    )
    try:
        assert await asyncio.to_thread(mint_started.wait, 5)
        request_claude_bridge_deletion(bridge_dir, "conv_refresh")
        if reactivate:
            bridge.prepare_bridge_dir("conv_refresh", workspace=tmp_path)
            bridge.build_hook_settings(
                bridge_dir,
                ap_server_url="https://example.com",
                ap_auth_headers={"Authorization": "Bearer synthetic-new-launch"},
            )
            snapshot = (bridge_dir / "permission_hook.json").read_bytes()
        finish_mint.set()
        await asyncio.wait_for(asyncio.shield(task), 5)
        assert calls == [True]
        if reactivate:
            assert (bridge_dir / "permission_hook.json").read_bytes() == snapshot
        else:
            assert not (bridge_dir / "permission_hook.json").exists()
            assert bridge_dir.exists() is retained
    finally:
        finish_mint.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if request is not None:
            request.close()
