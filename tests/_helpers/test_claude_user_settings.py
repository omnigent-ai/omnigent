"""Configuration path selection used by the Claude-native acceptance helpers."""

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
