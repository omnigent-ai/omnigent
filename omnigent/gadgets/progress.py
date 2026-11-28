"""Progress of gadget actions, read from the sessions doing the work.

A gadget session reports progress with ``sys_gadget_action_update``, but it can
skip a call, and its runner can stop mid-action. So while an action is open its
status comes from what the server records about the sessions themselves (live
status, runner liveness, pending approvals, last error), and the agent's report
only says how it ended and which session took the work. An action shows as
running only while one of its sessions is running on a live runner; once they
stop it settles.

Which sessions belong to an action: the session the agent recorded on it and
that session's children; otherwise the gadget session and the sessions started
from it (child sessions, and independent ones that carry its
``omnigent.created_by_session_id`` label) between this action's delivery and the
delivery of the next action the gadget session took up; one still queued behind it
doesn't cut the window short. Deliveries in the same second keep the order the
viewer made them in. A session is working while its turn runs or waits, or while a
background task it left running is still going, the same rule the session's
Tasks tab uses. A session bound to a runner or host that hasn't reported in counts
as offline, not as running in-process. Sessions are matched by creation time, so
attribution without a recorded session can include an unrelated child and keep the
action running longer, never shorter, and when an action's own session tree or family
is too large to read whole (so a worker may be missing) it is not called finished. An
independent session that is in no family and wasn't recorded on the action is not found:
finding it would mean scanning the workspace's sessions, so the skill asks the agent to
record it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store import (
    CREATED_BY_SESSION_LABEL_KEY as CREATED_BY_SESSION_LABEL,
)
from omnigent.stores.conversation_store import runner_seen_is_fresh

# Delivered but not started within this long: the message did not reach a turn.
START_GRACE_S = 120
# A session reported running with no recorded activity for this long is treated as stopped. Its
# status is mirrored asynchronously, so a lost final update must not hold an action open forever.
SILENT_RUNNING_LIMIT_S = 60 * 60
# A message to a stopped gadget session marks it working before its runner is back; for this long
# after the delivery that session is starting, not interrupted. Sandboxes can take minutes to wake.
RUNNER_START_GRACE_S = 10 * 60
# Child sessions considered per gadget session tree (one bounded spawn-tree read).
MAX_CHILD_SESSIONS = 100

REPORTED_TERMINAL = ("done", "failed", "cancelled")
# Reported states that say the gadget session took the action up.
_STARTED = ("running", "needs_input", *REPORTED_TERMINAL)
OPEN_STATUSES = ("pending", "queued", "running", "needs_input")
# What the gadget is told, most specific first.
STATUSES = (
    "pending",  # Requested; the web app has not delivered it to a session yet.
    "queued",  # Delivered; the gadget session has not started it (busy with an earlier action).
    "running",
    "needs_input",  # A session doing the work waits for an approval or answer.
    "done",
    "failed",
    "cancelled",
    "finished_unreported",  # Its sessions stopped and the agent never reported how it ended.
    "interrupted",  # A session doing the work failed, went offline, or never started it.
)


@dataclass(frozen=True)
class SessionState:
    """What the server records about one session. Times are epoch seconds."""

    id: str
    created_at: int
    updated_at: int
    parent_id: str | None
    root_id: str
    status: str  # idle, running, waiting or failed
    runner_online: bool
    background_work: bool
    pending_inputs: int
    error_code: str | None


@dataclass(frozen=True)
class Delivery:
    """When an action reached its gadget session.

    :param sequence: The viewer's delivery count at that point, which orders deliveries made
        in the same second; ``0`` for one recorded without it.
    """

    session_id: str
    delivered_at: int
    sequence: int = 0


class SessionStates(dict[str, SessionState]):
    """Sessions by ID, and whether every session that could belong to the work was read.

    ``complete`` is ``False`` when a listing reached its limit, so a session doing the work
    may be missing; progress then never concludes that the work stopped.
    ``truncated_trees`` names the session trees (by root ID) whose own listing reached its
    limit, so a session in one of them may be missing.
    """

    complete: bool = True
    truncated_trees: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ActionProgress:
    status: str
    reason: str | None
    session_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "session_ids": list(self.session_ids),
        }


def delivery_of(raw: Any) -> Delivery | None:
    """A viewer's open-action record as a delivery, or ``None`` while it is not delivered yet."""
    if not isinstance(raw, dict):
        return None
    session_id, delivered_at = raw.get("session_id"), raw.get("delivered_at")
    if not isinstance(session_id, str) or type(delivered_at) is not int:
        return None
    sequence = raw.get("delivery_sequence")
    return Delivery(session_id, delivered_at, sequence if type(sequence) is int else 0)


