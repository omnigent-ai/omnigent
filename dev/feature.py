#!/usr/bin/env python3
"""Drive the feature-agent (dev/feature-agent) against a Linear feature ticket.

Maintainer-only convenience wrapper. It creates a fresh ``feat/<ticket-key>``
worktree off latest ``main``, confirms (the agent pushes and opens a PR), then
runs the agent there with the ticket as its only input. The agent always builds
on omnigent-ai/omnigent.

Usage (from the repo root):
  python dev/feature.py https://linear.app/omnigent/issue/OMNI-1234/slug
  python dev/feature.py <linear-url> --skip-push   # commit locally, no push/PR
  python dev/feature.py <linear-url> --yes         # skip the confirmation
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.parse
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_REL = "dev/feature-agent"


def ticket_key(url: str) -> str:
    """Return the lowercase ticket key (``omni-1234``) of a Linear issue URL.

    Raises ``ValueError`` when ``url`` is not a ``linear.app`` issue URL.
    """
    parsed = urllib.parse.urlparse(url.strip())
    match = re.fullmatch(r"/[^/]+/issue/([A-Za-z][A-Za-z0-9]*-\d+)(?:/[^/]*)?/?", parsed.path)
    if parsed.scheme != "https" or parsed.netloc.lower() != "linear.app" or not match:
        raise ValueError(f"not a Linear issue URL: {url!r}")
    return match.group(1).lower()


def build_payload(feature_url: str, *, skip_push: bool = False, public: bool = False) -> str:
    """The agent's ``-p`` JSON payload; optional flags appear only when true."""
    ticket_key(feature_url)
    payload: dict[str, object] = {"feature_url": feature_url.strip()}
    if skip_push:
        payload["skip_push"] = True
    if public:
        payload["public"] = True
    return json.dumps(payload)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev/feature.py",
        description="Run dev/feature-agent against a Linear feature ticket, in a worktree.",
    )
    p.add_argument("feature_url", help="Linear feature ticket URL.")
    p.add_argument("--server", default=None, help="Omnigent server URL to run against.")
    p.add_argument(
        "--skip-push",
        dest="skip_push",
        action="store_true",
        help="Commit the feature locally but do not push or open a PR.",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation; authorizes the push and PR up front.",
    )
    p.add_argument(
        "--public", action="store_true", help="Share the session public-read once it starts."
    )
    return p.parse_args()


def main() -> None:
    from dev.repro import _launch_env
    from dev.resolve import _die, _resolve_base_ref, _unique_branch
    from omnigent.host.git_worktree import WorktreeError, create_worktree

    args = _parse_args()
    agent_dir = _REPO_ROOT / _AGENT_REL
    if not (agent_dir / "config.yaml").is_file():
        _die(f"{_AGENT_REL}/config.yaml not found under {_REPO_ROOT}.")
    try:
        payload = build_payload(args.feature_url, skip_push=args.skip_push, public=args.public)
    except ValueError as exc:
        _die(str(exc))

    base = _resolve_base_ref(_REPO_ROOT)
    branch = _unique_branch(ticket_key(args.feature_url), _REPO_ROOT, prefix="feat")
    action = "COMMIT locally only (--skip-push)" if args.skip_push else "PUSH and OPEN a PR"
    print(
        f"\nThe feature-agent will build the feature, then {action}.\n"
        f"  input:  {payload}\n  branch: {branch}  (off {base[:12]})\n"
    )
    if not args.yes:
        try:
            answer = input("Proceed? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            _die("aborted by user")

    try:
        created = create_worktree(repo_path=str(_REPO_ROOT), branch_name=branch, base_branch=base)
    except WorktreeError as exc:
        _die(f"could not create worktree: {exc}")
    worktree = Path(created.worktree_path)

    # Pass the agent by absolute path: the fresh worktree's base may predate it.
    cmd = ["omnigent", "run", str(agent_dir), "-p", payload]
    if args.server is not None:
        cmd += ["--server", args.server]
    print(f"→ running: {shlex.join(cmd)}\n→ cwd:     {worktree}\n")
    try:
        returncode = subprocess.run(
            cmd, cwd=str(worktree), env=_launch_env(args.feature_url), check=False
        ).returncode
    except OSError as exc:
        print(f"error: could not run omnigent: {exc}", file=sys.stderr)
        returncode = 1
    print(
        f"\n→ done (exit {returncode}). Branch {created.branch} in {worktree}\n"
        f"  Clean up: git worktree remove {worktree} && git branch -D {created.branch}"
    )
    raise SystemExit(returncode)


if __name__ == "__main__":
    sys.path.insert(0, str(_REPO_ROOT))
    main()
