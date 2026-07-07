"""CLI: render the harness capability matrix.

Examples::

    # List official harnesses.
    python -m tests.harness_bench --list

    # Dry (offline) render — declared matrix, no turns, no creds.
    python -m tests.harness_bench

    # Live probe one harness against a gateway profile (SDK → full-server,
    # the default: covers Tool calling + Policy DENY).
    python -m tests.harness_bench --harness codex --profile my-profile

    # Quicker run: SDK harnesses on sdk-inproc (skips the server boot; no
    # Tool calling / Policy DENY coverage).
    python -m tests.harness_bench --harness codex --profile my-profile --fast

    # Live probe all official harnesses, JSON out.
    python -m tests.harness_bench --profile my-profile --json

    # A community harness that ships its own BenchProfile.
    python -m tests.harness_bench --harness mypkg.harness:PROFILE --profile my-profile
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from tests.harness_bench.bench import run_bench
from tests.harness_bench.events import LineSink
from tests.harness_bench.manifest import OFFICIAL_PROFILES
from tests.harness_bench.profile import BenchProfile, resolve_profile
from tests.harness_bench.report import render_json, render_markdown, render_table
from tests.harness_bench.transport import driver_registry, resolve_transport_name


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tests.harness_bench",
        description="Probe a harness and report a verdict per capability dimension.",
    )
    parser.add_argument(
        "--harness",
        action="append",
        metavar="NAME",
        help="Harness to probe (repeatable). An official name, or a "
        "'module:attr' / 'module.ATTR' reference to a community "
        "BenchProfile. Defaults to every official harness.",
    )
    parser.add_argument(
        "--profile",
        metavar="NAME",
        default=None,
        help="Databricks gateway profile. Enables the live layer; without "
        "it the bench renders the declared matrix offline.",
    )
    parser.add_argument(
        "--live",
        dest="live",
        action="store_true",
        default=None,
        help="Force the live layer (requires --profile).",
    )
    parser.add_argument(
        "--no-live",
        dest="live",
        action="store_false",
        help="Force the offline (declared-only) render.",
    )
    transport_grp = parser.add_mutually_exclusive_group()
    transport_grp.add_argument(
        "--transport",
        metavar="NAME",
        default=None,
        help="Transport driver override (e.g. 'sdk-inproc', 'full-server'). "
        "Wins over the family default. By default SDK harnesses run on "
        "full-server (fullest coverage: Tool calling + Policy DENY); natives "
        "run on native-tui.",
    )
    transport_grp.add_argument(
        "--fast",
        action="store_true",
        help="Run SDK harnesses on sdk-inproc instead of the full-server "
        "default: skips the server boot for a quicker run, at the cost of the "
        "Tool calling + Policy DENY dimensions (reported SKIPPED). No effect on "
        "native harnesses. Mutually exclusive with --transport.",
    )
    fmt = parser.add_mutually_exclusive_group()
    fmt.add_argument(
        "--markdown",
        action="store_true",
        help="Emit the GitHub-flavored Markdown table (for docs / PRs).",
    )
    fmt.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.add_argument(
        "--no-color", action="store_true", help="Disable ANSI color in the terminal table."
    )
    parser.add_argument(
        "--jobs",
        "-j",
        type=int,
        default=1,
        metavar="N",
        help="Run up to N harnesses concurrently (default 1 = sequential). "
        "Probes within a harness stay sequential. Higher N cuts wall-clock but "
        "raises process / gateway load; 3-4 is a reasonable ceiling on one host.",
    )
    rich_grp = parser.add_mutually_exclusive_group()
    rich_grp.add_argument(
        "--rich",
        dest="rich",
        action="store_true",
        default=None,
        help="Force the live rich progress table (needs a TTY + rich).",
    )
    rich_grp.add_argument(
        "--no-rich",
        dest="rich",
        action="store_false",
        help="Force plain per-line progress (no live table).",
    )
    parser.add_argument(
        "--report",
        metavar="PATH",
        default=None,
        help="Also write the final matrix to PATH. Format follows --json / "
        "--markdown, else inferred from the extension (.json / .md), else plain text.",
    )
    parser.add_argument("--list", action="store_true", help="List official harnesses and exit.")
    return parser.parse_args(argv)


def _resolve_profiles(names: list[str] | None) -> list[BenchProfile]:
    if not names:
        return list(OFFICIAL_PROFILES.values())
    return [resolve_profile(name) for name in names]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.list:
        # Show the transport a default run would pick (SDK family → full-server),
        # not the raw family marker, so --list matches what actually runs.
        for name, profile in sorted(OFFICIAL_PROFILES.items()):
            transport = resolve_transport_name(profile, override=None, fast=False)
            print(f"{name}\t{transport}\t{profile.model}")
        return 0

    try:
        profiles = _resolve_profiles(args.harness)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # Validate the transport override up front so a typo is a clean CLI error
    # (exit 2) rather than a KeyError traceback out of the async run.
    if args.transport is not None and args.transport not in driver_registry():
        known = ", ".join(sorted(driver_registry()))
        print(
            f"unknown --transport {args.transport!r}; known transports: {known}", file=sys.stderr
        )
        return 2

    if args.jobs < 1:
        print("--jobs must be >= 1", file=sys.stderr)
        return 2

    # Live if explicitly forced, or implied by a supplied profile.
    live = args.live if args.live is not None else bool(args.profile)
    if live and not args.profile:
        print("--live requires --profile <name>", file=sys.stderr)
        return 2

    # Progress sink: only for a live run (offline is instant). Prefer the rich
    # live table when a TTY + rich are available (or --rich forces it), else
    # fall back to plain per-line output on stderr (the report goes to stdout).
    sink = None
    if live:
        sink = _select_progress_sink(args.rich)

    matrix = asyncio.run(
        run_bench(
            profiles,
            databricks_profile=args.profile,
            live=live,
            transport=args.transport,
            fast=args.fast,
            progress=sink,
            jobs=args.jobs,
        )
    )
    if sink is not None:
        sink.close()

    # Offline (not live) has nothing observed, so show the declared matrix.
    declared = not live
    if args.json:
        output = render_json(matrix)
    elif args.markdown:
        output = render_markdown(matrix, declared=declared)
    else:
        # Default: terminal table. Color only when stdout is a real TTY and
        # not suppressed, so piping to a file / pager stays plain.
        color = sys.stdout.isatty() and not args.no_color
        # If the rich live table already painted the grid to this same terminal,
        # drop the grid from the stdout report (keep the legend + notes) so the
        # matrix is not printed twice. When stdout is redirected, print it in
        # full -- the file needs the grid the on-screen table did not capture.
        grid = not (_grid_already_shown(sink) and sys.stdout.isatty())
        output = render_table(matrix, color=color, declared=declared, grid=grid)
    print(output, end="")

    if args.report:
        _write_report(args.report, matrix, json_flag=args.json, markdown_flag=args.markdown)

    # A drift is a non-zero exit so CI / scripts notice without parsing output.
    return 1 if matrix.has_drift else 0


def _grid_already_shown(sink) -> bool:
    """Whether the progress sink already painted the glyph grid to the terminal.

    True only for the rich live table (which sets ``drew_grid = True``); the
    plain :class:`LineSink` and a silent run do not, so their report prints the
    grid in full.
    """
    return bool(getattr(sink, "drew_grid", False))


def _select_progress_sink(rich_flag: bool | None):
    """Pick the progress sink for a live run.

    ``rich_flag``: ``True`` forces rich, ``False`` forces plain, ``None`` =
    auto (rich on a TTY, plain otherwise). Falls back to the plain
    :class:`LineSink` whenever rich is unavailable or not a terminal.
    """

    def _line(msg: str) -> None:
        print(msg, file=sys.stderr, flush=True)

    if rich_flag is not False:
        # richreport is imported lazily: it is the only place that touches the
        # optional `rich` dependency, so a plain/no-rich run never imports it.
        from tests.harness_bench.richreport import rich_sink_or_none

        rich_sink = rich_sink_or_none(force=bool(rich_flag))
        if rich_sink is not None:
            return rich_sink
        if rich_flag is True:
            print("--rich requested but rich/TTY unavailable; using plain output", file=sys.stderr)
    return LineSink(_line)


def _write_report(path: str, matrix, *, json_flag: bool, markdown_flag: bool) -> None:
    """Write the matrix to *path*; format from flags, else the extension."""
    if json_flag:
        content = render_json(matrix)
    elif markdown_flag:
        content = render_markdown(matrix, declared=False)
    elif path.endswith(".json"):
        content = render_json(matrix)
    elif path.endswith((".md", ".markdown")):
        content = render_markdown(matrix, declared=False)
    else:
        # Plain, un-colored grid — a file should never carry ANSI codes.
        content = render_table(matrix, color=False, declared=False)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content if content.endswith("\n") else content + "\n")
    print(f"report written to {path}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
