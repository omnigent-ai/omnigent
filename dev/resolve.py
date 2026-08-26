#!/usr/bin/env python3
"""Drive the resolve-agent (dev/resolve-agent) against a reproduced bug, in a worktree.

Maintainer-only convenience wrapper — the step *after* ``dev/repro.py``. Where
repro.py produces a reproduction (a verdict + an e2e test on a ``repro/<slug>``
branch), this feeds that reproduction to the resolve-agent, which either reviews
an existing fix PR (running the repro test against it) or, when none exists,
root-causes the bug, fixes it, proves the fix with a fail→pass test transition,
and opens a PR.

It takes a **pointer to a completed repro run**, not the bug itself:

  * a local repro **session** (link or bare id) — the common case, right after
    ``dev/repro.py``; or
  * a **--ci-link** (a CI run URL) — when repro-agent ran in throwaway CI and its
    worktree is gone.

Either way the driver creates a fresh ``fix/<slug>`` worktree off latest ``main``
(the slug derived from the pointer you passed) and hands the pointer to the agent.
It does NOT try to locate the repro worktree — there is no stored session→worktree
link, so guessing "the newest ``repro/*`` worktree" is wrong whenever more than
one exists (it branches + stages an unrelated bug). Instead the agent asks the
session where it ran (``sys_session_get_info`` → ``workspace``) and reads the full
uncommitted repro test off that worktree's disk; for --ci-link it recovers the
test from the run's artifacts.

Because the resolve-agent **pushes, opens a PR, or comments on an existing PR**,
this script confirms with you before launching it (skip with ``--yes``). The
agent runs unattended after that.

Usage (from the repo root):
  python dev/resolve.py http://localhost:6767/c/dc59e331-...   # local session link
  python dev/resolve.py dc59e331-...                           # bare session id
  python dev/resolve.py --ci-link https://github.com/omnigent-ai/omnigent-internal/actions/runs/30974269184
  python dev/resolve.py <session> --yes                        # skip the confirm
  python dev/resolve.py <session> --skip-push                  # author: commit locally, no push/PR
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import NoReturn

# dev/resolve.py → repo root is the parent of dev/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_AGENT_REL = "dev/resolve-agent"


def _die(msg: str) -> NoReturn:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


# --- pure helpers (unit-tested in tests/dev/test_resolve.py) ----------------


def parse_session_ref(ref: str) -> str:
    """Extract a bare session id from a session link or a bare id.

    Accepts a server URL like ``http://host:6767/c/<id>`` (the app's session
    route), a ``/sessions/<id>`` form, or an already-bare id. Returns the id
    (the last non-empty path segment), stripped of query/fragment. Raises
    ``ValueError`` on an empty input.
    """
    ref = ref.strip()
    if not ref:
        raise ValueError("empty session reference")
    # Drop scheme://host and any query/fragment, then take the last path segment.
    without_scheme = re.sub(r"^[a-zA-Z][a-zA-Z0-9+.-]*://[^/]+", "", ref)
    path = without_scheme.split("?", 1)[0].split("#", 1)[0]
    segments = [seg for seg in path.split("/") if seg and seg not in ("c", "sessions")]
    return segments[-1] if segments else ref


def parse_ci_run_url(url: str) -> dict[str, str] | None:
    """Parse a GitHub Actions run URL into ``{org, repo, run_id}``.

    Requires a real ``https://github.com`` (or ``www.github.com``) URL whose
    path is ``/<org>/<repo>/actions/runs/<run_id>`` (an optional trailing
    ``/job/<id>`` or ``/attempts/<n>`` is allowed). Parses the URL structurally
    — host, then anchored path — rather than substring-matching, so a string
    that merely *contains* that fragment is rejected. Returns ``None`` when the
    URL is not a recognizable Actions run URL, so the caller can reject it.
    """
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https"):
        return None
    if parsed.netloc.lower() not in ("github.com", "www.github.com"):
        return None
    m = re.fullmatch(
        r"/([^/]+)/([^/]+)/actions/runs/(\d+)(?:/(?:job/\d+|attempts/\d+))?/?",
        parsed.path,
    )
    if not m:
        return None
    return {"org": m.group(1), "repo": m.group(2), "run_id": m.group(3)}


def build_payload(
    *,
    session: str | None,
    ci_link: str | None,
    bug_url: str | None = None,
    skip_push: bool = False,
    public: bool = False,
) -> str:
    """Normalize the input modes into the agent's ``-p`` JSON payload.

    Exactly one of ``session`` / ``ci_link`` must be provided. ``session`` is
    normalized to a bare id; ``ci_link`` is passed through verbatim (the agent
    parses the run itself). ``bug_url`` is *optional and additive*: when given it
    is the **authoritative** bug the agent must resolve — the ``session`` /
    ``ci_link`` run is then used only to recover the reproduction test + verdict,
    and the agent must not resolve a different bug it happens to find on a shared
    server. ``skip_push`` is only added to the payload when true (author mode
    then commits locally but neither pushes nor opens a PR). ``public`` is added
    when true (the agent shares the session public-read at start). Raises
    ``ValueError`` if neither or both of session/ci_link are given, or one is
    unparseable.
    """
    if bool(session) == bool(ci_link):
        raise ValueError("provide exactly one of a session reference or --ci-link")
    payload: dict[str, object]
    if session:
        payload = {"session": parse_session_ref(session)}
    else:
        assert ci_link is not None
        if parse_ci_run_url(ci_link) is None:
            raise ValueError(f"not a GitHub Actions run URL: {ci_link!r}")
        payload = {"ci_link": ci_link.strip()}
    if bug_url and bug_url.strip():
        payload["bug_url"] = bug_url.strip()
    if skip_push:
        payload["skip_push"] = True
    if public:
        payload["public"] = True
    return json.dumps(payload)


def branch_slug(*, session: str | None, ci_link: str | None) -> str:
    """Derive a branch-safe slug from the pointer the caller actually passed.

    The fix branch is ``fix/<slug>``, and the slug comes from the *input*, not
    from guessing which repro worktree produced it — so it never shows a slug
    from an unrelated bug. For a `ci_link` the slug is the CI run id; for a
    `session` it is the (short) session id, lightly sanitized to the characters
    git allows in a ref. The agent recovers the real bug number from the
    reproduction; this is just a stable, honest label for the branch.
    """
    if ci_link:
        parsed = parse_ci_run_url(ci_link)
        return parsed["run_id"] if parsed else "bug"
    if session:
        sid = parse_session_ref(session)
        safe = re.sub(r"[^A-Za-z0-9._-]", "-", sid).strip("-")
        # A full UUID makes an unwieldy branch; the leading segment is unique
        # enough locally and keeps the branch name readable.
        return (safe.split("-", 1)[0] or safe or "bug")[:16]
    return "bug"


# --- git / subprocess plumbing ----------------------------------------------


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run git in the repo (or ``cwd``) and return stdout, dying on failure."""
    result = subprocess.run(
        ["git", "-C", str(cwd or _REPO_ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        _die(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _resolve_base_ref() -> str:
    """Resolve the commit to base the fix worktree on: latest ``origin/main``.

    Fetches ``origin main`` (best-effort) and resolves ``origin/main`` to a
    concrete SHA, so the fix sits on top of mainline rather than whatever branch
    this script happens to run from. Falls back to the local ``main`` ref, and
    finally to ``HEAD``, when the remote isn't reachable (offline runs).
    """
    subprocess.run(
        ["git", "-C", str(_REPO_ROOT), "fetch", "--quiet", "origin", "main"],
        capture_output=True,
        text=True,
        check=False,
    )
    for ref in ("origin/main", "main", "HEAD"):
        result = subprocess.run(
            [
                "git",
                "-C",
                str(_REPO_ROOT),
                "rev-parse",
                "--verify",
                "--quiet",
                f"{ref}^{{commit}}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        sha = result.stdout.strip()
        if result.returncode == 0 and sha:
            return sha
    _die(f"could not resolve a base ref (origin/main, main, HEAD) in {_REPO_ROOT}")


def _unique_branch(slug: str) -> str:
    """Return ``fix/<slug>`` (or ``fix/<slug>-2``, …) not yet used locally."""
    existing = set(_git("branch", "--format=%(refname:short)").split())
    base = f"fix/{slug}"
    if base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="dev/resolve.py",
        description="Run dev/resolve-agent against a reproduced bug, in a worktree.",
    )
    p.add_argument(
        "session",
        nargs="?",
        help="Repro-agent session link or bare id (the local path). Omit when using --ci-link.",
    )
    p.add_argument(
        "--ci-link",
        dest="ci_link",
        default=None,
        help="A GitHub Actions run URL for a CI repro run (the CI path). The "
        "agent recovers the verdict and test from the run's artifacts.",
    )
    p.add_argument(
        "--bug-url",
        dest="bug_url",
        default=None,
        help="Authoritative bug the reproduction is for (a GitHub issue or Linear "
        "ticket URL). Optional; when set, the agent resolves EXACTLY this bug and "
        "uses the session/ci-link run only to recover the test + verdict — it must "
        "not resolve a different bug it finds on a shared server. Prevents the "
        "agent mis-identifying the bug when many repro sessions share one server.",
    )
    p.add_argument(
        "--server",
        default=None,
        help="Omnigent server URL to run against. Omit to use the local server "
        "omnigent run spins up.",
    )
    p.add_argument(
        "--skip-push",
        dest="skip_push",
        action="store_true",
        help="Author mode only: commit the fix locally but do NOT push the branch "
        "or open a PR — leaving the commit in the local worktree for you to inspect, "
        "push, and PR yourself. No effect in review mode (which pushes nothing "
        "either way).",
    )
    p.add_argument(
        "--yes",
        action="store_true",
        help="Skip the pre-launch confirmation. The resolve-agent pushes, opens a "
        "PR, or comments on an existing PR, so this authorizes those outward "
        "actions up front.",
    )
    p.add_argument(
        "--public",
        action="store_true",
        help="Share the resolve session public-read (anyone who can reach the "
        "server) right after it starts. Off by default — useful when running "
        "against a shared --server so the session is spectatable while it works.",
    )
    return p.parse_args()


def _confirm_launch(
    payload: str, branch: str, base: str, *, skip_push: bool, assume_yes: bool
) -> None:
    """Confirm before launching, since the agent takes outward git/GitHub actions."""
    author_line = (
        "COMMIT locally but NOT push or open a PR (--skip-push)"
        if skip_push
        else "PUSH a branch and OPEN a ready-for-review PR"
    )
    print(
        "\nThe resolve-agent takes outward actions once launched. It will either:\n"
        "  - review an existing fix PR (comment findings on it), or\n"
        f"  - implement a fix, run tests, then {author_line}.\n"
        f"  input:    {payload}\n"
        f"  branch:   {branch}  (off {base[:12]})\n"
    )
    if assume_yes:
        print("→ --yes given; proceeding without confirmation.\n")
        return
    reply = input("Proceed? [y/N] ").strip().lower()
    if reply not in ("y", "yes"):
        _die("aborted by user")


def main() -> None:
    args = _parse_args()

    # Source-checkout guard: dev/resolve-agent must exist next to this script.
    agent_dir = _REPO_ROOT / _AGENT_REL
    if not (agent_dir / "config.yaml").is_file():
        _die(
            f"{_AGENT_REL}/config.yaml not found under {_REPO_ROOT}. "
            "Run this from an omnigent-ai/omnigent source checkout."
        )

    try:
        payload = build_payload(
            session=args.session,
            ci_link=args.ci_link,
            bug_url=args.bug_url,
            skip_push=args.skip_push,
            public=args.public,
        )
    except ValueError as exc:
        _die(str(exc))

    from omnigent.host.git_worktree import WorktreeError, create_worktree

    # Base the fresh fix worktree on the latest `main`, NOT this checkout's HEAD:
    # the script may be run from a feature branch, and branching off HEAD would
    # drag that branch's unrelated commits into the fix (contaminating the PR /
    # review). The agent recovers the reproduction from the pointer you gave —
    # the driver does NOT try to map your session to a repro/<slug> worktree
    # (there is no stored session→worktree link, so "pick the newest repro
    # worktree" is wrong whenever more than one exists). Instead the agent calls
    # sys_session_get_info to learn the repro session's own workspace and reads
    # the full uncommitted test off that worktree's disk (the session transcript
    # truncates large tool args, so the file — not the transcript — is the source
    # of truth); for --ci-link it recovers the test from the run's artifacts. The
    # branch is named from the pointer you actually passed (the session id or the
    # CI run id), so it never shows a phantom slug.
    slug = branch_slug(session=args.session, ci_link=args.ci_link)
    base = _resolve_base_ref()
    branch = _unique_branch(slug)

    # Confirm BEFORE creating the worktree, so answering "no" doesn't leave an
    # orphaned fix/<slug> worktree + branch on disk.
    _confirm_launch(payload, branch, base, skip_push=args.skip_push, assume_yes=args.yes)

    try:
        created = create_worktree(repo_path=str(_REPO_ROOT), branch_name=branch, base_branch=base)
    except WorktreeError as exc:
        _die(f"could not create worktree: {exc}")
    worktree = Path(created.worktree_path)
    print(f"→ worktree: {worktree}  (branch {created.branch})")

    # Pass the agent by ABSOLUTE path from this (main) checkout. `omnigent run`
    # resolves a relative agent path against its cwd — which we set to the fresh
    # fix worktree below — but that worktree is a bare checkout of `main` and does
    # not necessarily contain dev/resolve-agent (e.g. run before this lands, or
    # from an older base), so a relative path could 404. The main checkout always
    # has the agent files; cwd stays the worktree so the agent still edits there.
    agent_arg = str(_REPO_ROOT / _AGENT_REL)
    cmd = ["omnigent", "run", agent_arg, "-p", payload]
    if args.server is not None:
        cmd += ["--server", args.server]

    env = os.environ.copy()
    print(f"→ running: {' '.join(cmd)}")
    print(f"→ cwd:     {worktree}\n")

    result = subprocess.run(cmd, cwd=str(worktree), env=env, check=False)

    print(
        f"\n→ done (exit {result.returncode}). Fix branch {created.branch} in:\n"
        f"    {worktree}\n"
        f"  Inspect:  git -C {worktree} status\n"
        f"  Clean up: git worktree remove {worktree} && git branch -D {created.branch}"
    )
    raise SystemExit(result.returncode)


if __name__ == "__main__":
    # Ensure the omnigent package (this checkout) is importable when run as a
    # plain script from the repo root.
    sys.path.insert(0, str(_REPO_ROOT))
    main()
