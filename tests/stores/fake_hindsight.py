"""An in-memory stand-in for ``hindsight_client.Hindsight``.

Implements only the calls :class:`HindsightMemoryStore` makes, with the same
shapes the real client returns, so the shared memory-store contract can run
against the Hindsight backend without a Hindsight server. Document-list items
are dicts, as in the 0.5-0.8 clients Omnigent locks, or models, as in 1.x. Recall is a simple
term match over the retained text, ranked by matches: Hindsight extracts facts
from retained text, so paths and descriptions do not influence it here either.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


class NotFoundException(Exception):
    """Mirrors ``hindsight_client_api.exceptions.NotFoundException``."""

    status = 404


@dataclass
class _Doc:
    id: str
    original_text: str
    document_metadata: dict[str, str]
    created_at: datetime
    updated_at: datetime


@dataclass
class _Fact:
    document_id: str
    text: str


@dataclass
class _Page:
    items: list[Any] = field(default_factory=list)
    total: int = 0


@dataclass
class _Recall:
    results: list[_Fact] = field(default_factory=list)


class _Documents:
    def __init__(self, owner: FakeHindsight) -> None:
        self._owner = owner

    async def get_document(self, bank_id: str, document_id: str) -> _Doc:
        self._owner.calls.append(("get_document", bank_id, document_id))
        try:
            return self._owner.banks[bank_id][document_id]
        except KeyError:
            raise NotFoundException() from None

    async def list_documents(
        self,
        bank_id: str,
        q: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _Page:
        self._owner.calls.append(("list_documents", bank_id, q))
        docs = list(self._owner.banks.get(bank_id, {}).values())
        if q:
            docs = [d for d in docs if q.lower() in d.id.lower()]
        docs.sort(key=lambda d: d.updated_at, reverse=True)
        start = offset or 0
        limit = min(limit or 100, self._owner.list_limit_cap or 100)
        page = docs[start : start + limit]
        if self._owner.list_items_as_models:
            return _Page(items=page, total=len(docs))
        return _Page(
            total=len(docs),
            items=[
                {
                    "id": d.id,
                    "created_at": d.created_at.isoformat(),
                    "updated_at": d.updated_at.isoformat(),
                    "document_metadata": dict(d.document_metadata),
                }
                for d in page
            ],
        )

    async def delete_document(self, bank_id: str, document_id: str) -> None:
        self._owner.calls.append(("delete_document", bank_id, document_id))
        if document_id not in self._owner.banks.get(bank_id, {}):
            raise NotFoundException()
        del self._owner.banks[bank_id][document_id]


class FakeHindsight:
    """Banks of documents keyed by bank id then document id."""

    def __init__(
        self,
        *,
        fail_create_bank: bool = False,
        list_items_as_models: bool = False,
        list_limit_cap: int | None = None,
    ) -> None:
        """
        :param list_limit_cap: Most documents one list call returns, whatever
            ``limit`` asks for, as a server with a smaller page cap would.
        """
        self.banks: dict[str, dict[str, _Doc]] = {}
        self.list_items_as_models = list_items_as_models
        self.list_limit_cap = list_limit_cap
        self.calls: list[tuple[Any, ...]] = []
        self.retains: list[dict[str, Any]] = []
        self._fail_create_bank = fail_create_bank
        self._tick = 0
        self.documents = _Documents(self)

    def _now(self) -> datetime:
        self._tick += 1
        return datetime.fromtimestamp(1_700_000_000 + self._tick, tz=UTC)

    async def acreate_bank(self, bank_id: str, name: str | None = None) -> None:
        self.calls.append(("create_bank", bank_id))
        if self._fail_create_bank or bank_id in self.banks:
            raise RuntimeError("bank already exists")
        self.banks[bank_id] = {}

    async def aretain(
        self,
        bank_id: str,
        content: str,
        document_id: str | None = None,
        update_mode: str | None = None,
        metadata: dict[str, str] | None = None,
        **_: Any,
    ) -> None:
        self.retains.append(
            {"bank_id": bank_id, "document_id": document_id, "update_mode": update_mode}
        )
        assert document_id is not None
        bank = self.banks.setdefault(bank_id, {})
        now = self._now()
        existing = bank.get(document_id)
        bank[document_id] = _Doc(
            id=document_id,
            original_text=content,
            document_metadata=dict(metadata or {}),
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    async def arecall(self, bank_id: str, query: str, **_: Any) -> _Recall:
        self.calls.append(("recall", bank_id, query))
        if bank_id not in self.banks:
            raise NotFoundException()
        terms = [t.lower() for t in re.findall(r"\w+", query)]
        scored = []
        for doc in self.banks[bank_id].values():
            hits = sum(term in doc.original_text.lower() for term in terms)
            if hits:
                scored.append((hits, doc))
        scored.sort(key=lambda pair: -pair[0])
        return _Recall(results=[_Fact(document_id=d.id, text=d.original_text) for _, d in scored])
