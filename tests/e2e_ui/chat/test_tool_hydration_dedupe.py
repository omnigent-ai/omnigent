"""Tool calls render once when live replay races persisted history."""

from __future__ import annotations

import json

import httpx
from playwright.sync_api import Page, expect


def _persist_tool_call(
    client: httpx.Client,
    session_id: str,
    *,
    response_id: str,
    call_id: str,
    command: str,
) -> None:
    call = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call",
                "item_data": {
                    "agent": "watchdog",
                    "name": "sys_os_shell",
                    "arguments": json.dumps({"command": command}),
                    "call_id": call_id,
                },
                "response_id": response_id,
            },
        },
    )
    assert call.status_code == 202, call.text
    output = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "function_call_output",
                "item_data": {
                    "call_id": call_id,
                    "output": json.dumps({"stdout": "", "stderr": "", "exit_code": 0}),
                },
                "response_id": response_id,
            },
        },
    )
    assert output.status_code == 202, output.text


def _sse(event: str, data: dict[str, object]) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def test_live_tool_replay_dedupes_against_persisted_items(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Prefer persisted tool items when the live copy has no item id."""
    base_url, session_id = seeded_session
    response_id = "resp_tool_dedupe"
    call_id = "call_tool_dedupe"
    command = "git -C /workspace/HomeLab-Forge/server diff HEAD"

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        _persist_tool_call(
            client,
            session_id,
            response_id=response_id,
            call_id=call_id,
            command=command,
        )

    items_endpoint = f"/v1/sessions/{session_id}/items"
    page.add_init_script(
        f"""
        (() => {{
          const endpoint = {json.dumps(items_endpoint)};
          const originalFetch = window.fetch.bind(window);
          window.fetch = async (input, init) => {{
            const url = typeof input === "string" ? input : input.url;
            if (url.includes(endpoint)) {{
              await new Promise((resolve) => setTimeout(resolve, 300));
            }}
            return originalFetch(input, init);
          }};
        }})();
        """
    )

    stream = "".join(
        [
            _sse(
                "response.created",
                {
                    "type": "response.created",
                    "response": {
                        "id": response_id,
                        "status": "in_progress",
                        "model": "watchdog",
                        "created_at": 0,
                    },
                },
            ),
            _sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                        "status": "in_progress",
                        "model": "watchdog",
                        "response_id": response_id,
                    },
                },
            ),
            _sse(
                "response.output_item.done",
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": json.dumps(
                            {"stdout": "", "stderr": "", "exit_code": 0}
                        ),
                        "response_id": response_id,
                    },
                },
            ),
            _sse(
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": response_id,
                        "status": "completed",
                        "model": "watchdog",
                        "created_at": 0,
                    },
                },
            ),
            "data: [DONE]\n\n",
        ]
    )
    page.route(
        f"**/v1/sessions/{session_id}/stream*",
        lambda route: route.fulfill(
            status=200,
            headers={"content-type": "text/event-stream"},
            body=stream,
        ),
    )

    page.goto(f"{base_url}/c/{session_id}")

    expect(page.get_by_text(command, exact=True)).to_have_count(1, timeout=15_000)
