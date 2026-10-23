"""``sys_advise_models`` in the chat of a runner launched by ``omnigent host --server``.

The server builds the routing backends. A runner attached from another process
must still offer the advisor whenever its server reports
``smart_routing_enabled: true``. ``tests/e2e/test_advise_models_remote_runner.py``
covers both attachment topologies at the API layer; this is the visible journey.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Browser, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.host import isolated_host
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_JUDGE_MODEL = "routing-judge"
_PROMPT = "Which model should I use for this task: refactor the auth module?"
_RECOMMENDATION = "The advisor recommends gpt-4o-mini for the auth refactor."
_TURN_TIMEOUT_MS = 90_000
_AGENT_YAML = """\
spec_version: 1
name: advisor_probe
prompt: |
  You are a terse assistant. When asked which model to use, consult your
  model advisor tool and report its recommendation verbatim.
executor:
  model: gpt-4o-mini
  config:
    harness: openai-agents
spawn: true
"""


@dataclass(frozen=True)
class RoutingStack:
    base_url: str
    mock_url: str
    host_id: str
    workspace: Path


@pytest.fixture(scope="module")
def routing_stack(
    tmp_path_factory: pytest.TempPathFactory, mock_llm_server_url: str
) -> Iterator[RoutingStack]:
    """A server whose ``llm:`` block builds the OSS judge, plus an attached host."""
    root = tmp_path_factory.mktemp("advise-models-stack")
    config = root / "server.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": _JUDGE_MODEL,
                    "connection": {"base_url": f"{mock_llm_server_url}/v1", "api_key": "mock-key"},
                }
            }
        )
    )
    set_fallback_mock_llm(mock_llm_server_url, _JUDGE_MODEL, '{"model": "gpt-4o-mini"}')
    provider_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    workspace = root / "host-workspace"
    workspace.mkdir()
    with server_runner(
        root / "stack", server_env=provider_env, server_args=["--config", str(config)]
    ) as stack:
        info = httpx.get(f"{stack.base_url}/v1/info", timeout=10, trust_env=False)
        info.raise_for_status()
        assert info.json()["smart_routing_enabled"] is True, info.text
        with (
            isolated_host(root / "host", stack.base_url, env=provider_env) as host,
            httpx.Client(base_url=stack.base_url, trust_env=False) as client,
        ):
            host.wait_online(client, timeout=90)
            yield RoutingStack(
                base_url=stack.base_url,
                mock_url=mock_llm_server_url,
                host_id=host.host_id,
                workspace=workspace,
            )


def _create_session(stack: RoutingStack) -> str:
    """Create the session on the attached host and wait for its runner to come online."""
    bundle = bundle_files({"config.yaml": _AGENT_YAML.encode()})
    metadata = {"host_id": stack.host_id, "workspace": str(stack.workspace)}
    with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=120.0) as client:
        created = post_session_bundle(client.post, "/v1/sessions", bundle, metadata=metadata)
        created.raise_for_status()
        session_id = str(created.json()["session_id"])
        session = client.get(f"/v1/sessions/{session_id}")
        session.raise_for_status()
        runner_id = session.json()["runner_id"]
        assert runner_id, f"host launch did not bind a runner for session {session_id}"
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            status = client.get(f"/v1/runners/{runner_id}/status")
            if status.status_code == 200 and status.json().get("online") is True:
                return session_id
            time.sleep(0.5)
    raise AssertionError(f"runner {runner_id} for session {session_id} never came online")


def _script_advisor_call(mock_url: str, nonce: str) -> None:
    arguments = {
        "tasks": [{"title": "auth-refactor", "agents": [{"agent": "worker", "models": None}]}]
    }
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-{nonce}",
                        "name": "sys_advise_models",
                        "arguments": json.dumps(arguments),
                    }
                ]
            },
            {"text": _RECOMMENDATION},
        ],
        match=nonce,
        required_tools=["sys_list_models"],
    )


def _send_prompt(page: Page, base_url: str, session_id: str, prompt: str) -> None:
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()


def _settle_turn(page: Page) -> str | None:
    """Wait for the recommendation; if it never arrives, return the expanded error pill text."""
    reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=_RECOMMENDATION
    )
    try:
        expect(reply).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    except AssertionError as no_reply:
        pill = page.get_by_test_id("error-pill").first
        if not pill.is_visible():
            raise
        try:
            headline = pill.get_by_test_id("error-headline").inner_text()
            pill.get_by_role("button").first.click()
            content = pill.get_by_test_id("error-message-content")
            expect(content).to_be_visible()
            return f"{headline} | {content.inner_text()}"
        except (AssertionError, PlaywrightTimeoutError) as pill_error:
            raise AssertionError(
                f"no recommendation, and the error pill could not be read: {pill_error}"
            ) from no_reply
    return None


@pytest.mark.timeout(240)
@pytest.mark.min_server_version("0.19.0")
def test_host_launched_runner_offers_advise_models_when_server_routes(
    browser: Browser, routing_stack: RoutingStack
) -> None:
    """The chat on a host-launched runner completes the advisor turn with a recommendation."""
    nonce = f"ref-{uuid.uuid4().hex[:8]}"
    session_id = _create_session(routing_stack)
    _script_advisor_call(routing_stack.mock_url, nonce)
    context = browser.new_context()
    try:
        page = context.new_page()
        _send_prompt(page, routing_stack.base_url, session_id, f"{_PROMPT} ({nonce})")
        error_text = _settle_turn(page)
        assert error_text is None, (
            f"the turn failed instead of consulting the advisor: {error_text}"
        )
        expect(page.get_by_test_id("error-pill")).to_have_count(0)
    finally:
        context.close()
