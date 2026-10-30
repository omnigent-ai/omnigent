"""Exercise queued tool calls over real HTTP with title and agent requests.

A model can serve both session titles and agent turns. A title request must
leave a scripted tool call for the agent that advertises that tool. These
tests start the standalone mock server and use the same model-keyed queue
configuration as UI Repros, without an explicit tool guard.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.server.integration import mock_llm_server


@pytest.fixture(scope="module")
def mock_http_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    log_path = tmp_path_factory.mktemp("mock-tool-routing") / "server.log"
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    for name in ("OMNIGENT_REPRO_ATTEMPT_DIR", "OMNIGENT_REPRO_EVIDENCE_ROOT"):
        env.pop(name, None)
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(mock_llm_server.__file__).resolve()), str(port)],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
        )
        try:
            with httpx.Client(base_url=url, trust_env=False, timeout=1.0) as client:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and process.poll() is None:
                    try:
                        if client.get("/stats").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.05)
                else:
                    pytest.fail(f"Mock server did not start:\n{log_path.read_text()}")
            yield url
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("endpoint", ["/v1/messages", "/v1/responses", "/v1/chat/completions"])
@pytest.mark.parametrize("stream", [False, True])
def test_title_then_tool_turn_over_http(mock_http_url, endpoint, stream):
    model = "bundled-skill-none-http"
    tool_name = "Skill"
    tool_call = {"call_id": "toolu_skill", "name": tool_name, "arguments": '{"skill":"bundled"}'}
    with httpx.Client(base_url=mock_http_url, trust_env=False, timeout=5.0) as client:
        client.post("/mock/reset").raise_for_status()
        # Preserve the original Repro's model-only configuration and fallback.
        client.post(
            "/mock/configure", json={"key": model, "responses": [{"tool_calls": [tool_call]}]}
        ).raise_for_status()
        client.post(
            "/mock/set_fallback", json={"key": model, "text": "skill-turn-done"}
        ).raise_for_status()
        user = "Run your bundled skill using the native tool, then stop."
        title_body = {
            "model": model,
            "max_tokens": 32000,
            "stream": stream,
            "messages": [{"role": "user", "content": f"<session>{user}</session>"}],
            "system": "You are naming a coding session.",
        }
        if endpoint == "/v1/responses":
            title_body.pop("messages")
            title_body["input"] = f"<session>{user}</session>"
        title = client.post(endpoint, json=title_body)
        title.raise_for_status()
        assert "toolu_skill" not in title.text, "Title request consumed the agent's tool call"
        assert "Mock LLM response" in title.text

        schema = {"name": tool_name, "parameters": {"type": "object"}}
        if endpoint == "/v1/messages":
            tool = {"name": tool_name, "input_schema": schema["parameters"]}
        elif endpoint == "/v1/chat/completions":
            tool = {"type": "function", "function": schema}
        else:
            tool = {"type": "function", **schema}
        agent_body = {**title_body, "system": "Use your bundled skill.", "tools": [tool]}
        if endpoint == "/v1/responses":
            agent_body["input"] = user
        else:
            agent_body["messages"] = [{"role": "user", "content": user}]
        turn = client.post(endpoint, json=agent_body)
        turn.raise_for_status()
        assert "toolu_skill" in turn.text
        assert tool_name in turn.text
        wrapup = client.post(endpoint, json=agent_body)
        wrapup.raise_for_status()
        assert "skill-turn-done" in wrapup.text
        assert "toolu_skill" not in wrapup.text
        captured = client.get("/mock/requests").json()["requests"]
        assert captured == [title_body, agent_body, agent_body]


@pytest.mark.parametrize("guard", [{}, {"required_tools": ["probe__echo"]}])
def test_unadvertised_tool_still_reaches_allowlist_test_over_http(mock_http_url, guard):
    """Allowlist tests must receive their deliberately unadvertised tool call."""
    with httpx.Client(base_url=mock_http_url, trust_env=False, timeout=5.0) as client:
        client.post("/mock/reset").raise_for_status()
        client.post(
            "/mock/configure",
            json={
                "key": "sidecar-allowlist",
                **guard,
                "responses": [
                    {
                        "tool_calls": [
                            {
                                "call_id": "forbidden-call",
                                "name": "probe__danger",
                                "arguments": "{}",
                            }
                        ]
                    }
                ],
            },
        ).raise_for_status()
        request = {"model": "sidecar-allowlist", "input": "Run the tool on this turn."}
        title = client.post("/v1/responses", json=request)
        title.raise_for_status()
        assert "forbidden-call" not in title.text
        agent = client.post(
            "/v1/responses",
            json={**request, "tools": [{"type": "function", "name": "probe__echo"}]},
        )
        agent.raise_for_status()
        assert "forbidden-call" in agent.text
        assert "probe__danger" in agent.text
