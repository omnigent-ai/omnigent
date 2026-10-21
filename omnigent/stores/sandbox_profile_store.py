"""Bounded SQL persistence for sandbox profiles and accepted launch snapshots."""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import Engine, select

from omnigent.db.db_models import (
    SqlSandboxProfile,
    SqlSandboxProfileBinding,
    current_workspace_id,
    uuid_to_bytes,
)
from omnigent.db.utils import make_named_managed_session_maker

PROFILE_ID_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
MAX_PROFILE_BYTES = 16383


def encode_snapshot(value: dict[str, Any]) -> bytes:
    """Bound the serialized bytes, including all metadata and JSON overhead."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > MAX_PROFILE_BYTES:
        raise ValueError("Sandbox profile must be smaller than 16 KB")
    return payload


def _profile(row: SqlSandboxProfile) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "provider": row.provider,
        "config": json.loads(row.config_json),
        "revision": row.revision,
        "enabled": row.enabled,
    }


class SandboxProfileStore:
    """Reuse the host store's engine and workspace context for profile data."""

    def __init__(self, engine: Engine) -> None:
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.sandbox_profile_store", immediate=True
        )

    def list_profiles(
        self, *, include_disabled: bool = False, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= limit <= 100:
            raise ValueError("Profile page size must be between 1 and 100")
        query = select(SqlSandboxProfile).where(
            SqlSandboxProfile.workspace_id == current_workspace_id()
        )
        if not include_disabled:
            query = query.where(SqlSandboxProfile.enabled.is_(True))
        if cursor is not None:
            query = query.where(SqlSandboxProfile.id > cursor)
        with self._session("list_available_profiles") as session:
            rows = session.scalars(query.order_by(SqlSandboxProfile.id).limit(limit + 1)).all()
            return [_profile(row) for row in rows[:limit]], (
                rows[limit - 1].id if len(rows) > limit else None
            )

    def get(self, profile_id: str) -> dict[str, Any] | None:
        with self._session("read_profile") as session:
            row = session.get(SqlSandboxProfile, (current_workspace_id(), profile_id))
            return _profile(row) if row is not None else None

    def put(self, profile: dict[str, Any]) -> dict[str, Any]:
        encode_snapshot(profile)
        for key, maximum in (
            ("name", 100),
            ("description", 500),
            ("provider", 32),
            ("revision", 64),
        ):
            if not isinstance(profile[key], str) or len(profile[key]) > maximum:
                raise ValueError(f"Invalid sandbox profile {key}")
        if not re.fullmatch(PROFILE_ID_PATTERN, profile["id"]):
            raise ValueError("Invalid sandbox profile ID")
        values = {
            key: profile[key] for key in ("name", "description", "provider", "revision", "enabled")
        }
        values["config_json"] = encode_snapshot(profile["config"])
        with self._session("save_profile") as session:
            row = session.get(SqlSandboxProfile, (current_workspace_id(), profile["id"]))
            if row is None:
                row = SqlSandboxProfile(
                    workspace_id=current_workspace_id(), id=profile["id"], **values
                )
                session.add(row)
            else:
                for key, value in values.items():
                    setattr(row, key, value)
            return _profile(row)

    def bind_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        encoded = encode_snapshot(snapshot)
        with self._session("accept_session_profile") as session:
            row = session.get(SqlSandboxProfileBinding, (current_workspace_id(), session_id))
            if row is not None:
                if row.snapshot_json != encoded:
                    raise ValueError("A session's sandbox profile cannot be changed")
                return
            session.add(
                SqlSandboxProfileBinding(
                    workspace_id=current_workspace_id(),
                    session_id=session_id,
                    host_id=None,
                    snapshot_json=encoded,
                )
            )

    def get_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self._session("read_session_profile") as session:
            row = session.get(SqlSandboxProfileBinding, (current_workspace_id(), session_id))
            return json.loads(row.snapshot_json) if row is not None else None

    def bind_host(self, session_id: str, host_id: str) -> None:
        host_id = uuid_to_bytes(host_id).hex()
        with self._session("record_profile_host") as session:
            row = session.get(
                SqlSandboxProfileBinding,
                (current_workspace_id(), session_id),
                with_for_update=True,
            )
            if row is not None:
                if row.host_id is not None and row.host_id != host_id:
                    raise ValueError("A profiled session cannot be moved to a different host")
                row.host_id = host_id

    def get_session_host_id(self, session_id: str) -> str | None:
        with self._session("read_session_profile_host") as session:
            return session.scalar(
                select(SqlSandboxProfileBinding.host_id).where(
                    SqlSandboxProfileBinding.workspace_id == current_workspace_id(),
                    SqlSandboxProfileBinding.session_id == session_id,
                )
            )

    def get_host_snapshot(self, host_id: str) -> dict[str, Any] | None:
        with self._session("read_host_profile") as session:
            row = session.scalar(
                select(SqlSandboxProfileBinding)
                .where(
                    SqlSandboxProfileBinding.workspace_id == current_workspace_id(),
                    SqlSandboxProfileBinding.host_id == host_id,
                )
                .order_by(SqlSandboxProfileBinding.session_id)
                .limit(1)
            )
            return json.loads(row.snapshot_json) if row is not None else None
