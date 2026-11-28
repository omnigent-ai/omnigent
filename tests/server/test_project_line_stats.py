"""Lines added and removed by a landed commit, with generated files left out."""

from __future__ import annotations

import subprocess
from pathlib import Path

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.project_line_stats import is_generated, line_stats, parse_numstat
from tests.server.routes.test_projects_crud import (  # noqa: F401 - pytest fixtures
    project_agents,
    project_app,
    project_client,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "dev@example.com")
    _git(repo, "config", "user.name", "Dev")
    (repo / "app.py").write_text("a\nb\nc\n")
    (repo / ".gitattributes").write_text("client/*.ts linguist-generated=true\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_a_commit_counts_its_own_lines_and_leaves_generated_files_out(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app.py").write_text("a\nB\nc\nd\ne\n")  # 3 added, 1 removed
    (repo / "openapi.json").write_text("{}\n" * 40)  # built-in generated
    (repo / "client").mkdir()
    (repo / "client" / "api.ts").write_text("x\n" * 25)  # marked in .gitattributes
    (repo / "logo.png").write_bytes(b"\x89PNG\x00\x01\x02")  # binary: no lines
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "change")
    sha = _git(repo, "rev-parse", "HEAD")

    stats = line_stats(repo, sha)
    assert stats is not None
    assert (stats.added, stats.removed) == (3, 1)
    assert (stats.raw_added, stats.raw_removed, stats.files) == (68, 1, 4)
    # The result for a SHA never changes, so it's served from the cache.
    assert line_stats(repo, sha.upper()) is stats


def test_unknown_commits_and_missing_git_give_no_stats(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert line_stats(repo, "0" * 40) is None
    assert line_stats(None, "a" * 40) is None
    assert line_stats(repo, "not-a-sha") is None
    assert line_stats(tmp_path / "missing", "a" * 40) is None


def test_generated_paths_and_numstat_parsing() -> None:
    assert is_generated("openapi.json") and is_generated("web/pnpm-lock.yaml")
    assert is_generated("omnigent/api/v1/thing_pb2.py")
    assert is_generated("web/src/themePalettes.generated.css")
    assert not is_generated("omnigent/server/app.py")
    assert is_generated("client/api.ts", {"client/api.ts"})
    assert parse_numstat("3\t1\tapp.py\n-\t-\tlogo.png\nnoise\n") == [
        ("app.py", 3, 1),
        ("logo.png", 0, 0),
    ]


SHA_A, SHA_B = "a" * 40, "b" * 40


def _stats(sha: str, added: int = 3) -> dict:
    return {
        "sha": sha,
        "added": added,
        "removed": 1,
        "raw_added": added + 10,
        "raw_removed": 1,
        "files": 2,
    }


async def test_only_this_projects_agents_record_counts_for_its_landed_commits(
    project_client: httpx.AsyncClient,  # noqa: F811 - imported fixture
) -> None:
    created = (await project_client.post("/v1/projects", json={"name": "zz-lines"})).json()
    other = (await project_client.post("/v1/projects", json={"name": "zz-other"})).json()
    path = f"/v1/projects/{created['id']}"
    stages = (await project_client.get(f"{path}/tickets")).json()["stages"]
    done = next(stage["id"] for stage in stages if stage["is_complete"])
    pm = (
        await project_client.post(f"{path}/identities", json={"name": "PM", "instructions": "x"})
    ).json()
    for sha in (SHA_A, SHA_B):
        labels = [{"key": "verified", "value": sha}]
        if sha == SHA_A:
            labels.append({"key": "assignee", "value": "pm"})
        ticket = (await project_client.post(f"{path}/tickets", json={"title": sha[:4]})).json()
        await project_client.patch(
            f"{path}/tickets/{ticket['id']}", json={"labels": labels, "stage_id": done}
        )
    before = (await project_client.get(f"{path}/status/summary")).json()["totals"]
    assert (before["changes_landed"], before["changes_landed_lines_added"]) == (2, None)
    agent = {ORCHESTRATION_SOURCE_HEADER: created["main_session_id"]}
    missing = await project_client.get(f"{path}/tickets/line-stats/missing", headers=agent)
    assert missing.status_code == 200, missing.text
    assert missing.json() == {"shas": [SHA_A, SHA_B], "remaining": 0}

    recorded = await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats(SHA_A)]}, headers=agent
    )
    assert recorded.json() == {"recorded": 1}
    # A commit's counts never change: a second post keeps the first.
    again = await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats(SHA_A, added=99)]}, headers=agent
    )
    assert again.json() == {"recorded": 0}
    left = (await project_client.get(f"{path}/tickets/line-stats/missing", headers=agent)).json()
    assert left["shas"] == [SHA_B]
    summary = (await project_client.get(f"{path}/status/summary")).json()
    totals = summary["totals"]
    # The count is unchanged; lines cover the one commit that has them, credited to its role.
    assert (totals["changes_landed"], totals["changes_landed_lines_counted"]) == (2, 1)
    assert (totals["changes_landed_lines_added"], totals["changes_landed_lines_removed"]) == (3, 1)
    by_member = {member["identity_id"]: member for member in summary["members"]}
    assert (by_member[pm["id"]]["lines_added"], by_member[pm["id"]]["lines_removed"]) == (3, 1)
    items = (
        await project_client.get(f"{path}/status/items", params={"metric": "changes_landed"})
    ).json()
    details = {row["detail"]["commit"]: row["detail"] for row in items["items"]}
    counts = {key: value for key, value in _stats(SHA_A).items() if key != "sha"}
    assert details[SHA_A] == {"commit": SHA_A, **counts}
    assert details[SHA_B] == {"commit": SHA_B}

    # Refused: a person, another project's agent, and a commit not on this project's tickets.
    person = await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats(SHA_B)]}
    )
    assert person.status_code == 403
    outsider = {ORCHESTRATION_SOURCE_HEADER: other["main_session_id"]}
    foreign = await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats(SHA_B)]}, headers=outsider
    )
    assert foreign.status_code == 403
    stranger = await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats("c" * 40)]}, headers=agent
    )
    assert stranger.status_code == 400
    assert "Not a commit on this project's tickets" in stranger.text

    # A ticket with no role assigned counts for whoever landed it: here, the owner.
    await project_client.post(
        f"{path}/tickets/line-stats", json={"stats": [_stats(SHA_B, added=5)]}, headers=agent
    )
    summary = (await project_client.get(f"{path}/status/summary")).json()
    owner_row = next(member for member in summary["members"] if member["kind"] == "human")
    assert (owner_row["lines_added"], owner_row["lines_removed"]) == (5, 1)
    assert summary["totals"]["changes_landed_lines_added"] == 8
