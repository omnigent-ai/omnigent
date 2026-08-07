"""Synthetic end-to-end coverage for the native Computer Use panel."""

from __future__ import annotations

import base64
import re

import httpx
from playwright.sync_api import Page, expect

_ONE_PIXEL_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def _post_event(base_url: str, session_id: str, event_type: str, data: dict) -> None:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": event_type, "data": data},
        timeout=10.0,
    )
    response.raise_for_status()


def _publish_status(base_url: str, session_id: str, status: str, response_id: str) -> None:
    _post_event(
        base_url,
        session_id,
        "external_session_status",
        {"status": status, "response_id": response_id},
    )


def _seed_item(
    base_url: str,
    session_id: str,
    *,
    response_id: str,
    item_type: str,
    item_data: dict,
) -> None:
    _post_event(
        base_url,
        session_id,
        "external_conversation_item",
        {"response_id": response_id, "item_type": item_type, "item_data": item_data},
    )


def _upload_frame(base_url: str, session_id: str) -> dict:
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/resources/computer-use-frames",
        params={"source_id": "e2e:computer-use:frame:1"},
        files={"file": ("frame.png", _ONE_PIXEL_PNG, "image/png")},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()


def test_computer_use_live_frame_and_reload_parity(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A synthetic native call auto-opens, renders its frame, and survives reload."""
    base_url, session_id = seeded_session
    response_id = "resp_computer_use_e2e"
    call_id = "call_computer_use_e2e"
    presentation = {
        "kind": "computer_use",
        "provider": "codex",
        "app_name": "TextEdit",
        "app_id": "com.apple.TextEdit",
        "action_label": "Inspect document",
        "action_kinds": ["inspect"],
    }

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name="Message the agent")).to_be_visible(timeout=20_000)

    try:
        _publish_status(base_url, session_id, "running", response_id)
        _seed_item(
            base_url,
            session_id,
            response_id=response_id,
            item_type="function_call",
            item_data={
                "agent": "codex-native-ui",
                "name": "mcp__node_repl__js",
                "arguments": '{"code":"inspect TextEdit"}',
                "call_id": call_id,
                "presentation": presentation,
            },
        )

        computer_tab = page.get_by_role("tab", name="Computer running")
        expect(computer_tab).to_be_visible(timeout=15_000)
        expect(computer_tab).to_have_attribute("aria-selected", "true")
        panel = page.get_by_role("region", name="Computer Use")
        expect(panel.get_by_text("TextEdit", exact=True)).to_be_visible()
        expect(panel.get_by_text("Inspect document", exact=True)).to_be_visible()
        expect(panel.get_by_role("list", name="Computer actions")).to_contain_text("Inspecting")
        expect(panel.get_by_role("button", name="Stop")).to_be_visible()
        expect(
            panel.get_by_role("status", name="Loading computer preview", exact=True)
        ).to_be_visible()
        expect(panel.get_by_role("img", name="No computer preview available")).to_have_count(0)
        expect(panel.get_by_text("Latest frame", exact=True)).to_have_count(0)

        attachment = _upload_frame(base_url, session_id)
        _seed_item(
            base_url,
            session_id,
            response_id=response_id,
            item_type="function_call_output",
            item_data={
                "call_id": call_id,
                "output": "captured",
                "attachments": [attachment],
                "presentation": presentation,
                "presentation_final": True,
                "status": "completed",
            },
        )
        _publish_status(base_url, session_id, "idle", response_id)

        expect(
            panel.get_by_role("status", name=re.compile(r"codex computer use completed", re.I))
        ).to_be_visible(timeout=15_000)
        expect(panel.get_by_role("button", name="Stop")).to_have_count(0)
        frame = panel.get_by_role("img", name="Latest TextEdit frame")
        expect(frame).to_be_visible(timeout=15_000)
        expect(frame).to_have_js_property("naturalWidth", 1)
        expect(
            panel.get_by_role("status", name="Loading computer preview", exact=True)
        ).to_have_count(0)
        expect(panel.get_by_text("Latest frame", exact=True)).to_have_count(0)
        expect(panel.get_by_text("1 × 1", exact=True)).to_have_count(0)

        page.reload()
        restored_tab = page.get_by_role("tab", name="Computer")
        expect(restored_tab).to_be_visible(timeout=20_000)
        expect(restored_tab).to_have_attribute("aria-selected", "true")
        restored_panel = page.get_by_role("region", name="Computer Use")
        expect(
            restored_panel.get_by_role(
                "status", name=re.compile(r"codex computer use completed", re.I)
            )
        ).to_be_visible(timeout=20_000)
        expect(restored_panel.get_by_role("img", name="Latest TextEdit frame")).to_be_visible()
        expect(restored_panel.get_by_text("Latest frame", exact=True)).to_have_count(0)
        expect(restored_panel.get_by_text("1 × 1", exact=True)).to_have_count(0)
    finally:
        _publish_status(base_url, session_id, "idle", response_id)
