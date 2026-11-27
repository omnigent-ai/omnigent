"""A permission store that keeps gadget grants in memory, for tests of gadget sharing.

The SQL store keeps no gadget grants yet, so sharing is exercised against this one; the
rest of the store (accounts, groups, sessions) is the real SQL store.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnigent.entities import GadgetPermission
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import session_sharing
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC
from omnigent.stores.permission_store import MAX_SHARED_GADGETS_PAGE_SIZE
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

# Grants per database, so every store a test opens on one database sees the same grants.
# Cleared after every test (see ``tests/conftest.py``): a shared test database keeps its URI
# from test to test while its tables are emptied.
_GRANTS_BY_DATABASE: dict[str, dict[str, dict[str, int]]] = {}


def reset_gadget_grants() -> None:
    """Forget every grant, as the test database's tables are emptied between tests."""
    _GRANTS_BY_DATABASE.clear()


class GadgetGrantStore(SqlAlchemyPermissionStore):
    """:class:`SqlAlchemyPermissionStore` plus in-memory gadget grants."""

    def __init__(self, *args, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._gadget_grants = _GRANTS_BY_DATABASE.setdefault(self.storage_location, {})

    @property
    def supports_gadget_grants(self) -> bool:
        return True

    def grant_for_gadget(
        self, user_id: str, gadget_id: str, level: int, *, ensure_minimum: bool = False
    ) -> GadgetPermission:
        if user_id in (RESERVED_USER_PUBLIC, RESERVED_USER_LOCAL):
            raise OmnigentError("Gadgets are never public", code=ErrorCode.INVALID_INPUT)
        if level not in session_sharing.GADGET_GRANT_LEVELS:
            raise OmnigentError("Invalid level", code=ErrorCode.INVALID_INPUT)
        grants = self._gadget_grants.setdefault(gadget_id, {})
        stored = max(level, grants.get(user_id, level)) if ensure_minimum else level
        grants[user_id] = stored
        return GadgetPermission(user_id=user_id, gadget_id=gadget_id, level=stored)

    def revoke_for_gadget(self, user_id: str, gadget_id: str) -> bool:
        return self._gadget_grants.get(gadget_id, {}).pop(user_id, None) is not None

    def list_for_gadget(
        self, gadget_id: str, *, limit: int = 100, after_user_id: str | None = None
    ) -> tuple[list[GadgetPermission], str | None]:
        grants = sorted(self._gadget_grants.get(gadget_id, {}).items())
        rows = [item for item in grants if after_user_id is None or item[0] > after_user_id]
        page = rows[:limit]
        cursor = page[-1][0] if len(rows) > limit else None
        return [GadgetPermission(user, gadget_id, level) for user, level in page], cursor

    def resolve_gadget_access(self, principals: Sequence[str], gadget_id: str) -> int | None:
        grants = self._gadget_grants.get(gadget_id, {})
        levels = [grants[p] for p in principals if p in grants]
        return max(levels) if levels else None

    def list_gadgets_shared_with(
        self,
        principals: Sequence[str],
        *,
        limit: int = 50,
        after_gadget_id: str | None = None,
    ) -> tuple[list[str], str | None]:
        if not 1 <= limit <= MAX_SHARED_GADGETS_PAGE_SIZE:
            raise OmnigentError("Invalid limit", code=ErrorCode.INVALID_INPUT)
        wanted = set(principals)
        ids = sorted(
            gadget_id
            for gadget_id, grants in self._gadget_grants.items()
            if wanted & grants.keys() and (after_gadget_id is None or gadget_id > after_gadget_id)
        )
        page = ids[:limit]
        return page, (page[-1] if len(ids) > limit else None)

    def delete_for_gadget(self, gadget_id: str) -> int:
        return len(self._gadget_grants.pop(gadget_id, {}))
