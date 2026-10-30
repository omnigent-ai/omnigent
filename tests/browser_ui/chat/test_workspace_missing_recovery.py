"""Browser contract for continuing a missing-workspace session elsewhere.

The built SPA uses explicit host, fork, and launch responses. This proves
browser behavior and request ordering, not native transcript reconstruction.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import pytest
from playwright.sync_api import Locator, Page, Request, Route, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    install_chat_session_routes,
    list_payload,
    message_item,
)

_HOME = "/home/demo"
_MISSING = f"{_HOME}/project-worktrees/old-branch"
_DESTINATION = f"{_HOME}/project"
_FORK_ID = "browser-recovered-session"
_PROMPT = "Continue the changes we discussed."
_ANSWER = "The implementation plan is saved in this conversation."
_SOURCE_NOTICE = "The original session’s workspace was unavailable."
_DESCRIPTION = (
    "Choose an existing directory to continue this conversation in a new session, "
    "optionally in a new git worktree. Files from the missing workspace won’t be restored."
)


@pytest.mark.parametrize(
    "width",
    [
        pytest.param(1280, id="desktop"),
        pytest.param(390, id="mobile", marks=pytest.mark.browser_context_args(has_touch=True)),
    ],
)
@pytest.mark.parametrize("surface", ["conversation", "canvas", "side-chat"])
def test_continue_missing_workspace_in_another_directory(
    page: Page,
    chat_session_contract: ChatSessionContract,
    output_path: str,
    width: int,
    surface: str,
) -> None:
    """Reject another absent path, then fork into a selected existing directory."""
    chat = chat_session_contract
    page.set_viewport_size({"width": width, "height": 900 if width > 768 else 844})
    chat.set_catalog(harness="claude-native", models=[], selected_model="")
    chat.update_session(
        title="Workspace recovery example",
        workspace=_MISSING,
        git_branch="old-branch",
        status="failed",
        runner_online=False,
        host_online=True,
    )
    chat.set_health(runner_online=False, host_online=True)
    history = [
        message_item("old-answer", "assistant", _ANSWER, response_id="old-turn"),
        message_item("old-prompt", "user", _PROMPT, response_id="old-turn"),
    ]
    chat.set_items(
        [
            {
                "id": "missing-workspace-error",
                "type": "error",
                "response_id": "failed-turn",
                "status": "completed",
                "source": "execution",
                "code": "workspace_missing",
                "message": f"workspace path does not exist: {_MISSING}",
            },
            *history,
        ]
    )
    parent: ChatSessionContract | None = None
    if surface == "side-chat":
        parent = ChatSessionContract(
            page=page,
            contract=chat.contract,
            stream=chat.stream,
            session_id="browser-parent-session",
            harness="claude-native",
            selected_model="",
            models=[],
        )
        parent.update_session(title="Unaffected parent conversation", workspace=_DESTINATION)
        parent.set_items(
            [
                message_item(
                    "parent-answer",
                    "assistant",
                    "The parent conversation stays in its own workspace.",
                    response_id="parent-turn",
                )
            ]
        )
        install_chat_session_routes(parent)
        chat.update_session(
            labels={
                "omnigent.side_chat": "1",
                "omnigent.side_chat.source_id": parent.session_id,
            }
        )
    source_snapshot = chat._session()
    operations: list[tuple[str, Any]] = []
    created = False
    fork = ChatSessionContract(
        page=page,
        contract=chat.contract,
        stream=chat.stream,
        session_id=_FORK_ID,
        harness="claude-native",
        selected_model="",
        models=[],
    )
    fork.update_session(
        title="Fork of Workspace recovery example",
        host_id=None,
        workspace=None,
        runner_online=False,
        host_online=False,
        created_at=1_704_070_800,
        labels={
            "omnigent.fork.source_id": chat.session_id,
            "omnigent.fork.carry_history": "1",
        },
    )
    fork.set_items(
        [
            {
                **item,
                "id": f"fork-{item['id']}",
                **(
                    {"code": "source_workspace_missing", "level": "info", "title": _SOURCE_NOTICE}
                    if item.get("code") == "workspace_missing"
                    else {}
                ),
            }
            for item in chat._items
        ]
    )
    install_chat_session_routes(fork)
    contract = chat.contract
    contract.json(
        "/v1/sessions",
        lambda _request: list_payload(
            [session._session() for session in (parent, chat) if session is not None]
            + ([fork._session()] if created else [])
        ),
    )
    contract.json(
        "/health",
        {
            "sessions": {
                chat.session_id: {"runner_online": False, "host_online": True},
                _FORK_ID: {"runner_online": True, "host_online": True},
                "browser-parent-session": {"runner_online": True, "host_online": True},
            }
        },
    )

    def filesystem(route: Route) -> None:
        path = unquote(urlparse(route.request.url).path).partition("/filesystem")[2]
        path = path or _HOME
        operations.append(("list", path))
        if path in {_MISSING, f"{_HOME}/project-worktrees"}:
            route.fulfill(status=404, json={"detail": "Directory does not exist"})
            return
        children = ["project"] if path == _HOME else []
        route.fulfill(
            json=list_payload(
                [
                    {
                        "name": name,
                        "path": f"{path}/{name}",
                        "type": "directory",
                        "bytes": None,
                        "modified_at": 1_704_067_200,
                    }
                    for name in children
                ]
            )
        )

    contract.route(
        re.compile(rf"/v1/hosts/{re.escape(chat.host_id)}/filesystem(?:[/?].*)?$"),
        filesystem,
    )

    def create_fork(request: Request) -> dict[str, Any]:
        nonlocal created
        operations.append(("fork", request.post_data_json))
        created = True
        return fork._session()

    def launch(request: Request) -> dict[str, str]:
        operations.append(("launch", request.post_data_json))
        fork.update_session(
            host_id=chat.host_id,
            workspace=_DESTINATION,
            runner_id="recovered-runner",
            runner_online=True,
            host_online=True,
        )
        return {"runner_id": "recovered-runner"}

    contract.json(f"/v1/sessions/{chat.session_id}/fork", create_fork, method="POST")
    contract.json(f"/v1/hosts/{chat.host_id}/runners", launch, method="POST")

    def activate(control: Locator) -> None:
        if width < 768:
            control.tap()
        else:
            control.click()

    artifacts = Path(output_path)
    if surface == "canvas":
        contract.json(
            "/v1/info",
            {
                "accounts_enabled": False,
                "single_user": True,
                "login_url": None,
                "needs_setup": False,
                "features": {"canvas": True, "harness_install": False},
                "harness_install_enabled": False,
                "installable_harnesses": [],
            },
        )
        page.goto(f"{chat.base_url}/canvas/c/{chat.session_id}")
        expect(page.get_by_test_id("canvas-workspace")).to_be_visible()
    elif parent is not None:
        contract.json(f"/v1/sessions/{parent.session_id}/policies", list_payload([]))
        contract.json("/v1/policy-registry", list_payload([]))
        contract.json(f"/v1/sessions/{parent.session_id}/owner", {"owner": None})
        state = {
            "id": parent.session_id,
            "state": {
                "open": True,
                "rightRailTab": "sidechat",
                "openSideChats": [chat.session_id],
                "selectedSideChatId": chat.session_id,
            },
        }
        page.add_init_script(
            "localStorage.setItem('omnigent:session-workspace-state', "
            f"JSON.stringify([{json.dumps(state)}]));"
            f"localStorage.setItem('omnigent.sideChatInherited:{chat.session_id}', '[]');"
        )
        page.goto(parent.url)
        if width < 768:
            activate(page.get_by_role("button", name="Conversation actions", exact=True))
            activate(page.get_by_role("menuitem", name="Side chats", exact=True))
            expect(page.get_by_test_id("side-chats-panel-drawer")).to_have_attribute(
                "data-state", "open"
            )
        else:
            panel_toggle = page.get_by_role("button", name=re.compile("right panel", re.I))
            if panel_toggle.get_attribute("aria-label") == "Expand right panel":
                activate(panel_toggle)
            activate(page.get_by_role("tab", name="Side chat 1", exact=True))
        expect(page.get_by_text(_ANSWER, exact=True)).to_be_visible()
    else:
        page.goto(chat.url)
    pill = page.get_by_test_id("error-pill")
    expect(pill).to_contain_text("The session workspace no longer exists on the host.")
    action = pill.get_by_role("button", name="Continue in another directory…", exact=True)
    expect(action).to_be_visible()
    action.scroll_into_view_if_needed()
    page.screenshot(path=str(artifacts / "missing-workspace-action.png"), full_page=True)
    activate(action)

    dialog = page.get_by_role("dialog", name=re.compile(r"^Continue in another directory"))
    expect(dialog).to_be_visible()
    expect(dialog.get_by_text(_DESCRIPTION, exact=True)).to_be_visible()
    directory = dialog.get_by_role("combobox", name="Working directory path")
    expect(directory).to_be_visible()
    expect(directory).to_have_value("")
    worktree = dialog.get_by_role("textbox", name="Git worktree (optional)")
    expect(worktree).to_be_visible()
    expect(worktree).to_have_value("")
    submit = dialog.get_by_role("button", name="Continue", exact=True)
    expect(submit).to_be_disabled()
    page.screenshot(path=str(artifacts / "choose-another-directory.png"), full_page=True)

    directory.fill(_MISSING)
    activate(submit)
    expect(dialog).to_contain_text(f"{_MISSING} doesn't exist on this host")
    assert not created
    assert all(operation not in {"fork", "launch"} for operation, _ in operations)

    directory.fill("")
    activate(dialog.get_by_role("button", name="Browse directories", exact=True))
    picker = page.get_by_test_id("workspace-picker")
    expect(picker).to_be_visible()
    activate(picker.get_by_role("option", name="project", exact=True))
    expect(picker.get_by_role("textbox", name="Current folder path")).to_have_value(_DESTINATION)
    activate(picker.get_by_test_id("workspace-picker-select"))
    expect(picker).not_to_be_visible()
    expect(directory).to_have_value(_DESTINATION)
    expect(submit).to_be_enabled()
    operations.clear()
    page.screenshot(path=str(artifacts / "selected-existing-directory.png"), full_page=True)
    activate(submit)

    expect(page).to_have_url(f"{chat.base_url}/c/{_FORK_ID}")
    expect(dialog).not_to_be_visible()
    expect(page.get_by_text(_PROMPT, exact=True)).to_be_visible()
    expect(page.get_by_text(_ANSWER, exact=True)).to_be_visible()
    expect(pill).to_have_attribute("data-level", "info")
    expect(pill).to_contain_text(_SOURCE_NOTICE)
    expect(action).to_have_count(0)
    page.reload()
    expect(page.get_by_text(_ANSWER, exact=True)).to_be_visible()
    expect(pill).to_have_attribute("data-level", "info")
    expect(pill).to_contain_text(_SOURCE_NOTICE)
    expect(action).to_have_count(0)
    page.screenshot(path=str(artifacts / "continued-in-new-session.png"), full_page=True)

    assert operations[0] == ("list", _DESTINATION)
    mutations = [(operation, body) for operation, body in operations if operation != "list"]
    assert [operation for operation, _ in mutations] == ["fork", "launch"]
    fork_body = mutations[0][1]
    assert set(fork_body) <= {"title"}
    assert fork_body.get("title", "Fork of Workspace recovery example") == (
        "Fork of Workspace recovery example"
    )
    assert mutations[1][1] == {"session_id": _FORK_ID, "workspace": _DESTINATION}
    assert chat.session_patches == []
    assert chat.event_posts == []
    assert chat._session() == source_snapshot
    if parent is not None:
        assert parent.session_patches == []
        assert parent.event_posts == []

    page.goto(chat.url)
    expect(page.get_by_text(_ANSWER, exact=True)).to_be_visible()
    expect(pill).to_contain_text("The session workspace no longer exists on the host.")
    expect(action).to_be_visible()
