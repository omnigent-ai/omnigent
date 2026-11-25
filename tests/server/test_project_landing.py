"""Landing queue gates and batch, against a real temporary git repository."""

from __future__ import annotations

import asyncio
import shutil
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

    def label(
        self, key: str, value: str, *, session_id: str | None = None, user_id: str | None = None
    ) -> None:
        self.ticket["labels"] = [
            *(label for label in self.ticket["labels"] if label["key"] != key),
            {"key": key, "value": value},
        ]
        self.history.append(
            {
                "kind": "updated",
                "session_id": session_id,
                "agent_id": None,
                "user_id": user_id,
                "payload": {"changes": {"labels": list(self.ticket["labels"])}},
            }
        )

    def unlabel(self, key: str, *, user_id: str | None = None) -> None:
        self.ticket["labels"] = [label for label in self.ticket["labels"] if label["key"] != key]
        self.label("note", key, user_id=user_id)

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
        if body.stage_id is not None:
            self.ticket["stage_id"] = body.stage_id
        if body.comment:
            self.notes.append(body.comment)
        if body.labels is not None:
            self.ticket["labels"] = [label.model_dump() for label in body.labels]
            self.history.append(
                {
                    "kind": "updated",
                    "session_id": actor.session_id,
                    "agent_id": actor.agent_id,
                    "user_id": actor.user_id,
                    "payload": {"changes": {"labels": list(self.ticket["labels"])}},
                }
            )
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


async def test_members_cannot_stand_in_for_the_owner_and_outside_prs_need_owner_ok(
    world,
) -> None:
    world.project.user_id = "owner@example.com"
    sha = feature(world, "f-outside")
    world.tickets.label("approved", sha, user_id="member@example.com")
    with pytest.raises(OmnigentError, match="Reviewer"):
        await submit(world, sha)
    world.tickets.label("approved", sha, session_id="rs")
    world.tickets.label("risk", "auth", session_id="rs")
    world.tickets.label("risk-ok", "yes", user_id="member@example.com")
    with pytest.raises(OmnigentError, match="risk-ok"):
        await submit(world, sha)
    world.tickets.unlabel("risk-ok", user_id="owner@example.com")
    world.tickets.label("risk-ok", "yes", user_id="owner@example.com")
    world.tickets.ticket["requested_by"] = "github:octocat"
    world.tickets.label("kind", "fix", session_id="rs")
    with pytest.raises(OmnigentError, match="owner-ok"):
        await submit(world, sha)
    # An agent's owner-ok isn't adopted when the owner later edits other labels.
    world.tickets.label("owner-ok", sha, session_id="pr-worker")
    world.tickets.label("needs-human", "", user_id="owner@example.com")
    with pytest.raises(OmnigentError, match="owner-ok"):
        await submit(world, sha)
    world.tickets.unlabel("owner-ok", user_id="owner@example.com")
    world.tickets.label("owner-ok", sha, user_id="owner@example.com")
    assert (await submit(world, sha))["queued"]


async def test_a_members_feature_waits_for_a_product_approver(world) -> None:
    world.project.user_id = "owner@example.com"
    world.project.config = {
        "landing": {"enabled": True},
        "product_approvers": ["approver@example.com"],
    }
    sha = feature(world, "f-product")
    world.tickets.label("approved", sha, session_id="rs")
    world.tickets.ticket["requested_by"] = "member@example.com"
    with pytest.raises(OmnigentError, match="`kind`"):
        await submit(world, sha)
    world.tickets.label("kind", "fix", session_id="lead")
    assert (await world.queue.submit(world.project, "t1", sha, None, dry_run=True))["ok"]
    world.tickets.label("kind", "feature", session_id="rs")
    for setter in ({"session_id": "lead"}, {"user_id": "member@example.com"}):
        world.tickets.label("product-ok", "yes", **setter)
        with pytest.raises(OmnigentError, match="product-ok"):
            await world.queue.submit(world.project, "t1", sha, None, dry_run=True)
        world.tickets.unlabel("product-ok")
    world.tickets.label("product-ok", "yes", user_id="approver@example.com")
    assert (await world.queue.submit(world.project, "t1", sha, None, dry_run=True))["ok"]
    assert world.queue.pending == []  # a dry run never queues
    world.tickets.unlabel("product-ok")
    world.tickets.ticket["requested_by"] = "owner@example.com"  # the owner's own feature
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


