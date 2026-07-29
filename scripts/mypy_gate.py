#!/usr/bin/env python3
"""Fail CI on *new* mypy errors; known errors live in ``mypy-baseline.txt``.

Full-tree ``mypy omnigent`` currently reports thousands of pre-existing
errors, so we cannot gate the whole package cleanly yet. This script runs
mypy with the project config, fingerprints each error (path + code +
message, line-agnostic), and exits non-zero only when the run produces
signatures that are not covered by the committed baseline multiset.

When baseline signatures are no longer produced by the current run
(errors that were fixed but not pruned), print a warning with a count and
sample so the debt visibly shrinks instead of quietly re-admitting fixed
bugs later. Rewriting is intentional::

    uv run python scripts/mypy_gate.py --write-baseline

See the header of ``mypy-baseline.txt`` for the fingerprint format.
"""

from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = REPO_ROOT / "mypy-baseline.txt"
DEFAULT_TARGET = "omnigent"
# How many resolved (stale baseline) fingerprints to print as a sample.
_STALE_SAMPLE = 10

# path:line: error: message [code]
_ERROR_RE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): error: (?P<msg>.+?)(?:  \[(?P<code>[^\]]+)\])?$"
)

# mypy uses 0 = clean, 1 = type errors found. Anything else is an invocation /
# crash / blocking failure and must never look like a green gate.
_MYPY_OK_EXIT = frozenset({0, 1})


def _fingerprint(path: str, code: str, msg: str) -> str:
    return f"{path}\t{code}\t{msg}"


def parse_mypy_output(text: str) -> list[str]:
    """Return one fingerprint per mypy error line (order preserved)."""
    out: list[str] = []
    for line in text.splitlines():
        m = _ERROR_RE.match(line)
        if not m:
            continue
        out.append(
            _fingerprint(
                m.group("path"),
                m.group("code") or "unknown",
                m.group("msg"),
            )
        )
    return out


def load_baseline(path: Path) -> list[str]:
    if not path.is_file():
        return []
    sigs: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        sigs.append(line)
    return sigs


def write_baseline(path: Path, fingerprints: list[str]) -> None:
    header = (
        "# mypy error baseline (path<TAB>code<TAB>message). Line numbers are\n"
        "# omitted so edits that only shift lines do not create false 'new'\n"
        "# errors. Maintained by scripts/mypy_gate.py --write-baseline.\n"
        "# Failures: signatures present in a mypy run but not in this multiset.\n"
        "# Stale entries (in baseline but not in the current run) warn; prune\n"
        "# them with --write-baseline so fixed debt cannot quietly re-enter.\n"
    )
    body = "\n".join(fingerprints)
    path.write_text(header + body + ("\n" if body else ""), encoding="utf-8")


def run_mypy(target: str) -> tuple[int, list[str], str]:
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "mypy", target],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        # Interpreter cannot even spawn mypy — treat as hard failure.
        return 127, [], f"failed to invoke mypy: {exc}"
    text = proc.stdout + proc.stderr
    return proc.returncode, parse_mypy_output(text), text


def new_errors(current: list[str], baseline: list[str]) -> list[str]:
    """Multiset difference: fingerprints in current not covered by baseline."""
    left = collections.Counter(current)
    left.subtract(collections.Counter(baseline))
    extras: list[str] = []
    for sig, count in sorted(left.items()):
        if count > 0:
            extras.extend([sig] * count)
    return extras


def format_stale_warning(stale: list[str], *, sample: int = _STALE_SAMPLE) -> str:
    """Human-readable warning for baseline entries the current run no longer emits."""
    lines = [
        f"::warning::{len(stale)} baseline fingerprint(s) were not produced by "
        "this mypy run (fixed since the baseline was written, or otherwise gone). "
        "Run `uv run python scripts/mypy_gate.py --write-baseline` to prune them "
        "so those errors cannot silently re-enter later.",
        f"Stale sample ({min(sample, len(stale))} of {len(stale)}):",
    ]
    for sig in stale[:sample]:
        path, code, msg = sig.split("\t", 2)
        lines.append(f"  {path}: [{code}] {msg}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--baseline",
        type=Path,
        default=DEFAULT_BASELINE,
        help=f"baseline file (default: {DEFAULT_BASELINE})",
    )
    parser.add_argument(
        "--target",
        default=DEFAULT_TARGET,
        help=f"mypy target (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="rewrite the baseline from the current mypy run and exit 0",
    )
    args = parser.parse_args(argv)

    rc, fingerprints, raw = run_mypy(args.target)

    # Invocation / crash / blocking failure: never green, never rewrite baseline
    # from a partial/empty run.
    if rc not in _MYPY_OK_EXIT:
        print(
            f"::error::mypy exited {rc} (expected 0=clean or 1=type errors). "
            "The gate refuses to pass when mypy did not run successfully.",
            file=sys.stderr,
        )
        if raw.strip():
            print(raw, file=sys.stderr)
        return rc if rc != 0 else 1

    if args.write_baseline:
        write_baseline(args.baseline, fingerprints)
        print(
            f"Wrote {len(fingerprints)} error fingerprint(s) to {args.baseline} (mypy exit {rc})."
        )
        return 0

    baseline = load_baseline(args.baseline)
    if not baseline and fingerprints:
        print(
            f"::error::No baseline at {args.baseline} but mypy reported "
            f"{len(fingerprints)} error(s). Run with --write-baseline first.",
            file=sys.stderr,
        )
        return 1

    extras = new_errors(fingerprints, baseline)
    stale = new_errors(baseline, fingerprints)

    print(
        f"mypy: {len(fingerprints)} error(s); "
        f"baseline: {len(baseline)}; "
        f"new: {len(extras)}; "
        f"resolved-since-baseline: {len(stale)}"
    )
    if stale:
        print(format_stale_warning(stale), file=sys.stderr)

    if extras:
        print("::error::New mypy errors not in mypy-baseline.txt:", file=sys.stderr)
        for sig in extras:
            path, code, msg = sig.split("\t", 2)
            print(f"  {path}: [{code}] {msg}", file=sys.stderr)
        print(
            "Fix the new errors, or if they are intentional debt run "
            "`uv run python scripts/mypy_gate.py --write-baseline` "
            "(review the diff).",
            file=sys.stderr,
        )
        return 1

    print("No new mypy errors vs baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
