"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Powers the web UI's read-only "GitHub" rail tab. Everything here shells out to
``gh`` (for PR metadata) and ``git`` (for the branch-vs-base diff) inside the
session's workspace, mirroring the ``git`` subprocess pattern the changed-files
/ diff endpoints already use (see :mod:`omnigent.runtime.filesystem_registry`).

Design notes:

- Commands run via plain :func:`subprocess.run` in the workspace root, NOT the
  sandboxed OS-env shell helper. The helper strips secrets from the environment,
  which would break ``gh`` auth; a plain subprocess inherits the runner process
  environment (the developer's ``gh`` auth in local dev).
- The diff is computed locally with ``git`` against the PR's merge-base (the
  three-dot / "Files changed" semantics GitHub shows), so it yields full
  before/after file content the Monaco diff viewer can render — a unified-diff
  blob cannot.
- ``available: false`` payloads let the tab render a message ("gh not installed",
  "not a git repo") instead of surfacing an error.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from typing import Any

from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# ``gh pr view`` / ``gh repo view`` reach the GitHub API, so they get their own,
# slightly more generous timeout than the local ``git`` reads. Overridable via
# ``OMNIGENT_GH_TIMEOUT_SECONDS`` so operators can tune it without a restart.
_DEFAULT_GH_TIMEOUT_SECONDS = 15.0

# Fields requested from ``gh pr view``. Always pass ``--json`` — bare
# ``gh pr view`` opens an interactive/pager view and misbehaves in a
# non-interactive subprocess.
_PR_VIEW_FIELDS = "number,title,state,url,isDraft,author,baseRefName,headRefName,statusCheckRollup"


def _gh_timeout_seconds() -> float:
    """Return the ``gh``-subprocess timeout, honoring the env override."""
    raw = os.environ.get("OMNIGENT_GH_TIMEOUT_SECONDS")
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return _DEFAULT_GH_TIMEOUT_SECONDS


def _run(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
) -> tuple[int | None, str, str]:
    """Run a subprocess and capture its output, never raising.

    :param argv: Command and arguments.
    :param cwd: Working directory to run in.
    :param timeout: Wall-clock cap in seconds.
    :returns: ``(returncode, stdout, stderr)``. ``returncode`` is ``None`` when
        the command could not run at all (spawn error / timeout), so callers can
        distinguish "ran and failed" from "never ran".
    """
    started = time.monotonic()
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        _logger.warning(
            "github_resource: %r in %s timed out after %.2fs",
            argv,
            cwd,
            time.monotonic() - started,
        )
        return None, "", "timed out"
    except OSError as exc:
        _logger.warning("github_resource: %r in %s could not run: %s", argv, cwd, exc)
        return None, "", str(exc)
    return (
        result.returncode,
        result.stdout.decode("utf-8", errors="replace"),
        result.stderr.decode("utf-8", errors="replace"),
    )


def _git(argv: list[str], *, cwd: str) -> tuple[int | None, str, str]:
    return _run(["git", *argv], cwd=cwd, timeout=_git_timeout_seconds())


def _gh(argv: list[str], *, cwd: str) -> tuple[int | None, str, str]:
    return _run(["gh", *argv], cwd=cwd, timeout=_gh_timeout_seconds())


# Cap the per-check list so a pathological rollup can't bloat the payload; the
# counts stay exact regardless.
_MAX_CHECK_RUNS = 300


def _classify_check(check: dict[str, Any]) -> str:
    """Bucket a single ``statusCheckRollup`` entry: passing / failing / pending."""
    # CheckRun carries status/conclusion; StatusContext carries state.
    state = check.get("state")
    if state is not None:
        upper = str(state).upper()
        if upper == "SUCCESS":
            return "passing"
        if upper in ("FAILURE", "ERROR"):
            return "failing"
        return "pending"
    if str(check.get("status", "")).upper() != "COMPLETED":
        return "pending"
    conclusion = str(check.get("conclusion", "")).upper()
    return "passing" if conclusion in ("SUCCESS", "NEUTRAL", "SKIPPED") else "failing"


