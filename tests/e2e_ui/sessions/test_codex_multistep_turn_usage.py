"""UI journey: a multi-step codex (SDK) turn must report the whole turn's tokens.

The non-native codex harness reports per-turn token usage from Codex's
``thread/tokenUsage/updated`` notifications. Codex sends two breakdowns there:
``last`` (the most recent model request only) and ``total`` (cumulative across
the thread). ``CodexExecutor`` keeps only ``last``, so an agentic turn that
spans several model requests (model -> tool -> model -> final) is accounted as
if only the final model request happened - input, output, and total tokens
(and therefore cost) are under-reported everywhere the session's usage
surfaces, including the web SPA's agent-info popover.

Journey (real web SPA, live server + runner, real ``codex`` CLI app-server
pointed at the mock ``/v1/responses``):

1. start a headless ``codex`` (SDK-mode) session
2. send a message; the scripted turn runs a shell tool call between two model
   requests, then completes (2 model requests in one turn)
3. open the agent-info popover and expand its Token usage breakdown
4. observable failure: the breakdown shows only the final model request's
   tokens (Input 10 / Output 5 / Total 15) instead of the whole turn's
   (Input 20 / Output 10 / Total 30)

Regression guard: the final assertions (cumulative token counts render in the
per-model breakdown) FAIL on the current build and pass once the executor
accounts the full turn. The request-count precondition (the turn really made
two model requests) passes both before and after a fix, pinning the failure to
under-ACCOUNTING rather than a short-circuited turn.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import tarfile
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, Response, Route, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

# The e2e-ui CI job installs the codex CLI; without it the SDK harness cannot
# spawn its app-server, so the turn (and this reproduction) can never run.
pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None,
    reason=(
        "the codex CLI binary is required for the headless-codex usage e2e; "
        "install via `npm i -g @openai/codex`"
    ),
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

_FINAL_TEXT = "Codex multi step turn done."

# The mock's /v1/responses hardcodes per-request usage: a tool-call response
# reports input 10 / output 5 / total 15, and a text response reports input 10
# / output max(5, word_count) / total input+output. _FINAL_TEXT is exactly 5
# words, so both scripted requests report 10/5/15 and the whole turn's
# cumulative usage is input 20 / output 10 / total 30 - while the final
# request alone is 10/5/15, making under-accounting unambiguous.
_EXPECTED_INPUT = 20
_EXPECTED_OUTPUT = 10
_EXPECTED_TOTAL = 30


def _build_codex_bundle(name: str, model: str) -> bytes:
    """Build a one-file headless-``codex`` (SDK mode) agent bundle.

    The executor forwards ``OPENAI_*`` env into the codex app-server
    subprocess, and the live-server fixtures set ``OPENAI_BASE_URL`` /
    ``OPENAI_API_KEY`` on the runner, so the CLI's built-in openai provider
    reaches the mock without extra config.

    :param name: Agent name (unique per test run).
    :param model: Wire model id, doubling as the mock's queue key.
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "codex",
            "model": model,
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_codex_session(base_url: str, runner_id: str, model: str) -> str:
    """Create a runner-bound session for a fresh headless-``codex`` agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param model: Wire model id for the agent's executor.
    :returns: The new session id.
    """
    name = f"codex-usage-{uuid.uuid4().hex[:8]}"
    bundle = _build_codex_bundle(name, model)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _model_requests(mock_llm_server_url: str, token: str) -> list[dict]:
    """Return the mock's captured model requests carrying *token*.

    Both of the turn's requests include the original user message in their
    Responses-API ``input`` history, so counting token-carrying requests
    counts the turn's model requests without cross-test contamination.

    :param mock_llm_server_url: Mock server base URL.
    :param token: The unique routing token this test typed.
    :returns: The matching captured request bodies.
    """
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [r for r in resp.json()["requests"] if token in json.dumps(r)]


