"""Generic side-chat entry points and real fork/send journeys.

The fixture's openai-agents runner answers through the mock LLM. Host-launch
tests replace only host provisioning, while fork, runner binding, message
dispatch, streaming, and transcript persistence use the real backend.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry, open_right_rail

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


@pytest.fixture
def side_chat_forks(page: Page, seeded_session: tuple[str, str]) -> Iterator[list[str]]:
    """Track real forks and remove them from the fixture-owned server afterward."""
    base_url, session_id = seeded_session
    child_ids: list[str] = []
    pattern = f"**/v1/sessions/{session_id}/fork"

    def track_fork(route: Route) -> None:
        assert route.request.post_data_json["side_chat"] is True
        response = route.fetch()
        if response.ok:
            child_ids.append(response.json()["id"])
        route.fulfill(response=response)

    page.route(pattern, track_fork)
    try:
        yield child_ids
    finally:
        page.unroute(pattern, track_fork)
        for child_id in child_ids:
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0).raise_for_status()


def _items(base_url: str, session_id: str) -> list[dict[str, object]]:
    """Read the persisted transcript independently of the browser's local state."""
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["data"]


def _send_parent(page: Page, question: str, reply: str) -> None:
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()
    composer.fill(question)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)


def _start_side_chat(page: Page, entrypoint: str, question: str) -> None:
    if entrypoint == "slash":
        page.get_by_placeholder("Send a message…").fill(f"/side {question}")
        page.get_by_role("button", name="Send", exact=True).click()
    else:
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("button", name="Open new", exact=True).click()
        page.get_by_role("menuitem", name="Side chat", exact=True).click()
        page.get_by_test_id("side-chat-input").fill(question)
        page.get_by_test_id("side-chat-send").click()


def test_slash_menu_lists_side_command(page: Page, seeded_session: tuple[str, str]) -> None:
    """Typing ``/side`` surfaces the ``/side`` row in the composer command menu."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible()

    composer.fill("/side")
    # The restyled slash menu renders a row per matching command; /side is the
    # generic panel side chat, offered on every harness.
    expect(page.get_by_test_id("slash-menu-item-side")).to_be_visible()


def test_composer_add_tray_offers_a_side_chat(page: Page, seeded_session: tuple[str, str]) -> None:
    """The composer ``+`` tray has a "Start a new side chat" item."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    page.get_by_test_id("composer-attach").click()
    expect(page.get_by_role("menuitem", name="Start a new side chat")).to_be_visible()


def test_rail_new_tab_menu_offers_a_side_chat(page: Page, seeded_session: tuple[str, str]) -> None:
    """The Workspace rail's "Open new" (``+``) menu lists "Side chat"."""
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new", exact=True).click()
    expect(page.get_by_role("menuitem", name="Side chat", exact=True)).to_be_visible()