async def test_a_stale_sha_that_conflicts_is_refused_with_rebase_advice(world) -> None:
    stale = feature(world, "f3")
    write_commit(world.repo, "f3", "the tip's own f3")  # the tip changed the same file
    world.tickets.label("approved", stale, session_id="rs")
    with pytest.raises(OmnigentError, match="doesn't cherry-pick cleanly"):
        await submit(world, stale)
    assert git(world.repo, "rev-parse", BRANCH) != stale


async def test_off_by_default_and_agent_config_writes_refused(world) -> None:
    world.project.config = {}
    with pytest.raises(OmnigentError, match="off for this project"):
        await submit(world, "a" * 40)
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
    from omnigent.server.routes.projects import _refuse_owner_config_write

    agent = SimpleNamespace(headers={ORCHESTRATION_SOURCE_HEADER: "s"})
    person = SimpleNamespace(headers={})
    with pytest.raises(OmnigentError, match="Only the project owner"):
        _refuse_owner_config_write(agent, {"landing": {"enabled": True}}, {}, is_owner=True)
    _refuse_owner_config_write(agent, {"icon": "x"}, {}, is_owner=True)  # other edits work
    for key, value in (("landing", {"enabled": True}), ("product_approvers", ["a@example.com"])):
        with pytest.raises(OmnigentError, match="Only the project owner"):
            _refuse_owner_config_write(person, {key: value}, {}, is_owner=False)
        _refuse_owner_config_write(person, {key: value}, {}, is_owner=True)


