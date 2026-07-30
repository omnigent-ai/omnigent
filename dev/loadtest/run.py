#!/usr/bin/env python3
"""Runner for the Omnigent load test — server + host + load params → run + report.

One command with explicit inputs, rather than a long ``locust -f … -e KEY VALUE``
line, that also collects results into a timestamped directory:

    python dev/loadtest/run.py \
        --server http://localhost:8000 \
        --host-id host_abc123 \
        --users 50 --spawn-rate 5 --run-time 60s

It resolves the target server (→ locust ``--host``), threads the omnigent host
id and auth token through as the env vars the locustfile reads (``HOST_ID`` /
``AUTH_TOKEN``), maps the load knobs to locust's ``-u`` / ``-r`` / ``-t``, runs
locust headless, and writes a result set:

    <out-dir>/
      run_config.json         inputs + resolved locust argv + outcome
      report_stats.csv        locust per-endpoint stats (raw)
      report_failures.csv     locust failure breakdown (raw)
      report_stats_history.csv  per-10s time series (raw)
      report.html             locust's own HTML report
      console.log             full locust stdout/stderr
      summary.md              human-readable latency write-up (this tool)

``--web`` opens Locust's browser UI instead (no result files — the UI owns the
run interactively).

Inputs:

* ``--server``      the Omnigent server base URL to load (required).
* ``--host-id``     the connected Omnigent host the load is scoped to (env
                    ``HOST_ID``); the ``ws_load_test`` sockets are user-scoped,
                    so it is recorded run context there and is the binding input
                    for host-scoped locustfiles.
* ``--users`` / ``--spawn-rate`` / ``--run-time``   the load parameters.
* ``--locustfile``  which scenario to run (default the WS fan-out test).
* ``--auth-token``  bearer token for an authenticated deployment (omit locally).
* ``--out-dir``     where to write results (default
                    ``dev/loadtest/results/<scenario>-<timestamp>``).
"""

from __future__ import annotations

import argparse
import csv
import datetime
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

# Default scenario: the WebSocket fan-out test in this directory.
_DEFAULT_LOCUSTFILE = Path(__file__).with_name("ws_load_test.py")

# Where result directories are created when --out-dir is not given.
_RESULTS_ROOT = Path(__file__).with_name("results")

# Prefix locust's --csv writes; the files it produces are "<prefix>_stats.csv",
# "<prefix>_failures.csv", "<prefix>_stats_history.csv", "<prefix>_exceptions.csv".
_CSV_PREFIX = "report"


