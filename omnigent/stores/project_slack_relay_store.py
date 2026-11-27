"""Durable relay state for a project's Slack link, kept out of any agent's history.

The watcher is stateless between runs. Everything it needs to resume lives here:
the channel cursor, which messages were already handled, which threads the
project is talking in, and the replies waiting to be posted. Delivery to the
Lead happens between :meth:`SlackRelayStore.classify` and
:meth:`SlackRelayStore.commit`, keyed on each message's ts, so a crash in between
redelivers instead of losing a message, and the session queue drops the repeat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlProjectSlackBinding,
    SqlProjectSlackMessage,
    SqlProjectSlackOutbox,
    SqlProjectSlackThread,
    current_workspace_id,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError

# Threads quiet for longer than this are no longer re-read each run.
THREAD_OPEN_SECONDS = 7 * 24 * 3600
OPEN_THREADS_MAX = 50
BATCH_LIMIT = 40
REPLY_LEASE_SECONDS = 300
REPLY_ATTEMPTS_MAX = 3


def _ts_key(ts: str) -> tuple[int, int]:
    seconds, _, fraction = ts.partition(".")
    try:
        return int(seconds), int(fraction or 0)
    except ValueError:
        return 0, 0


def newer(left: str | None, right: str | None) -> str | None:
    """The later of two Slack timestamps."""
    if left is None:
        return right
    if right is None:
        return left
    return left if _ts_key(left) >= _ts_key(right) else right


@dataclass(frozen=True)
class SlackEvent:
    ts: str
    thread_ts: str
    user: str
    user_name: str
    text: str
    permalink: str
    mentioned: bool


@dataclass(frozen=True)
class RelayReply:
    id: str
    thread_ts: str
    text: str
    check_first: bool


class SlackRelayStore:
    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_slack_relay_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.project_slack_relay_store", immediate=True
        )

    def state(self, binding_id: str) -> dict[str, Any]:
        """What the watcher reads first: its cursor and the threads to re-read."""
        since = now_epoch() - THREAD_OPEN_SECONDS
        with self._session("read_slack_relay_state") as session:
            binding = session.get(SqlProjectSlackBinding, (current_workspace_id(), binding_id))
            threads = session.scalars(
                select(SqlProjectSlackThread)
                .where(
                    SqlProjectSlackThread.workspace_id == current_workspace_id(),
                    SqlProjectSlackThread.binding_id == binding_id,
                    SqlProjectSlackThread.updated_at >= since,
                )
                .order_by(SqlProjectSlackThread.updated_at.desc())
                .limit(OPEN_THREADS_MAX)
            ).all()
            return {
                "cursor": binding.channel_cursor if binding is not None else None,
                "threads": [
                    {"thread_ts": thread.thread_ts, "last_seen_ts": thread.last_seen_ts}
                    for thread in threads
                ],
                "batch_limit": BATCH_LIMIT,
            }

    def classify(
        self, binding_id: str, events: list[SlackEvent]
    ) -> tuple[list[SlackEvent], list[SlackEvent]]:
        """Split *events* into ones to hand the Lead and ones to skip.

        A message is handed on once: when it ``@``-mentions omni, or replies in a
        thread the project is already talking in. Repeats and chatter are skipped.
        """
        with self._session("classify_slack_events") as session:
            done = set(
                session.scalars(
                    select(SqlProjectSlackMessage.message_ts).where(
                        SqlProjectSlackMessage.workspace_id == current_workspace_id(),
                        SqlProjectSlackMessage.binding_id == binding_id,
                        SqlProjectSlackMessage.message_ts.in_([event.ts for event in events]),
                    )
                ).all()
            )
            known = set(
                session.scalars(
                    select(SqlProjectSlackThread.thread_ts).where(
                        SqlProjectSlackThread.workspace_id == current_workspace_id(),
                        SqlProjectSlackThread.binding_id == binding_id,
                        SqlProjectSlackThread.thread_ts.in_([event.thread_ts for event in events]),
                    )
                ).all()
            )
        deliver: list[SlackEvent] = []
        skip: list[SlackEvent] = []
        seen: set[str] = set()
        for event in sorted(events, key=lambda item: _ts_key(item.ts)):
            if event.ts in done or event.ts in seen:
                continue
            seen.add(event.ts)
            if event.mentioned or event.thread_ts in known:
                deliver.append(event)
                known.add(event.thread_ts)
            else:
                skip.append(event)
        return deliver, skip

    def commit(
        self,
        binding_id: str,
        *,
        delivered: list[SlackEvent],
        skipped: list[SlackEvent],
        outcome_for: dict[str, str] | None = None,
        cursor: str | None,
    ) -> None:
        """Record handled messages, update their threads, and advance the cursor."""
        now = now_epoch()
        outcomes = outcome_for or {}

        def write(session: Session) -> None:
            workspace = current_workspace_id()
            for event in [*delivered, *skipped]:
                if session.get(SqlProjectSlackMessage, (workspace, binding_id, event.ts)) is None:
                    session.add(
                        SqlProjectSlackMessage(
                            workspace_id=workspace,
                            binding_id=binding_id,
                            message_ts=event.ts,
                            outcome=outcomes.get(
                                event.ts, "delivered" if event in delivered else "ignored"
                            ),
                            created_at=now,
                        )
                    )
            for event in delivered:
                if outcomes.get(event.ts, "delivered") != "delivered":
                    continue
                thread = session.get(
                    SqlProjectSlackThread, (workspace, binding_id, event.thread_ts)
                )
                if thread is None:
                    session.add(
                        SqlProjectSlackThread(
                            workspace_id=workspace,
                            binding_id=binding_id,
                            thread_ts=event.thread_ts,
                            root_user=event.user,
                            request_ts=event.ts,
                            last_seen_ts=event.ts,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                    session.flush()
                else:
                    thread.request_ts = newer(thread.request_ts, event.ts) or event.ts
                    thread.last_seen_ts = newer(thread.last_seen_ts, event.ts) or event.ts
                    thread.updated_at = now
            if cursor is not None:
                binding = session.get(SqlProjectSlackBinding, (workspace, binding_id))
                if binding is not None:
                    binding.channel_cursor = newer(binding.channel_cursor, cursor)

        run_write_transaction(self._session_immediate, "commit_slack_events", write)

    def thread(self, binding_id: str, thread_ts: str) -> SqlProjectSlackThread | None:
        with self._session("get_slack_thread") as session:
            row = session.get(
                SqlProjectSlackThread, (current_workspace_id(), binding_id, thread_ts)
            )
            if row is not None:
                session.expunge(row)
            return row

    def enqueue_reply(
        self,
        binding_id: str,
        thread_ts: str,
        render: Any,
        *,
        ticket_id: str | None = None,
    ) -> str:
        """Queue a reply for *thread_ts*; returns its id.

        ``render(reply_id, mention_user)`` builds the Slack text; *mention_user*
        is the thread's root author the first time a request is answered, else
        ``None``, so they are pinged once per new request.

        :raises OmnigentError: ``NOT_FOUND`` when the project isn't in that thread.
        """
        now = now_epoch()
        reply_id = uuid.uuid4().hex

        def write(session: Session) -> str:
            workspace = current_workspace_id()
            thread = session.get(SqlProjectSlackThread, (workspace, binding_id, thread_ts))
            if thread is None:
                raise OmnigentError(
                    "That Slack thread isn't one this project was asked in",
                    code=ErrorCode.NOT_FOUND,
                )
            mention = None
            if thread.mentioned_request_ts != thread.request_ts:
                mention = thread.root_user
                thread.mentioned_request_ts = thread.request_ts
            if ticket_id is not None:
                thread.ticket_id = ticket_id
            thread.updated_at = now
            session.add(
                SqlProjectSlackOutbox(
                    workspace_id=workspace,
                    id=reply_id,
                    binding_id=binding_id,
                    thread_ts=thread_ts,
                    text=render(reply_id, mention),
                    state="pending",
                    attempts=0,
                    created_at=now,
                    updated_at=now,
                )
            )
            return reply_id

        return run_write_transaction(self._session_immediate, "enqueue_slack_reply", write)

    def claim_replies(self, binding_id: str, *, limit: int = 10) -> list[RelayReply]:
        """Lease up to *limit* replies to post, oldest first.

        A reply whose earlier lease ran out, or whose post was uncertain, comes
        back with ``check_first`` so the watcher looks for it before reposting.
        """
        now = now_epoch()

        def write(session: Session) -> list[RelayReply]:
            rows = session.scalars(
                select(SqlProjectSlackOutbox)
                .where(
                    SqlProjectSlackOutbox.workspace_id == current_workspace_id(),
                    SqlProjectSlackOutbox.binding_id == binding_id,
                    SqlProjectSlackOutbox.state.in_(("pending", "claimed", "uncertain")),
                )
                .order_by(SqlProjectSlackOutbox.created_at)
                .limit(max(limit * 3, limit))
            ).all()
            claimed: list[RelayReply] = []
            for row in rows:
                if len(claimed) >= limit:
                    break
                if row.state == "claimed" and (row.lease_until or 0) > now:
                    continue
                check_first = row.state in ("claimed", "uncertain")
                row.state = "claimed"
                row.attempts += 1
                row.lease_until = now + REPLY_LEASE_SECONDS
                row.updated_at = now
                claimed.append(RelayReply(row.id, row.thread_ts, row.text, check_first))
            return claimed

        return run_write_transaction(self._session_immediate, "claim_slack_replies", write)

    def ack_reply(
        self,
        binding_id: str,
        reply_id: str,
        *,
        slack_ts: str | None = None,
        failed: str | None = None,
        uncertain: bool = False,
    ) -> str:
        """Settle a claimed reply; returns its new state."""
        now = now_epoch()

        def write(session: Session) -> str:
            row = session.get(SqlProjectSlackOutbox, (current_workspace_id(), reply_id))
            if row is None or row.binding_id != binding_id:
                raise OmnigentError("No such reply", code=ErrorCode.NOT_FOUND)
            if row.state == "posted":
                return row.state
            if slack_ts:
                row.state, row.slack_ts, row.last_error = "posted", slack_ts, None
            elif uncertain:
                row.state = "uncertain"
            else:
                row.last_error = (failed or "failed")[:500]
                row.state = "failed" if row.attempts >= REPLY_ATTEMPTS_MAX else "pending"
            row.lease_until = None
            row.updated_at = now
            return row.state

        return run_write_transaction(self._session_immediate, "ack_slack_reply", write)
