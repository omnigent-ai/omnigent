"""Tests for parsing the ``memory:`` block (omnigent.spec.parser)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import parse
from omnigent.spec.validator import validate


def _write(tmp_path: Path, memory: object) -> Path:
    config: dict[str, object] = {"spec_version": 1, "name": "mem-agent"}
    if memory is not None:
        config["memory"] = memory
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def test_memory_absent_is_none(tmp_path: Path) -> None:
    spec = parse(_write(tmp_path, None))
    assert spec.memory is None


def test_memory_disabled_needs_no_api_key(tmp_path: Path) -> None:
    spec = parse(_write(tmp_path, {"enabled": False}))
    assert spec.memory is not None
    assert spec.memory.enabled is False
    # Auto flags still default on so flipping ``enabled`` later is enough.
    assert spec.memory.auto_recall is True
    assert spec.memory.auto_retain is True
    assert spec.memory.api_key is None


def test_memory_enabled_parses_all_fields(tmp_path: Path) -> None:
    spec = parse(
        _write(
            tmp_path,
            {
                "enabled": True,
                "api_key": "secret-key",
                "api_url": "https://example.test",
                "bank_id": "acme",
                "budget": "high",
                "max_tokens": 2048,
                "recall_timeout": 3.5,
                "auto_retain": False,
            },
        )
    )
    assert spec.memory is not None
    mem = spec.memory
    assert mem.enabled is True
    assert mem.api_key == "secret-key"
    assert mem.api_url == "https://example.test"
    assert mem.bank_id == "acme"
    assert mem.budget == "high"
    assert mem.max_tokens == 2048
    assert mem.recall_timeout == 3.5
    assert mem.auto_recall is True
    assert mem.auto_retain is False
    assert mem.provider == "hindsight"  # default when unspecified


def test_memory_api_key_env_expanded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HINDSIGHT_API_KEY", "from-env")
    spec = parse(_write(tmp_path, {"enabled": True, "api_key": "${HINDSIGHT_API_KEY}"}))
    assert spec.memory is not None
    assert spec.memory.api_key == "from-env"


def test_memory_api_key_unset_env_fails_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
    with pytest.raises(OmnigentError, match=r"HINDSIGHT_API_KEY"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "${HINDSIGHT_API_KEY}"}))


def test_memory_enabled_without_api_key_raises(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.api_key is required"):
        parse(_write(tmp_path, {"enabled": True}))


def test_memory_invalid_budget_raises(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.budget must be one of"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "k", "budget": "turbo"}))


def test_memory_non_mapping_raises(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory: must be a mapping"):
        parse(_write(tmp_path, ["not", "a", "mapping"]))


def test_memory_rejects_boolean_max_tokens(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.max_tokens must be an integer"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "k", "max_tokens": True}))


def test_memory_rejects_non_string_bank_id(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.bank_id must be a string"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "k", "bank_id": 123}))


def test_memory_explicit_provider_hindsight(tmp_path: Path) -> None:
    spec = parse(_write(tmp_path, {"enabled": True, "api_key": "k", "provider": "hindsight"}))
    assert spec.memory is not None
    assert spec.memory.provider == "hindsight"


def test_memory_rejects_unknown_provider(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.provider must be one of"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "k", "provider": "goodmemory"}))


def test_memory_rejects_non_string_provider(tmp_path: Path) -> None:
    with pytest.raises(OmnigentError, match=r"memory.provider must be one of"):
        parse(_write(tmp_path, {"enabled": True, "api_key": "k", "provider": 123}))


def test_validate_rejects_non_positive_bounds(tmp_path: Path) -> None:
    spec = parse(
        _write(
            tmp_path,
            {"enabled": True, "api_key": "k", "max_tokens": 0, "recall_timeout": 0.0},
        )
    )
    result = validate(spec)
    assert not result.valid
    paths = {error.path for error in result.errors}
    assert "memory.max_tokens" in paths
    assert "memory.recall_timeout" in paths
