from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills


def _skill(root: Path, name: str = "review", *, hidden: bool = False) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} description\n"
        f"user-invocable: {str(not hidden).lower()}\n---\nInstructions\n"
    )
    return directory


def _plugin(codex_home: Path, name: str = "toolkit", version: str = "1.0.0") -> Path:
    root = codex_home / "plugins" / "cache" / "market" / name / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / ".codex-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": version})
    )
    return root


def _config(codex_home: Path, **plugins: bool | None) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    (codex_home / "config.toml").write_text(
        "\n".join(
            f'[plugins."{name}@market"]\n'
            + (f"enabled = {str(enabled).lower()}\n" if enabled is not None else "")
            for name, enabled in plugins.items()
        )
    )


def _discover(
    home: Path,
    skills_filter: str | list[str] = "all",
    *,
    codex_home: Path | None = None,
) -> dict[str, Path | None]:
    ctx = SkillSourceContext(
        roots=(), home=home, skills_filter=skills_filter, bundle_dir=None, codex_home=codex_home
    )
    return {s.name: s.skill_dir for s in resolve_harness_skills(ctx, "codex-native")}


def test_enabled_plugin_skills_keep_namespaces_and_standalone_skills(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    standalone = _skill(codex_home / "skills")
    first = _skill(_plugin(codex_home) / "skills")
    second = _skill(_plugin(codex_home, "other") / "skills")
    _skill(_plugin(codex_home, "disabled") / "skills")
    _skill(_plugin(codex_home, "uninstalled") / "skills")
    _config(codex_home, toolkit=True, other=None, disabled=False, missing=True)

    assert _discover(tmp_path) == {
        "review": standalone,
        "toolkit:review": first,
        "other:review": second,
    }


@pytest.mark.parametrize(
    "versions,active",
    [
        (["1.9.0", "1.10.0"], "1.10.0"),
        (["2.0.0", "local"], "local"),
        (["1.0.0-rc.9", "1.0.0-rc.10"], "1.0.0-rc.10"),
        (["1.0.0-rc.1", "1.0.0"], "1.0.0"),
        (["1.0.0+build.9", "1.0.0+build.10"], "1.0.0+build.10"),
        (["aaa", "bbb"], "bbb"),
    ],
)
def test_only_active_plugin_version_is_discovered(
    tmp_path: Path, versions: list[str], active: str
) -> None:
    codex_home = tmp_path / ".codex"
    for version in versions:
        _skill(_plugin(codex_home, version=version) / "skills", version)
    _config(codex_home, toolkit=True)

    assert _discover(tmp_path) == {
        f"toolkit:{active}": codex_home
        / "plugins/cache/market/toolkit"
        / active
        / "skills"
        / active
    }


@pytest.mark.parametrize(
    "skills_filter,expected",
    [
        ("none", set()),
        ([], set()),
        (["review"], {"toolkit:review"}),
        (["toolkit:review"], {"toolkit:review"}),
        (["missing"], set()),
        ("all", {"toolkit:review"}),
    ],
)
def test_plugin_skill_filters(
    tmp_path: Path, skills_filter: str | list[str], expected: set[str]
) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    _skill(root / "skills")
    _skill(root / "skills", "hidden", hidden=True)
    _config(codex_home, toolkit=True)

    assert set(_discover(tmp_path, skills_filter)) == expected


@pytest.mark.parametrize("paths", ["./custom", ["./custom", "./extra"], []])
def test_plugin_manifest_skill_paths(tmp_path: Path, paths: str | list[str]) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "toolkit", "skills": paths})
    )
    _skill(root / "skills", "default")
    _skill(root / "custom", "custom")
    _skill(root / "extra", "extra")
    _skill(root / ".codex-plugin/migrated-command-skills", "command")
    _config(codex_home, toolkit=True)

    expected = {"toolkit:command"}
    if paths:
        expected.add("toolkit:custom")
        if isinstance(paths, list):
            expected.add("toolkit:extra")
    else:
        expected.add("toolkit:default")
    assert set(_discover(tmp_path)) == expected


def test_plugin_skill_uses_frontmatter_name(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    directory = _skill(_plugin(codex_home) / "skills")
    directory.rename(directory.with_name("directory-name"))
    _config(codex_home, toolkit=True)

    assert _discover(tmp_path) == {"toolkit:review": directory.with_name("directory-name")}


def test_plugin_manifest_can_point_to_a_single_skill(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    skill = _skill(root / "skills")
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "toolkit", "skills": "./skills/review"})
    )
    _config(codex_home, toolkit=True)

    assert _discover(tmp_path) == {"toolkit:review": skill}

    (skill / "SKILL.md").write_text("---\nname: [\n---\n")
    assert _discover(tmp_path) == {}


def test_plugin_discovery_honors_custom_codex_home(tmp_path: Path) -> None:
    for codex_home, name in [(tmp_path / ".codex", "default"), (tmp_path / "custom", "custom")]:
        _skill(_plugin(codex_home, name) / "skills")
        _config(codex_home, **{name: True})

    assert set(_discover(tmp_path, codex_home=tmp_path / "custom")) == {"custom:review"}


@pytest.mark.parametrize("config", ["invalid = [", 'plugins = "invalid"'])
def test_bad_plugin_config_keeps_standalone_skills(tmp_path: Path, config: str) -> None:
    codex_home = tmp_path / ".codex"
    standalone = _skill(codex_home / "skills")
    _skill(_plugin(codex_home) / "skills")
    (codex_home / "config.toml").write_text(config)

    assert _discover(tmp_path) == {"review": standalone}


@pytest.mark.parametrize("manifest", ["{", "[]", "{}", '{"name": 42}'])
def test_bad_plugin_manifest_does_not_hide_other_plugins(tmp_path: Path, manifest: str) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    _skill(root / "skills")
    (root / ".codex-plugin/plugin.json").write_text(manifest)
    _skill(_plugin(codex_home, "healthy") / "skills")
    _config(codex_home, toolkit=True, healthy=True)

    assert set(_discover(tmp_path)) == {"healthy:review"}
