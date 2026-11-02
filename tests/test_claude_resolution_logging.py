from __future__ import annotations

import hashlib
import json

import pytest

from omnigent.models.claude_resolution_logging import log_claude_model_resolution
from tests.debug_log_helpers import capture_debug_rows


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_resolution_preserves_comparable_fingerprints_without_private_values() -> None:
    private_values = ["private-selection", "private-pin", "private-rewrite", "private-reported"]
    with capture_debug_rows("runner") as rows:
        log_claude_model_resolution(
            phase="native_launch",
            selection_source="session_override",
            selected_model=private_values[0],
            resolved_model="opus",
            reported_model=private_values[3],
            model_env={
                "ANTHROPIC_DEFAULT_OPUS_MODEL": private_values[1],
                "PRIVATE_TOKEN": "private-secret",
            },
            model_overrides={"opus": private_values[2]},
            picker_values=["opus"],
            catalog_available=True,
            catalog_stale=True,
        )
    assert len(rows) == 1
    assert rows[0]["event_name"] == "claude_model_resolution"
    attrs = rows[0]["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["selected_model_sha256"] == _digest(private_values[0])
    assert attrs["resolved_model_sha256"] == _digest("opus")
    assert attrs["reported_model_sha256"] == _digest(private_values[3])
    assert attrs["selected_alias_pin_sha256"] == _digest(private_values[1])
    assert attrs["selected_canonical_override_sha256"] == _digest(private_values[2])
    assert attrs["alias_pin_count"] == attrs["canonical_override_count"] == "1"
    assert attrs["picker_exact_selected"] == "False"
    assert attrs["picker_exact_resolved"] == "True"
    assert attrs["catalog_available"] == "True"
    assert attrs["catalog_stale"] == "True"
    assert attrs["runtime_version"]
    serialized = json.dumps(rows)
    assert all(
        value not in serialized for value in [*private_values, "private-secret", "PRIVATE_TOKEN"]
    )


@pytest.mark.parametrize(
    ("arguments", "resolved", "count"),
    [
        (["--model", "private-explicit"], "private-explicit", 1),
        (["--model=private-explicit"], "private-explicit", 1),
        (["--model", "one", "--model=two"], None, 2),
        (["--model"], None, 1),
        (["--model", "--resume"], None, 1),
        (["--model="], None, 1),
        (["--resume"], "private-default", 0),
    ],
)
def test_explicit_arguments_match_precedence_without_guessing_ambiguous_model(
    arguments: list[str], resolved: str | None, count: int
) -> None:
    with capture_debug_rows("runner") as rows:
        log_claude_model_resolution(
            phase="native_launch",
            selection_source="agent_spec",
            selected_model="private-selected",
            resolved_model="private-default",
            explicit_launch_args=arguments,
        )
    attrs = rows[0]["attributes"]
    assert isinstance(attrs, dict)
    assert attrs.get("resolved_model_sha256") == (_digest(resolved) if resolved else None)
    assert attrs["selection_source"] == ("launch_arguments" if count else "agent_spec")
    assert attrs["explicit_model_argument_count"] == str(count)
    assert "catalog_available" not in attrs
    assert attrs["picker_vocabulary_present"] == "False"
    assert "private-" not in json.dumps(rows)


def test_unknown_labels_and_oversized_model_are_bounded() -> None:
    with capture_debug_rows("runner") as rows:
        log_claude_model_resolution(
            phase="sdk_query",
            selection_source="private-source",
            selected_model="x" * 4097,
            resolved_model=None,
        )
        log_claude_model_resolution(
            phase="private-phase",
            selection_source="cli_default",
            selected_model=None,
            resolved_model=None,
        )
    assert len(rows) == 1
    attrs = rows[0]["attributes"]
    assert isinstance(attrs, dict)
    assert attrs["selection_source"] == "unknown"
    assert attrs["selected_model_sha256"] == "oversized"


def test_broken_logging_does_not_change_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("sink unavailable")

    monkeypatch.setattr("omnigent.models.claude_resolution_logging._logger.info", broken)
    log_claude_model_resolution(
        phase="sdk_failure",
        selection_source="cli_default",
        selected_model=None,
        resolved_model=None,
    )
