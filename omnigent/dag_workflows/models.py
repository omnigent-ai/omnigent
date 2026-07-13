"""Serializable models and validation for the workflow DAG runtime."""

from __future__ import annotations

import hashlib
import json
import time
from enum import StrEnum
from typing import Any, Literal

import jsonschema
from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    BLOCKED = "blocked"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class WorkflowRuntimeConfig(BaseModel):
    """Spec-level hard limits for workflows created by one agent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_concurrency: int = Field(default=6, ge=1, le=64)
    max_nodes: int = Field(default=100, ge=1, le=1000)
    max_dispatches: int = Field(default=100, ge=1, le=10_000)
    max_cost_usd: float | None = Field(default=None, gt=0)


class WorkflowBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrency: int = Field(default=4, ge=1)
    max_dispatches: int = Field(default=100, ge=1)
    max_cost_usd: float | None = Field(default=None, gt=0)


class NodeCostBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_cost_usd: float = Field(gt=0)


class WorkflowNode(BaseModel):
    """One statically declared child-agent execution in a workflow DAG."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    role: Literal["investigate", "implement", "review", "generic"] = "generic"
    title: str = Field(min_length=1, max_length=120)
    contract: str = Field(min_length=1, max_length=100_000)
    deps: list[str] = Field(default_factory=list)
    agent: str = Field(min_length=1, max_length=96)
    model: str | None = Field(default=None, min_length=1, max_length=256)
    output_schema: dict[str, Any] = Field(default_factory=lambda: {"type": "object"})
    max_attempts: int = Field(default=2, ge=1, le=10)
    cost_budget: NodeCostBudget | None = None
    worktree_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def _validate_node(self) -> WorkflowNode:
        if self.id in self.deps:
            raise ValueError(f"node {self.id!r} cannot depend on itself")
        if len(self.deps) != len(set(self.deps)):
            raise ValueError(f"node {self.id!r} has duplicate dependencies")
        try:
            jsonschema.Draft202012Validator.check_schema(self.output_schema)
        except jsonschema.SchemaError as exc:
            raise ValueError(f"node {self.id!r} has invalid output_schema: {exc.message}") from exc
        return self


class WorkflowDefinition(BaseModel):
    """Immutable, versioned graph authored by Polly."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=96, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    name: str = Field(min_length=1, max_length=160)
    version: int = Field(default=1, ge=1)
    budget: WorkflowBudget = Field(default_factory=WorkflowBudget)
    nodes: list[WorkflowNode] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_graph(self) -> WorkflowDefinition:
        by_id = {node.id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("workflow node IDs must be unique")
        for node in self.nodes:
            missing = [dep for dep in node.deps if dep not in by_id]
            if missing:
                raise ValueError(f"node {node.id!r} has missing dependencies: {missing}")
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if node_id in visiting:
                raise ValueError(f"workflow graph contains a cycle through {node_id!r}")
            if node_id in visited:
                return
            visiting.add(node_id)
            for dep in by_id[node_id].deps:
                visit(dep)
            visiting.remove(node_id)
            visited.add(node_id)

        for node_id in by_id:
            visit(node_id)
        if self.budget.max_cost_usd is not None:
            without_reservation = [node.id for node in self.nodes if node.cost_budget is None]
            if without_reservation:
                raise ValueError(
                    "nodes require cost_budget when workflow budget.max_cost_usd is set: "
                    f"{without_reservation}"
                )
        return self

    @property
    def definition_hash(self) -> str:
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


class NodeAttempt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(ge=1)
    work_id: str | None = None
    child_session_id: str | None = None
    status: str = "launching"
    error: str | None = None
    started_at: float = Field(default_factory=time.time)
    completed_at: float | None = None


class WorkflowNodeRun(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    state: NodeState = NodeState.PENDING
    attempts: list[NodeAttempt] = Field(default_factory=list)
    child_session_id: str | None = None
    result: Any = None
    error: str | None = None
    reserved_cost_usd: float = 0.0

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)


class WorkflowRun(BaseModel):
    """Mutable execution state stored behind the workflow store interface."""

    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    parent_session_id: str
    definition: WorkflowDefinition
    definition_hash: str
    status: WorkflowStatus = WorkflowStatus.DRAFT
    nodes: dict[str, WorkflowNodeRun]
    seen_work_ids: set[str] = Field(default_factory=set)
    dispatch_count: int = 0
    spent_cost_usd: float = 0.0
    reserved_cost_usd: float = 0.0
    child_cost_usd: dict[str, float] = Field(default_factory=dict)
    blocked_reason: str | None = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    terminal_wake_sent: bool = False

    @classmethod
    def from_definition(
        cls, parent_session_id: str, definition: WorkflowDefinition
    ) -> WorkflowRun:
        return cls(
            workflow_id=definition.id,
            parent_session_id=parent_session_id,
            definition=definition,
            definition_hash=definition.definition_hash,
            nodes={node.id: WorkflowNodeRun(node_id=node.id) for node in definition.nodes},
        )

    def summary(self) -> dict[str, Any]:
        by_id = {node.id: node for node in self.definition.nodes}
        return {
            "workflow_id": self.workflow_id,
            "name": self.definition.name,
            "version": self.definition.version,
            "definition_hash": self.definition_hash,
            "status": self.status.value,
            "blocked_reason": self.blocked_reason,
            "dispatch_count": self.dispatch_count,
            "spent_cost_usd": self.spent_cost_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "nodes": [
                {
                    "id": node_id,
                    "title": by_id[node_id].title,
                    "role": by_id[node_id].role,
                    "deps": by_id[node_id].deps,
                    "agent": by_id[node_id].agent,
                    "state": node_run.state.value,
                    "attempt_count": node_run.attempt_count,
                    "child_session_id": node_run.child_session_id,
                    "result": node_run.result,
                    "error": node_run.error,
                }
                for node_id, node_run in self.nodes.items()
            ],
        }


def validate_definition_limits(
    definition: WorkflowDefinition,
    limits: WorkflowRuntimeConfig,
) -> None:
    """Validate a workflow definition against agent-configured hard limits."""

    if not limits.enabled:
        raise ValueError("workflows are disabled for this agent")
    if len(definition.nodes) > limits.max_nodes:
        raise ValueError(
            f"workflow has {len(definition.nodes)} nodes; configured maximum is {limits.max_nodes}"
        )
    if definition.budget.max_concurrency > limits.max_concurrency:
        raise ValueError(
            f"workflow max_concurrency {definition.budget.max_concurrency} exceeds configured "
            f"maximum {limits.max_concurrency}"
        )
    if definition.budget.max_dispatches > limits.max_dispatches:
        raise ValueError(
            f"workflow max_dispatches {definition.budget.max_dispatches} exceeds configured "
            f"maximum {limits.max_dispatches}"
        )
    if limits.max_cost_usd is not None:
        if definition.budget.max_cost_usd is None:
            raise ValueError("workflow must declare budget.max_cost_usd")
        if definition.budget.max_cost_usd > limits.max_cost_usd:
            raise ValueError(
                f"workflow max_cost_usd {definition.budget.max_cost_usd} exceeds configured "
                f"maximum {limits.max_cost_usd}"
            )
