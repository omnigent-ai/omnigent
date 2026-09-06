"""Owner-scoped library storage, separate from executable runtime Agent rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from omnigent.db.db_models import SqlCustomAgent, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.errors import ErrorCode, OmnigentError


def _row_data(row: SqlCustomAgent) -> dict[str, Any]:
    return {
        key: getattr(row, key)
        for key in (
            "id",
            "name",
            "description",
            "harness",
            "model",
            "bundle_location",
            "version",
            "created_at",
            "updated_at",
        )
    }


class CustomAgentsStore:
    """Every operation requires both workspace and owner identity."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._read_session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.custom_agents_store"
        )
        self._write_session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.custom_agents_store", immediate=True
        )

    def _query(self, owner_id: str):
        return select(SqlCustomAgent).where(
            SqlCustomAgent.workspace_id == current_workspace_id(),
            SqlCustomAgent.owner_id == owner_id,
            SqlCustomAgent.deleted_at.is_(None),
        )

    def list(self, owner_id: str, limit: int, offset: int) -> list[dict[str, Any]]:
        with self._read_session("list") as session:
            rows = session.scalars(
                self._query(owner_id)
                .order_by(SqlCustomAgent.created_at, SqlCustomAgent.id)
                .limit(limit)
                .offset(offset)
            )
            return [_row_data(row) for row in rows]

    def get(self, owner_id: str, agent_id: str) -> dict[str, Any]:
        with self._read_session("get") as session:
            row = session.scalar(self._query(owner_id).where(SqlCustomAgent.id == agent_id))
            if row is None:
                raise OmnigentError("Custom Agent not found", code=ErrorCode.NOT_FOUND)
            return _row_data(row)

    def create(self, owner_id: str, data: dict[str, Any]) -> dict[str, Any]:
        with self._write_session("create") as session:
            now = now_epoch()
            row = SqlCustomAgent(
                owner_id=owner_id, created_at=now, updated_at=now, version=1, **data
            )
            session.add(row)
            return _row_data(row)

    def update(
        self, owner_id: str, agent_id: str, version: int, data: dict[str, Any]
    ) -> dict[str, Any]:
        with self._write_session("update") as session:
            row = session.scalar(
                self._query(owner_id).where(SqlCustomAgent.id == agent_id).with_for_update()
            )
            if row is None:
                raise OmnigentError("Custom Agent not found", code=ErrorCode.NOT_FOUND)
            if row.version != version:
                raise OmnigentError(
                    "Custom Agent changed; reload before saving", code=ErrorCode.CONFLICT
                )
            for key, value in data.items():
                setattr(row, key, value)
            row.version += 1
            row.updated_at = now_epoch()
            return _row_data(row)

    def delete(self, owner_id: str, agent_id: str) -> None:
        with self._write_session("delete") as session:
            row = session.scalar(
                self._query(owner_id).where(SqlCustomAgent.id == agent_id).with_for_update()
            )
            if row is None:
                raise OmnigentError("Custom Agent not found", code=ErrorCode.NOT_FOUND)
            row.deleted_at = now_epoch()
