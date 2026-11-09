"""E2E: the tasks real Claude Code records fill the Tasks tab and the chat's task preview.

Every journey runs the real ``claude`` CLI in the session terminal (the runner
boots it through the ``claude-native`` wrapper, as ``omnigent claude`` does)
with the mock LLM scripting the model's turns. Claude Code executes each scripted
tool call for real: the ``sys_journal_*`` tools travel the Omnigent MCP relay to
the runner and the server's journal route, a background shell outlives its turn,
the composer's Interrupt stops a turn, and Claude's own task tools fire the
hooks the claude-native forwarder turns into session to-dos. The browser then
checks the Tasks tab and the chat's task preview against what the agent did.

Journeys:

* a named plan whose tasks keep their dependency order (the journal refuses an
  early start with ``step_not_ready``, and Claude Code gets the reason), and a
  task that stays Running while the background shell it started runs on after
  the turn ends, then turns Done when Claude Code wakes for the shell's result;
* a task left started when the user stops the turn reads Stopped, in the tab
  and in the preview;
* a fork carries the plan the agent wrote, and the fork's own Claude Code reads
  the copied plan back through ``sys_journal_read``;
* with no plan, Claude's own task list (TaskCreate / TaskUpdate) fills the tab.

Requires the mock LLM lane (``LLM_API_KEY`` unset): the turns script exact tool
calls, which a real backend cannot guarantee.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests._helpers.polling import poll_until
from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _server_state,
    configure_mock_llm,
    native_claude_mock_session_context,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _CLAUDE_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
    reason="requires the mock LLM lane: the turns script exact tool calls, "
    "which a real backend cannot guarantee",
)

_JOURNAL_TOOLS = {
    tool: f"mcp__omnigent__sys_journal_{tool}"
    for tool in ("read", "write_plan", "write_status", "write_note")
}
# Pre-approve what the scripted turns call, so no permission prompt waits on a person.
_LAUNCH_ARGS = ["--allowedTools", ",".join([*_JOURNAL_TOOLS.values(), "Bash"])]
_PLAN_NAME = "Ship the demo"
_STEPS = [
    {"id": "deploy", "name": "Deploy the swap", "depends_on": []},
    {"id": "verify", "name": "Verify routing", "depends_on": ["deploy"]},
    {"id": "video", "name": "Record the video", "depends_on": ["verify"]},
]
# Long enough for the turn to end and the tab to be checked while the shell runs.
_BACKGROUND_S = 45
_TURN_TIMEOUT_S = 150.0
_TURN_TIMEOUT_MS = int(_TURN_TIMEOUT_S * 1000)


@pytest.fixture
def journal_claude_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A claude-native session whose Claude Code may call the journal tools and Bash unasked.

    :returns: ``(base_url, session_id)``.
    """
    with native_claude_mock_session_context(
        live_server, mock_llm_server_url, tmp_path_factory, terminal_launch_args=_LAUNCH_ARGS
    ) as pair:
        yield pair


def _call(tool_name: str, /, **arguments: object) -> dict[str, str]:
    """A scripted tool call, e.g. ``_call("Bash", command="ls")``."""
    return {"name": tool_name, "arguments": json.dumps(arguments)}


def _journal_call(tool: str, /, **arguments: object) -> dict[str, str]:
    """A scripted call to an Omnigent journal tool, e.g. ``_journal_call("read")``."""
    return _call(_JOURNAL_TOOLS[tool], **arguments)


def _script_turn(
    mock_url: str, marker: str, responses: list[dict[str, object]], required_tool: str
) -> None:
    """Script the turn that carries *marker*, one model response per entry.

    Only requests that offer *required_tool*, Claude Code's main loop, draw from
    the queue, so side requests that echo the conversation (titles, Bash prefix
    checks) can't consume the script.
    """
    reset_mock_llm(mock_url)
    for key in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_url, key, "ok")
    configure_mock_llm(mock_url, responses, match=marker, required_tools=[required_tool])