def test_a_target_outside_the_repository_is_refused(world, tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    with pytest.raises(OmnigentError, match="server's repository"):
        landing.resolve_target({"landing": {"enabled": True, "checkout": str(other)}})


async def test_a_restart_inside_the_batch_window_keeps_the_queued_land(world) -> None:
    sha = feature(world, "f4")
    world.tickets.label("approved", sha)
    await submit(world, sha)
    # The server restarts before the batch runs: a new queue picks the land back up.
    resumed = landing.LandingQueue(world.queue.app)
    await resumed._resume_pending(delay=0)
    assert git(world.repo, "rev-parse", BRANCH) == sha


# -- approval carry ----------------------------------------------------------------------

TRAILER = "Co-authored-by: Example <bot@example.com>"


def write_commit(repo: Path, name: str, content: str, *, trailer: str | None = TRAILER) -> str:
    (repo / name).parent.mkdir(parents=True, exist_ok=True)
    (repo / name).write_text(content)
    git(repo, "add", name)
    message = f"{name}\n\n{trailer}" if trailer else name
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", message)
    return git(repo, "rev-parse", "HEAD")


def branch(world: SimpleNamespace, name: str, files: dict[str, str], start: str) -> str:
    """A feature branch from *start* with one commit per file; returns its head."""
    git(world.repo, "checkout", "-q", "-B", name, start)
    head = start
    for path, content in files.items():
        head = write_commit(world.repo, path, content)
    git(world.repo, "checkout", "-q", BRANCH)
    return head


def approve_then_rebase(world: SimpleNamespace, files: dict[str, str]) -> tuple[str, str]:
    """Approve a branch, move the tip, rebase onto it and let the worker's new SHA clear it."""
    approved = branch(world, "feat", files, world.base)
    world.tickets.label("approved", approved, session_id="rs")
    commit(world.repo, "unrelated")  # the integration tip moves on
    git(world.repo, "checkout", "-q", "-B", "rebased", approved)
    git(
        world.repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.com",
        "rebase",
        "-q",
        BRANCH,
    )
    rebased = git(world.repo, "rev-parse", "HEAD")
    git(world.repo, "checkout", "-q", BRANCH)
    world.tickets.unlabel("approved", user_id=None)  # the worker's new head cleared it
    world.tickets.history[-1]["session_id"] = "worker"
    return approved, rebased


async def test_an_identical_patch_carries_the_approval_and_lands(world) -> None:
    approved, rebased = approve_then_rebase(world, {"a.txt": "a", "b.txt": "b"})
    queued = await submit(world, rebased)
    assert queued["carried_from"] == approved and not queued["rebased"]
    await world.queue.run_batch()
    assert git(world.repo, "rev-parse", BRANCH) == rebased
    assert {"key": "approved", "value": rebased} in world.tickets.ticket["labels"]
    carry = [note for note in world.tickets.notes if "approval carried" in note]
    assert carry and approved in carry[0] and rebased in carry[0]
    assert world.tickets.ticket["stage_id"] == "review"


async def test_the_queue_rebases_a_stale_approved_sha_itself(world) -> None:
    approved = branch(world, "feat", {"a.txt": "a"}, world.base)
    world.tickets.label("approved", approved, session_id="rs")
    commit(world.repo, "unrelated")
    dry = await world.queue.submit(world.project, "t1", approved, None, dry_run=True)
    assert dry["rebased"] and dry["carried_from"] == approved
    await submit(world, approved)
    await world.queue.run_batch()
    tip = git(world.repo, "rev-parse", BRANCH)
    assert tip != approved and git(world.repo, "show", f"{tip}:a.txt") == "a"
    assert TRAILER in git(world.repo, "log", "-1", "--format=%B", tip)
    assert any("cherry-picked onto the tip" in note for note in world.tickets.notes)
    assert ("worker", f"[Landing queue] Ticket t1: `{tip}` landed on `{BRANCH}`.") in [
        (sid, text.split(" The server")[0]) for sid, text in world.sent
    ]


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"a.txt": "a, changed", "b.txt": "b"}, "different patch"),
        ({"a.txt": "  a", "b.txt": "b"}, "different patch"),  # indentation-only
        ({"a.txt": "a", "b.txt": "b", "c.txt": "c"}, "3 commit"),  # an added commit
        ({"b.txt": "b"}, "1 commit"),  # a dropped commit
    ],
)
async def test_a_changed_patch_needs_re_approval(world, change, reason) -> None:
    approved, _ = approve_then_rebase(world, {"a.txt": "a", "b.txt": "b"})
    changed = branch(world, "changed", change, git(world.repo, "rev-parse", BRANCH))
    with pytest.raises(OmnigentError, match=f"{reason}.*re-approve"):
        await submit(world, changed)
    assert approved != changed


async def test_different_trailers_or_a_withdrawn_approval_or_an_outside_pr_dont_carry(
    world,
) -> None:
    approved, rebased = approve_then_rebase(world, {"a.txt": "a"})
    git(world.repo, "checkout", "-q", "-B", "untrailed", BRANCH)
    untrailed = write_commit(world.repo, "a.txt", "a", trailer=None)
    git(world.repo, "checkout", "-q", BRANCH)
    with pytest.raises(OmnigentError, match="trailers"):
        await submit(world, untrailed)
    world.tickets.ticket["requested_by"] = "github:octocat"
    world.tickets.ticket["labels"].append({"key": "kind", "value": "fix"})
    world.tickets.label("owner-ok", rebased)  # even the owner's OK doesn't carry a PR
    with pytest.raises(OmnigentError, match="isn't approved"):
        await submit(world, rebased)
    world.tickets.ticket.pop("requested_by")
    world.tickets.label("kind", "fix", session_id="rs")  # the Reviewer edits without approving
    with pytest.raises(OmnigentError, match="isn't approved"):
        await submit(world, rebased)
    assert approved


