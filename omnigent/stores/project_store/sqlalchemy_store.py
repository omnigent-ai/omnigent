"""SQLAlchemy-backed project store."""

from __future__ import annotations

import builtins
import dataclasses
import json
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import zstandard
from sqlalchemy import LargeBinary, asc, delete, select, tuple_, type_coerce, update
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode, encode
from omnigent.db.db_models import (
    SqlPreference,
    SqlProject,
    SqlProjectIdentity,
    SqlProjectKpiRevision,
    SqlProjectRoleKpiRevision,
    SqlProjectShare,
    SqlProjectTicket,
    SqlProjectTicketEvent,
    SqlProjectTicketLink,
    SqlProjectTicketRead,
    SqlProjectTicketSession,
    SqlProjectTicketStageMove,
    SqlProjectTicketStageMoveBuild,
    current_workspace_id,
)
from omnigent.db.envelope import seal, seal_text
from omnigent.db.preference_rows import upsert_preference_row
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import Project
from omnigent.entities.mandate_sections import (
    apply_proposal,
    proposal_is_stale,
    proposal_target,
    section_spans,
    text_hash,
)
from omnigent.entities.project import (
    MANDATE_PROPOSAL_MAX_BYTES,
    MANDATE_PROPOSAL_MODES,
    MANDATE_PROPOSALS_MAX_PENDING,
    MANDATE_PROPOSALS_MAX_STORED_BYTES,
    PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES,
    PROJECT_IDENTITY_MAX_COUNT,
    PROJECT_IDENTITY_SCOPE_MAX_BYTES,
    PROJECT_MANDATE_MAX_BYTES,
    PROJECT_MANDATE_MAX_STORED_BYTES,
    ROLE_SCOPE_ITEM_MAX_CHARS,
    ROLE_SCOPE_KEYS,
    ROLE_SCOPE_MAX_ITEMS,
    MandateProposal,
    ProjectIdentity,
    ProjectShare,
    normalize_project_description,
)
from omnigent.entities.project_routing import choose_router
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.project_status_cache import invalidate_after_commit
from omnigent.stores.project_store import ProjectOrderPreference, ProjectStore
from omnigent.stores.project_store.access import (
    SHARE_ROLES,
    ProjectRole,
    accessible_project_ids,
    project_role,
)

# Max serialized length of a project's config blob. The value is persisted
# verbatim and reflected back on every read, so an unbounded blob is a mild
# storage/response-size amplifier on an otherwise cheap CRUD row. 64 KiB is far
# above any realistic set of default-session hints (a few short keys) while
# still capping abuse.
_CONFIG_MAX_SERIALIZED_LEN = 64 * 1024
_PROJECT_ORDER_KEY = "project_order"
_PREFERENCE_MAX_STORED_BYTES = 65535


# Server-owned config key: former main sessions, so their messages reach the
# current orchestrator. Hidden from ``Project.config`` and kept across replaces.
RETIRED_MAIN_SESSIONS_CONFIG_KEY = "_omnigent_retired_main_sessions"
_RETIRED_MAIN_SESSIONS_MAX = 50
# Server-owned routing keys: the owner's chosen router persona and whether the
# built-in Lead was removed. Owner-set only, through ``set_routing``.
ROUTER_IDENTITY_CONFIG_KEY = "_omnigent_router_identity"
LEAD_REMOVED_CONFIG_KEY = "_omnigent_lead_removed"
# Project config keys that hold the Lead's session defaults, edited from Team.
LEAD_SESSION_DEFAULT_KEYS = ("agent_id", "model", "effort")
# Server-owned: the owner's charter for the Lead, set only through ``set_lead_charter``.
LEAD_CHARTER_CONFIG_KEY = "_omnigent_lead_charter"
# Server-owned: the project's description for people, set only through ``set_description``.
DESCRIPTION_CONFIG_KEY = "_omnigent_description"
_SERVER_CONFIG_KEYS = frozenset(
    {
        DESCRIPTION_CONFIG_KEY,
        RETIRED_MAIN_SESSIONS_CONFIG_KEY,
        ROUTER_IDENTITY_CONFIG_KEY,
        LEAD_REMOVED_CONFIG_KEY,
        LEAD_CHARTER_CONFIG_KEY,
    }
)


def _client_config(config: dict[str, Any] | None) -> dict[str, Any]:
    """Drop server-owned keys from a client-supplied config."""
    return {k: v for k, v in (config or {}).items() if k not in _SERVER_CONFIG_KEYS}


def _server_config(raw: str | None) -> dict[str, Any]:
    """The server-owned keys of a stored config, kept when a client replaces it."""
    return {k: v for k, v in _decode_config(raw).items() if k in _SERVER_CONFIG_KEYS}


def _retired_ids(raw: str | None) -> list[str]:
    value = _decode_config(raw).get(RETIRED_MAIN_SESSIONS_CONFIG_KEY)
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _with_retired(config: dict[str, Any], retired: list[str]) -> dict[str, Any]:
    config = {k: v for k, v in config.items() if k != RETIRED_MAIN_SESSIONS_CONFIG_KEY}
    if not retired:
        return config
    return {**config, RETIRED_MAIN_SESSIONS_CONFIG_KEY: retired[-_RETIRED_MAIN_SESSIONS_MAX:]}


def _lead_charter(raw: str | None) -> str | None:
    value = _decode_config(raw).get(LEAD_CHARTER_CONFIG_KEY)
    return value if isinstance(value, str) and value else None


def _description(raw: str | None) -> str | None:
    value = _decode_config(raw).get(DESCRIPTION_CONFIG_KEY)
    return value if isinstance(value, str) and value else None


def _router_identity_id(raw: str | None) -> str | None:
    value = _decode_config(raw).get(ROUTER_IDENTITY_CONFIG_KEY)
    return value if isinstance(value, str) and value else None


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


