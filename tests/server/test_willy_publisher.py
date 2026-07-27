from __future__ import annotations

import importlib.util
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


def test_publisher_declaration_defers_to_managed_artifact_backend() -> None:
    module = _load_publisher()

    with pytest.raises(RuntimeError, match="requires the managed artifact backend"):
        module.publish_design_artifact(
            entry_path="artifacts/revenue/index.html",
            title="Revenue",
            operation="created",
        )
