"""Tests for parsing the ``OMNIGENT_SEEDED_AGENTS`` allowlist.

The seeding gate and the suppressed set live with the other seeder tests in
``tests/server/test_app.py``; the list-route filter in
``tests/server/routes/test_builtin_agents.py``.
"""

from __future__ import annotations

import os

import pytest

from omnigent.server.seeded_agents import SEEDED_AGENTS_ENV, seeded_agent_allowlist


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, None),
        ("", frozenset()),
        (" polly , ,debby, ", frozenset({"polly", "debby"})),
        (f"polly{os.pathsep}debby", frozenset({f"polly{os.pathsep}debby"})),
    ],
    ids=["unset-keeps-all", "empty-keeps-none", "trims-entries", "commas-not-pathsep"],
)
def test_seeded_agent_allowlist(
    monkeypatch: pytest.MonkeyPatch, raw: str | None, expected: frozenset[str] | None
) -> None:
    """Unset (seed all) must differ from empty (seed none); entries are comma-separated."""
    if raw is None:
        monkeypatch.delenv(SEEDED_AGENTS_ENV, raising=False)
    else:
        monkeypatch.setenv(SEEDED_AGENTS_ENV, raw)

    assert seeded_agent_allowlist() == expected
