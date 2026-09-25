"""Compare isolated model discovery with a real Codex CLI using its source home."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server


@pytest.mark.skipif(shutil.which("codex") is None, reason="requires the Codex CLI")
@pytest.mark.parametrize("relative", [False, True], ids=["absolute-catalog", "relative-catalog"])
@pytest.mark.parametrize("override", [False, True], ids=["configured-default", "launch-default"])
async def test_codex_source_catalog_and_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool, override: bool
) -> None:
    """The isolated probe matches Codex's own visible choices and effective default."""
    codex = shutil.which("codex")
    assert codex is not None
    source = tmp_path / "codex-home"
    source.mkdir()
    env = {
        key: value
        for key, value in app_server._clean_codex_env().items()
        if not key.startswith(("OPENAI_", "DATABRICKS_"))
    }
    env["CODEX_HOME"] = str(source)
    bundled = json.loads(
        subprocess.run(
            [codex, "debug", "models", "--bundled"],
            capture_output=True,
            check=True,
            env=env,
            timeout=15,
        ).stdout
    )
    template = next(model for model in bundled["models"] if model["visibility"] == "list")
    models = [
        {
            **template,
            "slug": slug,
            "display_name": slug,
            "visibility": visibility,
            "supported_in_api": True,
            "availability_nux": None,
            "upgrade": None,
        }
        for slug, visibility in [
            ("catalog-first", "list"),
            ("configured-default", "list"),
            ("hidden-model", "hide"),
        ]
    ]
    catalog = source / "models.json"
    catalog.write_text(json.dumps({**bundled, "models": models}))
    marker = tmp_path / "mcp-started"
    config = (
        'model = "configured-default"\n'
        f"model_catalog_json = {json.dumps(catalog.name if relative else str(catalog))}\n"
        'model_provider = "local"\n'
        '[model_providers.local]\nname = "Local"\n'
        'base_url = "http://127.0.0.1:1/v1"\nwire_api = "responses"\n'
        '[mcp_servers.unrelated]\ncommand = "touch"\n'
        f"args = [{json.dumps(str(marker))}]\n"
    )
    config_path = source / "config.toml"
    config_path.write_text(config)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda: source)
    monkeypatch.setattr(app_server, "_clean_codex_env", lambda: dict(env))
    # Resolve the override through Codex's config/read, without an Omnigent pin.
    launch = app_server.NativeCodexLaunch(
        ['model="catalog-first"'] if override else [], None, None
    )

    with monkeypatch.context() as direct:
        direct.setattr(app_server, "_probe_codex_home", lambda overrides: source)
        direct_rows = await asyncio.wait_for(
            app_server.probe_codex_model_options(codex_path=codex, launch=launch), timeout=20
        )

    rows = await asyncio.wait_for(
        app_server.probe_codex_model_options(codex_path=codex, launch=launch), timeout=20
    )

    assert rows == direct_rows
    assert app_server._probe_codex_home(launch.config_overrides) != source
    assert [row["id"] for row in rows] == ["catalog-first", "configured-default"]
    expected_default = "catalog-first" if override else "configured-default"
    assert [row["id"] for row in rows if row.get("isDefault")] == [expected_default]
    assert config_path.read_text() == config
    assert not marker.exists()
