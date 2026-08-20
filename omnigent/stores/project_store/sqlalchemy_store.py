"""SQLAlchemy-backed project store."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlProject, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
)
from omnigent.entities import Project
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.project_store import ProjectStore

# Max serialized length of a project's config blob. The value is persisted
# verbatim and reflected back on every read, so an unbounded blob is a mild
# storage/response-size amplifier on an otherwise cheap CRUD row. 64 KiB is far
# above any realistic set of default-session hints (a few short keys) while
# still capping abuse.
_CONFIG_MAX_SERIALIZED_LEN = 64 * 1024


def _encode_config(config: dict[str, Any] | None) -> str | None:
    """Pack a project's config dict into a compact JSON blob for storage.

    An empty or ``None`` config stores SQL ``NULL`` rather than ``"{}"``, so
    "no defaults" is one canonical representation.

    :param config: The config object, or ``None``.
    :returns: Compact JSON object string, or ``None`` when empty.
    :raises OmnigentError: ``INVALID_INPUT`` if the serialized config exceeds
        :data:`_CONFIG_MAX_SERIALIZED_LEN`.
    """
    if not config:
        return None
    blob = json.dumps(config, separators=(",", ":"))
    if len(blob) > _CONFIG_MAX_SERIALIZED_LEN:
        raise OmnigentError(
            f"project config too large ({len(blob)} bytes; max {_CONFIG_MAX_SERIALIZED_LEN})",
            code=ErrorCode.INVALID_INPUT,
        )
    return blob


def _decode_config(raw: str | None) -> dict[str, Any]:
    """Unpack the stored ``config`` blob to a dict (``{}`` when unset).

    Defensive against a non-object blob: the encode path only ever writes JSON
    objects, but a future writer or a manual DB edit could store a scalar/array,
    which would otherwise flow back as a non-dict. Coerce anything that isn't a
    dict to ``{}`` so callers can always treat config as a mapping.

    :param raw: The stored JSON blob, or ``None``.
    :returns: The decoded object, or an empty dict when ``NULL`` / empty / non-object.
    """
    if not raw:
        return {}
    decoded = json.loads(raw)
    return decoded if isinstance(decoded, dict) else {}


def _to_entity(row: SqlProject) -> Project:
    """
    Convert a :class:`SqlProject` ORM row to a :class:`Project`.

    :param row: The SQLAlchemy ORM row to convert.
    :returns: A :class:`Project` dataclass instance.
    """
    return Project(
        id=row.id,
        name=row.name,
        user_id=row.user_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
        config=_decode_config(row.config),
    )


class SqlAlchemyProjectStore(ProjectStore):
    """
    SQLAlchemy-backed implementation of :class:`ProjectStore`.

    Persists projects in a relational database via the SQLAlchemy ORM. Every
    query is scoped by ``workspace_id`` (tenant partition) and ``user_id``
    (projects are owner-private).
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the SQLAlchemy project store.

        Creates or reuses a SQLAlchemy engine and session factory for the given
        database URI.

        :param storage_location: SQLAlchemy database URI,
            e.g. ``"sqlite:///chat.db"``.
        """
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
        )

    def _name_taken(
        self,
        session: Session,
        *,
        user_id: str | None,
        name: str,
        exclude_id: str | None,
    ) -> bool:
        """Return whether ``user_id`` already has a project named ``name``.

        The sole enforcement point for per-owner name uniqueness: there is no
        unique index behind it (see ``SqlProject.__table_args__``). Being a
        check-then-write, it cannot close the concurrency window — two
        simultaneous creates or renames to the same name can both land.

        :param session: The active SQLAlchemy session.
        :param user_id: The owner scope.
        :param name: The candidate name.
        :param exclude_id: A project id to exclude (the row being renamed).
        :returns: ``True`` if another of the owner's projects has this name.
        """
        stmt = select(SqlProject.id).where(
            SqlProject.workspace_id == current_workspace_id(),
            SqlProject.user_id == user_id,
            SqlProject.name == name,
        )
        if exclude_id is not None:
            stmt = stmt.where(SqlProject.id != exclude_id)
        return session.execute(stmt).first() is not None

    def create(
        self,
        project_id: str,
        name: str,
        user_id: str | None,
        config: dict[str, Any] | None = None,
    ) -> Project:
        """Insert a new, empty project.

        Rejects a name the owner already uses via the ``_name_taken`` pre-check.
        That check is the only guard — no unique index backs it — so a
        concurrent create of the same name can slip through; see
        ``SqlProject.__table_args__`` for why that is acceptable.
        """
        with self._session("insert_project") as session:
            if self._name_taken(session, user_id=user_id, name=name, exclude_id=None):
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            row = SqlProject(
                id=project_id,
                name=name,
                user_id=user_id,
                created_at=now_epoch(),
                updated_at=None,
                config=_encode_config(config),
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """Return an owned project by id, or ``None`` if not found."""
        with self._session("select_project_by_id") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            return _to_entity(row)

    def list(self, *, user_id: str | None) -> list[Project]:
        """List the owner's projects ordered by ``created_at ASC, id ASC``."""
        with self._session("list_projects") as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.user_id == user_id)
                .order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``. A ``config`` of ``{}`` clears
        the stored defaults (distinct from ``None`` = leave unchanged).

        A rename re-checks ``_name_taken``, which — as on ``create`` — is the
        only uniqueness guard, so concurrent renames to the same name can both
        land.
        """
        with self._session("update_project") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return None
            changed = False
            if name is not None and row.name != name:
                if self._name_taken(session, user_id=user_id, name=name, exclude_id=project_id):
                    raise OmnigentError(
                        f"A project named {name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = name
                changed = True
            if config is not None:
                encoded = _encode_config(config)
                if row.config != encoded:
                    row.config = encoded
                    changed = True
            if changed:
                row.updated_at = now_epoch()
            session.flush()
            return _to_entity(row)

    def delete(self, project_id: str, *, user_id: str | None) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found."""
        with self._session("delete_project") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if row is None or row.user_id != user_id:
                return False
            session.delete(row)
            return True
