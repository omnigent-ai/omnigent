"""GitHub integration for the session workspace, backed by the ``gh`` CLI.

Powers the session PR selector, details, and link/unlink actions. Tracked PRs
use explicit host/repository/number identities. Branch and commit discovery
remains a fallback for sessions without recorded PRs. Files and patches come
from GitHub; recording an association does not modify the remote PR.

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
from typing import Any
from urllib.parse import quote

from filelock import Timeout as FileLockTimeout

from omnigent import config as _config
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.runtime.filesystem_registry import _git_timeout_seconds

_logger = logging.getLogger(__name__)

# ``gh pr view`` / ``gh repo view`` reach the GitHub API, so they get their own,
# slightly more generous timeout than the local ``git`` reads. Overridable via
# ``OMNIGENT_GH_TIMEOUT_SECONDS`` so operators can tune it without a restart.
_DEFAULT_GH_TIMEOUT_SECONDS = 15.0

# Fields requested from ``gh pr view``. Always pass ``--json`` — bare
# ``gh pr view`` opens an interactive/pager view and misbehaves in a
# non-interactive subprocess. ``body`` + ``comments`` feed the Summary tab;
# both are accepted by ``gh pr list`` too, so the fork-fallback path shares them.
_PR_VIEW_FIELDS = (
    "number,title,state,url,isDraft,author,baseRefName,headRefName,statusCheckRollup,body,comments"
)


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
    return _run(["gh", *argv], cwd=cwd, timeout=_gh_timeout_seconds(), env=env)


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


# Cap the comments list so a very chatty PR can't bloat the payload; ``gh``
# returns them oldest-first, so the cap keeps the earliest ``_MAX_COMMENTS``.
_MAX_COMMENTS = 100


def _shape_comments(raw: Any) -> list[dict[str, Any]]:
    """Shape ``gh``'s PR ``comments`` into the Summary tab's comment list.

    Keeps the top-level conversation comments GitHub shows by default: a
    minimized/collapsed comment is dropped (mirroring the PR page). Each entry
    is ``{author, body, created_at, url}``; the list is capped at
    ``_MAX_COMMENTS``.
    """
    shaped: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return shaped
    for comment in raw:
        if not isinstance(comment, dict):
            continue
        if comment.get("isMinimized"):
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
        if len(shaped) >= _MAX_COMMENTS:
            break
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
    rc, out, _ = _gh(["pr", "view", "--json", fields], cwd=root, token=token)
    if rc == 0:
        try:
            data = json.loads(out)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return data

    resolved = _resolve_pr_via_commit(root, token=token)
    if resolved is None:
        return None
    number, base_repo = resolved
    rc, out, _ = _gh(
        ["pr", "view", str(number), "-R", base_repo, "--json", fields], cwd=root, token=token
    )
    if rc != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


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
        drive the account selector), so the happy path stays fast.

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
            "checks": _summarize_checks(data.get("statusCheckRollup")),
            # PR description + conversation comments feed the Summary tab; an
            # empty body is null so the UI shows its "no description" state.
            "body": body if isinstance(body, str) and body.strip() else None,
            "comments": _shape_comments(data.get("comments")),
        }
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


def _selected_pr(session_id: str, pr_url: str) -> PullRequestRef:
    reference = PullRequestRef.from_url(pr_url)
    for entry in SessionPrRegistry(session_id).list():
        if entry.url == reference.url:
            return entry
    raise ValueError("This pull request is not associated with the session")


def _default_pr(session_id: str | None, pr_url: str | None) -> PullRequestRef | None:
    if session_id is None:
        return None
    if pr_url:
        return _selected_pr(session_id, pr_url)
    entries = SessionPrRegistry(session_id).list()
    return entries[0] if entries else None


def _pr_token(root: str, reference: PullRequestRef) -> str | None:
    if _in_sandbox():
        return None
    login = _config.github_account_preference(reference.repo_argument)
    if login:
        return _gh_auth_token(root, login, reference.host)
    return None


def _pr_json(root: str, reference: PullRequestRef, fields: str) -> dict[str, Any] | None:
    if reference.host != "github.com":
        _, accounts = _list_accounts(root)
        if reference.host not in {account.get("host") for account in accounts}:
            return None
    rc, out, _ = _gh(
        ["pr", "view", str(reference.number), "-R", reference.repo_argument, "--json", fields],
        cwd=root,
        token=_pr_token(root, reference),
    )
    if rc != 0:
        return None
    try:
        result = json.loads(out)
    except ValueError:
        return None
    return result if isinstance(result, dict) else None


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
    info["selected_account"] = _config.github_account_preference(reference.repo_argument) or next(
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
        "checks": _summarize_checks(data.get("statusCheckRollup")),
        "body": data.get("body") or None,
        "comments": _shape_comments(data.get("comments")),
    }
    return info


def github_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Read the selected session PR, with branch inference for untracked sessions."""
    if session_id is None:
        return _workspace_github_info(root)
    registry = SessionPrRegistry(session_id)
    entries = registry.list()
    if pr_url:
        info = _reference_info(root, _selected_pr(session_id, pr_url))
    elif entries:
        info = _reference_info(root, entries[0])
    else:
        info = _workspace_github_info(root)
        pr = info.get("pr")
        if isinstance(pr, dict) and isinstance(pr.get("url"), str):
            reference = PullRequestRef.from_url(pr["url"])
            registry.record([reference], relationship="inferred", source="branch")
            entries = registry.list()
            if any(entry.url == reference.url for entry in entries):
                key = _workspace_key(root)
                account = _config.github_account_preference(key) if key else None
                if account and not _config.github_account_preference(reference.repo_argument):
                    _config.set_github_account_preference(reference.repo_argument, account)
                info["selected_pr_url"] = reference.url
            else:
                info["pr"] = None
    info["prs"] = [entry.model_dump() for entry in entries]
    info["tracking_available"] = True
    return info