def _marker(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


def _journal(base_url: str, session_id: str) -> list[dict[str, object]]:
    """The session's journal entries, oldest first."""
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/journal",
        params={"order": "asc", "limit": 1000},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()["data"]


def _statuses(entries: list[dict[str, object]]) -> list[tuple[object, object]]:
    return [(e["step"], e["payload"]["status"]) for e in entries if e["kind"] == "status"]  # type: ignore[index]


def _session(base_url: str, session_id: str) -> dict[str, object]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=15.0)
    resp.raise_for_status()
    return resp.json()


def _tool_results(mock_url: str) -> list[str]:
    """The text of every tool result Claude Code sent the model, across all requests."""
    resp = httpx.get(f"{mock_url}/mock/message_requests", timeout=10.0)
    resp.raise_for_status()
    texts: list[str] = []
    for entry in resp.json()["requests"]:
        for message in entry["body"].get("messages") or []:
            content = message.get("content")
            if message.get("role") != "user" or not isinstance(content, list):
                continue
            for block in content:
                if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                    continue
                inner = block.get("content")
                if isinstance(inner, list):
                    texts.append(" ".join(str(part.get("text", "")) for part in inner))
                else:
                    texts.append(str(inner))
    return texts


def _wait_offered(mock_url: str, tool: str) -> None:
    """Wait until Claude Code offers *tool* to the model, failing with that reason.

    A scripted turn only plays once its guard tool is offered, so this names the
    cause when a tool never shows up (say, a Claude Code release without it).
    """

    try:
        poll_until(lambda: tool in _offered_tools(mock_url), timeout_s=_TURN_TIMEOUT_S)
    except AssertionError as exc:
        raise AssertionError(
            f"Claude Code never offered {tool} to the model; it offered "
            f"{sorted(_offered_tools(mock_url))}"
        ) from exc