async def test_a_carried_approval_that_fails_its_targeted_tests_falls_back(world) -> None:
    files = {
        "tests/test_carry.py": "import pathlib\n\ndef test_tip():\n"
        "    assert not pathlib.Path('broken').exists()\n"
    }
    approved, rebased = approve_then_rebase(world, files)
    world.tickets.label("targeted-tests", "tests/test_carry.py", session_id="worker")
    before = git(world.repo, "rev-parse", BRANCH)
    assert landing.safety_net(world.repo, rebased, before, ["tests/test_carry.py"]) is None
    commit(world.repo, "broken")  # the new tip breaks the identical patch's test
    git(world.repo, "checkout", "-q", "-B", "rebased2", rebased)
    git(world.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "rebase", "-q", BRANCH)
    again = git(world.repo, "rev-parse", "HEAD")
    git(world.repo, "checkout", "-q", BRANCH)
    tip = git(world.repo, "rev-parse", BRANCH)
    await submit(world, again)
    await world.queue.run_batch()
    assert git(world.repo, "rev-parse", BRANCH) == tip
    assert any("fails on the new tip" in text for _, text in world.sent)
    assert not any(label["key"] == "approved" for label in world.tickets.ticket["labels"])
    world.tickets.label("targeted-tests", "--co; rm -rf /", session_id="worker")
    assert landing.targeted_tests(world.tickets.ticket) is None
    assert approved


def test_pure_rebase_detection_and_uncounted_review_rounds(world) -> None:
    from omnigent.server.routes import project_tickets as routes

    approved, rebased = approve_then_rebase(world, {"a.txt": "a"})
    changed = branch(world, "changed", {"a.txt": "other"}, git(world.repo, "rev-parse", BRANCH))
    config = world.project.config
    assert landing.is_pure_rebase(config, approved, rebased)
    assert not landing.is_pure_rebase(config, approved, changed)
    assert not landing.is_pure_rebase(config, approved, "f" * 40)
    routes._review_rounds.pop("t-rounds", None)
    routes._rebase_only_rounds.add("t-rounds")
    routes._count_review_round("t-rounds")  # the re-approval of a pure rebase
    assert routes._review_rounds.get("t-rounds", 0) == 0
    routes._count_review_round("t-rounds")
    assert routes._review_rounds["t-rounds"] == 1


def test_the_safety_net_hides_the_servers_environment(world, monkeypatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATABASE_URI", "sqlite:///must-not-leak.db")
    probe = (
        "import os\n\ndef test_env():\n"
        "    assert not any(k.startswith('OMNIGENT_') for k in os.environ)\n"
        f"    assert os.environ['HOME'] != {str(Path.home())!r}\n"
    )
    _, rebased = approve_then_rebase(world, {"tests/test_env.py": probe})
    tip = git(world.repo, "rev-parse", BRANCH)
    assert landing.safety_net(world.repo, rebased, tip, ["tests/test_env.py"]) is None


def test_only_a_plausible_new_head_supersedes_an_approval(world) -> None:
    config = world.project.config
    approved = branch(world, "feat", {"a.txt": "a", "b.txt": "b"}, world.base)
    first_commit = git(world.repo, "rev-parse", f"{approved}~1")
    new_head = branch(world, "feat2", {"c.txt": "c"}, approved)
    tip = git(world.repo, "rev-parse", BRANCH)
    assert landing.could_supersede(config, approved, new_head)
    assert not landing.could_supersede(config, approved, tip)  # the integration tip
    assert not landing.could_supersede(config, approved, world.base)  # a base
    assert not landing.could_supersede(config, approved, first_commit)  # an older commit
    assert landing.could_supersede(config, approved, "e" * 40)  # unknown here: can't tell
    git(world.repo, "merge", "-q", "--ff-only", approved)  # the approval landed
    assert not landing.could_supersede(config, approved, new_head)
    assert landing.could_supersede({"landing": {"checkout": "/nonexistent"}}, approved, new_head)


def install_pnpm_layout(repo: Path) -> None:
    """Untracked pnpm-style packages: the checker lives in the root ``node_modules/.pnpm``
    and ``web/node_modules/.bin`` holds a shim that reaches it through ``../../../``,
    as pnpm's shims do; it fails when ``web/src/broken.ts`` exists."""
    (repo / ".gitignore").write_text("node_modules\n")
    store = repo / "node_modules" / ".pnpm" / "checker"
    store.mkdir(parents=True)
    (store / "run.sh").write_text(
        '#!/bin/sh\n[ ! -e src/broken.ts ] || { echo "broken.ts"; exit 1; }\n'
    )
    shims = repo / "web" / "node_modules" / ".bin"
    shims.mkdir(parents=True)
    shim = shims / "check"
    shim.write_text(
        '#!/bin/sh\nbasedir=$(dirname "$0")\n'
        'exec sh "$basedir/../../../node_modules/.pnpm/checker/run.sh" "$@"\n'
    )
    shim.chmod(0o755)
    scripts = {"type-check": "check", "lint": "check", "build": "check"}
    (repo / "web" / "package.json").write_text(
        '{"name": "web", "private": true, "scripts": '
        + ", ".join(f'"{name}": "{cmd}"' for name, cmd in scripts.items()).join("{}")
        + "}\n"
    )
    git(repo, "add", ".gitignore", "web/package.json")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "web")


