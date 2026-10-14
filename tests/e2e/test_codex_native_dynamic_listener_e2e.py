"""Exercise Codex's kernel-assigned listener and process cleanup without a model."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexNativeAppServer,
)


@pytest.mark.parametrize("cancel_start", [False, True], ids=["ready", "cancelled"])
async def test_codex_native_dynamic_listener(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_start: bool
) -> None:
    """A real CLI publishes its assigned endpoint and is reaped on cancellation."""
    codex = shutil.which(os.environ.get("OMNIGENT_CODEX_PATH", "codex"))
    if codex is None:
        pytest.skip("requires the Codex CLI")
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    server = CodexNativeAppServer(
        codex_path=codex,
        socket_path=tmp_path / "unused.sock",
        codex_home=tmp_path / "codex-home",
        env={"PATH": os.environ.get("PATH", os.defpath)},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
        listen_url="ws://127.0.0.1:0",
        reconcile_process_registry=False,
    )
    probing = asyncio.Event()
    real_wait = CodexNativeAppServer._wait_until_ready

    async def _wait(self: CodexNativeAppServer) -> CodexAppServerClient:
        probing.set()
        return await real_wait(self)

    monkeypatch.setattr(CodexNativeAppServer, "_wait_until_ready", _wait)
    startup = asyncio.create_task(server.start())
    try:
        await asyncio.wait_for(probing.wait(), 15)
        proc = server.proc
        assert proc is not None
        if cancel_start:
            startup.cancel()
            with pytest.raises(asyncio.CancelledError):
                await startup
        else:
            await asyncio.wait_for(startup, 15)
            assert server.listen_url is not None
            assert server.listen_url.startswith("ws://127.0.0.1:")
            assert 0 < int(server.listen_url.rsplit(":", 1)[1]) <= 65535
            client = CodexAppServerClient(ws_url=server.listen_url)
            try:
                await client.connect()
                assert "result" in await client.request("config/read", {})
            finally:
                await client.close()
    finally:
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await server.close()

    assert proc.returncode is not None
    assert server.proc is None
    assert server.stderr_task is None
