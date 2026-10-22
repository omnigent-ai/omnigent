"""Transport contract for server-captured input authority.

These references travel outside tool arguments. Only an authenticated, bound
runner can attest which inputs its runtime consumed; the server resolves the
human and delegation ancestry from immutable records.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeGuard

INPUT_AUTHORITY_FIELD = "_omnigent_input_authority"
REQUEST_AUTHORITY_FIELD = "_omnigent_request_authority"
REQUEST_AUTHORITY_HEADER = "X-Omnigent-Request-Authority"
SOURCE_SESSION_HEADER = "X-Omnigent-Source-Session"
LINKED_REQUEST_FIELD = "_omnigent_linked_request"
MAX_AUTHORITY_INPUTS = 32
DELIVERY_LEASE_SECONDS = 20


def may_replay_session_item(item: Mapping[str, object]) -> bool:
    """Linked history needs a server acceptance receipt; this grants no tool authority."""
    return not item.get("linked_request_id") or item.get("linked_input_accepted") is True


@dataclass(frozen=True)
class RequestAuthority:
    """An immutable snapshot taken before a tool call crosses an await."""

    input_ids: tuple[str, ...]
    verified: bool = True

    def to_wire(self) -> dict[str, object]:
        return {"input_ids": list(self.input_ids), "verified": self.verified}

    @classmethod
    def from_wire(cls, value: object) -> RequestAuthority:
        if not isinstance(value, dict) or value.get("verified") is not True:
            return cls((), verified=False)
        ids = value.get("input_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_AUTHORITY_INPUTS:
            return cls((), verified=False)
        if any(not _valid_id(item) for item in ids) or len(set(ids)) != len(ids):
            return cls((), verified=False)
        return cls(tuple(ids))

    @classmethod
    def from_input(cls, body: Mapping[str, object]) -> RequestAuthority:
        if REQUEST_AUTHORITY_FIELD in body:
            return cls.from_wire(body[REQUEST_AUTHORITY_FIELD])
        input_id = body.get(INPUT_AUTHORITY_FIELD)
        return cls((input_id,)) if _valid_id(input_id) else cls((), verified=False)

    def consume(self, other: RequestAuthority) -> RequestAuthority:
        ids = tuple(dict.fromkeys((*self.input_ids, *other.input_ids)))
        if not self.verified or not other.verified or len(ids) > MAX_AUTHORITY_INPUTS:
            return RequestAuthority((), verified=False)
        return RequestAuthority(ids)


def _valid_id(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


# Used only within the server after verifying the runner and immutable inputs.
# It also disables the permission store's positive cache on delegated reads.
requester_authority_active: ContextVar[bool] = ContextVar(
    "requester_authority_active", default=False
)

# Runner-only: child dispatch tasks inherit the snapshot taken when the call starts.
current_request_authority: ContextVar[RequestAuthority | None] = ContextVar(
    "current_request_authority", default=None
)


orchestration_enabled: ContextVar[bool] = ContextVar("orchestration_enabled", default=False)


@contextmanager
def orchestration_scope(enabled: bool) -> Iterator[None]:
    """Keep unsupported deployments out of the orchestration schema, including background work."""
    token = orchestration_enabled.set(enabled)
    try:
        yield
    finally:
        orchestration_enabled.reset(token)
