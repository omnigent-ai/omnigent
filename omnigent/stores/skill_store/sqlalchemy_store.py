"""SQLAlchemy implementation of :class:`SkillStore`.

Every installed row belongs to a *principal* (``"org"``, ``"local"`` for the
single local user, or ``"user:<id>"``), never absent, so each visibility
query is one range of the principal-ordered index and the identity index is
unique on every engine. A write that adds to a principal's set first locks
that principal's row in ``skill_principals`` (``SELECT ... FOR UPDATE``;
SQLite serializes through its immediate transaction), checks the budgets,
and writes everything in that one transaction.
"""

from __future__ import annotations

import contextlib
import re
import uuid
from collections.abc import Collection, Iterable, Sequence

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlInstalledSkill,
    SqlSkillPrincipal,
    SqlSkillSource,
    current_workspace_id,
)
from omnigent.db.enum_codecs import decode_installed_skill_scope, encode_installed_skill_scope
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    now_epoch,
    run_write_transaction,
)
from omnigent.entities import InstalledSkill, SkillSource
from omnigent.entities.skill import SkillScope
from omnigent.stores.skill_store import (
    DEFAULT_PAGE,
    MAX_DELIVERY_BYTES_PER_SCOPE,
    MAX_DELIVERY_ENTRIES_PER_SCOPE,
    MAX_DESCRIPTION_CHARS,
    MAX_DISPLAY_NAME_CHARS,
    MAX_INSTALLED_PER_OWNER,
    MAX_LOCATION_CHARS,
    MAX_NAME_CHARS,
    MAX_PAGE,
    MAX_PATH_CHARS,
    MAX_REF_CHARS,
    MAX_SOURCE_PATH_CHARS,
    MAX_URL_CHARS,
    MAX_USER_ID_CHARS,
    InstallResult,
    SkillInstall,
    SkillPage,
    SkillQuotaError,
    SkillStore,
    SkillValueError,
    check_bound,
)

ORG_PRINCIPAL = "org"
LOCAL_PRINCIPAL = "local"
# git's own name for the remote's default branch: what a source without a ref fetches.
DEFAULT_REF = "HEAD"
# Names per IN-list, under every supported engine's parameter limit.
_NAMES_PER_QUERY = 100
# A page cursor: the last row's ordering epoch and its id, so the next page
# needs no lookup of a row that may since have been deleted.
_CURSOR = re.compile(r"^(\d{1,19})\.([0-9a-f]{32})$")


def _encode_cursor(sort_value: int, row_id: str) -> str:
    return f"{sort_value}.{row_id}"


def _decode_cursor(after: str) -> tuple[int, str]:
    """``(sort_value, row_id)`` from a cursor this store produced.

    :raises SkillValueError: When *after* is not one of ours.
    """
    match = _CURSOR.match(after)
    if match is None:
        raise SkillValueError("invalid cursor")
    return int(match.group(1)), match.group(2)


def principal_for(scope: SkillScope, owner_user_id: str | None) -> str:
    """The principal a row belongs to; ``owner_user_id`` is ignored for the org."""
    if scope == "org":
        return ORG_PRINCIPAL
    return LOCAL_PRINCIPAL if owner_user_id is None else f"user:{owner_user_id}"


def _visible_principals(user_id: str | None) -> list[str]:
    """The partitions a reader sees: the org's, plus their own."""
    return [ORG_PRINCIPAL, principal_for("user", user_id)]


def _who(scope: SkillScope) -> str:
    return "everyone" if scope == "org" else "a user"


def _check_delivery_budget(
    bytes_after: int, entries_after: int, max_bytes: int, max_entries: int, what: str
) -> None:
    """Refuse a write that would leave a principal's enabled skills past what a runner unpacks.

    :param what: How the message starts, e.g. ``"Installed skills for everyone"``.
    :raises SkillQuotaError: When either total is over its budget.
    """
    if bytes_after > max_bytes:
        raise SkillQuotaError(
            f"{what} would exceed {max_bytes // (1024 * 1024)} MB unpacked together; "
            "remove or disable some first."
        )
    if entries_after > max_entries:
        raise SkillQuotaError(
            f"{what} would exceed {max_entries} files together; remove or disable some first."
        )


