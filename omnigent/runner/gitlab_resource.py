"""Session GitLab merge-request resources backed by the ``glab`` CLI."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any
from urllib.parse import urlparse

from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.runtime.filesystem_registry import _git_timeout_seconds


def _run(argv: list[str], *, root: str) -> tuple[int | None, str, str]:
    try:
        completed = subprocess.run(
            argv, cwd=root, capture_output=True, timeout=_git_timeout_seconds()
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, "", str(exc)
    return (
        completed.returncode,
        completed.stdout.decode("utf-8", errors="replace"),
        completed.stderr.decode("utf-8", errors="replace"),
    )


def _git(args: list[str], *, root: str) -> tuple[int | None, str, str]:
    return _run(["git", *args], root=root)


def _reference(pr_url: str) -> PullRequestRef:
    reference = PullRequestRef.from_url(pr_url)
    if reference.provider != "gitlab":
        raise ValueError("Expected a GitLab merge request URL")
    return reference


def _selected(session_id: str | None, pr_url: str | None) -> PullRequestRef | None:
    if pr_url:
        return _reference(pr_url)
    if not session_id:
        return None
    return next(
        (entry for entry in SessionPrRegistry(session_id).list() if entry.provider == "gitlab"),
        None,
    )


def _glab_args(reference: PullRequestRef, *args: str) -> list[str]:
    return ["glab", "mr", *args, "-R", f"{reference.host}/{reference.repository}"]


def _remote_reference(root: str, remote: str) -> tuple[PullRequestRef | None, str | None]:
    """Return the GitLab host/project encoded by a named git remote."""
    rc, remote_url, _ = _git(["remote", "get-url", remote], root=root)
    if rc != 0 or not remote_url.strip():
        return None, None
    remote_url = remote_url.strip()
    scp = re.match(r"^(?:[^@]+@)?(?P<host>[^:]+):(?P<path>.+)$", remote_url)
    if scp and "://" not in remote_url:
        host = scp.group("host")
        path = scp.group("path")
    else:
        parsed = urlparse(remote_url)
        host = parsed.hostname or ""
        path = parsed.path
    repository = path.removesuffix(".git").strip("/")
    if not host or not repository or "/" not in repository:
        return None, remote_url
    url = f"https://{host}/{repository}/-/merge_requests/1"
    try:
        return _reference(url), remote_url
    except ValueError:
        return None, remote_url


def _workspace_remotes(root: str, branch: str | None) -> list[str]:
    """Return candidate remotes, preferring the branch's upstream then origin."""
    preferred: list[str] = []
    if branch:
        rc, upstream, _ = _git(["config", f"branch.{branch}.remote"], root=root)
        if rc == 0 and upstream.strip() and upstream.strip() != ".":
            preferred.append(upstream.strip())
    rc, names, _ = _git(["remote"], root=root)
    if rc == 0:
        preferred.extend(name.strip() for name in names.splitlines() if name.strip())
    ordered = [*preferred[:1], "origin", *preferred[1:]]
    return list(dict.fromkeys(name for name in ordered if name))


def _workspace_references(root: str, branch: str | None) -> list[tuple[PullRequestRef, str]]:
    references: list[tuple[PullRequestRef, str]] = []
    seen: set[tuple[str, str]] = set()
    for remote in _workspace_remotes(root, branch):
        reference, remote_url = _remote_reference(root, remote)
        if reference is None or remote_url is None:
            continue
        identity = (reference.host.lower(), reference.repository.lower())
        if identity in seen:
            continue
        seen.add(identity)
        references.append((reference, remote_url))
    return references


