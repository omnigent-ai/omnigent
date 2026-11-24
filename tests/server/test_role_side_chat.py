"""Server-side lineage check behind the role side-chat refusal."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.errors import OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.role_side_chat import (
    refuse_role_side_chat,
    refuse_role_side_chat_message,
    role_side_chat_of,
)
from omnigent.stores.conversation_store import SIDE_CHAT_SOURCE_LABEL_KEY

PROJECT = "a" * 32
LEAD, PM, OTHER = "1" * 32, "2" * 32, "3" * 32


def _session(session_id: str, **fields: Any) -> SimpleNamespace:
    defaults = {
        "project_id": PROJECT,
        "labels": {},
        "side_chat_parent_id": None,
        "parent_conversation_id": None,
    }
    return SimpleNamespace(id=session_id, **{**defaults, **fields})


class _Conversations:
    def __init__(self, *sessions: SimpleNamespace) -> None:
        self.rows = {session.id: session for session in sessions}

    def get_conversation(self, session_id: str) -> SimpleNamespace | None:
        return self.rows.get(session_id)


class _Projects:
    project = SimpleNamespace(id=PROJECT, main_session_id=LEAD, user_id=None, lead_charter="")
    identity = SimpleNamespace(id="pm", name="PM", active_session_id=PM, instructions="")

    def get(self, project_id: str, *, user_id: str | None) -> SimpleNamespace | None:
        return self.project if project_id == PROJECT else None

    def get_identity(self, project_id: str, identity_id: str, *, user_id: str | None) -> Any:
        return self.identity if identity_id == "pm" else None


def _request(source: str) -> Request:
    header = (ORCHESTRATION_SOURCE_HEADER.lower().encode(), source.encode())
    return Request({"type": "http", "headers": [header]})


def _side_chat(session_id: str, source: str) -> SimpleNamespace:
    return _session(session_id, labels={SIDE_CHAT_SOURCE_LABEL_KEY: source})


SESSIONS = _Conversations(
    _session(LEAD),
    _session(PM, labels={PROJECT_IDENTITY_LABEL_KEY: "pm"}),
    _session(OTHER),
    _side_chat("4" * 32, LEAD),
    _side_chat("5" * 32, PM),
    _side_chat("6" * 32, OTHER),
    _session("7" * 32, parent_conversation_id="5" * 32),  # sub-agent of the PM's side chat
    _side_chat("8" * 32, "4" * 32),  # side chat of the Lead's side chat
)


@pytest.mark.parametrize(
    ("caller", "role"),
    [
        ("4" * 32, (LEAD, "Lead")),
        ("5" * 32, (PM, "PM")),
        ("7" * 32, (PM, "PM")),
        ("8" * 32, (LEAD, "Lead")),
        ("6" * 32, None),
        (LEAD, None),
        (PM, None),
    ],
)
def test_role_side_chat_follows_parents_and_side_chat_sources(
    caller: str, role: tuple[str, str] | None
) -> None:
    found = role_side_chat_of(
        _request(caller), user_id=None, conversations=SESSIONS, projects=_Projects()
    )
    assert (None if found is None else (found.role_session_id, found.role_name)) == role


def test_refusals_spare_the_role_and_lead_targets() -> None:
    stores = {"user_id": None, "conversations": SESSIONS, "projects": _Projects()}
    with pytest.raises(OmnigentError, match="side chat of the project's PM"):
        refuse_role_side_chat(_request("5" * 32), action="Creating tickets", **stores)
    refuse_role_side_chat(_request(PM), action="Creating tickets", **stores)
    refuse_role_side_chat(Request({"type": "http", "headers": []}), action="x", **stores)
    for target in (PM, LEAD):
        refuse_role_side_chat_message(_request("5" * 32), target, **stores)
    with pytest.raises(OmnigentError, match="may message only"):
        refuse_role_side_chat_message(_request("5" * 32), OTHER, **stores)
