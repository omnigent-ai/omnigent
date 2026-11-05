"""Child discovery from the selected session's sidebar row."""

from pathlib import Path

from playwright.sync_api import Page, expect

from tests.browser_ui.chat.session_contract import (
    ChatSessionContract,
    install_chat_session_routes,
    list_payload,
    message_item,
)


def test_selected_parent_exposes_child_links(
    page: Page, chat_session_contract: ChatSessionContract, tmp_path: Path
) -> None:
    chat = chat_session_contract
    child = ChatSessionContract(
        page=page, contract=chat.contract, stream=chat.stream, session_id="sidebar-worker"
    )
    child.update_session(title="Research worker")
    child.set_items(
        [
            message_item(
                "worker-message",
                "assistant",
                "This is the child transcript.",
                response_id="worker-response",
            )
        ]
    )
    install_chat_session_routes(child)
    chat.contract.json("/v1/sessions", list_payload([chat.session_snapshot()]))
    children = [
        {
            "id": "sidebar-worker",
            "title": "Research worker",
            "busy": True,
            "pending_elicitations_count": 0,
        },
        {
            "id": "sidebar-reviewer",
            "title": "Review worker",
            "busy": False,
            "pending_elicitations_count": 1,
        },
        {
            "id": "sidebar-closed",
            "title": "Closed worker",
            "busy": False,
            "pending_elicitations_count": 0,
            "labels": {"omnigent.closed": "true"},
        },
    ]
    for summary in children:
        chat.contract.json(f"/v1/sessions/{summary['id']}/child_sessions", list_payload([]))
    chat.contract.json(f"/v1/sessions/{chat.session_id}/child_sessions", list_payload(children))
    page.goto(chat.url)
    group = page.get_by_role("list", name="Child chats")
    expect(group).to_be_visible()
    link = group.get_by_role("link", name="Research worker: Working")
    expect(link).to_have_attribute("href", "/c/sidebar-worker")
    expect(group.get_by_role("link", name="Review worker: Needs response")).to_be_visible()
    expect(group.get_by_text("Closed worker")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "sidebar-child-discovery.png"), full_page=True)
    # Navigation is pinned to the child's route, not its parent's session.
    link.click()
    expect(page).to_have_url(child.url)
    expect(page.get_by_text("This is the child transcript.", exact=True)).to_be_visible()
    expect(page.get_by_label("Message the agent")).to_be_visible()
    assert not chat.contract.violations, chat.contract.violations
