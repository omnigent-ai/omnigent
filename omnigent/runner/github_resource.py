"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Implements GitHub's steps of the session PR panel: details, files, patches, and
the attach check. :mod:`omnigent.runner.pr_resource` selects the PR and the
provider and reaches these steps through the GitHub facet
(:mod:`omnigent.runner.git_providers.github`). The ``github_*`` functions here
delegate to ``pr_resource``. Tracked PRs use explicit host/repository/number
identities. Branch and commit discovery remains a fallback for sessions
without recorded PRs. Files and patches come from GitHub; recording an
association does not modify the remote PR.

Design notes:

- Commands run via plain :func:`subprocess.run` in the workspace root, NOT the
  sandboxed OS-env shell helper. The helper strips secrets from the environment,
  which would break ``gh`` auth; a plain subprocess inherits the runner process
  environment, so ``gh`` authenticates as it normally does — the developer's
  ``gh`` login in local dev, and in a managed sandbox the per-user ``hosts.yml``
  that :func:`omnigent.git_credential_github.configure_host_gh` writes from the
  credential broker at host startup. In a sandbox we additionally scrub
  ``GH_TOKEN``/``GITHUB_TOKEN`` from ``gh``'s env (gh ranks those *above*
  ``hosts.yml``), so a stray ambient token — e.g. a gh-MCP env passthrough —
  can't silently make the panel act as a shared identity instead of the
  connected owner. Outside a sandbox the env is inherited untouched.
- The list and patch are GitHub-computed, never a local ``git diff``, so a stale
  local ``origin/<base>`` can't inflate them with files outside the PR.
- Expanded context uses the selected PR's head and merge-base commits through
  GitHub, including fork heads and renames. Legacy calls without a session
  continue to read local git objects.
- The branch→PR lookup is a ``gh pr view --json`` (``--json`` avoids the
  interactive pager and the Projects-classic mis-parse of a bare view), backed by
  a commit-identity fallback for when the remote branch name is decoupled from the
  checkout. Fork / triangular PRs resolve once the two coordinates they turn on
  are set explicitly: the base repo (``gh repo set-default``, which ``gh`` stores
  in ``.git/config`` as ``remote.<name>.gh-resolved``) and the head owner (the
  authenticated account). The panel surfaces an account + remote selector so the
  user pins both; a per-repo account preference (``~/.omnigent/config.yaml``) is
  applied by running each ``gh`` call as the chosen account —
  ``GH_TOKEN=$(gh auth token --user <login>)`` — outside a sandbox (inside one we
  keep the single broker identity).
- When a stacking tool (git-stack, ``git pp``) pushes under a remote branch name
  that differs from the local checkout and leaves no upstream tracking, ``gh``'s
  branch-name lookup misses. The pushed commit is the invariant that still links
  the checkout to its PR, so the fallback asks GitHub which PR a pushed commit
  belongs to (``repos/{owner}/{repo}/commits/{sha}/pulls``), querying the repo it
  was pushed to (the fork for a fork PR) — see :func:`_resolve_pr_via_commit`.
- ``available: false`` payloads let the tab render a message ("gh not installed",
  "not a git repo") instead of surfacing an error.
- A token without the Checks permission (a fine-grained PAT) can read a PR but
  not its check runs: GitHub refuses just ``statusCheckRollup`` and ``gh`` fails
  the whole call, so PR reads retry without that field and report
  ``checks_supported: false`` rather than pretending there is no PR.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any
from urllib.parse import quote

from omnigent import config as _config
from omnigent.runner import pr_resource
from omnigent.runner.git_providers import (
    MERGE_OPTIONS_OBJECT,
    PR_LABELS_OBJECT,
    PR_MERGE_OBJECT,
    local_git,
)
from omnigent.runner.session_prs import PullRequestRef
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# ``gh pr view`` / ``gh repo view`` reach the GitHub API, so they get their own,
# slightly more generous timeout than the local ``git`` reads. Overridable via
# ``OMNIGENT_GH_TIMEOUT_SECONDS`` so operators can tune it without a restart.
_DEFAULT_GH_TIMEOUT_SECONDS = 15.0
# A request with its own time budget bounds every ``gh`` call by this monotonic
# deadline (see :func:`_gh_deadline_scope`); ``_gh_timed_out`` records a cut-off call.
_gh_deadline: ContextVar[float | None] = ContextVar("gh_deadline", default=None)
_gh_timed_out: ContextVar[bool] = ContextVar("gh_timed_out", default=False)

