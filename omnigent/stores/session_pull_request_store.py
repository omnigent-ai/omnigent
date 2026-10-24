"""Persistent store for the pull requests linked to each session.

Hosts report the PRs they observe and the states they fetch; this store
keeps them so the Pull Requests tab can list a session's PRs while its host is
offline. Reports merge commutatively, so a host re-reporting its local
registry on every poll converges on the same rows in any order. An unlinked PR
keeps its row with ``removed_at`` set, which hides it and ignores further
reports until the user links it again.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlSessionPullRequest,
    current_workspace_id,
    pull_request_url_cksum,
)
from omnigent.db.enum_codecs import (
    SESSION_PR_STATE,
    decode_session_pr_relationship,
    decode_session_pr_state,
    encode_session_pr_relationship,
    encode_session_pr_state,
)
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch_us,
    run_write_transaction,
)

Relationship = Literal["created", "worked_on", "attached", "inferred"]

# A stronger relationship survives any later report; "created" is never downgraded.
_RELATIONSHIP_RANK: dict[str, int] = {"inferred": 0, "worked_on": 1, "attached": 2, "created": 3}
_TITLE_MAX = 1024


@dataclass(frozen=True)
class PullRequestReport:
    """One PR as a host's registry describes it; times are epoch seconds.

    :ivar url: Canonical PR URL, as the host's git provider normalized it.
    :ivar provider: Git provider id, e.g. ``"github"``.
    :ivar host: Forge host, e.g. ``"github.com"``.
    :ivar repository: Base repository, e.g. ``"owner/repo"``.
    :ivar number: The PR number.
    :ivar relationship: How the PR joined the session.
    :ivar first_seen_at: When the PR was first observed.
    :ivar last_seen_at: When the PR was last observed.
    :ivar title: The host's latest title, or ``None`` when it has none.
    """

    url: str
    provider: str
    host: str
    repository: str
    number: int
    relationship: Relationship
    first_seen_at: float
    last_seen_at: float
    title: str | None = None


@dataclass(frozen=True)
class StoredPullRequest(PullRequestReport):
    """A linked PR with the last state its host fetched.

    :ivar state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``, or ``None`` until fetched.
    :ivar is_draft: Whether the PR was a draft at that fetch.
    :ivar state_checked_at: Epoch seconds of that fetch, or ``None``.
    """

    state: str | None = None
    is_draft: bool = False
    state_checked_at: float | None = None


def _us(seconds: float) -> int:
    return int(seconds * 1_000_000)


def _seconds(micros: int | None) -> float | None:
    return None if micros is None else micros / 1_000_000


def _to_entity(row: SqlSessionPullRequest) -> StoredPullRequest:
    return StoredPullRequest(
        url=row.url,
        provider=row.provider,
        host=row.host,
        repository=row.repository,
        number=row.number,
        relationship=cast(Relationship, decode_session_pr_relationship(row.relationship)),
        first_seen_at=row.first_seen_at / 1_000_000,
        last_seen_at=row.last_seen_at / 1_000_000,
        title=row.title,
        state=None if row.state is None else decode_session_pr_state(row.state),
        is_draft=row.is_draft,
        state_checked_at=_seconds(row.state_checked_at),
    )


def _display_order(entries: Iterable[StoredPullRequest]) -> list[StoredPullRequest]:
    """Explicit activity first, most recent first; then inferred PRs, oldest first."""
    return sorted(
        entries,
        key=lambda pr: (
            pr.relationship == "inferred",
            pr.first_seen_at if pr.relationship == "inferred" else -pr.last_seen_at,
            pr.url,
        ),
    )


def _merge(row: SqlSessionPullRequest, report: PullRequestReport) -> bool:
    """Fold ``report`` into ``row``; return whether anything changed."""
    before = (row.relationship, row.first_seen_at, row.last_seen_at, row.title)
    current = decode_session_pr_relationship(row.relationship)
    if _RELATIONSHIP_RANK[report.relationship] > _RELATIONSHIP_RANK[current]:
        row.relationship = encode_session_pr_relationship(report.relationship)
    row.first_seen_at = min(row.first_seen_at, _us(report.first_seen_at))
    row.last_seen_at = max(row.last_seen_at, _us(report.last_seen_at))
    # The host reports its current title, or none when a lookup failed.
    if report.title:
        row.title = report.title[:_TITLE_MAX]
    return before != (row.relationship, row.first_seen_at, row.last_seen_at, row.title)


def _new_row(conversation_id: str, report: PullRequestReport) -> SqlSessionPullRequest:
    return SqlSessionPullRequest(
        workspace_id=current_workspace_id(),
        conversation_id=conversation_id,
        url_cksum=pull_request_url_cksum(report.url),
        url=report.url,
        provider=report.provider,
        host=report.host,
        repository=report.repository,
        number=report.number,
        relationship=encode_session_pr_relationship(report.relationship),
        first_seen_at=_us(report.first_seen_at),
        last_seen_at=_us(report.last_seen_at),
        title=report.title[:_TITLE_MAX] if report.title else None,
    )


class SessionPullRequestStore:
    """SQLAlchemy-backed store of the pull requests linked to each session."""

    def __init__(self, storage_location: str) -> None:
        """Open the store.

        :param storage_location: SQLAlchemy database URI, e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store"
        )
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.session_pull_request_store", immediate=True
        )

    def _key(self, conversation_id: str, url: str) -> tuple[int, str, bytes]:
        return (current_workspace_id(), conversation_id, pull_request_url_cksum(url))

    def _rows(
        self, session: Session, conversation_ids: Sequence[str]
    ) -> list[SqlSessionPullRequest]:
        stmt = select(SqlSessionPullRequest).where(
            SqlSessionPullRequest.workspace_id == current_workspace_id(),
            SqlSessionPullRequest.conversation_id.in_(conversation_ids),
        )
        return list(session.execute(stmt).scalars().all())

    def list_for_session(self, conversation_id: str) -> list[StoredPullRequest]:
        """Return the session's linked PRs in display order, without unlinked ones.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        with self._session("list_session_pull_requests") as session:
            rows = self._rows(session, [conversation_id])
            return _display_order(_to_entity(row) for row in rows if row.removed_at is None)

    def removed_urls(self, conversation_id: str) -> list[str]:
        """Return the URLs the user unlinked from the session.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        """
        stmt = select(SqlSessionPullRequest.url).where(
            SqlSessionPullRequest.workspace_id == current_workspace_id(),
            SqlSessionPullRequest.conversation_id == conversation_id,
            SqlSessionPullRequest.removed_at.is_not(None),
        )
        with self._session("list_unlinked_session_pull_requests") as session:
            return sorted(session.execute(stmt).scalars().all())

    def primary_for_sessions(
        self, conversation_ids: Sequence[str]
    ) -> dict[str, StoredPullRequest]:
        """Return each session's first linked PR in display order.

        :param conversation_ids: Sessions to summarize; ones without PRs are absent.
        """
        if not conversation_ids:
            return {}
        with self._session("list_primary_session_pull_requests") as session:
            rows = self._rows(session, conversation_ids)
            by_session: dict[str, list[StoredPullRequest]] = {}
            for row in rows:
                if row.removed_at is None:
                    by_session.setdefault(row.conversation_id, []).append(_to_entity(row))
        return {
            conversation_id: _display_order(entries)[0]
            for conversation_id, entries in by_session.items()
        }

    def _write(self, operation: str, callback: Any) -> Any:
        """Run a write, replaying it once when a concurrent insert claimed the same row."""
        try:
            return run_write_transaction(self._session_immediate, operation, callback)
        except IntegrityError:
            return run_write_transaction(self._session_immediate, operation, callback)

    def record(self, conversation_id: str, reports: Sequence[PullRequestReport]) -> bool:
        """Merge reported PRs into the session; unlinked ones are ignored.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param reports: PRs as the host's registry describes them.
        :returns: Whether any row was added or changed.
        """
        unique = list({report.url: report for report in reports}.values())
        if not unique:
            return False

        def write(session: Session) -> bool:
            changed = False
            for report in unique:
                row = session.get(
                    SqlSessionPullRequest,
                    self._key(conversation_id, report.url),
                    with_for_update=True,
                )
                if row is None:
                    session.add(_new_row(conversation_id, report))
                    changed = True
                elif row.removed_at is None:
                    changed = _merge(row, report) or changed
            return changed

        return cast(bool, self._write("record_session_pull_requests", write))

    def attach(self, conversation_id: str, report: PullRequestReport) -> None:
        """Link a PR the user chose, clearing an earlier unlink.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param report: The PR as the host that verified it describes it.
        """
        attached = replace(report, relationship="attached")

        def write(session: Session) -> None:
            row = session.get(
                SqlSessionPullRequest,
                self._key(conversation_id, report.url),
                with_for_update=True,
            )
            if row is None:
                session.add(_new_row(conversation_id, attached))
                return
            row.removed_at = None
            _merge(row, attached)

        self._write("attach_session_pull_request", write)

    def remove(self, conversation_id: str, url: str) -> bool:
        """Unlink a PR, remembering it so later reports cannot link it again.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :returns: Whether the PR was linked.
        """

        def write(session: Session) -> bool:
            row = session.get(
                SqlSessionPullRequest, self._key(conversation_id, url), with_for_update=True
            )
            if row is None or row.removed_at is not None:
                return False
            row.removed_at = now_epoch_us()
            return True

        return cast(
            bool,
            run_write_transaction(self._session_immediate, "unlink_session_pull_request", write),
        )

    def update_state(
        self,
        conversation_id: str,
        url: str,
        *,
        state: str,
        is_draft: bool,
        title: str | None,
        checked_at: float,
    ) -> bool:
        """Store the state a host fetched for a linked PR, and refresh its title.

        A fetch older than the stored one, an unknown state, or a PR that is
        not linked is ignored.

        :param conversation_id: The session, e.g. ``"conv_abc123"``.
        :param url: The linked PR's URL.
        :param state: ``"OPEN"``, ``"CLOSED"`` or ``"MERGED"``.
        :param is_draft: Whether the PR is a draft.
        :param title: The fetched title, or ``None`` to keep the stored one.
        :param checked_at: Epoch seconds the host fetched it.
        :returns: Whether the row changed.
        """
        if state not in SESSION_PR_STATE:
            return False
        checked_us = _us(checked_at)
        fetched_title = title.strip()[:_TITLE_MAX] if title and title.strip() else None

        def write(session: Session) -> bool:
            row = session.get(
                SqlSessionPullRequest, self._key(conversation_id, url), with_for_update=True
            )
            if row is None or row.removed_at is not None:
                return False
            if row.state_checked_at is not None and row.state_checked_at > checked_us:
                return False
            before = (row.state, row.is_draft, row.title)
            row.state = encode_session_pr_state(state)
            row.is_draft = is_draft
            row.state_checked_at = checked_us
            if fetched_title is not None:
                row.title = fetched_title
            return before != (row.state, row.is_draft, row.title)

        return cast(
            bool,
            run_write_transaction(
                self._session_immediate, "update_session_pull_request_state", write
            ),
        )
