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


def test_verify_contains_only(tmp_path: Path) -> None:
    """A verify block with only contains parses; the other fields stay empty."""
    spec = parse(
        _agent_dir(tmp_path, {"spec_version": 1, "name": "a", "verify": {"contains": ["ok"]}})
    )

    assert spec.verify == VerifySpec(contains=("ok",))


def test_verify_bare_string_coerces_each_field(tmp_path: Path) -> None:
    """contains / not_contains / no_stubs / paths each accept a bare scalar."""
    spec = parse(
        _agent_dir(
            tmp_path,
            {
                "spec_version": 1,
                "name": "a",
                "verify": {
                    "contains": "ok",
                    "not_contains": "bad",
                    "no_stubs": "TODO",
                    "paths": "src/app.py",
                },
            },
        )
    )

    assert spec.verify == VerifySpec(
        contains=("ok",),
        not_contains=("bad",),
        no_stubs=("TODO",),
        paths=("src/app.py",),
    )


def test_verify_explicit_null(tmp_path: Path) -> None:
    """An explicit verify: null is treated the same as an absent block."""
    spec = parse(_agent_dir(tmp_path, {"spec_version": 1, "name": "a", "verify": None}))

    assert spec.verify is None


def test_verify_rejects_empty_command_string(tmp_path: Path) -> None:
    """An empty command string is rejected — it is never a useful gate."""
    with pytest.raises(OmnigentError, match=r"verify.commands must not contain empty strings"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"commands": [""]}},
            )
        )


def test_verify_rejects_empty_contains(tmp_path: Path) -> None:
    """An empty contains substring is rejected."""
    with pytest.raises(OmnigentError, match=r"verify.contains must not contain empty strings"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"contains": [""]}},
            )
        )


def test_verify_rejects_invalid_no_stubs_regex(tmp_path: Path) -> None:
    """An invalid no_stubs regex is rejected at parse time (fail fast)."""
    with pytest.raises(OmnigentError, match=r"verify.no_stubs: invalid regex"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"no_stubs": ["("], "paths": ["a.py"]}},
            )
        )


def test_verify_rejects_non_string_in_contains(tmp_path: Path) -> None:
    """A non-string entry in contains is a spec-load error."""
    with pytest.raises(OmnigentError, match=r"verify.contains must contain only strings"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"contains": [123]}},
            )
        )


def test_verify_rejects_non_string_in_paths(tmp_path: Path) -> None:
    """A non-string entry in paths is a spec-load error."""
    with pytest.raises(OmnigentError, match=r"verify.paths must contain only strings"):
        parse(
            _agent_dir(
                tmp_path,
                {"spec_version": 1, "name": "a", "verify": {"paths": [123]}},
            )
        )
