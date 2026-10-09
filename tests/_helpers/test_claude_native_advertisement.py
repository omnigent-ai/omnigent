"""The browser fault injection only ever removes the fixture session's advertisement."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from tests._helpers.claude_native_advertisement import remove_tmux_advertisement


def _advertised_bridge(root: Path, name: str, session_id: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / claude_native_bridge._CONFIG_FILE).write_text(
        json.dumps({"active_session_id": session_id}), encoding="utf-8"
    )
    target = directory / claude_native_bridge._TMUX_FILE
    target.write_text("synthetic advertisement", encoding="utf-8")
    return target


def test_only_removes_its_session_advertisement(tmp_path: Path) -> None:
    own = _advertised_bridge(tmp_path, "own", "fixture-session")
    other = _advertised_bridge(tmp_path, "other", "unrelated-session")
    os.utime(own, (1, 1))
    other_content = other.read_bytes()

    assert remove_tmux_advertisement("fixture-session", bridge_root=tmp_path) == str(own)
    assert not own.exists()
    assert other.read_bytes() == other_content


@pytest.mark.parametrize("own_advertisements", [0, 2], ids=["missing", "ambiguous"])
def test_requires_exactly_one_owned_advertisement(tmp_path: Path, own_advertisements: int) -> None:
    targets = [_advertised_bridge(tmp_path, "other", "unrelated-session")]
    targets.extend(
        _advertised_bridge(tmp_path, f"own-{i}", "fixture-session")
        for i in range(own_advertisements)
    )
    contents = {target: target.read_bytes() for target in targets}

    with pytest.raises(AssertionError, match="exactly one advertisement"):
        remove_tmux_advertisement("fixture-session", bridge_root=tmp_path)
    assert {target: target.read_bytes() for target in targets} == contents
