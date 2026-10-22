"""Transport contract for server-captured input authority.

These references travel outside tool arguments. Only an authenticated, bound
runner can attest which inputs its runtime consumed; the server resolves the
human and delegation ancestry from signed envelopes.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TypeGuard

INPUT_AUTHORITY_FIELD = "_omnigent_input_authority"
REQUEST_AUTHORITY_FIELD = "_omnigent_request_authority"
REQUEST_AUTHORITY_HEADER = "X-Omnigent-Request-Authority"
SOURCE_SESSION_HEADER = "X-Omnigent-Source-Session"
INPUT_PROOF_FIELD = "_omnigent_input_proof"
MAX_AUTHORITY_INPUTS = 32


@dataclass(frozen=True)
class RequestAuthority:
    """An immutable snapshot taken before a tool call crosses an await."""

    input_ids: tuple[str, ...]
    verified: bool = True
    proofs: tuple[str, ...] = ()

    def to_wire(self) -> dict[str, object]:
        return {
            "input_ids": list(self.input_ids),
            "verified": self.verified,
            "proofs": list(self.proofs),
        }

    @classmethod
    def from_wire(cls, value: object) -> RequestAuthority:
        if not isinstance(value, dict) or value.get("verified") is not True:
            return cls((), verified=False)
        ids = value.get("input_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_AUTHORITY_INPUTS:
            return cls((), verified=False)
        if any(not _valid_id(item) for item in ids) or len(set(ids)) != len(ids):
            return cls((), verified=False)
        proofs = value.get("proofs")
        if (
            not isinstance(proofs, list)
            or len(proofs) != len(ids)
            or any(not isinstance(proof, str) or not 1 <= len(proof) <= 2048 for proof in proofs)
        ):
            return cls((), verified=False)
        return cls(tuple(ids), proofs=tuple(proofs))

    @classmethod
    def from_input(cls, body: Mapping[str, object]) -> RequestAuthority:
        if REQUEST_AUTHORITY_FIELD in body:
            return cls.from_wire(body[REQUEST_AUTHORITY_FIELD])
        input_id = body.get(INPUT_AUTHORITY_FIELD)
        return cls.from_wire(
            {"input_ids": [input_id], "proofs": [body.get(INPUT_PROOF_FIELD)], "verified": True}
        )

    def consume(self, other: RequestAuthority) -> RequestAuthority:
        ids = tuple(dict.fromkeys((*self.input_ids, *other.input_ids)))
        if not self.verified or not other.verified or len(ids) > MAX_AUTHORITY_INPUTS:
            return RequestAuthority((), verified=False)
        proofs = dict(zip(self.input_ids, self.proofs, strict=True))
        for input_id, proof in zip(other.input_ids, other.proofs, strict=True):
            if input_id in proofs and proofs[input_id] != proof:
                return RequestAuthority((), verified=False)
            proofs[input_id] = proof
        return RequestAuthority(ids, proofs=tuple(proofs[input_id] for input_id in ids))


def _valid_id(value: object) -> TypeGuard[str]:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(character in "0123456789abcdef" for character in value)
    )


# Runner-only: nested dispatch inherits the invocation's immutable snapshot.
current_request_authority: ContextVar[RequestAuthority | None] = ContextVar(
    "current_request_authority", default=None
)