def _source_entity(row: SqlSkillSource) -> SkillSource:
    return SkillSource(
        id=row.id,
        url=row.url,
        ref=None if row.ref == DEFAULT_REF else row.ref,
        name=row.name,
        created_by=row.created_by,
        created_at=row.created_at,
        path=row.path,
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
        bundle_bytes=row.bundle_bytes,
        bundle_entries=row.bundle_entries,
        enabled=bool(row.enabled),
        user_invocable=bool(row.user_invocable),
        installed_by=row.installed_by,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
    )


def _chunks(values: Iterable[str], size: int) -> Iterable[list[str]]:
    batch: list[str] = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def _source_identity(url: str, ref: str | None, path: str):  # type: ignore[no-untyped-def]  # Select
    """The lookup ``ux_skill_sources_identity`` serves."""
    return select(SqlSkillSource).where(
        SqlSkillSource.workspace_id == current_workspace_id(),
        SqlSkillSource.url == url,
        SqlSkillSource.ref == (DEFAULT_REF if ref is None else ref),
        SqlSkillSource.path == path,
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
        self, *, url: str, ref: str | None, name: str, created_by: str | None, path: str = ""
    ) -> SkillSource:
        check_bound("url", url, MAX_URL_CHARS)
        check_bound("ref", ref, MAX_REF_CHARS)
        check_bound("path", path, MAX_SOURCE_PATH_CHARS)
        check_bound("name", name, MAX_DISPLAY_NAME_CHARS)
        check_bound("created_by", created_by, MAX_USER_ID_CHARS)

        def write(session: Session) -> SkillSource:
            existing = session.scalar(_source_identity(url, ref, path))
            if existing is not None:
                return _source_entity(existing)
            row = SqlSkillSource(
                id=uuid.uuid4().hex,
                url=url,
                ref=DEFAULT_REF if ref is None else ref,
                path=path,
                name=name,
                created_by=created_by,
                created_at=now_epoch(),
            )
            session.add(row)
            session.flush()
            return _source_entity(row)

        try:
            return run_write_transaction(self._session_immediate, "add_skill_source", write)
        except IntegrityError:
            # Another replica seeded the same source first; it is the one row.
            return run_write_transaction(self._session_immediate, "add_skill_source_retry", write)

    def find_source(self, url: str, ref: str | None, path: str = "") -> SkillSource | None:
        with self._session("find_skill_source") as session:
            row = session.scalar(_source_identity(url, ref, path))
            return None if row is None else _source_entity(row)

    def list_sources(
        self, *, limit: int = DEFAULT_PAGE, after: str | None = None
    ) -> SkillPage[SkillSource]:
        limit = max(1, min(limit, MAX_PAGE))
        with self._session("list_skill_sources") as session:
            stmt = (
                select(SqlSkillSource)
                .where(SqlSkillSource.workspace_id == current_workspace_id())
                .order_by(SqlSkillSource.created_at, SqlSkillSource.id)
                .limit(limit + 1)
            )
            if after is not None:
                created_at, row_id = _decode_cursor(after)
                # Compared column by column so each bind takes the column's type
                # (the id is 16 raw bytes, not its hex spelling).
                stmt = stmt.where(
                    or_(
                        SqlSkillSource.created_at > created_at,
                        and_(SqlSkillSource.created_at == created_at, SqlSkillSource.id > row_id),
                    )
                )
            rows = session.scalars(stmt).all()
            page = rows[:limit]
            return SkillPage(
                data=[_source_entity(row) for row in page],
                has_more=len(rows) > limit,
                last_id=_encode_cursor(page[-1].created_at, page[-1].id) if page else None,
            )

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

    def _ensure_principal(self, principal: str) -> None:
        """Make sure the principal's lock row exists; a concurrent creator is fine."""

        def write(session: Session) -> None:
            if session.get(SqlSkillPrincipal, (current_workspace_id(), principal)) is None:
                session.add(
                    SqlSkillPrincipal(
                        principal=principal,
                        installed_count=0,
                        enabled_bytes=0,
                        enabled_entries=0,
                        created_at=now_epoch(),
                    )
                )
                session.flush()

        with contextlib.suppress(IntegrityError):
            run_write_transaction(self._session_immediate, "ensure_skill_principal", write)

    @staticmethod
    def _lock_principal(session: Session, principal: str) -> SqlSkillPrincipal:
        """Lock the principal's row until the transaction ends and return it.

        A locking read returns the latest committed totals on every engine,
        including MySQL's repeatable-read snapshot; engines without row locks
        (SQLite) serialize through the immediate transaction instead.
        """
        lock = session.get(
            SqlSkillPrincipal,
            (current_workspace_id(), principal),
            with_for_update=True,
            populate_existing=True,
        )
        if lock is None:
            raise SkillValueError(f"no principal row for {principal}")
        return lock

    def _locked_skill(
        self, session: Session, skill_id: str
    ) -> tuple[SqlSkillPrincipal, SqlInstalledSkill] | None:
        """Lock a skill's principal first, then re-read the skill under that lock.

        The first read only learns the principal (immutable for a row); the
        locked re-read is what the write and the budget check see, so a
        concurrent change committed in between is never missed.
        """
        seen = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
        if seen is None:
            return None
        lock = self._lock_principal(session, seen.principal)
        row = session.get(
            SqlInstalledSkill,
            (current_workspace_id(), skill_id),
            with_for_update=True,
            populate_existing=True,
        )
        return None if row is None else (lock, row)

    def install_many(
        self,
        items: Sequence[SkillInstall],
        *,
        scope: SkillScope,
        owner_user_id: str | None,
        installed_by: str | None,
        max_rows: int | None = None,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> list[InstallResult]:
        max_rows = MAX_INSTALLED_PER_OWNER if max_rows is None else max_rows
        max_bytes = MAX_DELIVERY_BYTES_PER_SCOPE if max_bytes is None else max_bytes
        max_entries = MAX_DELIVERY_ENTRIES_PER_SCOPE if max_entries is None else max_entries
        principal = principal_for(scope, owner_user_id)
        owner = owner_user_id if scope == "user" else None
        check_bound("owner_user_id", owner, MAX_USER_ID_CHARS)
        check_bound("installed_by", installed_by, MAX_USER_ID_CHARS)
        for item in items:
            check_bound("name", item.name, MAX_NAME_CHARS)
            check_bound("display_name", item.display_name, MAX_DISPLAY_NAME_CHARS)
            check_bound("description", item.description, MAX_DESCRIPTION_CHARS)
            check_bound("source_url", item.source_url, MAX_URL_CHARS)
            check_bound("ref", item.ref, MAX_REF_CHARS)
            check_bound("path", item.path, MAX_PATH_CHARS)
            check_bound("bundle_location", item.bundle_location, MAX_LOCATION_CHARS)
        if len({item.name for item in items}) != len(items):
            raise SkillValueError("a skill name is repeated in one install")
        self._ensure_principal(principal)

        def write(session: Session) -> list[InstallResult]:
            lock = self._lock_principal(session, principal)
            existing = {
                row.name: row
                for row in session.scalars(
                    select(SqlInstalledSkill)
                    .where(
                        SqlInstalledSkill.workspace_id == current_workspace_id(),
                        SqlInstalledSkill.principal == principal,
                        SqlInstalledSkill.name.in_([item.name for item in items]),
                    )
                    .with_for_update()
                )
            }
            adding = [item for item in items if item.name not in existing]
            if lock.installed_count + len(adding) > max_rows:
                raise SkillQuotaError(
                    f"At most {max_rows} skills can be installed for {_who(scope)}; "
                    "remove some first."
                )
            # A replaced row keeps its enabled flag; only enabled skills count.
            after = lock.enabled_bytes
            entries_after = lock.enabled_entries
            for item in items:
                row = existing.get(item.name)
                if row is None:
                    after += item.bundle_bytes
                    entries_after += item.bundle_entries
                elif row.enabled:
                    after += item.bundle_bytes - row.bundle_bytes
                    entries_after += item.bundle_entries - row.bundle_entries
            _check_delivery_budget(
                after, entries_after, max_bytes, max_entries, f"Installed skills for {_who(scope)}"
            )
            now = now_epoch()
            results: list[InstallResult] = []
            for item in items:
                row = existing.get(item.name)
                replaced = None
                if row is None:
                    row = SqlInstalledSkill(
                        id=uuid.uuid4().hex,
                        name=item.name,
                        scope=encode_installed_skill_scope(scope),
                        principal=principal,
                        owner_user_id=owner,
                        enabled=True,
                        user_invocable=item.user_invocable,
                        installed_by=installed_by,
                        installed_at=now,
                    )
                    session.add(row)
                else:
                    replaced = (
                        row.bundle_location
                        if row.bundle_location != item.bundle_location
                        else None
                    )
                    row.updated_at = now
                row.display_name = item.display_name
                row.description = item.description
                row.source_url = item.source_url
                row.ref = item.ref
                row.commit = item.commit
                row.path = item.path
                row.bundle_location = item.bundle_location
                row.bundle_bytes = item.bundle_bytes
                row.bundle_entries = item.bundle_entries
                row.user_invocable = item.user_invocable
                session.flush()
                results.append(InstallResult(skill=_skill_entity(row), replaced_location=replaced))
            lock.installed_count += len(adding)
            lock.enabled_bytes = after
            lock.enabled_entries = entries_after
            session.flush()
            return results

        return run_write_transaction(self._session_immediate, "install_skills", write)

    def get(self, skill_id: str) -> InstalledSkill | None:
        with self._session("get_installed_skill") as session:
            row = session.get(SqlInstalledSkill, (current_workspace_id(), skill_id))
            return None if row is None else _skill_entity(row)

    def list_for_user(
        self, user_id: str | None, *, limit: int = DEFAULT_PAGE, after: str | None = None
    ) -> SkillPage[InstalledSkill]:
        """Merge one bounded keyset read per visible principal.

        Each partition is a single range of ``ix_installed_skills_principal_order``
        (``workspace_id, principal, installed_at, id``), so the rows scanned
        are at most ``2 * (limit + 1)``.
        """
        limit = max(1, min(limit, MAX_PAGE))
        cursor = None if after is None else _decode_cursor(after)
        with self._session("list_installed_skills") as session:
            rows: list[SqlInstalledSkill] = []
            for principal in _visible_principals(user_id):
                stmt = (
                    select(SqlInstalledSkill)
                    .where(
                        SqlInstalledSkill.workspace_id == current_workspace_id(),
                        SqlInstalledSkill.principal == principal,
                    )
                    .order_by(SqlInstalledSkill.installed_at, SqlInstalledSkill.id)
                    .limit(limit + 1)
                )
                if cursor is not None:
                    installed_at, row_id = cursor
                    stmt = stmt.where(
                        or_(
                            SqlInstalledSkill.installed_at > installed_at,
                            and_(
                                SqlInstalledSkill.installed_at == installed_at,
                                SqlInstalledSkill.id > row_id,
                            ),
                        )
                    )
                rows.extend(session.scalars(stmt).all())
            rows.sort(key=lambda row: (row.installed_at, row.id))
            page = rows[:limit]
            return SkillPage(
                data=[_skill_entity(row) for row in page],
                has_more=len(rows) > limit,
                last_id=_encode_cursor(page[-1].installed_at, page[-1].id) if page else None,
            )

    def find_for_user(self, user_id: str | None, names: Collection[str]) -> list[InstalledSkill]:
        """Point lookups on ``ux_installed_skills_identity``, one IN-list per 100 names."""
        found: list[InstalledSkill] = []
        with self._session("find_installed_skills") as session:
            for batch in _chunks(sorted(set(names)), _NAMES_PER_QUERY):
                stmt = select(SqlInstalledSkill).where(
                    SqlInstalledSkill.workspace_id == current_workspace_id(),
                    SqlInstalledSkill.principal.in_(_visible_principals(user_id)),
                    SqlInstalledSkill.name.in_(batch),
                )
                found.extend(_skill_entity(row) for row in session.scalars(stmt))
        return found

    def count_installed(self, scope: SkillScope, owner_user_id: str | None) -> int:
        """Rows of one principal, from the total its lock row keeps."""
        with self._session("count_installed_skills") as session:
            lock = session.get(
                SqlSkillPrincipal, (current_workspace_id(), principal_for(scope, owner_user_id))
            )
            return 0 if lock is None else lock.installed_count

    def update(
        self,
        skill_id: str,
        *,
        commit: str | None,
        bundle_location: str,
        bundle_bytes: int,
        bundle_entries: int,
        description: str,
        display_name: str | None,
        path: str | None = None,
        user_invocable: bool = True,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> InstallResult | None:
        max_bytes = MAX_DELIVERY_BYTES_PER_SCOPE if max_bytes is None else max_bytes
        max_entries = MAX_DELIVERY_ENTRIES_PER_SCOPE if max_entries is None else max_entries
        check_bound("display_name", display_name, MAX_DISPLAY_NAME_CHARS)
        check_bound("description", description, MAX_DESCRIPTION_CHARS)
        check_bound("bundle_location", bundle_location, MAX_LOCATION_CHARS)
        check_bound("path", path, MAX_PATH_CHARS)

        def write(session: Session) -> InstallResult | None:
            locked = self._locked_skill(session, skill_id)
            if locked is None:
                return None
            lock, row = locked
            replaced = row.bundle_location if row.bundle_location != bundle_location else None
            if row.enabled:
                after = lock.enabled_bytes - row.bundle_bytes + bundle_bytes
                entries_after = lock.enabled_entries - row.bundle_entries + bundle_entries
                _check_delivery_budget(
                    after,
                    entries_after,
                    max_bytes,
                    max_entries,
                    f"Updating {row.name}: the installed skills",
                )
                lock.enabled_bytes = after
                lock.enabled_entries = entries_after
            row.commit = commit
            row.bundle_location = bundle_location
            row.bundle_bytes = bundle_bytes
            row.bundle_entries = bundle_entries
            row.description = description
            row.display_name = display_name
            row.user_invocable = user_invocable
            if path is not None:
                row.path = path
            row.updated_at = now_epoch()
            session.flush()
            return InstallResult(skill=_skill_entity(row), replaced_location=replaced)

        return run_write_transaction(self._session_immediate, "update_installed_skill", write)

    def set_enabled(
        self,
        skill_id: str,
        enabled: bool,
        *,
        max_bytes: int | None = None,
        max_entries: int | None = None,
    ) -> InstalledSkill | None:
        max_bytes = MAX_DELIVERY_BYTES_PER_SCOPE if max_bytes is None else max_bytes
        max_entries = MAX_DELIVERY_ENTRIES_PER_SCOPE if max_entries is None else max_entries

        def write(session: Session) -> InstalledSkill | None:
            locked = self._locked_skill(session, skill_id)
            if locked is None:
                return None
            lock, row = locked
            if enabled and not row.enabled:
                after = lock.enabled_bytes + row.bundle_bytes
                entries_after = lock.enabled_entries + row.bundle_entries
                _check_delivery_budget(
                    after,
                    entries_after,
                    max_bytes,
                    max_entries,
                    f"Enabling {row.name}: the installed skills",
                )
                lock.enabled_bytes = after
                lock.enabled_entries = entries_after
            elif row.enabled and not enabled:
                lock.enabled_bytes -= row.bundle_bytes
                lock.enabled_entries -= row.bundle_entries
            row.enabled = enabled
            row.updated_at = now_epoch()
            session.flush()
            return _skill_entity(row)

        return run_write_transaction(self._session_immediate, "toggle_installed_skill", write)

    def delete(self, skill_id: str) -> InstalledSkill | None:
        def write(session: Session) -> InstalledSkill | None:
            locked = self._locked_skill(session, skill_id)
            if locked is None:
                return None
            lock, row = locked
            entity = _skill_entity(row)
            lock.installed_count -= 1
            if row.enabled:
                lock.enabled_bytes -= row.bundle_bytes
                lock.enabled_entries -= row.bundle_entries
            session.delete(row)
            session.flush()
            return entity

        return run_write_transaction(self._session_immediate, "delete_installed_skill", write)