# Always pass --json (bare gh pr view opens a pager); body, comments,
# reviews, labels, and the merge fields feed the Summary and Comments tabs.
_PR_VIEW_FIELDS = (
    "number,title,state,url,isDraft,author,baseRefName,headRefName,statusCheckRollup,body,comments"
    ",reviews,reviewRequests,latestReviews,labels,mergeStateStatus,autoMergeRequest"
)
_PR_REVIEW_FIELDS = frozenset({"reviewRequests", "latestReviews"})
_PR_COMMENTS_FIELD = "reviews"
# Older ``gh`` releases lack these; :func:`_gh_pr_view` retries without them.
_PR_MERGE_FIELDS = frozenset({"mergeStateStatus", "autoMergeRequest"})
# Reading check runs needs the Checks permission, which a fine-grained personal
# access token cannot hold; GitHub refuses just this field and ``gh`` fails the
# whole call, so :func:`_gh_pr_view` retries without it.
_PR_CHECKS_FIELD = "statusCheckRollup"


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
    env: dict[str, str] | None = None,
) -> tuple[int | None, str, str]:
    """Run a subprocess and capture its output, never raising.

    :param argv: Command and arguments.
    :param cwd: Working directory to run in.
    :param timeout: Wall-clock cap in seconds.
    :param env: Full child environment, or ``None`` to inherit this process's
        (the default).
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
            env=env,
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


def _in_sandbox() -> bool:
    """Whether the panel is running inside a managed sandbox (``IS_SANDBOX=1``)."""
    return (os.environ.get("IS_SANDBOX") or "").strip() == "1"


def _gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int | None, str, str]:
    timeout = _gh_timeout_seconds()
    if (deadline := _gh_deadline.get()) is not None:
        timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            _gh_timed_out.set(True)
            return None, "", "gh deadline exceeded"
    # In a managed sandbox the panel must authenticate as the connected owner via
    # the per-user hosts.yml that configure_host_gh writes — never an ambient
    # GH_TOKEN/GITHUB_TOKEN, which gh ranks ABOVE hosts.yml. Scrub them so a stray
    # token in the sandbox/runner env (e.g. a gh-MCP passthrough) can't silently
    # make the panel act as a shared identity. Outside a sandbox (local dev) the
    # env is inherited untouched, so the developer's own gh auth still works.
    #
    # ``token`` deliberately re-adds GH_TOKEN to run this one call as a chosen
    # account (the account selector). It's only ever set outside a sandbox — see
    # _account_token_for — so it never overrides the sandbox's broker identity.
    env: dict[str, str] | None = None
    if _in_sandbox():
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
        }
    if token:
        env = dict(os.environ) if env is None else env
        env["GH_TOKEN"] = token
        env["GH_ENTERPRISE_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
        env.pop("GITHUB_ENTERPRISE_TOKEN", None)
    result = _run(["gh", *argv], cwd=cwd, timeout=timeout, env=env)
    if (
        deadline is not None
        and result[0] is None
        and result[2] == "timed out"
        and time.monotonic() >= deadline
    ):
        _gh_timed_out.set(True)
    return result


@contextmanager
def _gh_deadline_scope(deadline: float) -> Iterator[None]:
    """End every ``gh`` call in the block by ``deadline``, a ``time.monotonic()`` value.

    ``_gh_timed_out`` starts false and turns true once the deadline cuts a call short.
    """
    deadline_token = _gh_deadline.set(deadline)
    timed_out_token = _gh_timed_out.set(False)
    try:
        yield
    finally:
        _gh_timed_out.reset(timed_out_token)
        _gh_deadline.reset(deadline_token)


# ── Account selection ────────────────────────────────────────────────────────
# The panel offers ONE selector — the account (head owner) — since fork/triangular
# PRs resolve by pushed-commit identity and the base repo is auto-resolved from the
# PR. A per-workspace account preference runs every ``gh`` call as the chosen login;
# a sandbox has a single identity so the account override no-ops there.


def _owner_repo_from_url(url: str | None) -> str | None:
    """Derive ``owner/repo`` from a git remote URL, or ``None``.

    Handles HTTPS/SSH/scp-style GitHub URLs, stripping any ``.git`` suffix.
    """
    if not url:
        return None
    candidate = url.strip()
    scp = re.match(r"^[\w.\-]+@[\w.\-]+:(?P<path>.+)$", candidate)
    if scp:
        path = scp.group("path")
    else:
        scheme = re.match(r"^\w+://(?:[^@/]+@)?[\w.\-]+/(?P<path>.+)$", candidate)
        if not scheme:
            return None
        path = scheme.group("path")
    parts = path.removesuffix(".git").strip("/").split("/")
    if len(parts) < 2 or not parts[-1] or not parts[-2]:
        return None
    return f"{parts[-2]}/{parts[-1]}"


def _owner_repo_from_pr_url(url: str | None) -> str | None:
    """Derive the base ``owner/repo`` from a PR's HTML URL, or ``None``.

    A PR lives on its base repo, so ``https://host/<owner>/<repo>/pull/<n>`` names
    it in the first two path segments — lets us skip a separate ``gh repo view``
    once a PR resolves.
    """
    if not url:
        return None
    match = re.match(r"^https?://[^/]+/([^/]+)/([^/]+)/pull/\d+", url.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def _list_accounts(root: str) -> tuple[bool, list[dict[str, Any]]]:
    """Return ``(authenticated, accounts)`` from ``gh auth status --json hosts``.

    ``accounts`` is ``[{login, active, state, host}]``; ``authenticated`` is true
    when at least one account validates (``state == "success"``). Falls back to a
    plain ``gh auth status`` for the boolean on an older ``gh`` without ``--json``.
    """
    _, out, _ = _gh(["auth", "status", "--json", "hosts"], cwd=root)
    try:
        data = json.loads(out)
    except ValueError:
        data = None
    hosts = data.get("hosts") if isinstance(data, dict) else None
    if isinstance(hosts, dict):
        accounts: list[dict[str, Any]] = []
        for host, entries in hosts.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict) or not entry.get("login"):
                    continue
                accounts.append(
                    {
                        "login": entry.get("login"),
                        "active": bool(entry.get("active")),
                        "state": entry.get("state"),
                        "host": entry.get("host") or host,
                    }
                )
        return any(a.get("state") == "success" for a in accounts), accounts
    # Older gh (no --json hosts): fall back to the plain status exit code.
    rc, _, _ = _gh(["auth", "status"], cwd=root)
    return rc == 0, []


def _resolved_base_nwo(root: str) -> str | None:
    """Return the gh-resolved base repo ``owner/repo`` for the checkout, or ``None``.

    Reads ``gh repo set-default --view`` — a local git-config read (no network),
    the same base ``gh`` itself resolves PRs against. ``None`` when no default is
    set (``--view`` exits non-zero) or the output isn't an ``owner/repo``.
    """
    rc, out, _ = _gh(["repo", "set-default", "--view"], cwd=root)
    if rc != 0:
        return None
    value = out.strip()
    if value and "/" in value and " " not in value:
        return value
    return None


def _workspace_key(root: str) -> str | None:
    """Return the stable per-workspace key for the account preference, or ``None``.

    The account preference is keyed by the **main worktree path** — the first
    entry of ``git worktree list`` — so a checkout and all its linked worktrees
    (which share one ``.git``) resolve to the same key, while separate clones stay
    distinct. Purely local (no network, no auth), so it's available before any
    ``gh`` call — the constraint that rules out keying on the resolved base repo.
    """
    rc, out, _ = _git(["worktree", "list", "--porcelain"], cwd=root)
    if rc != 0:
        return None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            return path or None
    return None


def _gh_auth_token(root: str, login: str, host: str = "github.com") -> str | None:
    """Return *login*'s GitHub token via ``gh auth token --user`` (never logged)."""
    rc, out, _ = _gh(["auth", "token", "--user", login, "-h", host], cwd=root)
    if rc != 0:
        return None
    return out.strip() or None


def _account_token_for(root: str, workspace_key: str | None = None) -> str | None:
    """Return the GH_TOKEN to run ``gh`` as this workspace's preferred account.

    ``None`` (use ``gh``'s active auth) inside a sandbox (single broker identity),
    when the workspace key can't be resolved, or when it has no stored preference.

    :param root: Absolute workspace path.
    :param workspace_key: Precomputed key to skip a repeat resolution.
    """
    if _in_sandbox():
        return None
    key = workspace_key if workspace_key is not None else _workspace_key(root)
    if not key:
        return None
    login = _config.github_account_preference(key)
    if not login:
        return None
    return _gh_auth_token(root, login)


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


# Cap the comments list so a very chatty PR can't bloat the payload; keep the
# earliest comments after merging issue comments and review bodies.
_MAX_COMMENTS = 100


def _shape_reviewers(data: dict[str, Any]) -> list[dict[str, str]]:
    """Combine completed reviews with outstanding requests, including re-requests."""
    reviewers: dict[tuple[str, str], dict[str, str]] = {}
    reviews = data.get("latestReviews")
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        author = review.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        state = review.get("state")
        if (
            not isinstance(login, str)
            or not login
            or state
            not in (
                "APPROVED",
                "COMMENTED",
                "CHANGES_REQUESTED",
                "DISMISSED",
            )
        ):
            continue
        reviewers[("user", login.lower())] = {"login": login, "kind": "user", "state": state}

    requests = data.get("reviewRequests")
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        kind = "team" if request.get("__typename") == "Team" else "user"
        login = (
            request.get("slug") or request.get("name") if kind == "team" else request.get("login")
        )
        if not isinstance(login, str) or not login:
            continue
        # A re-request is pending even if the reviewer previously approved.
        reviewers[(kind, login.lower())] = {"login": login, "kind": kind, "state": "PENDING"}
    return list(reviewers.values())


