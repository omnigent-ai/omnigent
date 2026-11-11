"""E2E: an ``omnigent://<host>/c/<id>`` link in a reply renders as a real link.

An agent that starts another chat links it with the desktop deep-link form.
Chat markdown used to drop that href and show ``[blocked]``. Only session links
are allowed: another custom scheme in the same reply stays blocked.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import expect

_AGENT_NAME = "hello_world"
_SESSION_LINK_TEXT = "the build worker chat"
_OTHER_SCHEME_TEXT = "the editor"


def _seed_reply(base_url: str, session_id: str, text: str) -> None:
    """Append a deterministic assistant reply to ``session_id``."""
    response = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={"type": "external_assistant_message", "data": {"agent": _AGENT_NAME, "text": text}},
        timeout=10.0,
    )
    response.raise_for_status()


def test_session_deep_link_in_reply_is_clickable(
    request: pytest.FixtureRequest,
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """The session link keeps its href; a different custom scheme stays blocked."""
    base_url, session_id, worker_id = seeded_session_pair
    deep_link = f"omnigent://{urlparse(base_url).netloc}/c/{worker_id}"
    _seed_reply(
        base_url,
        session_id,
        f"I started a worker for the build. Follow it in [{_SESSION_LINK_TEXT}]({deep_link}).\n\n"
        f"Other app links stay blocked: [{_OTHER_SCHEME_TEXT}](vscode://file/tmp/build.log).",
    )

    page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    link = page.get_by_role("link", name=_SESSION_LINK_TEXT)
    expect(link).to_be_visible(timeout=30_000)
    expect(link).to_have_attribute("href", deep_link)
    expect(link).to_be_in_viewport()
    link.hover()

    blocked = page.get_by_text(f"{_OTHER_SCHEME_TEXT} [blocked]")
    expect(blocked).to_be_in_viewport()
    expect(page.get_by_role("link", name=_OTHER_SCHEME_TEXT)).to_have_count(0)
    expect(page.get_by_text(f"{_SESSION_LINK_TEXT} [blocked]")).to_have_count(0)
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(3_000)  # Hold the final state for a readable demo.
