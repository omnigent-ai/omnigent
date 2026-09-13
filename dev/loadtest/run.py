#!/usr/bin/env python3
"""Runner for the Omnigent host load test — boot a local stack, drive real turns.

One command that boots the whole stack, runs the load, and writes a report.
Because turns execute on the **host** (a runner subprocess it spawns → LLM), and
the LLM is **mocked**, this test owns its target: it boots a local ``omnigent
server`` + a zero-latency mock LLM (reusing the benchmark harness's
``BenchEnvironment``), registers one agent, then runs Locust where **each user
is a real ``omnigent host``** that creates host-bound sessions and drives real
multi-turn conversations (see ``omnigent_load_test.py``).

There is no ``--server`` to point at — mocking the LLM requires a stack we
control. ``-u N`` scales the number of hosts; each host spawns real runner
subprocesses per session, so N×M runners run on THIS machine — keep N modest
(capacity-limited by design; it drives genuine turns rather than faking them).

Writes a timestamped result set:

    <out-dir>/
      run_config.json         inputs + resolved locust argv + outcome
      report_stats.csv        locust per-request stats (raw)
      report_failures.csv     locust failure breakdown (raw)
      report_stats_history.csv  per-10s time series (raw)
      report.html             locust's own HTML report
      console.log             full locust stdout/stderr
      summary.md              human-readable latency write-up (this tool)

Runs from a repo checkout only (imports ``dev.benchmarks`` + ``tests``), with
the ``loadtest`` and ``agents-sdk`` extras. Knobs: ``--users`` (N hosts),
``--spawn-rate``, ``--run-time``, ``--sessions-per-user``, ``--turns-per-session``,
``--reply-words``, ``--out-dir``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime
import json
import subprocess
import sys
from pathlib import Path

# Repo root so ``dev.benchmarks`` and ``tests`` import when run as a script.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_LOCUSTFILE = Path(__file__).with_name("omnigent_load_test.py")
_RESULTS_ROOT = Path(__file__).with_name("results")

# Prefix locust's --csv writes: "<prefix>_stats.csv", "<prefix>_failures.csv", …
_CSV_PREFIX = "report"


def _build_parser() -> argparse.ArgumentParser:
    """Build the runner's argument parser."""
    parser = argparse.ArgumentParser(
        description="Boot a local Omnigent stack and load-test it with real host turns.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--users", type=int, default=4, help="Concurrent hosts (locust -u).")
    parser.add_argument(
        "--spawn-rate", type=float, default=1.0, help="Hosts started per second (locust -r)."
    )
    parser.add_argument("--run-time", default="120s", help="Run duration, e.g. 120s / 5m.")
    parser.add_argument(
        "--sessions-per-user",
        type=int,
        default=2,
        help="Host-bound sessions each host drives, sequentially.",
    )
    parser.add_argument(
        "--turns-per-session",
        type=int,
        default=4,
        help="Turns per session — conversation history grows across them.",
    )
    parser.add_argument(
        "--reply-words",
        type=int,
        default=60,
        help="Word count of the mocked (streamed) assistant reply each turn.",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Result directory. Default: dev/loadtest/results/omnigent_load_test-<timestamp>.",
    )
    return parser


