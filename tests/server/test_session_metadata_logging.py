"""Harness observations identify children without inheriting a parent's harness."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from omnigent.entities import Conversation
from omnigent.server import session_metadata_logging as metadata
from tests.debug_log_helpers import capture_debug_rows


@pytest.fixture(autouse=True)
def enable_debug_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "debug_sink_enabled", lambda: True)


def _child(**kwargs: object) -> Conversation:
    return Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        root_conversation_id="root",
        parent_conversation_id="parent",
        kind="sub_agent",
        agent_id="agent",
        **kwargs,
    )


@pytest.mark.parametrize(
    ("wrapper", "harness"),
    [
        ("claude-code-native-ui-subagent", "claude-native"),
        ("codex-native-ui-subagent", "codex-native"),
        ("antigravity-native-ui-subagent", "antigravity-native"),
        ("devin-native-ui-subagent", "devin-native"),
        ("opencode-native-ui-subagent", "opencode-native"),
    ],
)
def test_native_mirror_keeps_own_identity_when_parent_spec_is_unavailable(
    wrapper: str, harness: str
) -> None:
    conv = _child(labels={"omnigent.wrapper": wrapper, "description": "private task"})
    resolver = Mock(side_effect=RuntimeError("parent bundle was deleted"))

    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(
            conv, observation="external_session_status", resolve_harness=resolver
        )

    resolver.assert_not_called()
    row = next(row for row in rows if row["event_name"] == "session_metadata")
    assert row["session_id"] == "child"
    attrs = row["attributes"]
    assert attrs["harness"] == harness
    assert attrs["harness_source"] == "native_subagent"
    assert attrs["session_kind"] == "sub_agent"
    assert attrs["parent_session_id"] == "parent"
    assert attrs["root_session_id"] == "root"
    assert "private task" not in repr(row)


def test_native_session_metadata_survives_missing_agent_spec_without_a_lookup() -> None:
    conv = Conversation(
        id="main",
        created_at=1,
        updated_at=1,
        root_conversation_id="main",
        agent_id="deleted_agent",
        labels={"omnigent.wrapper": "codex-native-ui"},
    )
    resolver = Mock(side_effect=RuntimeError("bundle missing"))
    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(conv, observation="message", resolve_harness=resolver)

    resolver.assert_not_called()
    attrs = rows[0]["attributes"]
    assert attrs["harness"] == "codex-native"
    assert attrs["harness_source"] == "native_wrapper"
    assert attrs["session_kind"] == "default"
    assert attrs["root_session_id"] == "main"
    assert "parent_session_id" not in attrs


@pytest.mark.parametrize(
    ("resolved", "canonical"), [("agy-native", "antigravity-native"), (None, None)]
)
def test_metadata_resolves_the_child_agent_harness(
    resolved: str | None, canonical: str | None
) -> None:
    conv = _child()
    resolver = Mock(return_value=resolved)
    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(conv, observation="message", resolve_harness=resolver)

    resolver.assert_called_once_with()
    attrs = rows[0]["attributes"]
    if canonical is None:
        assert "harness" not in attrs
        assert attrs["harness_resolution"] == "unknown"
    else:
        assert attrs["harness"] == canonical
        assert attrs["harness_resolution"] == "resolved"
    assert attrs["harness_source"] == "agent_spec"
    assert "harness_lookup_error_type" not in attrs


def test_acp_mirror_does_not_resolve_its_copied_parent_agent() -> None:
    conv = _child(labels={"omnigent.acp.subagent_id": "worker"})
    resolver = Mock(return_value="claude-native")

    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(
            conv, observation="external_session_status", resolve_harness=resolver
        )

    resolver.assert_not_called()
    assert rows[0]["session_id"] == "child"
    attrs = rows[0]["attributes"]
    assert "harness" not in attrs
    assert attrs["harness_resolution"] == "unknown"
    assert attrs["harness_source"] == "acp_subagent"
    assert attrs["parent_session_id"] == "parent"


@pytest.mark.parametrize(
    ("selection", "harness", "resolution"),
    [("auto", None, "deferred"), ("any", None, "deferred"), ("codex", "codex", "resolved")],
)
def test_session_override_skips_spec_resolution(
    selection: str, harness: str | None, resolution: str
) -> None:
    conv = _child(harness_override=selection)
    resolver = Mock(return_value="claude-native")
    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(conv, observation="message", resolve_harness=resolver)

    resolver.assert_not_called()
    attrs = rows[0]["attributes"]
    assert attrs.get("harness") == harness
    assert attrs["harness_resolution"] == resolution
    assert attrs["harness_source"] == "session_override"


def test_metadata_lookup_failure_is_explicit_and_does_not_fail_the_request() -> None:
    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(
            _child(),
            observation="external_session_status",
            resolve_harness=Mock(side_effect=RuntimeError("private backend details")),
        )

    attrs = rows[0]["attributes"]
    assert "harness" not in attrs
    assert attrs["harness_resolution"] == "unknown"
    assert attrs["harness_source"] == "lookup_failed"
    assert attrs["harness_lookup_error_type"] == "RuntimeError"
    assert "private backend details" not in repr(rows)


def test_disabled_sink_skips_metadata_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metadata, "debug_sink_enabled", lambda: False)
    resolver = Mock(side_effect=AssertionError("unnecessary lookup"))
    with capture_debug_rows("server") as rows:
        metadata.log_session_metadata(_child(), observation="message", resolve_harness=resolver)

    resolver.assert_not_called()
    assert rows == []


def test_logging_failure_does_not_interrupt_session_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        metadata._logger, "info", Mock(side_effect=RuntimeError("sink unavailable"))
    )
    metadata.log_session_metadata(_child(), observation="message", resolve_harness=lambda: "codex")