def _shape_comments(
    raw: Any, reviews: Any = None, *, pr_url: str | None = None, cap: bool = True
) -> list[dict[str, Any]]:
    """Merge PR conversation comments and submitted review bodies by timestamp."""
    shaped: list[dict[str, Any]] = []
    for comment in raw if isinstance(raw, list) else []:
        if not isinstance(comment, dict) or comment.get("isMinimized"):
            continue
        author = comment.get("author")
        shaped.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": str(comment.get("body") or ""),
                "created_at": comment.get("createdAt") or None,
                "url": comment.get("url") or None,
            }
        )
    for review in reviews if isinstance(reviews, list) else []:
        if (
            not isinstance(review, dict)
            or not review.get("submittedAt")
            or not str(review.get("body") or "").strip()
        ):
            continue
        author = review.get("author")
        shaped.append(
            {
                "author": author.get("login") if isinstance(author, dict) else None,
                "body": review["body"],
                "created_at": review.get("submittedAt") or None,
                # gh's review objects have no URL; the PR conversation contains
                # the review body, so link there rather than inventing an anchor.
                "url": pr_url,
            }
        )
    shaped.sort(key=lambda comment: comment["created_at"] or "")
    return shaped[:_MAX_COMMENTS] if cap else shaped


def _comments_payload(data: dict[str, Any]) -> dict[str, Any]:
    comments = data.get("comments")
    reviews = data.get(_PR_COMMENTS_FIELD)
    shaped = _shape_comments(comments, reviews, pr_url=data.get("url"), cap=False)
    result: dict[str, Any] = {"comments": shaped[:_MAX_COMMENTS]}
    if (
        not isinstance(comments, list)
        or not isinstance(reviews, list)
        or len(comments) >= _MAX_COMMENTS
        or len(reviews) >= _MAX_COMMENTS
        or len(shaped) > _MAX_COMMENTS
    ):
        result["comments_partial"] = True
    return result


# GitHub label colors are six hex digits; the panel puts them in inline styles.
_LABEL_COLOR = re.compile(r"[0-9a-fA-F]{6}")
# The label picker lists at most this many of the repository's labels.
_MAX_REPO_LABELS = 1000
# Every ``gh`` call of one label or merge request shares this budget, under the
# 20-second waits of the server and the host tunnel.
_PR_EDIT_REQUEST_SECONDS = 15.0
_LABEL_EDIT_TIMED_OUT = "GitHub took too long to update the labels. Refresh to see which applied."
_MERGE_TIMED_OUT = (
    "GitHub took too long to merge the pull request. Refresh to see whether it merged."
)
_DISABLE_AUTO_MERGE_TIMED_OUT = (
    "GitHub took too long to disable auto-merge. Refresh to see whether it is still on."
)
# ``gh pr merge`` method flags in the order the merge picker offers them, keyed
# by the ``gh repo view`` field that allows each.
_MERGE_METHODS = (
    ("merge", "mergeCommitAllowed"),
    ("squash", "squashMergeAllowed"),
    ("rebase", "rebaseMergeAllowed"),
)


def _shape_labels(raw: Any) -> list[dict[str, str | None]]:
    """Shape GitHub labels into ``{name, color, description}``, skipping malformed entries.

    Accepts both ``gh pr view --json labels`` and REST label objects.
    """
    shaped: list[dict[str, str | None]] = []
    for label in raw if isinstance(raw, list) else []:
        name = label.get("name") if isinstance(label, dict) else None
        if not isinstance(name, str) or not name:
            continue
        color = label.get("color")
        description = label.get("description")
        shaped.append(
            {
                "name": name,
                "color": color.lower()
                if isinstance(color, str) and _LABEL_COLOR.fullmatch(color)
                else None,
                "description": description
                if isinstance(description, str) and description
                else None,
            }
        )
    return shaped


def _head_commit_shas(root: str) -> list[str]:
    """Candidate commit SHAs to resolve the PR by identity, most-pushed first.

    ``@{push}`` is the exact ref the branch was pushed to — the pushed tip even
    when a stacking tool renamed the remote branch. ``HEAD`` is the local tip,
    which equals the pushed tip right after such a push when no upstream tracking
    was configured. Order-preserving and deduped; empty on a detached HEAD.
    """
    shas: list[str] = []
    for rev in ("@{push}", "HEAD"):
        rc, out, _ = _git(["rev-parse", "--verify", "--quiet", rev], cwd=root)
        sha = out.strip()
        if rc == 0 and sha and sha not in shas:
            shas.append(sha)
    return shas


def _remote_nwo(root: str, remote: str) -> str | None:
    """``owner/repo`` for a named git remote, or ``None``."""
    rc, url, _ = _git(["remote", "get-url", remote], cwd=root)
    if rc != 0:
        return None
    return _owner_repo_from_url(url.strip())


def _commit_lookup_repo(root: str) -> str | None:
    """The repo the branch was pushed to — where its commit (and PR) live.

    The branch's tracking remote (``branch.<name>.remote``), else ``origin``. One
    repo suffices: ``commits/{sha}/pulls`` resolves the PR across the fork network
    (it reports the PR's own base repo, whatever that is), and a commit that
    wasn't pushed to this repo won't be in the base repo either — so querying more
    repos for the same commit is redundant.
    """
    rc, br, _ = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    branch = br.strip()
    if rc == 0 and branch and branch != "HEAD":
        rc, remote, _ = _git(["config", f"branch.{branch}.remote"], cwd=root)
        remote = remote.strip()
        if rc == 0 and remote and remote != ".":
            nwo = _remote_nwo(root, remote)
            if nwo:
                return nwo
    return _remote_nwo(root, "origin")


