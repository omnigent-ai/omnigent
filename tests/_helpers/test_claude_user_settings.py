"""Configuration path selection used by the Claude-native acceptance helpers."""

import json
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"HOME": "/home/user"}, "/home/user/.claude/settings.json"),
        ({"HOME": "/home/user", "CLAUDE_CONFIG_DIR": "/srv/claude"}, "/srv/claude/settings.json"),
        ({"HOME": "/home/user", "CLAUDE_CONFIG_DIR": "cfg/../conf"}, "/repo/conf/settings.json"),
    ],
    ids=["home", "config dir", "relative config dir"],
)
def test_settings_path_follows_claudes_config_dir(env: dict[str, str], expected: str) -> None:
    from tests.e2e.routing._helpers import claude_user_settings_path

    assert claude_user_settings_path(Path("/repo"), env) == Path(expected)


@pytest.mark.parametrize("version", ["2.1.322", "2.1.323", "2.1.323-preview", None])
def test_settings_comparison_uses_the_running_sessions_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str | None
) -> None:
    from omnigent.harnesses.claude_native.bridge import CONTEXT_RAW_FILE
    from tests.e2e.routing import _helpers

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / CONTEXT_RAW_FILE).write_text(json.dumps({"version": version}))
    monkeypatch.setattr(_helpers, "claude_bridge_dir", lambda _: bridge_dir)
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"model": "original", "language": "en"}))
    before = _helpers.claude_settings_snapshot(path)

    path.write_text(json.dumps({"model": "changed", "language": "en"}))
    assert _helpers.claude_settings_unchanged(path, before, "session") is (version != "2.1.323")
    path.write_text(json.dumps({"model": "original", "language": "fr"}))
    assert not _helpers.claude_settings_unchanged(path, before, "session")
