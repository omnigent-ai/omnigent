from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import Mock

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


def _entry(
    name: str = "toolkit", version: str = "1.0.0", **overrides: object
) -> dict[str, object]:
    return {
        "marketplaceName": "market",
        "name": name,
        "version": version,
        "enabled": True,
        **overrides,
    }


def _inventory(cli: Mock, *entries: dict[str, object]) -> None:
    cli.return_value = subprocess.CompletedProcess(
        [], 0, stdout=json.dumps({"installed": entries, "available": [_entry("uninstalled")]})
    )


@pytest.fixture(autouse=True)
def codex_cli(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: "/test/codex")
    cli = Mock()
    _inventory(cli, _entry())
    monkeypatch.setattr("omnigent.spec.codex_plugin_skills.subprocess.run", cli)
    return cli


def _discover(
    home: Path,
    skills_filter: str | list[str] = "all",
    *,
    codex_home: Path | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Path | None]:
    ctx = SkillSourceContext(
        roots=(),
        home=home,
        skills_filter=skills_filter,
        bundle_dir=None,
        codex_home=codex_home,
        warnings=warnings,
    )
    return {s.name: s.skill_dir for s in resolve_harness_skills(ctx, "codex-native")}


def test_enabled_plugin_skills_keep_namespaces_and_standalone_skills(
    tmp_path: Path, codex_cli: Mock
) -> None:
    codex_home = tmp_path / ".codex"
    standalone = _skill(codex_home / "skills")
    first = _skill(_plugin(codex_home) / "skills")
    second = _skill(_plugin(codex_home, "other") / "skills")
    _skill(_plugin(codex_home, "disabled") / "skills")
    _skill(_plugin(codex_home, "uninstalled") / "skills")
    _inventory(
        codex_cli, _entry(), _entry("other"), _entry("disabled", enabled=False), _entry("missing")
    )

    warnings: list[str] = []
    assert _discover(tmp_path, warnings=warnings) == {
        "review": standalone,
        "toolkit:review": first,
        "other:review": second,
    }
    assert warnings == []


def test_plugin_updates_follow_the_inventory_version(tmp_path: Path, codex_cli: Mock) -> None:
    codex_home = tmp_path / ".codex"
    old = _skill(_plugin(codex_home, version="1.9.0") / "skills")
    new = _skill(_plugin(codex_home, version="1.10.0") / "skills")
    _skill(_plugin(codex_home, version="99.0.0") / "skills", "unused")

    _inventory(codex_cli, _entry(version="1.9.0"))
    assert _discover(tmp_path) == {"toolkit:review": old}
    _inventory(codex_cli, _entry(version="1.10.0"))
    assert _discover(tmp_path) == {"toolkit:review": new}


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
    tmp_path: Path, codex_cli: Mock, skills_filter: str | list[str], expected: set[str]
) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    _skill(root / "skills")
    _skill(root / "skills", "hidden", hidden=True)

    warnings: list[str] = []
    assert set(_discover(tmp_path, skills_filter, warnings=warnings)) == expected
    assert warnings == []
    if skills_filter == "none" or skills_filter == []:
        codex_cli.assert_not_called()


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

    assert _discover(tmp_path) == {"toolkit:review": directory.with_name("directory-name")}


def test_plugin_manifest_can_point_to_a_single_skill(tmp_path: Path) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    skill = _skill(root / "skills")
    (root / ".codex-plugin/plugin.json").write_text(
        json.dumps({"name": "toolkit", "skills": "./skills/review"})
    )

    assert _discover(tmp_path) == {"toolkit:review": skill}

    (skill / "SKILL.md").write_text("---\nname: [\n---\n")
    assert _discover(tmp_path) == {}


def test_plugin_inventory_uses_session_home_and_workspace(tmp_path: Path, codex_cli: Mock) -> None:
    codex_home = tmp_path / "custom"
    skill = _skill(_plugin(codex_home) / "skills")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ctx = SkillSourceContext(
        roots=(workspace,),
        home=tmp_path,
        skills_filter="all",
        bundle_dir=None,
        codex_home=codex_home,
    )

    assert {s.name: s.skill_dir for s in resolve_harness_skills(ctx, "codex-native")} == {
        "toolkit:review": skill
    }
    args, kwargs = codex_cli.call_args
    assert args == (["/test/codex", "plugin", "list", "--json"],)
    assert kwargs["env"]["CODEX_HOME"] == str(codex_home)
    assert kwargs["cwd"] == workspace
    assert kwargs["timeout"] == 5