def _descendants(root: str, sessions: dict[str, SessionState]) -> list[str]:
    children: dict[str, list[str]] = {}
    for state in sessions.values():
        if state.parent_id is not None:
            children.setdefault(state.parent_id, []).append(state.id)
    found: list[str] = []
    frontier = [root]
    while frontier:
        current = frontier.pop()
        for child in children.get(current, []):
            if child not in found and child != root:
                found.append(child)
                frontier.append(child)
    return found


def _attributed(
    value: dict,
    delivery: Delivery,
    next_delivered_at: int | None,
    sessions: dict[str, SessionState],
) -> list[str]:
    worker = value.get("session_id")
    if isinstance(worker, str) and worker != delivery.session_id and worker in sessions:
        return [worker, *_descendants(worker, sessions)]
    started = [
        session_id
        for session_id in _descendants(delivery.session_id, sessions)
        if sessions[session_id].created_at >= delivery.delivered_at
        and (next_delivered_at is None or sessions[session_id].created_at < next_delivered_at)
    ]
    # A child's own children belong with it even when they started after the next delivery.
    attributed = set(started)
    for session_id in started:
        attributed.update(_descendants(session_id, sessions))
    return [delivery.session_id, *sorted(attributed)]


def _working(state: SessionState) -> bool:
    return state.status in ("running", "waiting") or (
        state.background_work and state.status != "failed"
    )


def _busy(state: SessionState, now: int) -> bool:
    return (
        _working(state) and state.runner_online and now - state.updated_at < SILENT_RUNNING_LIMIT_S
    )


def derive_progress(
    value: dict,
    delivery: Delivery | None,
    *,
    sessions: dict[str, SessionState],
    earlier_open: bool,
    next_delivered_at: int | None,
    now: int,
) -> ActionProgress:
    """The status of one ledger entry from its delivery and its sessions' recorded state.

    ``earlier_open`` says an action delivered to the same gadget session before this one is still
    open, so the gadget session being busy means it has not reached this one yet.
    """
    reported = value.get("state")
    if reported in REPORTED_TERMINAL:
        ids = tuple(_attributed(value, delivery, next_delivered_at, sessions)) if delivery else ()
        return ActionProgress(str(reported), None, ids)
    if delivery is None:
        return ActionProgress("pending", None, ())
    gadget = sessions.get(delivery.session_id)
    if gadget is None:
        return ActionProgress("interrupted", "session_unavailable", (delivery.session_id,))
    ids = _attributed(value, delivery, next_delivered_at, sessions)
    states = [sessions[session_id] for session_id in ids]
    workers = [state for state in states if state.id != gadget.id]
    if any(state.pending_inputs > 0 and _busy(state, now) for state in states):
        return ActionProgress("needs_input", None, tuple(ids))
    if any(_busy(state, now) for state in workers):
        return ActionProgress("running", None, tuple(ids))
    if _busy(gadget, now):
        if earlier_open:
            return ActionProgress("queued", None, tuple(ids))
        return ActionProgress("running", None, tuple(ids))
    # A session that failed at or after the delivery failed on this action, even in the same
    # second: posting the message marks a session working, so an older failure would not
    # still show. That is known, so it counts before any sessions that weren't read; other
    # activity counts only after that second, which the post itself fills.
    failed = [
        state
        for state in states
        if state.status == "failed"
        and state.updated_at >= delivery.delivered_at
        and not _working(state)
    ]
    if failed and not any(_working(state) for state in states):
        return ActionProgress("interrupted", failed[0].error_code or "session_failed", tuple(ids))
    # Not every session that could do the work was read: one of them may still run it.
    if not getattr(sessions, "complete", True) and now - delivery.delivered_at < (
        SILENT_RUNNING_LIMIT_S
    ):
        return ActionProgress("running", "sessions_not_all_read", tuple(ids))
    stopped = [state for state in states if _working(state)]
    if stopped:
        offline = any(not state.runner_online for state in stopped)
        waking = (
            offline
            and all(state.id == gadget.id for state in stopped)
            and now - delivery.delivered_at < RUNNER_START_GRACE_S
        )
        if waking:
            return ActionProgress("queued", None, tuple(ids))
        return ActionProgress(
            "interrupted", "runner_offline" if offline else "no_activity", tuple(ids)
        )
    acted = [state for state in states if state.updated_at > delivery.delivered_at + 1]
    if acted:
        return ActionProgress("finished_unreported", None, tuple(ids))
    if earlier_open:
        return ActionProgress("queued", None, tuple(ids))
    if now - delivery.delivered_at < START_GRACE_S:
        return ActionProgress("queued", None, tuple(ids))
    return ActionProgress("interrupted", "not_started", tuple(ids))


