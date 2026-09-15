"""Real Codex/tmux recovery against a gated local model endpoint.

Run with an empty CODEX_HOME and OMNIGENT_CONFIG_HOME. A system-wide Codex
provider override must also be isolated so it cannot redirect model requests.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent._wrapper_labels import (
    CODEX_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harnesses.codex_native.bridge import bridge_dir_for_bridge_id, read_bridge_state
from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec
from tests.e2e.conftest import configure_mock_llm, release_mock_gate
from tests.e2e.test_host_codex_native_e2e import _poll_for_assistant_marker, _send_user_text

_SYSTEM_CODEX_CONFIG = Path("/etc/codex/managed_config.toml")
pytestmark = [
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="requires real Codex and tmux binaries",
    ),
    pytest.mark.skipif(
        _SYSTEM_CODEX_CONFIG.exists() and bool(_SYSTEM_CODEX_CONFIG.read_text().strip()),
        reason="requires isolated Codex system config to keep model requests local",
    ),
]


def _wait_for(check: Callable[[], Any], description: str, timeout: float = 60.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {description}")


def _app_server_pid(listen_url: str) -> int:
    # The unique loopback listener identifies only this test's app-server.
    output = subprocess.run(
        ["ps", "-eo", "pid=,args="], capture_output=True, text=True, check=True, timeout=10
    ).stdout
    matches = [
        int(line.strip().split(maxsplit=1)[0])
        for line in output.splitlines()
        if listen_url in line and "app-server" in line
    ]
    assert len(matches) == 1, f"Expected one app-server, found PIDs {matches}"
    return matches[0]


def _tmux(socket: str, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", socket, "-f", os.devnull, *args],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
    ).stdout.strip()


def _wait_for_pane_text(socket: str, text: str) -> None:
    _wait_for(
        lambda: text in _tmux(socket, "capture-pane", "-p", "-S", "-1000"),
        f"replacement TUI to display {text!r}",
    )


def test_codex_terminal_recovery_preserves_inflight_turn(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """A lost remote TUI cannot interrupt its backend's ongoing model request."""
    if mock_llm_server_url is None:
        pytest.skip("requires the local mock model endpoint")
    model = f"mock-codex-recovery-{uuid.uuid4().hex[:8]}"
    spec = yaml.safe_load(_materialize_codex_agent_spec(tmp_path, model=model).read_text())
    spec["name"] = f"codex-recovery-{uuid.uuid4().hex[:8]}"
    spec["executor"]["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_llm_server_url}/v1",
    }
    spec["spawn"] = False
    spec["os_env"]["cwd"] = str(tmp_path)
    with io.BytesIO() as buffer:
        with tarfile.open(fileobj=buffer, mode="w:gz") as bundle:
            data = yaml.safe_dump(spec).encode()
            entry = tarfile.TarInfo("codex-native-ui.yaml")
            entry.size = len(data)
            bundle.addfile(entry, io.BytesIO(data))
        payload = buffer.getvalue()

    create = http_client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "workspace": str(tmp_path),
                    "labels": {
                        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
                        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
                    },
                }
            )
        },
        files={"bundle": ("agent.tar.gz", payload, "application/gzip")},
    )
    assert create.is_success, create.text
    session_id = create.json()["session_id"]
    binding = http_client.patch(f"/v1/sessions/{session_id}", json={"runner_id": live_runner_id})
    assert binding.is_success, binding.text
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    terminal_url = f"/v1/sessions/{session_id}/resources/terminals"
    evidence: list[dict[str, Any]] = []

    def ensure_terminal() -> dict[str, Any]:
        response = http_client.post(
            terminal_url,
            json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            timeout=60,
        )
        assert response.status_code == 200, response.text[:1000]
        return response.json()

    def terminal_gone() -> bool:
        response = http_client.get(f"{terminal_url}/terminal_codex_main", timeout=10)
        assert response.status_code in (200, 404), response.text[:1000]
        return response.status_code == 404

    try:
        terminal = ensure_terminal()
        state = _wait_for(lambda: read_bridge_state(bridge_dir), "Codex thread creation")
        original_pid = _app_server_pid(state.socket_path)
        original_thread = state.thread_id
        print(f"E2E started: app_server_pid={original_pid}, thread={original_thread}", flush=True)

        # Warm-up proves a real model round-trip and lets the thread id persist
        # on the server before either terminal recovery path is exercised.
        marker = f"READY_{uuid.uuid4().hex}"
        configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=model)
        _send_user_text(http_client, session_id=session_id, text="Reply with the ready marker")
        _poll_for_assistant_marker(http_client, session_id=session_id, marker=marker, timeout=60)
        previous_reply = marker

        for failure in ("tui_exit", "tmux_server_loss"):
            marker = f"RECOVERED_{uuid.uuid4().hex}"
            prompt = f"Recovery probe {failure}"
            # Codex also generates titles on this model; only the recovery
            # prompt may consume the gated response.
            configure_mock_llm(
                mock_llm_server_url, [{"text": marker, "block": True}], key=model, match=prompt
            )
            _send_user_text(http_client, session_id=session_id, text=prompt)
            _wait_for(
                lambda: httpx.get(
                    f"{mock_llm_server_url}/gate/pending", timeout=5, trust_env=False
                ).json()["pending"],
                "in-flight Codex model request",
            )
            state = _wait_for(
                lambda: s if (s := read_bridge_state(bridge_dir)) and s.active_turn_id else None,
                "active Codex turn id",
            )
            active_turn = state.active_turn_id
            socket = terminal["metadata"]["tmux_socket"]
            target = terminal["metadata"]["tmux_target"]
            pane_pid = int(_tmux(socket, "list-panes", "-t", target, "-F", "#{pane_pid}"))
            print(f"E2E injecting {failure}: pane_pid={pane_pid}, turn={active_turn}", flush=True)
            if failure == "tui_exit":
                os.kill(pane_pid, signal.SIGTERM)
            else:
                _tmux(socket, "kill-server")
            _wait_for(terminal_gone, "terminal resource eviction")
            assert _app_server_pid(state.socket_path) == original_pid

            terminal = ensure_terminal()
            recovered = read_bridge_state(bridge_dir)
            assert recovered is not None
            assert _app_server_pid(recovered.socket_path) == original_pid
            assert recovered.thread_id == original_thread
            assert recovered.active_turn_id == active_turn
            assert terminal["metadata"]["tmux_socket"] != socket
            assert (
                _tmux(terminal["metadata"]["tmux_socket"], "list-panes", "-F", "#{pane_dead}")
                == "0"
            )
            _wait_for_pane_text(terminal["metadata"]["tmux_socket"], previous_reply)
            recovered = read_bridge_state(bridge_dir)
            assert recovered is not None and recovered.active_turn_id == active_turn

            release_mock_gate(mock_llm_server_url)
            _poll_for_assistant_marker(
                http_client, session_id=session_id, marker=marker, timeout=60
            )
            _wait_for_pane_text(terminal["metadata"]["tmux_socket"], marker)
            previous_reply = marker
            items = http_client.get(
                f"/v1/sessions/{session_id}/items", params={"limit": 100}
            ).json()
            replies = [item for item in items["data"] if marker in json.dumps(item)]
            assert len(replies) == 1, "The existing forwarder must publish exactly one reply"
            requests = httpx.get(
                f"{mock_llm_server_url}/mock/requests",
                params={"key": model},
                timeout=5,
                trust_env=False,
            ).json()
            probe_requests = [
                request
                for request in requests["requests"]
                if request["input"][-1].get("role") == "user"
                and request["input"][-1].get("content") == [{"type": "input_text", "text": prompt}]
            ]
            assert len(probe_requests) == 1, "Recovery must not replay the model request"
            evidence.append(
                {
                    "failure": failure,
                    "app_server_pid": original_pid,
                    "thread_id": original_thread,
                    "turn_id": active_turn,
                    "reply": marker,
                }
            )
            print(
                f"E2E recovered {failure}: same backend/thread/turn, one forwarded reply",
                flush=True,
            )
        (tmp_path / "recovery-evidence.json").write_text(json.dumps(evidence, indent=2))
    finally:
        log_dir = Path(os.environ["OMNIGENT_DATA_DIR"]) / "logs"
        if log_dir.exists():
            shutil.copytree(log_dir, tmp_path / "logs", dirs_exist_ok=True)
        transcript = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
        (tmp_path / "transcript.json").write_text(transcript.text)
        captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=5, trust_env=False)
        (tmp_path / "model-requests.json").write_text(captured.text)
        release_mock_gate(mock_llm_server_url)
        http_client.delete(f"/v1/sessions/{session_id}", timeout=30)
