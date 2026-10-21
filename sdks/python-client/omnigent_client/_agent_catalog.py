"""Shared catalog validation and pagination for synchronous and async clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ._errors import OmnigentError


@dataclass(frozen=True)
class RegisteredAgent:
    """A registered agent's durable id and optional harness."""

    id: str
    harness: str | None = None


@dataclass(frozen=True, kw_only=True)
class AgentCatalogEntry(RegisteredAgent):
    """A selectable registration, including its display name and catalog source."""

    name: str
    source: str


class RegisteredAgentCatalog:
    """Accumulate validated pages, server first, with optional early name lookup.

    The caller fetches ``params`` until ``done``. Keeping transport outside
    lets the synchronous CLI and async SDK share one selection policy.

    :param stop_at: Stop after a complete page containing this name.
    """

    def __init__(self, *, stop_at: str | None = None) -> None:
        self.entries: dict[str, AgentCatalogEntry] = {}
        self.params: dict[str, Any] = {"limit": 1000}
        self.done = False
        self._stop_at = stop_at
        self._cursors: set[str] = set()

    def skip_unavailable_scope(self, status: int | None) -> bool:
        """Only optional user-catalog compatibility errors end discovery quietly."""
        if self.params.get("scope") == "user" and status in (401, 403, 404, 422):
            self.entries = {
                name: entry for name, entry in self.entries.items() if entry.source == "server"
            }
            self.done = True
            return True
        return False

    def add_page(self, page: dict[str, Any]) -> None:
        """Validate a whole page before choosing an entry or advancing its cursor."""
        rows = page.get("data")
        if not isinstance(rows, list):
            raise OmnigentError("returned an unexpected body shape")
        source = self.params.get("scope", "server")
        for row in rows:
            if not isinstance(row, dict) or any(
                not isinstance(row.get(key), str) or not row[key].strip() for key in ("id", "name")
            ):
                raise OmnigentError(
                    "returned a malformed agent row (expected non-empty string id and name)"
                )
            self.entries.setdefault(
                row["name"],
                AgentCatalogEntry(
                    id=row["id"],
                    name=row["name"],
                    source=source,
                    harness=row.get("harness") if isinstance(row.get("harness"), str) else None,
                ),
            )
        if self._stop_at is not None and self._stop_at in self.entries:
            self.done = True
        elif page.get("has_more"):
            cursor = page.get("last_id")
            if not isinstance(cursor, str) or not cursor.strip() or cursor in self._cursors:
                raise OmnigentError("agent listing stalled: no usable cursor to fetch more agents")
            self._cursors.add(cursor)
            self.params["after"] = cursor
        elif source == "server":
            self.params = {"limit": 1000, "scope": "user"}
            self._cursors.clear()
        else:
            self.done = True
