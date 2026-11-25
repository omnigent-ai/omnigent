"""Who sees the host update notice, and when a host that doesn't report the board tools is fine."""

from __future__ import annotations

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from omnigent.db.db_models import SqlProjectTicketEvent, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.host.frames import CAP_PROJECT_TOOLS
from omnigent.server.host_update import (
    ATTRIBUTED_WRITE_WINDOW_S,
    AttributedBoardWrites,
    host_update,
)

PROJECT = uuid4().hex
SESSION = uuid4().hex


def _host(capabilities: list[str], owner: str | None = "alice@example.com") -> SimpleNamespace:
    hello = SimpleNamespace(version="0.24.0.dev0", capabilities=capabilities)
    return SimpleNamespace(host_id="host-a", hello=hello, owner=owner)


class _Writes:
    def __init__(self, recent: bool) -> None:
        self._recent = recent

    def recent(self, project_id: str, session_id: str) -> bool:
        return self._recent


CONVERSATION = SimpleNamespace(id=SESSION, project_id=PROJECT)


def test_the_owner_of_a_host_without_the_tools_is_told_to_update() -> None:
    found = host_update(CONVERSATION, _host([]), "alice@example.com", _Writes(False))
    assert found.needed and found.host_version == "0.24.0.dev0" and found.host_id == "host-a"


@pytest.mark.parametrize(
    ("host", "viewer", "writes"),
    [
        (_host([CAP_PROJECT_TOOLS]), "alice@example.com", _Writes(False)),
        (_host([]), "bob@example.com", _Writes(False)),
        (_host([]), "alice@example.com", _Writes(True)),
        (None, "alice@example.com", _Writes(False)),
        (_host([], owner=None), "alice@example.com", _Writes(False)),
    ],
    ids=["reports-tools", "another-member", "writes-as-itself", "host-offline", "no-owner"],
)
def test_no_notice(host, viewer, writes) -> None:
    assert not host_update(CONVERSATION, host, viewer, writes).needed


def test_single_user_mode_counts_as_the_owner() -> None:
    assert host_update(CONVERSATION, _host([], owner=None), None, _Writes(False)).needed


def test_recent_board_writes_are_found_by_session(db_uri: str) -> None:
    writes = AttributedBoardWrites(db_uri)
    now = time.time()
    assert not writes.recent(PROJECT, SESSION, now=now)
    db = make_named_managed_session_maker(get_or_create_engine(db_uri), query_name_prefix="test")
    with db("seed_ticket_event") as session:
        session.add(
            SqlProjectTicketEvent(
                workspace_id=current_workspace_id(),
                project_id=PROJECT,
                ticket_id=uuid4().hex,
                id=uuid4().hex,
                position=0,
                kind=0,
                session_id=SESSION,
                payload={},
                created_at=int(now),
                notification_state=0,
            )
        )
    assert writes.recent(PROJECT, SESSION, now=now)
    assert not writes.recent(PROJECT, uuid4().hex, now=now)
    assert not writes.recent(PROJECT, SESSION, now=now + ATTRIBUTED_WRITE_WINDOW_S + 1)
