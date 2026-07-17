"""Public contracts for opt-in session-event admission.

The admission seam lets an integration observe the runner's atomic
turn-versus-buffer decision before a user message is persisted.  It is
deliberately narrow: Omnigent still owns turn sequencing and the integration
only decides whether a session should use the correlated path.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias

AdmissionDisposition: TypeAlias = Literal[
    "new_turn",
    "active_steer",
    "next_turn_buffer",
]


@dataclass(frozen=True)
class AdmissionInfo:
    """Atomic runner decision associated with one user input."""

    admission_id: str
    input_seq: int
    disposition: AdmissionDisposition
    lineage_id: str
    active_response_id: str | None = None

    @classmethod
    def from_runner_payload(cls, payload: Mapping[str, object]) -> AdmissionInfo:
        """Parse the runner reservation response, failing on malformed data."""
        admission_id = payload.get("admissionId")
        input_seq = payload.get("inputSeq")
        disposition = payload.get("disposition")
        lineage_id = payload.get("lineageId")
        active_response_id = payload.get("activeResponseId")
        if not isinstance(admission_id, str) or not admission_id:
            raise ValueError("runner admission response has no admissionId")
        if not isinstance(input_seq, int) or isinstance(input_seq, bool) or input_seq < 0:
            raise ValueError("runner admission response has an invalid inputSeq")
        if disposition not in ("new_turn", "active_steer", "next_turn_buffer"):
            raise ValueError("runner admission response has an invalid disposition")
        if not isinstance(lineage_id, str) or not lineage_id:
            raise ValueError("runner admission response has no lineageId")
        if active_response_id is not None and not isinstance(active_response_id, str):
            raise ValueError("runner admission response has an invalid activeResponseId")
        return cls(
            admission_id=admission_id,
            input_seq=input_seq,
            disposition=disposition,
            lineage_id=lineage_id,
            active_response_id=active_response_id,
        )

    def to_dict(self) -> dict[str, object]:
        """Return the stable snake-case shape used by policies and acks."""
        return {
            "admission_id": self.admission_id,
            "input_seq": self.input_seq,
            "disposition": self.disposition,
            "lineage_id": self.lineage_id,
            "active_response_id": self.active_response_id,
        }


@dataclass(frozen=True)
class SessionInfo:
    """Public session snapshot passed to :meth:`SessionEventAdmitter.wants`."""

    id: str
    agent_id: str | None
    harness: str | None
    labels: Mapping[str, str]
    parent_session_id: str | None = None


class SessionEventAdmitter(Protocol):
    """Select sessions whose user messages require atomic admission."""

    async def wants(self, session: SessionInfo) -> bool:
        """Return ``True`` to use reserve-policy-consume for *session*."""


EventAdmittedCallback: TypeAlias = Callable[
    [str, str | None, AdmissionInfo],
    Awaitable[None] | None,
]


__all__ = [
    "AdmissionDisposition",
    "AdmissionInfo",
    "EventAdmittedCallback",
    "SessionEventAdmitter",
    "SessionInfo",
]
