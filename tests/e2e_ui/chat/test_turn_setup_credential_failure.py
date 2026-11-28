"""Turns while the host credential service cannot supply workspace credentials.

A real runner starts through a bootstrap whose token factory stands in for the
host credential service: it returns a token until a marker file exists, then
raises the reported ``DatabricksAuthError`` on every call. The failed turn must
name the credential problem; restoring credentials must let the next turn reply.
"""

from __future__ import annotations

import json
import os
import textwrap
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm

CREDENTIAL_FAILURE_MESSAGE = "Host credential service could not supply workspace credentials"

_MARKER_ENV = "OMNIGENT_TEST_CREDENTIAL_FAULT_MARKER"
_MESSAGE_ENV = "OMNIGENT_TEST_CREDENTIAL_FAULT_MESSAGE"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_TURN_TIMEOUT_MS = 90_000

_AGENT_YAML = """\
spec_version: 1
name: credential_probe
prompt: |
  Reply with the exact token the user asks for and nothing else.
executor:
  model: credential-probe
  config:
    harness: openai-agents
"""

RUNNER_BOOTSTRAP = textwrap.dedent(
    f"""\
    import os, pathlib
    import omnigent.runner._entry as entry
    from omnigent.inner.databricks_executor import DatabricksAuthError

    marker = pathlib.Path(os.environ[{_MARKER_ENV!r}])
    message = os.environ[{_MESSAGE_ENV!r}]

    def host_credential_service():
        if marker.exists():
            raise DatabricksAuthError(message)
        return "stand-in-workspace-token"

    entry._make_auth_token_factory = lambda *args, **kwargs: host_credential_service
    entry.main()
    """
)


@dataclass(frozen=True)
class CredentialStack:
    stack: ServerRunner
    mock_url: str
    fault_marker: Path


@pytest.fixture
def credential_stack(tmp_path: Path, mock_llm_server_url: str) -> Iterator[CredentialStack]:
    provider_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    marker = tmp_path / "credential-fault.marker"
    with server_runner(tmp_path / "stack", server_env=provider_env) as stack:
        stack.start_runner(
            bootstrap=RUNNER_BOOTSTRAP,
            env={
                **provider_env,
                _MARKER_ENV: str(marker),
                _MESSAGE_ENV: CREDENTIAL_FAILURE_MESSAGE,
            },
        )
        yield CredentialStack(stack, mock_llm_server_url, marker)


def create_bound_session(stack: ServerRunner) -> str:
    bundle = bundle_files({"config.yaml": _AGENT_YAML.encode()})
    response = post_session_bundle(
        httpx.post, f"{stack.base_url}/v1/sessions", bundle, timeout=30.0
    )
    response.raise_for_status()
    session_id = str(response.json()["session_id"])
    bind_session_runner(httpx.patch, stack.base_url, session_id, stack.runner_id, timeout=30.0)
    return session_id


def open_session(page: Page, stack: ServerRunner, session_id: str) -> None:
    page.goto(f"{stack.base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=30_000)


def run_scripted_turn(page: Page, mock_url: str) -> dict[str, str]:
    """Send a prompt with a scripted reply; return the reply or the new error pill's text."""
    nonce = uuid.uuid4().hex[:8]
    marker, token = f"usr-{nonce}", f"ast-{nonce}"
    reset_mock_llm(mock_url)
    configure_mock_llm(mock_url, [{"text": token}], key=marker, match=marker)
    pills = page.get_by_test_id("error-pill")
    new_pill = pills.nth(pills.count())
    page.get_by_role("textbox", name="Message the agent").fill(
        f"Context marker {marker}. Reply with exactly this token and nothing else: {token}"
    )
    page.get_by_role("button", name="Send", exact=True).click()
    reply = page.locator(_ASSISTANT, has_text=token).first
    expect(reply.or_(new_pill)).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    if reply.is_visible():
        page.wait_for_timeout(1_500)
        return {"reply": token}
    headline = new_pill.get_by_test_id("error-headline").inner_text()
    expander = new_pill.locator('button[aria-expanded="false"]')
    if expander.count() > 0:
        expander.first.click()
    content = new_pill.get_by_test_id("error-message-content")
    try:
        expect(content).to_be_visible(timeout=10_000)
        message = content.inner_text()
        content.scroll_into_view_if_needed()
    except (AssertionError, PlaywrightTimeoutError):
        message = ""
    page.wait_for_timeout(5_000)
    return {"headline": headline, "message": message}


def save_observation(
    name: str, page: Page, observed: dict[str, str], stack: ServerRunner, session_id: str
) -> None:
    directory = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not directory:
        return
    items = httpx.get(
        f"{stack.base_url}/v1/sessions/{session_id}/items", params={"limit": 100}, timeout=10.0
    ).json()
    Path(directory, f"{name}.json").write_text(
        json.dumps({"session_id": session_id, "observed": observed, "items": items}, indent=2)
    )
    page.screenshot(path=str(Path(directory) / f"{name}.png"))


@pytest.mark.timeout(420)
def test_credential_outage_names_cause_and_recovers(
    browser: Browser, credential_stack: CredentialStack
) -> None:
    stack, mock_url = credential_stack.stack, credential_stack.mock_url
    warm_session = create_bound_session(stack)
    context = browser.new_context()
    try:
        page = context.new_page()
        open_session(page, stack, warm_session)
        control = run_scripted_turn(page, mock_url)
        save_observation("01-control", page, control, stack, warm_session)
        assert "reply" in control, f"control turn failed before the outage: {control}"

        credential_stack.fault_marker.touch()
        fresh_session = create_bound_session(stack)
        open_session(page, stack, fresh_session)
        outage = run_scripted_turn(page, mock_url)
        save_observation("02-new-session-outage", page, outage, stack, fresh_session)

        open_session(page, stack, warm_session)
        warm = run_scripted_turn(page, mock_url)
        save_observation("03-warm-session-outage", page, warm, stack, warm_session)

        credential_stack.fault_marker.unlink()
        open_session(page, stack, fresh_session)
        recovered = run_scripted_turn(page, mock_url)
        save_observation("04-recovered", page, recovered, stack, fresh_session)
    finally:
        context.close()

    assert "reply" not in outage, f"the new session replied despite the outage: {outage}"
    shown = f"{outage['headline']} {outage['message']}"
    assert "credential" in shown.lower(), f"error does not name the credential failure: {shown!r}"
    assert "reply" in warm, f"a session that needed no new credentials failed: {warm}"
    assert "reply" in recovered, f"turn did not recover once credentials returned: {recovered}"