def update_session_pr(root: str, session_id: str, url: str, action: str) -> dict[str, Any]:
    """Attach a verified PR or persist an explicit exclusion."""
    reference = PullRequestRef.from_url(url)
    registry = SessionPrRegistry(session_id)
    try:
        if action == "attach":
            if _pr_json(root, reference, "number,url") is None:
                raise ValueError("Cannot access this pull request using gh on the host")
            registry.record([reference], relationship="attached", source="user")
            return github_info(root, session_id=session_id, pr_url=reference.url)
        if action == "remove":
            registry.remove(reference.url)
            return github_info(root, session_id=session_id)
    except FileLockTimeout as exc:
        raise ValueError("PR tracking is busy; try again.") from exc
    raise ValueError("Expected attach or remove")


def set_github_preference(
    root: str,
    *,
    account: str | None = None,
    remote: str | None = None,
    session_id: str | None = None,
    pr_url: str | None = None,
) -> dict[str, Any]:
    """Apply an account and/or remote selection, then return refreshed info.

    The account preference is keyed per workspace (the main worktree path, shared
    across a repo's worktrees) and persisted to the user config via
    :func:`omnigent.config.set_github_account_preference`; an empty ``account``
    clears the entry, falling back to ``gh``'s active account. The remote is
    ``gh repo set-default`` (persisted by ``gh`` in ``.git/config``) — the
    normal-case base is auto-resolved, so this is only an escape hatch.

    :param root: Absolute workspace path.
    :param account: GitHub login to prefer for this workspace, or ``None`` to
        leave it unchanged (empty string clears it).
    :param remote: Git remote name or ``owner/repo`` to set as the base repo, or
        ``None`` to leave the base unchanged.
    :returns: The refreshed :func:`github_info` payload.
    """
    if pr_url and session_id:
        reference = _selected_pr(session_id, pr_url)
        if account is not None:
            _config.set_github_account_preference(reference.repo_argument, account or None)
        return github_info(root, session_id=session_id, pr_url=pr_url)
    if remote:
        _gh(["repo", "set-default", remote], cwd=root)
    if account is not None:
        key = _workspace_key(root)
        if key:
            _config.set_github_account_preference(key, account or None)
    return github_info(root, session_id=session_id)


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