def _offered_tools(mock_url: str) -> set[str]:
    """Every tool name Claude Code advertised to the model so far."""
    resp = httpx.get(f"{mock_url}/mock/message_requests", timeout=10.0)
    resp.raise_for_status()
    return {
        str(offer.get("name"))
        for entry in resp.json()["requests"]
        for offer in entry["body"].get("tools") or []
        if isinstance(offer, dict)
    }


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Open the session once its Claude Code TUI is attached, on the chat view."""
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)


def _open_tasks_tab(page: Page) -> None:
    expand = page.get_by_role("button", name="Expand right panel")
    if expand.count():
        expand.first.click()
    page.locator("[data-workspace-tab=tasks]").first.click()
    expect(page.get_by_test_id("tasks-panel")).to_be_visible(timeout=30_000)


def _row(page: Page, name: str) -> Locator:
    return page.get_by_test_id("tasks-row").filter(has_text=name).first


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_plan_tasks_keep_their_order_and_stay_running_through_background_work(
    page: Page, journal_claude_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = journal_claude_session
    _open_session(page, base_url, session_id)
    marker = _marker("plan")
    _script_turn(
        mock_llm_server_url,
        marker,
        [
            {"tool_calls": [_journal_call("write_plan", name=_PLAN_NAME, steps=_STEPS)]},
            # Too early: the video waits on verify, which waits on the deploy.
            {"tool_calls": [_journal_call("write_status", step="video", status="started")]},
            {
                "tool_calls": [
                    _journal_call("write_status", step="deploy", status="started"),
                    _call(
                        "Bash",
                        command=f"sleep {_BACKGROUND_S}",
                        description="Deploy the swap",
                        run_in_background=True,
                    ),
                ]
            },
            {"text": "The deploy is building in the background."},
            # Claude Code wakes when the shell exits, and finishes the task.
            {
                "tool_calls": [
                    _journal_call("write_status", step="deploy", status="done", text="Pod 1/1")
                ]
            },
            {"text": "The deploy finished."},
        ],
        _JOURNAL_TOOLS["write_plan"],
    )
    _send(page, f"Context marker {marker}. Plan the demo and start the deploy.")
    _wait_offered(mock_llm_server_url, _JOURNAL_TOOLS["write_plan"])
    _open_tasks_tab(page)

    # The plan's name heads the tab; its tasks appear in plan order.
    expect(page.get_by_test_id("tasks-plan-name")).to_have_text(
        _PLAN_NAME, timeout=_TURN_TIMEOUT_MS
    )
    expect(page.get_by_test_id("tasks-row")).to_have_count(3)

    # The journal refused the early start, and Claude Code got the reason back.
    poll_until(
        lambda: any(
            "step_not_ready: video waits on verify" in text
            for text in _tool_results(mock_llm_server_url)
        ),
        timeout_s=_TURN_TIMEOUT_S,
    )
    assert ("video", "started") not in _statuses(_journal(base_url, session_id))

    # The turn ends with the shell still running: the session reads idle, and the
    # deploy stays Running, not Stopped, because its work is still in flight.
    poll_until(
        lambda: (lambda s: s["status"] == "idle" and (s.get("background_task_count") or 0) > 0)(
            _session(base_url, session_id)
        ),
        timeout_s=_TURN_TIMEOUT_S,
    )
    deploy = _row(page, "Deploy the swap")
    expect(deploy).to_have_attribute("data-state", "running", timeout=15_000)
    session = _session(base_url, session_id)
    assert session["status"] == "idle" and session["background_task_count"], (
        "the deploy must read Running only while its shell is what keeps it going"
    )

    # When the shell exits, Claude Code reports back and marks the task done.
    expect(deploy).to_have_attribute(
        "data-state", "done", timeout=(_BACKGROUND_S + int(_TURN_TIMEOUT_S)) * 1000
    )
    expect(page.get_by_test_id("tasks-summary")).to_have_text("1 of 3 finished")
    expect(_row(page, "Verify routing")).to_have_attribute("data-state", "ready")
    assert _statuses(_journal(base_url, session_id)) == [("deploy", "started"), ("deploy", "done")]

    # The chat's preview counts the finished task and names the plan on hover.
    preview = page.get_by_test_id("tasks-preview")
    expect(preview).to_contain_text("1/3")
    expect(preview).to_contain_text("Next: Verify routing")
    preview.hover()
    expect(page.get_by_role("tooltip")).to_contain_text(_PLAN_NAME)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_a_task_left_started_by_a_stopped_turn_reads_stopped(
    page: Page, journal_claude_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = journal_claude_session
    _open_session(page, base_url, session_id)
    marker = _marker("stop")
    build = {"id": "build", "name": "Build the image", "depends_on": []}
    _script_turn(
        mock_llm_server_url,
        marker,
        [
            {"tool_calls": [_journal_call("write_plan", name="Build it", steps=[build])]},
            {
                "tool_calls": [
                    _journal_call("write_status", step="build", status="started"),
                    _call("Bash", command="sleep 120", description="Build the image"),
                ]
            },
            {"text": "Built."},
        ],
        _JOURNAL_TOOLS["write_plan"],
    )
    _send(page, f"Context marker {marker}. Build the image.")
    _open_tasks_tab(page)
    row = _row(page, "Build the image")
    expect(row).to_have_attribute("data-state", "running", timeout=_TURN_TIMEOUT_MS)

    # Stop the turn mid-build, the way a person does.
    page.get_by_role("button", name="Interrupt", exact=True).click()
    poll_until(lambda: _session(base_url, session_id)["status"] == "idle", timeout_s=90)
    expect(row).to_have_attribute("data-state", "stopped", timeout=15_000)
    expect(page.get_by_test_id("tasks-preview")).to_contain_text("Stopped: Build the image")
    assert not _session(base_url, session_id).get("background_task_count")
    assert _statuses(_journal(base_url, session_id)) == [("build", "started")]


@pytest.mark.nightly
@pytest.mark.timeout(900)
def test_a_fork_carries_the_plan_and_its_agent_reads_it(
    page: Page, journal_claude_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, source_id = journal_claude_session
    _open_session(page, base_url, source_id)
    marker = _marker("work")
    _script_turn(
        mock_llm_server_url,
        marker,
        [
            {"tool_calls": [_journal_call("write_plan", name=_PLAN_NAME, steps=_STEPS)]},
            {"tool_calls": [_journal_call("write_status", step="deploy", status="started")]},
            {
                "tool_calls": [
                    _journal_call("write_status", step="deploy", status="done", text="Pod 1/1"),
                    _journal_call("write_note", step="verify", body="Check the traffic-id header"),
                ]
            },
            {"text": "Deployed."},
        ],
        _JOURNAL_TOOLS["write_plan"],
    )
    _send(page, f"Context marker {marker}. Plan the demo and deploy.")
    poll_until(lambda: len(_journal(base_url, source_id)) == 4, timeout_s=_TURN_TIMEOUT_S)
    poll_until(
        lambda: _session(base_url, source_id)["status"] == "idle", timeout_s=_TURN_TIMEOUT_S
    )
    source = _journal(base_url, source_id)

    resp = httpx.post(
        f"{base_url}/v1/sessions/{source_id}/fork",
        json={"title": "Fork of the demo"},
        timeout=60.0,
    )
    resp.raise_for_status()
    fork_id = resp.json()["id"]
    try:
        # The fork carries the journal in order, as its own entries.
        copied = _journal(base_url, fork_id)
        assert [(e["position"], e["kind"], e["step"], e["payload"]) for e in copied] == [
            (e["position"], e["kind"], e["step"], e["payload"]) for e in source
        ]
        assert not {e["id"] for e in copied} & {e["id"] for e in source}

        # The fork's own Claude Code reads its copied plan back. Its marker is the
        # longer one, so the fork's turn draws from its own script.
        _bind_session_runner(base_url, fork_id, str(_server_state["runner_id"]))
        _open_session(page, base_url, fork_id)
        fork_marker = _marker("forkread")
        _script_turn(
            mock_llm_server_url,
            fork_marker,
            [{"tool_calls": [_journal_call("read")]}, {"text": "Read the plan."}],
            _JOURNAL_TOOLS["read"],
        )
        _send(page, f"Context marker {fork_marker}. Read your plan.")
        poll_until(
            lambda: any(
                "plan v1" in text and _PLAN_NAME in text and "deploy" in text
                for text in _tool_results(mock_llm_server_url)
            ),
            timeout_s=_TURN_TIMEOUT_S,
        )

        _open_tasks_tab(page)
        expect(page.get_by_test_id("tasks-plan-name")).to_have_text(_PLAN_NAME, timeout=30_000)
        expect(_row(page, "Deploy the swap")).to_have_attribute("data-state", "done")
        expect(_row(page, "Verify routing")).to_contain_text("Check the traffic-id header")
        # The parent's journal is its own: the fork's turn added nothing to it.
        assert len(_journal(base_url, source_id)) == 4
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_claudes_own_task_list_fills_the_tasks_tab(
    page: Page, journal_claude_session: tuple[str, str], mock_llm_server_url: str
) -> None:
    base_url, session_id = journal_claude_session
    _open_session(page, base_url, session_id)
    marker = _marker("todo")
    _script_turn(
        mock_llm_server_url,
        marker,
        [
            {
                "tool_calls": [
                    _call(
                        "TaskCreate",
                        subject="Add the --json flag",
                        description="Print p50 and p95 as JSON.",
                        activeForm="Adding the --json flag",
                    ),
                    _call(
                        "TaskCreate",
                        subject="Document the flag",
                        description="Describe --json in the README.",
                        activeForm="Documenting the flag",
                    ),
                ]
            },
            {"tool_calls": [_call("TaskUpdate", taskId="1", status="in_progress")]},
            {"tool_calls": [_call("TaskUpdate", taskId="1", status="completed")]},
            {"text": "Added the flag."},
        ],
        "TaskCreate",
    )
    _send(page, f"Context marker {marker}. Make a to-do list and add the flag.")
    _wait_offered(mock_llm_server_url, "TaskCreate")
    _open_tasks_tab(page)

    expect(page.get_by_test_id("tasks-row")).to_have_count(2, timeout=_TURN_TIMEOUT_MS)
    expect(_row(page, "Add the --json flag")).to_have_attribute(
        "data-state", "done", timeout=_TURN_TIMEOUT_MS
    )
    expect(_row(page, "Document the flag")).to_have_attribute("data-state", "waiting")
    preview = page.get_by_test_id("tasks-preview")
    expect(preview).to_contain_text("1/2")
    expect(preview).to_contain_text("Next: Document the flag")
    # Everything shown came from Claude's own list: the session wrote no plan.
    assert not [e for e in _journal(base_url, session_id) if e["kind"] == "plan"]
