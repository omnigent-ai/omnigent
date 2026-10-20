"""Bounded PR panel snapshots stored separately from session list metadata."""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from omnigent.db.db_models import current_workspace_id
from omnigent.stores.conversation_store.session_prs import validate_session_pr_revision

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.stores.artifact_store import ArtifactStore
    from omnigent.stores.conversation_store import ConversationStore

MAX_PR_INFO_BYTES = 512 * 1024
MAX_PR_CHANGES_BYTES = 512 * 1024
MAX_PR_DIFF_BYTES = 5 * 1024 * 1024
PR_SNAPSHOT_KINDS = frozenset({"info", "changes", "diff"})
_logger = logging.getLogger(__name__)


def _encoded(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _text(value: object, limit: int = 4096) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _link(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 8192:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(parsed.query)
            if key in ("buildId", "discussionId") and value.isdecimal()
        ]
    )
    return urlunsplit(parsed._replace(query=query))


def _count(value: object) -> int:
    return max(0, min(value, 1_000_000_000)) if type(value) is int else 0


def _unavailable(kind: str, reason: str) -> dict[str, Any]:
    if kind == "info":
        result: dict[str, Any] = {"object": "session.github.info", "pr": None}
    elif kind == "changes":
        result = {"object": "list", "data": [], "has_more": False, "available": False}
    else:
        result = {"object": "session.github.pr_diff", "patch": ""}
    return {**result, "unavailable_reason": reason}


