"""Durable directed grants and input/request receipts in the metadata database."""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, NoReturn

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlLinkedReply,
    SqlLinkedRequest,
    SqlSessionInputAuthority,
    SqlSessionLink,
    SqlSessionLinkEpoch,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_OWNER
from omnigent.server.permissions import resolved_allows
from omnigent.session_authority import (
    DELIVERY_LEASE_SECONDS,
    MAX_AUTHORITY_INPUTS,
    RequestAuthority,
)
from omnigent.stores.permission_store.sqlalchemy_store import resolve_access_in_session

MAX_DELEGATION_DEPTH = 8
MAX_REQUESTS_PER_ROOT = 64
REQUEST_TTL_SECONDS = 300
MAX_MESSAGE_BYTES = 8_000


class DeliveryState(IntEnum):
    PENDING = 1
    HANDOFF = 2
    ACCEPTED = 3
    UNCERTAIN = 4
    REJECTED = 5
    CLAIMED = 6


@dataclass(frozen=True)
class Endpoint:
    session_id: str
    permission_id: str
    runner_id: str | None


@dataclass(frozen=True)
class InputIdentity:
    input_id: str
    requester: str
    account_generation: str | None
    parent_request_ids: tuple[str, ...] = ()


def _deny(message: str = "Session link operation is not permitted") -> NoReturn:
    raise OmnigentError(message, code=ErrorCode.FORBIDDEN)


def payload_digest(message: str) -> bytes:
    data = message.encode("utf-8")
    if not data or len(data) > MAX_MESSAGE_BYTES:
        raise OmnigentError(
            f"Message must contain 1–{MAX_MESSAGE_BYTES} UTF-8 bytes",
            code=ErrorCode.INVALID_INPUT,
        )
    return hashlib.sha256(data).digest()


def operation_digest(operation_id: str) -> bytes:
    if not 1 <= len(operation_id.encode()) <= 128:
        raise OmnigentError("Invalid operation id", code=ErrorCode.INVALID_INPUT)
    return hashlib.sha256(operation_id.encode()).digest()


def receipt_id(source_id: str, operation_id: str) -> str:
    """Retries use the same identifier even after a split-database commit."""
    return uuid.uuid5(
        uuid.UUID(source_id), f"{current_workspace_id()}:{operation_digest(operation_id).hex()}"
    ).hex


def payload_key(kind: str, identifier: str, digest: bytes) -> str:
    return f"session-links/{current_workspace_id()}/{kind}/{identifier}/{digest.hex()}"


def invalidate_link_epoch(
    session: Session, session_id: str, *, blocked: bool | None = None
) -> None:
    """Invalidate approvals without scanning every incoming/outgoing link."""
    row = session.get(
        SqlSessionLinkEpoch, (current_workspace_id(), session_id), with_for_update=True
    )
    if row is None and blocked is not None:
        session.add(
            SqlSessionLinkEpoch(
                workspace_id=current_workspace_id(),
                session_id=session_id,
                generation=uuid.uuid4().hex,
                blocked=blocked,
            )
        )
    elif row is not None:
        row.generation = uuid.uuid4().hex
        if blocked is not None:
            row.blocked = blocked


def _current_binding(session: Session, endpoint: Endpoint) -> None:
    metadata = session.get(
        SqlConversationMetadata,
        (current_workspace_id(), endpoint.session_id),
        with_for_update=True,
        populate_existing=True,
    )
    if metadata is None or metadata.runner_id != endpoint.runner_id:
        _deny("The session's runner binding changed; retry with its current runner")


def _current_input(session: Session, endpoint: Endpoint, identity: InputIdentity) -> None:
    record = session.get(
        SqlSessionInputAuthority,
        (current_workspace_id(), endpoint.session_id, identity.input_id),
    )
    if record is None or (
        record.requester,
        record.account_generation,
        record.runner_id,
    ) != (identity.requester, identity.account_generation, endpoint.runner_id):
        _deny("Consumed input no longer belongs to this session and runner")


def _permission(
    session: Session, user: str, endpoint: Endpoint, level: int, *, owner: bool = False
) -> bool:
    access = resolve_access_in_session(session, user, endpoint.permission_id, lock=True)
    return access.user_grant_level == LEVEL_OWNER if owner else resolved_allows(access, level)


