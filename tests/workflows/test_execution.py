"""Backend selection validates placement before contacting either runtime."""

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from click.testing import CliRunner
from omnigent_client import SessionsNamespace

from omnigent.workflows.__main__ import main
from omnigent.workflows.arclet import ArcletExecutor
from omnigent.workflows.connection import open_workflow_server_client
from omnigent.workflows.engine import WorkflowRunner
from omnigent.workflows.execution import open_executor, validate_backend
from omnigent.workflows.models import Workflow
from omnigent.workflows.simulation import simulate_member
from omnigent.workflows.state import ExecutionBinding


@pytest.mark.parametrize("backend", ["sessions", "arclet"])
def test_cli_selects_and_persists_backend_without_changing_it_on_resume(
    tmp_path, monkeypatch, backend
):
    bindings = []

    @asynccontextmanager
    async def executor(binding):
        bindings.append(binding)
        yield simulate_member

    monkeypatch.setattr("omnigent.workflows.__main__.open_executor", executor)
    definition = tmp_path / "workflow.yaml"
    example = (
        Path(__file__).resolve().parents[2] / "examples/dynamic-workflows/arclet-workflow.yaml"
    )
    definition.write_text(example.read_text())
    state = tmp_path / "state"
    cli = CliRunner()
    result = cli.invoke(
        main,
        [
            str(definition),
            "--backend",
            backend,
            "--server",
            "https://test.example",
            "--state",
            str(state),
        ],
    )
    assert result.exit_code == 3, result.output
    assert bindings == [
        ExecutionBinding(
            mode="server" if backend == "sessions" else backend, server="https://test.example"
        )
    ]
    assert cli.invoke(main, ["--resume", "--state", str(state)]).exit_code == 3
    assert bindings[1] == bindings[0]
    wrong = "arclet" if backend == "sessions" else "sessions"
    result = cli.invoke(main, ["--resume", "--state", str(state), "--backend", wrong])
    assert result.exit_code == 2 and "must match" in result.output


@pytest.mark.parametrize(
    "placement",
    [
        {"kind": "host", "host_id": "host", "workspace": "/workspace"},
        {"kind": "sandbox", "provider": "other"},
    ],
)
async def test_arclet_library_rejects_other_placements_before_any_http_call(
    owner_workflow, placement
):
    definition = owner_workflow.model_dump()
    definition["teams"][0]["members"][0]["placement"] = placement
    workflow = Workflow.model_validate(definition)
    with pytest.raises(ValueError, match="Arclet"):
        validate_backend(workflow, ExecutionBinding(mode="arclet"))
    async with httpx.AsyncClient() as client:
        runner = WorkflowRunner(
            workflow, ArcletExecutor(SessionsNamespace(client, "http://unused"))
        )
        await runner.run()
    assert runner.status == "failed"
    assert "provider: arclet" in runner.nodes["root"].error


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/run",
        "https://secret@workspace.example",
        "https://workspace.example?secret=test",
        "https://workspace.example#fragment",
    ],
)
async def test_connection_rejects_non_http_or_embedded_credentials(url):
    with pytest.raises(ValueError, match="HTTP"):
        async with open_executor(ExecutionBinding(mode="arclet", server=url)):
            pytest.fail("invalid connection must not open")


@pytest.mark.parametrize("failure", ["missing", "wrong-workspace", "expired"])
async def test_explicit_login_never_falls_back_to_another_identity(tmp_path, monkeypatch, failure):
    config = tmp_path / "databrickscfg"
    config.write_text("[chosen]\nhost = https://agents.example\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
    monkeypatch.setenv(
        "OMNIGENT_WORKFLOW_DATABRICKS_PROFILE", "absent" if failure == "missing" else "chosen"
    )
    monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "different-user-token")
    monkeypatch.setattr("omnigent.cli_auth.load_databricks_workspace_host", lambda _url: None)

    def expired(*args, **kwargs):
        raise ValueError("Login expired")

    monkeypatch.setattr(
        "omnigent.inner.databricks_executor._databricks_cli_profile_token", expired
    )
    server = "https://other.example" if failure == "wrong-workspace" else "https://agents.example"
    with pytest.raises((ValueError, OSError), match="profile"):
        async with open_workflow_server_client(server) as client:
            # Authentication must fail before any network request is sent.
            await client.get(server + "/v1/info")
