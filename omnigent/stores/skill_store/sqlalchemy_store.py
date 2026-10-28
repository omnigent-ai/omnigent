"""SQLAlchemy implementation of :class:`SkillStore`."""

from __future__ import annotations

import builtins
import uuid

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlInstalledSkill, SqlSkillSource, current_workspace_id
from omnigent.db.enum_codecs import decode_installed_skill_scope, encode_installed_skill_scope
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope
from omnigent.stores.skill_store import MAX_LIST, SkillStore


def _source_entity(row: SqlSkillSource) -> SkillSource:
    return SkillSource(
        id=row.id,
        url=row.url,
        ref=row.ref,
        name=row.name,
        created_by=row.created_by,
        created_at=row.created_at,
    )


def _skill_entity(row: SqlInstalledSkill) -> InstalledSkill:
    return InstalledSkill(
        id=row.id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        source_url=row.source_url,
        ref=row.ref,
        commit=row.commit,
        path=row.path,
        scope="org" if decode_installed_skill_scope(row.scope) == "org" else "user",
        owner_user_id=row.owner_user_id,
        bundle_location=row.bundle_location,
        enabled=bool(row.enabled),
        installed_by=row.installed_by,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
    )


class SqlAlchemySkillStore(SkillStore):
    """Skill sources and installed skills in the shared database."""

    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(engine, query_name_prefix="skill_store")
        self._session_immediate = make_named_managed_session_maker(
            engine, query_name_prefix="skill_store", immediate=True
        )

    # ── sources ──────────────────────────────────────────────────────────

    def add_source(
        self, *, url: str, ref: str | None, name: str, created_by: str | None
    ) -> SkillSource:
        def write(session: Session) -> SkillSource:
            existing = session.scalar(
                select(SqlSkillSource).where(
                    SqlSkillSource.workspace_id == current_workspace_id(),
                    SqlSkillSource.url == url,
                    SqlSkillSource.ref.is_(None) if ref is None else SqlSkillSource.ref == ref,
                )
            )
            if existing is not None:
                return _source_entity(existing)
            row = SqlSkillSource(
                id=uuid.uuid4().hex,
                url=url,
                ref=ref,
                name=name,
                created_by=created_by,
                created_at=now_epoch(),
            )
            session.add(row)
            session.flush()
            return _source_entity(row)

        return run_write_transaction(self._session_immediate, "add_skill_source", write)

    def list_sources(self, *, limit: int = MAX_LIST) -> builtins.list[SkillSource]:
        with self._session("list_skill_sources") as session:
            rows = session.scalars(
                select(SqlSkillSource)
                .where(SqlSkillSource.workspace_id == current_workspace_id())
                .order_by(SqlSkillSource.created_at, SqlSkillSource.name)
                .limit(limit)
            ).all()
            return [_source_entity(row) for row in rows]

    def get_source(self, source_id: str) -> SkillSource | None:
        with self._session("get_skill_source") as session:
            row = session.get(SqlSkillSource, (current_workspace_id(), source_id))
            return None if row is None else _source_entity(row)

    def delete_source(self, source_id: str) -> bool:
        def write(session: Session) -> bool:
            row = session.get(SqlSkillSource, (current_workspace_id(), source_id))
            if row is None:
                return False
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_skill_source", write)

    # ── installed skills ─────────────────────────────────────────────────

    def install(
        self,
        *,
        name: str,
        display_name: str | None,
        description: str,
        source_url: str,
        ref: str | None,
        commit: str | None,
        path: str,
        scope: SkillScope,
        owner_user_id: str | None,
        bundle_location: str,
        installed_by: str | None,
    ) -> InstalledSkill:
        owner = owner_user_id if scope == "user" else None

        def write(session: Session) -> InstalledSkill:
            # Reinstalling replaces the row in place so the id stays stable.
            row = session.scalar(
                select(SqlInstalledSkill).where(
                    SqlInstalledSkill.workspace_id == current_workspace_id(),
                    SqlInstalledSkill.name == name,
                    SqlInstalledSkill.scope == encode_installed_skill_scope(scope),
                    SqlInstalledSkill.owner_user_id.is_(None)
                    if owner is None
                    else SqlInstalledSkill.owner_user_id == owner,
                )
            )
            now = now_epoch()
            if row is None:
                row = SqlInstalledSkill(
                    id=uuid.uuid4().hex,
                    name=name,
                    scope=encode_installed_skill_scope(scope),
                    owner_user_id=owner,
                    enabled=True,
                    installed_by=installed_by,
                    installed_at=now,
                )
                session.add(row)
            else:
                row.updated_at = now
            row.display_name = display_name
            row.description = description
            row.source_url = source_url
            row.ref = ref
            row.commit = commit
            row.path = path
            row.bundle_location = bundle_location
            session.flush()
            return _skill_entity(row)

        return run_write_transaction(self._session_immediate, "install_skill", write)

    def get(self, skill_id: str) -> InstalledSkill | None:
        with self._session("get_installed_skill") as session:
            row = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
            return None if row is None else _skill_entity(row)

    def list_for_user(
        self, user_id: str | None, *, limit: int = MAX_LIST
    ) -> builtins.list[InstalledSkill]:
        with self._session("list_installed_skills") as session:
            visible = SqlInstalledSkill.scope == encode_installed_skill_scope("org")
            if user_id is not None:
                visible = or_(visible, SqlInstalledSkill.owner_user_id == user_id)
            else:
                visible = or_(visible, SqlInstalledSkill.owner_user_id.is_(None))
            rows = session.scalars(
                select(SqlInstalledSkill)
                .where(SqlInstalledSkill.workspace_id == current_workspace_id(), visible)
                .order_by(SqlInstalledSkill.installed_at, SqlInstalledSkill.name)
                .limit(limit)
            ).all()
            return [_skill_entity(row) for row in rows]

    def update(
        self,
        skill_id: str,
        *,
        commit: str | None,
        bundle_location: str,
        description: str,
        display_name: str | None,
    ) -> InstalledSkill | None:
        def write(session: Session) -> InstalledSkill | None:
            row = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
            if row is None:
                return None
            row.commit = commit
            row.bundle_location = bundle_location
            row.description = description
            row.display_name = display_name
            row.updated_at = now_epoch()
            session.flush()
            return _skill_entity(row)

        return run_write_transaction(self._session_immediate, "update_installed_skill", write)

    def set_enabled(self, skill_id: str, enabled: bool) -> InstalledSkill | None:
        def write(session: Session) -> InstalledSkill | None:
            row = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
            if row is None:
                return None
            row.enabled = enabled
            row.updated_at = now_epoch()
            session.flush()
            return _skill_entity(row)

        return run_write_transaction(self._session_immediate, "toggle_installed_skill", write)

    def delete(self, skill_id: str) -> InstalledSkill | None:
        def write(session: Session) -> InstalledSkill | None:
            row = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
            if row is None:
                return None
            entity = _skill_entity(row)
            session.delete(row)
            return entity

        return run_write_transaction(self._session_immediate, "delete_installed_skill", write)
