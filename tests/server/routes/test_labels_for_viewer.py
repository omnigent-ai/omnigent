"""Unit tests for ``_labels_for_viewer`` label-family collapsing.

Two dynamic-suffix families are stored under indexed/per-user keys but presented
on the wire as one canonical bare key so clients read the same key they write:
per-user pins (``omnigent.pinned.<user>``) and per-repo sandbox workspaces
(``omnigent.sandbox.repo.<index>``). The repo collapse is load-bearing for the
fork dialog, which reads the bare key — without it, forking a session created
after per-repo storage would see no repo and clone an empty sandbox.
"""

from __future__ import annotations

from omnigent.server.managed_hosts import MANAGED_REPO_LABEL_KEY
from omnigent.server.routes._sessions.orchestration import _labels_for_viewer
from omnigent.stores.conversation_store import pinned_label_key


def test_collapses_per_repo_labels_to_the_bare_space_joined_key() -> None:
    """Per-index repo labels present as one bare key, ordered, space-joined."""
    labels = {
        f"{MANAGED_REPO_LABEL_KEY}.0": "https://github.com/org/api#main",
        f"{MANAGED_REPO_LABEL_KEY}.1": "https://github.com/org/web",
        "kept": "yes",
    }
    viewer = _labels_for_viewer(labels, "alice@example.com")
    assert viewer[MANAGED_REPO_LABEL_KEY] == (
        "https://github.com/org/api#main https://github.com/org/web"
    )
    # The internal per-index keys never reach the wire.
    assert not any(k.startswith(f"{MANAGED_REPO_LABEL_KEY}.") for k in viewer)
    assert viewer["kept"] == "yes"


def test_no_repo_labels_leaves_no_bare_key() -> None:
    """A session with no sandbox repos exposes no sandbox-repo label."""
    viewer = _labels_for_viewer({"kept": "yes"}, None)
    assert MANAGED_REPO_LABEL_KEY not in viewer
    assert viewer == {"kept": "yes"}


def test_still_collapses_the_pin_family() -> None:
    """The added repo collapse doesn't regress the per-user pin collapse."""
    labels = {
        pinned_label_key("alice@example.com"): "1721760000000",
        pinned_label_key("bob@example.com"): "1721760001000",
    }
    viewer = _labels_for_viewer(labels, "alice@example.com")
    # This viewer's pin surfaces as the canonical bare key; others' don't leak.
    assert viewer.get("omnigent.pinned") == "1721760000000"
    assert not any(k.startswith("omnigent.pinned.") for k in viewer)