@pytest.mark.skipif(shutil.which("npm") is None, reason="needs npm")
async def test_a_carried_web_change_runs_its_checks_with_the_root_packages(world) -> None:
    """The scratch worktree gets the root node_modules too, so a pnpm shim resolves:
    a clean web rebase carries its approval and lands; a broken one is refused."""
    install_pnpm_layout(world.repo)
    world.base = git(world.repo, "rev-parse", BRANCH)
    approved, rebased = approve_then_rebase(world, {"web/src/app.ts": "export {};\n"})
    tip = git(world.repo, "rev-parse", BRANCH)
    assert landing.safety_net(world.repo, rebased, tip, []) is None
    queued = await submit(world, rebased)
    assert queued["carried_from"] == approved
    await world.queue.run_batch()
    assert git(world.repo, "rev-parse", BRANCH) == rebased
    assert {"key": "approved", "value": rebased} in world.tickets.ticket["labels"]

    broken = branch(world, "broken", {"web/src/broken.ts": "x"}, rebased)
    error = landing.safety_net(world.repo, broken, rebased, [])
    assert error is not None and "broken.ts" in error
    (world.repo / "node_modules").rename(world.repo / "node_modules.off")
    missing = landing.safety_net(world.repo, rebased, tip, [])
    assert missing == "node_modules is missing in the landing checkout"


async def test_the_verified_gate_is_off_by_default_and_needs_the_exact_sha(world) -> None:
    sha = feature(world, "f-verified")
    world.tickets.label("approved", sha, session_id="rs")
    assert (await world.queue.submit(world.project, "t1", sha, None, dry_run=True))["ok"]
    world.project.config = {"landing": {"enabled": True, "require_verified": True}}
    with pytest.raises(OmnigentError, match="isn't verified"):
        await world.queue.submit(world.project, "t1", sha, None, dry_run=True)
    with pytest.raises(OmnigentError, match="isn't verified"):
        await submit(world, sha)
    world.tickets.label("verified", world.base, session_id="worker")
    with pytest.raises(OmnigentError, match="verification doesn't carry"):
        await world.queue.submit(world.project, "t1", sha, None, dry_run=True)
    world.tickets.label("verified", sha, session_id="worker")
    dry = await world.queue.submit(world.project, "t1", sha, None, dry_run=True)
    assert dry["ok"] and "verified_carried_from" not in dry
    assert (await submit(world, sha))["queued"]


async def test_a_verification_carries_over_a_rebase_onto_untouched_files(world) -> None:
    world.project.config = {"landing": {"enabled": True, "require_verified": True}}
    verified, rebased = approve_then_rebase(world, {"a.txt": "a", "b.txt": "b"})
    world.tickets.label("verified", verified, session_id="worker")
    world.tickets.label("approved", rebased, session_id="rs")
    dry = await world.queue.submit(world.project, "t1", rebased, None, dry_run=True)
    assert dry["verified_carried_from"] == verified and dry["land_sha"] == rebased
    await submit(world, rebased)
    await world.queue.run_batch()
    assert git(world.repo, "rev-parse", BRANCH) == rebased
    assert {"key": "verified", "value": rebased} in world.tickets.ticket["labels"]
    assert {"key": "approved", "value": rebased} in world.tickets.ticket["labels"]
    assert any(
        "verification carried" in note and verified in note and rebased in note
        for note in world.tickets.notes
    )