def _resolve_pr_via_commit(root: str, *, token: str | None = None) -> tuple[int, str] | None:
    """Resolve ``(PR number, base owner/repo)`` by pushed-commit identity, or ``None``.

    Branch-name-independent: asks GitHub which PR a pushed commit belongs to
    (``repos/{owner}/{repo}/commits/{sha}/pulls``), so it finds the PR even when a
    stacking tool (git-stack, ``git pp``) pushed under a remote branch name that
    differs from the checkout, or a fork PR whose head ``gh`` can't name. The
    commit lives in the repo it was pushed to (the fork for a fork PR), and the
    response names the PR's own base repo — returned so the caller fetches the PR
    from the right place regardless of the local ``gh repo set-default``.

    Only **open** PRs are accepted. For a commit already on the default branch
    (master/main, or any merged tip) the endpoint returns the *merged* PR that
    introduced it, which is a false positive — never the branch's own outgoing
    PR — so a closed/merged row is skipped rather than surfaced.
    """
    shas = _head_commit_shas(root)
    repo = _commit_lookup_repo(root)
    if not shas or not repo:
        return None
    for sha in shas:
        rc, out, _ = _gh(["api", f"repos/{repo}/commits/{sha}/pulls"], cwd=root, token=token)
        if rc != 0:
            continue
        try:
            rows = json.loads(out)
        except ValueError:
            continue
        if not isinstance(rows, list) or not rows:
            continue

        def _is_open(row: Any) -> bool:
            return isinstance(row, dict) and str(row.get("state", "")).lower() == "open"

        # Accept only an OPEN PR. On the default branch (and any branch whose tip
        # is already merged) this endpoint returns the *merged* PR that introduced
        # the commit — a false positive, since it's not the branch's own outgoing
        # PR. A genuine fallback hit (a fork / renamed branch gh can't name) is
        # always open, so a closed row is never the PR we want; skip it rather
        # than falling back to the first row.
        chosen = next((r for r in rows if _is_open(r)), None)
        if chosen is None:
            continue
        number = chosen.get("number")
        base = chosen.get("base")
        base_repo = base.get("repo") if isinstance(base, dict) else None
        base_full = base_repo.get("full_name") if isinstance(base_repo, dict) else None
        if isinstance(number, int) and isinstance(base_full, str) and "/" in base_full:
            return number, base_full
    return None


def _refused_pr_fields(stderr: str) -> set[str]:
    """Read rejected JSON fields or GraphQL error paths, excluding CLI help catalogs."""
    if match := re.search(r'^Unknown JSON field: "([^"]+)"', stderr, re.MULTILINE):
        return {match[1]}
    return {
        field
        for path in re.findall(r"\(([\w.]+(?:,\s*[\w.]+)*)\)", stderr)
        for field in re.findall(r"\w+", path)
    }


def _gh_pr_view(
    args: list[str], fields: str, *, cwd: str, token: str | None = None
) -> dict[str, Any] | None:
    """Return ``gh pr view <args> --json <fields>`` as a dict, or ``None``.

    Retry without optional checks, reviewer, or merge fields only when the error
    names them. Each retry removes fields, so unrelated failures never retry here.
    """
    rc, out, err = _gh(["pr", "view", *args, "--json", fields], cwd=cwd, token=token)
    while rc != 0:
        refused = _refused_pr_fields(err) & set(fields.split(","))
        refused &= _PR_REVIEW_FIELDS | _PR_MERGE_FIELDS | {_PR_CHECKS_FIELD, _PR_COMMENTS_FIELD}
        if refused & _PR_REVIEW_FIELDS:
            refused.update(_PR_REVIEW_FIELDS)
        remaining = ",".join(field for field in fields.split(",") if field not in refused)
        if not remaining or remaining == fields:
            return None
        fields = remaining
        rc, out, err = _gh(["pr", "view", *args, "--json", fields], cwd=cwd, token=token)
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _checks_fields(data: dict[str, Any]) -> dict[str, Any]:
    """``checks`` and ``checks_supported`` for a ``gh pr view`` object; the field is
    absent only after :func:`_gh_pr_view` dropped it."""
    return {
        "checks": _summarize_checks(data.get(_PR_CHECKS_FIELD)),
        "checks_supported": _PR_CHECKS_FIELD in data,
    }


def _merge_fields(data: dict[str, Any]) -> dict[str, Any]:
    """``merge_state_status`` and ``auto_merge`` for a ``gh pr view`` object.

    Each is left out when :func:`_gh_pr_view` dropped its field for an older ``gh``.
    """
    fields: dict[str, Any] = {}
    if "mergeStateStatus" in data:
        status = data["mergeStateStatus"]
        fields["merge_state_status"] = status.upper() if isinstance(status, str) else None
    if "autoMergeRequest" in data:
        request = data["autoMergeRequest"]
        if isinstance(request, dict):
            method = request.get("mergeMethod")
            enabled_by = request.get("enabledBy")
            login = enabled_by.get("login") if isinstance(enabled_by, dict) else None
            fields["auto_merge"] = {
                "merge_method": method.upper() if isinstance(method, str) else None,
                "enabled_by": login if isinstance(login, str) else None,
                "enabled_at": request.get("enabledAt"),
            }
        else:
            fields["auto_merge"] = None
    return fields


def _pr_view_json(root: str, fields: str, *, token: str | None = None) -> dict[str, Any] | None:
    """Return the branch's PR as a ``gh``-JSON object for ``fields``, or ``None``.

    Two-step, so a decoupled remote branch name never hides the PR:

    1. ``gh pr view --json`` — resolves the current branch's PR against the
       gh-resolved base as the authenticated account. Fast, and covers same-repo
       branches and any push that set upstream tracking (plain ``push -u``, and
       ``git pp``, which pushes ``-u``). ``--json`` also avoids the interactive
       pager and the Projects-classic mis-parse of a bare ``gh pr view``.
    2. Commit-identity fallback — when a stacking tool (git-stack, ``git pp``)
       pushed under a remote branch name that differs from the checkout, or a
       fork PR whose head ``gh`` can't name, the branch-name lookup misses. The
       pushed commit still maps to the PR (:func:`_resolve_pr_via_commit`), which
       also yields the PR's base repo so the full object is fetched with an
       explicit ``-R``. This subsumes the old ``branch.<name>.merge`` head guess.

    :param token: Optional GH_TOKEN to run the calls as the selected account.
    """
    data = _gh_pr_view([], fields, cwd=root, token=token)
    if data is not None:
        return data

    resolved = _resolve_pr_via_commit(root, token=token)
    if resolved is None:
        return None
    number, base_repo = resolved
    # First-run convenience: point gh's default at the PR's real base so the
    # agent's own `gh` in the terminal targets it too. Only when unset, so a
    # manual `gh repo set-default` is never overridden; best-effort — resolution
    # itself doesn't need it (the explicit ``-R`` below carries the base).
    if not _resolved_base_nwo(root):
        _gh(["repo", "set-default", base_repo], cwd=root, token=token)
    return _gh_pr_view([str(number), "-R", base_repo], fields, cwd=root, token=token)