def _resolve_out_dir(out_dir: str | None) -> Path:
    """Resolve (and create) the result directory for this run."""
    if out_dir:
        out = Path(out_dir)
    else:
        stamp = datetime.datetime.now(datetime.timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
        out = _RESULTS_ROOT / f"omnigent_load_test-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _mock_reply(word_count: int) -> str:
    """Build a deterministic multi-sentence reply of roughly *word_count* words."""
    words = [
        "This",
        "is",
        "a",
        "mocked",
        "assistant",
        "reply",
        "used",
        "to",
        "exercise",
        "a",
        "real",
        "host",
        "turn.",
    ]
    out: list[str] = []
    while len(out) < word_count:
        out.extend(words)
    return " ".join(out[:word_count]) + "."


def _build_locust_argv(args: argparse.Namespace, server_url: str, out_dir: Path) -> list[str]:
    """Assemble the ``locust`` argv (current interpreter, headless, CSV+HTML).

    Launches ``sys.executable -m locust`` — never a bare ``locust`` off PATH,
    which can resolve to a broken locust from another Python.
    """
    return [
        sys.executable,
        "-m",
        "locust",
        "-f",
        str(_LOCUSTFILE),
        "--host",
        server_url,
        "-u",
        str(args.users),
        "-r",
        str(args.spawn_rate),
        "-t",
        args.run_time,
        "--headless",
        "--csv",
        str(out_dir / _CSV_PREFIX),
        "--html",
        str(out_dir / "report.html"),
    ]


def _read_stats(stats_csv: Path) -> list[dict[str, str]]:
    """Read a locust ``_stats.csv`` into row dicts; empty if the file is absent."""
    if not stats_csv.is_file():
        return []
    with stats_csv.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _fmt_num(raw: str) -> str:
    """Format a numeric CSV cell to one decimal, or ``"-"`` if blank.

    Used for latency (ms) and the requests/s rate alike, so it is named for the
    formatting, not a unit.
    """
    if raw is None or raw == "":
        return "-"
    try:
        return f"{float(raw):.1f}"
    except ValueError:
        return raw


def _write_run_config(
    out_dir: Path, args: argparse.Namespace, argv: list[str], exit_code: int
) -> None:
    """Record inputs, resolved locust argv, and outcome (no secrets involved)."""
    config = {
        "scenario": "omnigent_load_test",
        "users_hosts": args.users,
        "spawn_rate": args.spawn_rate,
        "run_time": args.run_time,
        "sessions_per_user": args.sessions_per_user,
        "turns_per_session": args.turns_per_session,
        "reply_words": args.reply_words,
        "harness": "openai-agents",
        "model": "mock (zero-latency)",
        "locust_argv": argv,
        "exit_code": exit_code,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (out_dir / "run_config.json").write_text(json.dumps(config, indent=2))


def _write_summary(
    out_dir: Path, args: argparse.Namespace, rows: list[dict[str, str]], exit_code: int
) -> None:
    """Write the human-readable ``summary.md`` from parsed locust stats."""
    lines: list[str] = []
    lines.append("# Load test results — omnigent_load_test (real host turns, mocked LLM)")
    lines.append("")
    lines.append(
        f"- **Load:** {args.users} concurrent hosts, each driving "
        f"{args.sessions_per_user} session(s) × {args.turns_per_session} turns per "
        f"iteration, repeated for {args.run_time} (see the turn count below)"
    )
    lines.append(f"- **Mock reply:** ~{args.reply_words} words, streamed; harness openai-agents")
    outcome = "PASS — no request failures" if exit_code == 0 else f"FAIL — locust exit {exit_code}"
    lines.append(f"- **Outcome:** {outcome}")
    lines.append("")

    # Locust's CSV includes an "Aggregated" row that already sums the per-name
    # rows — skip it so the headline count doesn't double.
    total_reqs = 0
    total_fails = 0
    for row in rows:
        if row.get("Name") == "Aggregated":
            continue
        try:
            total_reqs += int(row.get("Request Count", "0") or "0")
            total_fails += int(row.get("Failure Count", "0") or "0")
        except ValueError:
            pass
    fail_pct = (100.0 * total_fails / total_reqs) if total_reqs else 0.0
    lines.append(f"**{total_reqs} operations, {total_fails} failures ({fail_pct:.2f}%).**")
    lines.append("")

    lines.append("## Latency by operation (ms)")
    lines.append("")
    lines.append("| Operation | # | Fails | Avg | Med | p95 | p99 | Max | Ops/s |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for row in rows:
        lines.append(
            "| {name} | {n} | {fails} | {avg} | {med} | {p95} | {p99} | {mx} | {rps} |".format(
                name=row.get("Name", "?"),
                n=row.get("Request Count", "-"),
                fails=row.get("Failure Count", "-"),
                avg=_fmt_num(row.get("Average Response Time", "")),
                med=_fmt_num(row.get("Median Response Time", "")),
                p95=_fmt_num(row.get("95%", "")),
                p99=_fmt_num(row.get("99%", "")),
                mx=_fmt_num(row.get("Max Response Time", "")),
                rps=_fmt_num(row.get("Requests/s", "")),
            )
        )
    lines.append("")

    failures_csv = out_dir / f"{_CSV_PREFIX}_failures.csv"
    fail_rows = _read_stats(failures_csv)
    real_fails = [r for r in fail_rows if (r.get("Occurrences") or r.get("Occurences"))]
    if real_fails:
        lines.append("## Failures")
        lines.append("")
        lines.append("| Operation | Error | Count |")
        lines.append("|---|---|--:|")
        for r in real_fails:
            lines.append(
                "| {name} | {err} | {n} |".format(
                    name=r.get("Name", "?"),
                    err=(r.get("Error", "") or "").replace("|", "\\|")[:200],
                    n=r.get("Occurrences") or r.get("Occurences") or "-",
                )
            )
        lines.append("")

    lines.append("## Reading the numbers")
    lines.append("")
    lines.append(
        "- **turn** is the headline: one full post→idle agent turn that ran on a "
        "simulated host's runner (mocked LLM), so it is Omnigent's own per-turn "
        "overhead, not provider latency."
    )
    lines.append(
        "- Turn latency **grows across a conversation** as history accumulates, so "
        "the tail (p95/p99/max) reflects the later, longer turns."
    )
    lines.append(
        "- **host online** is the cost of a host subprocess registering over the "
        "host tunnel; **session create** is the host-bound create."
    )
    lines.append(
        "- **Fails** — any non-zero value means operations errored; see the Failures "
        "section and `console.log`. This is capacity-limited: N hosts × M sessions "
        "means N×M real runner processes on this box, so failures at high N usually "
        "mean the load box (not the server) is saturated."
    )
    lines.append("")
    lines.append("Raw data: `report_stats.csv`, `report_stats_history.csv`, `report.html`.")
    lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines))


async def _boot_and_run(args: argparse.Namespace, out_dir: Path) -> int:
    """Boot the stack, register the agent + mock reply, run locust, write results."""
    from dev.benchmarks.omnigent.environment import BenchEnvironment

    print(
        f"Booting local stack (server + mock LLM) for {args.users} hosts × "
        f"{args.sessions_per_user} sessions × {args.turns_per_session} turns …",
        flush=True,
    )
    async with BenchEnvironment(with_runner=True) as env:
        # Every host-bound session's turn hits the mock; a streamed multi-sentence
        # reply makes each turn do real streaming-pipeline work at zero model latency.
        await env.set_mock_fallback(_mock_reply(args.reply_words), stream=True)
        agent_name = await env.ensure_agent("loadtest-agent")
        agent_id = await env.agent_id(agent_name)
        print(f"  stack up: {env.base_url}  (agent {agent_id})", flush=True)

        argv = _build_locust_argv(args, env.base_url, out_dir)
        child_env = {
            **_os_environ(),
            "LOADTEST_SERVER_URL": env.base_url,
            "LOADTEST_AGENT_ID": agent_id,
            "LOADTEST_MOCK_URL": env.mock_url,
            "LOADTEST_WORKSPACE_ROOT": str(out_dir / "host-workspaces"),
            "SESSIONS_PER_USER": str(args.sessions_per_user),
            "TURNS_PER_SESSION": str(args.turns_per_session),
        }
        print(f"$ {' '.join(argv)}\n  results → {out_dir}", flush=True)

        console_log = out_dir / "console.log"
        # Locust is gevent-based; run it as a subprocess (not in this asyncio
        # loop) and stream its output to the console + console.log.
        with console_log.open("w") as log_fh:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                env=child_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async for raw in proc.stdout:
                line = raw.decode(errors="replace")
                sys.stdout.write(line)
                log_fh.write(line)
            exit_code = await proc.wait()

    _write_run_config(out_dir, args, argv, exit_code)
    rows = _read_stats(out_dir / f"{_CSV_PREFIX}_stats.csv")
    _write_summary(out_dir, args, rows, exit_code)
    print(f"\nResults written to {out_dir}\n  summary: {out_dir / 'summary.md'}", flush=True)
    return exit_code


def _os_environ() -> dict[str, str]:
    """Return a copy of the current environment (import kept local to helper)."""
    import os

    return dict(os.environ)


def main() -> int:
    """Parse inputs and run."""
    args = _build_parser().parse_args()
    import importlib.util

    for pkg, mod in (("locust", "locust"), ("openai-agents", "agents")):
        if importlib.util.find_spec(mod) is None:
            sys.exit(
                f"{pkg} not importable under {sys.executable} — install the extras: "
                "uv sync --extra loadtest --extra agents-sdk (run from a repo checkout)."
            )
    out_dir = _resolve_out_dir(args.out_dir)
    return asyncio.run(_boot_and_run(args, out_dir))


if __name__ == "__main__":
    raise SystemExit(main())
