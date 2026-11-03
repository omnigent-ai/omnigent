"""The prototype's module entry point is credential-free in simulation mode."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from omnigent.workflows.__main__ import main

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/dynamic-workflows/workflow.yaml"


@pytest.mark.parametrize("override", [None, "", "dynamic_workflows,-dynamic_workflows"])
def test_experiment_is_disabled_without_opt_in(tmp_path, monkeypatch, override):
    if override is None:
        monkeypatch.delenv("OMNIGENT_FEATURES", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_FEATURES", override)
    state = tmp_path / "state"
    result = CliRunner().invoke(main, [str(EXAMPLE), "--simulate", "--state", str(state)])
    assert result.exit_code == 1
    assert "OMNIGENT_FEATURES=dynamic_workflows" in result.output
    assert not state.exists()


def test_simulation_expands_graph_and_writes_report(tmp_path: Path) -> None:
    output = tmp_path / "report.json"
    result = CliRunner().invoke(main, [str(EXAMPLE), "--simulate", "--output", str(output)])
    assert result.exit_code == 0, result.output
    report = json.loads(output.read_text())
    assert report["simulated"] is True
    assert report["status"] == "succeeded"
    assert "investigate_check" in report["nodes"]
    assert all(state["status"] == "succeeded" for state in report["nodes"].values())


def test_validation_and_explicit_execution_choice() -> None:
    runner = CliRunner()
    assert runner.invoke(main, [str(EXAMPLE), "--validate"]).exit_code == 0
    missing_mode = runner.invoke(main, [str(EXAMPLE)])
    assert missing_mode.exit_code == 2
    assert "--simulate or an explicit --server" in missing_mode.output
    assert (
        runner.invoke(main, [str(EXAMPLE), "--simulate", "--server", "http://test"]).exit_code == 2
    )


def test_invalid_graph_does_not_write_success_report(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("name: invalid\nnodes: []\n")
    result = CliRunner().invoke(main, [str(invalid), "--simulate"])
    assert result.exit_code == 1
    assert "validation errors" in result.output


def test_owner_example_waits_repairs_and_resumes_with_same_session(tmp_path: Path) -> None:
    cli = CliRunner()
    state = tmp_path / "state"
    state_args = ["--state", str(state)]
    result = cli.invoke(
        main, [str(EXAMPLE.with_name("owner-workflow.yaml")), "--simulate", *state_args]
    )
    assert result.exit_code == 3, result.output
    first = json.loads((state / "state.json").read_text())
    assert first["nodes"]["implement"]["candidate_id"] == "implement-1"
    assert first["nodes"]["constraints"]["status"] == "succeeded"
    assert first["nodes"]["summarize"]["status"] == "pending"
    session_id = first["nodes"]["implement"]["members"]["owner"]["session_id"]
    wrong_mode = cli.invoke(main, ["--resume", *state_args, "--server", "http://wrong"])
    assert wrong_mode.exit_code == 2 and "must match" in wrong_mode.output
    for filename, exit_code in [("failed-check.json", 3), ("passed-check.json", 0)]:
        event = ["--event", str(EXAMPLE.with_name(filename)), *state_args]
        assert cli.invoke(main, event).exit_code == 0
        assert cli.invoke(main, event).exit_code == 0
        queued = json.loads(cli.invoke(main, ["--inspect", *state_args]).output)
        assert queued["inbox"][-1]["id"] in {"demo-tests-failed", "demo-tests-passed"}
        result = cli.invoke(main, ["--resume", *state_args])
        assert result.exit_code == exit_code, result.output
        report = json.loads(result.output)
        owner = report["nodes"]["implement"]
        assert owner["candidate_id"] == "implement-2"
        assert owner["members"]["owner"]["session_id"] == session_id
    assert report["status"] == "succeeded"
    assert owner["artifacts"][0]["revision"] == "sim-2"
    assert len(owner["members"]["owner"]["turns"]) == 2
    assert len(report["events"]) == 2
    assert report["nodes"]["summarize"]["status"] == "succeeded"
    completed = cli.invoke(main, ["--resume", *state_args])
    assert completed.exit_code == 0
    assert json.loads(completed.output) == report


@pytest.mark.parametrize("filename", ["state.json", "inbox.json", "controller.lock", "inbox.lock"])
def test_report_cannot_overwrite_controller_files(tmp_path: Path, filename: str) -> None:
    result = CliRunner().invoke(
        main,
        [
            str(EXAMPLE),
            "--simulate",
            "--state",
            str(tmp_path),
            "--output",
            str(tmp_path / filename),
        ],
    )
    assert result.exit_code == 2
    assert not (tmp_path / filename).exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--resume"],
        ["--inspect"],
        ["--resume", "--inspect"],
        ["--validate"],
    ],
)
def test_saved_operations_reject_missing_or_conflicting_arguments(arguments: list[str]) -> None:
    result = CliRunner().invoke(main, arguments)
    assert result.exit_code == 2


def test_simulation_never_executes_command_gates(tmp_path: Path) -> None:
    import yaml

    definition = yaml.safe_load(EXAMPLE.read_text())
    definition["nodes"][0]["gates"] = [
        {
            "id": "tests",
            "kind": "command",
            "command": ["nonexistent-workflow-check"],
            "cwd": str(tmp_path),
        }
    ]
    path = tmp_path / "workflow.yaml"
    path.write_text(yaml.safe_dump(definition))
    result = CliRunner().invoke(main, [str(path), "--simulate"])
    assert result.exit_code == 0, result.output
    gate = json.loads(result.output)["nodes"]["investigate"]["gate_results"][0]
    assert gate["outcome"]["passed"] is True
    assert "SIMULATED" in gate["outcome"]["detail"]