def _workspace_github_info(root: str) -> dict[str, Any]:
    """Resolve GitHub context for the workspace: repo, branch, base, and PR.

    Git-first: a git checkout is the fundamental requirement, so ``available``
    reflects "is a git repo". ``gh`` layers the repo / PR metadata on top;
    ``base_ref`` is the PR's base branch (``None`` when there's no PR, since the
    tab is a pure PR view).

    :param root: Absolute path to the session workspace.
    :returns: A ``session.github.info`` object. ``available`` is false only when
        this isn't a git repo (``reason: not_a_git_repo``). ``gh_available`` /
        ``authenticated`` report whether the ``gh`` CLI is present and signed in;
        ``repo`` / ``pr`` / ``base_ref`` are null without it. ``accounts`` /
        ``selected_account`` are populated only when the repo can't be reached (to
        drive the account selector), so the happy path stays fast. The GitHub
        facet copies these four fields into ``auth``; at the top level they are
        deprecated and will be removed in 0.19.0.

    Ordering is a fast-path optimization: resolve the PR first, and only fall to
    the ``gh repo view`` reachability probe (and, if that fails, the ``gh auth
    status`` account enumeration) when there's no PR. A resolved PR already tells
    us the repo and that we're authenticated, so those extra network calls are
    skipped whenever a PR exists.
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

    # Run the API-touching calls as the workspace's preferred account (local dev);
    # resolving the token is local (config read + keyring), so it's always cheap.
    token = _account_token_for(root)

    # Resolve the PR first. A hit tells us the repo (from the PR URL) and that
    # we're authenticated, so we skip the separate `gh repo view` and the account
    # enumeration entirely — the common case makes the fewest network calls.
    data = _pr_view_json(root, _PR_VIEW_FIELDS, token=token)
    if data is not None:
        author = data.get("author")
        body = data.get("body")
        payload["pr"] = {
            "number": data.get("number"),
            "title": data.get("title"),
            "state": data.get("state"),
            "url": data.get("url"),
            "is_draft": data.get("isDraft", False),
            "author": author.get("login") if isinstance(author, dict) else None,
            "base_ref": data.get("baseRefName"),
            "head_ref": data.get("headRefName"),
            **_checks_fields(data),
            # An empty body is null so the Summary tab shows its empty state.
            "body": body if isinstance(body, str) and body.strip() else None,
            **_comments_payload(data),
            **_merge_fields(data),
        }
        if _PR_REVIEW_FIELDS.issubset(data):
            payload["pr"]["reviewers"] = _shape_reviewers(data)
        if "labels" in data:
            payload["pr"]["labels"] = _shape_labels(data["labels"])
        payload["authenticated"] = True
        payload["base_ref"] = data.get("baseRefName")
        payload["repo"] = {"name_with_owner": _owner_repo_from_pr_url(data.get("url"))}
        return payload

    # No PR. Probe repo reachability to tell "no PR yet" from "can't reach repo".
    rc, out, _ = _gh(["repo", "view", "--json", "nameWithOwner"], cwd=root, token=token)
    if rc == 0:
        try:
            nwo = json.loads(out).get("nameWithOwner")
        except (ValueError, AttributeError):
            nwo = None
        if nwo:
            payload["authenticated"] = True
            payload["repo"] = {"name_with_owner": nwo}
            return payload  # -> no-pr

    # Repo unreachable / not signed in — enumerate accounts so the panel can offer
    # the account selector (the only state where it's shown).
    authenticated, accounts = _list_accounts(root)
    payload["authenticated"] = authenticated
    payload["accounts"] = accounts
    workspace_key = _workspace_key(root)
    pref_login = _config.github_account_preference(workspace_key) if workspace_key else None
    active_login = next((a["login"] for a in accounts if a.get("active")), None)
    payload["selected_account"] = pref_login or active_login
    return payload


def _repo_argument(ref: PullRequestRef) -> str:
    """The ``gh -R`` selector, including a GitHub Enterprise host.

    It is also the key of saved per-PR account preferences, so its format is fixed.
    """
    if ref.provider != "github":
        raise ValueError("This resource supports only GitHub pull requests")
    return f"{ref.host}/{ref.repository}"


def _pr_token(root: str, reference: PullRequestRef) -> str | None:
    if _in_sandbox():
        return None
    login = _config.github_account_preference(_repo_argument(reference))
    if login:
        return _gh_auth_token(root, login, reference.host)
    return None


def _pr_json(root: str, reference: PullRequestRef, fields: str) -> dict[str, Any] | None:
    if reference.host != "github.com":
        _, accounts = _list_accounts(root)
        if reference.host not in {account.get("host") for account in accounts}:
            return None
    return _gh_pr_view(
        [str(reference.number), "-R", _repo_argument(reference)],
        fields,
        cwd=root,
        token=_pr_token(root, reference),
    )


def _reference_info(root: str, reference: PullRequestRef) -> dict[str, Any]:
    info: dict[str, Any] = {
        "object": "session.github.info",
        "available": True,
        "gh_available": shutil.which("gh") is not None,
        "authenticated": False,
        "pr": None,
        "repo": {"name_with_owner": reference.repository},
        "selected_pr_url": reference.url,
    }
    if not info["gh_available"]:
        return info
    _, accounts = _list_accounts(root)
    info["accounts"] = [a for a in accounts if a.get("host") == reference.host]
    preferred = _config.github_account_preference(_repo_argument(reference))
    info["selected_account"] = preferred or next(
        (a["login"] for a in info["accounts"] if a.get("active")), None
    )
    data = _pr_json(root, reference, _PR_VIEW_FIELDS + ",headRefOid,baseRefOid")
    if data is None:
        return info
    author = data.get("author")
    info.update(
        authenticated=True, branch=data.get("headRefName"), base_ref=data.get("baseRefName")
    )
    info["pr"] = {
        "number": reference.number,
        "url": reference.url,
        "title": data.get("title"),
        "state": data.get("state"),
        "is_draft": data.get("isDraft", False),
        "author": author.get("login") if isinstance(author, dict) else None,
        "base_ref": data.get("baseRefName"),
        "head_ref": data.get("headRefName"),
        "head_sha": data.get("headRefOid"),
        "base_sha": data.get("baseRefOid"),
        **_checks_fields(data),
        "body": data.get("body") or None,
        **_comments_payload(data),
        **_merge_fields(data),
    }
    if _PR_REVIEW_FIELDS.issubset(data):
        info["pr"]["reviewers"] = _shape_reviewers(data)
    if "labels" in data:
        info["pr"]["labels"] = _shape_labels(data["labels"])
    return info


def _pr_title(data: dict[str, Any] | None) -> str | None:
    title = data.get("title") if data else None
    if not isinstance(title, str):
        return None
    return title.strip() or None


def _gh_installed() -> bool:
    """Whether the ``gh`` CLI is on ``PATH``."""
    return shutil.which("gh") is not None


def _pr_title_before(
    root: str, reference: PullRequestRef, deadline: float
) -> tuple[str | None, bool]:
    """Look up one PR's title, with every ``gh`` call ending by ``deadline``.

    :param deadline: A ``time.monotonic()`` value.
    :returns: ``(title, timed_out)``; ``timed_out`` is true only when the
        deadline ended the lookup.
    """
    with _gh_deadline_scope(deadline):
        data = _pr_json(root, reference, "title")
        return _pr_title(data), data is None and _gh_timed_out.get()


def _verify_pr_access(root: str, reference: PullRequestRef) -> None:
    """Raise unless ``gh`` on the host can read the PR.

    :raises ValueError: When ``gh`` cannot read it.
    """
    if _pr_json(root, reference, "number,url") is None:
        raise ValueError("Cannot access this pull request using gh on the host")


def _adopt_workspace_account(root: str, reference: PullRequestRef) -> None:
    """Give a branch-inferred PR the workspace's account unless it has its own."""
    key = _workspace_key(root)
    account = _config.github_account_preference(key) if key else None
    if account and not _config.github_account_preference(_repo_argument(reference)):
        _config.set_github_account_preference(_repo_argument(reference), account)