def _workspace_context(root: str) -> dict[str, Any]:
    rc, _, _ = _git(["rev-parse", "--show-toplevel"], root=root)
    if rc != 0:
        return {"object": "session.gitlab.info", "available": False, "reason": "not_a_git_repo"}

    _, branch_output, _ = _git(["branch", "--show-current"], root=root)
    branch = branch_output.strip() or None
    references = _workspace_references(root, branch)
    context: dict[str, Any] = {
        "object": "session.gitlab.info",
        "available": True,
        "provider": "gitlab",
        "branch": branch,
        "glab_available": shutil.which("glab") is not None,
        "authenticated": False,
        "repo": None,
        "repos": [],
        "merge_request": None,
        "merge_requests": [],
        "selected_mr_url": None,
    }
    if not references:
        context["reason"] = "repo_unresolved"
        return context

    context["repos"] = [
        {
            "host": reference.host,
            "path_with_namespace": reference.repository,
            "remote_url": remote_url,
        }
        for reference, remote_url in references
    ]
    context["repo"] = context["repos"][0]
    if not context["glab_available"]:
        return context

    authenticated_hosts: dict[str, bool] = {}
    merge_requests: list[dict[str, Any]] = []
    selected_authenticated_repo = False
    for reference, remote_url in references:
        authenticated = authenticated_hosts.get(reference.host)
        if authenticated is None:
            auth_rc, _, _ = _run(
                ["glab", "auth", "status", "--hostname", reference.host], root=root
            )
            authenticated = auth_rc == 0
            authenticated_hosts[reference.host] = authenticated
        if not authenticated:
            continue
        context["authenticated"] = True
        if not selected_authenticated_repo:
            selected_authenticated_repo = True
            context["repo"] = {
                "host": reference.host,
                "path_with_namespace": reference.repository,
                "remote_url": remote_url,
            }
        rc, output, _ = _run(
            _glab_args(reference, "view", "--output", "json"),
            root=root,
        )
        if rc != 0:
            continue
        try:
            mr = json.loads(output)
        except ValueError:
            continue
        if not isinstance(mr, dict):
            continue
        web_url = mr.get("web_url")
        if not isinstance(web_url, str) or any(
            existing.get("web_url") == web_url for existing in merge_requests
        ):
            continue
        merge_requests.append(mr)

    context["merge_requests"] = merge_requests
    if merge_requests:
        context["merge_request"] = merge_requests[0]
        context["selected_mr_url"] = merge_requests[0]["web_url"]
    return context


def gitlab_info(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, Any]:
    """Return workspace GitLab context and selected merge-request details."""
    reference = _selected(session_id, pr_url)
    if reference is None:
        context = _workspace_context(root)
        if session_id:
            context["tracked_merge_requests"] = [
                entry.model_dump()
                for entry in SessionPrRegistry(session_id).list()
                if entry.provider == "gitlab"
            ]
        return context

    # A tracked/selected MR must not hide the live checkout branch. The composer
    # uses this workspace value when the host-wide worktree probe is unavailable.
    branch_rc, branch_output, _ = _git(["branch", "--show-current"], root=root)
    branch = branch_output.strip() if branch_rc == 0 and branch_output.strip() else None
    if shutil.which("glab") is None:
        return {
            "object": "session.gitlab.info",
            "available": True,
            "provider": "gitlab",
            "branch": branch,
            "glab_available": False,
            "authenticated": False,
            "selected_mr_url": reference.url,
            "merge_request": None,
        }
    rc, output, error = _run(
        _glab_args(reference, "view", str(reference.number), "--output", "json"), root=root
    )
    if rc != 0:
        return {
            "object": "session.gitlab.info",
            "available": False,
            "reason": "unavailable",
            "message": error.strip() or "GitLab could not load the merge request.",
            "branch": branch,
        }
    try:
        mr = json.loads(output)
    except ValueError:
        return {
            "object": "session.gitlab.info",
            "available": False,
            "reason": "invalid_response",
            "branch": branch,
        }
    if not isinstance(mr, dict):
        return {
            "object": "session.gitlab.info",
            "available": False,
            "reason": "invalid_response",
            "branch": branch,
        }
    return {
        "object": "session.gitlab.info",
        "available": True,
        "provider": "gitlab",
        "branch": branch,
        "glab_available": True,
        "authenticated": True,
        "repo": {"host": reference.host, "path_with_namespace": reference.repository},
        "selected_mr_url": reference.url,
        "merge_request": mr,
        "tracked_merge_requests": [
            entry.model_dump()
            for entry in SessionPrRegistry(session_id).list()
            if entry.provider == "gitlab"
        ]
        if session_id
        else [],
    }


def gitlab_mr_diff(
    root: str, *, session_id: str | None = None, pr_url: str | None = None
) -> dict[str, str]:
    """Return the server-computed GitLab MR patch for the selected association."""
    reference = _selected(session_id, pr_url)
    if reference is None or shutil.which("glab") is None:
        return {"object": "session.gitlab.mr_diff", "patch": ""}
    rc, output, _ = _run(_glab_args(reference, "diff", str(reference.number)), root=root)
    return {"object": "session.gitlab.mr_diff", "patch": output if rc == 0 else ""}


def update_session_mr(session_id: str, *, url: str, action: str = "attach") -> dict[str, object]:
    """Attach or unlink a GitLab MR from the session's durable association list."""
    reference = _reference(url)
    registry = SessionPrRegistry(session_id)
    if action == "detach":
        registry.remove(reference.url)
    elif action == "attach":
        registry.record([reference], relationship="attached", source="resource")
    else:
        raise ValueError("Expected action 'attach' or 'detach'")
    return {
        "object": "session.gitlab.mr_association",
        "url": reference.url,
        "action": action,
        "tracked_merge_requests": [entry.model_dump() for entry in registry.list()],
    }
