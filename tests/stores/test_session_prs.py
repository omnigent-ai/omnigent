import json
import time

import pytest

from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.session_prs import validate_session_prs
from omnigent.stores.session_pr_snapshots import (
    MAX_PR_DIFF_BYTES,
    MAX_PR_INFO_BYTES,
    load_session_pr_snapshot,
    prune_session_pr_snapshots,
    sanitize_pr_snapshot,
    save_session_pr_snapshots,
    session_pr_snapshot_keys,
)

PR = {
    "provider": "github",
    "host": "github.com",
    "repository": "example/project",
    "number": 42,
    "url": "https://github.com/example/project/pull/42",
    "relationship": "created",
    "source": "native_hook",
    "first_seen_at": 1.0,
    "last_seen_at": 2.0,
    "title": "Saved change",
}


def test_pr_metadata_preserves_other_state_and_rejects_stale_reports(conversation_store):
    store = conversation_store
    conversation = store.create_conversation()
    assert conversation.session_pull_requests is None
    todos = [{"content": "Keep", "status": "pending", "activeForm": "Keeping"}]
    assert store.set_session_prs(conversation.id, [PR], updated_at_ns=10)
    store.set_session_todos(conversation.id, todos)
    store.set_session_state(conversation.id, {"count": 2, "_omnigent_session_prs_v1": []})
    saved = store.get_conversation(conversation.id)
    assert saved.session_pull_requests == [PR]
    assert saved.session_pull_requests_updated_at_ns == 10
    assert saved.session_state == {"count": 2}
    assert saved.session_todos == todos
    assert store.fork_conversation(conversation.id).session_pull_requests is None
    assert store.set_session_prs(conversation.id, [], updated_at_ns=11)
    assert not store.set_session_prs(conversation.id, [PR], updated_at_ns=10)
    assert not store.set_session_prs(conversation.id, [PR], updated_at_ns=11)
    assert not store.set_session_prs(conversation.id, [PR])
    assert store.get_conversation(conversation.id).session_pull_requests == []
    assert store.get_conversation(conversation.id).session_todos == todos
    assert store.set_session_prs(conversation.id, [PR], user_update=True)
    assert not store.set_session_prs(conversation.id, [], updated_at_ns=10)
    assert store.get_conversation(conversation.id).session_pull_requests == [PR]
    assert store.get_conversation(conversation.id).session_pull_requests_updated_at_ns == 11
    for revision in (True, 2**80, time.time_ns() + 600_000_000_000):
        with pytest.raises(ValueError):
            store.set_session_prs(conversation.id, [], updated_at_ns=revision)
    assert store.get_conversation(conversation.id).session_pull_requests == [PR]
    assert store.set_session_prs(conversation.id, [], updated_at_ns=12)


def test_legacy_pr_reports_evolve_until_an_authoritative_update(conversation_store):
    conversation = conversation_store.create_conversation()
    assert conversation_store.set_session_prs(conversation.id, [])
    assert conversation_store.set_session_prs(conversation.id, [PR])
    assert not conversation_store.set_session_prs(conversation.id, [])
    assert conversation_store.set_session_prs(conversation.id, [{**PR, "title": "New title"}])
    assert conversation_store.set_session_prs(conversation.id, [], user_update=True)
    assert not conversation_store.set_session_prs(conversation.id, [PR])
    assert conversation_store.set_session_prs(conversation.id, [], updated_at_ns=1)
    assert not conversation_store.set_session_prs(conversation.id, [PR])
    assert conversation_store.get_conversation(conversation.id).session_pull_requests == []


def test_pr_snapshot_validation_strips_private_fields_and_supports_private_hosts():
    private = {
        **PR,
        "host": "git.internal.test",
        "url": "https://git.internal.test/group/project/-/merge_requests/42",
    }
    assert validate_session_prs([{**private, "auth": {"token": "private"}}, private]) == [private]
    for changes in (
        {"url": "javascript:alert(1)"},
        {"url": "https://attacker.test/pull/42"},
        {"url": "https://user:secret@github.com/example/project/pull/42"},
        {"url": PR["url"] + "?token=secret"},
        {"url": PR["url"] + "\n"},
        {"number": True},
        {"last_seen_at": float("inf")},
        {"title": "x" * 4097},
    ):
        with pytest.raises(ValueError):
            validate_session_prs([{**PR, **changes}])
    for value in (None, {}, [PR] * 101, [{**PR, "title": "字" * 4096}] * 20):
        with pytest.raises(ValueError):
            validate_session_prs(value)


