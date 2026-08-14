"""CLI startup latency benchmark.

Measures the wall-clock time from ``omnigent claude --server <url>`` invocation
to the Claude terminal being ready (signalled by ``"Claude terminal ready."``
emitted by the startup spinner just before ``tmux attach``).

Unlike the HTTP/API benchmarks in ``run.py``, this drives the real CLI binary
end-to-end against a remote server: it exercises the full startup sequence
including auth, daemon tunnel, session create, runner launch, and terminal boot.

Each ``--runs`` invocation is one :class:`~measure.RunResult` containing all
latency samples — the same shape the HTTP/API journeys use — so JSON output is
directly compatible with the existing benchmark schema and the workspace ETL
notebook.

Usage::

    # Measure against the ai-devtools workspace (default), 5 runs:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py

    # Custom server, more runs:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py \\
        --server https://dbc-xxxx.cloud.databricks.com/api/2.0/omnigent \\
        --runs 10

    # Also benchmark ``isaac omni`` for comparison:
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --also-isaac-omni

    # Write JSON output (compatible with existing benchmark schema):
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --output startup.json

    # CI threshold gate (exit 1 if p50 > N ms):
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --max-p50-ms 12000
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from dev.benchmarks.omnigent.measure import RunResult, aggregate, check_thresholds, print_results
from dev.benchmarks.omnigent.schema import build_report

try:
    import pexpect

    _PEXPECT_AVAILABLE = True
except ImportError:
    _PEXPECT_AVAILABLE = False

# Signal that the Claude terminal is ready — emitted by the startup spinner
# just before the tmux attach hands off to the Claude Code TUI. Using this
# rather than a signal from inside the TUI avoids needing a live tmux attach
# to complete successfully in the benchmark's PTY environment.
_READY_SIGNAL = "Claude terminal ready"

# Default remote server used by the ai-devtools workspace.
_DEFAULT_SERVER = "https://dbc-a5d4177a-49dc.cloud.databricks.com/api/2.0/omnigent"

# Timeout per startup attempt (seconds). Generous to handle slow terminal boots.
_TIMEOUT_S = 90


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

    start = time.perf_counter()
    child = pexpect.spawn(
        cmd[0],
        args=cmd[1:],
        timeout=timeout_s,
        encoding="utf-8",
        codec_errors="ignore",
        env=dict(os.environ),
    )
    try:
        idx = child.expect([pexpect.TIMEOUT, pexpect.EOF, _READY_SIGNAL])
        elapsed_ms = (time.perf_counter() - start) * 1000
        if idx == 0:
            raise RuntimeError(f"Timed out after {timeout_s}s waiting for TUI ready signal")
        if idx == 1:
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


def _run_journey(
    cmd: list[str],
    *,
    label: str,
    runs: int,
    verbose: bool = False,
) -> list[RunResult]:
    """Run the startup benchmark, returning one RunResult per run."""
    results: list[RunResult] = []
    for i in range(runs):
        run_num = i + 1
        if verbose:
            print(f"  {label} run {run_num}/{runs}...", flush=True)
        result = RunResult()
        run_start = time.perf_counter()
        try:
            elapsed_ms = _measure_startup(cmd)
            result.latencies_ms.append(elapsed_ms)
            if verbose:
                print(f"    → {elapsed_ms:.0f}ms", flush=True)
        except BaseException as exc:  # noqa: BLE001
            result.record_failure(type(exc).__name__)
            if verbose:
                print(f"    → FAILED: {exc}", flush=True)
        result.wall_time = time.perf_counter() - run_start
        results.append(result)
    return results


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
        ("cli_startup", [omnigent_bin, "claude", "--server", args.server]),
    ]

    if args.also_isaac_omni:
        isaac_bin = args.isaac_bin or shutil.which("isaac")
        if isaac_bin is None:
            print(
                "WARNING: isaac binary not found on PATH, skipping --also-isaac-omni.",
                file=sys.stderr,
            )
        else:
            commands.append(("isaac_omni", [isaac_bin, "omni"]))

    journey_results: dict[str, dict] = {}
    all_run_results: dict[str, list[RunResult]] = {}

    for journey_name, cmd in commands:
        label = cmd[0] + (" omni" if journey_name == "isaac_omni" else " claude --server")
        print(f"\nBenchmarking: {label} ({args.runs} run(s))")
        run_results = _run_journey(cmd, label=label, runs=args.runs, verbose=args.verbose)
        all_run_results[journey_name] = run_results
        aggregated = aggregate(run_results)
        journey_results[journey_name] = {
            "kind": "latency",
            "backend": "remote",
            "needs_runner": True,
            **aggregated,
        }

    print()
    for journey_name, run_results in all_run_results.items():
        print_results(journey_name, run_results)

    if args.output:
        report = build_report(
            journey_results,
            generated_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
            config={
                "runs": args.runs,
                "server": args.server,
                "also_isaac_omni": args.also_isaac_omni,
            },
            harness="cli-startup",
        )
        Path(args.output).write_text(__import__("json").dumps(report, indent=2))
        print(f"\nResults written to {args.output}")

    # Threshold check: apply thresholds to each journey's runs.
    passed = True
    for _journey_name, run_results in all_run_results.items():
        if not check_thresholds(
            run_results, max_p50_ms=args.max_p50_ms, max_p99_ms=args.max_p99_ms
        ):
            passed = False
    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
