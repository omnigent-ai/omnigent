"""Land Reviewer-approved commits: fast-forward, rebuild, and restart once per batch.

A worker submits ``(ticket, sha)`` with ``sys_ticket_land``. The server lands it
only when the project turned landing on, the ticket's ``approved`` label is that
exact SHA and was set by the Reviewer's session or the owner, any ``risk`` label
has a ``risk-ok`` from the owner or a product approver who didn't request the
ticket, and the SHA fast-forwards the target branch.

Submissions that arrive close together form one batch: each is fast-forwarded in
turn, the web app is rebuilt once if any landed change touched ``web/``, the
tickets move to the landed stage (see ``landed_stage_id``), and one detached restart
runs. Any git, build or restart failure stops the queue for that project and
escalates to the lead. The queue never pushes, resets or forces.

An approval carries to another SHA only as the identical patch: the same commits by
``git patch-id --verbatim``, in order, with the same trailers, fast-forwarding the tip.
When an approved SHA no longer fast-forwards, the queue cherry-picks it onto the tip
in a scratch worktree. A carried approval lands only after ``tsc`` and ``oxlint``
(when ``web/`` changed) and the ticket's ``targeted-tests`` pass on the new SHA.

With ``require_verified`` on, the ``verified`` label must also be the SHA that lands. A
verification carries across a rebase only as the identical patch whose files no commit
it was rebased over touched; the queue then sets ``verified`` to the landed SHA.

Project config ``landing`` (owner-only): ``enabled``, plus optional ``checkout``
and ``branch`` for a throwaway target (a worktree of the same repository),
``restart: false`` to skip the restart, ``require_verified: true`` for the verified
gate (off by default) and ``landed_stage`` for the stage landed tickets move to.
The restart command is server-side only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_ticket import (
    CommentOnTicket,
    TicketActor,
    TicketLabel,
    UpdateTicket,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.github_pr_intake import OWNER_OK_LABEL, is_outside_requester
from omnigent.server.product_approval import (
    PRODUCT_OK_LABEL,
    needs_product_ok,
    product_approvers,
    product_gate_problem,
    risk_ok_givers,
)

_logger = logging.getLogger(__name__)

CONFIG_KEY = "landing"
APPROVED_LABEL = "approved"
VERIFIED_LABEL = "verified"
RISK_LABEL = "risk"
RISK_OK_LABEL = "risk-ok"
REVIEWER_IDENTITY = "reviewer"
TARGETED_TESTS_LABEL = "targeted-tests"
_TEST_TIMEOUT_S = 20 * 60
NOTE_PREFIX = "Landing queue"
LANDING_AGENT_ID = uuid.uuid5(uuid.NAMESPACE_URL, "omnigent:project-landing-queue").hex
_SHA = re.compile(r"[0-9a-f]{40}")
_BATCH_SECONDS = float(os.environ.get("OMNIGENT_LANDING_BATCH_SECONDS", "20"))
_BUILD_TIMEOUT_S = 20 * 60
_GIT_TIMEOUT_S = 120
_RESTART_WAIT_S = 10 * 60
_DEFAULT_BRANCH = os.environ.get("OMNIGENT_LANDING_BRANCH", "feat/project-ticket-board")


def _default_checkout() -> Path:
    configured = os.environ.get("OMNIGENT_LANDING_CHECKOUT")
    return Path(configured).expanduser() if configured else Path(__file__).resolve().parents[2]


def _restart_command() -> list[str] | None:
    configured = os.environ.get("OMNIGENT_LANDING_RESTART_COMMAND")
    path = Path(configured or "~/omnigent-ops/restart-pod.sh").expanduser()
    return [str(path)] if path.is_file() else None


def _state_dir() -> Path:
    base = Path(os.environ.get("OMNIGENT_DATA_DIR") or "~/.omnigent").expanduser()
    return base / "landing"


@dataclass
class Target:
    checkout: Path
    branch: str
    restart: bool


@dataclass
class Item:
    project: Any
    ticket: dict[str, Any]
    sha: str
    submitter: str | None
    queued_at: float = field(default_factory=time.time)


class GitError(RuntimeError):
    pass


def _git(checkout: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(checkout), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()[:500]}"
        )
    return result


def _common_dir(checkout: Path) -> Path:
    out = _git(checkout, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    return Path(out.strip()).resolve()


def refuse(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.CONFLICT)


# -- approval carry: identical patches across rebases ------------------------------------


def _is_ancestor(checkout: Path, ancestor: str, descendant: str) -> bool:
    return (
        _git(checkout, "merge-base", "--is-ancestor", ancestor, descendant, check=False).returncode
        == 0
    )


def _patch_id(checkout: Path, commit: str) -> str | None:
    diff = _git(checkout, "diff-tree", "--patch", "--no-color", "--full-index", commit).stdout
    result = subprocess.run(
        ["git", "-C", str(checkout), "patch-id", "--verbatim"],
        input=diff,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
    )
    words = result.stdout.split()
    return words[0] if result.returncode == 0 and words else None


def _branch_commits(checkout: Path, head: str, onto: str) -> list[str] | None:
    """*head*'s own commits since it forked from *onto*, oldest first; None with a merge."""
    base = _git(checkout, "merge-base", onto, head).stdout.strip()
    if _git(checkout, "rev-list", "--merges", f"{base}..{head}").stdout.strip():
        return None
    return _git(checkout, "rev-list", "--reverse", f"{base}..{head}").stdout.split()


