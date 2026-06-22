"""Tests for host identity management (config.yaml host section)."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
import yaml

from omnigent.host.identity import load_or_create_host_identity


def test_create_identity_when_no_config(tmp_path: Path) -> None:
    """
    Verify that load_or_create generates a host section in config.yaml
    when the file does not exist.

    If the file is missing after the call, the write path is broken.
    If host_id doesn't match the format, the UUID generation is wrong.
    """
    config_path = tmp_path / "config.yaml"
    identity = load_or_create_host_identity(config_path)

    assert config_path.exists(), "config.yaml should be created on first call"
    # host_id format: host_{32 hex chars}
    assert identity.host_id.startswith("host_"), (
        f"host_id should start with 'host_', got {identity.host_id!r}"
    )
    hex_part = identity.host_id[len("host_") :]
    assert len(hex_part) == 32, f"hex portion should be 32 chars (uuid4), got {len(hex_part)}"
    int(hex_part, 16)  # raises ValueError if not valid hex

    # Name defaults to machine hostname.
    assert identity.name == socket.gethostname()


def test_load_existing_identity(tmp_path: Path) -> None:
    """
    Verify that load_or_create reads the host section from an
    existing config.yaml.

    If the returned identity doesn't match the file contents,
    the YAML parsing is broken.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "server": "http://example.com",
                "host": {"host_id": "host_aabbccdd", "name": "my-laptop"},
            }
        )
    )

    identity = load_or_create_host_identity(config_path)

    assert identity.host_id == "host_aabbccdd"
    assert identity.name == "my-laptop"


def test_identity_stable_across_calls(tmp_path: Path) -> None:
    """
    Verify that calling load_or_create twice returns the same
    host_id (the file is read, not regenerated).

    If host_id changes, the function is ignoring the existing
    host section and generating a fresh UUID every time.
    """
    config_path = tmp_path / "config.yaml"
    first = load_or_create_host_identity(config_path)
    second = load_or_create_host_identity(config_path)

    assert first.host_id == second.host_id, (
        "host_id should be stable across calls — the host section "
        "should be read on the second call, not regenerated"
    )
    assert first.name == second.name


def test_create_preserves_existing_config(tmp_path: Path) -> None:
    """
    Verify that adding the host section doesn't clobber existing
    config keys like server and profile.

    If existing keys are lost, the yaml.safe_dump is overwriting
    instead of merging.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"server": "http://example.com", "profile": "oss"}))

    identity = load_or_create_host_identity(config_path)

    with open(config_path) as f:
        data = yaml.safe_load(f)

    # Host section was added.
    assert data["host"]["host_id"] == identity.host_id
    assert data["host"]["name"] == identity.name
    # Existing keys preserved.
    assert data["server"] == "http://example.com", (
        "Existing 'server' key should survive host section creation"
    )
    assert data["profile"] == "oss", "Existing 'profile' key should survive host section creation"


def test_env_override_returns_identity_without_touching_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A server-managed sandbox host gets its identity from env vars and
    must not read or write config.yaml (managed sandboxes are
    disposable; the server owns their identity).
    """
    monkeypatch.setenv("OMNIGENT_HOST_ID", "host_env_override")
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "managed-env")
    config_path = tmp_path / "config.yaml"

    identity = load_or_create_host_identity(config_path)

    assert identity.host_id == "host_env_override"
    assert identity.name == "managed-env"
    # The identity file must not be materialized by the env path.
    assert not config_path.exists()


def test_env_override_requires_both_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Setting only one identity env var is a launcher bug — fail loud
    instead of mixing a server-chosen id with a generated name.
    """
    monkeypatch.setenv("OMNIGENT_HOST_ID", "host_env_override")
    monkeypatch.delenv("OMNIGENT_HOST_NAME", raising=False)

    with pytest.raises(ValueError, match="must be set together"):
        load_or_create_host_identity(tmp_path / "config.yaml")
