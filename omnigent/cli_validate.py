"""Offline validation CLI; deliberately independent of omnigent.cli."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Sequence
from typing import Any

import click

from omnigent.cli_invocation import wrapper_required
from omnigent.spec.offline import (
    SKIPPED_CHECKS,
    Diagnostic,
    OfflineResult,
    invocation_error,
    validate_path,
)


def _emit(result: OfflineResult, *, json_output: bool) -> None:
    if json_output:
        click.echo(json.dumps(result.to_dict(), sort_keys=True, indent=2, ensure_ascii=True))
        return
    if result.exit_code == 0:
        click.echo("Supported offline checks passed; runtime readiness is not verified.")
    for diagnostic in result.diagnostics:
        location = f"{diagnostic.file}:{diagnostic.field}".strip(":")
        click.echo(f"{diagnostic.code} {location}: {diagnostic.message}")
    for code, reason in SKIPPED_CHECKS:
        click.echo(f"SKIPPED {code}: {reason}")


class _ValidationCommand(click.Command):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        **extra: Any,  # type: ignore[explicit-any]  # Click context keyword arguments
    ) -> Any:  # type: ignore[explicit-any]  # Click callback return value
        argv = list(args) if args is not None else sys.argv[1:]
        before_separator = argv[: argv.index("--")] if "--" in argv else argv
        try:
            code = super().main(
                args=argv,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                **extra,
            )
        except click.UsageError:
            # Click's messages echo unknown options and extra arguments verbatim.
            _emit(invocation_error(), json_output="--json" in before_separator)
            code = 2
        if standalone_mode:
            raise SystemExit(code or 0)
        return code


@click.command("validate", cls=_ValidationCommand)
@click.argument("path", required=False, default=".")
@click.option("--offline", is_flag=True, default=True, help="Use data-only checks (the default).")
@click.option("--json", "json_output", is_flag=True, help="Emit versioned JSON, including errors.")
@click.pass_context
def validate_command(ctx: click.Context, path: str, offline: bool, json_output: bool) -> None:
    """Validate a local AgentSpec v1 bundle or YAML without running agent code."""
    del offline  # There is intentionally no online mode.
    if wrapper_required(os.environ):
        result = OfflineResult(
            [
                Diagnostic(
                    "WRAPPER_REQUIRED", "This installation requires its configured CLI wrapper."
                )
            ],
            2,
        )
    else:
        result = validate_path(path)
    _emit(result, json_output=json_output)
    ctx.exit(result.exit_code)
