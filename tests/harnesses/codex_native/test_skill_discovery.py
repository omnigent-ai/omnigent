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


def test_shared_plugins_isolated_through_real_app_server_startup(tmp_path):
    import json
    import sys
    from concurrent.futures import ThreadPoolExecutor

    from omnigent.preference_sync import (
        PreferenceNamespace,
        SharedPreferenceEntry,
        SharedPreferenceKey,
    )
    from tests._helpers.live_server import isolated_process_env
    from tests._helpers.preferences import preference_snapshot

    codex = os.environ.get("OMNIGENT_CODEX_PATH") or shutil.which("codex")
    if not codex:
        pytest.skip("Codex CLI required for the real plugin-loading contract")
    source, workspace, marketplace = (
        tmp_path / name for name in ("source", "workspace", "market")
    )
    source.mkdir()
    workspace.mkdir()
    install_codex_plugin(
        codex,
        source,
        workspace,
        marketplace,
        marketplace_name="preferences-lab",
        plugin_name="preferences-check",
        skill_name="check",
    )
    before = (source / "config.toml").read_bytes()
    script = """
import asyncio, json, os, sys
from pathlib import Path
from omnigent.harnesses.codex_native.app_server import (
    CodexNativeAppServer, CodexAppServerClient, _allocate_loopback_port,
)
from omnigent.preference_models import adopted_model
async def run():
    root = Path(sys.argv[2])
    app = CodexNativeAppServer(codex_path=sys.argv[1], socket_path=root / "socket",
        codex_home=root / "codex", env=dict(os.environ), config_overrides=[],
        cwd=Path(sys.argv[3]), bridge_dir=root / "bridge", trust_project=True,
        pinned_model=adopted_model("codex-native", Path(sys.argv[3])),
        listen_url=f"ws://127.0.0.1:{_allocate_loopback_port()}")
    app.bridge_dir.mkdir()
    await app.start()
    try:
        client = CodexAppServerClient(ws_url=app.listen_url, client_name="preference-test")
        await client.connect()
        try:
            response = await client.request(
                "skills/list", {"cwds": [sys.argv[3]], "forceReload": True},
            )
            config = await client.request("config/read", {})
            print(json.dumps({"skills": response, "config": config}))
        finally:
            await client.close()
    finally:
        await app.close()
asyncio.run(run())
"""

    def launch(enabled):
        root = tmp_path / str(enabled)
        model = f"fixture-model-{enabled}"
        snapshot = preference_snapshot(
            root,
            identity=PreferenceNamespace(
                authority_id="test", user_id="alice", workspace_id=str(enabled)
            ),
            shared=[
                SharedPreferenceEntry(
                    key=SharedPreferenceKey(kind="model", name="model"), version=1, value=model
                ),
                SharedPreferenceEntry(
                    key=SharedPreferenceKey(
                        kind="plugin",
                        name={
                            "ecosystem": "codex",
                            "source": "preferences-lab",
                            "name": "preferences-check",
                        },
                    ),
                    version=1,
                    value=enabled,
                ),
            ],
        )
        environment = isolated_process_env(
            root / "home",
            {
                "CODEX_HOME": str(source),
                "OMNIGENT_PREFERENCE_SNAPSHOT": str(snapshot),
                "OMNIGENT_CODEX_PATH": codex,
            },
            base_env={"PATH": os.environ["PATH"]},
        )
        result = subprocess.run(
            [sys.executable, "-c", script, codex, str(root), str(workspace)],
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        response = json.loads(result.stdout.splitlines()[-1])
        assert response["config"]["result"]["config"]["model"] == model, response["config"]
        skills = [
            skill for entry in response["skills"]["result"]["data"] for skill in entry["skills"]
        ]
        active = [
            skill
            for skill in skills
            if skill.get("enabled") is True and "/plugins/cache/" in skill.get("path", "")
        ]
        assert bool(active) is enabled, skills
        assert all("preferences-check" in skill["path"] for skill in active)
        return root

    with ThreadPoolExecutor(max_workers=2) as pool:
        roots = list(pool.map(launch, [True, False]))
    assert len(roots) == 2
    assert (source / "config.toml").read_bytes() == before