def patch_identity_problem(checkout: Path, source: str, candidate: str, onto: str) -> str | None:
    """Why *candidate* isn't the approved *source* re-applied commit for commit, else None.

    Each branch's commits are taken from where it forked from *onto* (the integration
    tip). A carry needs the same count, the same ``git patch-id --verbatim`` in order
    (whitespace counts: indentation can change behavior), and the same trailers.
    """
    old = _branch_commits(checkout, source, onto)
    new = _branch_commits(checkout, candidate, onto)
    if old is None or new is None:
        return "a merge commit is in the range"
    if not old:
        return f"the approved `{source[:12]}` has no commits left to land"
    if len(old) != len(new):
        return f"{len(new)} commit(s) where {len(old)} were approved"
    for position, (before, after) in enumerate(zip(old, new, strict=True), start=1):
        before_id, after_id = _patch_id(checkout, before), _patch_id(checkout, after)
        if before_id is None or after_id is None or before_id != after_id:
            return f"commit {position} (`{after[:12]}`) changes a different patch"
        trailers = [
            _git(checkout, "log", "-1", "--format=%(trailers:unfold)", sha).stdout.strip()
            for sha in (before, after)
        ]
        if trailers[0] != trailers[1]:
            return f"commit {position} (`{after[:12]}`) has different trailers"
    return None


def _changed_paths(checkout: Path, base: str, head: str) -> list[str]:
    out = _git(checkout, "diff", "--name-only", "--no-renames", "-z", base, head).stdout
    return [path for path in out.split("\0") if path]


def verification_carry_problem(
    checkout: Path, verified: str, candidate: str, tip: str
) -> str | None:
    """Why the evidence for *verified* doesn't cover *candidate* landing on *tip*, else None.

    It covers it only as the identical patch, when no commit between the verified SHA's
    base and *tip* touched any file the patch changes.
    """
    problem = patch_identity_problem(checkout, verified, candidate, tip)
    if problem is not None:
        return problem
    base = _git(checkout, "merge-base", tip, verified).stdout.strip()
    paths = _changed_paths(checkout, base, verified)
    if base == tip or not paths:
        return None
    touching = _git(
        checkout,
        "log",
        "--format=%h",
        f"{base}..{tip}",
        "--",
        *(f":(literal){path}" for path in paths),
    ).stdout.split()
    if not touching:
        return None
    shown = ", ".join(f"`{sha}`" for sha in touching[:5])
    more = f" and {len(touching) - 5} more" if len(touching) > 5 else ""
    return f"tip commit(s) {shown}{more} since it was verified touch its files"


@contextlib.contextmanager
def _scratch_worktree(checkout: Path, commit: str) -> Any:
    """A detached throwaway worktree at *commit*, removed afterwards."""
    path = _state_dir() / "scratch" / uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(checkout, "worktree", "add", "--detach", str(path), commit)
    try:
        yield path
    finally:
        _git(checkout, "worktree", "remove", "--force", str(path), check=False)
        _git(checkout, "worktree", "prune", check=False)


