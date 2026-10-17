"""A Pi fork on a Gemini model must replay earlier tool results with their tool names.

Real server, runner and ``pi`` CLI on Pi's own Google login, routed to the local
Gemini stand-in in ``_pi_native_gemini_stand_in``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.pi_native.main import _SESSION_LABELS, _materialize_pi_agent_spec
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e._pi_native_gemini_stand_in import (
    PI_MODEL_SELECTION,
    GeminiStandIn,
    function_call_reply,
    serve_gemini_stand_in,
    text_reply,
    write_pi_own_login,
)

pytestmark = pytest.mark.timeout(900, method="signal")

TOOL_NAME = "read"
SOURCE_PROMPT = "Read alpha.txt"
FORK_PROMPT = "What did the tools return?"
TOOLS_DONE = "TOOLS-DONE"
FORK_REPLY = "FORK-RESUME-OK"


def wait_for(predicate: Callable[[], Any], timeout: float = 300.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for Pi; inspect server.log and runner.log")


@dataclass
class PiGeminiRig:
    client: httpx.Client
    base_url: str
    runner_id: str
    stand_in: GeminiStandIn
    workspace: Path


@contextmanager
def pi_gemini_rig(tmp_path: Path) -> Iterator[PiGeminiRig]:
    """Server + runner + real Pi on its own Google login routed to the stand-in."""
    pi_binary = os.environ.get("OMNIGENT_PI_PATH") or shutil.which("pi")
    for binary in (pi_binary or "pi", "node"):
        if reason := cli_unavailable_reason(binary):
            pytest.skip(reason)
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the real Pi terminal")
    workspace, config = tmp_path / "workspace", tmp_path / "config"
    for directory in (workspace, config):
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "alpha.txt").write_text("alpha-contents\n")
    stand_in = GeminiStandIn(
        replies=[
            function_call_reply(TOOL_NAME, {"path": str(workspace / "alpha.txt")}),
            text_reply(TOOLS_DONE),
            text_reply(FORK_REPLY),
        ]
    )
    # The empty config dir leaves the Pi surface without an omnigent-managed
    # provider, so the runner launches Pi on its own login.
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OMNIGENT_", "RUNNER_", "PI_", "OPENAI_", "ANTHROPIC_", "GEMINI_", "GOOGLE_")
        )
    }
    with (
        serve_gemini_stand_in(stand_in) as gemini_url,
        tempfile.TemporaryDirectory(prefix="pi-gemini-", dir="/tmp") as short_tmp,
    ):
        try:
            with (
                server_runner(
                    tmp_path / "stack",
                    server_env={"OMNIGENT_CONFIG_HOME": str(config)},
                    base_env=base_env,
                    workspace=workspace,
                ) as stack,
                httpx.Client(
                    base_url=stack.base_url,
                    trust_env=False,
                    timeout=30,
                    headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
                ) as client,
            ):
                write_pi_own_login(stack.runner_home, gemini_url)
                stack.start_runner(
                    env={
                        "TMPDIR": short_tmp,
                        "OMNIGENT_CONFIG_HOME": str(config),
                        "OMNIGENT_PI_PATH": str(pi_binary),
                    }
                )
                yield PiGeminiRig(client, stack.base_url, stack.runner_id, stand_in, workspace)
        finally:
            for socket in Path(short_tmp).rglob("tmux.sock"):
                # A hung kill must not mask the real test failure being raised.
                with suppress(subprocess.TimeoutExpired):
                    subprocess.run(
                        ["tmux", "-S", str(socket), "kill-server"], capture_output=True, timeout=10
                    )


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[PiGeminiRig]:
    with pi_gemini_rig(tmp_path) as value:
        yield value


def session_items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 1000, "order": "asc"}
    )
    response.raise_for_status()
    return response.json()["data"]


def send_message(client: httpx.Client, session_id: str, text: str) -> None:
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    response.raise_for_status()
    assert response.json()["queued"] is True


def assistant_text(item: dict[str, Any]) -> str:
    if item.get("type") != "message" or item.get("role") != "assistant":
        return ""
    content = item.get("content")
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content or [] if isinstance(block, dict))


def turn_outcome(items: list[dict[str, Any]], after: int) -> dict[str, Any] | None:
    """First error item or assistant reply committed after index *after*."""
    for item in items[after:]:
        if item.get("type") == "error":
            return {"kind": "error", "text": json.dumps(item)}
        if text := assistant_text(item):
            return {"kind": "assistant", "text": text}
    return None


def function_response_names(request_body: dict[str, Any]) -> list[str]:
    return [
        part["functionResponse"].get("name", "")
        for content in request_body.get("contents", [])
        for part in content.get("parts", [])
        if "functionResponse" in part
    ]


def last_function_response(requests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Body of the most recent request in ``requests`` that replayed a tool result.

    Searches only the supplied slice so a fork that drops tool results from its
    rebuilt history cannot fall back to an earlier turn's request and pass.
    Returns ``None`` when no request in the slice carries a ``functionResponse``.
    """
    for request in reversed(requests):
        if function_response_names(request["body"]):
            return request["body"]
    return None


