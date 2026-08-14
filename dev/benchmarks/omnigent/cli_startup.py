"""Convenience wrapper: run only the ``cli_startup`` journey.

This script is a thin shim around ``run.py --journeys cli_startup``. Prefer
calling ``run.py`` directly for full control over flags::

    # Equivalent to this script with --runs 3:
    uv run --no-sync dev/benchmarks/omnigent/run.py \\
        --journeys cli_startup --runs 3

    # With isaac omni comparison (also registered as a journey):
    uv run --no-sync dev/benchmarks/omnigent/run.py \\
        --journeys cli_startup,isaac_omni_startup --runs 5

Environment variables:

    OMNIGENT_BENCH_SERVER   Remote server URL (default: ai-devtools workspace).
    OMNIGENT_BIN            Path to the omnigent binary (default: PATH lookup).
    OMNIGENT_REMOTE_AUTH_TOKEN  Pre-minted bearer token (optional; falls back
                            to stored OIDC token / Databricks SDK credentials).

Usage::

    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --runs 10
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --output out.json
    uv run --no-sync dev/benchmarks/omnigent/cli_startup.py --max-p50-ms 12000
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow ``uv run <path>`` to import the sibling modules.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import argparse

from dev.benchmarks.omnigent.run import main as run_main


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Benchmark CLI startup latency via omnigent claude --server.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=True,
    )
    parser.add_argument("--runs", type=int, default=5, help="Timed runs.")
    parser.add_argument("--output", default=None, help="JSON output file.")
    parser.add_argument(
        "--max-p50-ms", type=float, default=None, help="Fail if p50 exceeds this (ms)."
    )
    parser.add_argument(
        "--max-p99-ms", type=float, default=None, help="Fail if p99 exceeds this (ms)."
    )
    args = parser.parse_args()

    argv = ["--journeys", "cli_startup", "--runs", str(args.runs)]
    if args.output:
        argv += ["--output", args.output]
    if args.max_p50_ms is not None:
        argv += ["--max-p50-ms", str(args.max_p50_ms)]
    if args.max_p99_ms is not None:
        argv += ["--max-p99-ms", str(args.max_p99_ms)]

    sys.argv = ["run.py", *argv]
    run_main()


if __name__ == "__main__":
    main()
