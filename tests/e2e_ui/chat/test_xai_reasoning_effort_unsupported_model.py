"""Exercise an xAI reasoning turn through server, runner, and browser.

The mock provider rejects ``reasoning_effort`` on grok-4 as api.x.ai does.
"""

from __future__ import annotations

import contextlib
import io
import json
import tarfile
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state, configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_ERROR_PILL = '[data-testid="error-pill"]'

# Shown only if the mock accepts the request without ``reasoning_effort``.
_REPLY = "Hello from grok-4 without reasoning_effort."

# Includes first-turn harness startup.
_TURN_SETTLE_TIMEOUT_MS = 120_000

# xAI uses Chat Completions; explicit mock auth prevents default-provider rerouting.
_AGENT_YAML = """\
name: {name}
prompt: You are a helpful assistant. Answer briefly.

executor:
  model: xai/grok-4
  harness: openai-agents
  use_responses: false
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_base_url}
"""


def _agent_bundle(name: str, mock_base_url: str) -> bytes:
    """Gzip-tar the inline agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, mock_base_url=mock_base_url)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def grok_reasoning_session(
    live_server: str, mock_llm_server_url: str
) -> Iterator[tuple[str, str]]:
    """Bind a reasoning-enabled grok-4 session to the shared runner."""
    name = f"grok_reason_{uuid.uuid4().hex[:8]}"
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"reasoning_effort": "medium"})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, f"{mock_llm_server_url}/v1"),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    if create_resp.status_code >= 400:
        raise RuntimeError(
            f"session create failed ({create_resp.status_code}): {create_resp.text}"
        )
    session_id = create_resp.json()["session_id"]
    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": str(_server_state["runner_id"])},
            timeout=10.0,
        ).raise_for_status()
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _grok_wire_requests(mock_url: str, token: str) -> list[dict]:
    """Return captured chat-completions bodies whose user text carries *token*."""
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=5.0)
    resp.raise_for_status()
    requests = resp.json()["requests"]
    return [r for r in requests if isinstance(r, dict) and token in json.dumps(r)]


def test_reasoning_turn_on_xai_grok4_succeeds_without_reasoning_effort(
    page: Page,
    grok_reasoning_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A grok-4 turn succeeds without sending ``reasoning_effort``."""
    base_url, session_id = grok_reasoning_session
    token = f"grok-effort-{uuid.uuid4().hex[:6]}"

    # Repeated entries keep any client retry under the same rejection contract.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY, "reject_params": ["reasoning_effort"]}] * 4,
        key="xai/grok-4",
        match=token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()

    composer.fill(f"Say hello ({token})")
    page.get_by_role("button", name="Send", exact=True).click()

    # Wait for either an assistant reply or an error pill.
    page.wait_for_selector(
        f"{_ASSISTANT}, {_ERROR_PILL}",
        timeout=_TURN_SETTLE_TIMEOUT_MS,
    )
    page.wait_for_timeout(1_500)

    wire_requests = _grok_wire_requests(mock_llm_server_url, token)
    offending = [r for r in wire_requests if r.get("reasoning_effort") is not None]

    pills = page.locator(_ERROR_PILL)
    if pills.count() > 0:
        # Expand the pill to expose the provider error.
        with contextlib.suppress(Exception):
            pills.first.click()
            page.wait_for_timeout(1_500)
        pill_text = pills.first.inner_text()
        raise AssertionError(
            f"turn on xai/grok-4 with reasoning effort 'medium' failed: {pill_text!r}; "
            f"{len(offending)}/{len(wire_requests)} captured request bodies carried "
            f"reasoning_effort (it must be omitted for grok models that reject it)"
        )

    expect(page.get_by_text(_REPLY).first).to_be_visible()

    # Assert on the provider's captured request, not only the rendered reply.
    assert not offending, f"request body still carried reasoning_effort: {offending[0]}"
