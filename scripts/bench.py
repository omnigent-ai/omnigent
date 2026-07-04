#!/usr/bin/env python3
"""Run one evaluation task across multiple Omnigent harnesses.

Usage::

    python scripts/bench.py scripts/bench_tasks/sample.yaml --harness claude-sdk,codex
    python scripts/bench.py scripts/bench_tasks/sample.yaml --harness codex --json
    python scripts/bench.py task.yaml --harness claude-sdk \
        --markdown-out results.md --json-out results.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from omnigent.llms._usage_observer import add_observer, notify

_TOKEN_USAGE_ENV = "OMNIGENT_TOKEN_USAGE_JSON"
_SUCCESS_CHECK_TIMEOUT_S = 600
_SUCCESS_CHECK_ERROR_TAIL_CHARS = 500
_HARNESS_RUN_TIMEOUT_S = 1800


class HarnessRunner(Protocol):
    """Callable that runs one harness against one copied workspace."""

    def __call__(self, harness: str, workspace: Path, prompt: str) -> None: ...


@dataclass(frozen=True)
class SuccessCheck:
    type: str
    command: str


@dataclass(frozen=True)
class BenchTask:
    name: str
    workspace: Path
    prompt: str
    success: SuccessCheck


@dataclass(frozen=True)
class BenchResult:
    harness: str
    success: bool
    wall_time_s: float
    input_tokens: int
    output_tokens: int
    total_tokens: int
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "harness": self.harness,
            "success": self.success,
            "wall_time_s": self.wall_time_s,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "error": self.error,
        }


@dataclass(frozen=True)
class BenchComparison:
    task: str
    results: list[BenchResult]

    def to_dict(self) -> dict[str, object]:
        return {
            "task": self.task,
            "results": [result.to_dict() for result in self.results],
        }


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass(frozen=True)
class SuccessCheckResult:
    passed: bool
    error: str | None = None


def load_task(path: Path) -> BenchTask:
    """Load a Phase 1 benchmark task from YAML."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must contain a YAML mapping")

    success = raw.get("success")
    if not isinstance(success, dict):
        raise ValueError(f"{path} must define success as a mapping")
    success_type = success.get("type")
    command = success.get("command")
    if success_type != "command":
        raise ValueError(f"{path} success.type must be 'command'")
    if not isinstance(command, str) or not command:
        raise ValueError(f"{path} success.command must be a non-empty string")

    name = raw.get("name")
    workspace = raw.get("workspace")
    prompt = raw.get("prompt")
    if not isinstance(name, str) or not name:
        raise ValueError(f"{path} name must be a non-empty string")
    if not isinstance(workspace, str) or not workspace:
        raise ValueError(f"{path} workspace must be a non-empty string")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError(f"{path} prompt must be a non-empty string")

    workspace_path = (path.parent / workspace).resolve()
    if not workspace_path.is_dir():
        raise ValueError(f"{path} workspace must point to a directory: {workspace_path}")
    return BenchTask(
        name=name,
        workspace=workspace_path,
        prompt=prompt,
        success=SuccessCheck(type=success_type, command=command),
    )


def run_benchmark(
    task: BenchTask,
    harnesses: Sequence[str],
    *,
    runner: HarnessRunner | None = None,
) -> BenchComparison:
    """Run *task* once per harness and return comparison results."""
    if not harnesses:
        raise ValueError("at least one harness is required")

    harnesses = _dedupe_preserving_order(harnesses)
    active_runner = runner or default_harness_runner
    results = [_run_one(task, harness, active_runner) for harness in harnesses]
    return BenchComparison(task=task.name, results=results)


