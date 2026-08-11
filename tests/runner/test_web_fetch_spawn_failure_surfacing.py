"""A failed ``web_fetch`` sub-agent spawn must reach the parent model.

``web_fetch`` runs its research on a ``__web_researcher`` child session, so
every way that child can fail to start is a way the parent can end up
answering from training knowledge with no sign the tool never ran. Three
seams carry that failure back:

- the child-create call the runner makes on the parent's behalf,
- the first-message forward that actually boots the child harness,
- the terminal ``failed`` status a child reports after it was accepted.

The first two must fail the tool call outright — a launching handle there
would tell the model work is under way that never started. The third lands
in the parent inbox, and the failure text must survive the trip.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import tool_dispatch
from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec, ExecutorSpec, LLMConfig
from omnigent.tools.builtins.web_fetch import RESEARCHER_NAME, WebFetchTool

_PARENT = "conv_parent"
_CHILD = "conv_child"
_TASK = "task_1"
_SPAWN_DETAIL = "harness spawn failed (see runner log)"


@pytest.fixture(autouse=True)
def _clean_runner_state() -> Iterator[None]:
    """Drop the runner-global session state each test touches."""
    yield
    runner_app.unregister_child_session(_CHILD)
    runner_app.unregister_subagent_work(_CHILD)
    runner_app._session_inboxes_ref.pop(_PARENT, None)


@pytest.fixture(autouse=True)
def _harness_cli_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the dispatch-time harness probe off the host's PATH.

    ``_execute_subagent_tool`` refuses the dispatch when the child's harness
    CLI is missing. That guard is not what these tests exercise, and whether
    ``claude`` is installed must not decide the result.
    """
    from omnigent.onboarding import harness_install

    monkeypatch.setattr(harness_install, "missing_harness_cli", lambda _harness: None)


def _parent_spec() -> AgentSpec:
    """Build a parent carrying the ``__web_researcher`` sub-agent."""
    parent = AgentSpec(
        spec_version=1,
        name="test-parent",
        llm=LLMConfig(model="openai/gpt-5.4"),
        executor=ExecutorSpec(config={"harness": "claude-sdk"}),
    )
    WebFetchTool(parent)  # appends the researcher spec to parent.sub_agents
    return parent


def _handler(
    *,
    create_status: int = 200,
    message_status: int = 202,
) -> Any:  # type: ignore[explicit-any]
    """Serve the server calls one ``web_fetch`` dispatch makes.

    :param create_status: Status for ``POST /v1/sessions`` — 503 stands in
        for a runner that could not spawn the child's harness.
    :param message_status: Status for the child's first-message POST — 503
        stands in for a harness that failed to boot on the turn path.
    """

    async def _serve(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/v1/sessions/{_PARENT}":
            return httpx.Response(200, json={"id": _PARENT, "agent_id": "ag_parent"})
        if path == f"/v1/sessions/{_PARENT}/child_sessions":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/sessions" and request.method == "POST":
            if create_status >= 400:
                return httpx.Response(
                    create_status,
                    json={"error": "harness_spawn_failed", "detail": _SPAWN_DETAIL},
                )
            return httpx.Response(200, json={"id": _CHILD, "session_id": _CHILD})
        if path == f"/v1/sessions/{_CHILD}/events":
            if message_status >= 400:
                return httpx.Response(
                    message_status,
                    json={"error": "harness_spawn_failed", "detail": _SPAWN_DETAIL},
                )
            return httpx.Response(message_status, json={"status": "accepted"})
        if path == f"/v1/sessions/{_CHILD}" and request.method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"error": str(request.url)})

    return _serve


async def _dispatch_web_fetch(handler: Any) -> tuple[str, asyncio.Queue[Any]]:  # type: ignore[explicit-any]
    """Run one ``web_fetch`` tool call against *handler*.

    :returns: The tool output string and the parent's inbox queue.
    """
    inbox: asyncio.Queue[Any] = asyncio.Queue()  # type: ignore[explicit-any]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="web_fetch",
            arguments=json.dumps({"query": "what shipped in the latest release"}),
            server_client=server_client,
            conversation_id=_PARENT,
            agent_spec=_parent_spec(),
            task_id=_TASK,
            session_inbox=inbox,
        )
    return output, inbox


@pytest.mark.asyncio
async def test_web_fetch_fails_the_tool_call_when_the_child_cannot_be_created() -> None:
    """A 503 on child-create must fail the call, not hand back a handle."""
    output, _inbox = await _dispatch_web_fetch(_handler(create_status=503))

    assert output.startswith("Error:"), output
    assert "harness_spawn_failed" in output, output
    assert "launching" not in output, output


@pytest.mark.asyncio
async def test_web_fetch_fails_the_tool_call_when_the_child_harness_will_not_boot() -> None:
    """A 503 on the child's first message must fail the call too.

    The child row exists by then, so the dispatch has to tear it down and
    report the spawn failure instead of returning a handle for work that
    never started.
    """
    output, _inbox = await _dispatch_web_fetch(_handler(message_status=503))

    assert output.startswith("Error:"), output
    assert "harness_spawn_failed" in output, output
    assert "launching" not in output, output
    assert runner_app.get_subagent_work(_CHILD) is None


