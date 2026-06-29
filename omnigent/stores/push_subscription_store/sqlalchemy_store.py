"""SQLAlchemy-backed push subscription store."""

from __future__ import annotations

from sqlalchemy import select

from omnigent.db.db_models import SqlPushSubscription
from omnigent.db.utils import (
    get_or_create_engine,
    make_managed_session_maker,
    now_epoch,
)
from omnigent.entities.push_subscription import PushSubscription
from omnigent.stores.push_subscription_store import PushSubscriptionStore


def _to_entity(row: SqlPushSubscription) -> PushSubscription:
    """Convert a :class:`SqlPushSubscription` ORM row to an entity."""
    return PushSubscription(
        id=row.id,
        user_id=row.user_id,
        endpoint=row.endpoint,
        p256dh=row.p256dh,
        auth=row.auth,
        created_at=row.created_at,
    )


class SqlAlchemyPushSubscriptionStore(PushSubscriptionStore):
    """SQLAlchemy-backed implementation of :class:`PushSubscriptionStore`."""

    def __init__(self, storage_location: str) -> None:
        """Create or reuse a SQLAlchemy engine + session factory."""
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        # immediate=True: upsert is a check-then-write, so take the SQLite
        # write lock up front to avoid a concurrent-create race on the endpoint.
        self._session = make_managed_session_maker(self._engine, immediate=True)

    def list_for_user(self, user_id: str) -> list[PushSubscription]:
        """Return all of the user's subscriptions."""
        with self._session() as session:
            rows = (
                session.execute(
                    select(SqlPushSubscription).where(SqlPushSubscription.user_id == user_id)
                )
                .scalars()
                .all()
            )
            return [_to_entity(r) for r in rows]

    def upsert(
        self,
        subscription_id: str,
        user_id: str,
        endpoint: str,
        p256dh: str,
        auth: str,
    ) -> PushSubscription:
        """Register a subscription, keyed by endpoint (refresh keys on repeat)."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlPushSubscription).where(SqlPushSubscription.endpoint == endpoint)
                )
                .scalars()
                .first()
            )
            if row is None:
                row = SqlPushSubscription(
                    id=subscription_id,
                    user_id=user_id,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth,
                    created_at=now_epoch(),
                )
                session.add(row)
            else:
                # Same browser re-subscribing: refresh owner + rotated keys.
                row.user_id = user_id
                row.p256dh = p256dh
                row.auth = auth
            session.flush()
            return _to_entity(row)

    def delete_by_endpoint(self, endpoint: str) -> bool:
        """Delete a subscription by endpoint. Idempotent."""
        with self._session() as session:
            row = (
                session.execute(
                    select(SqlPushSubscription).where(SqlPushSubscription.endpoint == endpoint)
                )
                .scalars()
                .first()
            )
            if row is None:
                return False
            session.delete(row)
            return True