def sanitize_pr_snapshot(kind: str, payload: object, pr_url: str) -> dict[str, Any]:
    """Keep panel fields only, without persisting host credentials or account configuration."""
    if kind not in PR_SNAPSHOT_KINDS or not isinstance(payload, dict):
        raise ValueError("Expected a PR info, changes, or diff snapshot")
    if payload.get("unavailable_reason") == "too_large":
        return _unavailable(kind, "too_large")
    if kind == "diff":
        patch = payload.get("patch")
        if not isinstance(patch, str):
            raise ValueError("PR diff snapshot requires a patch string")
        result: dict[str, Any] = {"object": "session.github.pr_diff", "patch": patch}
        for key in ("unavailable_reason", "message"):
            if isinstance(payload.get(key), str):
                result[key] = _text(payload[key])
        return (
            result
            if len(_encoded(result)) <= MAX_PR_DIFF_BYTES
            else _unavailable(kind, "too_large")
        )
    if kind == "changes":
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise ValueError("PR changes snapshot requires a file list")
        if len(entries) > 2000:
            return _unavailable(kind, "too_large")
        files = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                raise ValueError("PR changed files require a path")
            if not entry["path"] or len(entry["path"]) > 4096:
                raise ValueError("PR changed files require a bounded nonempty path")
            shaped: dict[str, Any] = {
                "object": "session.github.changed_file",
                **{
                    key: _text(entry[key])
                    for key in ("path", "name", "status", "previous_path")
                    if isinstance(entry.get(key), str)
                },
            }
            shaped["name"] = _text(entry.get("name")) or entry["path"].split("/")[-1]
            status = entry.get("status")
            shaped["status"] = (
                status if status in ("created", "modified", "deleted", "renamed") else "modified"
            )
            for key in ("lines_added", "lines_removed"):
                shaped[key] = _count(entry[key]) if type(entry.get(key)) is int else None
            files.append(shaped)
        result = {"object": "list", "data": files, "has_more": bool(payload.get("has_more"))}
        if payload.get("warning") or payload.get("warnings"):
            result["warning"] = "Some changed files were unavailable when this snapshot was saved."
        return (
            result
            if len(_encoded(result)) <= MAX_PR_CHANGES_BYTES
            else _unavailable(kind, "too_large")
        )

    raw_pr = payload.get("pr")
    if not isinstance(raw_pr, dict) or raw_pr.get("url") != pr_url:
        raise ValueError("PR info snapshot must describe the associated pull request")
    pr: dict[str, Any] = {
        "url": pr_url,
        "number": _count(raw_pr.get("number")),
        "is_draft": raw_pr.get("is_draft") is True,
        **{
            key: _text(raw_pr.get(key))
            for key in (
                "title",
                "state",
                "author",
                "author_id",
                "base_ref",
                "head_ref",
                "head_sha",
                "base_sha",
            )
        },
    }
    if pr["number"] <= 0:
        raise ValueError("PR info snapshot requires a positive pull request number")
    pr["title"] = pr["title"] or f"Pull request #{pr['number']}"
    pr["state"] = pr["state"] or "UNKNOWN"
    body = raw_pr.get("body")
    pr["body"] = _text(body, 128 * 1024)
    if (isinstance(body, str) and pr["body"] != body) or raw_pr.get("body_partial"):
        pr["body_partial"] = True
    checks = raw_pr.get("checks")
    checks = checks if isinstance(checks, dict) else {}
    runs = checks.get("runs")
    runs = runs if isinstance(runs, list) else []
    pr["checks"] = {
        **{key: _count(checks.get(key)) for key in ("passing", "failing", "pending", "total")},
        "runs": [
            {
                "name": _text(run.get("name")) or "Check",
                "bucket": run.get("bucket"),
                "url": _link(run.get("url")),
            }
            for run in runs[:200]
            if isinstance(run, dict) and run.get("bucket") in ("passing", "failing", "pending")
        ],
    }
    if checks.get("partial") or len(runs) > 200:
        pr["checks"]["partial"] = True
    if type(raw_pr.get("checks_supported")) is bool:
        pr["checks_supported"] = raw_pr["checks_supported"]
    reviewers = raw_pr.get("reviewers")
    if isinstance(reviewers, list):
        pr["reviewers"] = [
            {key: _text(reviewer.get(key)) for key in ("login", "kind", "state")}
            for reviewer in reviewers[:100]
            if isinstance(reviewer, dict)
            and isinstance(reviewer.get("login"), str)
            and reviewer["login"]
            and reviewer.get("kind") in ("user", "team")
            and reviewer.get("state")
            in ("PENDING", "APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED")
        ]
    comments = raw_pr.get("comments")
    comments = comments if isinstance(comments, list) else []
    pr["comments"] = [
        {
            **{key: _text(comment.get(key)) for key in ("author", "author_id", "created_at")},
            "body": _text(comment.get("body"), 64 * 1024) or "",
            "url": _link(comment.get("url")),
        }
        for comment in comments[-100:]
        if isinstance(comment, dict)
    ]
    if (
        raw_pr.get("comments_partial")
        or len(comments) > 100
        or any(
            isinstance(comment, dict)
            and isinstance(comment.get("body"), str)
            and len(comment["body"]) > 64 * 1024
            for comment in comments
        )
    ):
        pr["comments_partial"] = True
    repo = payload.get("repo")
    result = {
        "object": "session.github.info",
        "selected_pr_url": pr_url,
        "pr": pr,
        "repo": {"name_with_owner": _text(repo.get("name_with_owner"))}
        if isinstance(repo, dict)
        else None,
        **{key: _text(payload.get(key)) for key in ("provider", "branch", "base_ref")},
    }
    capabilities = payload.get("capabilities")
    if isinstance(capabilities, dict):
        result["capabilities"] = {
            key: capabilities.get(key) is True
            for key in (
                "account_switching",
                "base_remote_selection",
                "line_counts",
                "linked_pr_diff",
            )
        }
    while len(_encoded(result)) > MAX_PR_INFO_BYTES and pr["comments"]:
        pr["comments"].pop(0)
        pr["comments_partial"] = True
    while len(_encoded(result)) > MAX_PR_INFO_BYTES and pr["body"]:
        pr["body"] = pr["body"][: len(pr["body"]) // 2]
        pr["body_partial"] = True
    return (
        result if len(_encoded(result)) <= MAX_PR_INFO_BYTES else _unavailable(kind, "too_large")
    )


def delete_pr_snapshot_blobs(artifact_store: ArtifactStore | None, keys: list[str]) -> None:
    """Clean replaced snapshots without making a completed metadata update fail."""
    if artifact_store is None:
        return
    for key in keys:
        try:
            artifact_store.delete(key)
        except Exception:  # noqa: BLE001 — deletion remains best effort after metadata commits
            _logger.warning("Could not delete PR snapshot %s", key, exc_info=True)


def save_session_pr_snapshots(
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore | None,
    session_id: str,
    pr_url: str,
    captured_at_ns: int,
    snapshots: dict[str, Any],
) -> bool:
    """Write immutable blobs before atomically publishing newer per-resource references."""
    validate_session_pr_revision(captured_at_ns)
    if (
        not isinstance(pr_url, str)
        or not pr_url
        or not snapshots
        or snapshots.keys() - PR_SNAPSHOT_KINDS
    ):
        raise ValueError("PR snapshot requires an associated URL and info, changes, or diff")
    prepared = {
        kind: sanitize_pr_snapshot(kind, payload, pr_url) for kind, payload in snapshots.items()
    }
    if artifact_store is None:
        return False
    scope = hashlib.sha256(
        f"{current_workspace_id()}\0{session_id}\0{pr_url}".encode()
    ).hexdigest()
    keys: dict[str, str] = {}
    try:
        for kind, payload in prepared.items():
            key = f"session-prs/{scope}/{uuid.uuid4().hex}.json"
            keys[kind] = key
            artifact_store.put(key, _encoded(payload))
        info_pr = prepared.get("info", {}).get("pr") or {}
        saved, obsolete = conversation_store.set_session_pr_snapshot(
            session_id,
            pr_url,
            captured_at_ns,
            keys,
            head_sha=info_pr.get("head_sha"),
            base_sha=info_pr.get("base_sha"),
        )
    except Exception:
        delete_pr_snapshot_blobs(artifact_store, list(keys.values()))
        raise
    delete_pr_snapshot_blobs(artifact_store, obsolete)
    return saved


def prune_session_pr_snapshots(
    conversation_store: ConversationStore, artifact_store: ArtifactStore | None, session_id: str
) -> None:
    if artifact_store is not None:
        delete_pr_snapshot_blobs(
            artifact_store, conversation_store.prune_session_pr_snapshots(session_id)
        )


def session_pr_snapshot_keys(conv: Conversation) -> list[str]:
    return [
        entry["key"]
        for resources in conv.session_pr_snapshots.values()
        for entry in resources.values()
    ]


def load_session_pr_snapshot(
    artifact_store: ArtifactStore | None, conv: Conversation, pr_url: str, kind: str
) -> dict[str, Any]:
    """Read only the server-owned reference for an associated PR after route authorization."""
    if not any(pr["url"] == pr_url for pr in conv.session_pull_requests or []):
        raise ValueError("This pull request is not associated with the session")
    entry = conv.session_pr_snapshots.get(pr_url, {}).get(kind)
    if artifact_store is not None and entry is not None:
        try:
            payload = json.loads(artifact_store.get(entry["key"]))
            if not isinstance(payload, dict):
                raise ValueError("Invalid saved PR resource")
            return {
                **payload,
                "cached": True,
                "cached_at": entry["captured_at_ns"] / 1_000_000_000,
            }
        except Exception:  # noqa: BLE001 — missing or unreadable cache does not break the PR panel
            _logger.warning(
                "Could not load saved PR %s for session %s", kind, conv.id, exc_info=True
            )
    return {**_unavailable(kind, "not_synced"), "cached": True}
