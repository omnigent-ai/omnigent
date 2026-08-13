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
    # host_id format: a bare 32-char hex uuid4 (no prefix).
    assert len(identity.host_id) == 32, (
        f"host_id should be a bare 32-char hex uuid, got {identity.host_id!r}"
    )
    int(identity.host_id, 16)  # raises ValueError if not valid hex

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
                "host": {"host_id": "d6d0ccebce7b4b706d21e23696bb462a", "name": "my-laptop"},
            }
        )
    )

    identity = load_or_create_host_identity(config_path)

    assert identity.host_id == "d6d0ccebce7b4b706d21e23696bb462a"
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


def test_name_only_config_gets_generated_host_id(tmp_path: Path) -> None:
    """
    A config.yaml that names the host but omits host_id should keep the
    provided name and get a freshly generated host_id — the name must
    not be clobbered by the machine hostname.

    If the name comes back as the hostname, the partial section was
    discarded instead of completed.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"server": "http://example.com", "host": {"name": "my-custom-host"}})
    )

    identity = load_or_create_host_identity(config_path)

    assert identity.name == "my-custom-host", (
        "provided host name must be preserved, not replaced by the hostname"
    )
    assert len(identity.host_id) == 32, "a host_id should be generated when absent"
    int(identity.host_id, 16)  # raises ValueError if not valid hex

    # The generated id is persisted so it's stable across calls.
    with open(config_path) as f:
        data = yaml.safe_load(f)
    assert data["host"]["host_id"] == identity.host_id
    assert data["host"]["name"] == "my-custom-host"
    assert data["server"] == "http://example.com", "existing keys must survive"


def test_name_only_config_host_id_stable_across_calls(tmp_path: Path) -> None:
    """
    Once a host_id is generated for a name-only config, a second call
    must return the same id (the persisted section is read, not
    regenerated).
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"host": {"name": "my-custom-host"}}))

    first = load_or_create_host_identity(config_path)
    second = load_or_create_host_identity(config_path)

    assert first.host_id == second.host_id
    assert first.name == second.name == "my-custom-host"


def test_env_override_returns_identity_without_touching_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A server-managed sandbox host gets its identity from env vars and
    must not read or write config.yaml (managed sandboxes are
    disposable; the server owns their identity).
    """
    monkeypatch.setenv("OMNIGENT_HOST_ID", "329c39d03aad39ccf2f8597d596676bd")
    monkeypatch.setenv("OMNIGENT_HOST_NAME", "managed-env")
    config_path = tmp_path / "config.yaml"

    identity = load_or_create_host_identity(config_path)

    assert identity.host_id == "329c39d03aad39ccf2f8597d596676bd"
    assert identity.name == "managed-env"
    # The identity file must not be materialized by the env path.
    assert not config_path.exists()


def test_env_override_requires_both_vars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Setting only one identity env var is a launcher bug — fail loud
    instead of mixing a server-chosen id with a generated name.
    """
    monkeypatch.setenv("OMNIGENT_HOST_ID", "329c39d03aad39ccf2f8597d596676bd")
    monkeypatch.delenv("OMNIGENT_HOST_NAME", raising=False)

    with pytest.raises(ValueError, match="must be set together"):
        load_or_create_host_identity(tmp_path / "config.yaml")
