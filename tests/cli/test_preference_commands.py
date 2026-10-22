import pytest

from omnigent.preference_commands import _key


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("model", {"kind": "model", "name": "model"}),
        ("opencode_model", {"kind": "model", "name": "opencode_model"}),
        ("harness", {"kind": "harness", "name": "default"}),
        ("args/claude", {"kind": "harness_args", "name": "claude-sdk"}),
        (
            "plugin/codex/vendor%2Frepo/my-plugin",
            {
                "kind": "plugin",
                "name": {
                    "ecosystem": "codex",
                    "source": "vendor/repo",
                    "name": "my-plugin",
                },
            },
        ),
    ],
)
def test_preference_key_spelling(spelling, expected):
    assert _key(spelling).model_dump(mode="json") == expected


@pytest.mark.parametrize("spelling", ["credentials", "args/default", "plugin/claude/name", ""])
def test_reject_nonportable_or_incomplete_keys(spelling):
    with pytest.raises(ValueError):
        _key(spelling)
