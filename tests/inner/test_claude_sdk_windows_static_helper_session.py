"""Native Windows SDK process coverage against a credential-checking mock gateway."""

from __future__ import annotations

import asyncio
import os
import platform
import threading
from importlib.metadata import version
from pathlib import Path

import httpx
import pytest
import uvicorn

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from tests._helpers.live_server import reserve_port
from tests._helpers.polling import poll_until_async
from tests.server.integration.mock_llm_server import app as mock_gateway

pytestmark = pytest.mark.skipif(os.name != "nt", reason="native Windows SDK launcher")


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_windows_static_key_gateway_completes_real_sdk_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = "dummy-native-static-helper-key"
    marker = "WINDOWS_STATIC_HELPER_OK"
    model = "sonnet"
    authenticated: list[bool] = []

    async def credential_checked_gateway(scope, receive, send) -> None:
        if scope["type"] == "http" and scope["path"] == "/v1/messages":
            headers = dict(scope["headers"])
            valid = (
                headers.get(b"x-api-key") == token.encode()
                or headers.get(b"authorization") == f"Bearer {token}".encode()
            )
            authenticated.append(valid)
            if not valid:
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b"dummy credential required"})
                return
        await mock_gateway(scope, receive, send)

    home = tmp_path / "home"
    home.mkdir()
    config = home / ".claude"
    config.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config))
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    for name in (
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ):
        monkeypatch.delenv(name, raising=False)

    executor = None
    process = None
    messages = []
    key_file = None
    with reserve_port() as socket:
        base_url = f"http://127.0.0.1:{socket.getsockname()[1]}"
        server = uvicorn.Server(
            uvicorn.Config(
                credential_checked_gateway, log_level="warning", lifespan="off", interface="asgi3"
            )
        )
        worker = threading.Thread(target=server.run, kwargs={"sockets": [socket]}, daemon=True)
        worker.start()
        try:
            await poll_until_async(lambda: server.started, timeout_s=15)
            async with httpx.AsyncClient(base_url=base_url) as control:
                response = await control.post(
                    "/mock/configure", json={"responses": [{"text": marker}] * 12}
                )
                response.raise_for_status()
            executor = ClaudeSDKExecutor(
                cwd=str(tmp_path),
                model=model,
                gateway=True,
                base_url_override=base_url,
                gateway_auth_command=f"printf %s {token}",
                gateway_auth_refresh_interval_ms="1",
                skills_filter="none",
            )
            print(f"OS: {platform.platform()}; Python: {platform.python_version()}")
            print(f"Harness: claude-sdk {version('claude-agent-sdk')}; provider: loopback mock")
            print("Deployment: OSS; transport: real SDK and Claude CLI subprocess")
            client = None
            for turn in range(2):
                if turn:
                    assert key_file is not None
                    token = "dummy-native-static-helper-refreshed-key"
                    key_file.path.write_bytes(token.encode("utf-8"))
                    await asyncio.sleep(0.05)
                    print("Updated dummy credential in the same private file for helper refresh.")
                requests_before_turn = len(authenticated)
                prompt = f"Return exactly {marker}. Turn {turn + 1}."
                print(f"Prompt: {prompt}")
                messages.append(
                    {"role": "user", "content": prompt, "session_id": "windows-helper"}
                )
                events = [event async for event in executor.run_turn(messages, [], "")]
                errors = [event for event in events if isinstance(event, ExecutorError)]
                assert not errors, (errors, authenticated)
                completed = [event for event in events if isinstance(event, TurnComplete)]
                assert completed and completed[-1].response == marker
                assert len(authenticated) > requests_before_turn
                assert all(authenticated[requests_before_turn:])
                print(f"Reply: {completed[-1].response}")
                messages.append({"role": "assistant", "content": marker})
                state = executor._clients["windows-helper"]
                assert client is None or state.client is client
                client = state.client
                process = client._transport._process
                assert process is not None and process.returncode is None
                assert state.static_key_file is not None
                assert key_file is None or state.static_key_file is key_file
                key_file = state.static_key_file
                assert key_file.path.read_bytes() == token.encode("utf-8")
            assert len(authenticated) >= 2 and all(authenticated)
        finally:
            try:
                if executor is not None:
                    await executor.close()
            finally:
                server.should_exit = True
                await asyncio.to_thread(worker.join, 5)
                assert not worker.is_alive()
    assert process is not None and process.returncode is not None
    assert key_file is not None and not key_file.directory.exists()
    print("Two real SDK turns authenticated with different dummy keys; helper refresh verified.")
    print("CLI exited and private key file was removed.")