def _page_limit(limit: int) -> None:
    if not 1 <= limit <= 100:
        raise OmnigentError("Page size must be between 1 and 100", code=ErrorCode.INVALID_INPUT)


class SessionLinkStore:
    """Bounded authorization transactions; message bodies use the artifact store."""

    def __init__(self, storage_location: str) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.session_link_store", immediate=True
        )

    def invalidate_endpoint(self, session_id: str) -> None:
        with self._session("invalidate_endpoint_approvals") as session:
            invalidate_link_epoch(session, session_id)

    def accepted_inputs(self, request_ids: list[str]) -> set[str]:
        """Only acknowledged inputs may return as instructions after a restart."""
        if not request_ids:
            return set()
        with self._session("read_accepted_history_inputs") as session:
            return set(
                session.scalars(
                    select(SqlLinkedRequest.id).where(
                        SqlLinkedRequest.workspace_id == current_workspace_id(),
                        # Authorized forks keep the original immutable receipt.
                        SqlLinkedRequest.id.in_(request_ids),
                        SqlLinkedRequest.delivery_state == DeliveryState.ACCEPTED,
                    )
                )
            )

    def capture_input(
        self,
        endpoint: Endpoint,
        requester: str,
        generation: str | None,
        *,
        parent_request_id: str | None = None,
        input_id: str | None = None,
    ) -> str:
        if not requester or len(requester) > 128:
            _deny("Input has no verified requester")
        if not endpoint.runner_id or len(endpoint.runner_id) > 64:
            _deny("Input has no bound runner")
        identifier = input_id or uuid.uuid4().hex
        with self._session("capture_input") as session:
            require_active_account(session, requester, generation=generation)
            if not _permission(session, requester, endpoint, LEVEL_EDIT):
                _deny()
            _current_binding(session, endpoint)
            existing = session.get(
                SqlSessionInputAuthority, (current_workspace_id(), endpoint.session_id, identifier)
            )
            if existing is not None:
                if (
                    existing.requester,
                    existing.account_generation,
                    existing.parent_request_id,
                    existing.runner_id,
                ) != (requester, generation, parent_request_id, endpoint.runner_id):
                    _deny("Input authority cannot be replaced")
                return identifier
            session.add(
                SqlSessionInputAuthority(
                    workspace_id=current_workspace_id(),
                    session_id=endpoint.session_id,
                    id=identifier,
                    requester=requester,
                    account_generation=generation,
                    runner_id=endpoint.runner_id,
                    parent_request_id=parent_request_id,
                    captured_at=int(time.time()),
                    request_count=0,
                )
            )
        return identifier

    def resolve_inputs(self, endpoint: Endpoint, authority: RequestAuthority) -> InputIdentity:
        if (
            not authority.verified
            or not authority.input_ids
            or len(authority.input_ids) > MAX_AUTHORITY_INPUTS
        ):
            _deny("No verified consumed input; send a new message from the app")
        with self._session("resolve_consumed_inputs") as session:
            records: list[SqlSessionInputAuthority] = []
            for input_id in authority.input_ids:
                record = session.get(
                    SqlSessionInputAuthority,
                    (current_workspace_id(), endpoint.session_id, input_id),
                )
                if record is None or record.runner_id != endpoint.runner_id:
                    _deny("Input does not belong to this session and runner")
                records.append(record)
            identities = {(record.requester, record.account_generation) for record in records}
            if len(identities) != 1:
                _deny("Inputs from different people were consumed together; start a new turn")
            requester, generation = identities.pop()
            require_active_account(session, requester, generation=generation)
            if not _permission(session, requester, endpoint, LEVEL_EDIT):
                _deny()
            _current_binding(session, endpoint)
            parents = tuple(
                sorted(
                    {record.parent_request_id for record in records if record.parent_request_id}
                )
            )
            for parent_id in parents:
                parent = session.get(
                    SqlLinkedRequest, (current_workspace_id(), parent_id), with_for_update=True
                )
                if (
                    parent is not None
                    and parent.target_id == endpoint.session_id
                    and parent.accepted_runner_id == endpoint.runner_id
                    and parent.delivery_state in (DeliveryState.CLAIMED, DeliveryState.UNCERTAIN)
                ):
                    # A consumed-input receipt also settles an acknowledgement
                    # lost in transit or still in flight.
                    parent.delivery_state = DeliveryState.ACCEPTED
                    parent.accepted_at = int(time.time())
            return InputIdentity(records[-1].id, requester, generation, parents)

    @staticmethod
    def _epochs(
        session: Session, source: Endpoint, target: Endpoint, *, create: bool = False
    ) -> tuple[str | None, str | None]:
        generations: dict[str, str] = {}
        for endpoint in sorted((source, target), key=lambda item: item.session_id):
            _current_binding(session, endpoint)
        for identifier in sorted(
            {source.session_id, source.permission_id, target.session_id, target.permission_id}
        ):
            row = session.get(
                SqlSessionLinkEpoch, (current_workspace_id(), identifier), with_for_update=True
            )
            if row is None and create:
                row = SqlSessionLinkEpoch(
                    workspace_id=current_workspace_id(),
                    session_id=identifier,
                    generation=uuid.uuid4().hex,
                    blocked=False,
                )
                session.add(row)
            if row is not None:
                if row.blocked:
                    _deny("An endpoint is archived or unavailable")
                generations[identifier] = row.generation

        def generation(endpoint: Endpoint) -> str | None:
            if endpoint.session_id not in generations or endpoint.permission_id not in generations:
                return None
            return uuid.uuid5(
                uuid.UUID(generations[endpoint.session_id]), generations[endpoint.permission_id]
            ).hex

        return generation(source), generation(target)

    def connect(
        self, source: Endpoint, target: Endpoint, user: str, generation: str | None
    ) -> dict[str, Any]:
        if source.session_id == target.session_id:
            _deny("A session cannot link to itself")
        with self._session("approve_direction") as session:
            require_active_account(session, user, generation=generation)
            for endpoint in sorted((source, target), key=lambda item: item.permission_id):
                if not _permission(session, user, endpoint, LEVEL_OWNER, owner=True):
                    _deny("Creating a link requires ownership of both sessions")
            source_generation, target_generation = self._epochs(
                session, source, target, create=True
            )
            assert source_generation is not None and target_generation is not None
            link = session.get(
                SqlSessionLink,
                (current_workspace_id(), source.session_id, target.session_id),
                with_for_update=True,
            )
            if link is None:
                link = SqlSessionLink(
                    workspace_id=current_workspace_id(),
                    source_id=source.session_id,
                    target_id=target.session_id,
                )
                session.add(link)
            if (
                link.generation is None
                or link.revoked_at is not None
                or link.source_generation != source_generation
                or link.target_generation != target_generation
                or link.approved_by != user
                or link.approver_generation != generation
            ):
                link.generation = uuid.uuid4().hex
                link.source_generation = source_generation
                link.target_generation = target_generation
                link.approved_by = user
                link.approver_generation = generation
                link.approved_at = int(time.time())
                link.revoked_at = None
            return self._link_dict(link)

    def disconnect(
        self, source: Endpoint, target: Endpoint, user: str, generation: str | None
    ) -> str | None:
        with self._session("revoke_direction") as session:
            require_active_account(session, user, generation=generation)
            ownership = [
                _permission(session, user, endpoint, LEVEL_OWNER, owner=True)
                for endpoint in sorted((source, target), key=lambda item: item.permission_id)
            ]
            if not any(ownership):
                _deny("Disconnecting requires current ownership of either session")
            link = session.get(
                SqlSessionLink,
                (current_workspace_id(), source.session_id, target.session_id),
                with_for_update=True,
            )
            if link is not None and link.revoked_at is None:
                link.revoked_at = int(time.time())
            return link.generation if link else None

    def handoff_deadline(self, source_id: str, target_id: str, generation: str) -> int | None:
        """Latest outstanding lease; revocation waits until no new input can arrive."""
        with self._session("await_revoked_handoffs") as session:
            deadlines = []
            for state in (DeliveryState.CLAIMED, DeliveryState.UNCERTAIN):
                deadline = session.scalar(
                    select(SqlLinkedRequest.expires_at)
                    .where(
                        SqlLinkedRequest.workspace_id == current_workspace_id(),
                        SqlLinkedRequest.source_id == source_id,
                        SqlLinkedRequest.target_id == target_id,
                        SqlLinkedRequest.link_generation == generation,
                        SqlLinkedRequest.delivery_state == state,
                        SqlLinkedRequest.expires_at > time.time(),
                    )
                    .order_by(SqlLinkedRequest.expires_at.desc(), SqlLinkedRequest.id.desc())
                    .limit(1)
                )
                if deadline is not None:
                    deadlines.append(deadline)
            return max(deadlines, default=None)

    def affected_handoff_deadline(
        self, *, session_id: str | None = None, user_id: str | None = None
    ) -> int | None:
        """Bounded indexed probes for deliveries affected by an authority change."""
        scopes = []
        if session_id is not None:
            scopes.extend(
                (column, session_id)
                for column in (
                    SqlLinkedRequest.source_id,
                    SqlLinkedRequest.target_id,
                    SqlLinkedRequest.source_permission_id,
                    SqlLinkedRequest.target_permission_id,
                )
            )
        if user_id is not None:
            scopes.extend(
                (column, user_id)
                for column in (SqlLinkedRequest.requester, SqlLinkedRequest.approved_by)
            )
        deadlines = []
        with self._session("await_authority_handoffs") as session:
            for column, value in scopes:
                for state in (DeliveryState.CLAIMED, DeliveryState.UNCERTAIN):
                    deadline = session.scalar(
                        select(SqlLinkedRequest.expires_at)
                        .where(
                            SqlLinkedRequest.workspace_id == current_workspace_id(),
                            column == value,
                            SqlLinkedRequest.delivery_state == state,
                            SqlLinkedRequest.expires_at > time.time(),
                        )
                        .order_by(SqlLinkedRequest.expires_at.desc(), SqlLinkedRequest.id.desc())
                        .limit(1)
                    )
                    if deadline is not None:
                        deadlines.append(deadline)
        return max(deadlines, default=None)

    def wait_for_handoffs(
        self, *, session_id: str | None = None, user_id: str | None = None
    ) -> None:
        """After committing revocation, drain existing leases without holding locks."""
        deadline = self.affected_handoff_deadline(session_id=session_id, user_id=user_id)
        if deadline is None:
            return
        stop_at = time.monotonic() + max(0, deadline - time.time())
        while time.monotonic() < stop_at:
            time.sleep(min(0.05, max(0, stop_at - time.monotonic())))
            if self.affected_handoff_deadline(session_id=session_id, user_id=user_id) is None:
                return

    def list_links(
        self, session_id: str, *, incoming: bool = False, after: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        _page_limit(limit)
        endpoint = SqlSessionLink.target_id if incoming else SqlSessionLink.source_id
        peer = SqlSessionLink.source_id if incoming else SqlSessionLink.target_id
        with self._session("list_directions") as session:
            query = select(SqlSessionLink).where(
                SqlSessionLink.workspace_id == current_workspace_id(), endpoint == session_id
            )
            if after is not None:
                query = query.where(peer > after)
            rows = list(session.scalars(query.order_by(peer).limit(limit + 1)))
            page = rows[:limit]
            cursor = (
                (page[-1].source_id if incoming else page[-1].target_id)
                if len(rows) > limit
                else None
            )
            return [self._link_dict(row) for row in page], cursor

    @staticmethod
    def _link_dict(link: SqlSessionLink) -> dict[str, Any]:
        return {
            key: getattr(link, key)
            for key in (
                "source_id",
                "target_id",
                "generation",
                "approved_by",
                "approver_generation",
                "approved_at",
                "revoked_at",
            )
        }

    def _active_link(
        self,
        session: Session,
        source: Endpoint,
        target: Endpoint,
        identity: InputIdentity,
        generation: str | None = None,
    ) -> SqlSessionLink:
        key = (current_workspace_id(), source.session_id, target.session_id)
        observed = session.get(SqlSessionLink, key)
        if observed is None:
            _deny("This direction has no active approved link")
        approver, approver_generation = observed.approved_by, observed.approver_generation
        require_active_account(
            session,
            identity.requester,
            generation=identity.account_generation,
            related_accounts={approver: approver_generation},
        )
        # Account -> grants -> endpoint epochs -> link -> request is the write order.
        for user in sorted({identity.requester, approver}):
            for endpoint in sorted((source, target), key=lambda item: item.permission_id):
                if user == identity.requester and not _permission(
                    session, user, endpoint, LEVEL_EDIT
                ):
                    _deny("The requester needs their own edit access to both sessions")
                if user == approver and not _permission(
                    session, user, endpoint, LEVEL_OWNER, owner=True
                ):
                    _deny("The link approver no longer owns both sessions; reconnect")
        source_generation, target_generation = self._epochs(session, source, target)
        link = session.get(SqlSessionLink, key, with_for_update=True, populate_existing=True)
        if (
            link is None
            or link.revoked_at is not None
            or link.approved_by != approver
            or link.approver_generation != approver_generation
            or (generation is not None and link.generation != generation)
            or link.source_generation != source_generation
            or link.target_generation != target_generation
        ):
            _deny("This direction has no active approved link; reconnect the sessions")
        return link

    def check_link(self, source: Endpoint, target: Endpoint, identity: InputIdentity) -> None:
        with self._session("authorize_direction") as session:
            self._active_link(session, source, target, identity)

    def create_request(
        self,
        source: Endpoint,
        target: Endpoint,
        identity: InputIdentity,
        operation_id: str,
        message_hash: bytes,
    ) -> tuple[dict[str, Any], bool]:
        operation_hash = operation_digest(operation_id)
        request_id = receipt_id(source.session_id, operation_id)
        with self._session("retain_request") as session:
            link = self._active_link(session, source, target, identity)
            _current_input(session, source, identity)
            previous = session.get(SqlLinkedRequest, (current_workspace_id(), request_id))
            if previous is not None:
                if (
                    previous.target_id,
                    previous.payload_hash,
                    previous.requester,
                    previous.input_id,
                ) != (target.session_id, message_hash, identity.requester, identity.input_id):
                    raise OmnigentError(
                        "Operation id was already used for different work", code=ErrorCode.CONFLICT
                    )
                return self._request_dict(previous), False
            parent_id, root_id, depth = None, request_id, 1
            root_source, root_input = source.session_id, identity.input_id
            parents: list[SqlLinkedRequest] = []
            for parent_id in identity.parent_request_ids:
                parent = session.get(SqlLinkedRequest, (current_workspace_id(), parent_id))
                if (
                    parent is None
                    or parent.requester != identity.requester
                    or parent.account_generation != identity.account_generation
                    or parent.delivery_state != DeliveryState.ACCEPTED
                ):
                    _deny("Invalid delegation ancestry")
                parents.append(parent)
            if parents:
                if len({parent.root_request_id for parent in parents}) != 1:
                    _deny(
                        "Several independent requests were consumed; "
                        "start a separate turn to delegate"
                    )
                deepest = max(parents, key=lambda parent: (parent.depth, parent.id))
                parent_id, root_id, depth = deepest.id, deepest.root_request_id, deepest.depth + 1
                root = session.get(SqlLinkedRequest, (current_workspace_id(), root_id))
                if root is None:
                    _deny("Invalid delegation root")
                root_source, root_input = root.source_id, root.input_id
            if depth > MAX_DELEGATION_DEPTH:
                _deny("This delegation has reached its depth limit")
            budget = session.get(
                SqlSessionInputAuthority,
                (current_workspace_id(), root_source, root_input),
                with_for_update=True,
                populate_existing=True,
            )
            if (
                budget is None
                or budget.requester != identity.requester
                or budget.account_generation != identity.account_generation
                or budget.request_count >= MAX_REQUESTS_PER_ROOT
            ):
                _deny("This input has reached its delegation limit")
            budget.request_count += 1
            now = int(time.time())
            row = SqlLinkedRequest(
                workspace_id=current_workspace_id(),
                id=request_id,
                source_id=source.session_id,
                target_id=target.session_id,
                source_permission_id=source.permission_id,
                target_permission_id=target.permission_id,
                link_generation=link.generation,
                approved_by=link.approved_by,
                requester=identity.requester,
                account_generation=identity.account_generation,
                input_id=identity.input_id,
                parent_request_id=parent_id,
                root_request_id=root_id,
                depth=depth,
                reply_count=0,
                operation_hash=operation_hash,
                payload_hash=message_hash,
                created_at=now,
                expires_at=now + REQUEST_TTL_SECONDS,
                delivery_state=DeliveryState.PENDING,
                accepted_runner_id=None,
                accepted_at=None,
            )
            session.add(row)
            return self._request_dict(row), True

    @staticmethod
    def _request_dict(row: SqlLinkedRequest) -> dict[str, Any]:
        result = {
            key: getattr(row, key)
            for key in (
                "id",
                "source_id",
                "target_id",
                "link_generation",
                "requester",
                "account_generation",
                "input_id",
                "parent_request_id",
                "root_request_id",
                "depth",
                "created_at",
                "expires_at",
                "accepted_runner_id",
                "accepted_at",
            )
        }
        result["delivery_state"] = DeliveryState(row.delivery_state).name.lower()
        result["payload_key"] = payload_key("requests", row.id, row.payload_hash)
        return result

    @staticmethod
    def _expire_handoff(row: SqlLinkedRequest) -> None:
        if row.expires_at <= time.time():
            if row.delivery_state == DeliveryState.CLAIMED:
                row.delivery_state = DeliveryState.UNCERTAIN
            elif row.delivery_state in (DeliveryState.PENDING, DeliveryState.HANDOFF):
                row.delivery_state = DeliveryState.REJECTED

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self._session("get_request_receipt") as session:
            row = session.get(
                SqlLinkedRequest, (current_workspace_id(), request_id), with_for_update=True
            )
            if row is not None:
                self._expire_handoff(row)
            return self._request_dict(row) if row else None

    def begin_handoff(self, request_id: str) -> bool:
        with self._session("begin_request_handoff") as session:
            row = session.get(
                SqlLinkedRequest, (current_workspace_id(), request_id), with_for_update=True
            )
            if row is None or row.delivery_state != DeliveryState.PENDING:
                return False
            row.delivery_state = (
                DeliveryState.REJECTED if row.expires_at <= time.time() else DeliveryState.HANDOFF
            )
            return row.delivery_state == DeliveryState.HANDOFF

    def settle_handoff(self, request_id: str) -> None:
        with self._session("settle_request_handoff") as session:
            row = session.get(
                SqlLinkedRequest, (current_workspace_id(), request_id), with_for_update=True
            )
            if row and row.delivery_state == DeliveryState.HANDOFF:
                # The payload cannot leave the server without a successful claim.
                row.delivery_state = DeliveryState.REJECTED

    def accept_request(self, request_id: str, source: Endpoint, target: Endpoint) -> str | None:
        """Claim once immediately before queueing in the target's normal input path."""
        with self._session("accept_request_input") as session:
            row = session.get(SqlLinkedRequest, (current_workspace_id(), request_id))
            if (
                row is None
                or row.source_id != source.session_id
                or row.target_id != target.session_id
            ):
                _deny()
            identity = InputIdentity(row.input_id, row.requester, row.account_generation)
            self._active_link(session, source, target, identity, row.link_generation)
            _current_input(session, source, identity)
            row = session.get(
                SqlLinkedRequest,
                (current_workspace_id(), request_id),
                with_for_update=True,
                populate_existing=True,
            )
            if (
                row is None
                or not target.runner_id
                or row.delivery_state != DeliveryState.HANDOFF
                or row.expires_at <= time.time()
            ):
                return None
            # The immutable input and receipt commit together. A lost acknowledgement
            # never permits another enqueue, even after a runner restart.
            input_id = uuid.uuid4().hex
            session.add(
                SqlSessionInputAuthority(
                    workspace_id=current_workspace_id(),
                    session_id=target.session_id,
                    id=input_id,
                    requester=row.requester,
                    account_generation=row.account_generation,
                    runner_id=target.runner_id,
                    parent_request_id=row.id,
                    captured_at=int(time.time()),
                    request_count=0,
                )
            )
            row.delivery_state = DeliveryState.CLAIMED
            row.accepted_runner_id = target.runner_id
            # The worker's monotonic deadline starts before this RPC reaches us.
            row.expires_at = min(row.expires_at, int(time.time()) + DELIVERY_LEASE_SECONDS + 1)
            return input_id

    def confirm_handoff(self, request_id: str, *, accepted: bool) -> None:
        with self._session("confirm_worker_input") as session:
            row = session.get(
                SqlLinkedRequest, (current_workspace_id(), request_id), with_for_update=True
            )
            if row is not None and row.delivery_state == DeliveryState.CLAIMED:
                row.delivery_state = (
                    DeliveryState.ACCEPTED if accepted else DeliveryState.UNCERTAIN
                )
                if accepted:
                    row.accepted_at = int(time.time())

    def publish_reply(
        self,
        request_id: str,
        source: Endpoint,
        target: Endpoint,
        identity: InputIdentity,
        operation_id: str,
        message_hash: bytes,
        publish: Callable[[Session], None],
    ) -> dict[str, Any]:
        reply_id = receipt_id(request_id, operation_id)
        with self._session("publish_addressed_reply") as session:
            row = session.get(SqlLinkedRequest, (current_workspace_id(), request_id))
            if (
                row is None
                or row.source_id != source.session_id
                or row.target_id != target.session_id
                or row.requester != identity.requester
                or row.account_generation != identity.account_generation
                or row.delivery_state != DeliveryState.ACCEPTED
            ):
                _deny("Reply does not belong to this worker and requester")
            self._active_link(session, source, target, identity, row.link_generation)
            _current_input(session, target, identity)
            row = session.get(
                SqlLinkedRequest,
                (current_workspace_id(), request_id),
                with_for_update=True,
                populate_existing=True,
            )
            assert row is not None
            previous = session.get(SqlLinkedReply, (current_workspace_id(), request_id, reply_id))
            if previous is not None:
                if previous.payload_hash != message_hash:
                    raise OmnigentError("Reply operation id was reused", code=ErrorCode.CONFLICT)
                return {"reply_id": previous.id, "request_id": request_id, "published": True}
            # Same database: append joins this transaction. Split databases: stable
            # item IDs deduplicate a conversation commit followed by metadata failure.
            publish(session)
            row.reply_count += 1
            session.add(
                SqlLinkedReply(
                    workspace_id=current_workspace_id(),
                    request_id=request_id,
                    id=reply_id,
                    operation_hash=operation_digest(operation_id),
                    payload_hash=message_hash,
                    sequence=row.reply_count,
                    created_at=int(time.time()),
                    published_at=int(time.time()),
                )
            )
            return {"reply_id": reply_id, "request_id": request_id, "published": True}

    def list_replies(
        self, request_id: str, *, after: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], str | None]:
        _page_limit(limit)
        if after is not None and (not after.isdecimal() or len(after) > 18):
            raise OmnigentError("Invalid reply cursor", code=ErrorCode.INVALID_INPUT)
        with self._session("read_addressed_replies") as session:
            query = select(SqlLinkedReply).where(
                SqlLinkedReply.workspace_id == current_workspace_id(),
                SqlLinkedReply.request_id == request_id,
            )
            if after:
                query = query.where(SqlLinkedReply.sequence > int(after))
            rows = list(session.scalars(query.order_by(SqlLinkedReply.sequence).limit(limit + 1)))
            return (
                [
                    {
                        "id": row.id,
                        "created_at": row.created_at,
                        "payload_key": payload_key("replies", row.id, row.payload_hash),
                    }
                    for row in rows[:limit]
                ],
                str(rows[limit - 1].sequence) if len(rows) > limit else None,
            )

    def check_request(
        self, request_id: str, source: Endpoint, target: Endpoint, identity: InputIdentity
    ) -> dict[str, Any]:
        with self._session("authorize_request_poll") as session:
            row = session.get(SqlLinkedRequest, (current_workspace_id(), request_id))
            if (
                row is None
                or row.source_id != source.session_id
                or row.target_id != target.session_id
                or row.requester != identity.requester
                or row.account_generation != identity.account_generation
            ):
                _deny()
            self._active_link(session, source, target, identity, row.link_generation)
            _current_input(session, source, identity)
            row = session.get(
                SqlLinkedRequest,
                (current_workspace_id(), request_id),
                with_for_update=True,
                populate_existing=True,
            )
            assert row is not None
            self._expire_handoff(row)
            return self._request_dict(row)