def _summarize_checks(rollup: Any) -> dict[str, Any]:
    """Summarize a ``statusCheckRollup`` into bucket counts + per-check details.

    :returns: ``{passing, failing, pending, total, runs}`` where ``runs`` is a
        list of ``{name, bucket, url}`` (the job names the UI shows on hover).
    """
    counts = {"passing": 0, "failing": 0, "pending": 0}
    runs: list[dict[str, Any]] = []
    if isinstance(rollup, list):
        for check in rollup:
            if not isinstance(check, dict):
                continue
            bucket = _classify_check(check)
            counts[bucket] += 1
            if len(runs) < _MAX_CHECK_RUNS:
                # CheckRun → name (falling back to the workflow); StatusContext
                # → context. Link is detailsUrl (CheckRun) or targetUrl (status).
                name = check.get("name") or check.get("context") or check.get("workflowName")
                runs.append(
                    {
                        "name": str(name) if name else "check",
                        "bucket": bucket,
                        "url": check.get("detailsUrl") or check.get("targetUrl") or None,
                    }
                )
    return {
        "passing": counts["passing"],
        "failing": counts["failing"],
        "pending": counts["pending"],
        "total": counts["passing"] + counts["failing"] + counts["pending"],
        "runs": runs,
    }


def _git_default_base(root: str) -> str | None:
    """Detect the repo's default branch name from git alone (no ``gh``).

    Lets the branch-vs-base diff resolve a base even when ``gh`` is missing or
    unauthenticated (e.g. served over the host fallback). Prefers the remote's
    published HEAD, then a conventional ``main``/``master``.

    :param root: Absolute workspace path.
    :returns: A branch name, e.g. ``"main"``, or ``None``.
    """
    rc, out, _ = _git(["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], cwd=root)
    if rc == 0 and out.strip():
        # "refs/remotes/origin/main" → "main"
        return out.strip().rsplit("/", 1)[-1]
    # No published remote HEAD: fall back to a conventional default, preferring
    # the remote-tracking ref, then a local branch (a local-only workspace).
    for candidate in ("origin/main", "origin/master", "main", "master"):
        rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=root)
        if rc == 0:
            return candidate.rsplit("/", 1)[-1]
    return None


def github_info(root: str) -> dict[str, Any]:
    """Resolve GitHub context for the workspace: repo, branch, base, and PR.

    Git-first: a git checkout is the fundamental requirement (the branch-vs-base
    diff is viewable from git alone), and ``gh`` layers PR/repo metadata on top.
    So ``available`` reflects "is a git repo", and ``base_ref`` is populated from
    git even when ``gh`` is absent — this is what lets the host fallback serve
    the diff when the runner is offline and its machine has no ``gh``.

    :param root: Absolute path to the session workspace.
    :returns: A ``session.github.info`` object. ``available`` is false only when
        this isn't a git repo (``reason: not_a_git_repo``). ``gh_available`` /
        ``authenticated`` report whether the ``gh`` CLI is present and signed in;
        ``repo`` / ``pr`` are null without it, and the diff still renders.
    """
    payload: dict[str, Any] = {"object": "session.github.info"}

    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc != 0:
        payload.update(available=False, reason="not_a_git_repo")
        return payload
    branch = out.strip()
    git_base = _git_default_base(root)
    payload.update(
        available=True,
        branch=branch,
        base_ref=git_base,
        repo=None,
        pr=None,
    )

    # gh is an enhancement layer: without it (or its auth) the git diff still
    # renders; the UI notes the missing CLI / sign-in from these flags.
    if shutil.which("gh") is None:
        payload.update(gh_available=False, authenticated=False)
        return payload
    payload["gh_available"] = True

    auth_rc, _, _ = _gh(["auth", "status"], cwd=root)
    authenticated = auth_rc == 0
    payload["authenticated"] = authenticated
    if not authenticated:
        return payload

    default_branch: str | None = None
    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner,defaultBranchRef"], cwd=root)
    if rc == 0:
        try:
            data = json.loads(out)
            payload["repo"] = {"name_with_owner": data.get("nameWithOwner")}
            ref = data.get("defaultBranchRef")
            if isinstance(ref, dict):
                default_branch = ref.get("name")
        except (ValueError, AttributeError):
            pass

    pr: dict[str, Any] | None = None
    rc, out, _ = _gh(["pr", "view", "--json", _PR_VIEW_FIELDS], cwd=root)
    if rc == 0:
        try:
            data = json.loads(out)
            author = data.get("author")
            pr = {
                "number": data.get("number"),
                "title": data.get("title"),
                "state": data.get("state"),
                "url": data.get("url"),
                "is_draft": data.get("isDraft", False),
                "author": author.get("login") if isinstance(author, dict) else None,
                "base_ref": data.get("baseRefName"),
                "head_ref": data.get("headRefName"),
                "checks": _summarize_checks(data.get("statusCheckRollup")),
            }
        except (ValueError, AttributeError):
            pr = None
    payload["pr"] = pr

    # Diff base precedence: the PR's base, else gh's default branch, else the
    # git-derived default already set above.
    payload["base_ref"] = (pr.get("base_ref") if pr else None) or default_branch or git_base
    return payload


def resolve_base_ref(root: str, base: str | None) -> str | None:
    """Return an explicit base branch, else the repo's default diff base.

    Shared by the runner routes and the host reader so both resolve an omitted
    ``?base=`` identically (via :func:`github_info`).

    :param root: Absolute workspace path.
    :param base: Explicit base branch name, or ``None`` to derive the default.
    :returns: A base branch name, or ``None`` when none can be resolved.
    """
    if base:
        return base
    return github_info(root).get("base_ref")


def _resolve_diff_base(root: str, base: str) -> str | None:
    """Resolve a base branch name to the ref to diff HEAD against.

    Prefers the merge-base of ``origin/<base>`` (or ``<base>``) and HEAD, giving
    the three-dot / "Files changed" semantics GitHub shows. Falls back to the
    base ref itself, then ``None`` when nothing resolves.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :returns: A ref (SHA or name) to diff against, or ``None``.
    """
    candidates = [f"origin/{base}", base]
    resolved: str | None = None
    for candidate in candidates:
        rc, _, _ = _git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], cwd=root)
        if rc == 0:
            resolved = candidate
            break
    if resolved is None:
        return None
    rc, out, _ = _git(["merge-base", resolved, "HEAD"], cwd=root)
    if rc == 0 and out.strip():
        return out.strip()
    return resolved


