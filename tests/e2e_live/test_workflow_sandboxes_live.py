"""Opt-in test of parallel agents handing outputs to a downstream managed sandbox.

Requires OMNIGENT_WORKFLOW_SERVER_URL, OMNIGENT_WORKFLOW_SANDBOX_PROVIDER,
OMNIGENT_WORKFLOW_LIVE_AGENT_ID and an authenticated server client. See
examples/dynamic-workflows/README.md. This allocates compute and calls models.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest
from omnigent_client import SessionsNamespace

from omnigent.session_host import set_session_host
from omnigent.workflows.connection import open_workflow_server_client
from omnigent.workflows.engine import WorkflowRunner
from omnigent.workflows.execution import open_executor
from omnigent.workflows.models import Workflow
from omnigent.workflows.state import ExecutionBinding
from omnigent.workflows.store import RunStore


async def test_parallel_task_hands_outputs_to_downstream_sandbox(tmp_path, monkeypatch):
    server = os.environ.get("OMNIGENT_WORKFLOW_SERVER_URL", "").rstrip("/")
    agent_id = os.environ.get("OMNIGENT_WORKFLOW_LIVE_AGENT_ID")
    provider = os.environ.get("OMNIGENT_WORKFLOW_SANDBOX_PROVIDER")
    if not server or not agent_id or not provider:
        pytest.skip("Set the workflow server URL, sandbox provider and live agent ID")
    auth_directory = os.environ.get("OMNIGENT_WORKFLOW_LIVE_AUTH_DIR")
    if auth_directory:
        monkeypatch.setenv("OMNIGENT_DATA_DIR", auth_directory)

    nonce = uuid.uuid4().hex
    workflow = Workflow.model_validate(
        {
            "name": f"Live sandbox verification {nonce}",
            "goal": "Verify two independent agent contributions and their synthesis.",
            "teams": [
                {
                    "id": "workers",
                    "lead": "lead",
                    "strategy": "parallel",
                    "members": [
                        {
                            "id": member,
                            "agent_id": agent_id,
                            "role": f"Compute {expression}; report '{marker}=42' and '{nonce}'.",
                            "placement": {"kind": "sandbox", "provider": provider},
                        }
                        for member, expression, marker in (
                            ("lead", "19 + 23", "sum"),
                            ("reviewer", "6 * 7", "product"),
                        )
                    ],
                },
                {
                    "id": "consumer",
                    "lead": "reader",
                    "strategy": "owner",
                    "members": [
                        {
                            "id": "reader",
                            "agent_id": agent_id,
                            "role": "Consume accepted outputs from the prerequisite task.",
                            "placement": {"kind": "sandbox", "provider": provider},
                        }
                    ],
                },
            ],
            "nodes": [
                {
                    "id": "verify",
                    "team": "workers",
                    "goal": (
                        "Each contributor computes only its assigned expression. The owner "
                        "then synthesizes BOTH contributions, preserving sum=42, product=42 "
                        f"and {nonce} in the JSON summary. No tools or repository changes needed."
                        " The synthesized result must include outputs with numeric sum and "
                        "product from the contributions, and nonce containing the shared token."
                    ),
                    "max_rounds": 1,
                },
                {
                    "id": "consume",
                    "team": "consumer",
                    "depends_on": ["verify"],
                    "goal": (
                        "Use dependency_results.verify.outputs. Add its sum and product into "
                        "outputs.total and copy its nonce into outputs.receipt. Describe the "
                        "consumed values in summary. No tools or repository changes needed."
                    ),
                    "max_rounds": 1,
                },
            ],
            "max_parallel_members": 2,
            "member_timeout_seconds": 900,
        }
    )
    binding = ExecutionBinding(mode="server", server=server)
    evidence = {"nonce": nonce, "members": {}, "transcripts": {}, "cleanup": {}}
    store = RunStore(tmp_path / "checkpoint")
    runner = None
    async with open_workflow_server_client(server) as audit:
        sessions = SessionsNamespace(audit, server)
        # Fail before allocating if the login or registered agent is unavailable.
        agents = await sessions.list_registered_agents()
        assert agent_id in {agent.id for agent in agents}, "Agent ID is not registered"
        try:
            async with open_executor(binding) as executor:
                runner = WorkflowRunner(workflow, executor, store=store, binding=binding)
                await runner.run()
            assert runner.status == "succeeded", runner.snapshot()
            node = runner.nodes["verify"]
            assert set(node.contributions) == {"lead", "reviewer"}
            for member_id, marker in (("lead", "sum=42"), ("reviewer", "product=42")):
                assert nonce in node.contributions[member_id]
                assert marker in node.contributions[member_id]
            assert node.outputs == {"sum": 42, "product": 42, "nonce": nonce}
            consumer = runner.nodes["consume"]
            assert consumer.outputs == {"total": 84, "receipt": nonce}
            for member_id, member in [*node.members.items(), *consumer.members.items()]:
                session = await sessions.get(member.session_id)
                assert session.host_id and session.runner_id
                set_session_host(session.id, session.host_id, keyless=True)
                response = await audit.get(f"{server}/v1/hosts/{session.host_id}")
                response.raise_for_status()
                host = response.json()
                assert host["sandbox_provider"] == provider, host
                evidence["members"][member_id] = {
                    "session_id": session.id,
                    "runner_id": session.runner_id,
                    "host_id": session.host_id,
                    "sandbox_provider": host["sandbox_provider"],
                }
            for key in ("session_id", "runner_id", "host_id"):
                assert len({member[key] for member in evidence["members"].values()}) == 3
            assert len(node.members["lead"].turns) == 2
            assert len(node.members["reviewer"].turns) == 1
            assert len(consumer.members["reader"].turns) == 1
            assert all(text in node.summary for text in (nonce, "sum=42", "product=42"))
        finally:
            if runner is not None:
                session_ids = {
                    member.session_id
                    for state in runner.nodes.values()
                    for member in state.members.values()
                    if member.session_id
                }
                try:
                    recent = await sessions.list(limit=1000, visibility="mine")
                    session_ids.update(
                        session.id
                        for session in recent
                        if session.labels.get("omnigent.workflow.run") == runner.run_id
                    )
                except Exception as exc:
                    evidence["cleanup_discovery_error"] = str(exc)
                for session_id in session_ids:
                    try:
                        evidence["transcripts"][session_id] = (
                            await sessions.list_items(session_id, limit=100, order="desc")
                        ).data
                    except Exception as exc:
                        evidence["transcripts"][session_id] = {"error": str(exc)}
                    try:
                        response = await audit.delete(f"{server}/v1/sessions/{session_id}")
                        response.raise_for_status()
                        evidence["cleanup"][session_id] = "deleted"
                    except Exception as exc:
                        evidence["cleanup"][session_id] = str(exc)
                evidence["workflow"] = runner.snapshot()
            (tmp_path / "sandbox-evidence.json").write_text(json.dumps(evidence, indent=2))
            print(f"Live sandbox evidence: {tmp_path / 'sandbox-evidence.json'}")
        assert len(evidence["cleanup"]) == 3
        assert set(evidence["cleanup"].values()) == {"deleted"}, evidence["cleanup"]
        for member in evidence["members"].values():
            response = await audit.get(f"{server}/v1/hosts/{member['host_id']}")
            assert response.status_code == 404, response.text
        evidence["host_cleanup_verified"] = True
        (tmp_path / "sandbox-evidence.json").write_text(json.dumps(evidence, indent=2))
