"""E2E: image tool results must reach the model as native image blocks.

Two producers, one delivery defect:

1. An external stdio MCP tool returns ordered ``TextContent`` +
   ``ImageContent`` (PNG) + essential trailing ``TextContent``. The shared
   MCP formatter serializes the non-text blocks to a JSON/base64 string,
   so the model receives the picture as base64 *text*.
2. The embedded ``browser_screenshot`` tool resolves to
   ``{"ok": true, "data_url": "data:image/png;base64,..."}``; runner
   dispatch forwards that JSON verbatim as the tool-output string.

Both tests drive the real topology (live server + runner + claude-sdk
harness + real stdio MCP subprocess / real browser action wire contract)
with the mock LLM standing at the model boundary, then assert on the
model-bound Anthropic request payload: the exact PNG bytes must arrive
as a native image block, with the essential accompanying text still
readable, in order. While the bug is live these fail: the tool_result
content carries base64 inside plain text and no image block.

Usage::

    pytest tests/e2e/test_tool_image_delivery_e2e.py -v
"""

from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.tools.fixtures.image_stdio_mcp_server import (
    CHART_PNG_BASE64,
    TRAILING_TEXT,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMAGE_MCP_SERVER = _REPO_ROOT / "tests" / "tools" / "fixtures" / "image_stdio_mcp_server.py"

_TURN_TIMEOUT_S = 240

# Tool names as the claude-sdk bridge advertises them to the model.
_MCP_CHART_TOOL = "mcp__omnigent__chart_mcp__fetch_chart"
_BROWSER_SCREENSHOT_TOOL = "mcp__omnigent__browser_screenshot"


def _divert_title_generation(mock_llm_server_url: str) -> None:
    """Route background title-generation calls to their own queue.

    Title generation shares the agent's model key and races the turn's
    first request, so without this it can drain the queued tool_call.
    """
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "chart delivery session"}] * 4,
        match="Write the title",
    )


def _tool_results_by_name(
    requests: list[dict[str, Any]],
) -> dict[str, list[list[dict[str, Any]]]]:
    """Map tool name -> tool_result content-block lists across captured requests.

    Anthropic requests carry ``tool_use`` blocks (id + name) in assistant
    messages and ``tool_result`` blocks (tool_use_id + content) in user
    messages; join them so results are addressable by tool name. A string
    ``content`` is normalized to a single text block.
    """
    uses: dict[str, str] = {}
    for req in requests:
        for msg in req.get("messages") or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    uses[str(block.get("id"))] = str(block.get("name"))
    results: dict[str, list[list[dict[str, Any]]]] = {}
    for req in requests:
        for msg in req.get("messages") or []:
            content = msg.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                name = uses.get(str(block.get("tool_use_id")), "")
                inner = block.get("content")
                if isinstance(inner, str):
                    blocks: list[dict[str, Any]] = [{"type": "text", "text": inner}]
                elif isinstance(inner, list):
                    blocks = [b for b in inner if isinstance(b, dict)]
                else:
                    blocks = []
                results.setdefault(name, []).append(blocks)
    return results


def _results_for_tool(
    results: dict[str, list[list[dict[str, Any]]]], tool_suffix: str
) -> list[list[dict[str, Any]]]:
    """Collect tool_result occurrences whose tool name ends with *tool_suffix*."""
    return [r for name, rs in results.items() if name.endswith(tool_suffix) for r in rs]


def _image_datas(blocks: list[dict[str, Any]]) -> list[str]:
    """Base64 payloads of every native image block in *blocks*."""
    datas: list[str] = []
    for block in blocks:
        if block.get("type") != "image":
            continue
        source = block.get("source")
        data = source.get("data") if isinstance(source, dict) else None
        if isinstance(data, str):
            datas.append(data)
    return datas


def _flat_text(occurrences: list[list[dict[str, Any]]]) -> str:
    """Concatenate every text block across tool_result occurrences."""
    return "\n".join(
        str(block.get("text") or "")
        for blocks in occurrences
        for block in blocks
        if block.get("type") == "text"
    )