# git diff status letters → the status vocabulary the web changed-files list uses.
_STATUS_MAP = {
    "A": "created",
    "M": "modified",
    "D": "deleted",
    "R": "renamed",
    "C": "created",
    "T": "modified",
}


def github_changed_files(root: str, base: str) -> dict[str, Any]:
    """List files changed on HEAD relative to the base branch's merge-base.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    diff_base = _resolve_diff_base(root, base)
    if diff_base is None:
        return {"object": "list", "data": [], "has_more": False}

    # numstat first (adds/dels + final path), keyed by path for the status merge.
    # ``-M`` detects renames so a moved file shows once (matching the whole-PR
    # patch the diff view parses), not as a delete + add pair.
    counts: dict[str, tuple[int | None, int | None]] = {}
    rc, out, _ = _git(["diff", "-M", "--numstat", diff_base, "HEAD"], cwd=root)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            added, removed, path = parts[0], parts[1], parts[-1]
            counts[path] = (
                None if added == "-" else int(added),
                None if removed == "-" else int(removed),
            )

    data: list[dict[str, Any]] = []
    rc, out, _ = _git(["diff", "-M", "--name-status", diff_base, "HEAD"], cwd=root)
    if rc == 0:
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            code = parts[0][:1]
            # For renames/copies (``R100\told\tnew``) the last field is the
            # current path — the one the diff endpoint reads at HEAD.
            path = parts[-1]
            added, removed = counts.get(path, (None, None))
            data.append(
                {
                    "object": "session.github.changed_file",
                    "path": path,
                    "name": path.split("/")[-1],
                    "status": _STATUS_MAP.get(code, "modified"),
                    "lines_added": added,
                    "lines_removed": removed,
                }
            )
    return {"object": "list", "data": data, "has_more": False}


def github_file_diff(root: str, base: str, path: str) -> dict[str, Any]:
    """Return before/after content for one file, HEAD vs the base merge-base.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :param path: Repo-root-relative path, as returned by
        :func:`github_changed_files`.
    :returns: A ``session.github.file_diff`` object with ``before`` (merge-base
        content, ``None`` for an added file) and ``after`` (HEAD content,
        ``None`` for a deleted file).
    """
    diff_base = _resolve_diff_base(root, base)

    before: str | None = None
    if diff_base is not None:
        rc, out, _ = _git(["show", f"{diff_base}:{path}"], cwd=root)
        if rc == 0:
            before = out

    after: str | None = None
    rc, out, _ = _git(["show", f"HEAD:{path}"], cwd=root)
    if rc == 0:
        after = out

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": before,
        "after": after,
    }


def github_pr_diff(root: str, base: str) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch (HEAD vs the base merge-base).

    One ``git diff`` for every changed file, so the web view can render the
    entire PR from a single call (parsed client-side into per-file diffs).
    ``-M`` detects renames so a move renders as one file rather than a
    delete + add.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when the base can't be resolved / there are no changes).
    """
    diff_base = _resolve_diff_base(root, base)
    if diff_base is None:
        return {"object": "session.github.pr_diff", "patch": ""}
    rc, out, _ = _git(["diff", "-M", diff_base, "HEAD"], cwd=root)
    return {"object": "session.github.pr_diff", "patch": out if rc == 0 else ""}
