"""Opt-in test of parallel agents handing outputs to a downstream Arclet.

Requires OMNIGENT_WORKFLOW_ARCLET_URL, OMNIGENT_WORKFLOW_LIVE_AGENT_ID,
an authenticated server client, and the authenticated `arclet` CLI. See
examples/dynamic-workflows/README.md. This allocates compute and calls models.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
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
from tests._helpers.polling import poll_until


async def test_parallel_task_hands_outputs_to_downstream_arclet(tmp_path, monkeypatch):
    server = os.environ.get("OMNIGENT_WORKFLOW_ARCLET_URL", "").rstrip("/")
    agent_id = os.environ.get("OMNIGENT_WORKFLOW_LIVE_AGENT_ID")
    if not server or not agent_id:
        pytest.skip("Set OMNIGENT_WORKFLOW_ARCLET_URL and OMNIGENT_WORKFLOW_LIVE_AGENT_ID")
    auth_directory = os.environ.get("OMNIGENT_WORKFLOW_LIVE_AUTH_DIR")
    if auth_directory:
        monkeypatch.setenv("OMNIGENT_DATA_DIR", auth_directory)

    def arclets():
        result = subprocess.run(
            ["arclet", "list", "--json"], capture_output=True, text=True, check=True, timeout=30
        )
        return json.loads(result.stdout)["arclets"]

    baseline = {row["environment_id"] for row in await asyncio.to_thread(arclets)}
    nonce = uuid.uuid4().hex
    workflow = Workflow.model_validate(
        {
            "name": f"Live Arclet verification {nonce}",
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
                            "placement": {"kind": "sandbox", "provider": "arclet"},
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
                            "placement": {"kind": "sandbox", "provider": "arclet"},
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
    binding = ExecutionBinding(mode="arclet", server=server)
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
            inventory = await asyncio.to_thread(arclets)
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
                assert host["sandbox_provider"] in {"arclet", "databricks-arclet"}, host
                matches = [row for row in inventory if row["name"] == host["name"]]
                assert len(matches) == 1, (host, matches)
                arclet = matches[0]
                assert arclet["environment_id"] not in baseline
                assert arclet["state"] == "ACTIVE", arclet
                evidence["members"][member_id] = {
                    "session_id": session.id,
                    "runner_id": session.runner_id,
                    "host_id": session.host_id,
                    "arclet": arclet,
                }
            for key in ("session_id", "runner_id", "host_id"):
                assert len({member[key] for member in evidence["members"].values()}) == 3
            assert len({m["arclet"]["environment_id"] for m in evidence["members"].values()}) == 3
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
            (tmp_path / "arclet-evidence.json").write_text(json.dumps(evidence, indent=2))
            print(f"Live Arclet evidence: {tmp_path / 'arclet-evidence.json'}")
    assert len(evidence["cleanup"]) == 3
    assert set(evidence["cleanup"].values()) == {"deleted"}, evidence["cleanup"]
    created = {m["arclet"]["environment_id"] for m in evidence["members"].values()}
    await asyncio.to_thread(
        poll_until,
        lambda: all(
            row["state"] == "STOPPED" for row in arclets() if row["environment_id"] in created
        ),
        timeout_s=120,
        interval_s=5,
    )
    evidence["arclet_cleanup_verified"] = True
    (tmp_path / "arclet-evidence.json").write_text(json.dumps(evidence, indent=2))
