"""Skill contents match the menu, omit frontmatter/paths, and stay bounded."""

import json
from pathlib import Path

import pytest

from omnigent.host.skill_content import MAX_SKILL_CONTENT_BYTES, read_skill_content


def test_plain_plugin_and_private_diagnostics(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    skill = claude / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review diffs.\n---\n# Private instructions\n"
    )
    (skill / "other.md").write_text("other-file-secret")
    plugin = claude / "plugins" / "cache" / "toolkit"
    bundled = plugin / "skills" / "lint"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text("---\nname: lint\ndescription: Lint code.\n---\nRun lint.")
    (claude / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"toolkit@test": [{"installPath": str(plugin)}]}})
    )
    (claude / "settings.json").write_text(json.dumps({"enabledPlugins": {"toolkit@test": True}}))
    broken = claude / "skills" / "broken"
    broken.mkdir()
    (broken / "SKILL.md").write_text(
        "---\nname: [\ndescription: synthetic-private-frontmatter\n---\nprivate-body"
    )
    result = read_skill_content("claude-native", "review")
    assert result == {
        "name": "review",
        "description": "Review diffs.",
        "content": "# Private instructions",
        "truncated": False,
    }
    assert read_skill_content("claude-native", "toolkit:lint")["content"] == "Run lint."
    assert str(tmp_path) not in json.dumps(result)
    assert "other-file-secret" not in json.dumps(result)
    assert "synthetic-private-frontmatter" not in caplog.text
    assert "private-body" not in caplog.text
    assert "Private instructions" not in caplog.text
    with pytest.raises(LookupError):
        read_skill_content("claude-native", "../other.md")


@pytest.mark.parametrize(
    "content,truncated",
    [
        ("a" * MAX_SKILL_CONTENT_BYTES, False),
        ("é" * (MAX_SKILL_CONTENT_BYTES // 2 + 1), True),
        ("€" * MAX_SKILL_CONTENT_BYTES, True),
    ],
)
def test_content_byte_cap(monkeypatch, content, truncated):
    from omnigent.spec.types import SkillSpec

    monkeypatch.setattr(
        "omnigent.host.skill_content.resolve_harness_skills",
        lambda *_: [SkillSpec(name="large", description="Large", content=content)],
    )
    result = read_skill_content("claude-native", "large")
    body = result["content"]
    assert isinstance(body, str)
    assert len(body.encode("utf-8")) <= MAX_SKILL_CONTENT_BYTES
    assert content.startswith(body)
    assert result["truncated"] is truncated


@pytest.mark.parametrize("enabled", [False, True])
def test_installed_skill_identity_survives_collisions_and_invocation_filters(
    tmp_path, monkeypatch, enabled
):
    from omnigent.host.plugins import discover_plugins

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude = tmp_path / ".claude"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    # The directory, not the frontmatter, is a skill's identity.
    plain = claude / "skills" / "toolkit:lint"
    plain.mkdir(parents=True)
    (plain / "SKILL.md").write_text("---\nname: toolkit:lint\ndescription: Plain\n---\nplain body")
    installs = {}
    for market in ("first", "second"):
        plugin = claude / "plugins" / "cache" / market
        installs[f"toolkit@{market}"] = [{"installPath": str(plugin)}]
        for directory in ("lint", "same-name"):
            skill = plugin / "skills" / directory
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: lint\ndescription: Installed\nuser-invocable: false\n---\n"
                f"{market}/{directory} body"
            )
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": installs}))
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": dict.fromkeys(installs, enabled)})
    )
    plugins = discover_plugins()
    assert len({plugin["id"] for plugin in plugins}) == 2
    ids = set()
    for plugin in plugins:
        assert plugin["enabled"] is enabled
        bodies = set()
        for entry in plugin["skill_entries"]:
            ids.add(entry["id"])
            result = read_skill_content("claude-native", f"toolkit:{entry['name']}", entry["id"])
            bodies.add(result["content"])
            assert str(tmp_path) not in json.dumps(result)
        assert bodies == {
            f"{plugin['marketplace']}/{directory} body" for directory in ("lint", "same-name")
        }
    assert len(ids) == 4
    assert read_skill_content("claude-native", "toolkit:lint")["content"] == "plain body"
    with pytest.raises(LookupError):
        read_skill_content("claude-native", "toolkit:lint", "0" * 64)
    with pytest.raises(LookupError):
        read_skill_content("codex-native", "toolkit:lint", next(iter(ids)))
