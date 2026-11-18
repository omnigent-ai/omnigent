"""An agent files existing sessions into projects and the sidebar regroups live.

The scripted model calls ``sys_project_list`` and then ``sys_session_update``
once per session: two unfiled sessions go into an existing project, and the
agent's own running session goes into a project the update creates. The open sidebar must show each
row under its new folder without a reload. A nightly variant drives a real
Claude Code CLI, which reaches the same tools through the MCP relay.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.live_server import terminate_process
from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.sessions._sidebar_helpers import patch_session
from tests.e2e_ui.sessions.test_move_to_project import _seed_session

_REPLY = "Filed the sessions into their projects."
_TURN_TIMEOUT_MS = 90_000
_LIVE_UPDATE_TIMEOUT_MS = 20_000
_RECORD_HOLD_MS = 2_500
_PENDING_APPROVAL = '[data-testid="approval-card"][data-state="pending"]'
# Claude Code auto-launch, first-run setup and the first mock turn.
_NATIVE_READY_MS = 180_000
_VIEWPORT = {"width": 1280, "height": 800}

_AGENT_YAML = """\
spec_version: 1
name: project_filer
prompt: |
  You organize the user's sessions into projects with sys_project_list and
  sys_session_update, then reply with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents
"""


@dataclass(frozen=True)
class FilingJourney:
    base_url: str
    agent_session_id: str
    agent_title: str
    model: str
    existing_project: str
    new_project: str
    unfiled: dict[str, str]


def _project_ids_named(base_url: str, names: set[str]) -> list[str]:
    response = httpx.get(f"{base_url}/v1/projects", timeout=10.0)
    response.raise_for_status()
    projects = response.json()["data"]
    return [project["id"] for project in projects if project["name"] in names]


def _delete_projects_named(base_url: str, names: set[str]) -> None:
    # Cleanup must not replace the journey's own failure with a server error.
    with contextlib.suppress(httpx.HTTPError, ValueError, KeyError):
        for project_id in _project_ids_named(base_url, names):
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/projects/{project_id}", timeout=10.0)


def _session_project_id(base_url: str, session_id: str) -> str | None:
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    return response.json().get("project_id")


def _folder(page: Page, project: str) -> Locator:
    return page.locator("section").filter(
        has=page.get_by_role("button", name=re.compile(f"^{re.escape(project)}"))
    )


def _hold(page: Page) -> None:
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(_RECORD_HOLD_MS)


@pytest.fixture
def filing_journey(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[FilingJourney]:
    """An agent session plus two unfiled sessions and one existing project."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    uniq = uuid.uuid4().hex[:6]
    model = f"project-filer-{uniq}"
    existing_project = f"Release prep {uniq}"
    new_project = f"Follow-ups {uniq}"
    session_ids: list[str] = []
    try:
        httpx.post(
            f"{live_server}/v1/projects", json={"name": existing_project}, timeout=10.0
        ).raise_for_status()
        unfiled = {}
        for label in ("changelog draft", "flaky test triage"):
            title = f"{label} {uniq}"
            seeded_id = _seed_session(live_server, title=title)
            session_ids.append(seeded_id)
            unfiled[seeded_id] = title
        agent_title = f"project filer {uniq}"
        agent_session_id = _create_bundled_session(
            live_server, runner_id, _AGENT_YAML.format(model=model)
        )
        session_ids.append(agent_session_id)
        patch_session(live_server, agent_session_id, title=agent_title)
        yield FilingJourney(
            base_url=live_server,
            agent_session_id=agent_session_id,
            agent_title=agent_title,
            model=model,
            existing_project=existing_project,
            new_project=new_project,
            unfiled=unfiled,
        )
    finally:
        try:
            for session_id in session_ids:
                with contextlib.suppress(httpx.HTTPError):
                    httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            _delete_projects_named(live_server, {existing_project, new_project})
        finally:
            terminate_process(respawned)


def _tool_call(name: str, arguments: dict[str, object]) -> dict[str, object]:
    call = {"call_id": f"call_{uuid.uuid4().hex[:8]}", "name": name}
    return {"tool_calls": [{**call, "arguments": json.dumps(arguments)}]}


