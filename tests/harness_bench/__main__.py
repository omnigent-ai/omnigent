"""CLI: render the harness capability matrix.

Examples::

    # List official harnesses.
    python -m tests.harness_bench --list

    # Dry (offline) render — declared matrix, no turns, no creds.
    python -m tests.harness_bench

    # Live probe one harness against a gateway profile.
    python -m tests.harness_bench --harness codex --profile my-profile

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
from tests.harness_bench.manifest import OFFICIAL_PROFILES
from tests.harness_bench.profile import BenchProfile, resolve_profile
from tests.harness_bench.report import render_json, render_markdown, render_table
from tests.harness_bench.transport import driver_registry


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
    parser.add_argument(
        "--transport",
        metavar="NAME",
        default=None,
        help="Transport driver override (e.g. 'sdk-inproc', 'full-server'). "
        "Wins over each profile's declared transport. Defaults to the "
        "profile's transport.",
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
    parser.add_argument("--list", action="store_true", help="List official harnesses and exit.")
    return parser.parse_args(argv)


def _resolve_profiles(names: list[str] | None) -> list[BenchProfile]:
    if not names:
        return list(OFFICIAL_PROFILES.values())
    return [resolve_profile(name) for name in names]


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])

    if args.list:
        for name, profile in sorted(OFFICIAL_PROFILES.items()):
            print(f"{name}\t{profile.transport}\t{profile.model}")
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

    # Live if explicitly forced, or implied by a supplied profile.
    live = args.live if args.live is not None else bool(args.profile)
    if live and not args.profile:
        print("--live requires --profile <name>", file=sys.stderr)
        return 2

    # Live runs make network calls that can take tens of seconds per turn.
    # Stream progress to stderr (report goes to stdout) so the run is not
    # silent; offline is fast enough to stay quiet.
    def _progress(line: str) -> None:
        print(line, file=sys.stderr, flush=True)

    matrix = asyncio.run(
        run_bench(
            profiles,
            databricks_profile=args.profile,
            live=live,
            transport=args.transport,
            progress=_progress if live else None,
        )
    )
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
        output = render_table(matrix, color=color, declared=declared)
    print(output, end="")
    # A drift is a non-zero exit so CI / scripts notice without parsing output.
    return 1 if matrix.has_drift else 0


if __name__ == "__main__":
    raise SystemExit(main())
