"""First-class OMP terminal entrypoint using the shared native TUI lifecycle."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

import click

from omnigent._platform import resolve_cli_binary
from omnigent.harnesses.pi_native.main import (
    NativeTuiIdentity,
    _materialize_pi_agent_spec,
    run_pi_native,
)

OMP_NATIVE_IDENTITY = NativeTuiIdentity("omp", "OMP")


def resolve_omp_executable(
    *, env: Mapping[str, str] | None = None, which: Callable[[str], str | None] | None = None
) -> str:
    environ = os.environ if env is None else env
    command = environ.get("OMNIGENT_OMP_PATH", "").strip() or "omp"
    resolved = resolve_cli_binary(command, which=shutil.which if which is None else which)
    if resolved is None:
        raise click.ClickException(
            "Native OMP requires the 'omp' CLI on PATH. Install it with: "
            "npm install -g @oh-my-pi/pi-coding-agent, or set OMNIGENT_OMP_PATH=/path/to/omp."
        )
    return resolved


def _materialize_omp_agent_spec(tmpdir: Path) -> Path:
    return _materialize_pi_agent_spec(tmpdir, identity=OMP_NATIVE_IDENTITY)


def run_omp_native(
    *,
    server: str | None,
    session_id: str | None,
    extra_args: tuple[str, ...] | None = None,
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    run_pi_native(
        server=server,
        session_id=session_id,
        extra_args=extra_args,
        resume_picker=resume_picker,
        auto_open_conversation=auto_open_conversation,
        identity=OMP_NATIVE_IDENTITY,
    )