def create_pi_session(client: httpx.Client, tmp_path: Path) -> str:
    spec = _materialize_pi_agent_spec(tmp_path)
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({spec.name: spec.read_bytes()}),
        metadata={"labels": dict(_SESSION_LABELS)},
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    # The picker's choice lands on the session row as model_override before launch.
    client.patch(
        f"/v1/sessions/{session_id}", json={"model_override": PI_MODEL_SELECTION}
    ).raise_for_status()
    return session_id


def run_tool_turn(rig: PiGeminiRig, session_id: str) -> list[dict[str, Any]]:
    """Bind *session_id* to the runner and drive one turn that calls the ``read`` tool."""
    bind_session_runner(rig.client.patch, "", session_id, rig.runner_id)
    send_message(rig.client, session_id, SOURCE_PROMPT)
    return wait_for(
        lambda: next(
            (
                items
                for items in [session_items(rig.client, session_id)]
                if any(TOOLS_DONE in assistant_text(item) for item in items)
            ),
            None,
        )
    )


def test_pi_native_fork_on_gemini_replays_tool_results_with_names(
    rig: PiGeminiRig, tmp_path: Path
) -> None:
    client = rig.client
    source_id = create_pi_session(client, tmp_path)
    session_ids = [source_id]
    try:
        items = run_tool_turn(rig, source_id)
        calls = [item for item in items if item["type"] == "function_call"]
        outputs = [item for item in items if item["type"] == "function_call_output"]
        assert [call["name"] for call in calls] == [TOOL_NAME]
        assert [output["call_id"] for output in outputs] == [calls[0]["call_id"]]
        # The live turn names its tool result; only the rebuilt history is in question.
        live_request = last_function_response(rig.stand_in.snapshot_requests())
        assert live_request is not None
        assert function_response_names(live_request) == [TOOL_NAME]

        fork = client.post(f"/v1/sessions/{source_id}/fork", json={})
        fork.raise_for_status()
        fork_id = fork.json()["id"]
        session_ids.append(fork_id)
        bind_session_runner(client.patch, "", fork_id, rig.runner_id)
        before = len(session_items(client, fork_id))
        requests_before_fork = len(rig.stand_in.snapshot_requests())
        send_message(client, fork_id, FORK_PROMPT)
        outcome = wait_for(lambda: turn_outcome(session_items(client, fork_id), before))

        fork_request = last_function_response(
            rig.stand_in.snapshot_requests()[requests_before_fork:]
        )
        assert fork_request is not None, outcome["text"]
        assert function_response_names(fork_request) == [TOOL_NAME], outcome["text"]
        assert outcome["kind"] == "assistant" and FORK_REPLY in outcome["text"], outcome["text"]
        # Every scripted reply was consumed in order; no unexpected extra request.
        assert rig.stand_in.fallback_served == 0, outcome["text"]
    finally:
        for session_id in reversed(session_ids):
            with suppress(httpx.HTTPError):
                client.delete(f"/v1/sessions/{session_id}")
