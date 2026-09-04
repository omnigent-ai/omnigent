"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Powers the web UI's read-only "GitHub" rail tab, which is purely a PR view: the
changed-files list and the whole-PR patch come straight from GitHub via ``gh``
(``gh api .../pulls/<n>/files`` and ``gh pr diff``), so they match the PR's
"Files changed" exactly. With no PR for the branch the tab shows its "no PR"
empty state and fetches nothing.

Design notes:

- Commands run via plain :func:`subprocess.run` in the workspace root, NOT the
  sandboxed OS-env shell helper. The helper strips secrets from the environment,
  which would break ``gh`` auth; a plain subprocess inherits the runner process
  environment (the developer's ``gh`` auth in local dev).
- The list and patch are GitHub-computed, never a local ``git diff``, so a stale
  local ``origin/<base>`` can't inflate them with files outside the PR.
- Only the on-demand per-file expand-context reader (:func:`github_file_diff`)
  still uses ``git show`` for full before/after content — a unified-diff blob
  can't drive the viewer's context expansion.
- The branch→PR lookup resolves in one ``gh`` call, picked by whether the pushed
  ref (``branch.<name>.merge``) was renamed from the local branch — the mark of
  a fork / triangular push (Databricks prefixes it with ``<user>/``). Renamed →
  ``gh pr list --head <pushed-ref>``, which matches the head ref name alone and
  so finds a fork head a bare ``gh pr view`` misses. Not renamed → a bare ``gh pr
  view``, which is correct and more precise for same-repo branches and returns
  nothing for a base branch like ``master`` (``gh pr list --head master`` would
  wrongly match a stranger's PR whose head merely shares the name).
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


def _current_branch(root: str) -> str | None:
    """Return the workspace's current branch name, or ``None`` (detached / not a repo)."""
    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc != 0:
        return None
    return out.strip() or None


def _pushed_head_ref(root: str, branch: str) -> str | None:
    """Return ``branch``'s pushed head ref name, or ``None``.

    Reads the configured upstream (``branch.<name>.merge``), whose value is the
    ref that was actually pushed — which may carry a ``<user>/`` prefix under a
    fork / triangular push flow and so differ from the local branch name.
    ``None`` with no upstream configured.
    """
    rc, out, _ = _git(["config", f"branch.{branch}.merge"], cwd=root)
    if rc != 0 or not out.strip():
        return None
    return out.strip().removeprefix("refs/heads/") or None


def _pr_view_json(root: str, fields: str) -> dict[str, Any] | None:
    """Return the branch's PR as a ``gh``-JSON object for ``fields``, or ``None``.

    Resolves the PR in a single ``gh`` call, choosing the query from a cheap git
    signal: whether the pushed ref (``branch.<name>.merge``) was *renamed* from
    the local branch name. A fork / triangular push renames it (Databricks pushes
    carry a ``<user>/`` prefix), and only then does a bare ``gh pr view`` miss the
    PR — its head-repo-owner guess looks in the wrong place. There we resolve by
    the pushed ref with ``gh pr list --head`` (``--json`` takes the same fields,
    and a row shares the shape of a ``gh pr view`` object, so it parses
    identically; ``--state all`` keeps merged/closed PRs visible).

    Otherwise a bare ``gh pr view`` is correct and *more precise*: it resolves
    same-repo and standard-fork PRs, and returns nothing for a base branch like
    ``master``. Using ``gh pr list --head`` there would be wrong — it matches on
    the head ref name alone, so on ``master`` it returns a stranger's unrelated
    PR whose head merely happens to be named ``master``.
    """
    branch = _current_branch(root)
    pushed_ref = _pushed_head_ref(root, branch) if branch is not None else None
    if pushed_ref is not None and pushed_ref != branch:
        rc, out, _ = _gh(
            [
                "pr",
                "list",
                "--head",
                pushed_ref,
                "--state",
                "all",
                "--limit",
                "1",
                "--json",
                fields,
            ],
            cwd=root,
        )
        if rc != 0:
            return None
        try:
            rows = json.loads(out)
        except ValueError:
            return None
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            return rows[0]
        return None

    rc, out, _ = _gh(["pr", "view", "--json", fields], cwd=root)
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def github_info(root: str) -> dict[str, Any]:
    """Resolve GitHub context for the workspace: repo, branch, base, and PR.

    Git-first: a git checkout is the fundamental requirement, so ``available``
    reflects "is a git repo". ``gh`` layers the repo / PR metadata on top;
    ``base_ref`` is the PR's base branch (``None`` when there's no PR, since the
    tab is a pure PR view).

    :param root: Absolute path to the session workspace.
    :returns: A ``session.github.info`` object. ``available`` is false only when
        this isn't a git repo (``reason: not_a_git_repo``). ``gh_available`` /
        ``authenticated`` report whether the ``gh`` CLI is present and signed in;
        ``repo`` / ``pr`` / ``base_ref`` are null without it.
    """
    payload: dict[str, Any] = {"object": "session.github.info"}

    rc, out, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if rc != 0:
        payload.update(available=False, reason="not_a_git_repo")
        return payload
    branch = out.strip()
    payload.update(
        available=True,
        branch=branch,
        base_ref=None,
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

    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner"], cwd=root)
    if rc == 0:
        try:
            data = json.loads(out)
            payload["repo"] = {"name_with_owner": data.get("nameWithOwner")}
        except (ValueError, AttributeError):
            pass

    pr: dict[str, Any] | None = None
    data = _pr_view_json(root, _PR_VIEW_FIELDS)
    if data is not None:
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
    payload["pr"] = pr

    # A pure PR view: the base is the PR's base branch, else null (no PR).
    payload["base_ref"] = pr.get("base_ref") if pr else None
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


# GitHub pulls/files ``status`` → the status vocabulary the web list uses.
_GH_STATUS_MAP = {
    "added": "created",
    "removed": "deleted",
    "modified": "modified",
    "renamed": "renamed",
    "copied": "created",
    "changed": "modified",
    "unchanged": "modified",
}


def _pr_number(root: str) -> int | None:
    """Return the PR number for the workspace's branch, or ``None``.

    :param root: Absolute workspace path.
    :returns: The associated PR's number, or ``None`` when no PR resolves (none
        for the branch, ``gh`` missing, or not authenticated).
    """
    data = _pr_view_json(root, "number")
    if data is None:
        return None
    number = data.get("number")
    return number if isinstance(number, int) else None


def github_changed_files(root: str) -> dict[str, Any]:
    """List the PR's changed files, straight from GitHub.

    Sourced from ``gh api .../pulls/<n>/files`` so the set (and each file's
    status / line counts) matches the PR's "Files changed" exactly — never a
    local ``git diff``. Empty when the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
    number = _pr_number(root)
    if number is None:
        return empty
    # ``{owner}`` / ``{repo}`` are filled by ``gh`` from the repo; ``--paginate``
    # concatenates the pages of the (array) response into one JSON array.
    rc, out, _ = _gh(
        ["api", "--paginate", f"repos/{{owner}}/{{repo}}/pulls/{number}/files?per_page=100"],
        cwd=root,
    )
    if rc != 0:
        return empty
    try:
        entries = json.loads(out)
    except ValueError:
        return empty
    if not isinstance(entries, list):
        return empty

    data: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        # ``filename`` is the current path (the new name for a rename) — the one
        # the diff endpoint reads at HEAD, matching the whole-PR patch.
        path = entry.get("filename")
        if not path:
            continue
        data.append(
            {
                "object": "session.github.changed_file",
                "path": path,
                "name": str(path).split("/")[-1],
                "status": _GH_STATUS_MAP.get(str(entry.get("status")), "modified"),
                "lines_added": entry.get("additions"),
                "lines_removed": entry.get("deletions"),
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


def github_pr_diff(root: str) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch, straight from GitHub.

    ``gh pr diff <number>`` yields the PR's "Files changed" patch (server-computed
    against the base's merge-base), which the web view parses client-side into
    per-file diffs. The PR is resolved by number first (via :func:`_pr_number`,
    which handles fork / triangular heads a bare ``gh pr diff`` can't); empty when
    the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when there's no PR / no changes).
    """
    empty: dict[str, Any] = {"object": "session.github.pr_diff", "patch": ""}
    number = _pr_number(root)
    if number is None:
        return empty
    rc, out, _ = _gh(["pr", "diff", str(number)], cwd=root)
    return {"object": "session.github.pr_diff", "patch": out if rc == 0 else ""}
