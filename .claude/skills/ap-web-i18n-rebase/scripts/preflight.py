#!/usr/bin/env python3
"""
Preflight for an ap-web i18n rebase: confirm the strategy is safe and size
the real work before touching anything.

The whole "take main, re-wire on top" plan rests on one assumption — that the
locale layer is a pure addition on the translation branch, so it can't
conflict. This script verifies that and shows the actual conflict set (the
files main ALSO changed), which is the only part you'll hand-resolve.

Run from anywhere in the repo (paths auto-resolve). Read-only: it inspects
git, it does not modify the working tree or start a rebase.

Usage:
    python preflight.py [--onto main] [--branch <i18n-branch>]

--onto defaults to `main` (falls back to `origin/main` if no local `main`).
--branch defaults to the current branch.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=False
    ).stdout.strip()


def repo_root() -> Path | None:
    top = git("rev-parse", "--show-toplevel")
    return Path(top) if top else None


def resolve_onto(onto: str) -> str | None:
    """Prefer the given ref; fall back to origin/<ref> if the local one is absent."""
    if git("rev-parse", "--verify", "--quiet", onto):
        return onto
    alt = f"origin/{onto}"
    if git("rev-parse", "--verify", "--quiet", alt):
        return alt
    return None


def files_under(paths: list[str], prefix: str) -> list[str]:
    return sorted({p for p in paths if p.startswith(prefix) and p})


def main() -> int:
    ap = argparse.ArgumentParser(description="Preflight an ap-web i18n rebase.")
    ap.add_argument("--onto", default="main", help="Branch to rebase onto (default: main)")
    ap.add_argument("--branch", default=None, help="The i18n branch (default: current)")
    args = ap.parse_args()

    if repo_root() is None:
        print("Not inside a git repository.", file=sys.stderr)
        return 2

    onto = resolve_onto(args.onto)
    if onto is None:
        print(
            f"Could not find '{args.onto}' or 'origin/{args.onto}'. Fetch first?",
            file=sys.stderr,
        )
        return 2
    branch = args.branch or git("rev-parse", "--abbrev-ref", "HEAD")

    print(f"Branch being rebased : {branch}")
    print(f"Onto                 : {onto}")

    # Working tree cleanliness — a dirty tree makes a rebase a bad idea.
    if git("status", "--porcelain"):
        print("\n[!] Working tree is NOT clean. Commit or stash before rebasing.")

    merge_base = git("merge-base", "HEAD", onto)
    if not merge_base:
        print("\n[!] No merge base with onto target — unexpected; stop and investigate.")
        return 2

    ahead = git("rev-list", "--count", f"{onto}..HEAD")
    behind = git("rev-list", "--count", f"HEAD..{onto}")
    print(f"Commits to replay    : {ahead} (ahead)")
    print(f"Commits to catch up  : {behind} (behind)")

    commits = git("log", "--oneline", f"{onto}..HEAD")
    print(f"\nCommit(s) being replayed onto {onto}:\n{commits or '  (none)'}")

    # The load-bearing assumption: does `onto` already have i18n of its own?
    onto_locales = git("ls-tree", "-r", "--name-only", onto, "--", "ap-web/src/i18n/locales")
    if onto_locales:
        print(
            f"\n[!] WARNING: '{onto}' ALREADY has ap-web/src/i18n/locales:\n"
            f"{onto_locales}\n"
            "    The locale files WILL conflict — the 'pure addition' assumption\n"
            "    is false. Stop and reconsider before resolving locale files."
        )
    else:
        print(f"\nOK: '{onto}' has no ap-web i18n locales — locale JSON will not conflict.")

    # The real conflict set: files the branch translated AND `onto` also changed.
    branch_files = files_under(
        git("show", "--pretty=", "--name-only", "HEAD", "--", "ap-web/src").splitlines(),
        "ap-web/src",
    )
    # If multiple commits are being replayed, union their touched files.
    if int(ahead or "0") > 1:
        branch_files = files_under(
            git("diff", "--name-only", f"{onto}...HEAD", "--", "ap-web/src").splitlines(),
            "ap-web/src",
        )
    onto_changed = files_under(
        git("diff", "--name-only", f"{merge_base}", onto, "--", "ap-web/src").splitlines(),
        "ap-web/src",
    )
    overlap = sorted(set(branch_files) & set(onto_changed))

    print(f"\nFiles the branch touched under ap-web/src : {len(branch_files)}")
    print(f"Files '{onto}' changed since the base      : {len(onto_changed)}")
    print(f"\nLIKELY CONFLICT SET ({len(overlap)} file(s)) — your Phase-2 worklist:")
    for f in overlap:
        print(f"    {f}")
    if not overlap:
        print("    (none — the rebase may apply with no conflicts at all)")

    sha = git("rev-parse", "HEAD")
    print(f"\nTip: inspect the branch's wiring of a file with:\n  git show {sha} -- <path>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