def _assert_cumulative_usage(page: Page, model: str) -> None:
    """Open agent info and check the tokens from both model requests."""
    trigger = page.get_by_test_id("agent-info-trigger")
    trigger.focus()
    trigger.press("Enter")
    usage_section = page.get_by_test_id("agent-info-usage-by-model")
    expect(usage_section).to_be_visible(timeout=30_000)
    usage_section.locator("summary").press("Enter")
    model_group = page.get_by_test_id(f"agent-info-model-{model}")
    expect(model_group).to_be_visible(timeout=30_000)
    expect(model_group).to_contain_text(
        re.compile(rf"Total\s*{_EXPECTED_TOTAL}(?!\d)"), timeout=30_000
    )
    expect(model_group).to_contain_text(re.compile(rf"Input\s*{_EXPECTED_INPUT}(?!\d)"))
    expect(model_group).to_contain_text(re.compile(rf"Output\s*{_EXPECTED_OUTPUT}(?!\d)"))


@pytest.mark.timeout(600)
def test_codex_multistep_turn_reports_cumulative_usage(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A two-request codex turn's agent-info usage must cover both requests.

    The mock scripts request 1 as an ``exec_command`` tool call and request 2
    as the final answer, each reporting input 10 / output 5 / total 15. On the
    current build the executor reports only the final request's usage
    (10/5/15) instead of the turn's cumulative 20/10/30, so the agent-info
    per-model breakdown under-reports every bucket - the final assertions
    fail. Once the executor accounts the whole turn they pass.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])

        uid = uuid.uuid4().hex[:6]
        model = f"mock-codex-usage-{uid}"
        token = f"codexusage-{uid}"
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": f"call_echo_{uid}",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": "echo codex-usage-e2e"}),
                        }
                    ]
                },
                {"text": _FINAL_TEXT},
            ],
            key=model,
            match=token,
        )

        session_id = _create_codex_session(live_server, runner_id, model)
        try:
            page.goto(f"{live_server}/c/{session_id}")

            # Drive the multi-step turn to completion: the final assistant
            # message renders and the working indicator clears.
            _send(page, f"Run the echo command, then say done. {token}")
            expect(page.locator(_ASSISTANT).filter(has_text=_FINAL_TEXT).first).to_be_visible(
                timeout=240_000
            )
            expect(page.locator(_WORKING)).to_have_count(0, timeout=240_000)

            # Precondition: the turn really spanned two model requests (tool
            # call in between), so a smaller usage figure below is
            # under-accounting, not a short-circuited turn. Passes both
            # before and after a fix.
            requests = _model_requests(mock_llm_server_url, token)
            assert len(requests) == 2, (
                f"expected the turn to make exactly 2 model requests, saw "
                f"{len(requests)} - the codex/mock wiring broke, not the bug"
            )

            _assert_cumulative_usage(page, model)

            session_url = f"{live_server}/v1/sessions/{session_id}"
            usage_responses: list[Response] = []

            def record_usage(response: Response) -> None:
                if response.url == f"{session_url}/usage":
                    usage_responses.append(response)

            page.on("response", record_usage)
            # Pause SSE replay so persisted HTTP usage must hydrate the fresh page.
            pending_streams: list[Route] = []
            page.route(f"{session_url}/stream*", lambda route: pending_streams.append(route))
            with page.expect_response(
                lambda response: (
                    response.url.split("?", 1)[0] == session_url
                    and response.request.method == "GET"
                )
            ) as snapshot_response:
                page.reload(wait_until="domcontentloaded")
            expect(page.get_by_placeholder("Send a message…")).to_be_focused()
            _assert_cumulative_usage(page, model)
            expect(page.locator(_ASSISTANT).filter(has_text=_FINAL_TEXT).first).to_be_visible()

            # Old servers still hydrate from the full snapshot, without /usage.
            if snapshot_response.value.json().get("usage_included") is False:
                assert usage_responses, "reload never fetched the omitted usage"
                assert usage_responses[-1].ok
                usage = usage_responses[-1].json()
                assert usage["id"] == session_id
                model_usage = usage["usage_by_model"][model]
                assert model_usage["input_tokens"] == _EXPECTED_INPUT
                assert model_usage["output_tokens"] == _EXPECTED_OUTPUT
                assert model_usage["total_tokens"] == _EXPECTED_TOTAL
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