@pytest.mark.parametrize("entrypoint", ["slash", "panel"])
def test_side_chat_sends_with_stale_branch_metadata(
    page: Page,
    seeded_session: tuple[str, str],
    side_chat_forks: list[str],
    runner_id: str,
    mock_llm_server_url: str,
    entrypoint: str,
) -> None:
    """A working parent can fork and chat even if its saved branch no longer exists."""
    base_url, session_id = seeded_session
    host_id = "side-chat-test-host"
    workspace = "/workspace/existing-checkout"
    stale_branch = "worktree-from-another-machine"
    launches: list[dict[str, object]] = []

    def source_snapshot(route: Route) -> None:
        response = fetch_with_retry(route)
        snapshot = response.json()
        snapshot.update(
            host_id=host_id,
            host_online=True,
            workspace=workspace,
            git_branch=stale_branch,
        )
        route.fulfill(response=response, json=snapshot)

    def launch_on_fixture_runner(route: Route) -> None:
        launch = route.request.post_data_json
        launches.append(launch)
        if "git" in launch:
            route.fulfill(
                status=400,
                json={"detail": f"base branch does not exist: {stale_branch}"},
            )
            return
        response = httpx.patch(
            f"{base_url}/v1/sessions/{launch['session_id']}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        response.raise_for_status()
        route.fulfill(json={"runner_id": runner_id})

    page.route(re.compile(rf"/v1/sessions/{session_id}(?:\?.*)?$"), source_snapshot)
    page.route(f"**/v1/hosts/{host_id}/runners", launch_on_fixture_runner)
    parent_question = f"main-{session_id}: remember our main conversation"
    parent_reply = "The parent conversation is ready."
    question = f"side-{session_id}: answer this side question"
    followup = f"side-{session_id}: answer a follow-up"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": parent_reply}],
        key=f"main-{session_id}",
        match=f"main-{session_id}",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "First side answer."}, {"text": "Second side answer."}],
        key=f"side-{session_id}",
        match=f"side-{session_id}",
    )

    page.goto(f"{base_url}/c/{session_id}")
    _send_parent(page, parent_question, parent_reply)
    parent_items = _items(base_url, session_id)
    _start_side_chat(page, entrypoint, question)

    pane = page.locator(".side-chat-backdrop")
    expect(pane.locator(_ASSISTANT).filter(has_text="First side answer.")).to_be_visible(
        timeout=30_000
    )
    assert len(side_chat_forks) == 1
    assert launches == [{"session_id": side_chat_forks[0], "workspace": workspace}]
    expect(pane.get_by_text(parent_reply, exact=True)).to_have_count(0)

    page.get_by_test_id("side-chat-input").fill(followup)
    expect(page.get_by_test_id("side-chat-send")).to_be_enabled(timeout=30_000)
    page.get_by_test_id("side-chat-send").click()
    expect(pane.locator(_ASSISTANT).filter(has_text="Second side answer.")).to_be_visible(
        timeout=30_000
    )
    expect(pane.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
    assert _items(base_url, session_id) == parent_items
    child_items = _items(base_url, side_chat_forks[0])
    child_text = str(child_items)
    assert question in child_text
    assert followup in child_text
    assert "First side answer." in child_text
    assert "Second side answer." in child_text
    expect(page).to_have_url(f"{base_url}/c/{session_id}")


def test_runner_bound_side_chat_closes_without_stopping_parent(
    page: Page,
    seeded_session: tuple[str, str],
    side_chat_forks: list[str],
    runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A hostless session's real runner serves the side chat and survives its closure."""
    base_url, session_id = seeded_session
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    response.raise_for_status()
    assert response.json()["host_id"] is None
    assert response.json()["runner_id"] == runner_id
    question = f"runner-side-{session_id}: answer in the side chat"
    parent_question = f"runner-parent-{session_id}: keep chatting after the side chat closes"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "The runner answered the side chat."}],
        key=f"runner-side-{session_id}",
        match=f"runner-side-{session_id}",
    )
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "The parent still works."}],
        key=f"runner-parent-{session_id}",
        match=f"runner-parent-{session_id}",
    )

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder("Send a message…")).to_be_visible()
    _start_side_chat(page, "slash", question)
    pane = page.locator(".side-chat-backdrop")
    expect(
        pane.locator(_ASSISTANT).filter(has_text="The runner answered the side chat.")
    ).to_be_visible(timeout=30_000)
    assert len(side_chat_forks) == 1
    child_id = side_chat_forks[0]
    response = httpx.get(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)
    response.raise_for_status()
    assert response.json()["runner_id"] == runner_id
    assert response.json()["host_id"] is None
    assert not any(item.get("type") == "message" for item in _items(base_url, session_id))

    with page.expect_response(f"**/v1/sessions/{child_id}/events") as stopped:
        page.get_by_role("button", name="Close Side chat 1", exact=True).click()
    assert stopped.value.ok
    expect(page.get_by_test_id("side-chat-input")).to_have_count(0)
    _send_parent(page, parent_question, "The parent still works.")
    parent_items = str(_items(base_url, session_id))
    assert parent_question in parent_items
    assert question not in parent_items
