"""Deterministic coordination of node owners, feedback, validation, and graph expansion."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from omnigent.workflows.gates import GateRequest, execute_command_gate
from omnigent.workflows.models import (
    CommandGate,
    ExternalGate,
    NodeResult,
    TeamMember,
    Workflow,
    WorkflowNode,
)
from omnigent.workflows.state import (
    Checkpoint,
    EventRecord,
    ExecutionBinding,
    FeedbackEvent,
    GateEvent,
    GateOutcome,
    GateRun,
    InputEvent,
    MemberRun,
    NodeRun,
    Phase,
    ResolveTurnEvent,
    Status,
    TurnRun,
)
from omnigent.workflows.store import RunStore


@dataclass(frozen=True)
class MemberRequest:
    run_id: str
    node: WorkflowNode
    member: TeamMember
    phase: Phase
    prompt: str
    record: MemberRun
    turn_id: str
    round: int
    checkpoint: Callable[[], None]
    workflow_id: str | None = None
    root_session_id: str | None = None


MemberExecutor = Callable[[MemberRequest], Awaitable[str]]
GateExecutor = Callable[[GateRequest], Awaitable[GateOutcome]]


class UncertainTurn(RuntimeError):
    """An accepted remote action may exist; automatic resubmission would duplicate it."""


class WorkflowRunner:
    """One controller per run; waiting runs resume when input events become available."""

    def __init__(
        self,
        workflow: Workflow,
        execute: MemberExecutor,
        *,
        store: RunStore | None = None,
        validate: GateExecutor = execute_command_gate,
        binding: ExecutionBinding | None = None,
        workflow_id: str | None = None,
        root_session_id: str | None = None,
    ) -> None:
        self.workflow = Workflow.model_validate(workflow.model_dump())
        self.run_id = uuid4().hex
        self.workflow_id = workflow_id
        self.root_session_id = root_session_id
        self.status: Status = "pending"
        self.nodes = {node.id: NodeRun() for node in workflow.nodes}
        self.events: dict[str, EventRecord] = {}
        self.binding = binding or ExecutionBinding()
        self._execute, self._validate_gate, self._store = execute, validate, store
        self._slots = asyncio.Semaphore(workflow.max_parallel_members)
        self._loaded: dict[str, Any] | None = None
        self._running = False

    @classmethod
    def restore(
        cls,
        snapshot: dict[str, Any],
        execute: MemberExecutor,
        *,
        store: RunStore | None = None,
        validate: GateExecutor = execute_command_gate,
    ) -> WorkflowRunner:
        saved = Checkpoint.model_validate(snapshot)
        if set(saved.nodes) != {node.id for node in saved.workflow.nodes}:
            raise ValueError("Checkpoint nodes do not match its workflow")
        runner = cls(
            saved.workflow,
            execute,
            store=store,
            validate=validate,
            binding=saved.binding,
            workflow_id=saved.workflow_id,
            root_session_id=saved.root_session_id,
        )
        runner.run_id, runner.nodes = saved.run_id, saved.nodes
        runner.events = {record.event.id: record for record in saved.events}
        if len(runner.events) != len(saved.events):
            raise ValueError("Checkpoint contains duplicate event IDs")
        runner.status = saved.status
        runner._loaded = snapshot
        for state in runner.nodes.values():
            uncertain = False
            for member in state.members.values():
                for turn in member.turns.values():
                    if turn.status in {"running", "uncertain"}:
                        turn.status = "uncertain"
                        uncertain = True
            if state.status in {"succeeded", "failed", "blocked"}:
                continue
            if uncertain:
                state.status = "interrupted"
                state.error = "Inspect remote sessions and resolve uncertain turns before resuming"
            elif state.status in {"running", "cancelled"}:
                state.status = "pending"
        if runner.status in {"running", "cancelled"}:
            runner.status = "pending"
        return runner

    def snapshot(self) -> dict[str, Any]:
        return Checkpoint(
            run_id=self.run_id,
            workflow_id=self.workflow_id,
            root_session_id=self.root_session_id,
            status=self.status,
            workflow=self.workflow,
            nodes=self.nodes,
            events=list(self.events.values()),
            binding=self.binding,
        ).model_dump(mode="json")

    def _save(self) -> None:
        if self._store:
            self._store.save(self.snapshot())

    def enqueue(self, event: InputEvent) -> None:
        """Queue once in memory; external producers use RunStore.enqueue."""
        if existing := self.events.get(event.id):
            if existing.event != event:
                raise ValueError(f"Conflicting event ID: {event.id}")
            return
        self.events[event.id] = EventRecord(event=event)

    def _refresh_events(self) -> None:
        if self._store:
            for event in self._store.events():
                self.enqueue(event)
        for record in self.events.values():
            if record.status != "pending":
                continue
            event = record.event
            state = self.nodes.get(event.node_id)
            if state is None:
                record.status, record.error = "rejected", "Unknown node"
            elif state.status in {"succeeded", "failed", "blocked", "cancelled"}:
                record.status, record.error = "rejected", "Node is terminal"
            elif isinstance(event, ResolveTurnEvent):
                self._resolve_turn(state, record, event)
            elif isinstance(event, GateEvent):
                node = next(node for node in self.workflow.nodes if node.id == event.node_id)
                gate = next((gate for gate in node.gates if gate.id == event.gate_id), None)
                if (
                    state.stage != "validate"
                    or state.candidate_id != event.candidate_id
                    or not isinstance(gate, ExternalGate)
                ):
                    record.status, record.error = "rejected", "Stale or unknown external gate"

    def _resolve_turn(self, state: NodeRun, record: EventRecord, event: ResolveTurnEvent) -> None:
        member = state.members.get(event.member_id)
        turn = member.turns.get(event.turn_id) if member else None
        if state.status != "interrupted" or turn is None or turn.status != "uncertain":
            record.status, record.error = "rejected", "Turn is not awaiting reconciliation"
            return
        assert member is not None
        if event.session_id and member.session_id and member.session_id != event.session_id:
            record.status, record.error = "rejected", "Session ID does not match the saved member"
            return
        if self.binding.mode == "server" and not (member.session_id or event.session_id):
            record.status, record.error = "rejected", "Locate the remote session before resolving"
            return
        member.session_id = member.session_id or event.session_id
        turn.status, turn.output = "succeeded", event.output
        member.status, member.output = "succeeded", event.output
        record.status = "applied"
        if not any(
            t.status == "uncertain" for m in state.members.values() for t in m.turns.values()
        ):
            state.status, state.error = "pending", None

    def _has_input(self, node_id: str) -> bool:
        return any(
            r.status == "pending" and r.event.node_id == node_id for r in self.events.values()
        )

    async def run(self) -> dict[str, Any]:
        if self._running or self.status not in {"pending", "waiting"}:
            raise RuntimeError("A workflow runs once; only waiting checkpoints can resume")
        with self._store.controller_lock if self._store else nullcontext():
            if self._store and self._store.path.exists():
                if self._loaded is None or self._store.load() != self._loaded:
                    raise ValueError(
                        "Checkpoint already exists or changed; restore it before running"
                    )
            self._running = True
            try:
                return await self._run()
            finally:
                self._running = False
                self._save()
                self._loaded = self.snapshot()

    async def _run(self) -> dict[str, Any]:
        self.status = "running"
        active: dict[str, asyncio.Task[None]] = {}
        self._save()
        try:
            while True:
                self._refresh_events()
                changed = False
                for node in self.workflow.nodes:
                    state = self.nodes[node.id]
                    if node.id in active or state.status == "interrupted":
                        continue
                    if state.status == "waiting" and self._has_input(node.id):
                        state.status = "pending"
                    if state.status != "pending":
                        continue
                    dependencies = [self.nodes[parent].status for parent in node.depends_on]
                    if any(
                        status in {"failed", "blocked", "cancelled"} for status in dependencies
                    ):
                        state.status, state.error = "blocked", "A dependency did not succeed"
                        changed = True
                    elif all(status == "succeeded" for status in dependencies):
                        state.status = "running"
                        active[node.id] = asyncio.create_task(self._drive_node(node))
                self._save()
                if not active:
                    if changed:
                        continue
                    break
                await asyncio.wait(
                    active.values(), timeout=0.2, return_when=asyncio.FIRST_COMPLETED
                )
                for node_id, task in list(active.items()):
                    if task.done():
                        await task
                        del active[node_id]
        except BaseException:
            for task in active.values():
                task.cancel()
            await asyncio.gather(*active.values(), return_exceptions=True)
            for state in self.nodes.values():
                if state.status in {"pending", "running"}:
                    state.status = "cancelled"
            self.status = "cancelled"
            raise
        statuses = {state.status for state in self.nodes.values()}
        self.status = (
            "waiting"
            if statuses.intersection({"waiting", "interrupted", "pending"})
            else "failed"
            if statuses.intersection({"failed", "blocked"})
            else "succeeded"
        )
        self._save()
        return self.snapshot()

    def _next_round(
        self, node: WorkflowNode, message: str, specialists: tuple[str, ...] = ()
    ) -> None:
        state = self.nodes[node.id]
        if state.round >= node.max_rounds:
            raise RuntimeError(f"Node exceeded max_rounds={node.max_rounds}: {message}")
        state.round += 1
        state.feedback.append(message)
        state.candidate, state.candidate_id = None, None
        state.specialists = list(specialists)
        state.stage = "contribute" if specialists else "owner"
        self._save()

    def _feedback(self, node: WorkflowNode) -> bool:
        self._refresh_events()
        pending = [
            record
            for record in self.events.values()
            if record.status == "pending"
            and record.event.node_id == node.id
            and isinstance(record.event, FeedbackEvent)
        ]
        if not pending:
            return False
        messages = []
        for record in pending:
            assert isinstance(record.event, FeedbackEvent)
            messages.append(record.event.message)
            record.status = "applied"
        state = self.nodes[node.id]
        text = "\n".join(messages)
        if state.candidate or any(member.turns for member in state.members.values()):
            self._next_round(node, text)
        else:
            state.feedback.append(text)
        self._save()
        return True

    def _context(self, node: WorkflowNode) -> dict[str, Any]:
        state = self.nodes[node.id]
        return {
            "workflow_goal": self.workflow.goal,
            "node_goal": node.goal,
            "parent": node.parent,
            "dependency_results": {
                parent: {
                    "status": self.nodes[parent].status,
                    "summary": self.nodes[parent].summary,
                    "outputs": self.nodes[parent].outputs,
                    "artifacts": [a.model_dump() for a in self.nodes[parent].artifacts],
                }
                for parent in node.depends_on
            },
            "round": state.round,
            "feedback": state.feedback,
            "contributions": state.contributions,
            "previous_candidates": {
                key: value.model_dump() for key, value in state.candidates.items()
            },
            "gates": [gate.model_dump() for gate in node.gates],
        }

    async def _drive_node(self, node: WorkflowNode) -> None:
        state = self.nodes[node.id]
        team = next(team for team in self.workflow.teams if team.id == node.team)
        members = {member.id: member for member in team.members}
        if state.specialists is None:
            state.specialists = list(members) if team.strategy == "parallel" else []
            state.stage = "contribute" if state.specialists else "owner"
        try:
            while True:
                self._feedback(node)
                if state.stage == "contribute":
                    tasks = [
                        asyncio.create_task(
                            self._call(
                                node,
                                members[member_id],
                                "contribute",
                                f"You are {member_id}. Role: {members[member_id].role}. "
                                "Contribute evidence to the owner of this node. "
                                f"Context (JSON): {json.dumps(self._context(node))}",
                            )
                        )
                        for member_id in (state.specialists or [])
                    ]
                    try:
                        outputs = await asyncio.gather(*tasks)
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                    state.contributions.update(zip(state.specialists or [], outputs, strict=True))
                    state.stage = "owner"
                    self._save()
                    if self._feedback(node):
                        continue
                if state.stage == "owner":
                    phase: Phase = "synthesize" if team.strategy == "parallel" else "work"
                    expansion = (
                        "You may propose add_nodes using id, goal, team, "
                        "depends_on and allow_expansion. "
                        f"Use only these teams: {', '.join(t.id for t in self.workflow.teams)}. "
                        "Remaining node capacity: "
                        f"{self.workflow.max_nodes - len(self.workflow.nodes)}. "
                        "Additions run after this node and before its existing dependants."
                        if node.allow_expansion
                        else "add_nodes must be empty."
                    )
                    prompt = (
                        f"You are the continuing owner {team.lead}. "
                        f"Role: {members[team.lead].role}. Work toward this node's acceptance "
                        "criteria, using feedback to repair prior work. "
                        f"Context (JSON): {json.dumps(self._context(node))}\n"
                        "Return only JSON: {summary: nonempty string, "
                        "status: ready|waiting|consult, artifacts: [{id, kind, uri, revision}], "
                        "outputs: {name: JSON value}, "
                        "consult_members: [], add_nodes: []}. ready proposes a candidate for "
                        "independent validation; waiting requests input described in summary; "
                        "consult requests named specialists, with their task in summary. "
                        "Available specialists: "
                        f"{', '.join(m for m in members if m != team.lead)}. "
                        "Put small reusable results in outputs (up to 64 KiB of JSON). "
                        "Accepted outputs are passed unchanged to dependent tasks. "
                        "Publish files to a location other agents can read before returning "
                        "an artifact. For code, use a repository URL and full published commit "
                        "SHA; local paths and unpublished commits are not shared. Consumers "
                        "must fetch and verify that exact revision, not the latest branch. "
                        f"Artifact references do not transfer files. {expansion}"
                    )
                    result = NodeResult.model_validate_json(
                        await self._call(node, members[team.lead], phase, prompt)
                    )
                    state.candidate = result
                    state.candidate_id = f"{node.id}-{state.round}"
                    state.candidates[state.candidate_id] = result
                    self._save()
                    if self._feedback(node):
                        continue
                    if result.status == "consult":
                        if set(result.consult_members) - (members.keys() - {team.lead}):
                            raise ValueError("Owner requested an unknown specialist or itself")
                        self._next_round(node, result.summary, result.consult_members)
                        continue
                    if result.status == "waiting":
                        state.status = "waiting"
                        return
                    state.stage = "validate"
                    self._save()
                if not await self._validate(node):
                    if state.status == "waiting":
                        return
                    continue
                assert state.candidate is not None
                result = state.candidate
                if result.add_nodes:
                    if any(
                        isinstance(gate, CommandGate)
                        for added in result.add_nodes
                        for gate in added.gates
                    ):
                        raise ValueError(
                            "Agent-added nodes cannot introduce controller command gates"
                        )
                    expanded = self.workflow.expand(node.id, result.add_nodes)
                    self.workflow = expanded
                    self.nodes.update({added.id: NodeRun() for added in result.add_nodes})
                state.summary, state.artifacts = result.summary, result.artifacts
                state.outputs = result.outputs
                state.status, state.error = "succeeded", None
                self._save()
                return
        except UncertainTurn as exc:
            state.status, state.error = "interrupted", str(exc)
        except asyncio.CancelledError:
            state.status = "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 -- a failed node does not abort unrelated branches.
            state.status, state.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            for member in state.members.values():
                if member.status in {"running", "pending"}:
                    member.status = "cancelled"
            self._save()

    def _gate_events(self, node: WorkflowNode) -> None:
        state = self.nodes[node.id]
        for record in self.events.values():
            event = record.event
            if (
                record.status != "pending"
                or event.node_id != node.id
                or not isinstance(event, GateEvent)
            ):
                continue
            gate = next((gate for gate in node.gates if gate.id == event.gate_id), None)
            settled = any(
                result.candidate_id == event.candidate_id and result.gate_id == event.gate_id
                for result in state.gate_results
            )
            if (
                event.candidate_id != state.candidate_id
                or state.stage != "validate"
                or not isinstance(gate, ExternalGate)
                or settled
            ):
                record.status, record.error = (
                    "rejected",
                    "Stale, unknown, or already settled external gate",
                )
            elif state.candidate is None or set(gate.artifacts) - {
                a.id for a in state.candidate.artifacts
            }:
                record.status, record.error = "rejected", "Candidate is missing required artifacts"
            else:
                state.gate_results.append(
                    GateRun(
                        gate_id=event.gate_id,
                        candidate_id=event.candidate_id,
                        outcome=event.outcome,
                    )
                )
                record.status = "applied"

    async def _validate(self, node: WorkflowNode) -> bool:
        state = self.nodes[node.id]
        assert state.candidate is not None and state.candidate_id is not None
        candidate, candidate_id = state.candidate, state.candidate_id
        self._refresh_events()
        self._gate_events(node)
        for gate in node.gates:
            if any(
                result.candidate_id == candidate_id and result.gate_id == gate.id
                for result in state.gate_results
            ):
                continue
            missing = set(gate.artifacts) - {artifact.id for artifact in candidate.artifacts}
            if missing:
                outcome = GateOutcome(
                    passed=False, detail=f"Missing required artifacts: {sorted(missing)}"
                )
            elif isinstance(gate, ExternalGate):
                continue
            else:
                async with self._slots:
                    outcome = await self._validate_gate(
                        GateRequest(node, gate, candidate_id, candidate)
                    )
            state.gate_results.append(
                GateRun(gate_id=gate.id, candidate_id=candidate_id, outcome=outcome)
            )
            self._save()
            if self._feedback(node):
                return False
        if self._feedback(node):
            return False
        self._gate_events(node)
        results = [result for result in state.gate_results if result.candidate_id == candidate_id]
        failures = [result.model_dump() for result in results if not result.outcome.passed]
        if failures:
            self._next_round(node, "Validation failed (JSON): " + json.dumps(failures))
            return False
        if len(results) < len(node.gates):
            state.status = "waiting"
            self._save()
            return False
        return True

    async def _call(
        self, node: WorkflowNode, member: TeamMember, phase: Phase, prompt: str
    ) -> str:
        state = self.nodes[node.id]
        record = state.members.setdefault(member.id, MemberRun())
        turn_id = f"{node.id}.{state.round}.{phase}.{member.id}"
        if turn := record.turns.get(turn_id):
            if turn.status == "succeeded" and turn.output is not None:
                return turn.output
            raise UncertainTurn(
                f"Inspect session {record.session_id}; resolve turn {turn_id} before retrying"
            )
        try:
            async with self._slots:
                record.status, record.phase = "running", phase
                turn = record.turns[turn_id] = TurnRun(id=turn_id, phase=phase, prompt=prompt)
                self._save()
                async with asyncio.timeout(self.workflow.member_timeout_seconds):
                    output = await self._execute(
                        MemberRequest(
                            run_id=self.run_id,
                            workflow_id=self.workflow_id,
                            root_session_id=self.root_session_id,
                            node=node,
                            member=member,
                            phase=phase,
                            prompt=prompt,
                            record=record,
                            turn_id=turn_id,
                            round=state.round,
                            checkpoint=self._save,
                        )
                    )
                turn.status, turn.output = "succeeded", output
                record.status, record.output = "succeeded", output
                return output
        except asyncio.CancelledError:
            record.status = "cancelled"
            if turn_id in record.turns:
                record.turns[turn_id].status = "uncertain"
            raise
        except TimeoutError as exc:
            record.status, record.error = "failed", "TimeoutError: member turn timed out"
            if turn_id in record.turns:
                record.turns[turn_id].status = "failed"
            if record.cleanup_error:
                record.status = "interrupted"
                record.turns[turn_id].status = "uncertain"
                raise UncertainTurn(
                    "Turn timed out with unconfirmed cleanup; inspect the saved session/invocation"
                ) from exc
            raise
        except UncertainTurn as exc:
            record.status, record.error = "interrupted", str(exc)
            if turn_id in record.turns:
                record.turns[turn_id].status = "uncertain"
            raise
        except Exception as exc:
            record.status, record.error = "failed", f"{type(exc).__name__}: {exc}"
            if turn_id in record.turns:
                record.turns[turn_id].status = "failed"
                record.turns[turn_id].error = record.error
            raise
        finally:
            self._save()
