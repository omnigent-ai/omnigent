from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import parse


def _parse_memory(tmp_path: Path, memory: object):
    (tmp_path / "config.yaml").write_text(
        yaml.dump({"spec_version": 1, "name": "memory-agent", "memory": memory})
    )
    return parse(tmp_path).memory


def test_memory_is_disabled_when_omitted(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: no-memory\n")

    assert parse(tmp_path).memory is None


def test_parse_memory_control_plane_config(tmp_path: Path) -> None:
    memory = _parse_memory(
        tmp_path,
        {
            "mode": "explicit_capture",
            "max_context_chars": 8000,
            "providers": [
                {
                    "provider": "qm-notebook",
                    "scopes": ["personal"],
                    "recall": "ambient",
                    "capture": "explicit",
                    "fail_open": True,
                    "timeout_ms": 500,
                    "max_results": 12,
                    "max_chars": 5000,
                },
                {
                    "provider": "hindsight",
                    "scopes": ["conversation"],
                    "recall": "conditional",
                },
                {
                    "provider": "company-brain",
                    "scopes": ["org"],
                    "recall": "explicit",
                },
            ],
        },
    )

    assert memory is not None
    assert memory.mode == "explicit_capture"
    assert memory.max_context_chars == 8000
    assert [provider.provider for provider in memory.providers] == [
        "qm-notebook",
        "hindsight",
        "company-brain",
    ]
    assert memory.providers[0].scopes == ["personal"]
    assert memory.providers[0].capture == "explicit"
    assert memory.providers[1].timeout_ms == 1000
    assert memory.providers[2].capture == "off"


@pytest.mark.parametrize(
    ("memory", "match"),
    [
        ([], r"memory must be a YAML mapping"),
        ({"unexpected": True}, r"memory has unknown fields: unexpected"),
        ({"mode": "on"}, r"memory\.mode must be one of"),
        ({"max_context_chars": True}, r"memory\.max_context_chars must be an integer"),
        ({"max_context_chars": 24001}, r"must be between 1 and 24000"),
        ({"providers": {}}, r"memory\.providers must be a list"),
    ],
)
def test_parse_memory_rejects_invalid_root_config(
    tmp_path: Path, memory: object, match: str
) -> None:
    with pytest.raises(OmnigentError, match=match):
        _parse_memory(tmp_path, memory)


@pytest.mark.parametrize(
    ("provider", "match"),
    [
        ({"provider": "unknown", "scopes": ["personal"]}, r"provider must be one of"),
        ({"provider": "qm-notebook", "scopes": []}, r"scopes must be a non-empty list"),
        (
            {"provider": "qm-notebook", "scopes": ["personal", "personal"]},
            r"scopes must not contain duplicates",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["org"], "capture": "automatic"},
            r"capture must be one of",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["org"], "capture": "review"},
            r"capture must be 'off' for org scope",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["personal"], "capture": "automatic"},
            r"capture must be one of",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["conversation"]},
            r"must not use conversation scope",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["team"]},
            r"scopes entries must be one of",
        ),
        (
            {"provider": "company-brain", "scopes": ["personal"]},
            r"must be exactly \['org'\]",
        ),
        (
            {"provider": "company-brain", "scopes": ["org"], "capture": "review"},
            r"capture must be 'off' for company-brain",
        ),
        (
            {"provider": "hindsight", "scopes": ["personal"], "capture": "review"},
            r"capture must be 'off'; governed Hindsight writes are not implemented",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["personal"], "timeout_ms": 0},
            r"timeout_ms must be between 1 and 30000",
        ),
        (
            {"provider": "qm-notebook", "scopes": ["personal"], "extra": True},
            r"has unknown fields: extra",
        ),
    ],
)
def test_parse_memory_rejects_invalid_provider_config(
    tmp_path: Path, provider: dict[str, object], match: str
) -> None:
    with pytest.raises(OmnigentError, match=match):
        _parse_memory(tmp_path, {"providers": [provider]})


def test_parse_memory_rejects_duplicate_provider(tmp_path: Path) -> None:
    provider = {"provider": "qm-notebook", "scopes": ["personal"]}

    with pytest.raises(OmnigentError, match=r"may only be configured once"):
        _parse_memory(tmp_path, {"providers": [provider, provider]})
