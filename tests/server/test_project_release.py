"""Push-prefix calculation and revert drafts, against a real temporary git repository."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.server import project_release as release
from omnigent.server.project_release import TicketShas

BRANCH = "zz/target"
UPSTREAM = f"refs/remotes/origin/{BRANCH}"
TRAILER = "Co-authored-by: Example <bot@example.com>"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def write_commit(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    message = f"change {name}\n\n{TRAILER}"
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-q", "-b", BRANCH)
    base = write_commit(path, "base.txt", "base")
    git(path, "update-ref", UPSTREAM, base)
    return path


def ticket(ticket_id: str, *shas: str, done: bool = True, **extra: str) -> TicketShas:
    return TicketShas(
        ticket_id=ticket_id,
        title=f"Ticket {ticket_id}",
        stage_id="done" if done else "implemented",
        verified_stage=done,
        shas=set(shas),
        **extra,
    )


def prefix(repo: Path, *tickets: TicketShas) -> dict:
    return release.push_prefix(repo, BRANCH, UPSTREAM, list(tickets))


def test_an_unverified_ticket_holds_the_verified_ones_behind_it(repo: Path) -> None:
    first = write_commit(repo, "a.txt", "a")
    second = write_commit(repo, "b.txt", "b")
    write_commit(repo, "c.txt", "c")
    fourth = write_commit(repo, "d.txt", "d")  # t3 is two commits, c.txt and d.txt
    held = prefix(
        repo, ticket("t1", first, done=False), ticket("t2", second), ticket("t3", fourth)
    )
    assert held["pushable_sha"] is None and held["pushable_count"] == 0
    assert held["stop"]["ticket_id"] == "t1" and "not Verified" in held["stop"]["reason"]
    assert held["blocked_verified_ticket_ids"] == ["t2", "t3"]
    assert [row["ticket_id"] for row in held["commits"]] == ["t1", "t2", "t3", "t3"]
    assert {row["ticket_id"]: row["commit_count"] for row in held["tickets"]}["t3"] == 2

    clear = prefix(repo, ticket("t1", first), ticket("t2", second), ticket("t3", fourth))
    assert clear["pushable_sha"] == fourth and clear["stop"] is None
    assert clear["blocked_verified_ticket_ids"] == []


def test_an_unmapped_commit_and_a_partly_landed_ticket_stop_the_prefix(repo: Path) -> None:
    first = write_commit(repo, "a.txt", "a")
    second = write_commit(repo, "b.txt", "b")
    write_commit(repo, "c.txt", "c")  # nobody's head follows it
    result = prefix(repo, ticket("t1", first), ticket("t2", second))
    assert result["pushable_sha"] == second
    assert result["stop"]["ticket_id"] is None and result["commits"][2]["status"] == "unmapped"

    elsewhere = git(repo, "commit-tree", git(repo, "rev-parse", f"{first}^{{tree}}"), "-m", "x")
    partial = prefix(repo, ticket("t1", first), ticket("t2", second, verified=elsewhere))
    assert partial["pushable_sha"] == first
    assert "hasn't landed" in partial["stop"]["reason"]


def test_a_ticket_interleaved_with_another_counts_only_once_all_its_commits_are_in(
    repo: Path,
) -> None:
    first = write_commit(repo, "a.txt", "a")  # t1, first half
    second = write_commit(repo, "b.txt", "b")  # t2
    third = write_commit(repo, "a2.txt", "a2")  # t1, follow-up
    write_commit(repo, "c.txt", "c")  # t3, not verified
    fourth = git(repo, "rev-parse", "HEAD")
    result = prefix(
        repo,
        ticket("t1", first, third),
        ticket("t2", second),
        ticket("t3", fourth, done=False),
    )
    assert result["pushable_sha"] == third
    blocked = prefix(
        repo,
        ticket("t1", first, third),
        ticket("t2", second, done=False),
        ticket("t3", fourth),
    )
    # t1's first commit can't ship alone: its follow-up sits after the blocker.
    assert blocked["pushable_sha"] is None and blocked["stop"]["ticket_id"] == "t2"
    assert blocked["blocked_verified_ticket_ids"] == ["t1", "t3"]


def test_a_verified_revert_neutralizes_the_commit_it_undoes(repo: Path) -> None:
    bad = write_commit(repo, "a.txt", "bad")
    good = write_commit(repo, "b.txt", "good")
    drafted = release.draft_revert(repo, BRANCH, [bad], "ptb/revert-bad")
    assert drafted["ok"] and git(repo, "rev-parse", BRANCH) == good  # no branch moved
    revert = drafted["head"]
    assert git(repo, "log", "-1", "--format=%s", revert) == 'Revert "change a.txt"'
    assert TRAILER in git(repo, "log", "-1", "--format=%B", revert)
    git(repo, "merge", "-q", "--ff-only", revert)  # the Merger lands the reviewed revert

    tickets = [
        ticket("t1", bad, done=False, reverted_by=revert),
        ticket("t2", good),
        ticket("t3", revert),
    ]
    result = prefix(repo, *tickets)
    assert result["pushable_sha"] == revert and result["stop"] is None
    assert result["commits"][0]["status"] == "reverted"
    assert result["commits"][0]["reverted_by"] == revert

    tickets[2] = ticket("t3", revert, done=False)
    held = prefix(repo, *tickets)
    assert held["pushable_sha"] is None and "reverted-by" in held["stop"]["reason"]


def test_a_revert_that_doesnt_restore_the_files_doesnt_neutralize(repo: Path) -> None:
    bad = write_commit(repo, "a.txt", "bad")
    fake = write_commit(repo, "b.txt", "not a revert")
    result = prefix(repo, ticket("t1", bad, done=False, reverted_by=fake), ticket("t2", fake))
    assert result["pushable_sha"] is None and "isn't restored" in result["stop"]["reason"]


def test_a_conflicting_revert_writes_no_ref_and_names_the_later_commits(repo: Path) -> None:
    bad = write_commit(repo, "a.txt", "bad")
    later = write_commit(repo, "a.txt", "built on bad")
    drafted = release.draft_revert(repo, BRANCH, [bad], "ptb/revert-conflict")
    assert not drafted["ok"]
    assert drafted["conflict"]["files"] == ["a.txt"]
    assert [row["sha"] for row in drafted["conflict"]["later_commits"]] == [later]
    assert git(repo, "branch", "--list", "ptb/revert-conflict") == ""
    assert git(repo, "rev-parse", BRANCH) == later and git(repo, "status", "--porcelain") == ""

    both = release.draft_revert(repo, BRANCH, [bad, later], "ptb/revert-both")
    assert both["ok"] and [row["reverts"] for row in both["drafted"]] == [later, bad]
    assert git(repo, "show", f"{both['head']}:base.txt") == "base"
    assert git(repo, "ls-tree", "--name-only", both["head"]) == "base.txt"
    with pytest.raises(OmnigentError, match="already exists"):
        release.draft_revert(repo, BRANCH, [bad, later], "ptb/revert-both")


def test_unlanded_commits_and_a_diverged_upstream_are_refused(repo: Path) -> None:
    git(repo, "checkout", "-q", "-b", "side")
    side = write_commit(repo, "s.txt", "s")
    git(repo, "checkout", "-q", BRANCH)
    with pytest.raises(OmnigentError, match="hasn't landed"):
        release.draft_revert(repo, BRANCH, [side], "ptb/revert-side")
    git(repo, "update-ref", UPSTREAM, side)
    with pytest.raises(OmnigentError, match="fast-forward"):
        prefix(repo)


def test_the_verified_stage_and_everything_after_it_count() -> None:
    stages = [
        {"id": "open", "name": "Pending"},
        {"id": "implemented", "name": "Implemented"},
        {"id": "done", "name": "Verified"},
        {"id": "merged", "name": "Merged"},
    ]
    assert release.verified_stage_ids(stages) == {"done", "merged"}
    assert release.verified_stage_ids([{"id": "a"}, {"id": "b"}, {"id": "c"}]) == {"b", "c"}


class FakeStore:
    """Two pages of tickets; one has an old approval only in its label history."""

    def __init__(self, sha: str) -> None:
        self.sha = sha

    def stages(self, *_: object, **__: object) -> dict:
        return {"stages": [{"id": "open", "name": "Pending"}, {"id": "done", "name": "Verified"}]}

    def list(self, *_: object, after: str | None = None, **__: object) -> dict:
        if after is None:
            rows = [{"id": "old", "updated_at": 5, "labels": [], "stage_id": "done"}]
            return {"data": rows, "has_more": True, "last_id": "old"}
        rows = [
            {"id": "t1", "title": "One", "updated_at": 50, "stage_id": "done", "labels": []},
            {"id": "t2", "updated_at": 50, "stage_id": "open", "labels": []},
        ]
        return {"data": rows, "has_more": False}

    def events(self, project_id: str, ticket_id: str, **__: object) -> dict:
        labels = [{"key": "approved", "value": self.sha}] if ticket_id == "t1" else []
        return {"data": [{"kind": "updated", "payload": {"changes": {"labels": labels}}}]}


def test_tickets_are_collected_from_label_history_since_the_oldest_commit() -> None:
    sha = "a" * 40
    found = release.collect_tickets(FakeStore(sha), "p", None, since=10)
    assert [(row.ticket_id, row.shas, row.verified_stage) for row in found] == [
        ("t1", {sha}, True)
    ]
