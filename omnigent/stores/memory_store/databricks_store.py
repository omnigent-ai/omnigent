"""Managed Memory backend with server credentials and isolated actor partitions."""

from __future__ import annotations

import builtins
import hashlib
import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCategory, ErrorCode, OmnigentError
from omnigent.stores.memory_store import (
    DEFAULT_PAGE_SIZE,
    DEFAULT_SEARCH_LIMIT,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    MemoryActor,
    MemoryEntry,
    MemoryHit,
    MemoryPage,
    MemoryStore,
    path_in_prefix,
    validate_actor,
    validate_content,
    validate_limit,
    validate_path,
    validate_path_prefix,
)

if TYPE_CHECKING:
    from databricks.sdk.service.agentkit import ManagedMemoryEntry

    from omnigent.stores.memory_store.databricks_client import DatabricksManagedMemoryClient


@contextmanager
def _backend_errors(operation: str) -> Iterator[None]:
    from databricks.sdk.errors import BadRequest, PermissionDenied, Unauthenticated

    try:
        yield
    except OmnigentError:
        raise
    except PermissionDenied as exc:
        raise OmnigentError(
            "Managed Memory service identity lacks access to the configured store",
            code=ErrorCode.UPSTREAM_PERMISSION_DENIED,
        ) from exc
    except Unauthenticated as exc:
        raise OmnigentError(
            "Managed Memory server authentication failed",
            code=ErrorCode.INTERNAL_ERROR,
            category=ErrorCategory.CONFIG,
        ) from exc
    except BadRequest as exc:
        raise OmnigentError(
            f"Managed Memory rejected {operation}; check the request and pagination token",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    except Exception as exc:
        raise OmnigentError(
            f"Managed Memory backend failed during {operation}",
            code=ErrorCode.INTERNAL_ERROR,
            category=ErrorCategory.UPSTREAM,
        ) from exc


class DatabricksManagedMemoryStore(MemoryStore):
    """Map each (namespace, workspace, actor, path) to one cross-session entry.

    The namespace must be stable and unique to an OSS deployment sharing a remote
    store. It and the local workspace scope are included in opaque actor IDs.
    Only this adapter should write entries in those partitions.
    """

    supports_search = True

    def __init__(self, client: DatabricksManagedMemoryClient, *, namespace: str) -> None:
        if not isinstance(namespace, str) or not namespace.strip() or len(namespace) > 128:
            raise ValueError("Managed Memory namespace must contain 1-128 nonblank characters")
        self._client = client
        self._namespace = namespace

    def _actor_id(self, actor: MemoryActor) -> str:
        validate_actor(actor)
        identity = json.dumps(
            [self._namespace, current_workspace_id(), actor.type, actor.id],
            separators=(",", ":"),
        )
        return "omnigent-" + hashlib.sha256(identity.encode()).hexdigest()

    @staticmethod
    def _entry_id(actor_id: str, path: str) -> str:
        return "omnigent-" + hashlib.sha256(f"{actor_id}\0{path}".encode()).hexdigest()

    def _entry(
        self,
        actor: MemoryActor,
        actor_id: str,
        entry: ManagedMemoryEntry,
        *,
        with_content: bool = True,
        path: str | None = None,
    ) -> MemoryEntry:
        try:
            validate_path(entry.path)
        except OmnigentError as exc:
            raise RuntimeError("Managed Memory returned an invalid path") from exc
        expected_id = self._entry_id(actor_id, entry.path)
        if (
            entry.actor_id != actor_id
            or entry.session_id is not None
            or not entry.name
            or entry.name.rsplit("/", 1)[-1] != expected_id
            or (path is not None and entry.path != path)
        ):
            raise RuntimeError("Managed Memory returned an entry outside the requested partition")
        if entry.create_time is None or entry.update_time is None:
            raise RuntimeError("Managed Memory returned an entry without timestamps")
        if (with_content and not isinstance(entry.content, str)) or (
            entry.description is not None and not isinstance(entry.description, str)
        ):
            raise RuntimeError("Managed Memory returned invalid entry content")
        return MemoryEntry(
            actor=actor,
            path=entry.path,
            content=(entry.content or "") if with_content else "",
            description=entry.description or None,
            created_at=entry.create_time.seconds,
            updated_at=entry.update_time.seconds,
        )

    def write(
        self, actor: MemoryActor, path: str, content: str, description: str | None = None
    ) -> MemoryEntry:
        actor_id = self._actor_id(actor)
        validate_path(path)
        validate_content(content, description)
        from databricks.sdk.errors import NotFound, ResourceConflict
        from databricks.sdk.service.agentkit import (
            ManagedMemoryEntry,
            ManagedMemoryEntrySourceType,
        )

        entry_id = self._entry_id(actor_id, path)
        with _backend_errors("write"):
            # A competing create or delete can change existence between the read and write.
            for _ in range(3):
                try:
                    current = self._client.get_entry(entry_id)
                except NotFound:
                    try:
                        saved = self._client.create_entry(
                            ManagedMemoryEntry(
                                actor_id=actor_id,
                                path=path,
                                content=content,
                                description=description,
                                source_type=ManagedMemoryEntrySourceType.MANAGED_MEMORY_ENTRY_SOURCE_TYPE_AGENT,
                            ),
                            entry_id=entry_id,
                        )
                    except ResourceConflict:
                        continue
                else:
                    self._entry(actor, actor_id, current, path=path)
                    try:
                        saved = self._client.update_entry(
                            entry_id,
                            ManagedMemoryEntry.from_dict(
                                {"content": content, "description": description}
                            ),
                            update_mask="content,description",
                        )
                    except NotFound:
                        continue
                return self._entry(actor, actor_id, saved, path=path)
            raise OmnigentError(
                "Managed Memory write conflicted; retry the operation", code=ErrorCode.CONFLICT
            )

    def get(self, actor: MemoryActor, path: str) -> MemoryEntry | None:
        actor_id = self._actor_id(actor)
        validate_path(path)
        from databricks.sdk.errors import NotFound

        with _backend_errors("get"):
            try:
                entry = self._client.get_entry(self._entry_id(actor_id, path))
            except NotFound:
                # A missing store is a backend failure, not an empty memory partition.
                self._client.get_store()
                return None
            return self._entry(actor, actor_id, entry, path=path)

    def list(
        self,
        actor: MemoryActor,
        path_prefix: str | None = None,
        page_token: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> MemoryPage:
        actor_id = self._actor_id(actor)
        prefix = validate_path_prefix(path_prefix)
        validate_limit(page_size, MAX_PAGE_SIZE)
        from databricks.sdk.service.agentkit import ManagedMemoryEntry, ManagedMemoryEntryView

        with _backend_errors("list"):
            response = self._client.list_entries(
                actor_id,
                path_prefix=prefix,
                page_token=page_token,
                page_size=page_size,
                view=ManagedMemoryEntryView.MANAGED_MEMORY_ENTRY_VIEW_BASIC,
            )
            items = response.get("managed_memory_entries", [])
            token = response.get("next_page_token")
            if not isinstance(items, builtins.list) or len(items) > page_size:
                raise RuntimeError("Managed Memory returned an invalid page")
            if token is not None and not isinstance(token, str):
                raise RuntimeError("Managed Memory returned an invalid page token")
            entries = []
            for item in items:
                if not isinstance(item, dict):
                    raise RuntimeError("Managed Memory returned an invalid entry")
                entry = self._entry(
                    actor, actor_id, ManagedMemoryEntry.from_dict(item), with_content=False
                )
                if path_in_prefix(entry.path, prefix):
                    entries.append(entry)
            return MemoryPage(entries=entries, next_page_token=token or None)

    def search(
        self,
        actor: MemoryActor,
        query: str,
        path_prefix: str | None = None,
        limit: int = DEFAULT_SEARCH_LIMIT,
    ) -> builtins.list[MemoryHit]:
        actor_id = self._actor_id(actor)
        prefix = validate_path_prefix(path_prefix)
        validate_limit(limit, MAX_SEARCH_LIMIT)
        with _backend_errors("search"):
            results = self._client.search_entries(
                actor_id, query, path_prefix=prefix, page_size=limit
            )
            hits = []
            for result in results:
                if result.managed_memory_entry is None or result.score is None:
                    raise RuntimeError("Managed Memory returned an incomplete search result")
                if type(result.score) not in (int, float) or not math.isfinite(result.score):
                    raise RuntimeError("Managed Memory returned an invalid search score")
                entry = self._entry(actor, actor_id, result.managed_memory_entry)
                if path_in_prefix(entry.path, prefix):
                    hits.append(MemoryHit(entry, result.score))
            return hits[:limit]

    def delete(self, actor: MemoryActor, path: str) -> bool:
        actor_id = self._actor_id(actor)
        validate_path(path)
        from databricks.sdk.errors import NotFound

        with _backend_errors("delete"):
            try:
                entry_id = self._entry_id(actor_id, path)
                current = self._client.get_entry(entry_id)
                self._entry(actor, actor_id, current, path=path)
                self._client.delete_entry(entry_id)
            except NotFound:
                self._client.get_store()
                return False
            return True
