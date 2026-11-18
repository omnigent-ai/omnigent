"""Plugin discovery through Codex's real model-free runtime catalog."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harnesses.codex_native import skill_discovery
from omnigent.inner.codex_executor import populate_codex_skills_from_bundle
from omnigent.spec.skill_sources import SkillSourceContext, resolve_harness_skills
from tests._helpers.codex_plugins import install_codex_plugin


@pytest.mark.parametrize(
    "failure", [None, ValueError("bad data"), RuntimeError("rpc"), TimeoutError()]
)
async def test_catalog_query_closes_client_and_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: Exception | None
) -> None:
    start = AsyncMock()
    stop = AsyncMock()
    client = Mock(connect=AsyncMock(), close=AsyncMock(), request=AsyncMock())
    client.request.return_value = {
        "result": {
            "data": [
                {
                    "skills": [{"name": "plugin:skill"}],
                    "errors": [{"path": "/test/config.toml", "message": "bad config"}],
                }
            ]
        }
    }
    if isinstance(failure, ValueError):
        client.request.return_value = {"result": {"data": None}}
    elif failure is not None:
        client.request.side_effect = failure
    monkeypatch.setattr(skill_discovery, "_start_codex_model_discovery_process", start)
    monkeypatch.setattr(skill_discovery, "_stop_codex_model_discovery_process", stop)
    monkeypatch.setattr(skill_discovery, "_wait_for_discovery_listener", AsyncMock())
    monkeypatch.setattr(skill_discovery, "CodexAppServerClient", Mock(return_value=client))

    if failure is None:
        errors: list[str] = []
        assert skill_discovery.loaded_codex_skills(
            "/test/codex", tmp_path, tmp_path, errors=errors
        ) == [{"name": "plugin:skill"}]
        assert errors == ["/test/config.toml: bad config"]
    else:
        with pytest.raises(type(failure)):
            skill_discovery.loaded_codex_skills("/test/codex", tmp_path, tmp_path)
    assert start.call_args.kwargs["env"]["CODEX_HOME"] == str(tmp_path)
    assert start.call_args.kwargs["cwd"] == tmp_path
    client.request.assert_awaited_once_with(
        "skills/list", {"cwds": [str(tmp_path)], "forceReload": True}
    )
    client.close.assert_awaited_once()
    stop.assert_awaited_once_with(start.return_value)


def test_installed_plugin_survives_marketplace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI required for the real plugin-loading contract")
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", codex)
    home, workspace, marketplace = (tmp_path / name for name in ("codex", "workspace", "market"))
    home.mkdir()
    workspace.mkdir()
    install_codex_plugin(
        codex,
        home,
        workspace,
        marketplace,
        marketplace_name="discovery-lab",
        plugin_name="discovery-check",
        skill_name="check",
    )
    env = {**os.environ, "CODEX_HOME": str(home)}
    ctx = SkillSourceContext((workspace,), tmp_path, "all", None, codex_home=home)

    def selected_version() -> str | None:
        skills = [
            s
            for s in resolve_harness_skills(ctx, "codex-native")
            if s.name == "discovery-check:check"
        ]
        if not skills:
            return None
        assert len(skills) == 1
        return skills[0].skill_dir.parent.parent.name

    assert selected_version() == "1.0.0"
    marketplace.rename(tmp_path / "market-moved")
    inventory = subprocess.run(
        [codex, "plugin", "list", "--json"],
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert inventory.returncode != 0
    assert selected_version() == "1.0.0"
    cache = home / "plugins/cache/discovery-lab/discovery-check"
    for version in ["2.0.0-rc.2", "2.0.0-rc.10"]:
        shutil.copytree(cache / "1.0.0", cache / version)
    assert selected_version() == "2.0.0-rc.10"
    shutil.copytree(cache / "1.0.0", cache / "local")
    assert selected_version() == "local"
    config = home / "config.toml"
    enabled_config = config.read_text()
    config.write_text(enabled_config.replace("enabled = true", "enabled = false"))
    assert selected_version() is None
    config.write_text(enabled_config)
    assert selected_version() == "local"
    config.write_text("")
    assert selected_version() is None
    # An unreadable config fails `plugin list`. Codex 0.158 and later still answer
    # skills/list and report the failure in its errors; 0.142 rejects the request.
    # Either way the menu must say the plugin skills are missing.
    config.write_text(f'{enabled_config}model = "unterminated\n')
    warnings: list[str] = []
    broken = SkillSourceContext(
        (workspace,), tmp_path, "all", None, codex_home=home, warnings=warnings
    )
    assert resolve_harness_skills(broken, "codex-native") == []
    failed = "`codex plugin list` exited 1 and `codex app-server`"
    assert warnings in (
        [
            f"Codex plugin skills may be incomplete: {failed} reported errors loading skills; "
            "see the host log for details."
        ],
        [
            f"Codex plugin skills unavailable: {failed} could not list skills; "
            "see the host log for details."
        ],
    )


def test_menu_matches_codex_repo_skill_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI required for the real skill-precedence contract")
    home = tmp_path / "home"
    codex_home = home / ".codex"
    repo = tmp_path / "repo"
    workspace = repo / "app"
    workspace.mkdir(parents=True)
    (repo / ".git").mkdir()
    repo_skills = repo / ".agents" / "skills"
    copies = [
        repo_skills / "dup",
        codex_home / "skills" / "dup",
        home / ".agents" / "skills" / "dup",
    ]
    for skill_dir in [*copies, repo_skills / "repo-only"]:
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_dir.name}\ndescription: {skill_dir}\n---\nbody\n"
        )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)

    def codex_skill_dirs(launch_home: Path, name: str) -> list[Path]:
        loaded = skill_discovery.loaded_codex_skills(codex, launch_home, workspace)
        return [Path(skill["path"]).parent for skill in loaded if skill["name"] == name]

    # Codex lists every same-named copy and ranks the repo copy above the user ones.
    listed = codex_skill_dirs(codex_home, "dup")
    assert sorted(listed) == sorted(copy.resolve() for copy in copies)
    assert listed[0] == copies[0].resolve()
    ctx = SkillSourceContext((workspace,), home, "all", None, codex_home=codex_home)
    for harness in ("codex", "codex-native"):
        menu = {skill.name: skill.skill_dir for skill in resolve_harness_skills(ctx, harness)}
        assert menu["dup"].resolve() == listed[0]

    # A "none" launch links nothing, yet Codex still loads the repo skills.
    private_home = tmp_path / "private-codex-home"
    private_home.mkdir()
    populate_codex_skills_from_bundle(private_home, None, "none")
    assert codex_skill_dirs(private_home, "repo-only") == [(repo_skills / "repo-only").resolve()]
    unfiltered = replace(ctx, skills_filter="none")
    for harness in ("codex", "codex-native"):
        assert {skill.name for skill in resolve_harness_skills(unfiltered, harness)} == {
            "dup",
            "repo-only",
        }
