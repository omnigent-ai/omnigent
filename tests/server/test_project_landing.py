"""Landing queue gates and batch, against a real temporary git repository."""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.project_ticket import TicketActor
from omnigent.errors import OmnigentError
from omnigent.server import project_landing as landing

BRANCH = "zz/target"
STAGES = [{"id": "open"}, {"id": "in_progress"}, {"id": "review"}, {"id": "done"}]


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


class FakeTickets:
    def __init__(self) -> None:
        self.ticket: dict[str, Any] = {"id": "t1", "stage_id": "in_progress", "labels": []}
        self.history: list[dict[str, Any]] = []
        self.notes: list[str] = []

    def label(self, key: str, value: str, *, session_id: str | None = None) -> None:
        self.ticket["labels"] = [
            *(label for label in self.ticket["labels"] if label["key"] != key),
            {"key": key, "value": value},
        ]
        self.history.append(
            {
                "kind": "updated",
                "session_id": session_id,
                "agent_id": None,
                "payload": {"changes": {"labels": list(self.ticket["labels"])}},
            }
        )

    def get(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return dict(self.ticket)

    def stages(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"stages": STAGES}

    def events(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return {"data": self.history, "has_more": False}

    def comment(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict:
        assert actor.agent_id == landing.LANDING_AGENT_ID
        self.notes.append(body.text)
        return self.ticket

    def update(self, project_id: str, ticket_id: str, body: Any, actor: Any) -> dict:
        self.ticket["stage_id"] = body.stage_id
        return self.ticket


@pytest.fixture()
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", BRANCH)
    base = commit(repo, "base")
    restart = tmp_path / "restart.sh"
    restart.write_text(f"#!/bin/sh\necho restarted > {tmp_path / 'restarted'}\n")
    restart.chmod(0o755)
    monkeypatch.setenv("OMNIGENT_LANDING_CHECKOUT", str(repo))
    monkeypatch.setenv("OMNIGENT_LANDING_RESTART_COMMAND", str(restart))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(landing, "_DEFAULT_BRANCH", BRANCH)
    tickets = FakeTickets()
    project = SimpleNamespace(
        id="p1", user_id=None, main_session_id="lead", config={"landing": {"enabled": True}}
    )
    sent: list[tuple[str, str]] = []

    async def enqueue(session_id: str, event: Any, **_: Any) -> None:
        sent.append((session_id, event.data["content"][0]["text"]))

    async def deliver(*_: Any) -> bool:
        return True

    reviewer_session = SimpleNamespace(labels={"omnigent.project_identity_id": "rev"})
    app = SimpleNamespace(
        state=SimpleNamespace(
            project_ticket_notifications=SimpleNamespace(
                tickets=tickets,
                projects=SimpleNamespace(
                    get=lambda *a, **k: project,
                    list_identities=lambda *a, **k: [SimpleNamespace(id="rev", name="Reviewer")],
                ),
                inputs=SimpleNamespace(
                    enqueue=enqueue,
                    deliver_pending=deliver,
                    conversations=SimpleNamespace(
                        get_conversation=lambda sid: reviewer_session if sid == "rs" else None
                    ),
                ),
            )
        )
    )
    queue = landing.LandingQueue(app)
    return SimpleNamespace(
        repo=repo,
        base=base,
        tickets=tickets,
        project=project,
        queue=queue,
        sent=sent,
        tmp=tmp_path,
    )


async def submit(world: SimpleNamespace, sha: str) -> dict[str, Any]:
    return await world.queue.submit(world.project, "t1", sha, TicketActor(None, "worker"))


def feature(world: SimpleNamespace, name: str) -> str:
    git(world.repo, "checkout", "-q", "-b", name, world.base)
    sha = commit(world.repo, name)
    git(world.repo, "checkout", "-q", BRANCH)
    return sha


async def test_unapproved_or_wrongly_approved_or_risky_shas_are_refused(world) -> None:
    sha = feature(world, "f1")
    with pytest.raises(OmnigentError, match="isn't approved"):
        await submit(world, sha)
    world.tickets.label("approved", sha, session_id="some-worker")
    with pytest.raises(OmnigentError, match="Reviewer"):
        await submit(world, sha)
    world.tickets.label("approved", sha, session_id="rs")
    world.tickets.label("risk", "migration", session_id="rs")
    with pytest.raises(OmnigentError, match="risk-ok"):
        await submit(world, sha)
    world.tickets.label("risk-ok", "yes")  # set by a person
    assert (await submit(world, sha))["queued"]


async def test_an_approved_sha_lands_moves_the_ticket_and_restarts(world) -> None:
    sha = feature(world, "f2")
    world.tickets.label("approved", sha)  # the owner may approve directly
    await submit(world, sha)
    await world.queue.run_batch()
    assert git(world.repo, "rev-parse", BRANCH) == sha
    assert world.tickets.ticket["stage_id"] == "review"
    assert ("worker", f"[Landing queue] Ticket t1: `{sha}` landed on `{BRANCH}`.") in [
        (sid, text.split(" The server")[0]) for sid, text in world.sent
    ]
    for _ in range(50):
        if (world.tmp / "restarted").exists():
            break
        await asyncio.sleep(0.1)
    assert (world.tmp / "restarted").exists()


async def test_a_stale_sha_is_refused_with_rebase_advice(world) -> None:
    stale = feature(world, "f3")
    commit(world.repo, "moved-tip")  # the branch moved on without the change
    world.tickets.label("approved", stale)
    with pytest.raises(OmnigentError, match="rebase onto the tip"):
        await submit(world, stale)


async def test_off_by_default_and_agent_config_writes_refused(world) -> None:
    world.project.config = {}
    with pytest.raises(OmnigentError, match="off for this project"):
        await submit(world, "a" * 40)
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
    from omnigent.server.routes.projects import _refuse_agent_landing_write

    agent = SimpleNamespace(headers={ORCHESTRATION_SOURCE_HEADER: "s"})
    with pytest.raises(OmnigentError, match="Only the project owner"):
        _refuse_agent_landing_write(agent, {"landing": {"enabled": True}}, {})
    _refuse_agent_landing_write(agent, {"icon": "x"}, {})  # other config edits still work


def test_a_target_outside_the_repository_is_refused(world, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    with pytest.raises(OmnigentError, match="server's repository"):
        landing.resolve_target({"landing": {"enabled": True, "checkout": str(other)}})
