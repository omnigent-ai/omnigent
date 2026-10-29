"""A real Pi fork must retain paired parallel tools and mixed assistant text."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.pi_native.bridge import bridge_dir_for_session_id
from omnigent.harnesses.pi_native.main import _SESSION_LABELS, _materialize_pi_agent_spec
from omnigent.inner._proc import process_alive
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.live_server import terminate_process
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
    sse_text_response,
)

pytestmark = pytest.mark.timeout(900, method="signal")
_MODEL = "pi-mock-sonnet"
_CALL_IDS = ["toolu_par_a", "toolu_par_b"]
_MIXED_TEXT = "Reading both seeded files."
_RESPONSES_MODEL = "gpt-mock-resume"
_CONTEXT_BYTE_LIMIT = 16_384
_RETAINED_CALL_IDS = ["toolu_tail_a", "toolu_tail_b"]
_RECOVERED_OUTPUT = "Contents recovered from the missing call."
_CHECKPOINT_SUMMARY = (
    "RECOVERY-CHECKPOINT: read alpha-contents and beta-contents; do not repeat tools."
)
PiForkRig = tuple[httpx.Client, ServerRunner, list[dict[str, Any]], list[str]]


def _wait(predicate: Callable[[], Any]) -> Any:
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for Pi; inspect server.log and runner.log")


def _parallel_response(workspace: Path) -> str:
    stream = anthropic_sse_tool_call_response(
        [
            {"call_id": call_id, "name": "read", "arguments": json.dumps({"path": str(path)})}
            for call_id, path in zip(_CALL_IDS, sorted(workspace.glob("*.txt")), strict=True)
        ],
        model=_MODEL,
    )
    # Append a text block to the same assistant response as the two tool calls.
    text_events = "\n\n".join(
        event.replace('"index": 0', '"index": 2')
        for event in anthropic_sse_text_response(_MIXED_TEXT).split("\n\n")
        if event.startswith("event: content_block_")
    )
    return stream.replace("event: message_delta", f"{text_events}\n\nevent: message_delta")


def _is_summarizer_request(body: dict[str, Any]) -> bool:
    return "context summarization assistant" in json.dumps(body.get("system", ""))


@pytest.fixture
def pi_fork_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> Iterator[PiForkRig]:
    pi_binary = os.environ.get("OMNIGENT_PI_PATH") or shutil.which("pi")
    for binary in (pi_binary or "pi", "node"):
        if reason := cli_unavailable_reason(binary):
            pytest.skip(reason)
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real Pi terminal")
    workspace, config, home = (tmp_path / name for name in ("workspace", "config", "home"))
    for directory in (workspace, config, home):
        directory.mkdir()
    for name in ("alpha", "beta"):
        (workspace / f"{name}.txt").write_text(f"{name}-contents\n")
    replies = [
        _parallel_response(workspace),
        anthropic_sse_text_response("PARALLEL-TOOLS-DONE", model=_MODEL),
        anthropic_sse_text_response("FORK-RESUME-OK", model=_MODEL),
    ]
    requests: list[dict[str, Any]] = []
    auth_failures: list[str] = []
    command_auth = getattr(request, "param", False) is True
    bounded_context = getattr(request, "param", None) == "capacity"
    token_file = workspace / "token"
    mode_file = workspace / "auth-mode"
    token_file.write_text("mock-key")
    mode_file.write_text("ok")
    auth = {"api_key": "mock-key"}
    if command_auth:
        helper = workspace / "token-helper.py"
        helper.write_text(
            "import json,os,pathlib,sys,time\n"
            "root = pathlib.Path(__file__).parent\n"
            "with (root / 'auth-pids.jsonl').open('a') as f:\n"
            " row = {'helper':os.getpid(),'pi':os.environ.get('OMNIGENT_PI_NATIVE_PID')}\n"
            " f.write(json.dumps(row) + '\\n')\n"
            "mode = (root / 'auth-mode').read_text()\n"
            "if mode == 'slow': time.sleep(40)\n"
            "if mode == 'failed_exit':\n"
            " print('PRIVATE-HELPER-ERROR',file=sys.stderr); sys.exit(7)\n"
            "if mode == 'empty': sys.exit(0)\n"
            "print((root / 'token').read_text())\n"
        )
        # Keep a shell after the helper so cleanup must reach its process group.
        auth = {
            "auth_command": shlex.join([sys.executable, str(helper)]) + "; code=$?; exit $code"
        }
        replies[2:] = [
            anthropic_sse_text_response(f"AUTH-RECOVERED-{i}", model=_MODEL) for i in range(3)
        ]

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            assert path in {"/v1/messages", "/v1/responses"}
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bearer = self.headers.get("Authorization")
            api_key = self.headers.get("X-Api-Key")
            expected = token_file.read_text()
            if bearer != f"Bearer {expected}" and api_key != expected:
                auth_failures.append("provider received an unexpected credential")
                self.send_error(401, "Unexpected test credential")
                return
            requests.append(body)
            message_bytes = len(json.dumps(body.get("messages", [])).encode())
            if bounded_context and message_bytes > _CONTEXT_BYTE_LIMIT:
                payload = json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "invalid_request_error",
                            "message": (
                                f"Context capacity exceeded: {message_bytes} > "
                                f"{_CONTEXT_BYTE_LIMIT}"
                            ),
                        },
                    }
                ).encode()
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if command_auth and len(requests) == 1:
                token_file.write_text("rotated-during-tools")
            if path == "/v1/responses":
                pending = set()
                for item in body["input"]:
                    if item.get("type") == "function_call":
                        pending.add(item["call_id"])
                    elif item.get("type") == "function_call_output":
                        if item["call_id"] not in pending:
                            self.send_error(400, "No tool call found for function call output")
                            return
                        pending.remove(item["call_id"])
                payload = sse_text_response(
                    f"RESPONSES-RESUME-{len(requests)}", model=_RESPONSES_MODEL, stream=True
                ).encode()
            elif _is_summarizer_request(body):
                # Pi may summarize a split turn with a second request.
                payload = anthropic_sse_text_response(_CHECKPOINT_SUMMARY, model=_MODEL).encode()
            else:
                payload = replies.pop(0).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    sidecar = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=sidecar.serve_forever, daemon=True)
    thread.start()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "providers": {
                    "mock-pi": {
                        "kind": "gateway" if command_auth else "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": f"http://127.0.0.1:{sidecar.server_port}",
                            **auth,
                            "models": {"default": _MODEL},
                        },
                        "openai": {
                            "base_url": f"http://127.0.0.1:{sidecar.server_port}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _RESPONSES_MODEL},
                        },
                    }
                }
            }
        )
    )
    for key in list(os.environ):
        if key.startswith(("OMNIGENT_", "RUNNER_", "PI_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(config))
    monkeypatch.setenv("OMNIGENT_PI_PATH", str(pi_binary))
    try:
        with (
            tempfile.TemporaryDirectory(prefix="pi-fork-", dir="/tmp") as short_tmp,
            server_runner(
                tmp_path,
                workspace=workspace,
                server_env={"OMNIGENT_CONFIG_HOME": str(config)},
            ) as stack,
            httpx.Client(
                base_url=stack.base_url,
                trust_env=False,
                timeout=30,
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            ) as client,
        ):
            stack.start_runner(
                env={
                    "OMNIGENT_CONFIG_HOME": str(config),
                    "TMPDIR": short_tmp,
                    "OMNIGENT_PI_PATH": str(pi_binary),
                }
            )
            try:
                yield client, stack, requests, replies
            finally:
                (tmp_path / "provider-requests.json").write_text(json.dumps(requests, indent=2))
                terminate_process(stack.runner)
                for socket in Path(short_tmp).rglob("tmux.sock"):
                    subprocess.run(
                        ["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10
                    )
    finally:
        sidecar.shutdown()
        sidecar.server_close()
        thread.join(timeout=5)
        assert not auth_failures, auth_failures


@pytest.mark.parametrize("resume_api", ["anthropic", "responses"])
def test_pi_native_fork_rebuild_keeps_parallel_tool_results_adjacent(
    pi_fork_rig: PiForkRig, tmp_path: Path, resume_api: str
) -> None:
    client, stack, requests, _ = pi_fork_rig
    runner_id = stack.runner_id
    spec = _materialize_pi_agent_spec(tmp_path)
    instructions = "OMNI_PI_FORK_ROLE act as the chief of staff."
    spec.write_text(
        spec.read_text().replace("Pi is running in the session terminal.", instructions)
    )
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({spec.name: spec.read_bytes()}),
        metadata={"labels": dict(_SESSION_LABELS)},
    )
    created.raise_for_status()
    session_ids = [created.json()["session_id"]]

    def items_with_reply(session_id: str, text: str) -> list[dict[str, Any]]:
        response = client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        items = response.json()["data"]
        assert not [item for item in items if item["type"] == "error"], items
        return (
            items
            if any(
                item.get("role") == "assistant" and text in json.dumps(item.get("content"))
                for item in items
            )
            else []
        )

    def send_message(session_id: str, text: str) -> None:
        response = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        )
        response.raise_for_status()
        assert response.json()["queued"] is True

    try:
        session_id = session_ids[0]
        bind_session_runner(client.patch, "", session_id, runner_id)
        send_message(session_id, "Read both files.")
        items = _wait(lambda: items_with_reply(session_id, "PARALLEL-TOOLS-DONE"))
        calls = [i for i, item in enumerate(items) if item["type"] == "function_call"]
        outputs = [i for i, item in enumerate(items) if item["type"] == "function_call_output"]
        assert [items[i]["call_id"] for i in calls] == _CALL_IDS
        assert sorted(items[i]["call_id"] for i in outputs) == _CALL_IDS
        assert max(calls) < min(outputs), "Both calls must precede their outputs"
        assert _MIXED_TEXT in json.dumps(items)

        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "response_id": "lost-call-turn",
                    "item_type": "function_call_output",
                    "item_data": {"call_id": "toolu_missing", "output": _RECOVERED_OUTPUT},
                },
            },
        ).raise_for_status()
        fork = client.post(
            f"/v1/sessions/{session_id}/fork",
            json={"model_override": _RESPONSES_MODEL} if resume_api == "responses" else {},
        )
        fork.raise_for_status()
        fork_id = fork.json()["id"]
        session_ids.append(fork_id)
        bind_session_runner(client.patch, "", fork_id, runner_id)
        send_message(fork_id, "What did the tools return?")
        reply = "RESPONSES-RESUME-3" if resume_api == "responses" else "FORK-RESUME-OK"
        _wait(lambda: items_with_reply(fork_id, reply))

        assert len(requests) == 3
        assert all(json.dumps(request).count(instructions) == 1 for request in requests)
        if resume_api == "responses":

            def assert_responses_history() -> None:
                history = requests[-1]["input"]
                calls = [item for item in history if item.get("type") == "function_call"]
                results = [item for item in history if item.get("type") == "function_call_output"]
                assert sorted(item["call_id"] for item in calls) == _CALL_IDS
                assert sorted(item["call_id"] for item in results) == _CALL_IDS
                assert all(item["name"] == "read" for item in calls)
                assert json.dumps(history).count(_RECOVERED_OUTPUT) == 1
                assert "Recovered tool output" in json.dumps(history)
                assert all(f"{name}-contents" in json.dumps(history) for name in ("alpha", "beta"))

            assert_responses_history()
            terminal_url = f"/v1/sessions/{fork_id}/resources/terminals"
            terminal = client.get(f"{terminal_url}/terminal_pi_main").json()["metadata"]
            client.delete(f"{terminal_url}/terminal_pi_main").raise_for_status()
            _wait(
                lambda: (
                    subprocess.run(
                        [
                            "tmux",
                            "-S",
                            terminal["tmux_socket"],
                            "has-session",
                            "-t",
                            terminal["tmux_target"],
                        ],
                        capture_output=True,
                        timeout=10,
                    ).returncode
                    != 0
                )
            )
            bridge = (
                tmp_path / "home/.omnigent/pi-native" / bridge_dir_for_session_id(fork_id).name
            )
            session_files = list((bridge / "sessions").glob("*.jsonl"))
            assert session_files, "The fork must have written a native session before cold resume"
            for session_file in session_files:
                session_file.unlink()
            client.post(
                terminal_url,
                json={"terminal": "pi", "session_key": "main", "ensure_native_terminal": True},
            ).raise_for_status()
            send_message(fork_id, "Continue after a cold resume without rerunning the tools.")
            _wait(lambda: items_with_reply(fork_id, "RESPONSES-RESUME-4"))
            assert len(requests) == 4
            assert_responses_history()
            return
        history = requests[-1]["messages"]
        uses, results = [], []
        previous_uses: list[str] = []
        for message in history:
            blocks = message["content"] if isinstance(message["content"], list) else []
            current_uses = [block["id"] for block in blocks if block["type"] == "tool_use"]
            current_results = [
                block["tool_use_id"] for block in blocks if block["type"] == "tool_result"
            ]
            if current_results:
                assert message["role"] == "user"
                assert sorted(current_results) == sorted(previous_uses), history
            previous_uses = current_uses if message["role"] == "assistant" else []
            uses.extend(current_uses)
            results.extend(current_results)
        assert sorted(uses) == sorted(results) == _CALL_IDS
        assert _MIXED_TEXT in json.dumps(history)
        assert _RECOVERED_OUTPUT in json.dumps(history)
        assert "Recovered tool output" in json.dumps(history)
        assert all(f"{name}-contents" in json.dumps(history) for name in ("alpha", "beta"))
    finally:
        for session_id in reversed(session_ids):
            with suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}")


@pytest.mark.parametrize("pi_fork_rig", ["capacity"], indirect=True)
@pytest.mark.parametrize("restart_scope", ["process", "runner", "missing-files"])
def test_pi_native_compacted_restart_then_fork(
    pi_fork_rig: PiForkRig, tmp_path: Path, restart_scope: str
) -> None:
    """Real Pi compacts tool history, restarts, continues, then forks independently."""
    client, stack, requests, replies = pi_fork_rig
    for name in ("alpha", "beta"):
        (stack.workspace / f"{name}.txt").write_text(
            f"{name}-contents\n"
            + "\n".join(
                f"build step {number:04}: artifact {name}-{number:04} verified; checksum preserved"
                for number in range(60)
            )
        )
    summary = _CHECKPOINT_SUMMARY
    replies[2:] = [
        anthropic_sse_text_response(text, model=_MODEL)
        for text in (
            "READY-TO-COMPACT",
            "SECOND-TOOLS-DONE",
            "TAIL-TOOLS-DONE",
            "COMPACT-CONTINUED",
            "RESTART-CONTINUED",
            "FORK-CONTINUED",
        )
    ]
    replies.insert(3, _parallel_response(stack.workspace).replace("toolu_par_", "toolu_next_"))
    replies.insert(5, _parallel_response(stack.workspace).replace("toolu_par_", "toolu_tail_"))
    settings = stack.runner_home / ".pi/agent/settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"compaction": {"keepRecentTokens": 100}}))
    spec = _materialize_pi_agent_spec(tmp_path)
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({spec.name: spec.read_bytes()}),
        metadata={"labels": dict(_SESSION_LABELS)},
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    session_ids = [session_id]
    terminal_url = f"/v1/sessions/{session_id}/resources/terminals"
    bridge = stack.runner_home / ".omnigent/pi-native" / bridge_dir_for_session_id(session_id).name

    def items(target: str) -> list[dict[str, Any]]:
        response = client.get(
            f"/v1/sessions/{target}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        result = response.json()["data"]
        assert not [item for item in result if item["type"] == "error"], result
        return result

    def turn(target: str, prompt: str, reply: str) -> None:
        send_user_text(client, session_id=target, text=prompt)
        _wait(lambda: reply in json.dumps(items(target)))
        assert (
            sum(
                item.get("role") == "assistant" and reply in json.dumps(item.get("content"))
                for item in items(target)
            )
            == 1
        )

    def pane(*args: str) -> str:
        return subprocess.check_output(
            [
                "tmux",
                "-S",
                terminal["tmux_socket"],
                args[0],
                "-t",
                terminal["tmux_target"],
                *args[1:],
            ],
            text=True,
            timeout=10,
        )

    try:
        bind_session_runner(client.patch, "", session_id, stack.runner_id)
        turn(session_id, "Read both files.", "PARALLEL-TOOLS-DONE")
        turn(
            session_id,
            "Keep both file results for the handoff. " * 25,
            "READY-TO-COMPACT",
        )
        client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "compact", "data": {}}
        ).raise_for_status()
        session_file = next((bridge / "sessions").glob("*.jsonl"))
        _wait(lambda: '"type":"compaction"' in session_file.read_text())
        turn(session_id, "Read both files again for a second review.", "SECOND-TOOLS-DONE")
        for name in ("alpha", "beta"):
            (stack.workspace / f"{name}.txt").write_text(f"{name}-contents retained tail\n")
        turn(session_id, "Read the updated files for the retained handoff.", "TAIL-TOOLS-DONE")
        client.post(
            f"/v1/sessions/{session_id}/events", json={"type": "compact", "data": {}}
        ).raise_for_status()
        _wait(lambda: session_file.read_text().count('"type":"compaction"') == 2)
        turn(session_id, "Continue from the compacted checkpoint.", "COMPACT-CONTINUED")
        compacted = session_file.read_bytes()
        assert summary in compacted.decode()
        native_entries = [json.loads(line) for line in compacted.decode().splitlines()]
        native_checkpoints = [entry for entry in native_entries if entry["type"] == "compaction"]
        checkpoints = [item for item in items(session_id) if item["type"] == "compaction"]
        assert len(checkpoints) == 2
        assert [item["window_id"] for item in checkpoints] == [
            entry["id"] for entry in native_checkpoints
        ]
        assert [item["summary"] for item in checkpoints] == [
            entry["summary"] for entry in native_checkpoints
        ]
        latest = native_checkpoints[-1]
        first_kept = next(
            i
            for i, entry in enumerate(native_entries)
            if entry.get("id") == latest["firstKeptEntryId"]
        )
        expected_tail = [
            entry["message"]
            for entry in native_entries[first_kept : native_entries.index(latest)]
            if entry["type"] == "message"
        ]
        snapshot = checkpoints[-1]["compacted_messages"]
        assert {item["type"] for item in snapshot} <= {
            "message",
            "function_call",
            "function_call_output",
        }
        assert summary in snapshot[0]["content"][0]["text"]
        assert [item["call_id"] for item in snapshot if item["type"] == "function_call"] == [
            block["id"]
            for message in expected_tail
            if message["role"] == "assistant"
            for block in message["content"]
            if block["type"] == "toolCall"
        ]
        retained_results = [
            (message["toolCallId"], message["content"][0]["text"])
            for message in expected_tail
            if message["role"] == "toolResult"
        ]
        assert [
            (item["call_id"], item["output"])
            for item in snapshot
            if item["type"] == "function_call_output"
        ] == retained_results
        assert sorted(call_id for call_id, _ in retained_results) == _RETAINED_CALL_IDS
        (tmp_path / "before-restart.jsonl").write_bytes(compacted)
        first_summarizer = next(request for request in requests if _is_summarizer_request(request))
        assert all(
            f"{name}-contents" in json.dumps(first_summarizer) for name in ("alpha", "beta")
        )
        _wait(lambda: client.get(f"/v1/sessions/{session_id}").json()["status"] == "idle")
        before = client.get(f"/v1/sessions/{session_id}").json()
        terminal = client.get(f"{terminal_url}/terminal_pi_main").json()["metadata"]
        old_pid = pane("display-message", "-p", "#{pane_pid}").strip()
        (tmp_path / "before-restart-terminal.txt").write_text(
            pane("capture-pane", "-p", "-S", "-")
        )
        assert stack.runner is not None
        old_runner_pid = stack.runner.pid
        if restart_scope == "runner":
            # Leave the idle native pane alive when its owning runner disappears.
            assert process_alive(int(old_pid))
            print(
                f"Runner loss: idle Pi pane {old_pid} alive before killing runner {old_runner_pid}"
            )
            stack.runner.kill()
            stack.runner.wait(timeout=10)
            _wait(lambda: not client.get(f"/v1/runners/{stack.runner_id}/status").json()["online"])
            stack.runner = None
            stack.start_runner(
                env={
                    "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
                    "OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH") or shutil.which("pi"),
                    "TMPDIR": str(Path(terminal["tmux_socket"]).parents[1]),
                }
            )
            assert stack.runner is not None and stack.runner.pid != old_runner_pid
        else:
            client.delete(f"{terminal_url}/terminal_pi_main").raise_for_status()
            _wait(lambda: not process_alive(int(old_pid)))
        if restart_scope == "missing-files":
            session_file.unlink()
        else:
            assert session_file.read_bytes() == compacted
        client.post(
            terminal_url,
            json={"terminal": "pi", "session_key": "main", "ensure_native_terminal": True},
        ).raise_for_status()
        terminal = client.get(f"{terminal_url}/terminal_pi_main").json()["metadata"]
        new_pid = pane("display-message", "-p", "#{pane_pid}").strip()
        assert new_pid != old_pid
        assert not process_alive(int(old_pid)), "Production recovery must reap the old Pi pane"
        turn(session_id, "Continue using the two file results.", "RESTART-CONTINUED")
        resumed = json.dumps(requests[-1]["messages"])
        assert all(f"{name}-contents" in resumed for name in ("alpha", "beta"))
        assert summary in resumed
        assert "Read both files." not in resumed
        assert "build step 0059" not in resumed
        if restart_scope != "missing-files":
            assert session_file.read_bytes().startswith(compacted)
        assert (
            client.get(f"/v1/sessions/{session_id}").json()["external_session_id"]
            == before["external_session_id"]
        )
        parent_items = items(session_id)
        fork = client.post(f"/v1/sessions/{session_id}/fork", json={})
        fork.raise_for_status()
        fork_id = fork.json()["id"]
        session_ids.append(fork_id)
        bind_session_runner(client.patch, "", fork_id, stack.runner_id)
        turn(fork_id, "Continue independently using the same file results.", "FORK-CONTINUED")
        history = json.dumps(requests[-1]["messages"])
        assert all(
            text in history for text in ("alpha-contents", "beta-contents", "RESTART-CONTINUED")
        )
        assert summary in history
        assert "build step 0059" not in history
        for request in requests[-3:]:
            messages = request["messages"]
            tool_blocks = [
                block
                for message in messages
                for block in message["content"]
                if isinstance(block, dict)
            ]
            assert (
                sorted(block["id"] for block in tool_blocks if block["type"] == "tool_use")
                == _RETAINED_CALL_IDS
            )
            assert (
                sorted(
                    block["tool_use_id"] for block in tool_blocks if block["type"] == "tool_result"
                )
                == _RETAINED_CALL_IDS
            )
            for index, message in enumerate(messages):
                results = [
                    block["tool_use_id"]
                    for block in message["content"]
                    if isinstance(block, dict) and block["type"] == "tool_result"
                ]
                if results:
                    assert sorted(results) == sorted(
                        block["id"]
                        for block in messages[index - 1]["content"]
                        if block["type"] == "tool_use"
                    )
        assert items(session_id) == parent_items
        assert (
            client.get(f"/v1/sessions/{fork_id}").json()["external_session_id"]
            != before["external_session_id"]
        )
        for target in session_ids:
            transcript = items(target)
            for kind in ("function_call", "function_call_output"):
                assert sorted(
                    item["call_id"] for item in transcript if item["type"] == kind
                ) == sorted([*_CALL_IDS, "toolu_next_a", "toolu_next_b", *_RETAINED_CALL_IDS])
            (tmp_path / f"{target}-items.json").write_text(json.dumps(transcript, indent=2))
        assert len([request for request in requests if not _is_summarizer_request(request)]) == 10
        sizes = [len(json.dumps(request["messages"]).encode()) for request in requests]
        assert max(sizes) <= _CONTEXT_BYTE_LIMIT
        print(f"Constant context limit {_CONTEXT_BYTE_LIMIT} bytes; request sizes: {sizes}")
        (tmp_path / "after-restart-terminal.txt").write_text(pane("capture-pane", "-p", "-S", "-"))
        print(
            f"{restart_scope}: Pi pane {old_pid} -> {new_pid}; "
            f"runner {old_runner_pid} -> {stack.runner.pid}; compact/continue/fork passed"
        )
    finally:
        for target in reversed(session_ids):
            with suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{target}")


@pytest.mark.parametrize("pi_fork_rig", [True], indirect=True, ids=["token-command"])
def test_pi_native_token_failure_recovers_without_replaying_tools(
    pi_fork_rig: PiForkRig, tmp_path: Path
) -> None:
    """Real server/runner/Pi: refresh, classified failures, cleanup and same-PID recovery."""
    client, stack, requests, _ = pi_fork_rig
    runner_id = stack.runner_id
    workspace = tmp_path / "workspace"
    spec = _materialize_pi_agent_spec(tmp_path)
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({spec.name: spec.read_bytes()}),
        metadata={"labels": dict(_SESSION_LABELS)},
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]

    def items() -> list[dict[str, Any]]:
        response = client.get(
            f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
        )
        response.raise_for_status()
        return response.json()["data"]

    def send(text: str) -> None:
        response = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
            },
        )
        response.raise_for_status()
        assert response.json()["queued"]

    def native_pids() -> set[str]:
        return {
            row["pi"]
            for line in (workspace / "auth-pids.jsonl").read_text().splitlines()
            if (row := json.loads(line))["pi"]
        }

    try:
        bind_session_runner(client.patch, "", session_id, runner_id)
        send("Read both files.")
        _wait(lambda: "PARALLEL-TOOLS-DONE" in json.dumps(items()))
        assert len(requests) == 2
        pids = native_pids()
        assert len(pids) == 1
        for i, (mode, kind) in enumerate(
            [
                ("failed_exit", "failed_exit"),
                ("empty", "empty"),
                ("slow", "timeout"),
            ]
        ):
            (workspace / "auth-mode").write_text(mode)
            before = len(requests)
            old_errors = {item["id"] for item in items() if item["type"] == "error"}
            start = time.monotonic()
            send(f"Check authentication {i}.")
            error = _wait(
                lambda old_errors=old_errors: next(
                    (
                        item
                        for item in items()
                        if item["type"] == "error" and item["id"] not in old_errors
                    ),
                    None,
                )
            )
            assert error["code"] == f"pi_auth_{kind}", (
                error,
                [json.loads(p.read_text()) for p in tmp_path.rglob("*.result.json")],
            )
            assert time.monotonic() - start < 20
            assert len(requests) == before
            assert "PRIVATE-HELPER-ERROR" not in json.dumps(items())
            if mode == "slow":
                helper = json.loads((workspace / "auth-pids.jsonl").read_text().splitlines()[-1])[
                    "helper"
                ]
                _wait(lambda helper=helper: not process_alive(helper))
            (workspace / "auth-mode").write_text("ok")
            send(f"Continue after authentication recovered {i}.")
            _wait(lambda i=i: f"AUTH-RECOVERED-{i}" in json.dumps(items()))
            assert len(requests) == before + 1
            assert native_pids() == pids
        history = json.dumps(requests[-1]["messages"])
        for name in ("alpha", "beta"):
            assert f"{name}-contents" in history
        calls = [item for item in items() if item["type"] == "function_call"]
        assert sorted(item["call_id"] for item in calls) == _CALL_IDS
    finally:
        with suppress(httpx.HTTPError):
            client.delete(f"/v1/sessions/{session_id}")
