#!/usr/bin/env python3
"""
Mechanically resolve the *unambiguous* conflicts of an in-progress ap-web
i18n rebase, so the human/agent is left only with the translation re-wiring —
and only over the hunks that actually conflicted, not the whole file.

While a `git rebase main` is paused, this applies the strategy:
  * source / test files (.ts, .tsx, ...)  -> take MAIN's side **per conflict
                                              hunk**, leaving every already-
                                              merged (non-conflicting) region —
                                              and its surviving t() wiring —
                                              untouched.
  * ap-web/src/i18n/locales/** JSON files  -> take the BRANCH's version
                                              (--theirs); defensive, they
                                              rarely conflict since main has
                                              no i18n.
then `git add`s each resolved path and prints what it did, including the line
ranges of each hunk it took from main so you re-wire only those spots.

Why per-hunk and not `git checkout --ours`? A whole-file checkout of main
throws away the branch's t() wiring across the *entire* file — including the
regions that merged cleanly — forcing a full re-translation of the file. Git
already kept the branch's wiring in every non-conflicting region; only the
conflict hunks lost it. Resolving hunk-by-hunk preserves that free work and
shrinks the re-wire to just the conflicted spots. On a file where main touched
two functions out of thirty, that is the difference between re-reading 30
functions and re-reading 2.

It deliberately does NOT run `git rebase --continue`: after this, you still
have to re-apply the t() wiring onto main's side of each resolved hunk (the
keys survive in the clean locale JSON), and only then continue the rebase.

Rebase direction reminder: during a rebase, `--ours`/`<<<<<<<` is the branch
you are landing ON (main), `--theirs`/`>>>>>>>` is the commit being replayed
(the translations). This is the opposite of a merge.

Run from anywhere in the repo. Use --dry-run to preview without touching git.
Use --whole-file to fall back to the old `git checkout --ours` behavior for a
file that is too tangled to resolve hunk-by-hunk.

Usage:
    python resolve_conflicts.py [--dry-run] [--whole-file PATH ...]
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

LOCALE_PREFIX = "ap-web/src/i18n/locales/"
SOURCE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mts", ".cts")

CONFLICT_START = "<<<<<<<"
CONFLICT_BASE = "|||||||"  # only present in diff3 / zdiff3 conflict style
CONFLICT_SEP = "======="
CONFLICT_END = ">>>>>>>"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def conflicted_paths() -> list[str]:
    out = git("diff", "--name-only", "--diff-filter=U").stdout
    return [p for p in out.splitlines() if p.strip()]


def repo_root() -> Path | None:
    top = git("rev-parse", "--show-toplevel").stdout.strip()
    return Path(top) if top else None


def in_rebase() -> bool:
    top = git("rev-parse", "--git-dir").stdout.strip()
    if not top:
        return False
    gd = Path(top)
    return (gd / "rebase-merge").exists() or (gd / "rebase-apply").exists()


def take_ours_per_hunk(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Resolve every conflict block in `text` by keeping the `--ours` (main) side.

    Handles both 2-way (`<<< === >>>`) and diff3 (`<<< ||| === >>>`) markers.
    Returns (resolved_text, ranges) where `ranges` is a list of 1-based
    (start_line, end_line) spans in the *resolved* output covering the lines
    that came from main's side of each conflict — i.e. exactly the spots to
    re-wire. An empty `ranges` means the file carried no conflict markers.
    """
    # Preserve the file's original newline shape; splitlines(keepends) keeps it.
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    ranges: list[tuple[int, int]] = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        if not line.startswith(CONFLICT_START):
            out.append(line)
            i += 1
            continue

        # Enter a conflict block. Collect ours lines up to base/sep.
        i += 1
        ours: list[str] = []
        while i < n and not lines[i].startswith(
            (CONFLICT_BASE, CONFLICT_SEP, CONFLICT_END)
        ):
            ours.append(lines[i])
            i += 1
        # Skip the base section (diff3 only), up to the separator.
        if i < n and lines[i].startswith(CONFLICT_BASE):
            i += 1
            while i < n and not lines[i].startswith((CONFLICT_SEP, CONFLICT_END)):
                i += 1
        # Skip theirs section, up to and including the end marker.
        if i < n and lines[i].startswith(CONFLICT_SEP):
            i += 1
            while i < n and not lines[i].startswith(CONFLICT_END):
                i += 1
        if i < n and lines[i].startswith(CONFLICT_END):
            i += 1  # consume ">>>>>>>"

        # Emit main's side; record where it landed in the output (1-based).
        start = len(out) + 1
        out.extend(ours)
        end = len(out)  # inclusive; if ours was empty, end < start (zero-width)
        ranges.append((start, end))

    return "".join(out), ranges


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-resolve i18n rebase conflicts.")
    ap.add_argument("--dry-run", action="store_true", help="Show decisions; change nothing.")
    ap.add_argument(
        "--whole-file",
        action="append",
        default=[],
        metavar="PATH",
        help="Take main's WHOLE file (old `checkout --ours`) for this path instead "
        "of resolving per hunk. Repeatable; for files too tangled to re-wire by hunk.",
    )
    args = ap.parse_args()

    root = repo_root()
    if root is None:
        print("Not inside a git repository.", file=sys.stderr)
        return 2

    if not in_rebase():
        print(
            "No rebase appears to be in progress. Start one first:\n"
            "    git rebase main\n"
            "then re-run this script when it pauses on conflicts.",
            file=sys.stderr,
        )
        return 2

    paths = conflicted_paths()
    if not paths:
        print("No conflicted (UU) paths. Nothing to resolve.")
        return 0

    whole_file = set(args.whole_file)
    # Reported as (path, ranges) so the agent knows exactly where to re-wire.
    took_main_hunks: list[tuple[str, list[tuple[int, int]]]] = []
    took_main_whole: list[str] = []
    took_branch: list[str] = []
    skipped: list[str] = []

    for path in paths:
        if path.startswith(LOCALE_PREFIX):
            # Keep the branch's translations for locale JSON.
            if args.dry_run:
                print(f"would take branch (--theirs)  for {path}")
                took_branch.append(path)
                continue
            co = git("checkout", "--theirs", "--", path)
            if co.returncode != 0:
                print(f"[!] checkout --theirs failed for {path}: {co.stderr.strip()}")
                skipped.append(path)
                continue
            git("add", "--", path)
            took_branch.append(path)
            continue

        if not path.endswith(SOURCE_SUFFIXES):
            skipped.append(path)
            continue

        # Source / test file: take main's side. Whole-file on request, else per hunk.
        if path in whole_file:
            if args.dry_run:
                print(f"would take main WHOLE file     for {path}")
                took_main_whole.append(path)
                continue
            co = git("checkout", "--ours", "--", path)
            if co.returncode != 0:
                print(f"[!] checkout --ours failed for {path}: {co.stderr.strip()}")
                skipped.append(path)
                continue
            git("add", "--", path)
            took_main_whole.append(path)
            continue

        abs_path = root / path
        try:
            original = abs_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"[!] could not read {path} ({exc}); resolve by hand.")
            skipped.append(path)
            continue

        resolved, ranges = take_ours_per_hunk(original)
        if not ranges:
            # No markers found — git may have resolved it, or it's binary-ish.
            # Fall back to the whole-file ours checkout to be safe.
            if args.dry_run:
                print(f"would take main WHOLE file     for {path} (no markers found)")
                took_main_whole.append(path)
                continue
            co = git("checkout", "--ours", "--", path)
            if co.returncode == 0:
                git("add", "--", path)
                took_main_whole.append(path)
            else:
                skipped.append(path)
            continue

        if args.dry_run:
            print(f"would take main per-hunk ({len(ranges)})  for {path}")
            took_main_hunks.append((path, ranges))
            continue

        abs_path.write_text(resolved, encoding="utf-8")
        git("add", "--", path)
        took_main_hunks.append((path, ranges))

    if took_main_hunks:
        print(
            f"\nTook MAIN per-hunk (re-wire t() only inside these ranges): "
            f"{len(took_main_hunks)} file(s)"
        )
        for p, ranges in took_main_hunks:
            spans = ", ".join(
                f"L{a}" if a == b else f"L{a}-{b}" for a, b in ranges
            )
            print(f"    {p}  [{len(ranges)} hunk(s): {spans}]")
    if took_main_whole:
        print(f"\nTook MAIN whole file (re-wire entire file): {len(took_main_whole)} file(s)")
        for p in took_main_whole:
            print(f"    {p}")
    if took_branch:
        print(f"\nKept BRANCH (locale JSON): {len(took_branch)} file(s)")
        for p in took_branch:
            print(f"    {p}")
    if skipped:
        print(f"\nSKIPPED — resolve by hand (not an i18n-classifiable file): {len(skipped)}")
        for p in skipped:
            print(f"    {p}")

    if not args.dry_run and (took_main_hunks or took_main_whole):
        print(
            "\nNext: re-apply the t() wiring onto main's side of each resolved spot.\n"
            "  - per-hunk files: only the line ranges above lost their wiring; the\n"
            "    rest of the file kept the branch's t() calls from the clean merge.\n"
            "  - whole-file files: re-wire the entire file.\n"
            "`git show <i18n-sha> -- <path>` shows how the branch wired it.\n"
            "`git add` each file, then run `git rebase --continue`.\n"
            "Do NOT continue until every resolved spot is re-wired and staged."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
