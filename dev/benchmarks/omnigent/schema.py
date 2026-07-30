"""Benchmark report schema + metadata capture.

:func:`build_report` assembles the single JSON document the harness writes.
Its per-journey ``summary`` + ``runs`` shape mirrors MLflow's gateway
benchmark so the workspace ETL notebook flattens it unchanged — keyed by
journey (and ``harness``) instead of ``backend``. Bump :data:`SCHEMA_VERSION`
whenever the document's shape changes so the ETL can branch on it.
"""

from __future__ import annotations

import platform
import subprocess

# Incremented on any breaking change to the report document shape below.
# v4: per-journey ``summary`` gained ``runs_total`` / ``runs_ok`` (and omits the
# metric keys when every run failed); a journey that errored out of measurement
# entirely carries ``skipped: true`` + ``error`` with empty ``runs``/``summary``.
# v5: each run row gained ``http_requests`` / ``http_requests_per_op`` (server
# HTTP requests handled during the timed region, counted via the CI-only debug
# endpoint; ``null`` when uncounted); ``summary`` gains
# ``avg_http_requests_per_op`` when any run was counted; ``config`` gains
# ``network_delay_ms``.
# v6: each run row gained ``route_requests`` (per-route breakdown of
# ``http_requests``, ``"METHOD /route" -> count``); ``summary`` gains a
# ``network_routes`` appendix (``[{route, requests, per_op}]`` sorted by
# ``per_op`` desc) when any run recorded a breakdown.
SCHEMA_VERSION = 6


def _git(*args: str) -> str:
    """Run ``git *args`` at the repo root, returning stripped stdout or ``""``.

    Never raises: a missing git, detached checkout, or non-zero exit all
    surface as an empty string so a benchmark run outside a clean checkout
    still produces a valid report.
    """
    try:
        out = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_sha() -> str:
    """Return the current commit SHA, or ``""`` when unavailable."""
    return _git("rev-parse", "HEAD")


def git_branch() -> str:
    """Return the current branch name, or ``""`` when detached/unavailable."""
    return _git("rev-parse", "--abbrev-ref", "HEAD")


def host_info() -> dict[str, object]:
    """Capture coarse host facts for cross-machine result comparison."""
    import os

    return {
        "platform": platform.platform(),
        "python": platform.python_version(),
        "cpu_count": os.cpu_count(),
    }


def build_report(
    journey_results: dict[str, dict[str, object]],
    *,
    generated_at: str,
    config: dict[str, object],
    harness: str,
    resource_usage: dict[str, object] | None = None,
) -> dict[str, object]:
    """Assemble the full benchmark report document.

    :param journey_results: Per-journey ``{"kind", "runs", "summary"}``
        blocks (each ``runs``/``summary`` produced by
        :func:`measure.aggregate`), keyed by journey name.
    :param generated_at: ISO-8601 timestamp stamped by the caller (kept out
        of this pure function so it stays deterministic under test).
    :param config: The run's knobs (iterations, requests, concurrency, runs,
        mock_llm) for provenance.
    :param harness: Harness driving full-turn journeys, e.g.
        ``"openai-agents"``.
    :param resource_usage: Optional server-process CPU/memory stats collected
        during the run (see :meth:`BenchEnvironment.resource_usage`).
    :returns: The JSON-serializable report document.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "git_sha": git_sha(),
        "git_branch": git_branch(),
        "host": host_info(),
        "harness": harness,
        "config": config,
        "resource_usage": resource_usage or {},
        "journeys": journey_results,
    }
