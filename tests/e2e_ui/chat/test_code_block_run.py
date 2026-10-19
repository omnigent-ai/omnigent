"""E2E: run a shell code block from an assistant message in the browser.

The shared ``live_server`` runs with the ``code_block_run`` flag off, so this
module owns a server and runner with the flag on. Each test seeds a settled
assistant message with shell blocks straight into the store, so no agent turn
runs. The runs themselves are real: the server checks and forwards them, the
runner executes them, and the browser renders the live and saved output.

Covers:

- the owner confirms once, sees output, stops a long run, and finds both runs
  saved after a reload;
- a viewer sees a saved run's output without Run or Stop;
- a phone-sized viewport gets the bottom sheet and full-width panel.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, Route, expect

from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import _BUILD_OUTPUT

_REPO = Path(__file__).resolve().parents[3]

_QUICK_BLOCK = "echo hello-from-run"
_LONG_BLOCK = "echo started-long-run; sleep 60; echo never-printed"
# About 60 lines over three seconds: taller than the panel's cap, still streaming.
_STREAM_BLOCK = "for i in $(seq 1 60); do echo stream-line-$i; sleep 0.05; done"
_REPLY = (
    f"Try these:\n\n```bash\n{_QUICK_BLOCK}\n```\n\nand\n\n```bash\n{_LONG_BLOCK}\n```\n"
    f"\nthen\n\n```bash\n{_STREAM_BLOCK}\n```\n"
)
_RUN_NOTE = (
    "Runs in this session are saved, visible to anyone who can view it, "
    "and sent to the agent on its next turn."
)


@pytest.fixture(scope="module")
def code_run_stack(
    built_spa: None, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[ServerRunner]:
    """A server with ``code_block_run`` on and a connected runner."""
    with server_runner(
        tmp_path_factory.mktemp("code_block_run"),
        server_cwd=_REPO,
        server_env={
            "PYTHONPATH": str(_REPO),
            "OMNIGENT_FEATURES": "code_block_run",
            "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        },
        poll_interval=0.2,
    ) as stack:
        stack.start_runner(env={"PYTHONPATH": str(_REPO)}, cwd=_REPO)
        yield stack


@pytest.fixture
def run_session(code_run_stack: ServerRunner) -> Iterator[tuple[str, str, str]]:
    """Yield ``(base_url, session_id, message_id)`` for a seeded assistant reply."""
    base_url = code_run_stack.base_url
    spec = {
        "name": "code-run-agent",
        "prompt": "Answer briefly.",
        "executor": {"harness": "openai-agents", "model": "gpt-4o-mini"},
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }
    bundle = bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()})
    response = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
    response.raise_for_status()
    session_id = response.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, code_run_stack.runner_id, timeout=10.0)
    [message] = SqlAlchemyConversationStore(code_run_stack.database_uri).append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_seeded",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": _REPLY}],
                    agent="code-run-agent",
                ),
            )
        ],
    )
    try:
        yield base_url, session_id, message.id
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _finished_runs(base_url: str, session_id: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100}, timeout=10.0
    )
    response.raise_for_status()
    return [
        item
        for item in response.json()["data"]
        if item["type"] == "code_run" and item["phase"] == "finished"
    ]


def test_owner_runs_stops_and_finds_runs_saved(
    page: Page, run_session: tuple[str, str, str]
) -> None:
    """Confirm once, see output, stop a long run, reload and find both saved."""
    base_url, session_id, _ = run_session
    page.goto(f"{base_url}/c/{session_id}")
    run_buttons = page.get_by_test_id("code-run-button")
    expect(run_buttons).to_have_count(3, timeout=30_000)

    run_buttons.nth(0).click()
    dialog = page.get_by_test_id("code-run-dialog")
    expect(dialog).to_contain_text(_QUICK_BLOCK)
    expect(dialog).to_contain_text(_RUN_NOTE)
    dialog.get_by_role("button", name="Run").click()

    panels = page.get_by_test_id("code-run-panel")
    expect(panels.nth(0)).to_contain_text("hello-from-run", timeout=30_000)
    expect(panels.nth(0).get_by_test_id("code-run-status")).to_contain_text("Exited with code 0")
    # One line fits under the cap, so there is nothing to expand.
    expect(panels.nth(0).get_by_role("button", name="Expand output")).to_have_count(0)

    # Confirmed for this session: the second Run starts without the dialog.
    run_buttons.nth(1).click()
    expect(dialog).to_have_count(0)
    long_panel = panels.nth(1)
    expect(long_panel).to_contain_text("started-long-run", timeout=30_000)
    expect(long_panel.get_by_test_id("code-run-status")).to_contain_text("Running")
    long_panel.get_by_test_id("code-run-stop").click()
    expect(long_panel.get_by_test_id("code-run-status")).to_contain_text("Stopped", timeout=30_000)

    page.reload()
    expect(panels).to_have_count(2, timeout=30_000)
    expect(panels.nth(0)).to_contain_text("hello-from-run")
    expect(panels.nth(0).get_by_test_id("code-run-status")).to_contain_text("Exited with code 0")
    expect(panels.nth(1)).to_contain_text("started-long-run")
    expect(panels.nth(1)).not_to_contain_text("never-printed")
    expect(panels.nth(1).get_by_test_id("code-run-status")).to_contain_text("Stopped")
    assert {run["status"] for run in _finished_runs(base_url, session_id)} == {"exited", "stopped"}


def _output_height(panel: Locator) -> float:
    box = panel.get_by_test_id("code-run-output").bounding_box()
    assert box is not None
    return box["height"]


def test_expand_shows_all_output_and_survives_streaming(
    page: Page, run_session: tuple[str, str, str]
) -> None:
    """Expand grows the panel, stays expanded as output streams in, and collapse restores it."""
    base_url, session_id, _ = run_session
    page.goto(f"{base_url}/c/{session_id}")
    run_buttons = page.get_by_test_id("code-run-button")
    expect(run_buttons).to_have_count(3, timeout=30_000)
    run_buttons.nth(2).click()
    page.get_by_test_id("code-run-dialog").get_by_role("button", name="Run").click()

    panel = page.get_by_test_id("code-run-panel").filter(has_text="stream-line-1")
    expand = panel.get_by_role("button", name="Expand output")
    output = panel.get_by_test_id("code-run-output")
    # Sample once an intermediate line is out (the run may or may not still be going).
    expect(panel).to_contain_text("stream-line-20", timeout=30_000)
    expect(expand).to_be_visible()
    collapsed = _output_height(panel)
    assert collapsed <= 290, collapsed

    expand.click()
    collapse = panel.get_by_role("button", name="Collapse output")
    expect(collapse).to_be_visible()
    page.wait_for_function(
        "([el, h]) => el.getBoundingClientRect().height > h + 50",
        arg=[output.element_handle(), collapsed],
    )

    # Once a later line is out, the panel is still expanded past the cap.
    expect(panel).to_contain_text("stream-line-60", timeout=30_000)
    expect(panel.get_by_test_id("code-run-status")).to_contain_text("Exited with code 0")
    expect(collapse).to_be_visible()
    expect(output).not_to_have_class(re.compile(r"\bmax-h-72\b"))
    assert _output_height(panel) > 288

    collapse.click()
    expect(expand).to_be_visible()
    assert abs(_output_height(panel) - collapsed) <= 2


def test_viewer_sees_saved_output_without_run(
    page: Page, run_session: tuple[str, str, str]
) -> None:
    """A read-only viewer sees the owner's run but can neither run nor stop."""
    base_url, session_id, message_id = run_session
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/code-runs",
        json={
            "message_id": message_id,
            "block_hash": hashlib.sha256(_QUICK_BLOCK.encode()).hexdigest(),
            "language": "shell",
            "code": _QUICK_BLOCK,
        },
        timeout=30.0,
    )
    assert response.status_code == 202, response.text
    deadline = time.monotonic() + 30
    while not _finished_runs(base_url, session_id):
        assert time.monotonic() < deadline, "the run never finished"
        time.sleep(0.2)

    # The local server is single-user, so the viewer is the owner with the
    # session snapshot's permission level rewritten to read-only.
    def as_viewer(route: Route) -> None:
        upstream = route.fetch()
        body = upstream.json()
        body["permission_level"] = 1
        route.fulfill(response=upstream, body=json.dumps(body))

    page.route(f"**/v1/sessions/{session_id}?*", as_viewer)
    page.route(f"**/v1/sessions/{session_id}", as_viewer)
    page.goto(f"{base_url}/c/{session_id}")

    panel = page.get_by_test_id("code-run-panel")
    expect(panel).to_contain_text("hello-from-run", timeout=30_000)
    expect(panel.get_by_test_id("code-run-status")).to_contain_text("Exited with code 0")
    expect(page.get_by_role("button", name="Copy Code").first).to_be_visible()
    expect(page.get_by_test_id("code-run-button")).to_have_count(0)
    expect(page.get_by_test_id("code-run-stop")).to_have_count(0)