def _set_preference(
    root: str,
    reference: PullRequestRef | None,
    *,
    account: str | None,
    remote: str | None,
) -> None:
    """Save a gh account choice for one PR or the workspace, and the base remote.

    The workspace account preference is keyed by the main worktree path, shared
    across a repo's worktrees, and persisted with
    :func:`omnigent.config.set_github_account_preference`; an empty ``account``
    clears it, falling back to ``gh``'s active account. The remote is
    ``gh repo set-default`` (persisted by ``gh`` in ``.git/config``); the base
    is normally resolved automatically, so this is only an escape hatch.

    :param reference: The PR whose account to set; ``None`` for the workspace.
    :param account: GitHub login to prefer, ``None`` to leave it, or empty to clear it.
    :param remote: Git remote name or ``owner/repo`` to set as the workspace's
        base repo; ignored with a ``reference``.
    """
    if reference is not None:
        if account is not None:
            _config.set_github_account_preference(_repo_argument(reference), account or None)
        return
    if remote:
        _gh(["repo", "set-default", remote], cwd=root)
    if account is not None:
        key = _workspace_key(root)
        if key:
            _config.set_github_account_preference(key, account or None)


def github_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the panel payload; see :func:`pr_resource.pr_info`."""
    return pr_resource.pr_info(root, session_id=session_id, pr_url=pr_url)


def update_session_pr(root: str, session_id: str, url: str, action: str) -> dict[str, Any]:
    """Attach or remove a session PR; see :func:`pr_resource.update_session_pr`."""
    return pr_resource.update_session_pr(root, session_id, url, action)


def set_github_preference(
    root: str,
    *,
    account: str | None = None,
    remote: str | None = None,
    session_id: str | None = None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Apply an account or remote choice; see :func:`pr_resource.set_pr_preference`."""
    return pr_resource.set_pr_preference(
        root, account=account, remote=remote, session_id=session_id, pr_url=pr_url
    )


def resolve_base_ref(root: str, base: str | None) -> str | None:
    """Return an explicit base branch, else the base of the workspace branch's PR.

    :param root: Absolute workspace path.
    :param base: Explicit base branch name, or ``None`` to derive the default.
    :returns: A base branch name, or ``None`` when none can be resolved.
    """
    if base:
        return base
    return _workspace_github_info(root).get("base_ref")


def _git_run(root: str) -> local_git.TextRun:
    """Run git commands in ``root`` through :func:`_git`, for :mod:`local_git`."""

    def run(argv: list[str]) -> tuple[int | None, str]:
        rc, out, _ = _git(argv, cwd=root)
        return rc, out

    return run


def _resolve_diff_base(root: str, base: str) -> str:
    """Resolve a base branch name to the ref to diff HEAD against.

    See :func:`omnigent.runner.git_providers.local_git.resolve_diff_base`.

    :raises OmnigentError: If the base is unavailable or shallow ancestry is missing.
    """
    return local_git.resolve_diff_base(_git_run(root), base)


def _read_diff_content(root: str, ref: str, path: str) -> str | None:
    """Read a local diff side; only a confirmed absent tree entry means no content."""
    return local_git.read_file(_git_run(root), ref, path)


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


def _pr_number(root: str, *, token: str | None = None) -> int | None:
    """Return the PR number for the workspace's branch, or ``None``.

    :param root: Absolute workspace path.
    :param token: Optional GH_TOKEN to run ``gh`` as the selected account.
    :returns: The associated PR's number, or ``None`` when no PR resolves (none
        for the branch, ``gh`` missing, or not authenticated).
    """
    data = _pr_view_json(root, "number", token=token)
    if data is None:
        return None
    number = data.get("number")
    return number if isinstance(number, int) else None


def github_changed_files(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the selected PR's files; see :func:`pr_resource.pr_changed_files`."""
    return pr_resource.pr_changed_files(root, session_id=session_id, pr_url=pr_url)


def _pr_changed_files(root: str, reference: PullRequestRef | None) -> dict[str, Any]:
    """List the PR's changed files, straight from GitHub.

    Sourced from ``gh api .../pulls/<n>/files`` so the set (and each file's
    status / line counts) matches the PR's "Files changed" exactly — never a
    local ``git diff``. Empty when the branch has no PR.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, or ``None`` for the workspace branch's PR.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
    token = _pr_token(root, reference) if reference else _account_token_for(root)
    number = reference.number if reference else _pr_number(root, token=token)
    if number is None:
        return empty
    repository = reference.repository if reference else "{owner}/{repo}"
    host_args = _host_args(root, reference) if reference else []
    rc, out, _ = _gh(
        [
            "api",
            *host_args,
            "--paginate",
            *(["--slurp"] if reference else []),
            f"repos/{repository}/pulls/{number}/files?per_page=100",
        ],
        cwd=root,
        token=token,
    )
    if rc != 0:
        raise ValueError("GitHub could not load the PR's changed files. Refresh to retry.")
    try:
        entries = json.loads(out)
    except ValueError:
        raise ValueError(
            "GitHub returned an invalid changed-file list. Refresh to retry."
        ) from None
    if not isinstance(entries, list):
        raise ValueError("GitHub returned an invalid changed-file list. Refresh to retry.")

    if reference:
        entries = [
            entry for page in entries for entry in (page if isinstance(page, list) else [page])
        ]
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


def github_file_diff(
    root: str,
    base: str,
    path: str,
    *,
    session_id: str | None = None,
    pr_url: str | None = None,
    previous_path: str | None = None,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> dict[str, Any]:
    """Return one file's before/after; see :func:`pr_resource.pr_file_diff`."""
    return pr_resource.pr_file_diff(
        root,
        base,
        path,
        session_id=session_id,
        pr_url=pr_url,
        previous_path=previous_path,
        head_sha=head_sha,
        base_sha=base_sha,
    )


def _pr_file_diff(
    root: str,
    reference: PullRequestRef | None,
    path: str,
    *,
    base: str,
    previous_path: str | None,
    head_sha: str | None,
    base_sha: str | None,
) -> dict[str, Any]:
    """Return before/after content for one file, HEAD vs the base merge-base.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, read from GitHub; ``None`` diffs the local
        checkout.
    :param path: Repo-root-relative path, as returned by
        :func:`github_changed_files`.
    :param base: Base branch name for the local diff, e.g. ``"main"``.
    :returns: A ``session.github.file_diff`` object with ``before`` (merge-base
        content, ``None`` for an added file) and ``after`` (HEAD content,
        ``None`` for a deleted file).
    :raises OmnigentError: If the local diff base or file content is unavailable.
    """
    if reference:
        return _pr_file_contents(
            root,
            reference,
            path,
            previous_path=previous_path,
            head_sha=head_sha,
            base_sha=base_sha,
        )
    resolved = resolve_base_ref(root, base or None)
    diff_base = _resolve_diff_base(root, resolved) if resolved else None

    before = _read_diff_content(root, diff_base, path) if diff_base is not None else None
    after = _read_diff_content(root, "HEAD", path)

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": before,
        "after": after,
    }