def _normalize_mandate(mandate: str) -> str | None:
    """Trim a project mandate to its stored form; blank text clears it.

    :param mandate: Owner-written markdown, e.g. ``"Only touch the docs/ tree."``.
    :returns: The trimmed text, or ``None`` when it is blank.
    :raises OmnigentError: ``INVALID_INPUT`` if it exceeds
        :data:`PROJECT_MANDATE_MAX_BYTES` UTF-8 bytes, or compresses to more
        than the column holds.
    """
    trimmed = mandate.strip()
    size = len(trimmed.encode("utf-8"))
    if size > PROJECT_MANDATE_MAX_BYTES:
        raise OmnigentError(
            f"project mandate too large ({size} bytes; max {PROJECT_MANDATE_MAX_BYTES})",
            code=ErrorCode.INVALID_INPUT,
        )
    # Only text above the column's size can compress past it; check the real envelope.
    if size > PROJECT_MANDATE_MAX_STORED_BYTES and (
        len(seal_text(trimmed)) > PROJECT_MANDATE_MAX_STORED_BYTES
    ):
        raise OmnigentError(
            f"project mandate too large to store ({size} bytes compress to more than "
            f"{PROJECT_MANDATE_MAX_STORED_BYTES}); shorten it",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed or None


def _decode_proposals(raw: object) -> list[MandateProposal]:
    """Read stored proposals, skipping malformed entries rather than failing the project."""
    if not isinstance(raw, list):
        return []
    proposals = []
    for entry in raw:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("id"), str)
            and isinstance(entry.get("text"), str)
            and isinstance(entry.get("created_at"), int)
        ):
            session_id = entry.get("session_id")
            mode = entry.get("mode", "append")
            section = entry.get("section")
            base_hash = entry.get("base_hash")
            proposals.append(
                MandateProposal(
                    id=entry["id"],
                    text=entry["text"],
                    created_at=entry["created_at"],
                    session_id=session_id if isinstance(session_id, str) else None,
                    mode=mode if mode in MANDATE_PROPOSAL_MODES else "append",
                    section=section if isinstance(section, str) else None,
                    base_hash=base_hash if isinstance(base_hash, str) else None,
                )
            )
    return proposals


def _encode_proposals(proposals: list[MandateProposal]) -> list[dict[str, Any]] | None:
    """Store proposals as plain JSON objects; none stores SQL ``NULL``."""
    return [
        {
            "id": proposal.id,
            "text": proposal.text,
            "created_at": proposal.created_at,
            "session_id": proposal.session_id,
            # Append-only readers ignore these; an append omits them entirely.
            **(
                {}
                if proposal.mode == "append"
                else {
                    "mode": proposal.mode,
                    "section": proposal.section,
                    "base_hash": proposal.base_hash,
                }
            ),
        }
        for proposal in proposals
    ] or None


