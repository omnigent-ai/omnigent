"""A real Pi fork must retain paired parallel tools and mixed assistant text."""

from __future__ import annotations

import json
import os
import secrets
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

from omnigent.harnesses.pi_native.main import _SESSION_LABELS, _materialize_pi_agent_spec
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.live_server import isolated_local_server, local_server_env, terminate_process
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
)

pytestmark = pytest.mark.timeout(900, method="signal")
_MODEL = "pi-mock-sonnet"
_CALL_IDS = ["toolu_par_a", "toolu_par_b"]
_MIXED_TEXT = "Reading both seeded files."


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


@pytest.fixture
def pi_fork_rig(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[httpx.Client, str, list[dict[str, Any]]]]:
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

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            assert self.path.split("?", 1)[0] == "/v1/messages"
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
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
        "providers:\n  mock-pi:\n    kind: key\n    default: [anthropic]\n"
        f"    anthropic:\n      base_url: http://127.0.0.1:{sidecar.server_port}\n"
        f"      api_key: mock-key\n      models:\n        default: {_MODEL}\n"
    )
    for key in list(os.environ):
        if key.startswith(("OMNIGENT_", "RUNNER_", "PI_", "OPENAI_", "ANTHROPIC_")):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(config))
    token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(token)
    try:
        with (
            tempfile.TemporaryDirectory(prefix="pi-fork-", dir="/tmp") as short_tmp,
            isolated_local_server(tmp_path) as base_url,
            httpx.Client(
                base_url=base_url,
                trust_env=False,
                timeout=30,
                headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            ) as client,
            (tmp_path / "runner.log").open("w") as log,
        ):
            runner = subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=local_server_env(
                    {
                        "HOME": str(home),
                        "TMPDIR": short_tmp,
                        "OMNIGENT_PI_PATH": str(pi_binary),
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                        "RUNNER_SERVER_URL": base_url,
                    }
                ),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                _wait(lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"))
                yield client, runner_id, requests
            finally:
                terminate_process(runner)
                for socket in Path(short_tmp).rglob("tmux.sock"):
                    subprocess.run(
                        ["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10
                    )
    finally:
        sidecar.shutdown()
        sidecar.server_close()
        thread.join(timeout=5)


def test_pi_native_fork_rebuild_keeps_parallel_tool_results_adjacent(
    pi_fork_rig: tuple[httpx.Client, str, list[dict[str, Any]]], tmp_path: Path
) -> None:
    client, runner_id, requests = pi_fork_rig
    spec = _materialize_pi_agent_spec(tmp_path)
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

        fork = client.post(f"/v1/sessions/{session_id}/fork", json={})
        fork.raise_for_status()
        fork_id = fork.json()["id"]
        session_ids.append(fork_id)
        bind_session_runner(client.patch, "", fork_id, runner_id)
        send_message(fork_id, "What did the tools return?")
        _wait(lambda: items_with_reply(fork_id, "FORK-RESUME-OK"))

        assert len(requests) == 3
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
        assert all(f"{name}-contents" in json.dumps(history) for name in ("alpha", "beta"))
    finally:
        for session_id in reversed(session_ids):
            with suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}")
