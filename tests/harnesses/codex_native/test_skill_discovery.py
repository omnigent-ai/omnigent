"""Plugin discovery through Codex's real model-free runtime catalog."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.harnesses.codex_native import skill_discovery
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
    client.request.return_value = {"result": {"data": [{"skills": [{"name": "plugin:skill"}]}]}}
    if isinstance(failure, ValueError):
        client.request.return_value = {"result": {"data": None}}
    elif failure is not None:
        client.request.side_effect = failure
    monkeypatch.setattr(skill_discovery, "_start_codex_model_discovery_process", start)
    monkeypatch.setattr(skill_discovery, "_stop_codex_model_discovery_process", stop)
    monkeypatch.setattr(skill_discovery, "_wait_for_discovery_listener", AsyncMock())
    monkeypatch.setattr(skill_discovery, "CodexAppServerClient", Mock(return_value=client))

    if failure is None:
        assert skill_discovery.loaded_codex_skills("/test/codex", tmp_path, tmp_path) == [
            {"name": "plugin:skill"}
        ]
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