async def test_a_verification_doesnt_carry_over_a_tip_commit_touching_its_files(world) -> None:
    world.project.config = {"landing": {"enabled": True, "require_verified": True}}
    lines = [f"line {number}" for number in range(30)]
    start = write_commit(world.repo, "c.txt", "\n".join(lines) + "\n")
    verified = branch(world, "feat", {"c.txt": "\n".join(["first", *lines[1:]]) + "\n"}, start)
    tip = write_commit(world.repo, "c.txt", "\n".join([*lines[:-1], "last"]) + "\n")
    git(world.repo, "checkout", "-q", "-B", "rebased", verified)
    git(world.repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "rebase", "-q", BRANCH)
    rebased = git(world.repo, "rev-parse", "HEAD")
    git(world.repo, "checkout", "-q", BRANCH)
    assert landing.patch_identity_problem(world.repo, verified, rebased, tip) is None
    problem = landing.verification_carry_problem(world.repo, verified, rebased, tip)
    assert problem is not None and tip[:7] in problem and "touch its files" in problem
    world.tickets.label("verified", verified, session_id="worker")
    world.tickets.label("approved", rebased, session_id="rs")
    with pytest.raises(OmnigentError, match="touch its files"):
        await world.queue.submit(world.project, "t1", rebased, None, dry_run=True)


async def test_a_product_approver_can_give_risk_ok_even_on_their_own_request(world) -> None:
    world.project.user_id = "owner@example.com"
    world.project.config = {
        "landing": {"enabled": True},
        "product_approvers": ["approver@example.com"],
    }
    sha = feature(world, "f-approver-risk")
    world.tickets.label("approved", sha, session_id="rs")
    world.tickets.label("risk", "auth", session_id="rs")
    world.tickets.label("kind", "fix", session_id="rs")
    world.tickets.ticket["requested_by"] = "approver@example.com"
    world.tickets.label("risk-ok", "yes", user_id="approver@example.com")
    assert (await world.queue.submit(world.project, "t1", sha, None, dry_run=True))["ok"]
    world.tickets.ticket["requested_by"] = "owner@example.com"
    assert (await world.queue.submit(world.project, "t1", sha, None, dry_run=True))["ok"]
    # A member who isn't an approver still can't stand in.
    world.tickets.unlabel("risk-ok", user_id="owner@example.com")
    world.tickets.label("risk-ok", "yes", user_id="member@example.com")
    with pytest.raises(OmnigentError, match="risk-ok"):
        await world.queue.submit(world.project, "t1", sha, None, dry_run=True)


def _board(*names: str) -> list[dict[str, str]]:
    return [{"id": name.casefold(), "name": name} for name in names]


@pytest.mark.parametrize(
    ("config", "stages", "expected"),
    [
        ({}, _board("Pending", "Review", "Implemented", "Verified", "Merged"), "implemented"),
        ({}, _board("Pending", "Review", "Verified", "Landed", "Merged"), "landed"),
        (
            {"landing": {"landed_stage": "verified"}},
            _board("Pending", "Review", "Implemented", "Verified", "Merged"),
            "verified",
        ),
        ({"landing": {"landed_stage": "merged"}}, _board("Pending", "Done", "Merged"), "done"),
        ({"landing": {"landed_stage": "missing"}}, _board("Open", "Review", "Done"), "review"),
        ({}, _board("Done"), None),
    ],
)
def test_landed_tickets_move_to_the_landed_stage_by_name_not_position(
    config, stages, expected
) -> None:
    assert landing.landed_stage_id(config, stages) == expected