def render_markdown(comparison: BenchComparison) -> str:
    """Render a deterministic GitHub-flavored Markdown result table."""
    headers = [
        "Harness",
        "Success",
        "Wall time (s)",
        "Input tokens",
        "Output tokens",
        "Total tokens",
        "Error",
    ]
    rows = [
        [
            _markdown_cell(result.harness),
            _markdown_cell(str(result.success).lower()),
            _markdown_cell(f"{result.wall_time_s:.3f}"),
            _markdown_cell(str(result.input_tokens)),
            _markdown_cell(str(result.output_tokens)),
            _markdown_cell(str(result.total_tokens)),
            _markdown_cell(result.error or ""),
        ]
        for result in comparison.results
    ]
    headers = [_markdown_cell(header) for header in headers]
    widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(len(headers))]

    def fmt(row: Sequence[str]) -> str:
        cells = [cell.ljust(widths[index]) for index, cell in enumerate(row)]
        return "| " + " | ".join(cells) + " |"

    lines = [f"# {comparison.task}", "", fmt(headers)]
    lines.append("| " + " | ".join("-" * width for width in widths) + " |")
    lines.extend(fmt(row) for row in rows)
    return "\n".join(lines) + "\n"


def render_json(comparison: BenchComparison) -> str:
    """Render stable machine-readable results."""
    return json.dumps(comparison.to_dict(), indent=2, sort_keys=True) + "\n"


def default_harness_runner(harness: str, workspace: Path, prompt: str) -> None:
    """Run the live Omnigent headless prompt path for one harness."""
    from omnigent.chat import run_prompt
    from omnigent.cli import _materialize_harness_launcher_file

    launcher = _materialize_harness_launcher_file(
        harness=harness,
        model=None,
        system_prompt=None,
    )
    try:
        with tempfile.TemporaryDirectory(prefix="omnigent-bench-tokens-") as tmp:
            usage_path = Path(tmp) / "tokens.json"
            previous_usage_path = os.environ.get(_TOKEN_USAGE_ENV)
            had_previous_usage_path = _TOKEN_USAGE_ENV in os.environ
            os.environ[_TOKEN_USAGE_ENV] = str(usage_path)
            try:
                with _pushd(workspace):
                    run_prompt(
                        target=str(launcher),
                        client_tools=None,
                        harness=None,
                        model=None,
                        prompt=prompt,
                        system_prompt=None,
                        ephemeral=True,
                    )
            finally:
                os.environ.pop(_TOKEN_USAGE_ENV, None)
                totals = _sum_usage_files(Path(tmp))
                notify(
                    model=harness,
                    input_tokens=totals.input_tokens,
                    output_tokens=totals.output_tokens,
                    total_tokens=totals.total_tokens,
                )
                if had_previous_usage_path:
                    assert previous_usage_path is not None
                    os.environ[_TOKEN_USAGE_ENV] = previous_usage_path
    finally:
        shutil.rmtree(launcher.parent, ignore_errors=True)