def rebase_onto_tip(checkout: Path, sha: str, tip: str) -> str | None:
    """Cherry-pick *sha*'s own commits onto *tip* in a scratch worktree; None on conflict.

    Only objects are written; no branch moves. The approved head's committer is kept.
    """
    commits = _branch_commits(checkout, sha, tip)
    if not commits:
        return None
    committer = _git(checkout, "log", "-1", "--format=%cn%x00%ce", sha).stdout.strip()
    name, _, email = committer.partition("\x00")
    env = {**os.environ, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    with _scratch_worktree(checkout, tip) as scratch:
        result = subprocess.run(
            ["git", "-C", str(scratch), "cherry-pick", *commits],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            _git(scratch, "cherry-pick", "--abort", check=False)
            return None
        return _git(scratch, "rev-parse", "HEAD").stdout.strip()


_TEST_TARGET = re.compile(r"tests/[A-Za-z0-9_./-]+\.py(::[A-Za-z0-9_\[\]-]+)*")


def targeted_tests(ticket: dict[str, Any]) -> list[str] | None:
    """The ticket's recorded targeted tests (label ``targeted-tests``); None if malformed."""
    raw = next(
        (
            label.get("value") or ""
            for label in ticket.get("labels") or []
            if label.get("key") == TARGETED_TESTS_LABEL
        ),
        "",
    )
    tests = raw.replace(",", " ").split()
    if any(not _TEST_TARGET.fullmatch(test) or ".." in test for test in tests):
        return None
    return tests


def _isolated_env(home: Path) -> dict[str, str]:
    """The server's environment without ``OMNIGENT_*``, with a throwaway HOME and TMPDIR.

    Checks run branch code; it must not see the server's database, data dirs or runner ids.
    """
    env = {key: value for key, value in os.environ.items() if not key.startswith("OMNIGENT_")}
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    return {**env, "HOME": str(home), "TMPDIR": str(home / "tmp")}


def _run(command: list[str], cwd: Path, timeout: float, env: dict[str, str]) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"`{' '.join(command)}`: {str(exc)[:200]}"
    if result.returncode != 0:
        return f"`{' '.join(command)}`: {(result.stdout + result.stderr).strip()[-300:]}"
    return None


def _link_node_modules(checkout: Path, scratch: Path) -> str | None:
    """Give *scratch* the checkout's installed packages, or say which are missing.

    pnpm keeps the packages in the root ``node_modules/.pnpm``; ``web/node_modules``
    holds links and ``.bin`` shims that reach them through ``../../../node_modules``
    relative to the path they're run from, so both directories are needed.
    """
    for relative in ("node_modules", "web/node_modules"):
        modules = checkout / relative
        if not modules.is_dir():
            return f"{relative} is missing in the landing checkout"
        (scratch / relative).symlink_to(modules)
    return None


def safety_net(checkout: Path, sha: str, tip: str, tests: list[str]) -> str | None:
    """Checks for a carried approval at *sha*; the first failure, or None.

    ``tsc`` and ``oxlint`` run when ``web/`` changed since *tip*, then the targeted tests.
    """
    web_changed = bool(
        _git(checkout, "diff", "--name-only", tip, sha, "--", "web/").stdout.strip()
    )
    if not web_changed and not tests:
        return None
    with _scratch_worktree(checkout, sha) as scratch, tempfile.TemporaryDirectory() as home:
        env = _isolated_env(Path(home))
        if web_changed:
            error = _link_node_modules(checkout, scratch)
            if error:
                return error
            for script in ("type-check", "lint"):
                error = _run(
                    ["npm", "run", "--silent", script], scratch / "web", _BUILD_TIMEOUT_S, env
                )
                if error:
                    return error
        if tests:
            return _run(
                [sys.executable, "-m", "pytest", "-q", "-x", *tests],
                scratch,
                _TEST_TIMEOUT_S,
                env,
            )
    return None


@dataclass
class Plan:
    """What landing a submitted SHA means now."""

    tip: str
    land_sha: str
    carried_from: str | None = None
    rebased: bool = False
    verified_from: str | None = None


def resolve_target(config: dict[str, Any]) -> Target:
    """The project's landing target; overrides must stay inside this repository."""
    raw = config.get(CONFIG_KEY)
    raw = raw if isinstance(raw, dict) else {}
    default = _default_checkout()
    checkout = Path(str(raw.get("checkout") or default)).expanduser()
    branch = str(raw.get("branch") or _DEFAULT_BRANCH)
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", branch) or branch.startswith("-"):
        raise refuse("The landing branch name is invalid")
    try:
        same_repo = _common_dir(checkout) == _common_dir(default)
    except (GitError, OSError, subprocess.SubprocessError):
        same_repo = False
    if not same_repo:
        raise refuse("The landing checkout must be a worktree of the server's repository")
    return Target(checkout=checkout, branch=branch, restart=raw.get("restart", True) is not False)


def landing_enabled(config: dict[str, Any]) -> bool:
    raw = config.get(CONFIG_KEY)
    return isinstance(raw, dict) and raw.get("enabled") is True


_LANDED_STAGE_NAMES = ("implemented", "landed")


def landed_stage_id(config: dict[str, Any], stages: list[dict[str, Any]]) -> str | None:
    """The stage a landed ticket moves to, chosen by name rather than position.

    In order: the config's ``landed_stage``, the ``implemented`` stage id, a stage named
    Implemented or Landed, then the stage before the completed one. Never the completed one.
    """
    if len(stages) < 2:
        return None
    candidates = stages[:-1]
    raw = config.get(CONFIG_KEY)
    configured = (raw if isinstance(raw, dict) else {}).get("landed_stage")
    ids = [stage["id"] for stage in candidates]
    if isinstance(configured, str) and configured in ids:
        return configured
    if "implemented" in ids:
        return "implemented"
    for stage in candidates:
        if str(stage.get("name") or "").strip().casefold() in _LANDED_STAGE_NAMES:
            return stage["id"]
    return stages[-2]["id"]


def verification_required(config: dict[str, Any]) -> bool:
    """Whether the owner turned on the ``verified`` landing gate; off by default."""
    raw = config.get(CONFIG_KEY)
    return isinstance(raw, dict) and raw.get("require_verified") is True


class LandingQueue:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.pending: list[Item] = []
        self.stopped: dict[str, str] = {}
        self._lock = asyncio.Lock()
        self._batch: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- plumbing ------------------------------------------------------------

    @property
    def _notifications(self) -> Any:
        return getattr(self.app.state, "project_ticket_notifications", None)

    def _actor(self, project: Any) -> TicketActor:
        return TicketActor(user_id=project.user_id, agent_id=LANDING_AGENT_ID)

    async def _note(self, project: Any, ticket_id: str, text: str) -> None:
        try:
            await asyncio.to_thread(
                self._notifications.tickets.comment,
                project.id,
                ticket_id,
                CommentOnTicket(text=f"{NOTE_PREFIX}: {text}"[:2000], audience="agents"),
                self._actor(project),
            )
        except Exception:
            _logger.exception("Landing: could not note ticket %s", ticket_id)

    async def _send(self, project: Any, session_id: str | None, text: str) -> None:
        if not session_id:
            return
        from starlette.datastructures import Headers

        from omnigent.server.schemas import SessionEventInput

        inputs = self._notifications.inputs
        try:
            await inputs.enqueue(
                session_id,
                SessionEventInput(
                    type="message",
                    data={"role": "user", "content": [{"type": "input_text", "text": text}]},
                ),
                user_id=project.user_id,
                headers=Headers(),
                first=False,
            )
            await inputs.deliver_pending(self.app, session_id)
        except Exception:
            _logger.exception("Landing: could not message session %s", session_id)

    async def _stop(self, project: Any, tickets: list[dict[str, Any]], reason: str) -> None:
        """Stop the queue for *project* and tell its router; the owner re-enables it."""
        from omnigent.server.project_router import router_session_id

        self.stopped[project.id] = reason
        self._save_stopped()
        for ticket in tickets:
            await self._note(project, ticket["id"], f"stopped: {reason}")
        await self._send(
            project,
            await router_session_id(self.app, project),
            f"[{NOTE_PREFIX}] Stopped for this project: {reason}. Nothing else lands until the "
            "owner fixes it and turns landing off and on again in the project's `landing` config.",
        )

    def _save_stopped(self) -> None:
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "stopped.json").write_text(json.dumps(self.stopped))

    def _save_pending(self) -> None:
        """Keep queued lands on disk so a restart inside the batch window can't drop them."""
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "project_id": item.project.id,
                "user_id": item.project.user_id,
                "ticket_id": item.ticket["id"],
                "sha": item.sha,
                "submitter": item.submitter,
            }
            for item in self.pending
        ]
        (directory / "pending.json").write_text(json.dumps(entries))

    async def _resume_pending(self, delay: float = 5.0) -> None:
        await asyncio.sleep(delay)
        try:
            entries = json.loads((_state_dir() / "pending.json").read_text())
        except (OSError, ValueError):
            return
        projects = self._notifications.projects
        for entry in entries if isinstance(entries, list) else []:
            project = await asyncio.to_thread(
                projects.get, entry["project_id"], user_id=entry.get("user_id")
            )
            if project is not None:
                self.pending.append(
                    Item(project, {"id": entry["ticket_id"]}, entry["sha"], entry.get("submitter"))
                )
        if self.pending:
            await self.run_batch()

    def _load_stopped(self) -> None:
        try:
            self.stopped = json.loads((_state_dir() / "stopped.json").read_text())
        except (OSError, ValueError):
            self.stopped = {}

    # -- gates -----------------------------------------------------------------

    async def _label_events(
        self, project: Any, ticket_id: str
    ) -> list[tuple[dict[str, Any], set[tuple[str, str]]]]:
        """Every label edit on the ticket, oldest first, with the full label state it left."""
        tickets = self._notifications.tickets
        edits: list[tuple[dict[str, Any], set[tuple[str, str]]]] = []
        after = -1
        for _ in range(50):
            page = await asyncio.to_thread(
                tickets.events,
                project.id,
                ticket_id,
                user_id=project.user_id,
                after=after,
                limit=100,
            )
            for event in page.get("data", []):
                payload = event.get("payload") or {}
                labels = (payload.get("changes") or {}).get("labels") or (
                    payload.get("labels") if event.get("kind") == "created" else None
                )
                if not isinstance(labels, list):
                    continue
                state = {
                    (label["key"], (label.get("value") or "").strip())
                    for label in labels
                    if isinstance(label, dict) and isinstance(label.get("key"), str)
                }
                edits.append((event, state))
            if not page.get("has_more"):
                break
            after = page.get("last_position", after)
        return edits

    @staticmethod
    def _label_setters(
        edits: list[tuple[dict[str, Any], set[tuple[str, str]]]],
    ) -> dict[tuple[str, str], list[dict]]:
        """Each current ``(key, value)`` label, with every event that set or kept it since it
        last appeared. Edits replace the whole label list, so a later edit that merely keeps a
        label is listed too. ``approved`` is trusted if any of those events is trusted, since
        the Reviewer may re-assert it; owner sign-offs only by the event that introduced them.
        """
        setters: dict[tuple[str, str], list[dict]] = {}
        for event, state in edits:
            setters = {pair: [*setters.get(pair, []), event] for pair in state}
        return setters

    async def _approval_source(
        self, project: Any, edits: list[tuple[dict[str, Any], set[tuple[str, str]]]]
    ) -> str | None:
        """The SHA the Reviewer or owner last approved and didn't withdraw.

        A worker's new head clears the label without withdrawing it, so an identical patch
        can carry it. The Reviewer or owner withdraws it by removing ``approved``, and the
        Reviewer also by any label edit without it. The queue's own carries are skipped.
        """
        source: str | None = None
        previous: str | None = None
        for event, state in edits:
            value = next((v for key, v in state if key == APPROVED_LABEL and v), None)
            if event.get("agent_id") == LANDING_AGENT_ID:
                previous = value
                continue
            trusted, by_reviewer = await self._trust(project, event)
            if trusted and value is not None:
                source = value
            elif trusted and value is None and (previous is not None or by_reviewer):
                source = None
            previous = value
        return source

    @staticmethod
    def _is_owner(project: Any, event: dict[str, Any] | None) -> bool:
        """A person's own edit as the project owner; shared members don't count."""
        return (
            event is not None
            and event.get("session_id") is None
            and event.get("agent_id") is None
            and event.get("user_id") == project.user_id
        )

    @staticmethod
    def _may_give_risk_ok(
        project: Any, event: dict[str, Any] | None, requested_by: str | None
    ) -> bool:
        """A person's own edit, by the owner or a product approver who didn't request it."""
        return (
            event is not None
            and event.get("session_id") is None
            and event.get("agent_id") is None
            and event.get("user_id") in risk_ok_givers(project, requested_by)
        )

    @staticmethod
    def _is_product_approver(project: Any, event: dict[str, Any] | None) -> bool:
        """A person's own edit, by someone on the project's product-approver list."""
        return (
            event is not None
            and event.get("session_id") is None
            and event.get("agent_id") is None
            and event.get("user_id") in product_approvers(project)
        )

    async def _trust(self, project: Any, event: dict[str, Any]) -> tuple[bool, bool]:
        """Whether *event* is the owner's or the Reviewer's, and whether it is the Reviewer's."""
        if self._is_owner(project, event):
            return True, False
        session_id = event.get("session_id")
        if not session_id:
            return False, False
        identities = await asyncio.to_thread(
            self._notifications.projects.list_identities, project.id, user_id=project.user_id
        )
        reviewer = {i.id for i in identities if i.name.strip().casefold() == REVIEWER_IDENTITY}
        conv = await asyncio.to_thread(
            self._notifications.inputs.conversations.get_conversation, session_id
        )
        is_reviewer = conv is not None and conv.labels.get(PROJECT_IDENTITY_LABEL_KEY) in reviewer
        return is_reviewer, is_reviewer

    async def _check(self, project: Any, ticket: dict[str, Any], sha: str, target: Target) -> Plan:
        """Raise a refusal unless *sha* may land now; return how it lands.

        The approval is the Reviewer's (or owner's) last approved SHA. Another SHA, or the
        approved one once the tip moved on, lands only as an identical patch on the tip,
        cherry-picked by the queue if needed. Outside pull requests never carry.
        """
        labels = {
            label.get("key"): (label.get("value") or "").strip()
            for label in ticket.get("labels") or []
        }
        edits = await self._label_events(project, ticket["id"])
        setters = self._label_setters(edits)
        source = await self._approval_source(project, edits)
        if source is None:
            if labels.get(APPROVED_LABEL) == sha:
                raise refuse(
                    "The `approved` label wasn't set by the Reviewer's session or the owner"
                )
            raise refuse(
                f"`{sha[:12]}` isn't approved: the ticket's `approved` label must be exactly "
                "this SHA, set by the Reviewer"
            )
        if RISK_LABEL in labels:
            risk_ok = setters.get((RISK_OK_LABEL, labels.get(RISK_OK_LABEL, "")), [])
            if RISK_OK_LABEL not in labels or not self._may_give_risk_ok(
                project, risk_ok[0] if risk_ok else None, ticket.get("requested_by")
            ):
                raise refuse(
                    f"The ticket has `risk: {labels[RISK_LABEL]}`; the owner or a product "
                    "approver who didn't request it must add `risk-ok` first"
                )
        problem = product_gate_problem(project, ticket)
        if problem is not None:
            raise refuse(problem)
        if needs_product_ok(project, ticket):
            product_ok = setters.get((PRODUCT_OK_LABEL, labels.get(PRODUCT_OK_LABEL, "")), [])
            if PRODUCT_OK_LABEL not in labels or not self._is_product_approver(
                project, product_ok[0] if product_ok else None
            ):
                raise refuse(
                    f"This is a feature requested by `{ticket.get('requested_by')}`, not the "
                    f"owner: a product approver must add `{PRODUCT_OK_LABEL}` first"
                )
        if is_outside_requester(ticket.get("requested_by")):
            owner_ok = setters.get((OWNER_OK_LABEL, sha), [])
            if labels.get(OWNER_OK_LABEL) != sha or not self._is_owner(
                project, owner_ok[0] if owner_ok else None
            ):
                raise refuse(
                    f"This change came from an outside pull request "
                    f"(`{ticket['requested_by']}`): the owner must set `{OWNER_OK_LABEL}` = "
                    f"`{sha}` before it lands"
                )
        outside = is_outside_requester(ticket.get("requested_by"))
        try:
            plan = await asyncio.to_thread(self._plan, target, sha, source, not outside)
            if verification_required(project.config):
                await asyncio.to_thread(
                    self._check_verified, target, plan, labels.get(VERIFIED_LABEL, "").lower()
                )
        except (GitError, subprocess.SubprocessError) as exc:
            raise refuse(f"Couldn't compare `{sha[:12]}` with the approved commit: {exc}") from exc
        return plan

    @staticmethod
    def _check_verified(target: Target, plan: Plan, verified: str) -> None:
        """Refuse unless ``verified`` is the landing SHA or carries to it; record a carry."""
        land = plan.land_sha
        if verified == land:
            return
        if not verified:
            raise refuse(
                f"`{land[:12]}` isn't verified: verify it on an isolated server, attach the "
                f"evidence, then set the ticket's `{VERIFIED_LABEL}` label to exactly `{land}`"
            )
        if (
            not _SHA.fullmatch(verified)
            or _git(
                target.checkout, "cat-file", "-e", f"{verified}^{{commit}}", check=False
            ).returncode
        ):
            raise refuse(
                f"The `{VERIFIED_LABEL}` label `{verified[:40]}` isn't a full commit SHA in "
                "this repository"
            )
        problem = verification_carry_problem(target.checkout, verified, land, plan.tip)
        if problem is not None:
            raise refuse(
                f"`{land[:12]}` isn't the verified `{verified[:12]}` unchanged on the tip "
                f"({problem}), so the verification doesn't carry: verify `{land}` on an "
                f"isolated server and set `{VERIFIED_LABEL}` to it"
            )
        plan.verified_from = verified

    @staticmethod
    def _plan(target: Target, sha: str, source: str, may_carry: bool) -> Plan:
        checkout = target.checkout
        if _git(checkout, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
            raise refuse(f"`{sha[:12]}` isn't a commit in this repository")
        tip = _git(checkout, "rev-parse", f"refs/heads/{target.branch}").stdout.strip()
        if tip == sha:
            raise refuse(f"`{sha[:12]}` is already the tip of `{target.branch}`")
        fast_forward = _is_ancestor(checkout, tip, sha)
        if fast_forward and sha == source:
            return Plan(tip=tip, land_sha=sha)
        if not may_carry:
            if sha != source:
                raise refuse(
                    f"`{sha[:12]}` isn't approved: the ticket's `approved` label must be "
                    "exactly this SHA, set by the Reviewer"
                )
            raise refuse(
                f"`{sha[:12]}` doesn't fast-forward `{target.branch}` (tip `{tip[:12]}`): "
                "rebase onto the tip, then get the rebased SHA re-approved"
            )
        if _git(checkout, "cat-file", "-e", f"{source}^{{commit}}", check=False).returncode:
            raise refuse(f"The approved `{source[:12]}` isn't a commit in this repository")
        candidate = sha if fast_forward else rebase_onto_tip(checkout, sha, tip)
        if candidate is None:
            raise refuse(
                f"`{sha[:12]}` doesn't fast-forward `{target.branch}` (tip `{tip[:12]}`) and "
                "doesn't cherry-pick cleanly onto it: rebase onto the tip, resolve the "
                "conflict, then get the rebased SHA re-approved"
            )
        problem = patch_identity_problem(checkout, source, candidate, tip)
        if problem is not None:
            raise refuse(
                f"`{sha[:12]}` isn't the approved `{source[:12]}` re-applied ({problem}), so "
                "the approval doesn't carry: get the Reviewer to re-approve it"
            )
        return Plan(tip=tip, land_sha=candidate, carried_from=source, rebased=not fast_forward)

    # -- submit ----------------------------------------------------------------

    async def submit(
        self, project: Any, ticket_id: str, sha: str, who: TicketActor, *, dry_run: bool = False
    ) -> dict:
        sha = sha.strip().lower()
        if not _SHA.fullmatch(sha):
            raise OmnigentError(
                "Give the full 40-character commit SHA", code=ErrorCode.INVALID_INPUT
            )
        if dry_run:
            return await self.check_only(project, ticket_id, sha)
        if not landing_enabled(project.config):
            raise refuse(
                "The landing queue is off for this project; hand the branch and SHA to your "
                "coordinator instead"
            )
        if project.id in self.stopped:
            raise refuse(f"The landing queue is stopped: {self.stopped[project.id]}")
        tickets = self._notifications.tickets
        ticket = await asyncio.to_thread(
            tickets.get, project.id, ticket_id, user_id=project.user_id
        )
        ticket = ticket.get("ticket", ticket)
        stages = (await asyncio.to_thread(tickets.stages, project.id, user_id=project.user_id))[
            "stages"
        ]
        if ticket.get("stage_id") == stages[-1]["id"]:
            raise refuse("The ticket is already complete")
        target = await asyncio.to_thread(resolve_target, project.config)
        try:
            plan = await self._check(project, ticket, sha, target)
        except OmnigentError as exc:
            await self._note(project, ticket["id"], f"refused `{sha[:12]}`: {exc}.")
            raise
        tip = plan.tip
        if any(item.ticket["id"] == ticket["id"] for item in self.pending):
            raise refuse("This ticket already has a landing queued")
        self.pending.append(Item(project, ticket, sha, who.session_id))
        self._save_pending()
        await self._note(
            project,
            ticket["id"],
            f"queued `{sha[:12]}` to fast-forward `{target.branch}` from `{tip[:12]}`"
            f"{_describe(plan)}.",
        )
        if self._batch is None or self._batch.done():
            self._batch = self._spawn(self._run_batch_later())
        return {"queued": True, "sha": sha, "branch": target.branch, "tip": tip, **_carry(plan)}

    async def check_only(self, project: Any, ticket_id: str, sha: str) -> dict:
        """Run every landing gate for a hand-land, without queueing; refusals raise 409."""
        tickets = self._notifications.tickets
        ticket = await asyncio.to_thread(
            tickets.get, project.id, ticket_id, user_id=project.user_id
        )
        ticket = ticket.get("ticket", ticket)
        target = await asyncio.to_thread(resolve_target, project.config)
        plan = await self._check(project, ticket, sha, target)
        return {
            "ok": True,
            "dry_run": True,
            "sha": sha,
            "branch": target.branch,
            "tip": plan.tip,
            **_carry(plan),
        }

    def _spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def _run_batch_later(self) -> None:
        await asyncio.sleep(_BATCH_SECONDS)
        try:
            await self.run_batch()
        except Exception:
            _logger.exception("Landing batch failed")

    # -- the batch -------------------------------------------------------------

    async def run_batch(self) -> None:
        async with self._lock:
            items, self.pending = self.pending, []
            self._save_pending()
            if not items:
                return
            landed: list[Item] = []
            restart = False
            by_project: dict[str, list[Item]] = {}
            for item in items:
                by_project.setdefault(item.project.id, []).append(item)
            for project_items in by_project.values():
                done, wants_restart = await self._land_project(project_items)
                landed.extend(done)
                restart = restart or (bool(done) and wants_restart)
            if landed and restart:
                await self._restart(landed)

    async def _land_project(self, items: list[Item]) -> tuple[list[Item], bool]:
        project = items[0].project
        tickets = [item.ticket for item in items]
        try:
            target = await asyncio.to_thread(resolve_target, project.config)
            await asyncio.to_thread(self._require_clean, target)
            start = _git(target.checkout, "rev-parse", "HEAD").stdout.strip()
        except (GitError, OmnigentError, subprocess.SubprocessError) as exc:
            await self._stop(project, tickets, str(exc))
            return [], False
        landed: list[Item] = []
        for item in items:
            fresh = await asyncio.to_thread(
                self._notifications.tickets.get,
                project.id,
                item.ticket["id"],
                user_id=project.user_id,
            )
            item.ticket = fresh.get("ticket", fresh)
            try:
                plan = await self._check(project, item.ticket, item.sha, target)
                if plan.carried_from is not None:
                    await self._carry(project, item, plan, target)
                if plan.verified_from is not None:
                    await self._carry_verified(project, item, plan)
            except OmnigentError as exc:
                await self._note(project, item.ticket["id"], f"refused `{item.sha[:12]}`: {exc}.")
                await self._send(
                    project, item.submitter, f"[{NOTE_PREFIX}] Ticket {item.ticket['id']}: {exc}."
                )
                continue
            item.sha = plan.land_sha
            try:
                await asyncio.to_thread(_git, target.checkout, "merge", "--ff-only", item.sha)
            except (GitError, subprocess.SubprocessError) as exc:
                await self._stop(project, [item.ticket], str(exc))
                return landed, False
            landed.append(item)
            await self._note(
                project,
                item.ticket["id"],
                f"fast-forwarded `{target.branch}` to `{item.sha[:12]}`.",
            )
        if not landed:
            return [], False
        end = _git(target.checkout, "rev-parse", "HEAD").stdout.strip()
        changed = _git(target.checkout, "diff", "--name-only", start, end, "--", "web/").stdout
        if changed.strip():
            error = await asyncio.to_thread(self._build, target)
            if error:
                await self._stop(
                    project, [item.ticket for item in landed], f"web build failed: {error}"
                )
                return [], False
            for item in landed:
                await self._note(project, item.ticket["id"], "rebuilt the web app.")
        await self._finish(project, landed, target)
        return landed, target.restart

    async def _carry(self, project: Any, item: Item, plan: Plan, target: Target) -> None:
        """Run the safety net, then record the carried approval on the SHA that lands."""
        assert plan.carried_from is not None
        tests = targeted_tests(item.ticket)
        if tests is None:
            raise refuse(
                f"the `{TARGETED_TESTS_LABEL}` label must list test paths under `tests/`, so the "
                "carried approval can't be checked: get the Reviewer to re-approve"
            )
        try:
            failure = await asyncio.to_thread(
                safety_net, target.checkout, plan.land_sha, plan.tip, tests
            )
        except (GitError, OSError, subprocess.SubprocessError) as exc:
            failure = str(exc)[:300]
        if failure:
            raise refuse(
                f"the identical patch `{plan.land_sha[:12]}` fails on the new tip ({failure}); "
                "fix it, then get the Reviewer to re-approve"
            )
        labels = [
            TicketLabel(key=label["key"], value=label.get("value"))
            for label in item.ticket.get("labels") or []
            if label.get("key") != APPROVED_LABEL
        ]
        labels.append(TicketLabel(key=APPROVED_LABEL, value=plan.land_sha))
        checks = [
            *(["`tsc` and `oxlint`"] if _touches_web(target, plan) else []),
            *([f"{len(tests)} targeted test(s)"] if tests else []),
        ]
        comment = (
            f"{NOTE_PREFIX}: approval carried from `{plan.carried_from}` to `{plan.land_sha}`: "
            "identical patch"
            f"{', cherry-picked onto the tip by the queue' if plan.rebased else ''}. "
            + (f"Passed {' and '.join(checks)}." if checks else "No checks apply.")
        )
        await asyncio.to_thread(
            self._notifications.tickets.update,
            project.id,
            item.ticket["id"],
            UpdateTicket(labels=labels, comment=comment, audience="agents"),
            self._actor(project),
        )

    async def _carry_verified(self, project: Any, item: Item, plan: Plan) -> None:
        """Record the carried verification on the SHA that lands."""
        assert plan.verified_from is not None
        fresh = await asyncio.to_thread(
            self._notifications.tickets.get, project.id, item.ticket["id"], user_id=project.user_id
        )
        item.ticket = fresh.get("ticket", fresh)
        labels = [
            TicketLabel(key=label["key"], value=label.get("value"))
            for label in item.ticket.get("labels") or []
            if label.get("key") != VERIFIED_LABEL
        ]
        labels.append(TicketLabel(key=VERIFIED_LABEL, value=plan.land_sha))
        comment = (
            f"{NOTE_PREFIX}: verification carried from `{plan.verified_from}` to "
            f"`{plan.land_sha}`: identical patch, and no commit it was rebased over touches "
            "its files."
        )
        await asyncio.to_thread(
            self._notifications.tickets.update,
            project.id,
            item.ticket["id"],
            UpdateTicket(labels=labels, comment=comment, audience="agents"),
            self._actor(project),
        )

    @staticmethod
    def _require_clean(target: Target) -> None:
        """The checkout must be on the target branch."""
        branch = _git(target.checkout, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if branch != target.branch:
            raise GitError(f"the checkout is on `{branch}`, not `{target.branch}`")
        # Local edits are allowed: `merge --ff-only` refuses to overwrite any of them.

    @staticmethod
    def _build(target: Target) -> str | None:
        try:
            result = subprocess.run(
                ["npm", "run", "build"],
                cwd=target.checkout / "web",
                capture_output=True,
                text=True,
                timeout=_BUILD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return str(exc)[:300]
        if result.returncode != 0:
            return (result.stderr or result.stdout).strip()[-300:]
        return None

    async def _finish(self, project: Any, landed: list[Item], target: Target) -> None:
        """Move landed tickets on and tell their submitters, before any restart."""
        tickets = self._notifications.tickets
        stages = (await asyncio.to_thread(tickets.stages, project.id, user_id=project.user_id))[
            "stages"
        ]
        landed_stage = landed_stage_id(project.config, stages)
        restarting = (
            " The server restarts now; once it's back, verify live." if target.restart else ""
        )
        for item in landed:
            if landed_stage and item.ticket.get("stage_id") != landed_stage:
                try:
                    await asyncio.to_thread(
                        tickets.update,
                        project.id,
                        item.ticket["id"],
                        UpdateTicket(
                            stage_id=landed_stage,
                            comment=(
                                f"{NOTE_PREFIX}: landed `{item.sha[:12]}` on `{target.branch}`."
                            ),
                            audience="agents",
                        ),
                        self._actor(project),
                    )
                except Exception:
                    _logger.exception("Landing: could not move ticket %s", item.ticket["id"])
            await self._send(
                project,
                item.submitter,
                f"[{NOTE_PREFIX}] Ticket {item.ticket['id']}: `{item.sha}` landed on "
                f"`{target.branch}`.{restarting}",
            )

    async def _restart(self, landed: list[Item]) -> None:
        """Start one restart that outlives this server, and record what it covers."""
        command = _restart_command()
        if command is None:
            for project_id in {item.project.id for item in landed}:
                project = next(item.project for item in landed if item.project.id == project_id)
                await self._stop(
                    project,
                    [item.ticket for item in landed if item.project.id == project_id],
                    "no restart command is configured on this server",
                )
            return
        directory = _state_dir()
        directory.mkdir(parents=True, exist_ok=True)
        batch = {
            "started_at": time.time(),
            "tickets": [
                {
                    "project_id": item.project.id,
                    "user_id": item.project.user_id,
                    "ticket_id": item.ticket["id"],
                }
                for item in landed
            ],
        }
        (directory / "batch.json").write_text(json.dumps(batch))
        (directory / "restart.rc").unlink(missing_ok=True)
        for item in landed:
            await self._note(
                item.project,
                item.ticket["id"],
                "restarting the server (database backed up first).",
            )
        log = directory / "restart.log"
        script = f'"$@" > "{log}" 2>&1; echo $? > "{directory / "restart.rc"}"'
        # A new session keeps the restart alive when it stops this server.
        subprocess.Popen(
            ["/bin/sh", "-c", script, "landing-restart", *command],
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # -- after a restart -------------------------------------------------------

    def start(self) -> None:
        self._load_stopped()
        if (_state_dir() / "pending.json").exists():
            self._spawn(self._resume_pending())
        if (_state_dir() / "batch.json").exists():
            self._spawn(self._report_restart())

    async def _report_restart(self) -> None:
        directory = _state_dir()
        try:
            batch = json.loads((directory / "batch.json").read_text())
        except (OSError, ValueError):
            return
        deadline = time.time() + _RESTART_WAIT_S
        rc_path = directory / "restart.rc"
        while not rc_path.exists() and time.time() < deadline:
            await asyncio.sleep(2)
        rc = rc_path.read_text().strip() if rc_path.exists() else None
        (directory / "batch.json").unlink(missing_ok=True)
        projects = self._notifications.projects
        for entry in batch.get("tickets", []):
            project = await asyncio.to_thread(
                projects.get, entry["project_id"], user_id=entry["user_id"]
            )
            if project is None:
                continue
            if rc == "0":
                await self._note(
                    project, entry["ticket_id"], "the server restarted and is back up."
                )
            else:
                tail = ""
                with contextlib.suppress(OSError):
                    tail = (directory / "restart.log").read_text()[-300:]
                await self._stop(
                    project,
                    [{"id": entry["ticket_id"]}],
                    f"the restart exited with {rc or 'no result'}: {tail.strip()}",
                )


def _touches_web(target: Target, plan: Plan) -> bool:
    diff = _git(target.checkout, "diff", "--name-only", plan.tip, plan.land_sha, "--", "web/")
    return bool(diff.stdout.strip())


def _describe(plan: Plan) -> str:
    verified = (
        f", carrying the verification of `{plan.verified_from[:12]}`"
        if plan.verified_from is not None
        else ""
    )
    if plan.carried_from is None:
        return verified
    if plan.rebased:
        return (
            f", carrying the approval of `{plan.carried_from[:12]}` as `{plan.land_sha[:12]}` "
            f"(cherry-picked onto the tip, identical patch){verified}"
        )
    return f", carrying the approval of `{plan.carried_from[:12]}` (identical patch){verified}"


def _carry(plan: Plan) -> dict[str, Any]:
    carried: dict[str, Any] = {}
    if plan.verified_from is not None:
        carried.update(verified_carried_from=plan.verified_from, land_sha=plan.land_sha)
    if plan.carried_from is None:
        return carried
    return {
        **carried,
        "carried_from": plan.carried_from,
        "land_sha": plan.land_sha,
        "rebased": plan.rebased,
    }


def start(app: Any) -> LandingQueue:
    queue = LandingQueue(app)
    app.state.project_landing = queue
    queue.start()
    return queue


def could_supersede(config: dict[str, Any], approved: str, newer: str) -> bool:
    """Whether *newer* can be a new head replacing the approved commit, as far as git can tell.

    No when the approved commit is already on the landing branch, or *newer* is on it (such as
    its tip or a base) or is an ancestor of the approved commit. Yes when git can't tell.
    """
    try:
        target = resolve_target(config)
        branch = f"refs/heads/{target.branch}"

        def known(sha: str) -> bool:
            return not _git(
                target.checkout, "cat-file", "-e", f"{sha}^{{commit}}", check=False
            ).returncode

        if known(approved) and _is_ancestor(target.checkout, approved, branch):
            return False
        if known(newer):
            if _is_ancestor(target.checkout, newer, branch):
                return False
            if known(approved) and _is_ancestor(target.checkout, newer, approved):
                return False
        return True
    except (GitError, OmnigentError, OSError, subprocess.SubprocessError):
        return True


def is_pure_rebase(config: dict[str, Any], approved: str, newer: str) -> bool:
    """Whether *newer* is the approved commit re-applied onto the landing branch unchanged."""
    try:
        target = resolve_target(config)
        tip = _git(target.checkout, "rev-parse", f"refs/heads/{target.branch}").stdout.strip()
        for sha in (approved, newer):
            if _git(
                target.checkout, "cat-file", "-e", f"{sha}^{{commit}}", check=False
            ).returncode:
                return False
        return patch_identity_problem(target.checkout, approved, newer, tip) is None
    except (GitError, OmnigentError, OSError, subprocess.SubprocessError):
        return False