@pytest.mark.parametrize(
    "failure,summary,logged",
    [
        (
            FileNotFoundError(2, "No such file or directory", "/private/bin/codex"),
            "`codex plugin list` could not be run",
            "[Errno 2] No such file or directory: '/private/bin/codex'",
        ),
        (subprocess.TimeoutExpired("codex", 5), "`codex plugin list` timed out after 5s", ""),
        (subprocess.CalledProcessError(2, "codex"), "`codex plugin list` exited 2", ""),
        (
            subprocess.CalledProcessError(
                1, "codex", stderr="Error: bad /Users/alice/.codex/config.toml\n  line 3\n"
            ),
            "`codex plugin list` exited 1",
            "Error: bad /Users/alice/.codex/config.toml line 3",
        ),
    ],
)
def test_unavailable_plugin_inventory_keeps_standalone_skills(
    tmp_path: Path,
    codex_cli: Mock,
    caplog: pytest.LogCaptureFixture,
    failure: Exception,
    summary: str,
    logged: str,
) -> None:
    codex_home = tmp_path / ".codex"
    standalone = _skill(codex_home / "skills")
    _skill(_plugin(codex_home) / "skills")
    codex_cli.side_effect = failure
    warnings: list[str] = []

    with caplog.at_level(logging.WARNING, logger="omnigent.spec.codex_plugin_skills"):
        assert _discover(tmp_path, warnings=warnings) == {"review": standalone}

    # The menu learns what failed without host paths; the host log keeps the output.
    message = f"Codex plugin skills unavailable: {summary}"
    assert warnings == [f"{message}; see the host log for details." if logged else f"{message}."]
    assert "/" not in warnings[0]
    assert [record.getMessage() for record in caplog.records] == [
        f"{message}: {logged}" if logged else message
    ]


def test_plugin_cache_without_a_codex_binary_warns(
    tmp_path: Path, codex_cli: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: None)
    codex_home = tmp_path / ".codex"
    standalone = _skill(codex_home / "skills")
    warnings: list[str] = []
    # Without a plugin cache there are no plugin skills to miss.
    assert _discover(tmp_path, warnings=warnings) == {"review": standalone}
    assert warnings == []
    _skill(_plugin(codex_home) / "skills")
    assert _discover(tmp_path, warnings=warnings) == {"review": standalone}
    assert warnings == ["Codex plugin skills unavailable: `codex` was not found on the host."]
    codex_cli.assert_not_called()


def test_unavailable_plugin_inventory_without_a_collector_only_logs(
    tmp_path: Path, codex_cli: Mock, caplog: pytest.LogCaptureFixture
) -> None:
    _skill(_plugin(tmp_path / ".codex") / "skills")
    codex_cli.side_effect = subprocess.CalledProcessError(1, "codex")
    with caplog.at_level(logging.WARNING, logger="omnigent.spec.codex_plugin_skills"):
        assert _discover(tmp_path) == {}
    assert [record.getMessage() for record in caplog.records] == [
        "Codex plugin skills unavailable: `codex plugin list` exited 1"
    ]


@pytest.mark.parametrize("output", ["{", "[]", '{"installed": {}}', '{"installed": [null, {}]}'])
def test_malformed_plugin_inventory(tmp_path: Path, codex_cli: Mock, output: str) -> None:
    _skill(_plugin(tmp_path / ".codex") / "skills")
    codex_cli.return_value = subprocess.CompletedProcess([], 0, stdout=output)
    warnings: list[str] = []
    assert _discover(tmp_path, warnings=warnings) == {}
    # Unreadable output is a discovery failure; entries that merely fail validation are skipped.
    assert len(warnings) == (0 if output.startswith('{"installed": [') else 1)


@pytest.mark.parametrize("manifest", ["{", "[]", "{}", '{"name": 42}'])
def test_bad_plugin_manifest_does_not_hide_other_plugins(
    tmp_path: Path, codex_cli: Mock, manifest: str
) -> None:
    codex_home = tmp_path / ".codex"
    root = _plugin(codex_home)
    _skill(root / "skills")
    (root / ".codex-plugin/plugin.json").write_text(manifest)
    _skill(_plugin(codex_home, "healthy") / "skills")
    _inventory(codex_cli, _entry(), _entry("healthy"))

    assert set(_discover(tmp_path)) == {"healthy:review"}
