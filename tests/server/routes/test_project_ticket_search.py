"""Keyword search over a project's tickets: matching, ordering, paging and access."""

from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProjectTicket
from omnigent.db.utils import get_or_create_engine
from omnigent.entities.project_ticket import CommentOnTicket, CreateTicket, TicketActor
from omnigent.errors import OmnigentError
from omnigent.stores import project_ticket_store
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.project_ticket_store import ProjectTicketStore, search_snippet, search_terms
from tests.server.routes.test_projects_crud import ALICE, BOB, _as_user
from tests.server.routes.test_projects_crud import multi_user_app as multi_user_app
from tests.server.routes.test_projects_crud import multi_user_client as multi_user_client
from tests.server.routes.test_projects_crud import project_agents as project_agents


def _touch(db_uri: str, ticket_id: str, updated_at: int) -> None:
    """Pin a ticket's ``updated_at``, so ordering doesn't hang on same-second writes."""
    with Session(get_or_create_engine(db_uri)) as session:
        session.execute(
            update(SqlProjectTicket)
            .where(SqlProjectTicket.id == ticket_id)
            .values(updated_at=updated_at)
        )
        session.commit()


def _board(db_uri: str, titles: list[str]) -> tuple[str, ProjectTicketStore, list[str]]:
    """A project of tickets titled *titles*, the first least recently updated."""
    project = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "zz-search (throwaway)", ALICE)
    store = ProjectTicketStore(db_uri)
    ids = []
    for index, title in enumerate(titles):
        ticket = store.create(project.id, CreateTicket(title=title), TicketActor(ALICE))
        _touch(db_uri, ticket["id"], 1_700_000_000 + index)
        ids.append(ticket["id"])
    return project.id, store, ids


def test_search_terms_fold_case_and_drop_repeats():
    assert search_terms("  Login LOGIN  bug ") == ["login", "bug"]
    assert search_terms("   ") == []


def test_search_snippet_centres_on_the_word():
    text = "x " * 200 + "the needle sits here " + "y " * 200
    snippet = search_snippet(text, "needle")
    assert "needle" in snippet
    assert snippet.startswith("…") and snippet.endswith("…")
    assert search_snippet("short text", "short") == "short text"


def test_search_matches_every_word_across_fields_newest_first(db_uri):
    project_id, store, ids = _board(
        db_uri, ["Login button broken", "Unrelated chore", "Login page slow", "Fix signup"]
    )
    store.comment(
        project_id,
        ids[3],
        CommentOnTicket(text="Same root cause as the LOGIN crash"),
        TicketActor(ALICE),
    )
    _touch(db_uri, ids[3], 1_700_000_100)

    page = store.search(project_id, "login", user_id=ALICE)
    assert [row["id"] for row in page["data"]] == [ids[3], ids[2], ids[0]]
    assert page["data"][0]["match"]["field"] == "comment"
    assert "LOGIN crash" in page["data"][0]["match"]["snippet"]
    assert page["data"][1]["match"] == {"field": "title", "snippet": ""}
    assert page["has_more"] is False and page["last_id"] is None
    assert page["stages"]

    # Every word must hit, in any field: title plus comment counts.
    both = store.search(project_id, "signup crash", user_id=ALICE)
    assert [row["id"] for row in both["data"]] == [ids[3]]
    assert store.search(project_id, "login chore", user_id=ALICE)["data"] == []
    # A pasted short ID finds its ticket.
    assert [row["id"] for row in store.search(project_id, ids[1][:8], user_id=ALICE)["data"]] == [
        ids[1]
    ]
    with pytest.raises(OmnigentError):
        store.search(project_id, "   ", user_id=ALICE)


def test_search_pages_by_limit_and_by_scan_bound(db_uri, monkeypatch):
    project_id, store, ids = _board(db_uri, [f"Alpha {n}" for n in range(7)])
    newest_first = ids[::-1]

    first = store.search(project_id, "alpha", user_id=ALICE, limit=3)
    assert [row["id"] for row in first["data"]] == newest_first[:3]
    assert first["has_more"] is True and first["last_id"] == newest_first[2]
    second = store.search(project_id, "alpha", user_id=ALICE, limit=3, after=first["last_id"])
    assert [row["id"] for row in second["data"]] == newest_first[3:6]
    third = store.search(project_id, "alpha", user_id=ALICE, limit=3, after=second["last_id"])
    assert [row["id"] for row in third["data"]] == newest_first[6:]
    assert third["has_more"] is False and third["last_id"] is None

    # A limit that ends exactly on the oldest ticket has nothing more.
    exact = store.search(project_id, "alpha", user_id=ALICE, limit=7)
    assert len(exact["data"]) == 7 and exact["has_more"] is False

    # The scan bound stops a page early, small batches included, and resumes after it.
    monkeypatch.setattr(project_ticket_store, "SEARCH_BATCH", 2)
    bounded = store.search(project_id, "nomatch", user_id=ALICE, scan_limit=3)
    assert bounded["data"] == [] and bounded["scanned"] == 3
    assert bounded["has_more"] is True and bounded["last_id"] == newest_first[2]
    rest = store.search(project_id, "nomatch", user_id=ALICE, after=bounded["last_id"])
    assert rest["scanned"] == 4 and rest["has_more"] is False


async def test_search_route_never_widens_access(multi_user_client, db_uri):
    project_id, store, ids = _board(db_uri, ["Secret launch plan"])
    other = SqlAlchemyProjectStore(db_uri).create(uuid4().hex, "zz-bob (throwaway)", BOB)
    store.create(other.id, CreateTicket(title="Bob's launch notes"), TicketActor(BOB))
    path = f"/v1/projects/{project_id}/tickets/search"

    mine = await multi_user_client.get(path, params={"q": "launch"}, headers=_as_user(ALICE))
    assert mine.status_code == 200, mine.text
    assert [row["id"] for row in mine.json()["data"]] == ids

    # Bob can't read Alice's project, so searching it is the same 404 as reading it.
    theirs = await multi_user_client.get(path, params={"q": "launch"}, headers=_as_user(BOB))
    assert theirs.status_code == 404
    # A cursor from another project doesn't leak into this one.
    foreign = store.search(other.id, "launch", user_id=BOB)["data"][0]["id"]
    crossed = await multi_user_client.get(
        path, params={"q": "launch", "after": foreign}, headers=_as_user(ALICE)
    )
    assert crossed.status_code == 404
    blank = await multi_user_client.get(path, params={"q": ""}, headers=_as_user(ALICE))
    assert blank.status_code == 422
