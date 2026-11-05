"""Experimental workflow UI endpoints backed by the workflow engine."""

from __future__ import annotations

import os
from typing import Any, Literal
from uuid import uuid4

import yaml
from fastapi import APIRouter, HTTPException, Request
from filelock import Timeout as LockTimeout
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.routes._auth_helpers import require_user
from omnigent.workflows.designs import SavedWorkflow, WorkflowDesigns
from omnigent.workflows.execution import validate_connection_url
from omnigent.workflows.models import CommandGate, Workflow
from omnigent.workflows.service import WorkflowService
from omnigent.workflows.state import EVENT_ADAPTER, ExecutionBinding
from omnigent.workflows.store import RunStore

Backend = Literal["simulation", "server"]
MAX_DEFINITION_LENGTH = 65536


class StartWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: Backend
    workflow_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    definition: str = Field(min_length=1, max_length=MAX_DEFINITION_LENGTH)


class CompileWorkflow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow: Workflow


def validate_web_workflow(workflow: Workflow) -> None:
    if any(isinstance(gate, CommandGate) for node in workflow.nodes for gate in node.gates):
        raise ValueError("Web workflows use external gates; use the local CLI for command gates")


def example_definition() -> str:
    placement = {"kind": "sandbox", "provider": "replace-with-sandbox-provider"}
    return yaml.safe_dump(
        {
            "name": "Investigate and propose a change",
            "goal": "Produce an evidence-based recommendation.",
            "teams": [
                {
                    "id": "delivery",
                    "strategy": "owner",
                    "lead": "owner",
                    "members": [
                        {
                            "id": "owner",
                            "role": "Own the outcome and repair failed candidates.",
                            "agent_id": "replace-with-registered-agent-id",
                            "placement": placement,
                        },
                    ],
                }
            ],
            "nodes": [
                {
                    "id": "investigate",
                    "team": "delivery",
                    "goal": "Investigate the problem and report evidence.",
                },
                {
                    "id": "propose",
                    "team": "delivery",
                    "goal": "Propose a change. Return an artifact named result with its revision.",
                    "depends_on": ["investigate"],
                    "gates": [{"id": "review", "kind": "external", "artifacts": ["result"]}],
                },
                {
                    "id": "summarize",
                    "team": "delivery",
                    "goal": "Summarize the accepted recommendation.",
                    "depends_on": ["propose"],
                },
            ],
        },
        sort_keys=False,
    )


