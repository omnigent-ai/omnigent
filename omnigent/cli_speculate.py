"""``omnigent speculate``: run K replicas of one task and promote only verified bytes."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

import click

from omnigent.host.speculate import MAX_REPLICAS, Drafter, SpeculationError, Verifier, speculate

_REPLICA_TIMEOUT_S = 1800.0


class _Command(click.ParamType):
    """A shell-quoted command whose executable exists, parsed to argv."""

    name = "cmd"

    def convert(
        self, value: str, param: click.Parameter | None, ctx: click.Context | None
    ) -> list[str]:
        try:
            argv = shlex.split(value)
        except ValueError as exc:
            self.fail(str(exc), param, ctx)
        if not argv or shutil.which(argv[0]) is None:
            self.fail(f"command not found: {value!r}", param, ctx)
        return argv


@click.command("speculate")
@click.argument("task")
@click.option(
    "--replica",
    "replicas",
    type=_Command(),
    multiple=True,
    required=True,
    help="Command for one replica. TASK is appended as its last argument, and it runs with "
    "its own worktree as its cwd. Repeat to mix harnesses; replicas cycle through them.",
)
@click.option(
    "--verify",
    type=_Command(),
    required=True,
    help="Verifier command; exit 0 passes. It runs on a fresh copy of the exact candidate "
    "bytes, outside every replica, so it must import code from its cwd (for example "
    "python -m pytest), not from an editable install. The copy has no ignored files and no "
    ".git, so a verifier that needs dependencies can install them itself, e.g. "
    "--verify \"sh -c 'npm ci && npm test'\".",
)
@click.option(
    "-k",
    type=click.IntRange(1, MAX_REPLICAS),
    default=4,
    show_default=True,
    help="Number of replicas.",
)
@click.option(
    "--protect",
    multiple=True,
    metavar="PATH",
    help="Path a candidate may not change, e.g. tests/. It must match a checkpoint file.",
)
def speculate_command(
    task: str,
    replicas: tuple[list[str], ...],
    verify: list[str],
    k: int,
    protect: tuple[str, ...],
) -> None:
    """Run K replicas of TASK from this checkout and promote only verified bytes.

    The checkpoint includes uncommitted and untracked files. Each replica works in its own
    detached git worktree for up to two rounds, and its exit status is ignored. Each
    distinct candidate is verified once, as soon as its replica exits. The first to pass
    wins, and the other replicas are killed. When no replica passes, the controller
    composes their edits and verifies that candidate too. Prints the ledger as JSON.

    Exit status 0 means a branch speculate/<run> was promoted from the checkpoint, or the
    checkpoint already passes. 1 means no candidate passed. 2 means a usage or runtime
    error, reported on stderr with nothing on stdout. On SIGINT, SIGTERM or SIGHUP it kills
    every replica and exits with 128 plus the signal number, e.g. 130 for Ctrl-C.

    \b
    Example:
      omnigent speculate "make tests/test_pager.py pass" --protect tests/ \\
        --replica "claude -p --permission-mode acceptEdits" \\
        --replica "codex exec -s workspace-write" \\
        --verify "python -m pytest -q tests/test_pager.py"
    """
    if not hasattr(os, "killpg"):
        click.echo("Error: omnigent speculate needs a POSIX system (Linux or macOS)", err=True)
        raise SystemExit(2)
    if len(replicas) > k:
        raise click.UsageError(f"-k {k} is less than the {len(replicas)} --replica commands")
    drafters = [_drafter(replicas[i % len(replicas)]) for i in range(k)]
    verifier = Verifier(tuple(verify), protect)

    def interrupt(signum: int, _frame: object) -> None:
        # Replicas run in their own sessions, so no signal sent here reaches them. Unwinding
        # sets their stop event, so each is killed before its worktree is removed.
        raise SystemExit(128 + signum)

    stops = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, interrupt) for sig in stops}
    try:
        outcome = speculate(Path.cwd(), task, drafters=drafters, verifier=verifier)
    except SpeculationError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(2) from exc
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    promotion = outcome.promotion
    report = {
        "run_id": outcome.run_id,
        "checkpoint": outcome.checkpoint,
        "admitted": outcome.admitted,
        "rounds": outcome.rounds,
        "ledger": [{**asdict(e.receipt), "attempts": list(e.attempts)} for e in outcome.ledger],
        "promoted": promotion.evidence.receipt.tree if promotion else None,
        "branch": outcome.branch,
        "commit": promotion.commit if promotion else None,
        "composed_only_win": outcome.composed_only_win,
    }
    click.echo(json.dumps(report, indent=2))
    if outcome.admitted and promotion is None:
        raise SystemExit(1)


def _drafter(argv: list[str]) -> Drafter:
    """Run ``argv`` plus the prompt in a replica's directory, its stdout sent to stderr.

    Only a command that cannot start raises. Its exit status is reported, never trusted.
    The replica's process group is killed once the stop event is set.
    """

    def draft(workdir: Path, prompt: str, stop: threading.Event) -> None:
        proc = subprocess.Popen(
            [*argv, prompt],
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=sys.stderr,
            start_new_session=True,
        )
        deadline = time.monotonic() + _REPLICA_TIMEOUT_S
        try:
            # Short waits on the event, so a replica the run no longer needs dies promptly.
            while proc.poll() is None and not stop.wait(0.1):
                if time.monotonic() > deadline:
                    # The edits made before the timeout are still verified.
                    limit = f"{_REPLICA_TIMEOUT_S:.0f}s"
                    click.echo(f"replica in {workdir} timed out after {limit}", err=True)
                    break
            if status := proc.returncode:
                click.echo(f"replica in {workdir} exited with status {status}", err=True)
        finally:
            # Nothing the replica started may keep editing after its snapshot. macOS raises
            # EPERM for a group whose only member is an unreaped zombie.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()

    return draft