def session_tree_status(
    sessions: dict[str, SessionState], root_id: str, now: int | None = None
) -> dict[str, Any]:
    """Whether a session and the sessions it started are working, by the same rules as actions."""
    clock = int(time.time()) if now is None else now
    root = sessions.get(root_id)
    if root is None:
        return {"status": "unavailable", "reason": "session_unavailable"}
    states = [root, *(sessions[session_id] for session_id in _descendants(root_id, sessions))]
    if any(state.pending_inputs > 0 and _busy(state, clock) for state in states):
        return {"status": "needs_input", "reason": None}
    if any(_busy(state, clock) for state in states):
        return {"status": "running", "reason": None}
    stopped = [state for state in states if _working(state)]
    if stopped:
        offline = any(not state.runner_online for state in stopped)
        return {"status": "interrupted", "reason": "runner_offline" if offline else "no_activity"}
    if root.status == "failed":
        return {"status": "interrupted", "reason": root.error_code or "session_failed"}
    # A session past the tree's listing limit may still be working: don't call it idle.
    if any(state.root_id in getattr(sessions, "truncated_trees", ()) for state in states):
        return {"status": "unknown", "reason": "sessions_not_all_read"}
    return {"status": "idle", "reason": None}


def derive_ledger_progress(
    entries: Iterable[tuple[str, dict, Delivery | None]],
    *,
    sessions: dict[str, SessionState],
    now: int | None = None,
) -> dict[str, ActionProgress]:
    """Progress of several entries, taking delivery order within each gadget session into
    account."""
    clock = int(time.time()) if now is None else now
    result: dict[str, ActionProgress] = {}
    by_session: dict[str, list[tuple[int, str, dict, Delivery]]] = {}
    for record_id, value, delivery in entries:
        if delivery is None:
            result[record_id] = derive_progress(
                value,
                None,
                sessions=sessions,
                earlier_open=False,
                next_delivered_at=None,
                now=clock,
            )
        else:
            by_session.setdefault(delivery.session_id, []).append(
                (delivery.delivered_at, record_id, value, delivery)
            )
    for delivered in by_session.values():
        # Delivery order: the time, then the viewer's delivery count within the same second.
        delivered.sort(key=lambda item: (item[0], item[3].sequence, item[1]))
        earlier_open = False
        for index, (_, record_id, value, delivery) in enumerate(delivered):
            # This action's sessions are those started before the gadget session took up a
            # later action; one still queued behind it doesn't end this one's window.
            next_at = next(
                (
                    later[0]
                    for later in delivered[index + 1 :]
                    if later[2].get("state") in _STARTED
                    or isinstance(later[2].get("session_id"), str)
                ),
                None,
            )
            progress = derive_progress(
                value,
                delivery,
                sessions=sessions,
                earlier_open=earlier_open,
                next_delivered_at=next_at,
                now=clock,
            )
            result[record_id] = progress
            earlier_open = earlier_open or progress.status in OPEN_STATUSES
    return result


