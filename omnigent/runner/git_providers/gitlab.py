"""GitLab MR panel adapter, authenticated by glab on the execution host."""

from __future__ import annotations

import base64
import binascii
import json
import re
import shutil
import subprocess
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from omnigent.git_providers import EnvInstances, ParsedRemote, provider_display
from omnigent.git_providers.gitlab import GitLabProvider
from omnigent.runner.git_providers import (
    CHANGED_FILE_OBJECT,
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    ProviderCapabilities,
    PullRequestFacet,
    gitlab_observer,
    local_git,
)
from omnigent.runner.session_prs import PullRequestRef

if TYPE_CHECKING:
    from omnigent.runner.gitlab_client import GitLabClient

_CAPABILITIES = ProviderCapabilities(False, False, True, True)
_COMMIT = re.compile(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}")
_STATES = {"opened": "OPEN", "merged": "MERGED", "closed": "CLOSED", "locked": "OPEN"}
_BUCKETS = {"success": "passing", "skipped": "passing", "failed": "failing", "canceled": "failing"}


def _object(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _git(root: str, args: list[str]) -> tuple[int | None, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    return result.returncode, result.stdout.decode("utf-8", errors="replace")


def _branch(root: str) -> str | None:
    rc, branch = _git(root, ["branch", "--show-current"])
    return branch.strip() or None if rc == 0 else None


def _remotes(root: str) -> list[ParsedRemote]:
    """Prefer the branch's upstream, then origin and the remaining remotes."""
    remotes = local_git.remote_urls(lambda args: _git(root, args))
    branch = _branch(root)
    _, upstream = _git(root, ["config", f"branch.{branch}.remote"]) if branch else (None, "")
    remotes.sort(key=lambda item: (item[0] != upstream.strip(), item[0] != "origin"))
    found = {}
    for _, url in remotes:
        parsed = GitLabProvider().parse_remote_url(url, EnvInstances())
        if parsed:
            found.setdefault((parsed.host, parsed.repository), parsed)
    return list(found.values())


def _client(root: str, host: str, deadline: float | None = None) -> GitLabClient:
    from omnigent.runner.gitlab_client import GitLabClient

    return GitLabClient(root, host, deadline=deadline)


def _endpoint(reference: PullRequestRef) -> str:
    return f"projects/{quote(reference.repository, safe='')}/merge_requests/{reference.number}"


def _mr(client: GitLabClient, reference: PullRequestRef) -> dict[str, Any]:
    mr = client.object(_endpoint(reference))
    returned = gitlab_observer.pr_from_object(mr)
    if (
        mr.get("iid") != reference.number
        or not isinstance(mr.get("state"), str)
        or mr["state"] not in _STATES
        or returned is None
        or returned.url != reference.url
    ):
        raise ValueError("GitLab returned an invalid merge request identity or state.")
    return mr


def _source_remote(root: str, remotes: list[ParsedRemote]) -> ParsedRemote:
    branch = _branch(root)
    _, push = _git(root, ["config", f"branch.{branch}.pushRemote"])
    if not push.strip():
        _, push = _git(root, ["config", "remote.pushDefault"])
    named = dict(local_git.remote_urls(lambda args: _git(root, args)))
    url = named.get(push.strip()) or named.get("origin")
    source = GitLabProvider().parse_remote_url(url, EnvInstances()) if url else None
    return source or remotes[0]


def _resolved(
    root: str, reference: PullRequestRef | None
) -> tuple[GitLabClient, PullRequestRef, dict[str, Any]] | None:
    if reference is not None:
        client = _client(root, reference.host)
        return client, reference, _mr(client, reference)
    branch, remotes = _branch(root), _remotes(root)
    if not branch or not remotes:
        return None
    source = _source_remote(root, remotes)
    client = _client(root, source.host)
    project = client.object(f"projects/{quote(source.repository, safe='')}")
    source_id = project.get("id")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        raise ValueError("GitLab returned an invalid source project identity.")
    parent = GitLabProvider().parse_remote_url(
        str(_object(project.get("forked_from_project")).get("web_url") or ""), EnvInstances()
    )
    candidates = list(remotes)
    if parent and parent not in candidates:
        candidates.append(parent)
    failure = None
    for repo in candidates:
        if repo.host != source.host:
            continue
        try:
            values, partial = client.pages(
                f"projects/{quote(repo.repository, safe='')}/merge_requests",
                source_branch=branch,
                state="opened",
                scope="all",
            )
            if partial:
                raise ValueError(
                    "GitLab returned an incomplete MR list. Attach the intended MR URL."
                )
            matches = [
                mr
                for mr in values
                if mr.get("source_project_id") == source_id and mr.get("source_branch") == branch
            ]
            if len(matches) > 1:
                raise ValueError(
                    "Several merge requests use this branch. Attach the intended MR URL."
                )
            if not matches:
                continue
            selected = gitlab_observer.pr_from_object(matches[0])
            if (
                selected is None
                or selected.host != repo.host
                or selected.repository != repo.repository
            ):
                raise ValueError("GitLab returned an invalid merge request identity.")
            mr = _mr(client, selected)
            if mr.get("source_project_id") != source_id or mr.get("source_branch") != branch:
                raise ValueError("The merge request changed. Refresh to retry.")
            return client, selected, mr
        except (ValueError, TimeoutError) as exc:
            failure = exc
    if failure is not None:
        raise failure
    return None


def _base_info(root: str, reference: PullRequestRef | None = None) -> dict[str, Any]:
    repo = reference or next(iter(_remotes(root)), None)
    host = repo.host if repo else "gitlab.com"
    return {
        "object": INFO_OBJECT,
        "available": True,
        "provider": "gitlab",
        "provider_display": provider_display("gitlab"),
        "branch": _branch(root),
        "base_ref": None,
        "repo": {"name_with_owner": repo.repository} if repo else None,
        "pr": None,
        "selected_pr_url": reference.url if reference else None,
        "capabilities": _CAPABILITIES.to_json(),
        "warnings": [],
        "auth": {
            "authenticated": False,
            "hint": f"Run GITLAB_HOST={host} glab auth login on the execution host.",
            "cli": {"name": "glab", "available": shutil.which("glab") is not None},
            "accounts": None,
            "selected_account": None,
        },
    }


def _optional_pages(
    client: GitLabClient, path: str, warnings: list[str], label: str, **query: str | int
) -> tuple[list[dict[str, Any]], bool]:
    try:
        values, partial = client.pages(path, **query)
    except (ValueError, TimeoutError):
        values, partial = [], True
    if partial:
        warnings.append(f"GitLab {label} are incomplete. Refresh to retry.")
    return values, partial


def _pr_payload(
    client: GitLabClient, ref: PullRequestRef, mr: dict[str, Any], warnings: list[str]
) -> dict[str, Any]:
    notes, comments_partial = _optional_pages(
        client, _endpoint(ref) + "/notes", warnings, "comments", sort="asc", order_by="created_at"
    )
    comments = [
        {
            "author": _object(note.get("author")).get("username"),
            "author_id": str(_object(note.get("author")).get("id") or "") or None,
            "body": str(note.get("body") or ""),
            "created_at": note.get("created_at"),
            "url": f"{ref.url}#note_{note['id']}",
        }
        for note in notes
        if not note.get("system") and isinstance(note.get("id"), int)
    ]
    runs, partial = [], False
    pipeline = mr.get("head_pipeline")
    if isinstance(pipeline, dict) and isinstance(pipeline.get("id"), int):
        project_id = pipeline.get("project_id") or mr.get("source_project_id")
        if not isinstance(project_id, int):
            partial = True
            warnings.append("GitLab pipeline project is unavailable; job details are incomplete.")
        else:
            prefix = f"projects/{project_id}/pipelines/{pipeline['id']}"
            jobs, partial = _optional_pages(
                client, prefix + "/jobs", warnings, "checks", include_retried="false"
            )
            bridges, bridges_partial = _optional_pages(
                client, prefix + "/bridges", warnings, "downstream checks"
            )
            partial |= bridges_partial
            for job in [*jobs, *bridges]:
                downstream = _object(job.get("downstream_pipeline"))
                status = job.get("status")
                bucket = _BUCKETS.get(str(status), "pending")
                if job.get("allow_failure") and (bucket == "failing" or status == "manual"):
                    bucket = "passing"
                runs.append(
                    {
                        "name": str(job.get("name") or "Job"),
                        "bucket": bucket,
                        "url": job.get("web_url") or downstream.get("web_url"),
                    }
                )
        if not runs:
            runs.append(
                {
                    "name": f"Pipeline #{pipeline['id']}",
                    "bucket": _BUCKETS.get(str(pipeline.get("status")), "pending"),
                    "url": pipeline.get("web_url"),
                }
            )
    counts = {
        bucket: sum(run["bucket"] == bucket for run in runs)
        for bucket in ("passing", "failing", "pending")
    }
    refs = _object(mr.get("diff_refs"))
    author = _object(mr.get("author"))
    return {
        "number": ref.number,
        "url": ref.url,
        "title": str(mr.get("title") or ""),
        "state": _STATES.get(str(mr.get("state")), "OPEN"),
        "is_draft": bool(mr.get("draft") or mr.get("work_in_progress")),
        "author": author.get("username"),
        "author_id": str(author.get("id") or "") or None,
        "base_ref": mr.get("target_branch"),
        "head_ref": mr.get("source_branch"),
        "head_sha": refs.get("head_sha") or mr.get("sha"),
        "base_sha": refs.get("base_sha"),
        "body": mr.get("description"),
        "comments": comments,
        "comments_partial": comments_partial,
        "checks": {**counts, "total": len(runs), "runs": runs, "partial": partial},
    }


def _info(root: str, reference: PullRequestRef | None = None) -> dict[str, Any]:
    info = _base_info(root, reference)
    if reference is None and _git(root, ["rev-parse", "--show-toplevel"])[0] != 0:
        return {**info, "available": False, "reason": "not_a_git_repo"}
    if not info["auth"]["cli"]["available"]:
        return info
    try:
        resolved = _resolved(root, reference)
        info["auth"]["authenticated"] = True
        info["auth"]["hint"] = None
        if resolved:
            client, ref, mr = resolved
            info.update(repo={"name_with_owner": ref.repository}, selected_pr_url=ref.url)
            info["pr"] = _pr_payload(client, ref, mr, info["warnings"])
            info["base_ref"] = info["pr"]["base_ref"]
    except (ValueError, TimeoutError) as exc:
        info["auth"]["hint"] = str(exc)
        info["warnings"].append(str(exc))
    return info


def _changes(
    client: GitLabClient, ref: PullRequestRef, mr: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    changes, partial = client.pages(_endpoint(ref) + "/diffs")
    count = str(mr.get("changes_count") or "")
    partial |= count.endswith("+") or (count.isdigit() and int(count) > len(changes))
    if any(
        not isinstance(c.get("old_path"), str) or not isinstance(c.get("new_path"), str)
        for c in changes
    ):
        raise ValueError("GitLab returned an invalid changed-file response.")
    return changes, partial


def _file_status(change: dict[str, Any]) -> str:
    return (
        "renamed"
        if change.get("renamed_file")
        else "created"
        if change.get("new_file")
        else "deleted"
        if change.get("deleted_file")
        else "modified"
    )


def _patch_path(prefix: str, path: str) -> str:
    name = prefix + path
    return (
        json.dumps(name, ensure_ascii=False)
        if any(c.isspace() or c in '\\"' for c in name)
        else name
    )


def _content(client: GitLabClient, project: object, path: str, revision: str) -> str:
    if not isinstance(project, int) or isinstance(project, bool) or project <= 0:
        raise ValueError("The merge request's source or target project is unavailable.")
    payload = client.object(
        f"projects/{project}/repository/files/{quote(path, safe='')}", ref=revision
    )
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        raise ValueError("GitLab returned an invalid file response.")
    try:
        return base64.b64decode(payload["content"].replace("\n", ""), validate=True).decode(
            "utf-8"
        )
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError(
            "Expanded context is unavailable for this binary or invalid file."
        ) from exc


class GitLabPullRequests:
    """Normalize GitLab metadata and exact MR revisions to the shared PR contract."""

    capabilities = _CAPABILITIES
    workspace_info = staticmethod(_info)
    reference_info = staticmethod(_info)
    shell_pr_operations = staticmethod(gitlab_observer.shell_pr_operations)
    pr_from_object = staticmethod(gitlab_observer.pr_from_object)
    mcp_prs = staticmethod(gitlab_observer.mcp_prs)

    def titles_available(self, root: str) -> bool:
        del root
        return shutil.which("glab") is not None

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        try:
            title = _mr(_client(root, reference.host, deadline), reference).get("title")
            return (title.strip() or None if isinstance(title, str) else None), False
        except TimeoutError:
            return None, True
        except ValueError:
            return None, False

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        _mr(_client(root, reference.host), reference)

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        pass

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        resolved = _resolved(root, reference)
        if resolved is None:
            return {"object": "list", "data": [], "has_more": False}
        changes, partial = _changes(*resolved)
        data, limited = [], False
        for change in changes:
            patch = change.get("diff")
            unavailable = (
                change.get("too_large")
                or change.get("collapsed")
                or not isinstance(patch, str)
                or not patch
            )
            limited |= bool(unavailable)
            lines = patch.splitlines() if not unavailable else []
            data.append(
                {
                    "object": CHANGED_FILE_OBJECT,
                    "path": change["new_path"],
                    "name": change["new_path"],
                    "previous_path": change["old_path"] if change.get("renamed_file") else None,
                    "status": _file_status(change),
                    "lines_added": None
                    if unavailable
                    else sum(line.startswith("+") for line in lines),
                    "lines_removed": None
                    if unavailable
                    else sum(line.startswith("-") for line in lines),
                }
            )
        return {
            "object": "list",
            "data": data,
            "has_more": partial,
            "warning": "GitLab returned incomplete file changes or omitted large/binary patches."
            if partial or limited
            else None,
        }

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        resolved = _resolved(root, reference)
        if resolved is None:
            return {"object": PR_DIFF_OBJECT, "patch": ""}
        changes, partial = _changes(*resolved)
        if partial:
            return {
                "object": PR_DIFF_OBJECT,
                "patch": "",
                "unavailable_reason": "incomplete_diff",
                "message": "GitLab returned an incomplete diff. "
                "View the merge request on GitLab for all changes.",
            }
        patches = []
        for change in changes:
            patch = change.get("diff")
            if (
                change.get("too_large")
                or change.get("collapsed")
                or not isinstance(patch, str)
                or not patch
            ):
                return {
                    "object": PR_DIFF_OBJECT,
                    "patch": "",
                    "unavailable_reason": "patch_unavailable",
                    "message": "GitLab omitted a large, binary or metadata-only patch. "
                    "View the full diff on GitLab.",
                }
            old, new = _patch_path("a/", change["old_path"]), _patch_path("b/", change["new_path"])
            before, after = (
                ("/dev/null" if change.get("new_file") else old),
                ("/dev/null" if change.get("deleted_file") else new),
            )
            patches.append(
                f"diff --git {old} {new}\n--- {before}\n+++ {after}\n" + patch.rstrip("\n") + "\n"
            )
        return {"object": PR_DIFF_OBJECT, "patch": "".join(patches)}

    def file_diff(
        self,
        root: str,
        reference: PullRequestRef | None,
        path: str,
        *,
        base: str,
        previous_path: str | None,
        head_sha: str | None,
        base_sha: str | None,
    ) -> dict[str, Any]:
        for value in (path, previous_path):
            if value is not None and (
                not value
                or value.startswith("/")
                or any(p in {".", ".."} for p in value.split("/"))
                or "\0" in value
            ):
                raise ValueError("Invalid repository-relative file path.")
        resolved = _resolved(root, reference)
        if resolved is None:
            if base:

                def run(args: list[str]) -> tuple[int | None, str]:
                    return _git(root, args)

                revision = local_git.resolve_diff_base(run, base)
                return {
                    "object": FILE_DIFF_OBJECT,
                    "path": path,
                    "before": local_git.read_file(run, revision, previous_path or path),
                    "after": local_git.read_file(run, "HEAD", path),
                }
            raise ValueError("No merge request is available for expanded context.")
        client, ref, mr = resolved
        revisions = _object(mr.get("diff_refs"))
        head, merge_base = revisions.get("head_sha"), revisions.get("base_sha")
        if (
            not isinstance(head, str)
            or not isinstance(merge_base, str)
            or not _COMMIT.fullmatch(head)
            or not _COMMIT.fullmatch(merge_base)
        ):
            raise ValueError(
                "GitLab has not prepared the merge request's diff revisions. Refresh to retry."
            )
        if (head_sha is not None and head_sha != head) or (
            base_sha is not None and base_sha != merge_base
        ):
            raise ValueError("The merge request changed. Refresh before expanding file context.")
        changes, _ = _changes(client, ref, mr)
        change = next((c for c in changes if c["new_path"] == path), None)
        if change is None or (previous_path is not None and previous_path != change["old_path"]):
            raise ValueError(
                "This file is not in the current merge request diff. Refresh to retry."
            )
        before = (
            None
            if change.get("new_file")
            else _content(client, mr.get("target_project_id"), change["old_path"], merge_base)
        )
        after = (
            None
            if change.get("deleted_file")
            else _content(client, mr.get("source_project_id"), path, head)
        )
        return {"object": FILE_DIFF_OBJECT, "path": path, "before": before, "after": after}

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        del root, reference
        if account is not None or remote is not None:
            raise ValueError("Use glab on the host to select a GitLab account or remote.")


PULL_REQUESTS: PullRequestFacet = GitLabPullRequests()