def create_workflows_router(
    service: WorkflowService, auth_provider: AuthProvider | None, designs: WorkflowDesigns
) -> APIRouter:
    router = APIRouter()

    def owner(request: Request) -> str:
        return require_user(request, auth_provider) or "local"

    def binding_for(user: str, backend: Backend) -> ExecutionBinding:
        if backend == "simulation":
            return ExecutionBinding(mode="simulation")
        # The prototype uses operator credentials, so live execution belongs to one named user.
        allowed = os.environ.get("OMNIGENT_WORKFLOW_OWNER")
        if user != allowed and not (
            allowed is None and user == "local" and local_single_user_enabled()
        ):
            raise ValueError("Live workflows are not configured for your account")
        server = os.environ.get("OMNIGENT_WORKFLOW_SERVER_URL")
        if not server:
            raise ValueError("Configure the workflow server connection to enable task agents")
        validate_connection_url(server)
        return ExecutionBinding(mode=backend, server=server)

    def run_store(request: Request, run_id: str) -> RunStore:
        try:
            return service.store(owner(request), run_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Workflow not found") from exc

    @router.get("/workflows/options")
    async def options(request: Request) -> dict[str, Any]:
        user = owner(request)
        result = []
        for backend in ("simulation", "server"):
            reason = None
            try:
                binding_for(user, backend)
            except ValueError as exc:
                reason = str(exc)
            result.append(
                {
                    "id": backend,
                    "enabled": reason is None,
                    "reason": reason,
                    "example": example_definition(),
                }
            )
        return {"backends": result}

    @router.get("/workflows")
    async def list_runs(request: Request) -> dict[str, Any]:
        return {"data": service.list_runs(owner(request))}

    @router.get("/workflows/designs/{workflow_id}")
    async def get_design(request: Request, workflow_id: str) -> SavedWorkflow:
        try:
            return designs.load(owner(request), workflow_id)
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(404, "Workflow not found") from exc

    @router.post("/workflows/designs/{workflow_id}")
    async def save_design(
        request: Request, workflow_id: str, body: CompileWorkflow
    ) -> SavedWorkflow:
        try:
            validate_web_workflow(body.workflow)
            if len(body.workflow.model_dump_json()) > MAX_DEFINITION_LENGTH:
                raise ValueError("Workflow exceeds the API limit of 65,536 characters")
            return await designs.save(owner(request), workflow_id, body.workflow)
        except FileNotFoundError as exc:
            raise HTTPException(409, "Workflow root session is unavailable") from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.post("/workflows/definition")
    async def compile_definition(request: Request, body: CompileWorkflow) -> dict[str, str]:
        owner(request)
        try:
            validate_web_workflow(body.workflow)
            definition = yaml.safe_dump(
                body.workflow.model_dump(mode="json", exclude_none=True), sort_keys=False
            )
            if len(definition) > MAX_DEFINITION_LENGTH:
                raise ValueError("Workflow YAML exceeds the API limit of 65,536 characters")
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"definition": definition}

    @router.post("/workflows", status_code=201)
    async def start(request: Request, body: StartWorkflow) -> dict[str, Any]:
        user = owner(request)
        try:
            binding = binding_for(user, body.backend)
            workflow = Workflow.model_validate(yaml.safe_load(body.definition))
            validate_web_workflow(workflow)
            workflow_id = body.workflow_id or uuid4().hex
            if body.workflow_id:
                try:
                    saved = designs.load(user, workflow_id)
                except (ValueError, FileNotFoundError) as exc:
                    raise HTTPException(404, "Workflow not found") from exc
            else:
                saved = await designs.save(user, workflow_id, workflow)
            if sum(not task.done() for task in service.tasks.values()) >= 8:
                raise HTTPException(429, "The workflow controller is at capacity")
            return service.inspect(
                service.create(
                    user,
                    workflow,
                    binding,
                    workflow_id=saved.workflow_id,
                    root_session_id=saved.root_session_id,
                )
            )
        except (ValueError, yaml.YAMLError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @router.get("/workflows/{run_id}")
    async def inspect(request: Request, run_id: str) -> dict[str, Any]:
        return service.inspect(run_store(request, run_id))

    @router.post("/workflows/{run_id}/events", status_code=202)
    async def event(request: Request, run_id: str, body: dict[str, Any]) -> dict[str, str]:
        store = run_store(request, run_id)
        try:
            incoming = EVENT_ADAPTER.validate_python(body)
            saved = store.load()
            state = saved["nodes"].get(incoming.node_id)
            if state is None or state["status"] in {"succeeded", "failed", "blocked"}:
                raise ValueError("Choose a nonterminal node for this event")
            store.enqueue(incoming)
        except (ValueError, ValidationError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"status": "queued"}

    @router.post("/workflows/{run_id}/resume")
    async def resume(request: Request, run_id: str) -> dict[str, Any]:
        store = run_store(request, run_id)
        try:
            binding = ExecutionBinding.model_validate(store.load()["binding"])
            if binding.mode not in {"simulation", "server"}:
                raise ValueError("This backend is available through the CLI only")
            if binding_for(owner(request), binding.mode) != binding:
                raise ValueError("The configured connection changed; restore it before resuming")
            service.start(store)
        except (ValueError, LockTimeout) as exc:
            raise HTTPException(409, str(exc)) from exc
        return service.inspect(store)

    @router.post("/workflows/{run_id}/stop")
    async def stop(request: Request, run_id: str) -> dict[str, Any]:
        store = run_store(request, run_id)
        try:
            await service.stop(store)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return service.inspect(store)

    return router