def _workspace_pr_reference(root: str, *, token: str | None = None) -> PullRequestRef | None:
    """Return the PR identity for the workspace's branch, or ``None``.

    :param root: Absolute workspace path.
    :param token: Optional GH_TOKEN to run ``gh`` as the selected account.
    :returns: The associated PR's host/repository/number identity, or ``None``
        when no PR resolves (none for the branch, ``gh`` missing, or not
        authenticated).
    """
    data = _pr_view_json(root, "number,url", token=token)
    if data is None:
        return None
    number = data.get("number")
    url = data.get("url")
    if not isinstance(number, int) or not isinstance(url, str):
        return None
    try:
        reference = PullRequestRef.from_url(url)
    except ValueError:
        return None
    return reference if reference.number == number else None


def github_changed_files(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """List the PR's changed files, straight from GitHub.

    Sourced from ``gh api .../pulls/<n>/files`` so the set (and each file's
    status / line counts) matches the PR's "Files changed" exactly — never a
    local ``git diff``. Empty when the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``list`` object whose ``data`` entries carry ``path`` / ``name``
        / ``status`` / ``lines_added`` / ``lines_removed``.
    """
    empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
    tracked_reference = _default_pr(session_id, pr_url)
    token = _pr_token(root, tracked_reference) if tracked_reference else _account_token_for(root)
    reference = tracked_reference or _workspace_pr_reference(root, token=token)
    if reference is None:
        return empty
    host_args = _host_args(root, reference)
    rc, out, _ = _gh(
        [
            "api",
            *host_args,
            "--paginate",
            "--slurp",
            f"repos/{reference.repository}/pulls/{reference.number}/files?per_page=100",
        ],
        cwd=root,
        token=token,
    )
    if rc != 0:
        return empty
    try:
        entries = json.loads(out)
    except ValueError:
        return empty
    if not isinstance(entries, list):
        return empty

    entries = [entry for page in entries for entry in (page if isinstance(page, list) else [page])]
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
    """Return before/after content for one file, HEAD vs the base merge-base.

    :param root: Absolute workspace path.
    :param base: Base branch name, e.g. ``"main"``.
    :param path: Repo-root-relative path, as returned by
        :func:`github_changed_files`.
    :returns: A ``session.github.file_diff`` object with ``before`` (merge-base
        content, ``None`` for an added file) and ``after`` (HEAD content,
        ``None`` for a deleted file).
    """
    reference = _default_pr(session_id, pr_url)
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


def github_pr_diff(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return the whole PR as one unified diff patch, straight from GitHub.

    ``gh pr diff <number>`` yields the PR's "Files changed" patch (server-computed
    against the base's merge-base), which the web view parses client-side into
    per-file diffs. The PR's full host/repository/number identity is resolved
    first, which handles fork / triangular heads a bare ``gh pr diff`` can't;
    empty when the branch has no PR.

    :param root: Absolute workspace path.
    :returns: A ``session.github.pr_diff`` object with the ``patch`` text
        (empty when there's no PR / no changes).
    """
    empty: dict[str, Any] = {"object": "session.github.pr_diff", "patch": ""}
    tracked_reference = _default_pr(session_id, pr_url)
    token = _pr_token(root, tracked_reference) if tracked_reference else _account_token_for(root)
    reference = tracked_reference or _workspace_pr_reference(root, token=token)
    if reference is None:
        return empty
    _host_args(root, reference)
    rc, out, _ = _gh(
        ["pr", "diff", str(reference.number), "-R", reference.repo_argument],
        cwd=root,
        token=token,
    )
    return {"object": "session.github.pr_diff", "patch": out if rc == 0 else ""}


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
