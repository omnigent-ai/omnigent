"""Land Reviewer-approved commits: fast-forward, rebuild, and restart once per batch.

A worker submits ``(ticket, sha)`` with ``sys_ticket_land``. The server lands it
only when the project turned landing on, the ticket's ``approved`` label is that
exact SHA and was set by the Reviewer's session or the owner, any ``risk`` label
has an owner-set ``risk-ok``, and the SHA fast-forwards the target branch.

Submissions that arrive close together form one batch: each is fast-forwarded in
turn, the web app is rebuilt once if any landed change touched ``web/``, the
tickets move to the stage before the completed one, and one detached restart
runs. Any git, build or restart failure stops the queue for that project and
escalates to the lead. The queue never pushes, resets, rebases or forces.

Project config ``landing`` (owner-only): ``enabled``, plus optional ``checkout``
and ``branch`` for a throwaway target (a worktree of the same repository) and
``restart: false`` to skip the restart. The restart command is server-side only.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.entities.project import PROJECT_IDENTITY_LABEL_KEY
from omnigent.entities.project_ticket import CommentOnTicket, TicketActor, UpdateTicket
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.github_pr_intake import OWNER_OK_LABEL, is_outside_requester

_logger = logging.getLogger(__name__)

CONFIG_KEY = "landing"
APPROVED_LABEL = "approved"
RISK_LABEL = "risk"
RISK_OK_LABEL = "risk-ok"
REVIEWER_IDENTITY = "reviewer"
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
        """Stop the queue for *project* and tell the lead; the owner re-enables it."""
        self.stopped[project.id] = reason
        self._save_stopped()
        for ticket in tickets:
            await self._note(project, ticket["id"], f"stopped: {reason}")
        await self._send(
            project,
            project.main_session_id,
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

    async def _label_setters(
        self, project: Any, ticket_id: str
    ) -> dict[tuple[str, str], list[dict]]:
        """Each current ``(key, value)`` label, with every event that set or kept it since it
        last appeared. Edits replace the whole label list, so a later edit that merely keeps a
        label is listed too. ``approved`` is trusted if any of those events is trusted, since
        the Reviewer may re-assert it; owner sign-offs only by the event that introduced them.
        """
        tickets = self._notifications.tickets
        setters: dict[tuple[str, str], list[dict]] = {}
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
                setters = {pair: [*setters.get(pair, []), event] for pair in state}
            if not page.get("has_more"):
                break
            after = page.get("last_position", after)
        return setters

    @staticmethod
    def _is_owner(project: Any, event: dict[str, Any] | None) -> bool:
        """A person's own edit as the project owner; shared members don't count."""
        return (
            event is not None
            and event.get("session_id") is None
            and event.get("agent_id") is None
            and event.get("user_id") == project.user_id
        )

    async def _is_reviewer_or_owner(self, project: Any, event: dict[str, Any]) -> bool:
        if self._is_owner(project, event):
            return True
        session_id = event.get("session_id")
        if not session_id:
            return False
        identities = await asyncio.to_thread(
            self._notifications.projects.list_identities, project.id, user_id=project.user_id
        )
        reviewer = {i.id for i in identities if i.name.strip().casefold() == REVIEWER_IDENTITY}
        conv = await asyncio.to_thread(
            self._notifications.inputs.conversations.get_conversation, session_id
        )
        return conv is not None and conv.labels.get(PROJECT_IDENTITY_LABEL_KEY) in reviewer

    async def _check(self, project: Any, ticket: dict[str, Any], sha: str, target: Target) -> str:
        """Raise a refusal unless *sha* may land now; return the current tip."""
        labels = {
            label.get("key"): (label.get("value") or "").strip()
            for label in ticket.get("labels") or []
        }
        if labels.get(APPROVED_LABEL) != sha:
            raise refuse(
                f"`{sha[:12]}` isn't approved: the ticket's `approved` label must be exactly "
                "this SHA, set by the Reviewer"
            )
        setters = await self._label_setters(project, ticket["id"])
        approvers = setters.get((APPROVED_LABEL, sha), [])
        if not any([await self._is_reviewer_or_owner(project, event) for event in approvers]):
            raise refuse("The `approved` label wasn't set by the Reviewer's session or the owner")
        if RISK_LABEL in labels:
            risk_ok = setters.get((RISK_OK_LABEL, labels.get(RISK_OK_LABEL, "")), [])
            if RISK_OK_LABEL not in labels or not self._is_owner(
                project, risk_ok[0] if risk_ok else None
            ):
                raise refuse(
                    f"The ticket has `risk: {labels[RISK_LABEL]}`; "
                    "the owner must add `risk-ok` first"
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
        return await asyncio.to_thread(self._fast_forward_check, target, sha)

    @staticmethod
    def _fast_forward_check(target: Target, sha: str) -> str:
        if _git(target.checkout, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode:
            raise refuse(f"`{sha[:12]}` isn't a commit in this repository")
        tip = _git(target.checkout, "rev-parse", f"refs/heads/{target.branch}").stdout.strip()
        if tip == sha:
            raise refuse(f"`{sha[:12]}` is already the tip of `{target.branch}`")
        ancestor = _git(target.checkout, "merge-base", "--is-ancestor", tip, sha, check=False)
        if ancestor.returncode != 0:
            raise refuse(
                f"`{sha[:12]}` doesn't fast-forward `{target.branch}` (tip `{tip[:12]}`): rebase "
                "onto the tip, then get the rebased SHA re-approved"
            )
        return tip

    # -- submit ----------------------------------------------------------------

    async def submit(self, project: Any, ticket_id: str, sha: str, who: TicketActor) -> dict:
        sha = sha.strip().lower()
        if not _SHA.fullmatch(sha):
            raise OmnigentError(
                "Give the full 40-character commit SHA", code=ErrorCode.INVALID_INPUT
            )
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
            tip = await self._check(project, ticket, sha, target)
        except OmnigentError as exc:
            await self._note(project, ticket["id"], f"refused `{sha[:12]}`: {exc}.")
            raise
        if any(item.ticket["id"] == ticket["id"] for item in self.pending):
            raise refuse("This ticket already has a landing queued")
        self.pending.append(Item(project, ticket, sha, who.session_id))
        self._save_pending()
        await self._note(
            project,
            ticket["id"],
            f"queued `{sha[:12]}` to fast-forward `{target.branch}` from `{tip[:12]}`.",
        )
        if self._batch is None or self._batch.done():
            self._batch = self._spawn(self._run_batch_later())
        return {"queued": True, "sha": sha, "branch": target.branch, "tip": tip}

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
                await self._check(project, item.ticket, item.sha, target)
            except OmnigentError as exc:
                await self._note(project, item.ticket["id"], f"refused `{item.sha[:12]}`: {exc}.")
                await self._send(
                    project, item.submitter, f"[{NOTE_PREFIX}] Ticket {item.ticket['id']}: {exc}."
                )
                continue
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
        landed_stage = stages[-2]["id"] if len(stages) >= 2 else None
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


def start(app: Any) -> LandingQueue:
    queue = LandingQueue(app)
    app.state.project_landing = queue
    queue.start()
    return queue