@pytest.mark.timeout(420)
def test_external_mcp_image_tool_result_reaches_model_as_image_block(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """An external MCP tool's PNG must arrive at the model as a native image.

    While the delivery defect is live this fails: the fetch_chart tool_result reaching
    the model is one text string embedding the ImageContent as JSON/base64,
    with no image block.
    """
    skip_if_harness_cli_missing("claude-sdk")
    reset_mock_llm(mock_llm_server_url)
    _divert_title_generation(mock_llm_server_url)

    model = f"mock-imgmcp-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"img-mcp-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt=(
            "You have one tool: fetch_chart. It returns a chart image plus "
            "a trailing textual correction. When asked for the chart "
            "reading, call it once, then answer using the image AND the "
            "correction."
        ),
        mock_llm_base_url=mock_llm_server_url,
        extra_config={
            "tools": {
                "chart_mcp": {
                    "type": "mcp",
                    "command": sys.executable,
                    "args": [str(_IMAGE_MCP_SERVER)],
                },
            },
        },
    )

    # Extra text copies absorb background calls (e.g. title generation)
    # that share the model-keyed queue.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_{uuid.uuid4().hex[:8]}",
                        "name": _MCP_CHART_TOOL,
                        "arguments": "{}",
                    }
                ]
            },
        ]
        + [{"text": "The verified reading is TANGERINE-PRIME."}] * 5,
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Fetch the chart and report the verified reading.",
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=_TURN_TIMEOUT_S,
    )
    assert body["status"] == "completed", (
        f"turn failed before delivery could be observed: "
        f"status={body['status']} error={body.get('error')}"
    )

    requests = get_mock_requests(mock_llm_server_url, key=model)
    results = _tool_results_by_name(requests)
    chart_results = _results_for_tool(results, "fetch_chart")
    assert chart_results, (
        f"no fetch_chart tool_result reached the model — the journey did not "
        f"exercise the MCP tool. Tools with results: {sorted(results)}; "
        f"{len(requests)} model requests captured"
    )

    flat_text = _flat_text(chart_results)
    image_datas = [d for blocks in chart_results for d in _image_datas(blocks)]
    assert any(d == CHART_PNG_BASE64 for d in image_datas), (
        "the MCP tool's PNG never reached the model as a native image block "
        f"(image blocks seen: {len(image_datas)}); base64-as-text present: "
        f"{CHART_PNG_BASE64[:24] in flat_text}. Delivered tool_result text: "
        f"{flat_text[:600]!r}"
    )
    assert TRAILING_TEXT in flat_text, (
        "the essential trailing correction was not delivered as readable "
        f"text beside the image. Delivered tool_result text: {flat_text[:600]!r}"
    )
    ordered = False
    for blocks in chart_results:
        image_idx = next(
            (i for i, b in enumerate(blocks) if CHART_PNG_BASE64 in _image_datas([b])),
            None,
        )
        trailing_idx = next(
            (
                i
                for i, b in enumerate(blocks)
                if b.get("type") == "text" and TRAILING_TEXT in str(b.get("text") or "")
            ),
            None,
        )
        if image_idx is not None and trailing_idx is not None and image_idx < trailing_idx:
            ordered = True
    assert ordered, (
        "the trailing correction must follow the image block in the delivered "
        f"tool_result. Occurrences: {[[b.get('type') for b in r] for r in chart_results]}"
    )


