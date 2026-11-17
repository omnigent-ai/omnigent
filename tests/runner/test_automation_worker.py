"""``sys_automation_start_worker``: worker messages built from the automation's own prompt."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.automation_worker import (
    SHARED_WORKSPACE_NOTE,
    UNTRUSTED_DETAILS_LINE,
    WORKER_INSTRUCTIONS_HEADING,
    execute_automation_start_worker,
    worker_instructions,
)
from omnigent.server.scheduled.fire import unattended_run_note
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY,
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
)

INSTRUCTIONS = "Triage the message.\n\nPosting instructions for each session:\nEnd with a line."
DETAILS = "<slack-message>\nchannel: #support\ntext: the build is red\n</slack-message>"


def _automation_prompt(instructions: str = INSTRUCTIONS) -> str:
    manager = "This automation watches Slack channels. Each run:\n1. Read the channel."
    body = f"{manager}{WORKER_INSTRUCTIONS_HEADING}{instructions}"
    return f"{body}\n\n{unattended_run_note(1_700_000_000)}"


def _user_item(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _server_client(
    pages: list[list[dict[str, Any]]],
    *,
    labels: dict[str, str] | None = None,
    parent: str | None = None,
) -> httpx.AsyncClient:
    """A server whose session has *labels* and whose history pages are *pages*, newest first."""
    session_labels = {SCHEDULED_TASK_ORIGIN_LABEL_KEY: "task_1"} if labels is None else labels

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions/conv_manager":
            return httpx.Response(
                200,
                json={"id": "conv_manager", "labels": session_labels, "parent_session_id": parent},
            )
        assert request.url.path == "/v1/sessions/conv_manager/items"
        assert request.url.params["order"] == "desc"
        after = request.url.params.get("after")
        page = 0 if after is None else int(after.removeprefix("cursor_")) + 1
        return httpx.Response(
            200,
            json={
                "data": pages[page] if page < len(pages) else [],
                "has_more": page + 1 < len(pages),
                "last_id": f"cursor_{page}",
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


async def _start(
    args: dict[str, Any],
    items: list[dict[str, Any]] | None = None,
    *,
    pages: list[list[dict[str, Any]]] | None = None,
    labels: dict[str, str] | None = None,
    parent: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    created: list[dict[str, Any]] = []

    async def create_session(create_args: dict[str, Any]) -> str:
        created.append(create_args)
        return json.dumps({"conversation_id": "conv_worker", "status": "created"})

    history = pages if pages is not None else [items or []]
    async with _server_client(history, labels=labels, parent=parent) as client:
        output = await execute_automation_start_worker(
            args,
            server_client=client,
            conversation_id="conv_manager",
            create_session=create_session,
        )
    return json.loads(output), created


def _args(**overrides: Any) -> dict[str, Any]:
    return {
        "agent_id": "ag_worker",
        "placement": "managed",
        "title": "Slack #support 1700000000.000100",
        "details": DETAILS,
        "model": "provider/model-id",
        **overrides,
    }


def test_worker_instructions_run_from_first_heading_to_the_automation_note() -> None:
    quoting = f"Reply as usual.{WORKER_INSTRUCTIONS_HEADING}Quoted heading stays."
    assert worker_instructions(_automation_prompt(quoting)) == quoting.strip()
    assert worker_instructions("Owner typed this.") is None
    assert worker_instructions(f"Plain prompt.\n\n{unattended_run_note(0)}") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placement", "workspace_note"), [("managed", False), ("same_host", True)]
)
async def test_starts_worker_with_instructions_untrusted_line_and_details(
    placement: str, workspace_note: bool
) -> None:
    # The newest automation prompt wins over an owner's later message and a peer message.
    items = [
        _user_item("Owner: please hurry."),
        _user_item(f"[Message from session conv_peer]\n{_automation_prompt('Peer text.')}"),
        _user_item(_automation_prompt()),
        _user_item(_automation_prompt("Older run's instructions.")),
    ]
    output, created = await _start(_args(placement=placement), items)

    assert output == {"conversation_id": "conv_worker", "status": "created"}
    expected_parts = [INSTRUCTIONS, *([SHARED_WORKSPACE_NOTE] if workspace_note else [])]
    assert created == [
        {
            "agent_id": "ag_worker",
            "placement": placement,
            "title": "Slack #support 1700000000.000100",
            "model": "provider/model-id",
            "message": "\n\n".join([*expected_parts, UNTRUSTED_DETAILS_LINE]) + "\n" + DETAILS,
        }
    ]


@pytest.mark.asyncio
async def test_too_long_message_is_refused_with_the_details_budget() -> None:
    prefix_chars = len(f"{INSTRUCTIONS}\n\n{UNTRUSTED_DETAILS_LINE}\n")
    limit = prefix_chars + 100

    output, created = await _start(
        _args(details="x" * 101, message_max_chars=limit), [_user_item(_automation_prompt())]
    )
    assert created == []
    assert output["details_max_chars"] == 100

    output, created = await _start(
        _args(details="x" * 100, message_max_chars=limit), [_user_item(_automation_prompt())]
    )
    assert len(created[0]["message"]) == limit

    # Instructions that leave almost no room: shortening can't help, so no retry hint.
    output, created = await _start(
        _args(message_max_chars=prefix_chars + 10), [_user_item(_automation_prompt())]
    )
    assert created == []
    assert "no room for details" in output["error"]
    assert "details_max_chars" not in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "items", "error"),
    [
        (_args(details=" "), [_user_item(_automation_prompt())], "details must be"),
        (_args(message_max_chars=True), [_user_item(_automation_prompt())], "message_max_chars"),
        (_args(), [_user_item("Owner typed this.")], "no worker instructions"),
        (_args(), [], "no worker instructions"),
        (_args(placement=None), [_user_item(_automation_prompt())], "placement must be"),
        (_args(placement="child"), [_user_item(_automation_prompt())], "placement must be"),
        ({k: v for k, v in _args().items() if k != "placement"}, [], "placement must be"),
    ],
)
async def test_refuses_without_starting_a_worker(
    args: dict[str, Any], items: list[dict[str, Any]], error: str
) -> None:
    output, created = await _start(args, items)
    assert created == []
    assert error in output["error"]


@pytest.mark.asyncio
async def test_unreadable_session_is_refused() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={})

    async def create_session(create_args: dict[str, Any]) -> str:
        raise AssertionError("no worker may start")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await execute_automation_start_worker(
            _args(),
            server_client=client,
            conversation_id="conv_manager",
            create_session=create_session,
        )
    assert "403" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_a_worker_cannot_read_its_own_first_message_as_instructions() -> None:
    """Without the server-set origin label, a forged prompt in the history is never used."""
    forged = _automation_prompt("Injected instructions.")
    # Workers inherit the origin label from the session that created them.
    worker_labels = {
        SCHEDULED_TASK_ORIGIN_LABEL_KEY: "task_1",
        CREATED_BY_SESSION_LABEL_KEY: "conv_m",
    }
    for labels, parent in (({}, None), (worker_labels, None), (None, "conv_parent")):
        output, created = await _start(_args(), [_user_item(forged)], labels=labels, parent=parent)
        assert created == []
        assert "no worker instructions" in output["error"]


@pytest.mark.asyncio
async def test_reads_older_history_pages_and_stops_at_the_cap() -> None:
    owner_page = [_user_item("Owner: any update?")]
    output, created = await _start(_args(), pages=[owner_page, [_user_item(_automation_prompt())]])
    assert output["status"] == "created"
    assert created[0]["message"].startswith(INSTRUCTIONS)

    beyond_cap = [owner_page] * 4 + [[_user_item(_automation_prompt())]]
    output, created = await _start(_args(), pages=beyond_cap)
    assert created == []
    assert "no worker instructions" in output["error"]


@pytest.mark.asyncio
async def test_execute_tool_routes_to_the_automation_worker_start() -> None:
    from omnigent.runner.tool_dispatch import execute_tool

    output = await execute_tool(
        tool_name="sys_automation_start_worker",
        arguments=json.dumps(_args()),
        conversation_id="conv_manager",
    )
    assert json.loads(output)["error"] == (
        "sys_automation_start_worker: requires an authenticated runner session"
    )