async def _dispatch_and_settle(
    handler: Any,  # type: ignore[explicit-any]
    *,
    status: str,
    child_output: str,
) -> tuple[str, asyncio.Queue[Any]]:  # type: ignore[explicit-any]
    """Dispatch ``web_fetch`` and settle its child while the call is in flight.

    The tool call blocks on the researcher, so the terminal status has to be
    reported from outside it — the same shape as the runner reporting a
    child's ``external_session_status``.

    :param status: Terminal child status, e.g. ``"completed"``.
    :param child_output: Text the child reports as its result.
    :returns: The tool output string and the parent's inbox queue.
    """
    inbox: asyncio.Queue[Any] = asyncio.Queue()  # type: ignore[explicit-any]

    async def _settle() -> None:
        while runner_app.get_subagent_work(_CHILD) is None:
            await asyncio.sleep(0)
        runner_app.mark_subagent_work_terminal(_CHILD, status=status, output=child_output)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        settle = asyncio.create_task(_settle())
        output = await execute_tool(
            tool_name="web_fetch",
            arguments=json.dumps({"query": "what shipped in the latest release"}),
            server_client=server_client,
            conversation_id=_PARENT,
            agent_spec=_parent_spec(),
            task_id=_TASK,
            session_inbox=inbox,
        )
        await settle
    return output, inbox


@pytest.mark.asyncio
async def test_web_fetch_returns_the_researcher_findings_as_its_result() -> None:
    """``web_fetch`` must answer with content, not a handle for later work.

    Its description promises page content and ``is_async`` reports the call
    as synchronous, so the parent has no reason to poll an inbox before
    answering. Returning a launching handle is what lets a turn answer from
    training knowledge while the research is still pending.
    """
    findings = "The 0.9.0 release notes list a new sandbox backend."
    output, _inbox = await _dispatch_and_settle(
        _handler(), status="completed", child_output=findings
    )

    assert findings in output, output
    assert "launching" not in output, output


@pytest.mark.asyncio
async def test_web_fetch_reports_a_failed_researcher_as_a_tool_error() -> None:
    """A failed researcher must reach the model as a failed tool call.

    This is the reported defect: the spawn failure has to arrive as a tool
    error the model can disclose, not as a handle it can ignore.
    """
    output, _inbox = await _dispatch_and_settle(
        _handler(),
        status="failed",
        child_output=f"harness_spawn_failed: {_SPAWN_DETAIL}",
    )

    assert output.startswith("Error:"), output
    assert "harness_spawn_failed" in output, output


@pytest.mark.asyncio
async def test_web_fetch_result_is_not_also_delivered_to_the_parent_inbox() -> None:
    """A result returned inline must not wake the parent a second time.

    The parent already has the findings in its tool result; a duplicate
    inbox item would re-enter the turn loop for work that is done.
    """
    _output, inbox = await _dispatch_and_settle(
        _handler(), status="completed", child_output="findings"
    )

    assert inbox.empty(), inbox.get_nowait()


@pytest.mark.asyncio
async def test_web_fetch_that_outruns_its_budget_falls_back_to_the_inbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A researcher slower than the budget must not strand its result.

    The tool call gives up and says so — the model must not read a timeout
    as findings — but the late completion still has to reach the parent
    inbox rather than vanish.
    """
    monkeypatch.setattr(tool_dispatch, "_WEB_FETCH_RESULT_TIMEOUT", 0.05)

    output, inbox = await _dispatch_web_fetch(_handler())

    assert output.startswith("Error:"), output
    assert "timed out" in output, output

    ack = runner_app.mark_subagent_work_terminal(
        _CHILD, status="completed", output="late findings"
    )
    assert ack.delivered, ack.reason
    assert inbox.get_nowait()["output"] == "late findings"


@pytest.mark.asyncio
async def test_subagent_failure_after_accept_reaches_the_parent_inbox() -> None:
    """A sub-agent that fails after accept must deliver its error inbox-side.

    ``sys_session_send`` keeps its async contract — only ``web_fetch`` waits
    — so the terminal payload still has to survive the trip to the parent.
    """
    inbox: asyncio.Queue[Any] = asyncio.Queue()  # type: ignore[explicit-any]
    runner_app._session_inboxes_ref[_PARENT] = inbox
    runner_app.register_subagent_work(
        parent_session_id=_PARENT,
        child_session_id=_CHILD,
        agent=RESEARCHER_NAME,
        title=f"web_fetch_{_TASK}",
    )

    ack = runner_app.mark_subagent_work_terminal(
        _CHILD,
        status="failed",
        output=f"harness_spawn_failed: {_SPAWN_DETAIL}",
    )
    assert ack.delivered, ack.reason

    item = inbox.get_nowait()
    assert item["status"] == "failed", item
    assert "harness_spawn_failed" in item["output"], item
    assert item["conversation_id"] == _CHILD, item


@pytest.mark.asyncio
async def test_parent_wake_notice_names_the_failure() -> None:
    """The notice that wakes the parent must say the child failed.

    The parent is re-entered by this text alone; a notice that reads like a
    normal completion invites the model to answer as if the fetch worked.
    """
    notice = runner_app._format_subagent_wake_notice(
        agent=RESEARCHER_NAME,
        title=f"web_fetch_{_TASK}",
        status="failed",
        pending=1,
    )

    assert "failed" in notice, notice
    assert "sys_read_inbox" in notice, notice
