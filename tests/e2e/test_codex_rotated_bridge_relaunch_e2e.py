"""A real Codex /new session keeps working after its host relaunches the runner.

The server, host, runner, Codex app-server, TUI, and transports are real; only
model replies are scripted. Every process and bridge directory belongs to the
test. Run with plain pytest: this journey owns its environment.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import psutil
import pytest

from dev.repro_env.runtime import write_model_config
from omnigent.harnesses.codex_native.bridge import (
    CODEX_NATIVE_BRIDGE_ID_LABEL_KEY,
    codex_terminal_interactive,
    read_bridge_state,
)
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm
from tests.e2e.test_host_codex_native_e2e import _poll_for_assistant_marker, _send_user_text

_REPO = Path(__file__).resolve().parents[2]
_MODEL = "gpt-5.4-mini"


def _wait(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def _get(client: httpx.Client, path: str) -> dict:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def _tmux(terminal: dict, command: str, *args: str) -> str:
    metadata = terminal["metadata"]
    return subprocess.check_output(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            command,
            "-t",
            metadata["tmux_target"],
            *args,
        ],
        text=True,
        timeout=10,
    )


def _exited(process: psutil.Process) -> bool:
    try:
        return not process.is_running() or process.status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


@pytest.mark.posix_only
@pytest.mark.min_runner_version("0.19.0")
@pytest.mark.timeout(360)
@pytest.mark.parametrize("cold_bridge", [False, True], ids=["retained-state", "missing-state"])
def test_rotated_codex_session_relaunches_into_its_bridge_directory(
    tmp_path: Path, isolated_mock_llm_server_url: str, cold_bridge: bool
) -> None:
    """Native /new, runner relaunch, and chat/terminal follow-ups preserve one bridge."""
    for binary in ("codex", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock_url, "claude-sonnet-4-20250514", _MODEL)
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    native_env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CODEX_HOME": str(tmp_path / "codex-config"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "TERM": "xterm-256color",
    }
    Path(native_env["CODEX_HOME"]).mkdir()
    set_fallback_mock_llm(mock_url, key="default", text="Acknowledged.")
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    with (
        server_runner(
            tmp_path / "stack",
            server_cwd=_REPO,
            base_env=base_env,
            server_env={**native_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": None},
            poll_interval=0.1,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=90,
            headers={
                "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                "x-omnigent-background-session-titles": "off",
            },
        ) as client,
    ):
        stack.start_host(env=native_env, cwd=_REPO)
        host = _wait(
            lambda: next(
                (h for h in _get(client, "/v1/hosts")["hosts"] if h["status"] == "online"),
                None,
            ),
            "isolated host registration",
        )
        created = create_native_session(
            client,
            stack.base_url,
            harness="codex",
            model=_MODEL,
            metadata={
                "host_id": host["host_id"],
                "workspace": str(stack.workspace),
                "terminal_launch_args": ["-s", "read-only", "-a", "never"],
            },
        )
        original_id = created["session_id"]
        state_root = stack.runner_home / ".omnigent" / "codex-native"
        bridge_dir = state_root / hashlib.sha256(original_id.encode()).hexdigest()[:32]
        evidence: dict = {
            "commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=_REPO, text=True
            ).strip(),
            "codex_version": subprocess.check_output(["codex", "--version"], text=True).strip(),
            "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "original_session_id": original_id,
            "cold_bridge": cold_bridge,
            "turns": [],
        }

        def ensure_terminal(session_id: str) -> dict:
            response = client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
            )
            assert response.is_success, f"{response.text}\n{stack.log_tail()}"
            return response.json()

        def round_trip(session_id: str, phase: str, terminal: dict | None = None) -> None:
            prompt = f"ROTATED_BRIDGE_{phase}_{uuid.uuid4().hex}"
            reply = f"ACK_{phase}_{uuid.uuid4().hex}"
            configure_mock_llm(mock_url, [{"text": reply}], key="bridge-relaunch", match=prompt)
            set_fallback_mock_llm(mock_url, key="bridge-relaunch", text=reply)
            if terminal is None:
                _send_user_text(client, session_id=session_id, text=prompt)
            else:
                _wait(
                    lambda: codex_terminal_interactive(
                        bridge_dir, _tmux(terminal, "capture-pane", "-p")
                    ),
                    "relaunched native terminal to render its input",
                )
                _tmux(terminal, "send-keys", "-l", prompt)
                time.sleep(0.3)
                _tmux(terminal, "send-keys", "Enter")
            _poll_for_assistant_marker(client, session_id=session_id, marker=reply, timeout=90)
            requests = [
                r for r in get_mock_requests(mock_url) if prompt in json.dumps(r.get("input"))
            ]
            assert requests, "The real Codex CLI never sent the prompt to the model endpoint"
            _wait(
                lambda: (
                    (s := read_bridge_state(bridge_dir)) is not None and s.active_turn_id is None
                ),
                "idle Codex thread",
            )
            snapshot = _get(client, f"/v1/sessions/{session_id}")
            assert not snapshot.get("last_task_error"), snapshot
            evidence["turns"].append(
                {"phase": phase, "prompt": prompt, "reply": reply, "snapshot": snapshot}
            )
            (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
            print(f"{phase}: real Codex reply persisted for {session_id}", flush=True)

        terminal = ensure_terminal(original_id)
        original = _wait(lambda: read_bridge_state(bridge_dir), "original bridge state")
        # Codex's placeholder text varies by version; the runner's own check doesn't.
        _wait(
            lambda: codex_terminal_interactive(bridge_dir, _tmux(terminal, "capture-pane", "-p")),
            "initial native terminal to render its input",
        )
        round_trip(original_id, "original")
        (tmp_path / "before-new.txt").write_text(_tmux(terminal, "capture-pane", "-p"))
        _tmux(terminal, "send-keys", "-l", "/new")
        time.sleep(0.3)
        _tmux(terminal, "send-keys", "Enter")
        rotated = _wait(
            lambda: (
                (s := read_bridge_state(bridge_dir)) is not None
                and s.session_id != original_id
                and s
            ),
            "native /new to rotate the Omnigent session",
        )
        rotated_id = rotated.session_id
        assert rotated.thread_id != original.thread_id
        labels = _get(client, f"/v1/sessions/{rotated_id}/labels")
        assert labels["labels"][CODEX_NATIVE_BRIDGE_ID_LABEL_KEY] == original_id, labels
        evidence["rotated_session_id"] = rotated_id
        evidence["bridge_directory"] = str(bridge_dir)
        round_trip(rotated_id, "rotated")
        before_stop = _get(client, f"/v1/sessions/{rotated_id}")
        old_runner_id = before_stop["runner_id"]
        launched = re.search(
            rf"Runner started: {re.escape(old_runner_id)} \(pid=(\d+)\)",
            stack.log_path("host").read_text(),
        )
        assert launched is not None, "The host must identify the runner this test owns"
        registered = json.loads((state_root / "process-registry.json").read_text())
        assert registered, "The real Codex app-server must be registered before stopping"
        owned_pids = {int(launched.group(1)), *(entry["pid"] for entry in registered)}
        owned_pids.add(int(_tmux(terminal, "display-message", "-p", "#{pane_pid}").strip()))
        owned_processes = [psutil.Process(pid) for pid in owned_pids]
        stopped = client.post(f"/v1/sessions/{rotated_id}/events", json={"type": "stop_session"})
        stopped.raise_for_status()
        _wait(
            lambda: not _get(client, f"/v1/runners/{old_runner_id}/status")["online"],
            "Stop session to terminate its host-launched runner",
        )
        _wait(
            lambda: all(_exited(process) for process in owned_processes),
            "owned runner, app-server, and native terminal to exit before bridge deletion",
        )
        evidence["stopped_process_ids"] = sorted(owned_pids)
        if cold_bridge:
            shutil.rmtree(bridge_dir)
        round_trip(rotated_id, "reconnected")
        relaunched = _get(client, f"/v1/sessions/{rotated_id}")
        assert relaunched["runner_id"] != old_runner_id
        terminal = ensure_terminal(rotated_id)
        resumed = _wait(lambda: read_bridge_state(bridge_dir), "recreated labelled bridge state")
        assert resumed.session_id == rotated_id
        assert resumed.thread_id == rotated.thread_id
        wrong_dir = state_root / hashlib.sha256(rotated_id.encode()).hexdigest()[:32]
        assert not wrong_dir.exists(), (
            "Relaunch created state where the rotated executor never reads"
        )
        round_trip(rotated_id, "terminal", terminal)
        (tmp_path / "after-reconnect.txt").write_text(_tmux(terminal, "capture-pane", "-p"))
        (tmp_path / "model-requests.json").write_text(
            json.dumps(get_mock_requests(mock_url), indent=2)
        )
        evidence["relaunched_runner_id"] = relaunched["runner_id"]
        evidence["final_snapshot"] = _get(client, f"/v1/sessions/{rotated_id}")
        (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