def _validate_proposal_shape(text: str, mode: str, section: str | None) -> str:
    """Return the trimmed text, or raise ``INVALID_INPUT`` for a malformed proposal."""
    if mode not in MANDATE_PROPOSAL_MODES:
        raise OmnigentError(
            f"mode must be one of {', '.join(MANDATE_PROPOSAL_MODES)}",
            code=ErrorCode.INVALID_INPUT,
        )
    trimmed = text.strip()
    size = len(trimmed.encode("utf-8"))
    if mode == "append":
        if section is not None or not trimmed or size > MANDATE_PROPOSAL_MAX_BYTES:
            raise OmnigentError(
                f"a mandate addition must be 1 to {MANDATE_PROPOSAL_MAX_BYTES} bytes, "
                f"with no section (got {size})",
                code=ErrorCode.INVALID_INPUT,
            )
        return trimmed
    if size > PROJECT_MANDATE_MAX_BYTES:
        raise OmnigentError(
            f"a replacement must be at most {PROJECT_MANDATE_MAX_BYTES} bytes (got {size})",
            code=ErrorCode.INVALID_INPUT,
        )
    if mode == "replace_section" and not (section or "").strip():
        raise OmnigentError(
            "replace_section needs the section's heading title", code=ErrorCode.INVALID_INPUT
        )
    if mode == "replace_all" and not trimmed:
        raise OmnigentError(
            "replace_all needs the full new mandate; a blank mandate is set by the owner",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


_IDENTITY_NAME_MAX_CHARS = 60


def _normalize_identity_name(name: str) -> str:
    """Trim an identity name; it must be non-empty and short enough for the roster."""
    trimmed = " ".join(name.split())
    if not trimmed or len(trimmed) > _IDENTITY_NAME_MAX_CHARS:
        raise OmnigentError(
            f"identity name must be 1-{_IDENTITY_NAME_MAX_CHARS} characters",
            code=ErrorCode.INVALID_INPUT,
        )
    if trimmed.casefold() in ("lead", "orchestrator"):
        raise OmnigentError(
            f"{trimmed} names the project's built-in Lead", code=ErrorCode.INVALID_INPUT
        )
    return trimmed


def _normalize_identity_instructions(instructions: str) -> str:
    """Trim persona text and bound its UTF-8 size."""
    trimmed = instructions.strip()
    size = len(trimmed.encode("utf-8"))
    if size > PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES:
        raise OmnigentError(
            f"identity instructions too large ({size} bytes; "
            f"max {PROJECT_IDENTITY_INSTRUCTIONS_MAX_BYTES})",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


def _normalize_identity_scope(
    scope: Mapping[str, Sequence[str]] | None,
) -> dict[str, list[str]] | None:
    """Trim a role's scope lists; ``None`` when both are empty.

    :raises OmnigentError: ``INVALID_INPUT`` for too many or too long items.
    """
    if scope is None:
        return None
    normalized: dict[str, list[str]] = {}
    for key in ROLE_SCOPE_KEYS:
        items = [item.strip() for item in scope.get(key) or () if item.strip()]
        if len(items) > ROLE_SCOPE_MAX_ITEMS or any(
            len(item) > ROLE_SCOPE_ITEM_MAX_CHARS for item in items
        ):
            raise OmnigentError(
                f"A role's scope holds at most {ROLE_SCOPE_MAX_ITEMS} items per list, "
                f"each up to {ROLE_SCOPE_ITEM_MAX_CHARS} characters",
                code=ErrorCode.INVALID_INPUT,
            )
        normalized[key] = items
    if not any(normalized.values()):
        return None
    if len(seal(normalized)) > PROJECT_IDENTITY_SCOPE_MAX_BYTES:
        raise OmnigentError("A role's scope is too large", code=ErrorCode.INVALID_INPUT)
    return normalized


def _identity_entity(row: SqlProjectIdentity) -> ProjectIdentity:
    return ProjectIdentity(
        id=row.id,
        project_id=row.project_id,
        name=row.name,
        instructions=row.instructions,
        agent_id=row.agent_id,
        model=row.model,
        effort=row.effort,
        active_session_id=row.active_session_id,
        created_by_session_id=row.created_by_session_id,
        scope=row.scope,
        project_agent_id=row.project_agent_id,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


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
        config=_client_config(_decode_config(row.config)),
        main_session_id=row.main_session_id or row.legacy_orchestrator_session_id,
        mandate=row.mandate,
        mandate_proposals=_decode_proposals(row.mandate_proposals),
        retired_main_session_ids=_retired_ids(row.config),
        router_identity_id=_router_identity_id(row.config),
        lead_removed=_decode_config(row.config).get(LEAD_REMOVED_CONFIG_KEY) is True,
        lead_charter=_lead_charter(row.config),
        description=_description(row.config),
    )


def router_session_id_for_row(session: Session, row: SqlProject) -> str | None:
    """The session of the project's router (see ``choose_router``), read in *session*."""
    identities = session.scalars(
        select(SqlProjectIdentity)
        .where(
            SqlProjectIdentity.workspace_id == row.workspace_id,
            SqlProjectIdentity.project_id == row.id,
        )
        .order_by(asc(SqlProjectIdentity.created_at), asc(SqlProjectIdentity.id))
    )
    personas = [_identity_entity(identity) for identity in identities]
    return choose_router(_to_entity(row), personas).session_id


def _encode_order(preference: ProjectOrderPreference) -> str:
    """Serialize an order only if its compressed frame fits a MySQL BLOB."""
    serialized = json.dumps(preference, separators=(",", ":"))
    stored = encode(serialized)
    assert stored is not None
    if len(stored) > _PREFERENCE_MAX_STORED_BYTES:
        raise OmnigentError("Project order is too large to save", code=ErrorCode.INVALID_INPUT)
    return serialized


def _decode_order(raw: bytes | str | memoryview | None) -> ProjectOrderPreference:
    """Keep invalid preferences from breaking project discovery."""
    default: ProjectOrderPreference = {"sort_mode": "alphabetical", "ordered_project_ids": None}
    if raw is None:
        return default
    try:
        # The 10,000-ID API limit serializes to under 400 KiB, including legacy spacing.
        decoded = json.loads(decode(raw, max_decoded_bytes=512 * 1024) or "null")
        # The original format stored only the manual ID array.
        if isinstance(decoded, list):
            decoded = {"sort_mode": "manual", "ordered_project_ids": decoded}
        if not isinstance(decoded, dict) or decoded.get("sort_mode") not in (
            "alphabetical",
            "manual",
        ):
            return default
        ids = decoded.get("ordered_project_ids")
        if ids is None:
            return default
        if (
            not isinstance(ids, list)
            or len(ids) > 10000
            or any(
                not isinstance(id, str) or re.fullmatch("[0-9a-f]{32}", id) is None for id in ids
            )
        ):
            return default
        return {
            "sort_mode": decoded["sort_mode"],
            "ordered_project_ids": list(dict.fromkeys(ids)),
        }
    except (ValueError, RecursionError, zstandard.ZstdError):
        return default


MANDATE_EDIT_REFUSAL = "Only the project owner or a product approver can change the Mandate"


def may_edit_project_rules(row: SqlProject, role: ProjectRole | None, user_id: str | None) -> bool:
    """The owner, or a member on ``product_approvers``, edits the mandate and the stages."""
    if role == "owner":
        return True
    if role is None or user_id is None:
        return False
    approvers = _decode_config(row.config).get("product_approvers")
    return isinstance(approvers, list) and user_id in {
        entry.strip() for entry in approvers if isinstance(entry, str)
    }


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
        self._session_immediate = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.project_store",
            immediate=True,
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
        mandate: str | None = None,
        description: str | None = None,
    ) -> Project:
        """Insert a new, empty project.

        Rejects a name the owner already uses via the ``_name_taken`` pre-check.
        That check is the only guard — no unique index backs it — so a
        concurrent create of the same name can slip through; see
        ``SqlProject.__table_args__`` for why that is acceptable.
        """
        created_at = now_epoch()
        stored_description = normalize_project_description(description or "")
        encoded_config = _encode_config(
            {**_client_config(config), DESCRIPTION_CONFIG_KEY: stored_description}
            if stored_description
            else _client_config(config)
        )
        stored_mandate = _normalize_mandate(mandate) if mandate is not None else None

        def write(session: Session) -> Project:
            require_active_account(session, user_id)
            if self._name_taken(session, user_id=user_id, name=name, exclude_id=None):
                raise OmnigentError(
                    f"A project named {name!r} already exists",
                    code=ErrorCode.ALREADY_EXISTS,
                )
            row = SqlProject(
                id=project_id,
                name=name,
                user_id=user_id,
                created_at=created_at,
                updated_at=None,
                config=encoded_config,
                mandate=stored_mandate,
            )
            session.add(row)
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "insert_project", write)

    def get(self, project_id: str, *, user_id: str | None) -> Project | None:
        """Return a project the caller owns or was shared, or ``None`` if not found."""
        with self._session("select_project_by_id") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if project_role(session, row, user_id) is None:
                return None
            return _to_entity(row)

    def list(self, *, user_id: str | None) -> list[Project]:
        """List owned and shared projects ordered by ``created_at ASC, id ASC``."""
        with self._session("list_projects") as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.id.in_(accessible_project_ids(user_id)))
                .order_by(asc(SqlProject.created_at), asc(SqlProject.id))
            )
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(r) for r in rows]

    def list_page(
        self, *, user_id: str | None, limit: int, after_id: str | None = None
    ) -> tuple[builtins.list[Project], bool]:
        """Return a ``(created_at, id)`` keyset page served by ``ix_projects_user_id``."""
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._session("list_projects_page") as session:
            stmt = (
                select(SqlProject)
                .where(SqlProject.workspace_id == current_workspace_id())
                .where(SqlProject.id.in_(accessible_project_ids(user_id)))
            )
            if after_id is not None:
                after = session.get(SqlProject, (current_workspace_id(), after_id))
                if project_role(session, after, user_id) is None:
                    raise ValueError("after must be the id of one of your projects")
                stmt = stmt.where(
                    tuple_(SqlProject.created_at, SqlProject.id) > (after.created_at, after.id)
                )
            stmt = stmt.order_by(asc(SqlProject.created_at), asc(SqlProject.id)).limit(limit + 1)
            rows = session.execute(stmt).scalars().all()
            return [_to_entity(row) for row in rows[:limit]], len(rows) > limit

    def update(
        self,
        project_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        config: dict[str, Any] | None = None,
        main_session_id: str | None = None,
        expected_main_session_id: str | None = None,
        mandate: str | None = None,
    ) -> Project | None:
        """Update mutable fields of an owned project.

        ``None`` leaves a field unchanged. Returns ``None`` if the project does
        not exist or is not owned by ``user_id``. A ``config`` of ``{}`` clears
        the stored defaults (distinct from ``None`` = leave unchanged), and a
        blank ``mandate`` clears the stored mandate. Only the owner or a product
        approver may change it.

        :raises OmnigentError: ``FORBIDDEN`` when any other member changes the mandate.

        A rename re-checks ``_name_taken``, which — as on ``create`` — is the
        only uniqueness guard, so concurrent renames to the same name can both
        land.
        """
        client_config = _client_config(config) if config is not None else None
        stored_mandate = _normalize_mandate(mandate) if mandate is not None else None
        updated_at = now_epoch()

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            role = project_role(session, row, user_id)
            if row is None or role is None:
                return None
            if (
                mandate is not None
                and row.mandate != stored_mandate
                and not may_edit_project_rules(row, role, user_id)
            ):
                # Members may resend the stored mandate unchanged with a whole-form save.
                raise OmnigentError(MANDATE_EDIT_REFUSAL, code=ErrorCode.FORBIDDEN)
            changed = False
            current_main = row.main_session_id or row.legacy_orchestrator_session_id
            if main_session_id is not None and current_main != main_session_id:
                if current_main != expected_main_session_id:
                    raise OmnigentError(
                        "The project's main session changed; reload the project",
                        code=ErrorCode.CONFLICT,
                    )
                row.main_session_id = main_session_id
                if current_main is not None:
                    # Keep the old orchestrator's address routable to its successor.
                    retired = [r for r in _retired_ids(row.config) if r != main_session_id]
                    if current_main not in retired:
                        retired.append(current_main)
                    row.config = _encode_config(_with_retired(_decode_config(row.config), retired))
            if name is not None and row.name != name:
                if self._name_taken(
                    session, user_id=row.user_id, name=name, exclude_id=project_id
                ):
                    raise OmnigentError(
                        f"A project named {name!r} already exists",
                        code=ErrorCode.ALREADY_EXISTS,
                    )
                row.name = name
                changed = True
            if client_config is not None:
                encoded_config = _encode_config({**client_config, **_server_config(row.config)})
                if row.config != encoded_config:
                    row.config = encoded_config
                    changed = True
            if mandate is not None and row.mandate != stored_mandate:
                row.mandate = stored_mandate
                changed = True
            if changed:
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "update_project", write)

    def delete(
        self,
        project_id: str,
        *,
        user_id: str | None,
        require_no_main: bool = False,
        delete_tickets: bool = False,
    ) -> bool:
        """Delete an owned project. Idempotent; returns ``False`` if not found.

        With ``delete_tickets`` the project's tickets go too, with their events,
        session links, read state and related-ticket links; otherwise a project
        with tickets is refused so its ticket history is retained.
        """

        def write(session: Session) -> bool:
            row = session.scalar(
                select(SqlProject)
                .where(
                    SqlProject.workspace_id == current_workspace_id(),
                    SqlProject.id == project_id,
                )
                .with_for_update()
            )
            if row is None or row.user_id != user_id:
                return False
            if require_no_main and (row.main_session_id or row.legacy_orchestrator_session_id):
                return False
            if delete_tickets:
                for model in (
                    SqlProjectTicketStageMove,
                    SqlProjectTicketStageMoveBuild,
                    SqlProjectTicketEvent,
                    SqlProjectTicketSession,
                    SqlProjectTicketRead,
                    SqlProjectTicketLink,
                    SqlProjectTicket,
                ):
                    session.execute(
                        delete(model).where(
                            model.workspace_id == current_workspace_id(),
                            model.project_id == project_id,
                        )
                    )
            elif (
                session.scalar(
                    select(SqlProjectTicket.id)
                    .where(
                        SqlProjectTicket.workspace_id == current_workspace_id(),
                        SqlProjectTicket.project_id == project_id,
                    )
                    .limit(1)
                )
                is not None
            ):
                raise OmnigentError(
                    "Projects with tickets cannot be deleted; ticket history is retained.",
                    code=ErrorCode.CONFLICT,
                )
            for identity in self._identity_rows(session, project_id):
                session.delete(identity)
            for model in (SqlProjectShare, SqlProjectKpiRevision, SqlProjectRoleKpiRevision):
                session.execute(
                    delete(model).where(
                        model.workspace_id == current_workspace_id(),
                        model.project_id == project_id,
                    )
                )
            session.delete(row)
            return True

        return run_write_transaction(self._session_immediate, "delete_project", write)

    def propose_mandate_addition(
        self,
        project_id: str,
        text: str,
        *,
        user_id: str | None,
        session_id: str | None = None,
        mode: str = "append",
        section: str | None = None,
    ) -> tuple[Project, MandateProposal] | None:
        """Queue a change for the owner to accept; it changes no instructions until then."""
        trimmed = _validate_proposal_shape(text, mode, section)
        section_title = section.strip() if mode == "replace_section" and section else None

        def write(session: Session) -> tuple[Project, MandateProposal] | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or project_role(session, row, user_id) is None:
                return None
            pending = _decode_proposals(row.mandate_proposals)
            if len(pending) >= MANDATE_PROPOSALS_MAX_PENDING:
                raise OmnigentError(
                    f"{len(pending)} mandate proposals already await the owner; "
                    "ask them to accept or dismiss one first",
                    code=ErrorCode.CONFLICT,
                )
            proposal = MandateProposal(
                id=uuid.uuid4().hex,
                text=trimmed,
                created_at=now_epoch(),
                session_id=session_id,
                mode=mode,
                section=section_title,
            )
            if mode != "append":
                matches = len(section_spans(row.mandate or "", section_title or ""))
                if mode == "replace_section" and matches != 1:
                    raise OmnigentError(
                        f"the mandate has {matches} sections titled {section_title!r}; "
                        "name exactly one existing heading",
                        code=ErrorCode.INVALID_INPUT,
                    )
                target = proposal_target(row.mandate, proposal) or ""
                proposal = dataclasses.replace(proposal, base_hash=text_hash(target))
                # Refuse now what accepting would refuse: an oversized result.
                _normalize_mandate(apply_proposal(row.mandate, proposal))
            encoded = _encode_proposals([*pending, proposal])
            if len(seal(encoded)) > MANDATE_PROPOSALS_MAX_STORED_BYTES:
                raise OmnigentError(
                    "pending mandate proposals are too large to store together; "
                    "ask the owner to accept or dismiss one first",
                    code=ErrorCode.CONFLICT,
                )
            row.mandate_proposals = encoded
            session.flush()
            return _to_entity(row), proposal

        return run_write_transaction(self._session_immediate, "propose_mandate_addition", write)

    def resolve_mandate_proposal(
        self, project_id: str, proposal_id: str, *, user_id: str | None, accept: bool
    ) -> Project | None:
        """Accept (apply to the mandate) or dismiss a pending proposal.

        Only the owner or a product approver may; ``FORBIDDEN`` for other members.
        """

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            role = project_role(session, row, user_id)
            if row is None or role is None:
                return None
            if not may_edit_project_rules(row, role, user_id):
                raise OmnigentError(
                    "Only the project owner or a product approver can accept or dismiss "
                    "a mandate proposal",
                    code=ErrorCode.FORBIDDEN,
                )
            pending = _decode_proposals(row.mandate_proposals)
            proposal = next((entry for entry in pending if entry.id == proposal_id), None)
            if proposal is None:
                raise OmnigentError("Mandate proposal not found", code=ErrorCode.NOT_FOUND)
            if accept:
                if proposal_is_stale(row.mandate, proposal):
                    raise OmnigentError(
                        "the mandate changed since this proposal was written; "
                        "dismiss it and propose the change again",
                        code=ErrorCode.CONFLICT,
                    )
                row.mandate = _normalize_mandate(apply_proposal(row.mandate, proposal))
                row.updated_at = now_epoch()
            row.mandate_proposals = _encode_proposals(
                [entry for entry in pending if entry.id != proposal_id]
            )
            session.flush()
            return _to_entity(row)

        operation = "accept_mandate_proposal" if accept else "dismiss_mandate_proposal"
        return run_write_transaction(self._session_immediate, operation, write)

    def set_routing(
        self,
        project_id: str,
        *,
        user_id: str | None,
        router_identity_id: str | None,
        lead_removed: bool,
    ) -> Project:
        """Set who receives the project's routed events. Owner-only.

        :param router_identity_id: A persona's id, ``"orchestrator"`` for the Lead,
            or ``None`` for the default (the Lead, else the first persona).
        :param lead_removed: Whether the owner removed the built-in Lead.
        :raises OmnigentError: ``NOT_FOUND`` for an unknown project or persona,
            ``FORBIDDEN`` for a shared member.
        """
        updated_at = now_epoch()

        def write(session: Session) -> Project:
            row = self._require_owner(session, project_id, user_id, action="set its routing")
            if router_identity_id not in (None, "orchestrator"):
                identity = session.get(
                    SqlProjectIdentity, (current_workspace_id(), project_id, router_identity_id)
                )
                if identity is None:
                    raise OmnigentError("Persona not found", code=ErrorCode.NOT_FOUND)
            config = _decode_config(row.config)
            config.pop(ROUTER_IDENTITY_CONFIG_KEY, None)
            config.pop(LEAD_REMOVED_CONFIG_KEY, None)
            if router_identity_id is not None:
                config[ROUTER_IDENTITY_CONFIG_KEY] = router_identity_id
            if lead_removed:
                config[LEAD_REMOVED_CONFIG_KEY] = True
            encoded = _encode_config(config)
            if row.config != encoded:
                row.config = encoded
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "set_project_routing", write)

    def set_lead_charter(self, project_id: str, *, user_id: str | None, charter: str) -> Project:
        """Set the owner's charter for the built-in Lead; blank clears it. Owner-only.

        :raises OmnigentError: ``NOT_FOUND`` for an unknown project, ``FORBIDDEN``
            for a shared member, ``INVALID_INPUT`` for oversized text.
        """
        stored = _normalize_identity_instructions(charter)
        updated_at = now_epoch()

        def write(session: Session) -> Project:
            row = self._require_owner(
                session, project_id, user_id, action="edit the Lead's charter"
            )
            config = _decode_config(row.config)
            config.pop(LEAD_CHARTER_CONFIG_KEY, None)
            if stored:
                config[LEAD_CHARTER_CONFIG_KEY] = stored
            encoded = _encode_config(config)
            if row.config != encoded:
                row.config = encoded
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "set_project_lead_charter", write)

    def set_description(
        self, project_id: str, *, user_id: str | None, description: str
    ) -> Project:
        """Set the project's description (its charter for people); blank clears it.

        Owner-only: members read it but can't change it. The route also lets the
        Lead's session write it, since that session runs as the owner.

        :raises OmnigentError: ``NOT_FOUND`` for an unknown project, ``FORBIDDEN``
            for a shared member, ``INVALID_INPUT`` for oversized text.
        """
        stored = normalize_project_description(description)
        updated_at = now_epoch()

        def write(session: Session) -> Project:
            row = self._require_owner(
                session, project_id, user_id, action="edit the project description"
            )
            config = _decode_config(row.config)
            config.pop(DESCRIPTION_CONFIG_KEY, None)
            if stored:
                config[DESCRIPTION_CONFIG_KEY] = stored
            encoded = _encode_config(config)
            if row.config != encoded:
                row.config = encoded
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(self._session_immediate, "set_project_description", write)

    def set_lead_session_defaults(
        self, project_id: str, *, user_id: str | None, values: dict[str, str | None]
    ) -> Project | None:
        """Set the Lead's agent, model or effort: the project's session defaults.

        Like other identities, any person with access (owner or member) may edit
        them. A ``None`` or blank value clears that default.

        :param values: Keys among ``agent_id``, ``model`` and ``effort``.
        :returns: The project, or ``None`` if not found / no access.
        """
        unknown = set(values) - set(LEAD_SESSION_DEFAULT_KEYS)
        if unknown:
            raise ValueError(f"not a Lead session default: {sorted(unknown)}")
        updated_at = now_epoch()

        def write(session: Session) -> Project | None:
            require_active_account(session, user_id)
            row = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if row is None or project_role(session, row, user_id) is None:
                return None
            config = _decode_config(row.config)
            for key, value in values.items():
                if value and value.strip():
                    config[key] = value.strip()
                else:
                    config.pop(key, None)
            encoded = _encode_config(config)
            if row.config != encoded:
                row.config = encoded
                row.updated_at = updated_at
            session.flush()
            return _to_entity(row)

        return run_write_transaction(
            self._session_immediate, "set_project_lead_session_defaults", write
        )

    def list_main_sessions(self) -> builtins.list[tuple[str, str | None, str]]:
        with self._session("list_project_main_sessions") as session:
            rows = session.execute(
                select(SqlProject.id, SqlProject.user_id, SqlProject.main_session_id).where(
                    SqlProject.workspace_id == current_workspace_id(),
                    SqlProject.main_session_id.is_not(None),
                )
            ).all()
            return [(row[0], row[1], row[2]) for row in rows]

    def set_main_session(
        self,
        project_id: str,
        session_id: str,
        *,
        user_id: str | None,
        expected_main_session_id: str | None = None,
    ) -> Project:
        project = self.update(
            project_id,
            user_id=user_id,
            main_session_id=session_id,
            expected_main_session_id=expected_main_session_id,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return project

    # -- Shares --

    def _require_owner(
        self,
        session: Session,
        project_id: str,
        user_id: str | None,
        *,
        action: str = "manage its shares",
    ) -> SqlProject:
        row = session.get(SqlProject, (current_workspace_id(), project_id))
        role = project_role(session, row, user_id)
        if row is None or role is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if role != "owner":
            raise OmnigentError(f"Only the project owner can {action}", code=ErrorCode.FORBIDDEN)
        return row

    def _require_share_manager(
        self, session: Session, project_id: str, user_id: str | None
    ) -> tuple[SqlProject, ProjectRole]:
        """Load the project for a share write by its owner or a Manager."""
        row = session.get(SqlProject, (current_workspace_id(), project_id))
        role = project_role(session, row, user_id)
        if row is None or role is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        if role not in ("owner", "manager"):
            raise OmnigentError(
                "Only the project owner or a Manager can manage its shares",
                code=ErrorCode.FORBIDDEN,
            )
        return row, role

    @staticmethod
    def _share_entity(row: SqlProjectShare) -> ProjectShare:
        return ProjectShare(
            user_id=row.user_id,
            created_at=row.created_at,
            created_by=row.created_by,
            role=row.role or "member",
        )

    @staticmethod
    def _check_share_role(role: str) -> None:
        if role not in SHARE_ROLES:
            raise OmnigentError(
                f"Share role must be one of {', '.join(SHARE_ROLES)}", code=ErrorCode.INVALID_INPUT
            )

    def list_shares(self, project_id: str, *, user_id: str | None) -> builtins.list[ProjectShare]:
        with self._session("list_project_shares") as session:
            row = session.get(SqlProject, (current_workspace_id(), project_id))
            if project_role(session, row, user_id) is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            rows = session.scalars(
                select(SqlProjectShare)
                .where(
                    SqlProjectShare.workspace_id == current_workspace_id(),
                    SqlProjectShare.project_id == project_id,
                )
                .order_by(asc(SqlProjectShare.created_at), asc(SqlProjectShare.user_id))
            ).all()
            return [self._share_entity(r) for r in rows]

    def add_share(
        self, project_id: str, member_id: str, *, user_id: str | None, role: str = "member"
    ) -> ProjectShare:
        self._check_share_role(role)

        def write(session: Session) -> ProjectShare:
            require_active_account(session, user_id)
            row, caller_role = self._require_share_manager(session, project_id, user_id)
            if caller_role != "owner" and role != "member":
                raise OmnigentError(
                    "Only the project owner can make someone a Manager", code=ErrorCode.FORBIDDEN
                )
            if member_id == row.user_id:
                raise OmnigentError(
                    "The owner already has access to this project", code=ErrorCode.INVALID_INPUT
                )
            existing = session.get(
                SqlProjectShare, (current_workspace_id(), project_id, member_id)
            )
            if existing is None:
                existing = SqlProjectShare(
                    workspace_id=current_workspace_id(),
                    project_id=project_id,
                    user_id=member_id,
                    created_at=now_epoch(),
                    created_by=user_id,
                    role=role,
                )
                session.add(existing)
                session.flush()
            elif caller_role == "owner" and existing.role != role:
                # A Manager re-inviting someone never changes their role.
                existing.role = role
                session.flush()
            invalidate_after_commit(session, project_id)
            return self._share_entity(existing)

        return run_write_transaction(self._session_immediate, "add_project_share", write)

    def set_share_role(
        self, project_id: str, member_id: str, role: str, *, user_id: str | None
    ) -> ProjectShare:
        self._check_share_role(role)

        def write(session: Session) -> ProjectShare:
            require_active_account(session, user_id)
            self._require_owner(session, project_id, user_id, action="change a member's role")
            existing = session.get(
                SqlProjectShare, (current_workspace_id(), project_id, member_id)
            )
            if existing is None:
                raise OmnigentError("Share not found", code=ErrorCode.NOT_FOUND)
            if existing.role != role:
                existing.role = role
                session.flush()
            return self._share_entity(existing)

        return run_write_transaction(self._session_immediate, "set_project_share_role", write)

    def remove_share(self, project_id: str, member_id: str, *, user_id: str | None) -> bool:
        def write(session: Session) -> bool:
            require_active_account(session, user_id)
            _, caller_role = self._require_share_manager(session, project_id, user_id)
            existing = session.get(
                SqlProjectShare, (current_workspace_id(), project_id, member_id)
            )
            if existing is None:
                return False
            if caller_role != "owner" and existing.role != "member":
                raise OmnigentError(
                    "Only the project owner can remove a Manager", code=ErrorCode.FORBIDDEN
                )
            session.delete(existing)
            session.flush()
            invalidate_after_commit(session, project_id)
            return True

        return run_write_transaction(self._session_immediate, "remove_project_share", write)

    # -- Identities --

    def _owned_project(self, session: Session, project_id: str, user_id: str | None) -> bool:
        row = session.get(SqlProject, (current_workspace_id(), project_id))
        return project_role(session, row, user_id) is not None

    def _identity_rows(
        self, session: Session, project_id: str
    ) -> builtins.list[SqlProjectIdentity]:
        stmt = (
            select(SqlProjectIdentity)
            .where(
                SqlProjectIdentity.workspace_id == current_workspace_id(),
                SqlProjectIdentity.project_id == project_id,
            )
            .order_by(asc(SqlProjectIdentity.created_at), asc(SqlProjectIdentity.id))
        )
        return builtins.list(session.execute(stmt).scalars().all())

    def list_identities(
        self, project_id: str, *, user_id: str | None
    ) -> builtins.list[ProjectIdentity]:
        with self._session("list_project_identities") as session:
            if not self._owned_project(session, project_id, user_id):
                return []
            return [_identity_entity(row) for row in self._identity_rows(session, project_id)]

    def get_identity(
        self, project_id: str, identity_id: str, *, user_id: str | None
    ) -> ProjectIdentity | None:
        with self._session("select_project_identity") as session:
            if not self._owned_project(session, project_id, user_id):
                return None
            row = session.get(
                SqlProjectIdentity, (current_workspace_id(), project_id, identity_id)
            )
            return _identity_entity(row) if row is not None else None

    @staticmethod
    def _require_unique_name(
        rows: builtins.list[SqlProjectIdentity], name: str, exclude_id: str | None
    ) -> None:
        folded = name.casefold()
        if any(row.id != exclude_id and row.name.casefold() == folded for row in rows):
            raise OmnigentError(
                f"An identity named {name!r} already exists", code=ErrorCode.ALREADY_EXISTS
            )

    def _insert_identity(
        self,
        session: Session,
        project_id: str,
        identity_id: str,
        *,
        name: str,
        instructions: str,
        agent_id: str | None,
        model: str | None,
        effort: str | None,
        created_at: int,
        created_by_session_id: str | None = None,
    ) -> SqlProjectIdentity:
        """Insert a normalized identity; the caller holds the project row lock."""
        rows = self._identity_rows(session, project_id)
        if len(rows) >= PROJECT_IDENTITY_MAX_COUNT:
            raise OmnigentError(
                f"A project can define at most {PROJECT_IDENTITY_MAX_COUNT} identities",
                code=ErrorCode.INVALID_INPUT,
            )
        self._require_unique_name(rows, name, None)
        row = SqlProjectIdentity(
            workspace_id=current_workspace_id(),
            project_id=project_id,
            id=identity_id,
            name=name,
            instructions=instructions,
            agent_id=agent_id or None,
            model=model or None,
            effort=effort or None,
            active_session_id=None,
            created_by_session_id=created_by_session_id,
            created_at=created_at,
            updated_at=None,
        )
        session.add(row)
        session.flush()
        return row

    def create_identity(
        self,
        project_id: str,
        identity_id: str,
        *,
        user_id: str | None,
        name: str,
        instructions: str = "",
        agent_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        created_by_session_id: str | None = None,
        scope: Mapping[str, Sequence[str]] | None = None,
    ) -> ProjectIdentity:
        stored_name = _normalize_identity_name(name)
        stored_instructions = _normalize_identity_instructions(instructions)
        stored_scope = _normalize_identity_scope(scope)
        created_at = now_epoch()

        def write(session: Session) -> ProjectIdentity:
            require_active_account(session, user_id)
            project = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if project is None or project_role(session, project, user_id) is None:
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            existing = session.get(
                SqlProjectIdentity, (current_workspace_id(), project_id, identity_id)
            )
            if existing is not None:
                return _identity_entity(existing)
            row = self._insert_identity(
                session,
                project_id,
                identity_id,
                name=stored_name,
                instructions=stored_instructions,
                agent_id=agent_id,
                model=model,
                effort=effort,
                created_at=created_at,
                created_by_session_id=created_by_session_id,
            )
            row.scope = stored_scope
            invalidate_after_commit(session, project_id)
            return _identity_entity(row)

        return run_write_transaction(self._session_immediate, "insert_project_identity", write)

    def update_identity(
        self,
        project_id: str,
        identity_id: str,
        *,
        user_id: str | None,
        name: str | None = None,
        instructions: str | None = None,
        agent_id: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        clear: frozenset[str] = frozenset(),
        agent_editor: bool = False,
        scope: Mapping[str, Sequence[str]] | None = None,
    ) -> ProjectIdentity | None:
        stored_scope = _normalize_identity_scope(scope)
        stored_name = _normalize_identity_name(name) if name is not None else None
        stored_instructions = (
            _normalize_identity_instructions(instructions) if instructions is not None else None
        )
        updated_at = now_epoch()

        def write(session: Session) -> ProjectIdentity | None:
            require_active_account(session, user_id)
            project = session.get(
                SqlProject, (current_workspace_id(), project_id), with_for_update=True
            )
            if project is None or project_role(session, project, user_id) is None:
                return None
            row = session.get(
                SqlProjectIdentity, (current_workspace_id(), project_id, identity_id)
            )
            if row is None:
                return None
            if agent_editor and row.created_by_session_id is None:
                raise OmnigentError(
                    "Only the project owner can edit an identity the owner created",
                    code=ErrorCode.FORBIDDEN,
                )
            if stored_name is not None and stored_name != row.name:
                self._require_unique_name(
                    self._identity_rows(session, project_id), stored_name, identity_id
                )
                row.name = stored_name
            if stored_instructions is not None:
                row.instructions = stored_instructions
            if "scope" in clear or scope is not None:
                row.scope = stored_scope
            for field_name, value in (
                ("agent_id", agent_id),
                ("model", model),
                ("effort", effort),
            ):
                if field_name in clear:
                    setattr(row, field_name, None)
                elif value is not None:
                    setattr(row, field_name, value or None)
            row.updated_at = updated_at
            session.flush()
            return _identity_entity(row)

        return run_write_transaction(self._session_immediate, "update_project_identity", write)

    def delete_identity(self, project_id: str, identity_id: str, *, user_id: str | None) -> bool:
        def write(session: Session) -> bool:
            require_active_account(session, user_id)
            if not self._owned_project(session, project_id, user_id):
                return False
            row = session.get(
                SqlProjectIdentity, (current_workspace_id(), project_id, identity_id)
            )
            if row is None:
                return False
            session.delete(row)
            session.execute(
                delete(SqlProjectRoleKpiRevision).where(
                    SqlProjectRoleKpiRevision.workspace_id == current_workspace_id(),
                    SqlProjectRoleKpiRevision.project_id == project_id,
                    SqlProjectRoleKpiRevision.identity_id == identity_id,
                )
            )
            invalidate_after_commit(session, project_id)
            return True

        return run_write_transaction(self._session_immediate, "delete_project_identity", write)

    def set_identity_session(
        self,
        project_id: str,
        identity_id: str,
        session_id: str,
        *,
        user_id: str | None,
        expected_session_id: str | None,
    ) -> ProjectIdentity:
        def write(session: Session) -> ProjectIdentity:
            require_active_account(session, user_id)
            if not self._owned_project(session, project_id, user_id):
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            row = session.get(
                SqlProjectIdentity,
                (current_workspace_id(), project_id, identity_id),
                with_for_update=True,
            )
            if row is None:
                raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
            if row.active_session_id != session_id:
                if row.active_session_id != expected_session_id:
                    raise OmnigentError(
                        "The identity's session changed; reload the project",
                        code=ErrorCode.CONFLICT,
                    )
                row.active_session_id = session_id
                row.updated_at = now_epoch()
            session.flush()
            return _identity_entity(row)

        return run_write_transaction(
            self._session_immediate, "set_project_identity_session", write
        )

    def set_identity_project_agent(
        self,
        project_id: str,
        identity_id: str,
        project_agent_id: str | None,
        *,
        user_id: str | None,
        expected: str | None,
    ) -> ProjectIdentity:
        """Point an identity at its project agent, or clear it, if it still has *expected*.

        :raises OmnigentError: ``CONFLICT`` when another writer changed it first.
        """

        def write(session: Session) -> ProjectIdentity:
            require_active_account(session, user_id)
            if not self._owned_project(session, project_id, user_id):
                raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
            row = session.get(
                SqlProjectIdentity,
                (current_workspace_id(), project_id, identity_id),
                with_for_update=True,
            )
            if row is None:
                raise OmnigentError("Identity not found", code=ErrorCode.NOT_FOUND)
            if row.project_agent_id != project_agent_id:
                if row.project_agent_id != expected:
                    raise OmnigentError(
                        "The identity's agent changed; reload the project",
                        code=ErrorCode.CONFLICT,
                    )
                row.project_agent_id = project_agent_id
            session.flush()
            return _identity_entity(row)

        return run_write_transaction(self._session_immediate, "set_project_identity_agent", write)

    def get_order(self, *, user_id: str | None) -> builtins.list[str] | None:
        """Return the active order used by project-list endpoints."""
        preference = self.get_order_preference(user_id=user_id)
        return preference["ordered_project_ids"] if preference["sort_mode"] == "manual" else None

    def get_order_preference(self, *, user_id: str | None) -> ProjectOrderPreference:
        """Read project ordering from the user's preferences."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id
        with self._session("read_project_order") as session:
            # Decode raw bytes here so malformed values cannot fail in the ORM result processor.
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary)).where(
                    SqlPreference.workspace_id == current_workspace_id(),
                    SqlPreference.user_id == preference_user_id,
                    SqlPreference.key == _PROJECT_ORDER_KEY,
                )
            )
            return _decode_order(raw)

    def save_order(
        self, ids: builtins.list[str] | None, *, user_id: str | None
    ) -> ProjectOrderPreference:
        """Validate project ownership and update only the user's preference."""
        preference_user_id = RESERVED_USER_LOCAL if user_id is None else user_id

        def write(session: Session) -> ProjectOrderPreference:
            require_active_account(session, user_id)
            workspace_id = current_workspace_id()
            if ids is None:
                raw = session.scalar(
                    select(type_coerce(SqlPreference.value, LargeBinary))
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .with_for_update()
                )
                preference = _decode_order(raw)
                preference["sort_mode"] = "alphabetical"
                if raw is None:
                    return preference
                session.execute(
                    update(SqlPreference)
                    .where(
                        SqlPreference.workspace_id == workspace_id,
                        SqlPreference.user_id == preference_user_id,
                        SqlPreference.key == _PROJECT_ORDER_KEY,
                    )
                    .values(value=_encode_order(preference))
                )
                return preference
            owned = set(session.scalars(accessible_project_ids(user_id)))
            if len(set(ids)) != len(ids) or not set(ids).issubset(owned):
                raise OmnigentError("Invalid project order", code=ErrorCode.INVALID_INPUT)
            preference: ProjectOrderPreference = {
                "sort_mode": "manual",
                "ordered_project_ids": ids,
            }
            upsert_preference_row(
                session,
                user_id=preference_user_id,
                key=_PROJECT_ORDER_KEY,
                value=_encode_order(preference),
            )
            return preference

        return run_write_transaction(self._session_immediate, "save_project_order", write)
