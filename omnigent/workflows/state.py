"""Checkpointed node ownership, turns, candidates, and input events."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter

from omnigent.workflows.models import Artifact, Definition, Identifier, NodeResult, Text, Workflow

Status = Literal[
    "pending", "running", "waiting", "interrupted", "succeeded", "failed", "blocked", "cancelled"
]
Phase = Literal["contribute", "synthesize", "work"]


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TurnRun(State):
    id: str
    phase: Phase
    prompt: str
    status: Literal["running", "uncertain", "succeeded", "failed"] = "running"
    output: str | None = None
    error: str | None = None
    remote_id: str | None = None


class MemberRun(State):
    status: Status = "pending"
    phase: Phase = "contribute"
    session_id: str | None = None
    output: str | None = None
    error: str | None = None
    cleanup_error: str | None = None
    turns: dict[str, TurnRun] = Field(default_factory=dict)


class GateOutcome(Definition):
    passed: bool = Field(strict=True)
    detail: str
    evidence: Text | None = None


class GateRun(State):
    gate_id: str
    candidate_id: str
    outcome: GateOutcome


class NodeRun(State):
    status: Status = "pending"
    summary: str | None = None
    error: str | None = None
    members: dict[str, MemberRun] = Field(default_factory=dict)
    stage: Literal["contribute", "owner", "validate"] = "contribute"
    round: int = 1
    specialists: list[str] | None = None
    contributions: dict[str, str] = Field(default_factory=dict)
    feedback: list[str] = Field(default_factory=list)
    candidate_id: str | None = None
    candidate: NodeResult | None = None
    candidates: dict[str, NodeResult] = Field(default_factory=dict)
    gate_results: list[GateRun] = Field(default_factory=list)
    artifacts: tuple[Artifact, ...] = ()
    outputs: dict[str, JsonValue] = Field(default_factory=dict)


class Event(Definition):
    id: Text
    node_id: Identifier


class FeedbackEvent(Event):
    kind: Literal["feedback"] = "feedback"
    message: Text


class GateEvent(Event):
    kind: Literal["gate_result"] = "gate_result"
    candidate_id: Text
    gate_id: Identifier
    outcome: GateOutcome


class ResolveTurnEvent(Event):
    """A caller confirms an uncertain turn's output after inspecting the remote session."""

    kind: Literal["resolve_turn"] = "resolve_turn"
    member_id: Identifier
    turn_id: Text
    output: Text
    session_id: Text | None = None


InputEvent = Annotated[FeedbackEvent | GateEvent | ResolveTurnEvent, Field(discriminator="kind")]
EVENT_ADAPTER: TypeAdapter[InputEvent] = TypeAdapter(InputEvent)


class EventRecord(State):
    event: InputEvent
    status: Literal["pending", "applied", "rejected"] = "pending"
    error: str | None = None


class ExecutionBinding(Definition):
    mode: Literal["custom", "simulation", "server", "arclet"] = "custom"
    server: str | None = None


class Checkpoint(State):
    schema_version: Literal[2] = 2
    run_id: str
    status: Status
    workflow: Workflow
    nodes: dict[str, NodeRun]
    events: list[EventRecord]
    binding: ExecutionBinding
