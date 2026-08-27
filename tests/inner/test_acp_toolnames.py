"""Tests for the generic ACP tool-name seam (:mod:`omnigent.inner.acp_toolnames`).

The generic half only: the reader that runs a vendor's dialects and the contract
those dialects must honor. A vendor's own dialect is tested with that vendor (see
``tests/inner/goose/test_goose_toolnames.py``).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from omnigent.inner.acp_toolnames import AcpToolNameSource, read_tool_name


class _Named:
    """A dialect that reports a fixed name when its marker key is present."""

    def __init__(self, marker: str, name: str) -> None:
        self._marker = marker
        self._name = name

    def read(self, tool_call: Mapping[str, Any]) -> str | None:
        return self._name if self._marker in tool_call else None


def test_no_sources_resolves_nothing() -> None:
    """The generic ACP harness supplies no dialects, so nothing is resolved.

    This is the path every builtin ACP row takes; it must leave the portable
    ``title`` / ``kind`` fields authoritative.
    """
    assert read_tool_name({"title": "shell"}, ()) is None


def test_first_matching_source_wins() -> None:
    """Sources are tried in order and the first non-empty name is returned."""
    sources = (_Named("a", "from_a"), _Named("b", "from_b"))
    assert read_tool_name({"b": 1}, sources) == "from_b"
    assert read_tool_name({"a": 1, "b": 1}, sources) == "from_a"


def test_unrecognized_frame_resolves_nothing() -> None:
    """A frame no dialect recognizes falls through, rather than guessing."""
    assert read_tool_name({"title": "shell"}, (_Named("a", "from_a"),)) is None


def test_sources_satisfy_the_runtime_protocol() -> None:
    """A dialect is duck-typed against the protocol, so a rename fails loudly."""
    assert isinstance(_Named("a", "x"), AcpToolNameSource)