def _build_parser() -> argparse.ArgumentParser:
    """Build the runner's argument parser."""
    parser = argparse.ArgumentParser(
        description="Run an Omnigent load test against a server + host, and write a result set.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        required=True,
        help="Omnigent server base URL to load, e.g. http://localhost:8000.",
    )
    parser.add_argument(
        "--host-id",
        default=None,
        help="Connected Omnigent host id the load is scoped to (env HOST_ID).",
    )
    parser.add_argument(
        "--users",
        type=int,
        default=50,
        help="Number of concurrent simulated users (locust -u).",
    )
    parser.add_argument(
        "--spawn-rate",
        type=float,
        default=5.0,
        help="Users started per second (locust -r).",
    )
    parser.add_argument(
        "--run-time",
        default="60s",
        help="How long to run, e.g. 60s / 5m / 1h (locust -t).",
    )
    parser.add_argument(
        "--locustfile",
        default=str(_DEFAULT_LOCUSTFILE),
        help="Locustfile (scenario) to run.",
    )
    parser.add_argument(
        "--auth-token",
        default=None,
        help="Bearer token for an authenticated deployment (env AUTH_TOKEN).",
    )
    parser.add_argument(
        "--session-ids",
        default=None,
        help="Comma-separated session ids to watch (env SESSION_IDS).",
    )
    parser.add_argument(
        "--mount-prefix",
        default="",
        help=(
            "Path the Omnigent app is mounted under on a fronted deployment "
            "(e.g. /omnigent or /api/2.0/omnigent). Empty for a plain server."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory for result files. Default: dev/loadtest/results/<scenario>-<timestamp>.",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Open Locust's web UI instead of a headless run (writes no result files).",
    )
    return parser


def _resolve_out_dir(args: argparse.Namespace) -> Path:
    """Resolve (and create) the result directory for this run.

    :param args: Parsed CLI arguments.
    :returns: The created output directory.
    """
    if args.out_dir:
        out = Path(args.out_dir)
    else:
        scenario = Path(args.locustfile).stem
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        out = _RESULTS_ROOT / f"{scenario}-{stamp}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def _build_locust_argv(args: argparse.Namespace, out_dir: Path) -> list[str]:
    """Assemble the ``locust`` argv from parsed runner arguments.

    :param args: Parsed CLI arguments.
    :param out_dir: Directory the CSV/HTML reports are written into.
    :returns: Full argv beginning with ``[sys.executable, "-m", "locust"]``.
    """
    # Launch locust as a module of the CURRENT interpreter, not a bare
    # ``locust`` console script. A bare name resolves through PATH, which can
    # find a stale/broken locust from another Python (e.g. a ~/.local 3.10
    # install missing gevent's deps) even when run.py itself runs under a venv.
    # ``sys.executable -m locust`` guarantees the same interpreter + site.
    argv = [
        sys.executable,
        "-m",
        "locust",
        "-f",
        args.locustfile,
        "--host",
        args.server,
        "-u",
        str(args.users),
        "-r",
        str(args.spawn_rate),
        "-t",
        args.run_time,
    ]
    if args.web:
        # The interactive UI owns the run; it ignores -t until started and
        # writes no CSV/HTML, so skip the report flags in this mode.
        return argv
    # Headless run with a full result set: CSV (machine-readable), HTML
    # (locust's own report), and headless so no browser is needed.
    argv += [
        "--headless",
        "--csv",
        str(out_dir / _CSV_PREFIX),
        "--html",
        str(out_dir / "report.html"),
    ]
    return argv


def _build_env(args: argparse.Namespace) -> dict[str, str]:
    """Build the child environment, threading inputs the locustfile reads.

    Passed as real environment variables rather than locust ``-e`` flags so they
    reach every worker process identically.

    :param args: Parsed CLI arguments.
    :returns: The environment for the locust subprocess.
    """
    env = dict(os.environ)
    if args.host_id:
        env["HOST_ID"] = args.host_id
    if args.auth_token:
        env["AUTH_TOKEN"] = args.auth_token
    if args.session_ids:
        env["SESSION_IDS"] = args.session_ids
    if args.mount_prefix:
        env["MOUNT_PREFIX"] = args.mount_prefix
    return env


def _write_run_config(
    out_dir: Path,
    args: argparse.Namespace,
    argv: list[str],
    exit_code: int,
) -> None:
    """Record the run's inputs, resolved locust argv, and outcome.

    The auth token is deliberately never written — only whether one was used.

    :param out_dir: Result directory.
    :param args: Parsed CLI arguments.
    :param argv: The locust argv that was executed.
    :param exit_code: Locust's exit code (0 = all requests passed).
    """
    config = {
        "server": args.server,
        "host_id": args.host_id,
        "users": args.users,
        "spawn_rate": args.spawn_rate,
        "run_time": args.run_time,
        "locustfile": args.locustfile,
        "scenario": Path(args.locustfile).stem,
        "session_ids": args.session_ids,
        "mount_prefix": args.mount_prefix,
        "auth": bool(args.auth_token),
        "locust_argv": argv,
        "exit_code": exit_code,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    (out_dir / "run_config.json").write_text(json.dumps(config, indent=2))


def _read_stats(stats_csv: Path) -> list[dict[str, str]]:
    """Read locust's ``_stats.csv`` into a list of row dicts.

    :param stats_csv: Path to ``report_stats.csv``.
    :returns: One dict per row (keyed by CSV header), empty if the file is
        missing (e.g. locust crashed before writing stats).
    """
    if not stats_csv.is_file():
        return []
    with stats_csv.open(newline="") as fh:
        return list(csv.DictReader(fh))


def _fmt_ms(raw: str) -> str:
    """Format a locust millisecond field to one decimal, or ``"-"`` if blank.

    :param raw: Raw CSV cell, e.g. ``"14.508"`` or ``""``.
    :returns: A compact display string, e.g. ``"14.5"``.
    """
    if raw is None or raw == "":
        return "-"
    try:
        return f"{float(raw):.1f}"
    except ValueError:
        return raw


def _write_summary(
    out_dir: Path,
    args: argparse.Namespace,
    rows: list[dict[str, str]],
    exit_code: int,
) -> None:
    """Write the human-readable ``summary.md`` from parsed stats.

    Explains the latency distribution per endpoint and calls out failures, so a
    reader does not need to open the CSV or the HTML report.

    :param out_dir: Result directory.
    :param args: Parsed CLI arguments.
    :param rows: Parsed ``_stats.csv`` rows.
    :param exit_code: Locust's exit code.
    """
    lines: list[str] = []
    lines.append(f"# Load test results — {Path(args.locustfile).stem}")
    lines.append("")
    lines.append(f"- **Server:** `{args.server}`")
    if args.host_id:
        lines.append(f"- **Host:** `{args.host_id}`")
    lines.append(f"- **Load:** {args.users} users, spawn {args.spawn_rate}/s, for {args.run_time}")
    lines.append(f"- **Auth:** {'bearer token' if args.auth_token else 'none (local)'}")
    outcome = "PASS — no request failures" if exit_code == 0 else f"FAIL — locust exit {exit_code}"
    lines.append(f"- **Outcome:** {outcome}")
    lines.append("")

    total_reqs = 0
    total_fails = 0
    for row in rows:
        try:
            total_reqs += int(row.get("Request Count", "0") or "0")
            total_fails += int(row.get("Failure Count", "0") or "0")
        except ValueError:
            pass
    fail_pct = (100.0 * total_fails / total_reqs) if total_reqs else 0.0
    lines.append(f"**{total_reqs} requests, {total_fails} failures ({fail_pct:.2f}%).**")
    lines.append("")

    lines.append("## Latency by request (ms)")
    lines.append("")
    lines.append("| Request | # | Fails | Avg | Med | p95 | p99 | Max | Req/s |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for row in rows:
        lines.append(
            "| {name} | {n} | {fails} | {avg} | {med} | {p95} | {p99} | {mx} | {rps} |".format(
                name=row.get("Name", "?"),
                n=row.get("Request Count", "-"),
                fails=row.get("Failure Count", "-"),
                avg=_fmt_ms(row.get("Average Response Time", "")),
                med=_fmt_ms(row.get("Median Response Time", "")),
                p95=_fmt_ms(row.get("95%", "")),
                p99=_fmt_ms(row.get("99%", "")),
                mx=_fmt_ms(row.get("Max Response Time", "")),
                rps=_fmt_ms(row.get("Requests/s", "")),
            )
        )
    lines.append("")

    # Failure breakdown, if any rows landed in the failures CSV.
    failures_csv = out_dir / f"{_CSV_PREFIX}_failures.csv"
    fail_rows = _read_stats(failures_csv)
    real_fails = [r for r in fail_rows if (r.get("Occurrences") or r.get("Occurences"))]
    if real_fails:
        lines.append("## Failures")
        lines.append("")
        lines.append("| Request | Error | Count |")
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
        "- **Avg / Med** — typical latency; a median far below the average means a "
        "few slow outliers are pulling the mean up."
    )
    lines.append(
        "- **p95 / p99** — tail latency: 95% / 99% of requests were at or below this. "
        "Watch these, not the average — they are what a loaded client actually feels."
    )
    lines.append(
        "- **Max** — the single worst request; often a cold connection or a GC / "
        "scheduling hiccup."
    )
    lines.append("- **Req/s** — throughput for that request type at this concurrency.")
    lines.append(
        "- **Fails** — any non-zero value means requests errored; see the Failures "
        "section and `console.log`. Exit code 0 means locust saw zero failures."
    )
    lines.append("")
    lines.append(
        "Raw data: `report_stats.csv`, `report_stats_history.csv` "
        "(per-10s time series), `report.html`."
    )
    lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines))


