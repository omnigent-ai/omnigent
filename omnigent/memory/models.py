from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from omnigent.spec.types import MemoryProviderName, MemoryScopeKind


@dataclass(frozen=True)
class MemoryScope:
    workspace_id: int
    kind: MemoryScopeKind
    subject_id: str | None = None

    def __post_init__(self) -> None:
        if self.workspace_id < 0:
            raise ValueError("workspace_id must be non-negative")
        if self.kind == "org":
            if self.subject_id is not None:
                raise ValueError("org memory scope must not have a subject_id")
            return
        if not self.subject_id or not self.subject_id.strip():
            raise ValueError(f"{self.kind} memory scope requires a subject_id")

    @property
    def key(self) -> str:
        if self.kind == "org":
            return f"{self.workspace_id}:org"
        segment = "user" if self.kind == "personal" else self.kind
        return f"{self.workspace_id}:{segment}:{quote(self.subject_id or '', safe='')}"


@dataclass(frozen=True)
class MemoryTurnContext:
    operation_id: str
    workspace_id: int
    account_subject: str
    conversation_id: str
    query: str
    turn_id: str | None = None

    def resolve_scopes(self, kinds: list[MemoryScopeKind]) -> tuple[MemoryScope, ...]:
        scopes: list[MemoryScope] = []
        for kind in kinds:
            if kind == "personal":
                scopes.append(MemoryScope(self.workspace_id, kind, self.account_subject))
            elif kind == "conversation":
                scopes.append(MemoryScope(self.workspace_id, kind, self.conversation_id))
            elif kind == "org":
                scopes.append(MemoryScope(self.workspace_id, kind))
        return tuple(dict.fromkeys(scopes))


@dataclass(frozen=True)
class RetrievalResult:
    provider: MemoryProviderName
    scope: MemoryScope
    text: str
    score: float | None = None
    record_id: str | None = None
    version: str | None = None
    source_title: str | None = None
    source_uri: str | None = None
    snapshot_sha: str | None = None
    sensitivity: str = "internal"


@dataclass(frozen=True)
class MemoryRecallRequest:
    context: MemoryTurnContext
    scopes: tuple[MemoryScope, ...]
    max_results: int
    max_chars: int


@dataclass(frozen=True)
class MemoryRecallFailure:
    provider: MemoryProviderName
    reason: str
    timed_out: bool = False


@dataclass(frozen=True)
class MemoryRecall:
    results: tuple[RetrievalResult, ...] = ()
    failures: tuple[MemoryRecallFailure, ...] = ()
    should_inject: bool = False
