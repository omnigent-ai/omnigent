"""Browser-only contracts for native slash-menu interaction."""

import time
from urllib.parse import quote

import pytest
from playwright.sync_api import Page, expect

_ROWS = "[data-testid^='slash-menu-item-']"


def _composer(page: Page):
    return page.get_by_label("Message the agent")


@pytest.mark.parametrize(
    ("project_kind", "gesture"),
    [
        ("none", "enter"),
        ("id", "tab-enter"),
        ("legacy", "click"),
        ("snapshot", "enter"),
        ("partial", "enter"),
    ],
)
def test_new_command_opens_landing_in_current_project(
    page: Page, chat_session_contract, project_kind: str, gesture: str
) -> None:
    handle = chat_session_contract
    project_name = "Customer X & tools"
    project = {"id": "project-new", "name": project_name, "config": {}}
    handle.contract.json(f"/v1/hosts/{handle.host_id}/filesystem", {"path": "/", "entries": []})
    if project_kind != "none":
        handle.contract.json("/v1/projects", {"object": "list", "data": [project]})
        handle.contract.json("/v1/projects/project-new", project)
        if project_kind == "legacy":
            handle.update_session(labels={"omni_project": project_name})
        else:
            handle.update_session(project_id=project["id"])
        if project_kind == "snapshot":
            handle.contract.json("/v1/sessions", {"object": "list", "data": []})
        if project_kind == "partial":
            row = handle.session_snapshot()
            row.pop("project_id", None)
            handle.contract.json("/v1/sessions", {"object": "list", "data": [row]})
        if project_kind == "id":
            handle.harness = "codex-native"
            handle.update_session(labels={"omnigent.wrapper": "codex-native-ui"})
            handle.contract.json(f"/v1/sessions/{handle.session_id}/codex_goal", {"goal": None})

    page.goto(handle.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/n" if gesture == "tab-enter" else "/new")
    command = page.get_by_test_id("slash-menu-item-new")
    expect(command).to_have_attribute("data-active", "true")
    expect(command).to_contain_text("/new")
    expect(command).not_to_contain_text("New session")
    expect(command).to_contain_text("Start a blank session in the same project")
    if gesture == "tab-enter":
        composer.press("Tab")
        expect(composer).to_have_value("/new ")
        expect(page).to_have_url(handle.url)
    if gesture == "click":
        command.click()
    else:
        composer.press("Enter")

    target = f"{handle.base_url}/"
    if project_kind != "none":
        target += f"?project={quote(project_name, safe='')}"
    expect(page).to_have_url(target)
    if project_kind != "none":
        expect(page.get_by_role("heading", name=project_name, exact=True, level=1)).to_be_visible()
    assert handle.event_posts == []
    page.go_back()
    expect(_composer(page)).to_have_value("")


def test_new_command_respects_unfiled_sidebar_row(page: Page, chat_session_contract) -> None:
    handle = chat_session_contract
    handle.update_session(project_id="old-project", labels={"omni_project": "Old project"})
    row = {**handle.session_snapshot(), "project_id": None, "labels": {}}
    handle.contract.json("/v1/sessions", {"object": "list", "data": [row]})
    handle.contract.json(f"/v1/hosts/{handle.host_id}/filesystem", {"path": "/", "entries": []})
    page.goto(handle.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/new")
    composer.press("Enter")
    expect(page).to_have_url(f"{handle.base_url}/")
    assert handle.event_posts == []


def test_new_command_deleted_project_opens_unfiled(page: Page, chat_session_contract) -> None:
    handle = chat_session_contract
    handle.update_session(project_id="deleted", labels={"omni_project": "Deleted project"})
    handle.contract.json(
        "/v1/projects/deleted", {"error": {"message": "Project not found"}}, status=404
    )
    handle.contract.json(f"/v1/hosts/{handle.host_id}/filesystem", {"path": "/", "entries": []})
    page.goto(handle.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/new")
    composer.press("Enter")
    expect(page).to_have_url(f"{handle.base_url}/")
    assert handle.event_posts == []


def test_new_command_project_lookup_error_is_visible_and_retryable(
    page: Page, chat_session_contract
) -> None:
    handle = chat_session_contract
    handle.update_session(project_id="project-new", labels={"omni_project": "Stale name"})
    handle.contract.json("/v1/projects/project-new", {"message": "Unavailable"}, status=503)
    page.goto(handle.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/new")
    composer.press("Enter")
    expect(
        page.get_by_text("Couldn't open a new session in this project. Please try again.")
    ).to_be_visible()
    expect(page).to_have_url(handle.url)
    assert handle.event_posts == []

    project = {"id": "project-new", "name": "Current name", "config": {}}
    handle.contract.json("/v1/projects/project-new", project)
    handle.contract.json("/v1/projects", {"object": "list", "data": [project]})
    handle.contract.json(f"/v1/hosts/{handle.host_id}/filesystem", {"path": "/", "entries": []})
    composer.fill("/new")
    composer.press("Enter")
    expect(page).to_have_url(f"{handle.base_url}/?project=Current%20name")
    assert handle.event_posts == []


def test_slash_menu_tracks_real_focus_and_wrapping_keyboard_navigation(
    page: Page, chat_session_contract
) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")

    rows = page.locator(_ROWS)
    assert rows.count() >= 2, "wrap navigation needs at least two matches"
    expect(rows.first).to_have_attribute("data-active", "true")
    composer.press("ArrowUp")
    expect(rows.last).to_have_attribute("data-active", "true")
    composer.press("ArrowDown")
    expect(rows.first).to_have_attribute("data-active", "true")

    composer.blur()
    expect(rows).to_have_count(0)


def test_inline_skill_completion_preserves_text_around_the_caret(
    page: Page, chat_session_contract
) -> None:
    chat_session_contract.set_skills(
        [{"name": "review-pr", "description": "Review a pull request"}]
    )
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("please /revthis change")

    page.evaluate(
        """
        () => {
          const target = document.querySelector("textarea[aria-label='Message the agent']");
          if (!target) throw new Error("composer not found");
          target.setSelectionRange(11, 11);
          target.dispatchEvent(new Event("select", { bubbles: true }));
          document.dispatchEvent(new Event("selectionchange"));
        }
        """
    )

    skill = page.get_by_test_id("slash-menu-item-review-pr")
    expect(skill).to_have_attribute("data-active", "true")
    composer.press("Tab")

    expect(composer).to_have_value("please /review-pr this change")
    assert composer.evaluate("element => [element.selectionStart, element.selectionEnd]") == [
        18,
        18,
    ]


def test_enter_executes_a_substring_matched_builtin(page: Page, chat_session_contract) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()

    composer.fill("/ontext")
    context_row = page.get_by_test_id("slash-menu-item-context")
    expect(context_row).to_have_attribute("data-active", "true")
    composer.press("Enter")

    expect(composer).to_have_value("")
    expect(page.get_by_text("No usage data yet — send a message first.")).to_be_visible()


@pytest.mark.parametrize("phase", ["discovery", "runner-starting", "sandbox-starting"])
def test_open_menu_accepts_an_async_skill_catalog(
    page: Page, chat_session_contract, phase: str
) -> None:
    if phase != "discovery":
        chat_session_contract.set_health(runner_online=False, host_online=True)
        chat_session_contract.update_session(created_at=time.time())
    if phase == "sandbox-starting":
        chat_session_contract.update_session(sandbox_status={"stage": "provisioning"})
    chat_session_contract.set_skills(
        [{"name": "code-review", "description": "Review the current change"}]
    )
    release_skills = chat_session_contract.hold_skills()
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()

    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible()
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()
    expect(
        page.get_by_text("Skills unavailable while disconnected.", exact=True)
    ).not_to_be_visible()
    composer.fill("/review")
    expect(composer).to_have_value("/review")
    assert len(chat_session_contract.skill_requests) == 1

    release_skills()
    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible()
    skill = page.get_by_test_id("slash-menu-item-code-review")
    expect(skill).to_have_attribute("data-active", "true")
    composer.press("Tab")
    expect(composer).to_have_value("/code-review ")


def test_slash_menu_stops_loading_when_sandbox_launch_fails(
    page: Page, chat_session_contract
) -> None:
    chat_session_contract.set_health(runner_online=False, host_online=True)
    chat_session_contract.update_session(
        created_at=time.time(),
        host_id=None,
        workspace=None,
        sandbox_status={"stage": "provisioning"},
    )
    page.goto(chat_session_contract.url)
    chat_session_contract.wait_for_stream()
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")
    expect(page.get_by_text("Loading skills…", exact=True)).to_be_visible()

    chat_session_contract.emit(
        {
            "event": "session.sandbox_status",
            "data": {
                "type": "session.sandbox_status",
                "conversation_id": chat_session_contract.session_id,
                "stage": "failed",
                "error": "Test sandbox could not start",
            },
        }
    )

    expect(page.get_by_text("Loading skills…", exact=True)).not_to_be_visible()
    expect(page.get_by_text("Skills unavailable while disconnected.", exact=True)).to_be_visible()


def test_read_only_composer_skips_skill_discovery(page: Page, chat_session_contract) -> None:
    chat_session_contract.update_session(permission_level=1)
    page.goto(chat_session_contract.url)

    expect(_composer(page)).to_be_disabled()
    assert chat_session_contract.skill_requests == []


def test_native_file_paste_closes_the_slash_menu(page: Page, chat_session_contract) -> None:
    page.goto(chat_session_contract.url)
    composer = _composer(page)
    expect(composer).to_be_visible()
    composer.fill("/")
    expect(page.locator(_ROWS).first).to_be_visible()

    page.evaluate(
        """
        () => {
          const target = document.querySelector("textarea[aria-label='Message the agent']");
          if (!target) throw new Error("composer not found");
          const transfer = new DataTransfer();
          transfer.items.add(new File(["hello"], "notes.txt", { type: "text/plain" }));
          target.dispatchEvent(
            new ClipboardEvent("paste", {
              clipboardData: transfer,
              bubbles: true,
              cancelable: true,
            }),
          );
        }
        """
    )

    expect(page.get_by_text("notes.txt")).to_be_visible()
    expect(page.locator(_ROWS)).to_have_count(0)
    expect(composer).to_have_value("/")
