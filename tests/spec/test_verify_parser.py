"""Tests for parsing the top-level ``verify:`` block."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.spec.parser import parse
from omnigent.spec.types import VerifySpec


def _agent_dir(tmp_path: Path, config: dict) -> Path:
    """Write *config* as config.yaml under *tmp_path* and return the dir."""
    (tmp_path / "config.yaml").write_text(yaml.dump(config))
    return tmp_path


def test_verify_absent_yields_none(tmp_path: Path) -> None:
    """An agent without a verify block has verify=None — no behavior change."""
    spec = parse(_agent_dir(tmp_path, {"spec_version": 1, "name": "a"}))

    assert spec.verify is None


def test_verify_block_parses(tmp_path: Path) -> None:
    """A full verify block maps onto VerifySpec fields."""
    spec = parse(
        _agent_dir(
            tmp_path,
            {
                "spec_version": 1,
                "name": "a",
                "verify": {
                    "commands": ["pytest", "ruff check ."],
                    "contains": ["passed"],
                    "not_contains": ["error"],
                    "no_stubs": ["TODO|FIXME"],
                    "paths": ["src/app.py"],
                },
            },
        )
    )

    assert spec.verify == VerifySpec(
        commands=("pytest", "ruff check ."),
        contains=("passed",),
        not_contains=("error",),
        no_stubs=("TODO|FIXME",),
        paths=("src/app.py",),
    )


def test_verify_commands_accepts_bare_string(tmp_path: Path) -> None:
    """A scalar commands value is coerced to a single-element tuple."""
    spec = parse(
        _agent_dir(tmp_path, {"spec_version": 1, "name": "a", "verify": {"commands": "pytest"}}),
    )

    assert spec.verify is not None
    assert spec.verify.commands == ("pytest",)


def test_verify_non_mapping_rejected(tmp_path: Path) -> None:
    """A non-mapping verify block is a spec-load error."""
    with pytest.raises(OmnigentError, match=r"verify: must be a mapping"):
        parse(_agent_dir(tmp_path, {"spec_version": 1, "name": "a", "verify": ["nope"]}))


def test_verify_no_checks_rejected(tmp_path: Path) -> None:
    """A verify block declaring no checks is pointless and rejected."""
    with pytest.raises(OmnigentError, match=r"must declare at least one check"):
        parse(
            _agent_dir(tmp_path, {"spec_version": 1, "name": "a", "verify": {"paths": ["x.py"]}})
        )


def test_verify_no_stubs_without_paths_rejected(tmp_path: Path) -> None:
    """no_stubs without paths would scan nothing — rejected at load time."""
    with pytest.raises(OmnigentError, match=r"verify.no_stubs requires verify.paths"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"no_stubs": ["TODO"]}},
            )
        )


def test_verify_non_string_command_rejected(tmp_path: Path) -> None:
    """A non-string entry in commands is a spec-load error."""
    with pytest.raises(OmnigentError, match=r"verify.commands must contain only strings"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"commands": [123]}},
            )
        )


def test_verify_unknown_key_rejected(tmp_path: Path) -> None:
    """An unknown verify key (e.g. a typo like ``command:`` vs ``commands:``) is rejected."""
    with pytest.raises(OmnigentError, match=r"verify: unknown key"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"command": ["pytest"]}},
            )
        )