class SessionStateReader:
    """Reads the recorded state of a caller's gadget sessions and their children.

    ``can_read(session_id)`` checks the caller's access; sessions they cannot read are left out,
    so an action delivered to one shows as interrupted instead of exposing its state.
    """

    def __init__(
        self,
        conversation_store: Any,
        can_read: Callable[[str], bool],
        user_id: str | None = None,
        can_take_actions: Callable[[str], bool] | None = None,
    ) -> None:
        """
        :param can_take_actions: Whether a readable session may also do the caller's gadget
            actions: its agent's gadget calls must be allowed, so on a server with sharing
            it is one the caller owns and hasn't shared with edit access. ``None`` allows any
            readable session.
        """
        self._store = conversation_store
        self._can_read = can_read
        self._user_id = user_id
        self._can_take_actions = can_take_actions

    def available(self, session_id: str) -> bool:
        """Whether the session still exists, is not archived and the caller can read it."""
        if not self._can_read(session_id):
            return False
        conversation = self._store.get_conversations([session_id]).get(session_id)
        return conversation is not None and not getattr(conversation, "archived", False)

    def takes_actions(self, session_id: str) -> bool:
        """Whether the session is available and its agent could do the caller's actions."""
        if not self.available(session_id):
            return False
        return self._can_take_actions is None or self._can_take_actions(session_id)

    def read(self, session_ids: Iterable[str]) -> SessionStates:
        from omnigent.server.routes._sessions.helpers import (
            _last_task_error_from_labels,
            _session_status_from_cache,
        )

        roots = [
            session_id for session_id in dict.fromkeys(session_ids) if self._can_read(session_id)
        ]
        if not roots:
            return SessionStates()
        conversations = dict(self._store.get_conversations(roots))
        complete = True
        truncated: set[str] = set()
        trees_read: set[str] = set()

        def read_tree(conversation: Any) -> None:
            # One bounded read of the root index (list_spawn_tree), not a listing sorted by
            # time, so the rows examined stay within the limit however large the tree is.
            nonlocal complete
            root = conversation.root_conversation_id
            if root in trees_read:
                return
            trees_read.add(root)
            page = self._store.list_spawn_tree(root, limit=MAX_CHILD_SESSIONS)
            complete = complete and not page.has_more
            if page.has_more:
                truncated.add(root)
            for child in page.data:
                conversations.setdefault(child.id, child)

        for conversation in list(conversations.values()):
            read_tree(conversation)
        # Independent sessions have no parent link, only a label naming the session that started
        # them. Sessions a session starts join its family, which is one bounded read. One
        # started outside a family is found only through the action, which records the session
        # it hands work to: a workspace-wide listing would scan the session history.
        candidates: dict[str, Any] = {}
        families = sorted(
            {family for c in conversations.values() if (family := getattr(c, "family_id", None))}
        )
        if families:
            try:
                for members in self._store.get_session_families(families).values():
                    for member in members:
                        candidates.setdefault(member.id, member)
            except NotImplementedError:
                pass
            except OmnigentError:
                complete = False  # A family too large to read: some members are unknown.
        # A worker can start workers of its own: follow the labels until nothing new is found.
        found = True
        while found:
            found = False
            for conversation in candidates.values():
                if conversation.id in conversations:
                    continue
                starter = (conversation.labels or {}).get(CREATED_BY_SESSION_LABEL)
                if starter in conversations and self._can_read(conversation.id):
                    conversations[conversation.id] = conversation
                    read_tree(conversation)
                    found = True
        connectivity = self._store.get_session_connectivity(list(conversations))
        now = int(time.time())
        own_liveness: dict[str, bool] = {}
        hosted: set[str] = set()
        for session_id, link in connectivity.items():
            if link.runner_last_seen is not None:
                own_liveness[session_id] = runner_seen_is_fresh(link.runner_last_seen, now)
            elif link.runner_id is not None or link.host_id is not None:
                # Bound to a runner or host that hasn't reported in: starting, or gone.
                hosted.add(session_id)

        def runner_online(session_id: str) -> bool:
            # Children run in their parent's runner and may carry no stamp of their own. A
            # tree with no stamp is online only when nothing in it is bound to a runner or
            # host, which means it runs in-process; the silent-running limit still applies.
            # Only the parent link is followed: a session another one started (the creation
            # label) runs on its own runner, so its creator's heartbeat says nothing about it.
            seen: set[str] = set()
            chain: list[str] = []
            current: str | None = session_id
            while current is not None and current not in seen:
                if current in own_liveness:
                    return own_liveness[current]
                seen.add(current)
                chain.append(current)
                parent = conversations.get(current)
                current = parent.parent_conversation_id if parent is not None else None
            return not any(link in hosted for link in chain)

        states = SessionStates()
        states.complete = complete
        states.truncated_trees = frozenset(truncated)
        for conversation in conversations.values():
            status = _session_status_from_cache(conversation.id, conversation.live_status)
            if conversation.live_status == "waiting" and status == "running":
                status = "waiting"
            error = _last_task_error_from_labels(conversation.labels or {})
            states[conversation.id] = SessionState(
                id=conversation.id,
                created_at=int(conversation.created_at),
                updated_at=int(conversation.updated_at),
                parent_id=conversation.parent_conversation_id
                or (conversation.labels or {}).get(CREATED_BY_SESSION_LABEL),
                root_id=conversation.root_conversation_id,
                status=status,
                runner_online=runner_online(conversation.id),
                background_work=conversation.background_work_running is True,
                pending_inputs=int(conversation.pending_elicitation_count or 0),
                error_code=error.get("code") if error else None,
            )
        return states
