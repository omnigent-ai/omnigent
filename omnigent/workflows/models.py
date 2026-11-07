"""Portable definitions for the dynamic workflow prototype."""

from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class Definition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConnectedHost(Definition):
    kind: Literal["host"] = "host"
    host_id: Text
    workspace: Text


class ManagedSandbox(Definition):
    kind: Literal["sandbox"] = "sandbox"
    provider: Text
    workspace: Text | None = None


class TeamMember(Definition):
    id: Identifier
    role: Text
    agent_id: Text
    placement: Annotated[ConnectedHost | ManagedSandbox, Field(discriminator="kind")]


class AgentTeam(Definition):
    id: Identifier
    lead: Identifier
    members: tuple[TeamMember, ...] = Field(min_length=1, max_length=16)
    strategy: Literal["parallel", "owner"] = "parallel"

    @model_validator(mode="after")
    def validate_members(self) -> Self:
        identifiers = [member.id for member in self.members]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError(f"Team {self.id!r} has duplicate member IDs")
        if self.lead not in identifiers:
            raise ValueError(f"Team {self.id!r} has unknown lead {self.lead!r}")
        return self


class Artifact(Definition):
    """A reference to an immutable revision; no files are transferred by the controller."""

    id: Identifier
    kind: Text
    uri: Text
    revision: Text


class Gate(Definition):
    id: Identifier
    artifacts: tuple[Identifier, ...] = ()


class ExternalGate(Gate):
    kind: Literal["external"] = "external"


class CommandGate(Gate):
    kind: Literal["command"] = "command"
    command: tuple[Text, ...] = Field(min_length=1)
    cwd: Text
    timeout_seconds: float = Field(default=300, gt=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_directory(self) -> Self:
        if not Path(self.cwd).is_absolute():
            raise ValueError("Command gate cwd must be absolute on the controller host")
        return self


class WorkflowNode(Definition):
    id: Identifier
    name: Text | None = None
    goal: Text
    team: Identifier
    depends_on: tuple[Identifier, ...] = ()
    allow_expansion: bool = False
    parent: Identifier | None = None
    gates: tuple[Annotated[CommandGate | ExternalGate, Field(discriminator="kind")], ...] = ()
    max_rounds: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def validate_gates(self) -> Self:
        if len({gate.id for gate in self.gates}) != len(self.gates):
            raise ValueError("Duplicate gate IDs")
        return self


class NodeResult(Definition):
    summary: Text
    add_nodes: tuple[WorkflowNode, ...] = ()
    status: Literal["ready", "waiting", "consult"] = "ready"
    artifacts: tuple[Artifact, ...] = ()
    outputs: dict[Identifier, JsonValue] = Field(default_factory=dict)
    consult_members: tuple[Identifier, ...] = ()

    @model_validator(mode="after")
    def validate_result(self) -> Self:
        if len(json.dumps(self.outputs, allow_nan=False).encode()) > 65536:
            raise ValueError("Task outputs exceed 64 KiB; publish large content as an artifact")
        if len({artifact.id for artifact in self.artifacts}) != len(self.artifacts):
            raise ValueError("Duplicate artifact IDs")
        if (self.status == "consult") != bool(self.consult_members):
            raise ValueError(
                "consult requires consult_members; other statuses cannot request them"
            )
        if len(set(self.consult_members)) != len(self.consult_members):
            raise ValueError("Duplicate specialist requests")
        if self.status != "ready" and self.add_nodes:
            raise ValueError("Only a ready candidate may propose graph expansion")
        return self


class Workflow(Definition):
    name: Text
    goal: Text
    teams: tuple[AgentTeam, ...] = Field(min_length=1)
    nodes: tuple[WorkflowNode, ...] = Field(min_length=1)
    max_nodes: int = Field(default=64, ge=1, le=256)
    max_parallel_members: int = Field(default=4, ge=1, le=32)
    member_timeout_seconds: float = Field(default=1800, gt=0, le=86400, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_graph(self) -> Self:
        team_ids = {team.id for team in self.teams}
        if len(team_ids) != len(self.teams):
            raise ValueError("Duplicate team IDs")
        node_ids = {node.id for node in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("Duplicate node IDs")
        if len(self.nodes) > self.max_nodes:
            raise ValueError(f"Workflow exceeds max_nodes={self.max_nodes}")
        for node in self.nodes:
            if node.team not in team_ids:
                raise ValueError(f"Node {node.id!r} has unknown team {node.team!r}")
            unknown = set(node.depends_on) - node_ids
            if unknown:
                raise ValueError(f"Node {node.id!r} has unknown dependencies: {sorted(unknown)}")
            if len(set(node.depends_on)) != len(node.depends_on):
                raise ValueError(f"Node {node.id!r} has duplicate dependencies")
            if node.parent is not None and node.parent not in node_ids:
                raise ValueError(f"Node {node.id!r} has unknown parent {node.parent!r}")
        try:
            TopologicalSorter({node.id: node.depends_on for node in self.nodes}).prepare()
            TopologicalSorter(
                {node.id: (node.parent,) if node.parent else () for node in self.nodes}
            ).prepare()
        except CycleError as exc:
            raise ValueError("Workflow dependencies contain a cycle") from exc
        return self

    def expand(self, source_id: str, additions: tuple[WorkflowNode, ...]) -> Workflow:
        """Insert validated work after a source and before its existing dependants."""
        source = next((node for node in self.nodes if node.id == source_id), None)
        if source is None:
            raise ValueError(f"Unknown expansion source {source_id!r}")
        if not source.allow_expansion:
            raise ValueError(f"Node {source_id!r} does not allow expansion")
        if not additions:
            return self
        existing_ids = {node.id for node in self.nodes}
        added_ids = tuple(node.id for node in additions)
        if existing_ids.intersection(added_ids):
            raise ValueError("Expansion cannot replace existing nodes")
        inserted = tuple(
            node.model_copy(
                update={
                    "depends_on": tuple(dict.fromkeys((source_id, *node.depends_on))),
                    "parent": node.parent or source_id,
                }
            )
            for node in additions
        )
        rewired = tuple(
            node.model_copy(update={"depends_on": (*node.depends_on, *added_ids)})
            if source_id in node.depends_on
            else node
            for node in self.nodes
        )
        # model_copy alone skips validation, including cycle and graph-size checks.
        return Workflow.model_validate({**self.model_dump(), "nodes": (*rewired, *inserted)})