@pytest.mark.timeout(300)
@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_agent_files_existing_sessions_into_projects(
    request: pytest.FixtureRequest,
    mock_llm_server_url: str,
    filing_journey: FilingJourney,
) -> None:
    """Listed sessions are filed by project name, and the open sidebar regroups them."""
    journey = filing_journey
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool_call("sys_project_list", {}),
            *(
                _tool_call(
                    "sys_session_update",
                    {"session_id": session_id, "project": journey.existing_project},
                )
                for session_id in journey.unfiled
            ),
            _tool_call(
                "sys_session_update",
                {"session_id": journey.agent_session_id, "project": journey.new_project},
            ),
        ],
        key=journey.model,
    )
    set_fallback_mock_llm(mock_llm_server_url, journey.model, _REPLY)

    page: Page = request.getfixturevalue("page")
    page.goto(f"{journey.base_url}/c/{journey.agent_session_id}")
    sessions_section = page.locator("section").filter(
        has=page.get_by_role("button", name="Sessions", exact=True)
    )
    for title in [*journey.unfiled.values(), journey.agent_title]:
        expect(sessions_section.get_by_text(title, exact=True)).to_be_visible(timeout=30_000)
    expect(_folder(page, journey.new_project)).to_have_count(0)
    # Project folders start collapsed in a fresh browser; the move must not open this one.
    folder_toggle = page.get_by_role("button", name=journey.existing_project, exact=True)
    expect(folder_toggle).to_have_attribute("aria-expanded", "false")
    _hold(page)

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(
        f"File the changelog and flaky-test sessions under '{journey.existing_project}', "
        f"and put this session in a new '{journey.new_project}' project."
    )
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_text(_REPLY, exact=True).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    transcript = page.get_by_role("log")
    transcript.get_by_role("button", name=re.compile("^Worked for")).first.click()
    transcript.get_by_role("button", name="Called 4 tools").click()
    expect(transcript.get_by_role("button", name=re.compile("^Project list"))).to_be_visible()
    into_existing = transcript.get_by_role(
        "button", name=re.compile(f"^Update session .* · {re.escape(journey.existing_project)}")
    )
    expect(into_existing).to_have_count(2)
    expect(
        transcript.get_by_role(
            "button", name=re.compile(f"^Update session .* · {re.escape(journey.new_project)}")
        )
    ).to_be_visible()
    into_existing.first.click()
    into_existing.last.click()
    expect(transcript.get_by_text("updated", exact=True)).to_have_count(2)
    _hold(page)

    for title in journey.unfiled.values():
        expect(sessions_section.get_by_text(title, exact=True)).to_have_count(
            0, timeout=_LIVE_UPDATE_TIMEOUT_MS
        )
    expect(folder_toggle).to_have_attribute("aria-expanded", "false")
    folder_toggle.click()
    existing_folder = _folder(page, journey.existing_project)
    for title in journey.unfiled.values():
        expect(existing_folder.get_by_text(title, exact=True)).to_be_visible(
            timeout=_LIVE_UPDATE_TIMEOUT_MS
        )
        expect(sessions_section.get_by_text(title, exact=True)).to_have_count(0)
    expect(
        _folder(page, journey.new_project).get_by_text(journey.agent_title, exact=True)
    ).to_be_visible(timeout=_LIVE_UPDATE_TIMEOUT_MS)
    _hold(page)

    (existing_id,) = _project_ids_named(journey.base_url, {journey.existing_project})
    (new_id,) = _project_ids_named(journey.base_url, {journey.new_project})
    for session_id in journey.unfiled:
        assert _session_project_id(journey.base_url, session_id) == existing_id
    assert _session_project_id(journey.base_url, journey.agent_session_id) == new_id


@pytest.mark.nightly
@pytest.mark.timeout(600)
@pytest.mark.browser_context_args(viewport=_VIEWPORT, record_video_size=_VIEWPORT)
def test_native_claude_files_itself_into_a_project(
    request: pytest.FixtureRequest,
    native_claude_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Claude Code reaches both tools through the relay and files its running session."""
    base_url, session_id = native_claude_mock_session
    project = f"Native follow-ups {uuid.uuid4().hex[:6]}"
    title = f"claude code filer {uuid.uuid4().hex[:6]}"
    go_token = f"file-me-{uuid.uuid4().hex[:6]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool_call("mcp__omnigent__sys_project_list", {}),
            _tool_call(
                "mcp__omnigent__sys_session_update",
                {"session_id": session_id, "project": project},
            ),
            {"text": _REPLY},
        ],
        key="native-project-move",
        match=go_token,
        required_tools=["mcp__omnigent__sys_project_list", "mcp__omnigent__sys_session_update"],
    )
    patch_session(base_url, session_id, title=title)
    try:
        page: Page = request.getfixturevalue("page")
        page.goto(f"{base_url}/c/{session_id}")
        expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(timeout=_NATIVE_READY_MS)
        chat_view = page.get_by_test_id("view-mode-chat")
        expect(chat_view).to_be_enabled(timeout=30_000)
        chat_view.click()
        expect(_folder(page, project)).to_have_count(0)

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(f"Put this session in a new '{project}' project. {go_token}")
        page.get_by_role("button", name="Send", exact=True).click()
        # Manual permission mode asks before each MCP tool call, as for any relayed tool.
        for tool in ("mcp__omnigent__sys_project_list", "mcp__omnigent__sys_session_update"):
            card = page.locator(_PENDING_APPROVAL).filter(has_text=tool)
            expect(card).to_be_visible(timeout=_NATIVE_READY_MS)
            _hold(page)
            card.get_by_role("button", name="Approve", exact=True).click()
        expect(
            page.locator('[data-testid="message-bubble"][data-role="assistant"]', has_text=_REPLY)
        ).to_be_visible(timeout=_NATIVE_READY_MS)

        expect(page.get_by_role("button", name=f"Project: {project}")).to_be_visible(
            timeout=_LIVE_UPDATE_TIMEOUT_MS
        )
        expect(_folder(page, project).get_by_text(title, exact=True)).to_be_visible(
            timeout=_LIVE_UPDATE_TIMEOUT_MS
        )
        _hold(page)
        (project_id,) = _project_ids_named(base_url, {project})
        assert _session_project_id(base_url, session_id) == project_id
    finally:
        _delete_projects_named(base_url, {project})
