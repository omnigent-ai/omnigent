"""Tests for probing which terminal queries tmux answers itself."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnigent.terminals import query_replies
from omnigent.terminals.query_replies import answered_queries, tmux_answered_queries

_HAS_TMUX = shutil.which("tmux") is not None
_DA1_REPLY = b"\x1b[?1;2c"


def test_answered_queries_attributes_each_reply_to_the_query_before_its_sentinel() -> None:
    # tmux answered "dsr:6" (between the first and second sentinels) but not "da2".
    replies = _DA1_REPLY + b"\x1b[3;1R" + _DA1_REPLY + _DA1_REPLY

    assert answered_queries(replies, ["da2", "dsr:6", "osc:12"]) == {"da1", "dsr:6"}


def test_answered_queries_is_unknown_when_sentinels_are_missing() -> None:
    assert answered_queries(b"\x1b[0n" + _DA1_REPLY, ["dsr:5", "dsr:6"]) is None


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_probe_finds_the_queries_every_supported_tmux_answers() -> None:
    """Invariants that hold from tmux 3.3 to 3.8; the rest varies by release."""
    query_replies._cache.clear()
    answered = await tmux_answered_queries(shutil.which("tmux") or "", {})

    assert answered is not None
    assert {"da1", "da2", "dsr:5", "dsr:6"} <= answered
    # No release up to 3.8 answers these, so the browser must keep answering.
    assert not {"dsr:?6", "osc:4", "osc:12"} & answered


@pytest.mark.skipif(not _HAS_TMUX, reason="tmux not installed")
@pytest.mark.asyncio
async def test_probe_runs_once_per_tmux_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    query_replies._cache.clear()
    probes: list[str] = []
    original_probe = query_replies._probe

    async def counting_probe(tmux: str, client_env: dict[str, str]) -> frozenset[str] | None:
        probes.append(tmux)
        return await original_probe(tmux, client_env)

    monkeypatch.setattr(query_replies, "_probe", counting_probe)
    tmux = shutil.which("tmux") or ""

    first = await tmux_answered_queries(tmux, {})
    second = await tmux_answered_queries(tmux, {})

    assert first is not None and first == second
    assert probes == [tmux]


@pytest.mark.asyncio
async def test_probe_failure_leaves_every_reply_to_the_browser(tmp_path: Path) -> None:
    """A tmux that can't start a session yields ``None``: drop nothing."""
    query_replies._cache.clear()
    broken = tmp_path / "tmux"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)

    assert await tmux_answered_queries(str(broken), {}) is None
