"""Server execution persists its connection and shares one login across agents."""

from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.workflows.__main__ import main
from omnigent.workflows.connection import open_workflow_server_client
from omnigent.workflows.execution import open_executor
from omnigent.workflows.simulation import simulate_member
from omnigent.workflows.state import ExecutionBinding


def test_cli_persists_server_connection_and_refuses_changes_on_resume(tmp_path, monkeypatch):
    bindings = []

    @asynccontextmanager
    async def executor(binding):
        bindings.append(binding)
        yield simulate_member

    monkeypatch.setattr("omnigent.workflows.__main__.open_executor", executor)
    definition = tmp_path / "workflow.yaml"
    example = (
        Path(__file__).resolve().parents[2] / "examples/dynamic-workflows/sandbox-workflow.yaml"
    )
    definition.write_text(example.read_text())
    state = tmp_path / "state"
    cli = CliRunner()
    result = cli.invoke(
        main,
        [
            str(definition),
            "--server",
            "https://test.example",
            "--state",
            str(state),
        ],
    )
    assert result.exit_code == 3, result.output
    assert bindings == [ExecutionBinding(mode="server", server="https://test.example")]
    assert cli.invoke(main, ["--resume", "--state", str(state)]).exit_code == 3
    assert bindings[1] == bindings[0]
    for changed in (["--simulate"], ["--server", "https://other.example"]):
        result = cli.invoke(main, ["--resume", "--state", str(state), *changed])
        assert result.exit_code == 2 and "must match" in result.output
    assert len(bindings) == 2


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
        async with open_executor(ExecutionBinding(mode="server", server=url)):
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
