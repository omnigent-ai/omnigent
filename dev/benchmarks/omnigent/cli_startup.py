"""CLI startup latency benchmark.

Measures the wall-clock time from ``omnigent claude --server <url>`` invocation
to the Claude Code TUI being ready for input (signalled by "This session cost"
appearing in the output — the last line rendered before the prompt is active).

Unlike the HTTP/API benchmarks in ``run.py``, this drives the real CLI binary
end-to-end against a remote server: it exercises the full startup sequence
including auth, daemon tunnel, session create, runner launch, and terminal boot.

Usage::

    # Measure against the ai-devtools workspace (default), 5 runs:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py

    # Custom server, more runs:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py \\
        --server https://dbc-xxxx.cloud.databricks.com/api/2.0/omnigent \\
        --runs 10

    # Also run ``isaac omni`` for comparison:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --also-isaac-omni

    # Write JSON output:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --output startup.json

    # CI threshold gate (exit 1 if p50 > N ms):
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --max-p50-ms 12000

The JSON schema is compatible with the existing benchmark report so the same
Databricks ETL notebook can ingest it. The journey name is ``cli_startup``
(or ``isaac_omni`` for the isaac variant).
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dev.benchmarks.omnigent.schema import SCHEMA_VERSION, git_branch, git_sha, host_info

try:
    import pexpect

    _PEXPECT_AVAILABLE = True
except ImportError:
    _PEXPECT_AVAILABLE = False

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    _RICH_AVAILABLE = True
except ImportError:
    _RICH_AVAILABLE = False
    console = None  # type: ignore[assignment]

# Signal that the Claude terminal is ready — emitted by the startup spinner
# just before the tmux attach hands off to the Claude Code TUI. Using this
# rather than a signal from inside the TUI avoids needing a live tmux attach
# to complete successfully in the benchmark's PTY environment.
_READY_SIGNAL = "Claude terminal ready"

# Default remote server used by the ai-devtools workspace.
_DEFAULT_SERVER = "https://dbc-a5d4177a-49dc.cloud.databricks.com/api/2.0/omnigent"

# Timeout per startup attempt (seconds). Generous to handle slow terminal boots.
_TIMEOUT_S = 90


@dataclass
class StartupResult:
    """Results for one benchmark command over all runs.

    :param command: The command label, e.g. ``"omnigent claude --server"``.
    :param latencies_ms: Per-run wall-clock latency in milliseconds.
    :param failures: Failure reason mapped to count.
    """

    command: str
    latencies_ms: list[float] = field(default_factory=list)
    failures: dict[str, int] = field(default_factory=dict)


def _percentile(data: list[float], p: float) -> float:
    """Return the *p*-th percentile of *data* (0–100)."""
    if not data:
        return float("nan")
    sorted_data = sorted(data)
    idx = (p / 100) * (len(sorted_data) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_data) - 1)
    return sorted_data[lo] + (sorted_data[hi] - sorted_data[lo]) * (idx - lo)


def _measure_startup(cmd: list[str], *, timeout_s: float = _TIMEOUT_S) -> float:
    """Spawn *cmd*, wait for the TUI ready signal, return elapsed ms.

    Sends ``/exit`` once the signal is seen so the session is cleaned up
    (doesn't count toward the measurement).

    :param cmd: Command + args to spawn, e.g. ``["omnigent", "claude", "--server", ...]``.
    :param timeout_s: Max seconds to wait for the ready signal.
    :returns: Wall-clock milliseconds from spawn to ready signal.
    :raises RuntimeError: On timeout, spawn failure, or known server error.
    """
    if not _PEXPECT_AVAILABLE:
        raise RuntimeError(
            "pexpect is required for the CLI startup benchmark. "
            "Install it with: pip install pexpect"
        )

    env = dict(__import__("os").environ)
    start = time.perf_counter()
    child = pexpect.spawn(
        cmd[0],
        args=cmd[1:],
        timeout=timeout_s,
        encoding="utf-8",
        codec_errors="ignore",
        env=env,
    )
    try:
        idx = child.expect([pexpect.TIMEOUT, pexpect.EOF, _READY_SIGNAL])
        elapsed_ms = (time.perf_counter() - start) * 1000
        if idx == 0:
            raise RuntimeError(f"Timed out after {timeout_s}s waiting for TUI ready signal")
        if idx == 1:
            # Capture the output to provide an actionable error message.
            output = (child.before or "").strip()
            if "another replica" in output:
                raise RuntimeError(
                    "host is on another replica — stale daemon from a previous run. "
                    "Run `omnigent stop` to clear it and retry."
                )
            raise RuntimeError(
                f"Process exited before TUI ready signal appeared. Last output: {output[-200:]!r}"
            )
        # idx == 2: matched the ready signal
        child.sendline("/exit")
        child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=10)
    finally:
        if child.isalive():
            child.terminate(force=True)
    return elapsed_ms


def _run_benchmark(
    cmd: list[str],
    *,
    label: str,
    runs: int,
    verbose: bool = False,
) -> StartupResult:
    """Run the startup benchmark *runs* times and return collected results."""
    result = StartupResult(command=label)
    for i in range(runs):
        run_num = i + 1
        if verbose:
            print(f"  {label} run {run_num}/{runs}...", flush=True)
        try:
            elapsed_ms = _measure_startup(cmd)
            result.latencies_ms.append(elapsed_ms)
            if verbose:
                print(f"    → {elapsed_ms:.0f}ms", flush=True)
        except BaseException as exc:  # noqa: BLE001
            reason = type(exc).__name__
            result.failures[reason] = result.failures.get(reason, 0) + 1
            if verbose:
                print(f"    → FAILED: {exc}", flush=True)
    return result


def _print_table(results: list[StartupResult]) -> None:
    """Print a Rich summary table."""
    if not _RICH_AVAILABLE or console is None:
        _print_plain(results)
        return

    table = Table(title="CLI Startup Latency", show_header=True, header_style="bold")
    table.add_column("Command", style="cyan", no_wrap=True)
    table.add_column("Runs", justify="right")
    table.add_column("Failures", justify="right")
    table.add_column("Min (ms)", justify="right")
    table.add_column("Avg (ms)", justify="right")
    table.add_column("p50 (ms)", justify="right")
    table.add_column("p95 (ms)", justify="right")
    table.add_column("p99 (ms)", justify="right")
    table.add_column("Max (ms)", justify="right")

    for r in results:
        n_ok = len(r.latencies_ms)
        n_fail = sum(r.failures.values())
        if not r.latencies_ms:
            table.add_row(r.command, str(n_fail), str(n_fail), *["—"] * 7)
            continue
        table.add_row(
            r.command,
            str(n_ok),
            str(n_fail),
            f"{min(r.latencies_ms):.0f}",
            f"{statistics.mean(r.latencies_ms):.0f}",
            f"{_percentile(r.latencies_ms, 50):.0f}",
            f"{_percentile(r.latencies_ms, 95):.0f}",
            f"{_percentile(r.latencies_ms, 99):.0f}",
            f"{max(r.latencies_ms):.0f}",
        )

    console.print(table)


def _print_plain(results: list[StartupResult]) -> None:
    """Fallback plain-text summary."""
    for r in results:
        n_ok = len(r.latencies_ms)
        n_fail = sum(r.failures.values())
        if not r.latencies_ms:
            print(f"{r.command}: all {n_fail} run(s) failed")
            continue
        print(
            f"{r.command}: n={n_ok} failures={n_fail} "
            f"avg={statistics.mean(r.latencies_ms):.0f}ms "
            f"p50={_percentile(r.latencies_ms, 50):.0f}ms "
            f"p95={_percentile(r.latencies_ms, 95):.0f}ms "
            f"max={max(r.latencies_ms):.0f}ms"
        )


def _build_journey_entry(result: StartupResult) -> dict[str, Any]:
    """Build a journey entry matching the existing benchmark JSON schema."""
    runs_data = []
    for ms in result.latencies_ms:
        runs_data.append(
            {
                "n_success": 1,
                "n_failures": 0,
                "failures": {},
                "wall_time_s": ms / 1000,
                "mean_ms": ms,
                "p50_ms": ms,
                "p95_ms": ms,
                "p99_ms": ms,
                "max_ms": ms,
                "rps": None,
                "http_requests": None,
                "http_requests_per_op": None,
                "route_requests": {},
            }
        )
    n_ok = len(result.latencies_ms)
    summary: dict[str, Any] = {"runs_total": len(result.latencies_ms), "runs_ok": n_ok}
    if result.latencies_ms:
        summary.update(
            {
                "avg_mean_ms": statistics.mean(result.latencies_ms),
                "avg_p50_ms": _percentile(result.latencies_ms, 50),
                "avg_p95_ms": _percentile(result.latencies_ms, 95),
                "avg_p99_ms": _percentile(result.latencies_ms, 99),
                "avg_rps": None,
            }
        )
    return {
        "kind": "latency",
        "backend": "remote",
        "needs_runner": True,
        "runs": runs_data,
        "summary": summary,
    }


def _check_thresholds(
    results: list[StartupResult],
    *,
    max_p50_ms: float | None,
    max_p99_ms: float | None,
) -> bool:
    """Return True if all thresholds pass, False if any breach."""
    passed = True
    for r in results:
        if not r.latencies_ms:
            continue
        p50 = _percentile(r.latencies_ms, 50)
        p99 = _percentile(r.latencies_ms, 99)
        if max_p50_ms is not None and p50 > max_p50_ms:
            print(
                f"THRESHOLD BREACH: {r.command} p50={p50:.0f}ms > {max_p50_ms:.0f}ms",
                file=sys.stderr,
            )
            passed = False
        if max_p99_ms is not None and p99 > max_p99_ms:
            print(
                f"THRESHOLD BREACH: {r.command} p99={p99:.0f}ms > {max_p99_ms:.0f}ms",
                file=sys.stderr,
            )
            passed = False
    return passed


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark CLI startup latency (time-to-prompt) for omnigent claude.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        default=_DEFAULT_SERVER,
        help="Omnigent server URL passed to omnigent claude --server.",
    )
    parser.add_argument(
        "--runs",
        type=int,
        default=5,
        help="Number of startup attempts to time.",
    )
    parser.add_argument(
        "--also-isaac-omni",
        action="store_true",
        help="Also benchmark `isaac omni` end-to-end (includes isaac pre-config overhead).",
    )
    parser.add_argument(
        "--omnigent-bin",
        default=None,
        help="Path to the omnigent binary. Defaults to the one on PATH.",
    )
    parser.add_argument(
        "--isaac-bin",
        default=None,
        help="Path to the isaac binary. Defaults to the one on PATH.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write JSON results to this file (compatible with the benchmark schema).",
    )
    parser.add_argument(
        "--max-p50-ms",
        type=float,
        default=None,
        help="Fail (exit 1) if any command's p50 latency exceeds this value (ms).",
    )
    parser.add_argument(
        "--max-p99-ms",
        type=float,
        default=None,
        help="Fail (exit 1) if any command's p99 latency exceeds this value (ms).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print per-run timing as it runs.",
    )
    args = parser.parse_args()

    if not _PEXPECT_AVAILABLE:
        print(
            "ERROR: pexpect is required. Install with: pip install pexpect",
            file=sys.stderr,
        )
        sys.exit(1)

    omnigent_bin = args.omnigent_bin or shutil.which("omnigent")
    if omnigent_bin is None:
        print("ERROR: omnigent binary not found on PATH. Use --omnigent-bin.", file=sys.stderr)
        sys.exit(1)

    commands: list[tuple[str, list[str]]] = [
        (
            "omnigent claude --server",
            [omnigent_bin, "claude", "--server", args.server],
        ),
    ]

    if args.also_isaac_omni:
        isaac_bin = args.isaac_bin or shutil.which("isaac")
        if isaac_bin is None:
            print(
                "WARNING: isaac binary not found on PATH, skipping --also-isaac-omni.",
                file=sys.stderr,
            )
        else:
            commands.append(("isaac omni", [isaac_bin, "omni"]))

    all_results: list[StartupResult] = []
    for label, cmd in commands:
        print(f"\nBenchmarking: {label} ({args.runs} run(s))")
        result = _run_benchmark(cmd, label=label, runs=args.runs, verbose=args.verbose)
        all_results.append(result)

    print()
    _print_table(all_results)

    if args.output:
        journeys = {
            r.command.replace(" ", "_").replace("--", ""): _build_journey_entry(r)
            for r in all_results
        }
        report: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "git_sha": git_sha(),
            "git_branch": git_branch(),
            "host": host_info(),
            "harness": "cli-startup",
            "config": {
                "runs": args.runs,
                "server": args.server,
                "also_isaac_omni": args.also_isaac_omni,
            },
            "journeys": journeys,
        }
        output_path = Path(args.output)
        output_path.write_text(json.dumps(report, indent=2))
        print(f"\nResults written to {output_path}")

    if not _check_thresholds(all_results, max_p50_ms=args.max_p50_ms, max_p99_ms=args.max_p99_ms):
        sys.exit(1)


if __name__ == "__main__":
    main()