def test_pr_panel_snapshots_keep_latest_resources_and_clean_removed_blobs(
    conversation_store, tmp_path
):
    store = conversation_store
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversation = store.create_conversation()
    store.set_session_prs(conversation.id, [PR], updated_at_ns=1)
    info = {"pr": {**PR, "body": "Description", "comments": [{"body": "Comment"}]}}
    assert save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        3_000_000_000,
        {"info": info, "diff": {"patch": "first patch"}},
    )
    saved = store.get_conversation(conversation.id)
    first_keys = session_pr_snapshot_keys(saved)
    store.set_session_state(conversation.id, {"count": 2})
    store.set_session_todos(
        conversation.id, [{"content": "Keep", "status": "pending", "activeForm": "Keeping"}]
    )
    assert (
        store.get_conversation(conversation.id).session_pr_snapshots == saved.session_pr_snapshots
    )
    assert store.fork_conversation(conversation.id).session_pr_snapshots == {}
    assert save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        5_000_000_000,
        {"diff": {"patch": "new patch"}},
    )
    assert not artifacts.exists(first_keys[1])
    assert not save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        4_000_000_000,
        {"diff": {"patch": "stale patch"}},
    )
    saved = store.get_conversation(conversation.id)
    assert load_session_pr_snapshot(artifacts, saved, PR["url"], "diff")["patch"] == "new patch"
    cached_info = load_session_pr_snapshot(artifacts, saved, PR["url"], "info")
    assert cached_info["pr"]["comments"][0]["body"] == "Comment"
    assert cached_info["cached_at"] == 3
    assert len(list((tmp_path / "artifacts").rglob("*.json"))) == 2
    info["pr"]["head_sha"] = "new-head"
    assert save_session_pr_snapshots(
        store, artifacts, conversation.id, PR["url"], 6_000_000_000, {"info": info}
    )
    assert (
        load_session_pr_snapshot(
            artifacts, store.get_conversation(conversation.id), PR["url"], "diff"
        )["unavailable_reason"]
        == "not_synced"
    )
    assert not save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        5_500_000_000,
        {"diff": {"patch": "old head"}},
    )
    for timestamp in (True, -1, 10**30, time.time_ns() + 600_000_000_000):
        with pytest.raises(ValueError):
            save_session_pr_snapshots(
                store, artifacts, conversation.id, PR["url"], timestamp, {"info": info}
            )
        with pytest.raises(ValueError):
            store.set_session_pr_snapshot(
                conversation.id, PR["url"], timestamp, {"info": "invalid"}
            )
    assert save_session_pr_snapshots(
        store, artifacts, conversation.id, PR["url"], 7_000_000_000, {"info": info}
    )
    store.set_session_prs(conversation.id, [], updated_at_ns=2)
    prune_session_pr_snapshots(store, artifacts, conversation.id)
    assert store.get_conversation(conversation.id).session_pr_snapshots == {}
    assert not list((tmp_path / "artifacts").rglob("*.json"))
    assert not save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        6_000_000_000,
        {"info": info},
    )
    store.set_session_prs(conversation.id, [{**PR, "last_seen_at": 10}], updated_at_ns=3)
    assert not save_session_pr_snapshots(
        store,
        artifacts,
        conversation.id,
        PR["url"],
        9_000_000_000,
        {"info": info},
    )
    assert (
        load_session_pr_snapshot(
            artifacts, store.get_conversation(conversation.id), PR["url"], "info"
        )["unavailable_reason"]
        == "not_synced"
    )
    assert not list((tmp_path / "artifacts").rglob("*.json"))


def test_pr_panel_snapshots_are_bounded_and_strip_host_configuration():
    info = {
        "auth": {"token": "secret", "accounts": [{"login": "private"}]},
        "accounts": [{"token": "secret"}],
        "selected_account": "private",
        "pr": {
            **PR,
            "body": "Description",
            "checks": {
                "runs": [
                    {
                        "name": "test",
                        "bucket": "passing",
                        "url": "https://ci.test/job?token=secret",
                    }
                ]
            },
            "comments": [
                {
                    "body": "字" * 20_000,
                    "url": "https://github.com/example/project/pull/42#issuecomment-1",
                    "token": "secret",
                }
            ]
            * 100,
        },
    }
    shaped = sanitize_pr_snapshot("info", info, PR["url"])
    encoded = json.dumps(shaped, ensure_ascii=False, separators=(",", ":")).encode()
    assert len(encoded) <= MAX_PR_INFO_BYTES
    assert b"secret" not in encoded and b"accounts" not in encoded
    assert shaped["pr"]["body"] == "Description"
    assert shaped["pr"]["comments_partial"] is True
    assert shaped["pr"]["checks"]["runs"][0]["url"] == "https://ci.test/job"
    assert (
        sanitize_pr_snapshot("diff", {"patch": "x" * MAX_PR_DIFF_BYTES}, PR["url"])[
            "unavailable_reason"
        ]
        == "too_large"
    )
    assert (
        sanitize_pr_snapshot("changes", {"data": [{"path": "file"}] * 2001}, PR["url"])[
            "unavailable_reason"
        ]
        == "too_large"
    )
    with pytest.raises(ValueError, match="associated pull request"):
        sanitize_pr_snapshot("info", info, PR["url"] + "0")
    nullable = sanitize_pr_snapshot(
        "info",
        {
            "pr": {
                **PR,
                "title": None,
                "state": None,
                "reviewers": [{"login": None, "state": {}}],
                "comments": [
                    {
                        "body": None,
                        "url": "https://dev.azure.com/org/repo/pullrequest/42?discussionId=5&token=secret",
                    }
                ],
                "checks": {
                    "runs": [
                        {
                            "name": None,
                            "bucket": "pending",
                            "url": "https://dev.azure.com/org/project/_build/results?buildId=7&token=secret",
                        }
                    ]
                },
            }
        },
        PR["url"],
    )["pr"]
    assert nullable["title"] == "Pull request #42" and nullable["state"] == "UNKNOWN"
    assert nullable["reviewers"] == [] and nullable["comments"][0]["body"] == ""
    assert nullable["comments"][0]["url"].endswith("?discussionId=5")
    assert nullable["checks"]["runs"][0]["url"].endswith("?buildId=7")
    files = sanitize_pr_snapshot(
        "changes", {"data": [{"path": "test.py", "status": "unknown"}]}, PR["url"]
    )["data"]
    assert files[0]["status"] == "modified" and files[0]["lines_added"] is None
