"""What a session's pull request info shows to its owner, an editor and a viewer."""

from __future__ import annotations

from dataclasses import fields
from typing import Any

import pytest

from omnigent.runner.git_providers import ProviderCapabilities
from omnigent.server.pull_request_viewers import (
    OWNER_CAPABILITIES,
    READ_CAPABILITIES,
    PullRequestViewer,
    info_for_viewer,
    review_comments_for_viewer,
    review_threads_for_viewer,
)

_ACCOUNTS = [
    {"login": "owner-login", "host": "github.com", "active": True},
    {"login": "owner-bot", "host": "github.com", "active": False},
]
_ALL_ON = dict.fromkeys((field.name for field in fields(ProviderCapabilities)), True)


def _info() -> dict[str, Any]:
    return {
        "object": "session.github.info",
        "accounts": list(_ACCOUNTS),
        "selected_account": "owner-login",
        "auth": {
            "authenticated": True,
            "accounts": list(_ACCOUNTS),
            "selected_account": "owner-login",
        },
        "capabilities": dict(_ALL_ON),
        "pr": {"number": 7},
    }


def test_every_capability_is_classified_as_a_change_or_a_read() -> None:
    """A new capability fails here until someone decides whether non-owners keep it."""
    assert OWNER_CAPABILITIES.isdisjoint(READ_CAPABILITIES)
    assert {
        field.name for field in fields(ProviderCapabilities)
    } == OWNER_CAPABILITIES | READ_CAPABILITIES


def test_the_owner_sees_the_info_unchanged() -> None:
    info = _info()
    assert info_for_viewer(info, PullRequestViewer.OWNER) is info


@pytest.mark.parametrize(
    ("viewer", "selected"),
    [(PullRequestViewer.EDITOR, "owner-login"), (PullRequestViewer.VIEWER, None)],
)
def test_anyone_else_sees_no_accounts_and_no_change_controls(
    viewer: PullRequestViewer, selected: str | None
) -> None:
    info = _info()
    shown = info_for_viewer(info, viewer)

    assert "accounts" not in shown
    assert shown["auth"]["accounts"] is None
    assert shown.get("selected_account") == selected
    assert shown["auth"]["selected_account"] == selected
    assert shown["auth"]["authenticated"] is True
    assert shown["owner_only_changes"] is True
    assert shown["capabilities"] == {name: name in READ_CAPABILITIES for name in sorted(_ALL_ON)}
    assert shown["pr"] == {"number": 7}
    assert info == _info(), "the shared payload is not modified"


@pytest.mark.parametrize("viewer", [PullRequestViewer.EDITOR, PullRequestViewer.VIEWER])
def test_info_from_a_host_without_capabilities_still_turns_every_change_off(
    viewer: PullRequestViewer,
) -> None:
    """A client's defaults for an older host would otherwise offer the account and base pickers."""
    shown = info_for_viewer({"object": "session.github.info", "accounts": _ACCOUNTS}, viewer)

    assert "auth" not in shown
    assert "accounts" not in shown
    capabilities = shown["capabilities"]
    assert set(capabilities) == set(_ALL_ON)
    assert not any(capabilities[name] for name in OWNER_CAPABILITIES)
    assert capabilities["line_counts"] is True
    assert capabilities["linked_pr_diff"] is True


def test_an_unknown_capability_from_a_newer_host_is_turned_off() -> None:
    info = {"capabilities": {"line_counts": True, "rebasing": True}}
    shown = info_for_viewer(info, PullRequestViewer.EDITOR)
    assert shown["capabilities"]["rebasing"] is False
    assert shown["capabilities"]["line_counts"] is True


def _comment() -> dict[str, Any]:
    return {
        "id": "PRRC_1",
        "database_id": 5,
        "author": "reviewer",
        "reactions": [{"content": "EYES", "count": 3, "viewer_reacted": True}],
        "can_react": True,
        "can_update": True,
        "can_delete": True,
        "viewer_did_author": True,
    }


def _threads() -> dict[str, Any]:
    thread = {
        "id": "PRRT_1",
        "can_reply": True,
        "can_resolve": False,
        "can_unresolve": True,
        "comments": {"data": [_comment()], "has_more": True, "next_cursor": "n"},
    }
    return {"data": [thread], "head_sha": "a" * 40, "has_more": False}


def test_the_owner_reads_threads_and_comments_unchanged() -> None:
    threads, comments = _threads(), {"data": [_comment()]}
    assert review_threads_for_viewer(threads, PullRequestViewer.OWNER) is threads
    assert review_comments_for_viewer(comments, PullRequestViewer.OWNER) is comments


@pytest.mark.parametrize("viewer", [PullRequestViewer.EDITOR, PullRequestViewer.VIEWER])
def test_anyone_else_reads_threads_with_ids_and_counts_but_no_rights(
    viewer: PullRequestViewer,
) -> None:
    threads = _threads()
    shown = review_threads_for_viewer(threads, viewer)

    [thread] = shown["data"]
    assert {key: thread[key] for key in ("can_reply", "can_resolve", "can_unresolve")} == {
        "can_reply": False,
        "can_resolve": False,
        "can_unresolve": False,
    }
    assert thread["comments"]["next_cursor"] == "n"
    [comment] = thread["comments"]["data"]
    assert comment == {
        **_comment(),
        "reactions": [{"content": "EYES", "count": 3, "viewer_reacted": False}],
        "can_react": False,
        "can_update": False,
        "can_delete": False,
        "viewer_did_author": False,
    }
    assert shown["head_sha"] == "a" * 40
    # The host's answer itself is left as it was.
    assert threads == _threads()


def test_a_comment_page_hides_the_rights_and_keeps_its_paging() -> None:
    shown = review_comments_for_viewer(
        {"data": [_comment(), "unexpected"], "has_more": True}, PullRequestViewer.VIEWER
    )
    assert shown["has_more"] is True
    assert shown["data"][0]["can_update"] is False
    assert shown["data"][1] == "unexpected"


def test_a_page_without_data_stays_empty() -> None:
    assert review_threads_for_viewer({"warning": "x"}, PullRequestViewer.VIEWER) == {
        "warning": "x",
        "data": [],
    }


@pytest.mark.parametrize("viewer", [PullRequestViewer.EDITOR, PullRequestViewer.VIEWER])
def test_anyone_else_reads_the_overviews_comments_without_the_accounts_rights(
    viewer: PullRequestViewer,
) -> None:
    """Even from a host that sends them unasked, as an older or a forked host may."""
    info = {"object": "session.github.info", "pr": {"number": 7, "comments": [_comment(), "x"]}}
    shown = info_for_viewer(info, viewer)

    [comment, unexpected] = shown["pr"]["comments"]
    assert comment == {
        **_comment(),
        "reactions": [{"content": "EYES", "count": 3, "viewer_reacted": False}],
        "can_react": False,
        "can_update": False,
        "can_delete": False,
        "viewer_did_author": False,
    }
    assert unexpected == "x"
    assert shown["pr"]["number"] == 7
    assert info["pr"]["comments"][0] == _comment()


@pytest.mark.parametrize("pr", [None, {"number": 7}, {"number": 7, "comments": None}])
def test_an_overview_without_comments_keeps_its_pr_as_it_was(pr: object) -> None:
    shown = info_for_viewer({"object": "session.github.info", "pr": pr}, PullRequestViewer.VIEWER)
    assert shown["pr"] == pr
