"""Tests for the ephemeral App Platform configuration wrapper."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_ENTRYPOINT = Path(__file__).parents[2] / "deploy" / "digitalocean" / "entrypoint.py"


def _load_entrypoint() -> ModuleType:
    spec = importlib.util.spec_from_file_location("digitalocean_entrypoint", _ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generate_config_uses_environment_without_persisting_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_PUBLIC_URL", "https://example.ondigitalocean.app/")
    monkeypatch.setenv("DIGITALOCEAN_REGION", "blr1")
    monkeypatch.setenv("DIGITALOCEAN_WORKSPACE_SIZE_GB", "200")
    monkeypatch.setenv("DIGITALOCEAN_TOKEN", "server-only-secret")

    config_path = _load_entrypoint().generate_config()
    raw = config_path.read_text(encoding="utf-8")
    config = yaml.safe_load(raw)

    assert config["sandbox"]["server_url"] == "https://example.ondigitalocean.app"
    assert config["sandbox"]["digitalocean"]["region"] == "blr1"
    assert config["sandbox"]["digitalocean"]["workspace"]["size_gb"] == 200
    assert "server-only-secret" not in raw
    assert os.stat(config_path).st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("OMNIGENT_PUBLIC_URL", "http://insecure.example.com", "public https"),
        ("DIGITALOCEAN_WORKSPACE_SIZE_GB", "zero", "positive integer"),
        ("DIGITALOCEAN_WORKSPACE_MOUNT_PATH", "/", "absolute non-root"),
    ],
)
def test_generate_config_rejects_unsafe_values(
    monkeypatch: pytest.MonkeyPatch,
    variable: str,
    value: str,
    message: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_PUBLIC_URL", "https://example.ondigitalocean.app")
    monkeypatch.setenv(variable, value)

    with pytest.raises(RuntimeError, match=message):
        _load_entrypoint().generate_config()
