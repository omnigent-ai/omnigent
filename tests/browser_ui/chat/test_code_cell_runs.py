"""Run a Python block from an assistant message and read its output in place."""

from __future__ import annotations

import json
from typing import Any

from playwright.sync_api import Page, Route, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, message_item

_CODE = "values = [1, 2, 3]\nprint(sum(values))\n"
_REPLY = (
    f"Here is the total:\n\n```python\n{_CODE}```\n\n"
    "The same in JavaScript:\n\n```js\nconsole.log(1)\n```"
)


def _seed(chat: ChatSessionContract, *, enabled: bool) -> list[dict[str, Any]]:
    chat.features = {"code_cell_runs": enabled}
    chat.set_items([message_item("cell-reply", "assistant", _REPLY, response_id="cell-turn")])
    runs: list[dict[str, Any]] = []

    def run(route: Route) -> None:
        runs.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "completed",
                    "exit_code": 0,
                    "stdout": "6\n",
                    "stderr": "",
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                    "duration_ms": 420,
                    "error": None,
                }
            ),
        )

    chat.contract.route(
        f"{chat.base_url}/v1/sessions/{chat.session_id}/cell-runs",
        lambda route: run(route) if route.request.method == "POST" else route.fallback(),
    )
    return runs


def test_run_cell_shows_output_under_the_python_block(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    runs = _seed(chat, enabled=True)
    page.goto(chat.url)

    run = page.get_by_role("button", name="Run cell")
    expect(run).to_have_count(1)  # The js block offers no Run action.
    expect(page.get_by_role("region", name="Cell output")).to_have_count(0)
    assert runs == []

    run.click()
    output = page.get_by_role("region", name="Cell output")
    expect(output.get_by_role("status")).to_have_text("Finished in 0.4 s")
    expect(output.get_by_label("Standard output")).to_have_text("6")
    assert runs == [{"language": "python", "code": _CODE}]


def test_run_cell_is_absent_without_the_feature(
    page: Page, chat_session_contract: ChatSessionContract
) -> None:
    chat = chat_session_contract
    runs = _seed(chat, enabled=False)
    page.goto(chat.url)

    expect(page.get_by_role("button", name="Copy Code").first).to_be_visible()
    expect(page.get_by_role("button", name="Run cell")).to_have_count(0)
    assert runs == []