def github_pr_diff(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the selected PR's patch; see :func:`pr_resource.pr_diff`."""
    return pr_resource.pr_diff(root, session_id=session_id, pr_url=pr_url)


def _pr_patch(root: str, reference: PullRequestRef | None) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch, straight from GitHub.

    ``gh pr diff <number>`` yields the PR's "Files changed" patch (server-computed
    against the base's merge-base), which the web view parses client-side into
    per-file diffs. The PR is resolved by number first (via :func:`_pr_number`,
    which handles fork / triangular heads a bare ``gh pr diff`` can't); empty when
    the branch has no PR.

    :param root: Absolute workspace path.
    :param reference: The tracked PR, or ``None`` for the workspace branch's PR.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when there's no PR / no changes).
    """
    empty: dict[str, Any] = {"object": "session.github.pr_diff", "patch": ""}
    token = _pr_token(root, reference) if reference else _account_token_for(root)
    number = reference.number if reference else _pr_number(root, token=token)
    if number is None:
        return empty
    if reference:
        _host_args(root, reference)
    repo_args = ["-R", _repo_argument(reference)] if reference else []
    rc, out, _ = _gh(["pr", "diff", str(number), *repo_args], cwd=root, token=token)
    if rc != 0:
        raise ValueError("GitHub could not load the PR diff. Refresh or open the PR on GitHub.")
    return {"object": "session.github.pr_diff", "patch": out}


def _host_args(root: str, reference: PullRequestRef) -> list[str]:
    if reference.host != "github.com":
        _, accounts = _list_accounts(root)
        if reference.host not in {account.get("host") for account in accounts}:
            raise ValueError("Sign in to this GitHub host with gh before viewing its PRs")
    return ["--hostname", reference.host]


def _pr_api(root: str, reference: PullRequestRef, endpoint: str) -> dict[str, Any]:
    rc, out, _ = _gh(
        ["api", *_host_args(root, reference), endpoint],
        cwd=root,
        token=_pr_token(root, reference),
    )
    if rc != 0:
        raise ValueError("GitHub could not load the selected PR's file content")
    try:
        result = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned an unexpected file response") from exc
    if not isinstance(result, dict):
        raise ValueError("GitHub returned an unexpected file response")
    return result


def _api_string(value: object, *keys: str) -> str:
    """Read a required nonempty string from a GitHub API response."""
    for key in keys:
        if not isinstance(value, dict):
            raise ValueError("GitHub returned an unexpected file response")
        value = value.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("GitHub returned an unexpected file response")
    return value


def _pr_file_contents(
    root: str,
    reference: PullRequestRef,
    path: str,
    *,
    previous_path: str | None,
    head_sha: str | None,
    base_sha: str | None,
) -> dict[str, Any]:
    for candidate in (path, previous_path or path):
        if candidate.startswith("/") or any(p in {"", ".."} for p in candidate.split("/")):
            raise ValueError("Invalid repository-relative path")
    pr = _pr_api(root, reference, f"repos/{reference.repository}/pulls/{reference.number}")
    current_head = _api_string(pr, "head", "sha")
    current_base = _api_string(pr, "base", "sha")
    if (head_sha and head_sha != current_head) or (base_sha and base_sha != current_base):
        raise ValueError("The pull request changed; refresh before expanding context")
    head_repo = pr["head"].get("repo")
    if not isinstance(head_repo, dict):
        raise ValueError("The pull request's head repository is no longer available")
    head_repository = _api_string(head_repo, "full_name")
    comparison = _pr_api(
        root,
        reference,
        f"repos/{reference.repository}/compare/{current_base}...{current_head}",
    )
    merge_base = _api_string(comparison, "merge_base_commit", "sha")

    def contents(repository: str, ref: str, filename: str) -> str | None:
        # A missing side is expected for additions/deletions. Other failures stay visible.
        endpoint = f"repos/{repository}/contents/{quote(filename, safe='/')}?ref={quote(ref)}"
        rc, out, err = _gh(
            ["api", *_host_args(root, reference), endpoint],
            cwd=root,
            token=_pr_token(root, reference),
        )
        if rc != 0:
            if "HTTP 404" in err:
                return None
            raise ValueError("GitHub could not load the selected file revision")
        try:
            value = json.loads(out)
            if (
                not isinstance(value, dict)
                or value.get("encoding") != "base64"
                or not isinstance(value.get("content"), str)
            ):
                raise ValueError("Unexpected file content")
            text = base64.b64decode(value["content"]).decode("utf-8")
            if "\x00" in text:
                raise ValueError("Binary content")
            return text
        except (ValueError, UnicodeError) as exc:
            raise ValueError("Expanded context is unavailable for this file") from exc

    return {
        "object": "session.github.file_diff",
        "path": path,
        "before": contents(reference.repository, merge_base, previous_path or path),
        "after": contents(head_repository, current_head, path),
    }


def _repo_labels(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """List the PR repository's labels by name for the label picker.

    :raises ValueError: When ``gh`` cannot list them within :data:`_PR_EDIT_REQUEST_SECONDS`.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        rc, out, _ = _gh(
            [
                "label",
                "list",
                "-R",
                _repo_argument(reference),
                "--json",
                "name,color,description",
                "--limit",
                # One past the cap tells a full list from a truncated one.
                str(_MAX_REPO_LABELS + 1),
            ],
            cwd=root,
            token=_pr_token(root, reference),
        )
    if rc != 0:
        raise ValueError("GitHub could not list this repository's labels. Refresh to retry.")
    try:
        raw = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned an unexpected label list") from exc
    raw = raw if isinstance(raw, list) else []
    labels = sorted(
        _shape_labels(raw[:_MAX_REPO_LABELS]), key=lambda label: str(label["name"]).casefold()
    )
    return {"object": "list", "data": labels, "has_more": len(raw) > _MAX_REPO_LABELS}


def _labels_api(
    root: str, host: list[str], token: str | None, args: list[str], *, missing_ok: bool = False
) -> list[Any] | None:
    """Run one ``gh api`` issue-labels request and return the PR's labels after it.

    :param missing_ok: Return ``None`` when GitHub reports that the PR lacks the
        label being removed.
    :raises ValueError: With a user-facing message when GitHub refuses the request.
    """
    rc, out, err = _gh(["api", *host, *args], cwd=root, token=token)
    try:
        body = json.loads(out) if out.strip() else None
    except ValueError:
        body = None
    if rc == 0 and isinstance(body, list):
        return body
    message = body.get("message") if isinstance(body, dict) else None
    if missing_ok and message == "Label does not exist":
        return None
    if re.search(r"\(HTTP 40[134]\)", err):
        raise ValueError("The GitHub account on the host cannot edit this pull request's labels.")
    if isinstance(message, str) and "(HTTP 422)" in err:
        raise ValueError(f"GitHub rejected the label change: {message}")
    raise ValueError("GitHub could not update the labels. Refresh to retry.")


def _edit_labels(
    root: str, reference: PullRequestRef, *, add: Sequence[str], remove: Sequence[str]
) -> dict[str, Any]:
    """Remove, then add, the PR's labels through GitHub's issue labels API.

    Every ``gh`` call, including the account and host lookups, shares
    :data:`_PR_EDIT_REQUEST_SECONDS`; running out reports that the outcome is unknown.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        try:
            return _apply_label_edit(root, reference, add=add, remove=remove)
        except ValueError:
            if _gh_timed_out.get():
                raise ValueError(_LABEL_EDIT_TIMED_OUT) from None
            raise


def _apply_label_edit(
    root: str, reference: PullRequestRef, *, add: Sequence[str], remove: Sequence[str]
) -> dict[str, Any]:
    """Make the edit's requests; each returns the PR's labels, so the last one is the result.

    A final read supplies the labels when every removed label was already absent.
    """
    host = _host_args(root, reference)
    token = _pr_token(root, reference)
    if _gh_timed_out.get():
        # The selected account's token is unknown, so the edit must not run as another.
        raise ValueError(_LABEL_EDIT_TIMED_OUT)
    endpoint = f"repos/{reference.repository}/issues/{reference.number}/labels"
    labels: list[Any] | None = None
    for name in remove:
        removed = _labels_api(
            root,
            host,
            token,
            ["-X", "DELETE", f"{endpoint}/{quote(name, safe='')}"],
            missing_ok=True,
        )
        labels = removed if removed is not None else labels
    if add:
        fields = [arg for name in add for arg in ("-f", f"labels[]={name}")]
        labels = _labels_api(root, host, token, ["-X", "POST", endpoint, *fields])
    if labels is None:
        labels = _labels_api(root, host, token, [f"{endpoint}?per_page=100"])
    return {"object": PR_LABELS_OBJECT, "labels": _shape_labels(labels)}


def _merge_options(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Return the merge methods the PR's repository allows and the one to preselect.

    The default is the viewer's last-used method, or the repository default,
    when it is allowed; otherwise the first allowed method.

    :raises ValueError: When ``gh`` cannot read the repository's merge settings.
    """
    fields = ",".join(["viewerDefaultMergeMethod", *(field for _, field in _MERGE_METHODS)])
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        rc, out, _ = _gh(
            ["repo", "view", _repo_argument(reference), "--json", fields],
            cwd=root,
            token=_pr_token(root, reference),
        )
    if rc != 0:
        raise ValueError(
            "GitHub could not read this repository's merge settings. Refresh to retry."
        )
    try:
        data = json.loads(out)
    except ValueError as exc:
        raise ValueError("GitHub returned unexpected merge settings") from exc
    if not isinstance(data, dict):
        raise ValueError("GitHub returned unexpected merge settings")
    methods = [method for method, field in _MERGE_METHODS if data.get(field) is True]
    preferred = data.get("viewerDefaultMergeMethod")
    preferred = preferred.lower() if isinstance(preferred, str) else None
    default = preferred if preferred in methods else next(iter(methods), None)
    return {"object": MERGE_OPTIONS_OBJECT, "methods": methods, "default": default}


def _merge_error(stderr: str, *, action: str) -> str:
    """Turn ``gh pr merge``'s error output into a user-facing message.

    :param action: What failed, e.g. ``"merge this pull request"``.
    """
    if "Head branch was modified" in stderr:
        return (
            "The pull request's branch changed since the panel loaded. "
            "Review the new commits, then merge again."
        )
    if re.search(r"\(HTTP 40[13]\)|Resource not accessible|must have (?:write|push)", stderr):
        return f"The GitHub account on the host cannot {action}."
    detail = next((line.strip() for line in stderr.splitlines() if line.strip()), "")
    # gh prefixes GraphQL errors and its failure marks; neither helps the reader.
    detail = re.sub(r"^(?:X |GraphQL: )+", "", detail).strip()[:300]
    if not detail:
        return f"GitHub could not {action}. Refresh to retry."
    return f"GitHub could not {action}: {detail}"


def _run_pr_merge(
    root: str, reference: PullRequestRef, flags: list[str], *, action: str, timed_out: str
) -> None:
    """Run ``gh pr merge <flags>`` on the PR as its selected account.

    Every ``gh`` call shares :data:`_PR_EDIT_REQUEST_SECONDS`. Without a saved
    account, ``gh``'s active account runs it; a saved one without a token refuses.

    :param action: What the command does, for error messages.
    :param timed_out: Message when the deadline leaves the outcome unknown.
    :raises ValueError: With a user-facing message when GitHub refuses.
    """
    with _gh_deadline_scope(time.monotonic() + _PR_EDIT_REQUEST_SECONDS):
        _host_args(root, reference)
        token = _pr_token(root, reference)
        # Without the selected account's token gh would run as another account.
        if _gh_timed_out.get():
            raise ValueError(timed_out)
        login = (
            None if _in_sandbox() else _config.github_account_preference(_repo_argument(reference))
        )
        if login and token is None:
            raise ValueError(
                f"The selected GitHub account {login} isn't signed in on the host. "
                "Sign in with gh or choose another account."
            )
        rc, _, err = _gh(
            ["pr", "merge", str(reference.number), "-R", _repo_argument(reference), *flags],
            cwd=root,
            token=token,
        )
        if rc != 0:
            raise ValueError(
                timed_out if _gh_timed_out.get() else _merge_error(err, action=action)
            )


def _merge_pr(
    root: str, reference: PullRequestRef, *, merge_method: str, expected_head: str
) -> dict[str, Any]:
    """Merge the PR with ``gh pr merge``, only while its head is ``expected_head``.

    ``--match-head-commit`` makes GitHub refuse the merge when the branch moved
    since the panel showed it.

    :raises ValueError: With a user-facing message when GitHub refuses the merge.
    """
    _run_pr_merge(
        root,
        reference,
        [f"--{merge_method}", "--match-head-commit", expected_head],
        action="merge this pull request",
        timed_out=_MERGE_TIMED_OUT,
    )
    return {"object": PR_MERGE_OBJECT, "pr_url": reference.url}


def _disable_auto_merge(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Turn off the PR's auto-merge with ``gh pr merge --disable-auto``.

    :raises ValueError: With a user-facing message when GitHub refuses.
    """
    _run_pr_merge(
        root,
        reference,
        ["--disable-auto"],
        action="disable auto-merge",
        timed_out=_DISABLE_AUTO_MERGE_TIMED_OUT,
    )
    return {"object": PR_MERGE_OBJECT, "pr_url": reference.url}
