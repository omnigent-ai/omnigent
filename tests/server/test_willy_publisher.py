from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _load_publisher() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "examples"
        / "willy"
        / "tools"
        / "python"
        / "publish_design_artifact.py"
    )
    spec = importlib.util.spec_from_file_location("willy_publish_design_artifact", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_publisher_resolves_virtual_path_from_managed_artifact_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_dir = tmp_path / "managed-artifacts"
    entry = artifact_dir / "revenue" / "index.html"
    entry.parent.mkdir(parents=True)
    entry.write_text("<h1>Revenue</h1>")
    (entry.parent / "app.js").write_text("console.log('ok')")
    unrelated_cwd = tmp_path / "workspace"
    unrelated_cwd.mkdir()
    monkeypatch.chdir(unrelated_cwd)
    monkeypatch.setenv("OMNIGENT_ARTIFACT_DIR", str(artifact_dir))

    module = _load_publisher()
    payload = json.loads(
        module.publish_design_artifact(
            entry_path="artifacts/revenue/index.html",
            title="Revenue",
            operation="created",
        )
    )

    assert payload["ok"] is True
    assert payload["entry_path"] == "artifacts/revenue/index.html"
    assert payload["resource_count"] == 2