def main() -> int:
    """Parse inputs, run locust, and write the result set."""
    args = _build_parser().parse_args()

    if importlib.util.find_spec("locust") is None:
        sys.exit(
            f"locust is not importable under {sys.executable} — install the extra: "
            "pip install -e '.[loadtest]' (or: uv sync --extra loadtest), and run "
            "run.py with that same interpreter."
        )
    if not Path(args.locustfile).is_file():
        sys.exit(f"locustfile not found: {args.locustfile}")

    env = _build_env(args)

    if args.web:
        # Interactive UI: hand off to locust directly, no result files.
        argv = _build_locust_argv(args, Path("."))
        print(f"$ {' '.join(argv)}", flush=True)
        os.execvpe(sys.executable, argv, env)

    out_dir = _resolve_out_dir(args)
    argv = _build_locust_argv(args, out_dir)
    print(f"$ {' '.join(argv)}", flush=True)
    if args.host_id:
        print(f"  HOST_ID={args.host_id}", flush=True)
    print(f"  results → {out_dir}", flush=True)

    # Stream locust output to the console AND capture it to console.log via tee,
    # so a scripted run keeps a full log while the operator still sees progress.
    console_log = out_dir / "console.log"
    with console_log.open("w") as log_fh:
        proc = subprocess.Popen(
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            sys.stdout.write(line)
            log_fh.write(line)
        exit_code = proc.wait()

    _write_run_config(out_dir, args, argv, exit_code)
    rows = _read_stats(out_dir / f"{_CSV_PREFIX}_stats.csv")
    _write_summary(out_dir, args, rows, exit_code)

    print(f"\nResults written to {out_dir}", flush=True)
    print(f"  summary:  {out_dir / 'summary.md'}", flush=True)
    print(f"  html:     {out_dir / 'report.html'}", flush=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
