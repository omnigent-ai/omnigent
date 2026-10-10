"""Gateway-backed Claude launches must withhold unsupported WebSearch.

Run the real Claude CLI with the product terminal composition against the shared
mock model server standing in for a gateway: it calls WebSearch when offered and
rejects Claude Code's nested server-side ``web_search`` request with a region
error. The turn must finish on the scripted answer without that error, and no
nested request may reach the gateway. No live provider credentials are used.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir
from omnigent.harnesses.claude_native.main import ClaudeNativeUcodeConfig, _claude_terminal_request
from tests.e2e._harness_probes import cli_unavailable_reason

_MODEL = "claude-sonnet-4-5"
_PROMPT_TOKEN = "WEBSEARCH-GATEWAY-CLI-PROBE-7f3a"
_PROMPT = f"Search the web for today's weather in Paris ({_PROMPT_TOKEN})"
# The mock serves the longest matching token, so these selectors rank the
# scripted turns: search offered > search deferred behind ToolSearch > no search.
_SEARCH_OFFERED_MATCH = f"weather in Paris ({_PROMPT_TOKEN})"
_SEARCH_DEFERRED_MATCH = f"Paris ({_PROMPT_TOKEN})"
_FINAL_TEXT = "WEBSEARCH-CLI-TURN-FINISHED"
_NO_SEARCH_TEXT = f"I can't search the web from this session. {_FINAL_TEXT}"
RESTRICTION_MESSAGE = (
    "Web search is only available in the US: the web_search tool "
    "is not supported in this region (mock gateway US-only restriction)."
)


def _configure(mock_url: str, body: dict) -> None:
    httpx.post(f"{mock_url}/mock/configure", json=body, timeout=10.0).raise_for_status()


def _script_gateway(mock_url: str) -> None:
    """Call WebSearch when offered, reject its nested server leg, answer without it otherwise."""
    _configure(
        mock_url,
        {
            "match": _SEARCH_OFFERED_MATCH,
            "required_tools": ["WebSearch"],
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "toolu_ws_1",
                            "name": "WebSearch",
                            "arguments": json.dumps({"query": "Paris weather today"}),
                        }
                    ]
                },
                {"text": _FINAL_TEXT},
                {"text": _FINAL_TEXT},
            ],
        },
    )
    _configure(
        mock_url,
        {
            "match": _SEARCH_DEFERRED_MATCH,
            "required_tools": ["ToolSearch"],
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": "toolu_ts_1",
                            "name": "ToolSearch",
                            "arguments": json.dumps({"query": "WebSearch"}),
                        }
                    ]
                },
                {"text": _NO_SEARCH_TEXT},
                {"text": _NO_SEARCH_TEXT},
            ],
        },
    )
    # Guard on Bash: the main turn always advertises it, while Claude Code's
    # tool-less background requests (title generation) must not consume the reply.
    _configure(
        mock_url,
        {
            "match": _PROMPT_TOKEN,
            "required_tools": ["Bash"],
            "responses": [{"text": _NO_SEARCH_TEXT}] * 3,
        },
    )
    rejection = [{"error": RESTRICTION_MESSAGE, "status_code": 400}] * 6
    _configure(mock_url, {"key": _MODEL, "required_tools": ["web_search"], "responses": rejection})
    _configure(
        mock_url, {"key": "default", "required_tools": ["web_search"], "responses": rejection}
    )


def _is_server_web_search_tool(tool: object) -> bool:
    return isinstance(tool, dict) and (
        tool.get("name") == "web_search" or str(tool.get("type", "")).startswith("web_search")
    )


@pytest.mark.posix_only
@pytest.mark.timeout(300)
def test_websearch_under_gateway_launch_does_not_surface_us_only_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    isolated_mock_llm_server_url: str,
) -> None:
    """The real Claude launch must not expose the mock gateway's WebSearch region error."""
    reason = cli_unavailable_reason("claude")
    if reason is not None:
        pytest.skip(f"requires a runnable 'claude' CLI; {reason}")

    mock_url = isolated_mock_llm_server_url
    _script_gateway(mock_url)
    bridge_dir: Path | None = None
    try:
        # Gateway-backed launch composition: base URL override + apiKeyHelper.
        claude_config = ClaudeNativeUcodeConfig(
            env={"ANTHROPIC_BASE_URL": mock_url},
            api_key_helper="echo test-key",
        )

        config_dir = tmp_path / "claude-config"
        config_dir.mkdir()
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        monkeypatch.chdir(workspace)

        for name in (
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
        ):
            monkeypatch.delenv(name, raising=False)
        bridge_dir = prepare_bridge_dir(f"conv_e2e_{uuid.uuid4().hex[:12]}", workspace=workspace)
        body = _claude_terminal_request(
            (
                "-p",
                _PROMPT,
                "--allowedTools",
                "WebSearch",
                "--model",
                _MODEL,
                "--output-format",
                "text",
            ),
            command="claude",
            bridge_dir=bridge_dir,
            claude_config=claude_config,
        )
        spec = body["spec"]

        env = {
            k: v
            for k, v in os.environ.items()
            # Keep any corporate proxy out of the loopback gateway path.
            if k.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(spec["env"])
        env.update(
            {
                "NO_PROXY": "127.0.0.1,localhost",
                "no_proxy": "127.0.0.1,localhost",
                "CLAUDE_CONFIG_DIR": str(config_dir),
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_TELEMETRY": "1",
            }
        )

        proc = subprocess.run(
            [spec["command"], *spec["args"]],
            cwd=spec["cwd"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)

    output = f"{proc.stdout}\n{proc.stderr}"
    requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
    assert requests, f"the claude CLI never reached the gateway endpoint: {output[-2000:]}"
    assert proc.returncode == 0, f"claude CLI exited {proc.returncode}: {output[-2000:]}"
    assert _FINAL_TEXT in proc.stdout, (
        f"claude CLI did not finish on the scripted answer: {output[-2000:]}"
    )

    # The gateway's region rejection must not surface as the WebSearch outcome.
    assert "only available in the US" not in output and "API Error" not in proc.stdout, (
        "WebSearch under the gateway launch surfaced the US-only region "
        f"restriction to the user: {proc.stdout.strip()[-1500:]!r}"
    )
    # And the launch must never have sent the gateway a request carrying the
    # server-side web_search tool, the nested leg the gateway cannot serve.
    nested_search_requests = [
        r
        for r in requests
        if isinstance(r, dict)
        and any(_is_server_web_search_tool(t) for t in (r.get("tools") or []))
    ]
    assert not nested_search_requests, (
        "the launch still routed a nested server-side web_search request to "
        f"the gateway: {len(nested_search_requests)} request(s)"
    )