def test_phone_run_uses_a_bottom_sheet_and_full_width_panel(
    page: Page, run_session: tuple[str, str, str]
) -> None:
    """On a phone: a bottom-sheet dialog, a touch-sized Run target, a full-width panel."""
    base_url, session_id, _ = run_session
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{base_url}/c/{session_id}")
    run_button = page.get_by_test_id("code-run-button").first
    expect(run_button).to_be_visible(timeout=30_000)
    box = run_button.bounding_box()
    assert box is not None
    # The icon matches Copy beside it; its 44px tap area extends to the left.
    right, middle = box["x"] + box["width"], box["y"] + box["height"] / 2
    for x, y in ((right - 43, middle - 21), (right - 43, middle + 21), (right - 1, middle - 21)):
        hit = page.evaluate(
            "([x, y]) => !!document.elementFromPoint(x, y)?.closest("
            "'[data-testid=code-run-button]')",
            [x, y],
        )
        assert hit, f"({x}, {y}) is outside the Run tap area {box}"

    run_button.click()
    dialog = page.get_by_test_id("code-run-dialog")
    expect(dialog).to_have_attribute("data-variant", "sheet")
    dialog.evaluate("el => Promise.all(el.getAnimations().map((a) => a.finished))")
    sheet = dialog.bounding_box()
    assert sheet is not None
    assert abs(sheet["y"] + sheet["height"] - 844) <= 2, sheet
    dialog.get_by_role("button", name="Run").click()

    panel = page.get_by_test_id("code-run-panel").first
    expect(panel).to_contain_text("hello-from-run", timeout=30_000)
    block = page.locator(".chat-code-block").first.bounding_box()
    panel_box = panel.bounding_box()
    assert block is not None and panel_box is not None
    assert abs(panel_box["width"] - block["width"]) <= 2, (panel_box, block)
