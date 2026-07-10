"""Tests for the top-level ``context_providers:`` spec block."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import _parse_context_providers, parse
from omnigent.spec.types import FunctionRef


def _agent_dir(tmp_path: Path, config: dict) -> Path:
    config.setdefault("spec_version", 1)
    config.setdefault("name", "ctx-test")
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


# ── via the public parser (integration) ──────────────────────────────────────


def test_absent_block_is_none(tmp_path: Path) -> None:
    spec = parse(_agent_dir(tmp_path, {}))
    assert spec.context_providers is None


def test_parse_full_spec(tmp_path: Path) -> None:
    config = {
        "context_providers": [
            "pkg.mod.recall",
            {
                "type": "function",
                "function": {"path": "pkg.mod.facts", "arguments": {"k": 1}},
            },
        ],
    }
    spec = parse(_agent_dir(tmp_path, config))
    assert spec.context_providers == [
        FunctionRef(path="pkg.mod.recall", arguments=None),
        FunctionRef(path="pkg.mod.facts", arguments={"k": 1}),
    ]


# ── the parser helper directly ───────────────────────────────────────────────


def test_bare_string() -> None:
    assert _parse_context_providers(["pkg.mod.recall"]) == [
        FunctionRef(path="pkg.mod.recall", arguments=None)
    ]


def test_path_arguments_dict() -> None:
    assert _parse_context_providers([{"path": "pkg.mod.recall", "arguments": {"d": "ops"}}]) == [
        FunctionRef(path="pkg.mod.recall", arguments={"d": "ops"})
    ]


def test_function_wrapper() -> None:
    assert _parse_context_providers(
        [{"type": "function", "function": {"path": "pkg.mod.recall"}}]
    ) == [FunctionRef(path="pkg.mod.recall", arguments=None)]


def test_none_and_empty_are_none() -> None:
    assert _parse_context_providers(None) is None
    assert _parse_context_providers([]) is None


def test_non_list_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"must be a list"):
        _parse_context_providers("pkg.mod.recall")


def test_empty_path_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"non-empty"):
        _parse_context_providers([""])


def test_missing_path_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"`path`"):
        _parse_context_providers([{"arguments": {"k": 1}}])


def test_bad_arguments_rejected() -> None:
    with pytest.raises(OmnigentError, match=r"`arguments`"):
        _parse_context_providers([{"path": "pkg.mod.recall", "arguments": "nope"}])
