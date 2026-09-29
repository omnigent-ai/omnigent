"""Synchronous native-test observations; injection helpers are explicitly synthetic."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from playwright.sync_api import Page, Response, expect

from omnigent.server.schemas import SessionEventInput


class DriverSetupError(AssertionError):
    """A trigger was rejected; waiting cannot establish product behavior."""


@dataclass(frozen=True)
class AcceptedInput:
    session_id: str
    input_id: str


def post_event(client: httpx.Client, session_id: str, event: SessionEventInput) -> dict:
    """Require acceptance before a caller begins any outcome wait."""
    response = client.post(
        f"/v1/sessions/{session_id}/events", json=event.model_dump(exclude_none=True)
    )
    if response.status_code not in (200, 202):
        raise DriverSetupError(
            f"{event.type} for {session_id} rejected: {response.status_code}: {response.text}"
        )
    return response.json()


def send_message(client: httpx.Client, session_id: str, text: str) -> AcceptedInput:
    result = post_event(
        client,
        session_id,
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        ),
    )
    return _accepted_input(session_id, result)


def _accepted_input(session_id: str, result: dict) -> AcceptedInput:
    input_id = result.get("pending_id") or result.get("item_id")
    if not result.get("queued") or not isinstance(input_id, str) or not input_id:
        raise DriverSetupError(f"Message for {session_id} has no accepted input id: {result}")
    return AcceptedInput(session_id, input_id)


def send_composer_message(page: Page, session_id: str, text: str) -> AcceptedInput:
    """Drive the real composer and reject failed HTTP submissions immediately."""
    composer = page.get_by_role("textbox", name="Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    with page.expect_response(
        lambda response: _matches_message_response(response, session_id, text)
    ) as pending:
        page.get_by_role("button", name="Send", exact=True).click()
    response = pending.value
    if response.status not in (200, 202):
        raise DriverSetupError(
            f"Message for {session_id} rejected: {response.status}: {response.text()}"
        )
    return _accepted_input(session_id, response.json())


def _matches_message_response(response: Response, session_id: str, text: str) -> bool:
    if (
        urlsplit(response.url).path != f"/v1/sessions/{session_id}/events"
        or response.request.method != "POST"
    ):
        return False
    try:
        body = response.request.post_data_json
    except (json.JSONDecodeError, ValueError):
        return False
    if not isinstance(body, dict) or body.get("type") != "message":
        return False
    data = body.get("data")
    if not isinstance(data, dict) or data.get("role", "user") != "user":
        return False
    content = data.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "input_text" and block.get("text") == text
        for block in content
    )


def inject_child_start(
    client: httpx.Client,
    parent_id: str,
    *,
    subagent_id: str,
    agent_type: str,
    description: str,
    tool_use_id: str,
) -> str:
    """Synthetic forwarder event for component tests, never native execution proof."""
    result = post_event(
        client,
        parent_id,
        SessionEventInput(
            type="external_subagent_start",
            data={
                "subagent_id": subagent_id,
                "agent_type": agent_type,
                "description": description,
                "tool_use_id": tool_use_id,
            },
        ),
    )
    child_id = result.get("child_session_id")
    if not isinstance(child_id, str) or not child_id:
        raise DriverSetupError(f"Child event for {parent_id} returned no child id: {result}")
    return child_id


def _get(client: httpx.Client, path: str, **kwargs: Any) -> dict:
    response = client.get(path, **kwargs)
    response.raise_for_status()
    return response.json()


def _list(client: httpx.Client, path: str) -> list[dict]:
    result: list[dict] = []
    after = None
    while True:
        params: dict[str, Any] = {"limit": 100, "order": "asc"}
        if after:
            params["after"] = after
        page = _get(client, path, params=params)
        result.extend(page["data"])
        if not page.get("has_more"):
            return result
        cursor = page.get("last_id")
        if not cursor or cursor == after:
            raise AssertionError(f"Invalid pagination cursor for {path}")
        after = cursor


@dataclass(frozen=True)
class NativeDelegation:
    parent_id: str
    child_id: str
    call_id: str
    tool_name: str
    invocation: dict
    result: dict
    child: dict


def wait_native_delegation(
    client: httpx.Client,
    parent_id: str,
    *,
    call_id: str,
    tool_name: str,
    timeout: float = 60,
) -> NativeDelegation:
    """Observe a real Claude invocation, result and child linked to that exact call."""
    deadline = time.monotonic() + timeout
    while True:
        items = _list(client, f"/v1/sessions/{parent_id}/items")
        invocation = next(
            (
                item
                for item in items
                if item.get("type") == "function_call"
                and item.get("call_id") == call_id
                and item.get("name") == tool_name
            ),
            None,
        )
        result = next(
            (
                item
                for item in items
                if item.get("type") == "function_call_output" and item.get("call_id") == call_id
            ),
            None,
        )
        children = _list(client, f"/v1/sessions/{parent_id}/child_sessions")
        child = next(
            (
                child
                for child in children
                if child.get("parent_session_id") == parent_id
                and child.get("labels", {}).get("omnigent.claude_native.tool_use_id") == call_id
            ),
            None,
        )
        observed = {"invocation": invocation, "result": result, "child": child}
        if invocation and result and child:
            return NativeDelegation(
                parent_id, child["id"], call_id, tool_name, invocation, result, child
            )
        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Native delegation {parent_id}/{call_id} not observed: {observed}"
            )
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def _completion_from_requests(
    requests: list[dict], call_id: str, expected_text: str
) -> dict | None:
    for index, request in enumerate(requests):
        for message in request.get("messages", []):
            if message.get("role") != "user":
                continue
            blocks = message.get("content", [])
            if isinstance(blocks, str):
                blocks = [{"type": "text", "text": blocks}]
            for block in blocks:
                tool_result = block.get("type") == "tool_result"
                if tool_result and block.get("tool_use_id") != call_id:
                    continue
                if tool_result and block.get("is_error"):
                    raise AssertionError(f"Native call {call_id} returned an error")
                content = block.get("content", "") if tool_result else block.get("text", "")
                text = (
                    content
                    if isinstance(content, str)
                    else "\n".join(
                        part.get("text", "") for part in content if isinstance(part, dict)
                    )
                )
                notifications = re.findall(
                    r"<task-notification>.*?</task-notification>", text, re.S
                )
                if notifications:
                    for notification in notifications:
                        try:
                            event = ElementTree.fromstring(notification)
                        except ElementTree.ParseError:
                            continue
                        if event.findtext("tool-use-id") != call_id:
                            continue
                        status = event.findtext("status")
                        if status in {"failed", "cancelled"}:
                            raise AssertionError(f"Native call {call_id} ended with {status}")
                        if status == "completed" and expected_text in (
                            event.findtext("result") or ""
                        ):
                            return {
                                "request_index": index,
                                "kind": "notification",
                                "text": notification,
                            }
                plain_result = re.sub(
                    r"<task-notification>.*?</task-notification>", "", text, flags=re.S
                )
                if tool_result and expected_text in plain_result:
                    return {"request_index": index, "kind": "tool_result", "text": text}
    return None


def wait_claude_completion(
    mock: httpx.Client,
    *,
    call_id: str,
    expected_text: str,
    timeout: float = 60,
) -> dict:
    """Require the native parent to receive its tool's reply or completed notification."""
    deadline = time.monotonic() + timeout
    while True:
        completion = _completion_from_requests(
            _get(mock, "/mock/requests")["requests"],
            call_id,
            expected_text,
        )
        if completion is not None:
            return completion
        if time.monotonic() >= deadline:
            raise AssertionError(f"Native parent did not receive completion for {call_id}")
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def navigate_to_child(page: Page, child_id: str) -> None:
    """Click the exact Agents row and prove the navigation, with no URL fallback."""
    from tests.e2e_ui.conftest import open_right_rail

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name=re.compile("^Agents")).click()
    row = rail.locator(f'[data-testid="subagent-row"][data-child-session-id="{child_id}"]')
    expect(row).to_have_count(1)
    row.click()
    expect(page).to_have_url(re.compile(rf"/c/{re.escape(child_id)}(?:[?#].*)?$"))


@dataclass(frozen=True)
class LogWindow:
    """A byte window in one log file, captured immediately around an attempt."""

    path: Path
    start: int
    inode: int

    @classmethod
    def begin(cls, path: Path) -> LogWindow:
        stat = path.stat()
        return cls(path, stat.st_size, stat.st_ino)

    def finish(self, *, session_id: str, turn_id: str | None = None) -> list[str]:
        stat = self.path.stat()
        if stat.st_ino != self.inode or stat.st_size < self.start:
            raise AssertionError(f"Log rotated/truncated during observation: {self.path}")
        with self.path.open("rb") as handle:
            handle.seek(self.start)
            text = handle.read(stat.st_size - self.start).decode(errors="replace")
        fields = [("session(?:_id)?", session_id)]
        if turn_id:
            fields.append(("turn(?:_id)?", turn_id))
        patterns = [
            re.compile(rf'\b{field}["\']?\s*[:=]\s*["\']?{re.escape(value)}(?![\w-])')
            for field, value in fields
        ]
        return [line for line in text.splitlines() if all(p.search(line) for p in patterns)]
