"""Loaders for the committed OpenCode v2 wire fixtures.

Fixtures live in ``tests/fixtures/opencode_v2/``. They were captured from a
real ``opencode serve`` 2.0.18 and back the ``opencode-native`` unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "opencode_v2"
OPENAPI_PATH = _FIXTURES_DIR / "openapi.json"
EVENTS_PATH = _FIXTURES_DIR / "events.ndjson"
MESSAGES_PATH = _FIXTURES_DIR / "messages.json"


def load_events() -> list[dict[str, Any]]:
    """Parse ``events.ndjson`` into decoded event dicts, in capture order."""
    text = EVENTS_PATH.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def events_of_type(type_: str) -> list[dict[str, Any]]:
    """Return every captured event whose ``type`` equals *type_*, in capture order."""
    return [event for event in load_events() if event.get("type") == type_]


def load_messages() -> dict[str, Any]:
    """Parse ``messages.json`` (a ``SessionMessagesResponse``: ``{data, cursor}``)."""
    return json.loads(MESSAGES_PATH.read_text(encoding="utf-8"))
