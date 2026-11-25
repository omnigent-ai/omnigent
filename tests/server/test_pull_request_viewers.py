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
