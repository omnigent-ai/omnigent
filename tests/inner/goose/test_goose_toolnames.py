"""Tests for Goose's tool-name dialect.

The name this resolves is what Omnigent's TOOL_CALL policies match on, so a
regression here silently un-gates Goose's shell and file writes rather than
failing visibly. That is why the self-gating contract is asserted directly.
"""

from __future__ import annotations

from omnigent.inner.acp_toolnames import AcpToolNameSource
from omnigent.inner.goose.toolnames import GooseToolNameSource


def _tool_call(name: str) -> dict:
    """A Goose ``toolCall`` carrying *name* at its vendor ``_meta`` path."""
    return {
        "title": "shell · echo hi",
        "rawInput": {"command": "echo hi"},
        "_meta": {"goose": {"toolCall": {"toolName": name}}},
    }


def test_reads_goose_meta_tool_name() -> None:
    """The vendor id is preferred over ACP's friendly ``title`` label.

    ``ask_on_os_tools`` gates Goose on exactly ``developer__shell`` /
    ``developer__write``; given only ``"shell · echo hi"`` that rule matches
    nothing.
    """
    assert GooseToolNameSource().read(_tool_call("developer__shell")) == "developer__shell"


def test_self_gates_on_frames_without_the_dialect() -> None:
    """Returns ``None`` for anything not carrying Goose's ``_meta`` path.

    The protocol requires self-gating: a source may be handed frames from an
    agent's fork or a future multi-dialect wrap, and must stay inert on them
    rather than claiming a name.
    """
    source = GooseToolNameSource()
    assert source.read({}) is None
    assert source.read({"title": "shell"}) is None
    assert source.read({"_meta": {}}) is None
    assert source.read({"_meta": "not-a-mapping"}) is None
    assert source.read({"_meta": {"goose": {}}}) is None
    assert source.read({"_meta": {"goose": {"toolCall": {}}}}) is None
    # Present but unusable values are not names either.
    assert source.read({"_meta": {"goose": {"toolCall": {"toolName": ""}}}}) is None
    assert source.read({"_meta": {"goose": {"toolCall": {"toolName": 7}}}}) is None
    # Another vendor's _meta must not be mistaken for Goose's.
    assert source.read({"_meta": {"cognition.ai/subagent_started": {"agentId": "x"}}}) is None


def test_satisfies_the_runtime_protocol() -> None:
    """The dialect is duck-typed against the seam's protocol."""
    assert isinstance(GooseToolNameSource(), AcpToolNameSource)
