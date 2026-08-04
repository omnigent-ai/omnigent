#!/usr/bin/env python3
"""Drive the repro-agent (dev/repro-agent) against a bug, in an isolated worktree.

Maintainer-only convenience wrapper — it lives under ``dev/`` (not shipped in the
wheel) because it depends on a source checkout: the repro-agent authors its test
into ``tests/e2e_ui/`` / ``tests/e2e/``, which only exist here.

What it does:
  1. Prompts for the bug URL (or takes it as an argument).
  2. Creates an isolated git worktree off this repo (branch ``repro/<slug>``,
     a sibling ``<repo>-worktrees/repro-<slug>`` dir), so the authored test lands
     on its own branch with a clean diff and never dirties your checkout.
  3. Runs ``omnigent run dev/repro-agent`` FROM that worktree (so the worktree is
     the agent's workspace), passing the bug as the ``-p`` input contract and
     forwarding ``--server`` when you give one.
  4. Prints the worktree path + branch at the end so you (or the fix step) can
     pick up the diff. The worktree is always kept — remove it yourself with
     ``git worktree remove`` when done.

Usage (from the repo root):
  python dev/repro.py                     # prompts for the bug URL
  python dev/repro.py https://github.com/omnigent-ai/omnigent/issues/1234
  python dev/repro.py OMNI-1234 --server http://localhost:6767
  python dev/repro.py <bug_url> --public  # share the session public-read at start

Reproducing runs against whatever server ``omnigent run`` uses (the local server
it spins up by default, or the one you pass with ``--server``).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

# dev/repro.py → repo root is the parent of dev/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_REL = "dev/repro-agent"


def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _slug_from_bug_url(bug_url: str) -> str:
    """Derive a branch-safe slug from a bug URL or bare id.

    GitHub issue → the issue number (``3987``); Linear ticket → the lowercased
    key (``omni-1234``); a bare ``OMNI-1234`` / ``#3987`` argument is accepted
    too. Anything else falls back to ``bug`` (the caller adds a unique suffix).
    """
    m = re.search(r"/issues/(\d+)", bug_url)
    if m:
        return m.group(1)
    m = re.search(r"/issue/([A-Za-z]+-\d+)", bug_url)
    if m:
        return m.group(1).lower()
    # Bare forms: "OMNI-1234", "#3987", "3987".
    m = re.fullmatch(r"#?(\d+)", bug_url.strip())
    if m:
        return m.group(1)
    m = re.fullmatch(r"([A-Za-z]+-\d+)", bug_url.strip())
    if m:
        return m.group(1).lower()
    return "bug"


def _unique_branch(slug: str) -> str:
    """Return ``repro/<slug>`` (or ``repro/<slug>-2``, ``-3``, …) not yet used.

    Checks existing local branches so a re-run of the same bug never collides —
    it just lands on the next free suffix.
    """
    existing = set(
        subprocess.run(
            ["git", "-C", str(_REPO_ROOT), "branch", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
    )
    base = f"repro/{slug}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev/repro.py",
        description="Run dev/repro-agent against a bug in an isolated worktree.",
    )
    p.add_argument(
        "bug_url",
        nargs="?",
        help="Bug report link (GitHub issue or Linear ticket URL), or a bare "
        "id like OMNI-1234 / 3987. Prompted for if omitted.",
    )
    p.add_argument(
        "--server",
        default=None,
        help="Omnigent server URL to reproduce against. Omit to use the local "
        "server omnigent run spins up.",
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="Share the reproduction session public-read (anyone who can reach "
        "the server) right after it starts. Off by default — useful when "
        "watching a live run or reproducing against a shared --server.",
    )
    return p.parse_args()


def main() -> None:
    args = _parse_args()

    # Source-checkout guard: dev/repro-agent must exist next to this script.
    agent_dir = _REPO_ROOT / _AGENT_REL
    if not (agent_dir / "config.yaml").is_file():
        _die(
            f"{_AGENT_REL}/config.yaml not found under {_REPO_ROOT}. "
            "Run this from an omnigent-ai/omnigent source checkout."
        )

    bug_url = args.bug_url or input("Bug URL (GitHub issue or Linear ticket): ").strip()
    if not bug_url:
        _die("no bug URL given")

    # The agent's input contract is the bug URL (it always reproduces against the
    # running build, so there's no version to pass), plus an optional `public`
    # flag telling it to share the session public-read right after it starts.
    payload: dict[str, object] = {"bug_url": bug_url}
    if args.public:
        payload["public"] = True
    prompt = json.dumps(payload)

    # Create the isolated worktree off the CURRENT checkout's HEAD. We resolve
    # HEAD to a concrete commit and pass it as the base, because create_worktree
    # otherwise branches from the MAIN work tree's HEAD — which, when this script
    # runs from a linked worktree (a feature branch), would miss the very commit
    # that adds dev/repro-agent. Pinning the base to our own HEAD makes the new
    # worktree a faithful copy of what we're running from.
    from omnigent.host.git_worktree import WorktreeError, create_worktree

    head = subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    if not head:
        _die(f"could not resolve HEAD of {_REPO_ROOT}")

    branch = _unique_branch(_slug_from_bug_url(bug_url))
    try:
        created = create_worktree(repo_path=str(_REPO_ROOT), branch_name=branch, base_branch=head)
    except WorktreeError as exc:
        _die(f"could not create worktree: {exc}")
    worktree = Path(created.worktree_path)
    print(f"→ worktree: {worktree}  (branch {created.branch})")

    # Run the agent FROM the worktree so the worktree is its workspace (local
    # mode uses the runner's cwd as the workspace). dev/repro-agent resolves
    # relative to the worktree, which is a full checkout of the same commit.
    cmd = ["omnigent", "run", _AGENT_REL, "-p", prompt]
    if args.server is not None:
        cmd += ["--server", args.server]
    print(f"→ running: {' '.join(cmd)}")
    print(f"→ cwd:     {worktree}\n")

    result = subprocess.run(cmd, cwd=str(worktree), check=False)

    # Always keep the worktree; the authored test (if any) lives on its branch.
    print(
        f"\n→ done (exit {result.returncode}). Test (if authored) is on branch "
        f"{created.branch} in:\n    {worktree}\n"
        f"  Inspect:  git -C {worktree} status\n"
        f"  Clean up: git worktree remove {worktree} && "
        f"git branch -D {created.branch}"
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    # Ensure the omnigent package (this checkout) is importable when run as a
    # plain script from the repo root.
    sys.path.insert(0, str(_REPO_ROOT))
    main()
