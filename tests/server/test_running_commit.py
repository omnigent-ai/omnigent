"""The running commit and a ticket's ship status, against a real temporary git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.server import project_landing
from omnigent.server import running_commit as running

BRANCH = "zz/target"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    git(repo, "add", name)
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", name)
    return git(repo, "rev-parse", "HEAD")


def ticket(sha: str | None, key: str = "approved") -> dict:
    return {
        "labels": [{"key": "kind", "value": "fix"}, *([{"key": key, "value": sha}] if sha else [])]
    }


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", BRANCH)
    commit(path, "base")
    monkeypatch.setenv("OMNIGENT_LANDING_CHECKOUT", str(path))
    monkeypatch.delenv("OMNIGENT_BUILD_COMMIT", raising=False)
    monkeypatch.setattr(project_landing, "_DEFAULT_BRANCH", BRANCH)
    running._reset_for_tests()
    yield path
    running._reset_for_tests()


def test_the_running_commit_is_read_once_from_the_build_or_the_checkout(repo, monkeypatch) -> None:
    head = git(repo, "rev-parse", "HEAD")
    assert running.capture(repo) == head
    commit(repo, "later")  # the checkout moves on; the process doesn't
    assert running.running_commit() == head
    running._reset_for_tests()
    monkeypatch.setenv("OMNIGENT_BUILD_COMMIT", "B" * 40)
    assert running.capture(repo) == "b" * 40
    running._reset_for_tests()
    monkeypatch.delenv("OMNIGENT_BUILD_COMMIT")
    assert running.capture(repo.parent) is None  # not a git checkout: nothing to report


def test_a_landed_ticket_is_live_or_waits_for_a_restart(repo) -> None:
    shipped = git(repo, "rev-parse", "HEAD")
    running.capture(repo)
    landed_later = commit(repo, "landed after the server started")
    git(repo, "checkout", "-q", "-b", "side")
    unlanded = commit(repo, "still in review")
    git(repo, "checkout", "-q", BRANCH)
    assert running.ship_status({}, ticket(shipped)) == {
        "state": "live",
        "sha": shipped,
        "running_commit": shipped,
    }
    assert running.ship_status({}, ticket(landed_later, "verified"))["state"] == "waiting_restart"
    assert running.ship_status({}, ticket(unlanded)) is None
    assert running.ship_status({}, ticket("0" * 40)) is None  # unknown to git
    assert running.ship_status({}, ticket(None)) is None


def test_no_status_without_git(repo, monkeypatch) -> None:
    sha = git(repo, "rev-parse", "HEAD")
    running.capture(repo.parent)  # a packaged deploy: no commit to compare with
    assert running.ship_status({}, ticket(sha)) is None
    running._reset_for_tests()
    running.capture(repo)
    monkeypatch.setenv("OMNIGENT_LANDING_CHECKOUT", str(repo.parent))  # not a checkout
    assert running.ship_status({}, ticket(sha)) is None


class _Board:
    """A ticket store with just what the deploy status reads."""

    def __init__(self, tickets: list[dict]) -> None:
        self.tickets = tickets

    def stages(self, project_id, *, user_id):
        return {
            "stages": [
                {"id": "code_review", "is_complete": False},
                {"id": "implemented", "is_complete": False},
                {"id": "merged", "is_complete": True},
            ]
        }

    def list(self, project_id, *, user_id, after, limit):
        return {"data": self.tickets, "has_more": False}


def test_deploy_status_counts_what_waits_for_a_restart_and_landing_drift(repo) -> None:
    from types import SimpleNamespace

    from omnigent.server.project_deploy_status import deploy_status

    live = git(repo, "rev-parse", "HEAD")
    running.capture(repo)
    merged_later = commit(repo, "merged after the server started")
    drifted = commit(repo, "on the branch but its ticket is still in review")
    git(repo, "checkout", "-q", "-b", "side")
    in_review = commit(repo, "still in review")
    git(repo, "checkout", "-q", BRANCH)

    def row(ticket_id: str, stage: str, sha: str) -> dict:
        return {"id": ticket_id, "title": ticket_id, "stage_id": stage, **ticket(sha)}

    board = _Board(
        [
            row("live", "merged", live),
            row("merged-later", "merged", merged_later),
            row("landed", "implemented", merged_later),
            row("drift", "code_review", drifted),
            row("review", "code_review", in_review),
        ]
    )
    status = deploy_status(SimpleNamespace(id="p", config={}), board, None)
    assert status["running_commit"] == live
    assert status["tip"] == drifted
    assert status["restart_needed"] is True
    assert status["merged_not_deployed"] == 1
    assert [item["ticket_id"] for item in status["not_deployed"]] == ["merged-later", "landed"]
    assert [item["ticket_id"] for item in status["landing_drift"]] == ["drift"]


def test_a_server_apart_from_the_checkout_hands_the_comparison_to_the_runner(
    repo, tmp_path, monkeypatch
) -> None:
    """An app server can't read the landing checkout; the runner beside it can."""
    from types import SimpleNamespace

    from omnigent.runner.project_tickets import _deploy_status_here
    from omnigent.server.project_deploy_status import deploy_status

    live = git(repo, "rev-parse", "HEAD")
    merged_later = commit(repo, "merged after the server started")
    elsewhere = tmp_path / "app"
    elsewhere.mkdir()
    board = _Board(
        [
            {"id": "live", "title": "live", "stage_id": "merged", **ticket(live)},
            {"id": "later", "title": "later", "stage_id": "merged", **ticket(merged_later)},
        ]
    )
    project = SimpleNamespace(id="p", config={"landing": {"checkout": str(elsewhere)}})
    monkeypatch.setenv("OMNIGENT_BUILD_COMMIT", live)
    running._reset_for_tests()

    status = deploy_status(project, board, None)
    assert status["landing"] == "unavailable"
    assert "land by hand from the host" in status["reason"]
    assert status["running_commit"] == live
    assert {row["ticket_id"] for row in status["candidates"]} == {"live", "later"}

    # The runner reads its own checkout (here the repo, via the landing env).
    here = _deploy_status_here(status)
    assert here["landing"] == "read_by_runner"
    assert here["merged_not_deployed"] == 1
    assert [row["ticket_id"] for row in here["not_deployed"]] == ["later"]

    monkeypatch.delenv("OMNIGENT_BUILD_COMMIT")
    running._reset_for_tests()
    monkeypatch.setattr(running, "_CODE_DIR", elsewhere)
    unknown = _deploy_status_here({**status, "running_commit": None})
    assert "OMNIGENT_BUILD_COMMIT" in unknown["note"]
