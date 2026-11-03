"""Experimental module entry point: python -m omnigent.workflows."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click
import yaml
from filelock import Timeout as LockTimeout
from pydantic import ValidationError

from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.workflows.engine import MemberExecutor, WorkflowRunner
from omnigent.workflows.execution import open_executor, validate_backend
from omnigent.workflows.gates import execute_command_gate
from omnigent.workflows.models import Workflow
from omnigent.workflows.simulation import simulate_gate
from omnigent.workflows.state import EVENT_ADAPTER, Checkpoint, ExecutionBinding
from omnigent.workflows.store import RunStore


@click.command()
@click.argument(
    "definition", required=False, type=click.Path(exists=True, dir_okay=False, path_type=Path)
)
@click.option(
    "--simulate", is_flag=True, help="Simulated agents/checks; no server, model, or command calls."
)
@click.option("--validate", "validate_only", is_flag=True, help="Validate without executing.")
@click.option("--server", help="Omnigent server URL.")
@click.option(
    "--backend",
    type=click.Choice(["sessions", "arclet"]),
    help="Execution library (default: sessions).",
)
@click.option(
    "--output", type=click.Path(dir_okay=False, path_type=Path), help="Write a diagnostic report."
)
@click.option(
    "--state",
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for checkpoints and events.",
)
@click.option(
    "--resume", is_flag=True, help="Resume the saved run and its original execution mode."
)
@click.option(
    "--inspect", "inspect_only", is_flag=True, help="Print saved state and the event inbox."
)
@click.option(
    "--event",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Queue one JSON event and exit.",
)
def main(
    definition: Path | None,
    simulate: bool,
    validate_only: bool,
    server: str | None,
    output: Path | None,
    backend: str | None,
    state: Path | None,
    resume: bool,
    inspect_only: bool,
    event: Path | None,
) -> None:
    """Run a YAML workflow, inspect a checkpoint, or submit feedback to its owner."""
    if not resolve_feature_flags().enabled(Feature.DYNAMIC_WORKFLOWS):
        raise click.ClickException(
            "Dynamic workflows are experimental and disabled. "
            "Enable with OMNIGENT_FEATURES=dynamic_workflows."
        )
    if sum((validate_only, resume, inspect_only, event is not None)) > 1:
        raise click.UsageError("Choose one of --validate, --resume, --inspect, or --event")
    if simulate and (server or backend):
        raise click.UsageError("Use either --simulate or --server")
    if (resume or inspect_only or event) and (
        state is None or not (state / "state.json").is_file()
    ):
        raise click.UsageError("This operation requires --state with an existing checkpoint")
    if definition and (resume or inspect_only or event):
        raise click.UsageError("Saved runs use their checkpoint definition; omit DEFINITION")
    if (inspect_only or event) and (simulate or server or output or backend):
        raise click.UsageError("Inspection and event submission do not take execution options")
    if (
        output
        and state
        and output.resolve()
        in {
            (state / name).resolve()
            for name in ("state.json", "inbox.json", "controller.lock", "inbox.lock")
        }
    ):
        raise click.UsageError("--output cannot overwrite a checkpoint or inbox")
    runner: WorkflowRunner | None = None
    try:
        store = RunStore(state) if state else None
        if inspect_only:
            assert store is not None
            click.echo(
                json.dumps(
                    {**store.load(), "inbox": [e.model_dump(mode="json") for e in store.events()]},
                    indent=2,
                )
            )
            return
        if event:
            assert store is not None
            incoming = EVENT_ADAPTER.validate_json(event.read_text())
            store.enqueue(incoming)
            click.echo(f"Queued event {incoming.id}; resume the run to process it.")
            return
        saved = store.load() if resume and store else None
        selected_mode = "server" if backend in {None, "sessions"} else backend
        if saved:
            checkpoint = Checkpoint.model_validate(saved)
            workflow, binding = checkpoint.workflow, checkpoint.binding
            if binding.mode == "custom":
                raise click.UsageError("Resume custom executors through the Python API")
            if (
                (simulate and binding.mode != "simulation")
                or (server and server != binding.server)
                or (backend and selected_mode != binding.mode)
            ):
                raise click.UsageError("Execution mode and server must match the checkpoint")
        else:
            if definition is None:
                raise click.UsageError("Supply a YAML DEFINITION for a new run")
            workflow = Workflow.model_validate(yaml.safe_load(definition.read_text()))
            binding = ExecutionBinding.model_validate(
                {
                    "mode": "simulation" if simulate else selected_mode,
                    "server": server,
                }
            )
        if validate_only:
            click.echo(f"Valid workflow: {workflow.name} ({len(workflow.nodes)} nodes)")
            return
        if binding.mode != "simulation" and not binding.server:
            raise click.UsageError("Choose --simulate or an explicit --server URL")

        validate_backend(workflow, binding)

        async def drive(execute: MemberExecutor) -> None:
            nonlocal runner
            validator = simulate_gate if binding.mode == "simulation" else execute_command_gate
            runner = (
                WorkflowRunner.restore(saved, execute, store=store, validate=validator)
                if saved
                else WorkflowRunner(
                    workflow, execute, store=store, validate=validator, binding=binding
                )
            )
            if runner.status in {"succeeded", "failed"}:
                return
            await runner.run()

        async def run() -> None:
            async with open_executor(binding) as execute:
                await drive(execute)

        try:
            asyncio.run(run())
        finally:
            if runner is not None:
                serialized = (
                    json.dumps(
                        {"simulated": binding.mode == "simulation", **runner.snapshot()}, indent=2
                    )
                    + "\n"
                )
                if output:
                    output.write_text(serialized)
                    click.echo(f"{runner.status}: report written to {output}")
                else:
                    click.echo(serialized, nl=False)
        if runner is not None and runner.status != "succeeded":
            raise click.exceptions.Exit(3 if runner.status == "waiting" else 1)
    except click.exceptions.Exit:
        raise
    except LockTimeout as exc:
        raise click.ClickException("Another controller or inbox writer owns this run") from exc
    except (OSError, ValueError, RuntimeError, yaml.YAMLError, ValidationError) as exc:
        raise click.ClickException(str(exc)) from exc


if __name__ == "__main__":
    main()