class _ScreenshotRenderer(threading.Thread):
    """Simulated desktop renderer speaking the real browser action wire contract.

    Subscribes to the session SSE stream, claims each
    ``browser.action_request`` via ``action_claim``, and resolves it via
    ``action_result`` with a successful ``{ok, data_url}`` screenshot —
    exactly the protocol the Omnigent desktop app's embedded browser uses.
    """

    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        session_id: str,
        data_url: str,
    ) -> None:
        super().__init__(daemon=True)
        self._base_url = base_url
        self._headers = headers
        self._session_id = session_id
        self._data_url = data_url
        self.connected = threading.Event()
        self.served = threading.Event()
        self.stopping = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers=self._headers,
                timeout=httpx.Timeout(10.0, read=float(_TURN_TIMEOUT_S)),
            ) as client:
                with client.stream("GET", f"/v1/sessions/{self._session_id}/stream") as resp:
                    if resp.status_code != 200:
                        self.error = f"stream returned {resp.status_code}"
                        return
                    self.connected.set()
                    for line in resp.iter_lines():
                        if self.stopping.is_set():
                            return
                        if not line.startswith("data:"):
                            continue
                        raw = line[len("data:") :].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                        except ValueError:
                            continue
                        if event.get("type") != "browser.action_request":
                            continue
                        self._serve(client, event)
        except Exception as exc:
            if not self.stopping.is_set():
                self.error = f"{type(exc).__name__}: {exc}"

    def _serve(self, client: httpx.Client, event: dict[str, Any]) -> None:
        action_id = str(event.get("action_id") or "")
        claim = client.post(f"/v1/sessions/{self._session_id}/browser/action_claim/{action_id}")
        if claim.status_code != 200 or not claim.json().get("claimed"):
            return
        token = claim.json().get("claim_token")
        result: dict[str, Any] = {"error": f"unsupported action {event.get('action')}"}
        if event.get("action") == "screenshot":
            result = {"ok": True, "data_url": self._data_url}
        resp = client.post(
            f"/v1/sessions/{self._session_id}/browser/action_result/{action_id}",
            json={"result": result, "claim_token": token},
        )
        if resp.status_code < 400 and event.get("action") == "screenshot":
            self.served.set()

    def stop(self) -> None:
        self.stopping.set()


@pytest.mark.timeout(420)
def test_browser_screenshot_result_reaches_model_as_image_block(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A successful browser_screenshot must arrive at the model as a native image.

    While the delivery defect is live this fails: the model receives the raw
    ``{"ok": true, "data_url": "data:image/png;base64,..."}`` JSON as text.
    """
    skip_if_harness_cli_missing("claude-sdk")
    reset_mock_llm(mock_llm_server_url)
    _divert_title_generation(mock_llm_server_url)

    model = f"mock-shot-{uuid.uuid4().hex[:6]}"
    agent_name = register_inline_agent(
        http_client,
        name=f"browser-shot-{uuid.uuid4().hex[:6]}",
        harness="claude-sdk",
        model=model,
        profile="",
        prompt=(
            "You can drive the embedded browser. When asked what the page "
            "shows, call browser_screenshot and describe the image."
        ),
        mock_llm_base_url=mock_llm_server_url,
    )

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_{uuid.uuid4().hex[:8]}",
                        "name": _BROWSER_SCREENSHOT_TOOL,
                        "arguments": "{}",
                    }
                ]
            },
        ]
        + [{"text": "The page shows a solid orange square."}] * 5,
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )

    renderer = _ScreenshotRenderer(
        str(http_client.base_url),
        dict(http_client.headers),
        session_id,
        f"data:image/png;base64,{CHART_PNG_BASE64}",
    )
    renderer.start()
    try:
        assert renderer.connected.wait(15), (
            f"renderer SSE subscription never connected: {renderer.error}"
        )
        # Let the subscriber registration settle so the turn starts with a
        # renderer present (browser tools are stripped without one).
        time.sleep(1.0)

        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content="Take a screenshot of the embedded browser and describe it.",
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=_TURN_TIMEOUT_S,
        )
    finally:
        renderer.stop()

    assert body["status"] == "completed", (
        f"turn failed before delivery could be observed: "
        f"status={body['status']} error={body.get('error')}"
    )
    assert renderer.served.is_set(), (
        f"the simulated renderer never served a screenshot result — the "
        f"browser_screenshot journey did not run (renderer error: {renderer.error})"
    )

    requests = get_mock_requests(mock_llm_server_url, key=model)
    results = _tool_results_by_name(requests)
    shot_results = _results_for_tool(results, "browser_screenshot")
    assert shot_results, (
        f"no browser_screenshot tool_result reached the model. Tools with "
        f"results: {sorted(results)}; {len(requests)} model requests captured"
    )

    flat_text = _flat_text(shot_results)
    image_datas = [d for blocks in shot_results for d in _image_datas(blocks)]
    assert any(d == CHART_PNG_BASE64 for d in image_datas), (
        "the screenshot PNG never reached the model as a native image block "
        f"(image blocks seen: {len(image_datas)}); data_url-as-text present: "
        f"{'data:image/png;base64,' in flat_text}. Delivered tool_result "
        f"text: {flat_text[:600]!r}"
    )
