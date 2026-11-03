"""A real host exit report preserves an earlier failure across server restart."""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
import pytest
from websockets.asyncio.client import connect

from omnigent.host.frames import HostHelloFrame, HostRunnerExitedFrame, encode_host_frame
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True], ids=["warm", "cold"])
@pytest.mark.timeout(120)
async def test_runner_exit_preserves_original_failure(tmp_path: Path, restart: bool) -> None:
    server_root = tmp_path / "server"
    log_path = server_root / "runtime.log"
    server_env = {"OMNIGENT_PROCESS_LOG_FILE": str(log_path)}
    runner_id = "runner_never_connected"
    original = {"code": "native_turn_error", "message": "Synthetic provider rejection"}
    with contextlib.ExitStack() as stack:
        base_url = stack.enter_context(isolated_local_server(server_root, env=server_env))
        with httpx.Client(trust_env=False) as client:
            session_id = create_native_session(client, base_url, harness="claude")["session_id"]
            # Bind a runner that has never opened its tunnel to this owned test server.
            store = SqlAlchemyConversationStore(f"sqlite:///{server_root / 'chat.db'}")
            stack.callback(store._engine.dispose)
            assert store.set_runner_id(session_id, runner_id)
            response = client.post(
                f"{base_url}/v1/sessions/{session_id}/events",
                json={
                    "type": "external_session_status",
                    "data": {"status": "failed", "failure_detail": original["message"]},
                },
            )
            response.raise_for_status()
            snapshot = client.get(f"{base_url}/v1/sessions/{session_id}")
            snapshot.raise_for_status()
            assert snapshot.json()["last_task_error"]["message"] == original["message"]

        if restart:
            stack.close()
            base_url = stack.enter_context(isolated_local_server(server_root, env=server_env))
            # The early close consumed the store's dispose callback; re-register it
            # so the recreated SQLite pool is also disposed at exit.
            stack.callback(store._engine.dispose)

        host_id = "89153a620ca447618d15c97bc63e52b6"
        socket_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
        async with connect(socket_url) as socket:
            await socket.send(
                encode_host_frame(
                    HostHelloFrame(
                        version="0.1-test", frame_protocol_version=1, name="Owned test host"
                    )
                )
            )
            await socket.send(
                encode_host_frame(
                    HostRunnerExitedFrame(
                        runner_id=runner_id,
                        error="Synthetic runner exited with status 1",
                        had_connected=False,
                    )
                )
            )
            async with asyncio.timeout(10):
                while True:
                    log = log_path.read_text(errors="replace") if log_path.exists() else ""
                    decisions = [
                        line
                        for line in log.splitlines()
                        if f"Runner disconnect for session={session_id}:" in line
                    ]
                    if decisions:
                        break
                    await asyncio.sleep(0.05)
            assert "already_failed" in decisions[-1]
            assert "failed_before_start" not in decisions[-1]
            async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
                snapshot = await client.get(f"/v1/sessions/{session_id}")
                snapshot.raise_for_status()
            body = snapshot.json()
            assert body["status"] == "failed"
            error = body["last_task_error"]
            assert {key: error[key] for key in original} == original
            refreshed = store.get_conversation(session_id)
            assert refreshed is not None and refreshed.live_status == "failed"
            assert refreshed.labels["omnigent.last_task_error_code"] == original["code"]
            # Re-read after the snapshot GET so a late sweep- or snapshot-path
            # turn failure is captured, not only the log up to the decision.
            log = log_path.read_text(errors="replace")
            assert "Synthetic runner exited with status 1" in log
            failure_lines = [
                line
                for line in log.splitlines()
                if f"session turn failed for {session_id}" in line
            ]
            assert not any("runner_failed_to_start" in line for line in failure_lines)
            assert not any("origin=runner_offline_sweep" in line for line in failure_lines)