def _run_with_timeout(
    runner: HarnessRunner,
    harness: str,
    workspace: Path,
    prompt: str,
    timeout_s: float,
) -> None:
    """Run *runner* on a daemon thread so a hung harness can't block the rest of the list.

    The harness run itself has no built-in timeout (unlike the success check), so a
    stuck harness/agent would otherwise block ``run_benchmark`` forever. The worker
    thread is a daemon: on timeout we abandon it and report a failure for this
    harness rather than waiting on it, so the script never blocks the remaining
    harnesses. The thread may still be running afterward, so callers must not
    treat post-timeout state (e.g. observed usage) as final without a lock.
    """
    outcome: dict[str, BaseException] = {}

    def target() -> None:
        try:
            runner(harness, workspace, prompt)
        except BaseException as exc:  # noqa: BLE001
            outcome["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise TimeoutError(f"harness {harness!r} did not finish within {timeout_s:.0f}s")
    error = outcome.get("error")
    if error is not None:
        raise error


def _run_one(task: BenchTask, harness: str, runner: HarnessRunner) -> BenchResult:
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    usage_lock = threading.Lock()

    def observe(
        *,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        total_tokens: int,
    ) -> None:
        del model
        with usage_lock:
            usage["input_tokens"] += input_tokens
            usage["output_tokens"] += output_tokens
            usage["total_tokens"] += total_tokens

    with tempfile.TemporaryDirectory(prefix="omnigent-bench-") as tmp:
        fresh_workspace = Path(tmp) / task.workspace.name
        shutil.copytree(task.workspace, fresh_workspace, symlinks=True)

        error: str | None = None
        started = time.perf_counter()
        remove_observer = add_observer(observe)
        try:
            _run_with_timeout(
                runner, harness, fresh_workspace, task.prompt, _HARNESS_RUN_TIMEOUT_S
            )
        except SystemExit as exc:
            error = f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        finally:
            wall_time_s = time.perf_counter() - started
            remove_observer()

        # A timed-out runner's daemon thread may still be writing to `usage` via
        # `observe()`; snapshot under the lock rather than reading the dict directly.
        with usage_lock:
            usage_snapshot = dict(usage)

        check_result = _run_success_check(task.success, fresh_workspace)
        success = error is None and check_result.passed
        if error is None:
            error = check_result.error
        return BenchResult(
            harness=harness,
            success=success,
            wall_time_s=wall_time_s,
            input_tokens=usage_snapshot["input_tokens"],
            output_tokens=usage_snapshot["output_tokens"],
            total_tokens=usage_snapshot["total_tokens"],
            error=error,
        )


def _run_success_check(success: SuccessCheck, workspace: Path) -> SuccessCheckResult:
    if success.type != "command":
        raise ValueError(f"unsupported success check type: {success.type}")
    try:
        completed = subprocess.run(
            success.command,
            cwd=workspace,
            shell=True,  # Success commands come from trusted task YAML.
            capture_output=True,
            text=True,
            check=False,
            timeout=_SUCCESS_CHECK_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return SuccessCheckResult(
            passed=False,
            error=f"success check timed out after {_SUCCESS_CHECK_TIMEOUT_S}s",
        )
    if completed.returncode == 0:
        return SuccessCheckResult(passed=True)

    output = _tail_text(completed.stderr or completed.stdout or "")
    suffix = f": {output}" if output else ""
    return SuccessCheckResult(
        passed=False,
        error=f"success check failed (exit {completed.returncode}){suffix}",
    )


def _sum_usage_files(directory: Path) -> UsageTotals:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for path in directory.iterdir():
        if not path.is_file() or "current-test" in path.name or ".tmp" in path.name:
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            totals = raw.get("totals")
            if not isinstance(totals, dict):
                continue
            input_tokens += int(totals.get("input_tokens") or 0)
            output_tokens += int(totals.get("output_tokens") or 0)
            total_tokens += int(totals.get("total_tokens") or 0)
        except (OSError, json.JSONDecodeError, TypeError, UnicodeDecodeError, ValueError):
            continue
    return UsageTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _tail_text(text: str | bytes) -> str:
    if isinstance(text, bytes):
        text = text.decode(errors="replace")
    compact = " ".join(text.split())
    return compact[-_SUCCESS_CHECK_ERROR_TAIL_CHARS:]


def _markdown_cell(value: str) -> str:
    return value.replace("\r", " ").replace("\n", " ").replace("|", r"\|")


def _dedupe_preserving_order(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


@contextlib.contextmanager
def _pushd(path: Path) -> Iterator[None]:
    previous = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(previous)


def _parse_harnesses(raw: str) -> list[str]:
    harnesses = [item.strip() for item in raw.split(",") if item.strip()]
    if not harnesses:
        raise argparse.ArgumentTypeError("--harness must name at least one harness")
    return harnesses


def _parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", type=Path, help="Path to a benchmark task YAML file")
    parser.add_argument(
        "--harness",
        required=True,
        type=_parse_harnesses,
        help="Comma-separated harness names, e.g. claude-sdk,codex",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON results to stdout")
    parser.add_argument(
        "--markdown",
        action="store_true",
        help="Print Markdown results to stdout. This is the default when --json is absent.",
    )
    parser.add_argument("--json-out", type=Path, help="Write JSON results to this path")
    parser.add_argument("--markdown-out", type=Path, help="Write Markdown results to this path")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    task = load_task(args.task)
    comparison = run_benchmark(task, args.harness)
    markdown = render_markdown(comparison)
    json_text = render_json(comparison)

    if args.markdown_out is not None:
        args.markdown_out.parent.mkdir(parents=True, exist_ok=True)
        args.markdown_out.write_text(markdown, encoding="utf-8")
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json_text, encoding="utf-8")

    if args.markdown or not args.json:
        print(markdown, end="")
    if args.json:
        print(json_text, end="")
    # Completed comparisons return 0 even when a harness fails the task.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
