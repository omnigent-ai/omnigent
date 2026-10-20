"""Selected-profile catalogs use their own cache identity and configured default."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native import app_server


@pytest.mark.parametrize("selector", ["explicit", "root-inline", "root-file"])
@pytest.mark.parametrize("change", ["catalog", "profile"])
def test_selected_profile_catalog_changes_invalidate_the_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str, change: str
) -> None:
    """A profile edit or its catalog edit invalidates only the selected configuration."""
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    root_catalog = source / "root.json"
    work_catalog = source / "work.json"
    root_catalog.write_text(json.dumps({"models": [{"slug": "root-model"}]}))
    work_catalog.write_text(json.dumps({"models": [{"slug": "work-model"}]}))
    config = f"model_catalog_json = {json.dumps(str(root_catalog))}\n"
    overlay = f'model_catalog_json = {json.dumps(str(work_catalog))}\nmodel = "work-model"\n'
    if selector != "explicit":
        config += 'profile = "work"\n'
    if selector == "root-inline":
        config += "[profiles.work]\n" + overlay
    else:
        (source / "work.config.toml").write_text(overlay)
    (source / "config.toml").write_text(config)
    launch = app_server.resolve_native_codex_launch(
        model=None, terminal_launch_args=["--profile", "work"] if selector == "explicit" else []
    )
    before = app_server.codex_catalog_fingerprint(launch, codex_path="/test/codex")

    if change == "catalog":
        work_catalog.write_text(json.dumps({"models": [{"slug": "new-work-model"}]}))
    else:
        path = source / ("config.toml" if selector == "root-inline" else "work.config.toml")
        path.write_text(path.read_text().replace("work-model", "new-work-model"))

    after = app_server.codex_catalog_fingerprint(launch, codex_path="/test/codex")
    assert after != before


def test_profile_catalog_identity_without_a_root_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file profile works without an optional root config.toml."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    catalog = tmp_path / "models.json"
    catalog.write_text('{"models": []}')
    (tmp_path / "work.config.toml").write_text(
        f"model_catalog_json = {json.dumps(str(catalog))}\n"
    )
    launch = app_server.resolve_native_codex_launch(
        model=None, terminal_launch_args=["--profile", "work"]
    )
    before = app_server.codex_catalog_fingerprint(launch, codex_path="/test/codex")

    catalog.write_text('{"models": [{"slug": "work-model"}]}')

    assert app_server.codex_catalog_fingerprint(launch, codex_path="/test/codex") != before


@pytest.mark.parametrize("selector", ["explicit", "root-inline", "root-file"])
def test_selected_profile_keeps_its_configured_hidden_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selector: str
) -> None:
    """A model omitted by the visible picker stays the configured launch default."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    config = 'model = "root-model"\n'
    overlay = 'model = "custom-hidden-model"\n'
    if selector != "explicit":
        config += 'profile = "work"\n'
    if selector == "root-inline":
        config += "[profiles.work]\n" + overlay
    else:
        (tmp_path / "work.config.toml").write_text(overlay)
    (tmp_path / "config.toml").write_text(config)

    configured = app_server._codex_picker_config(tmp_path, "work").get("model")
    marked = app_server.mark_launch_default(
        [{"id": "visible-model", "isDefault": True}], configured
    )

    assert configured == "custom-hidden-model"
    assert not any(row.get("isDefault") for row in marked)
