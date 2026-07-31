"""Unit tests for the smart-routing usage telemetry events."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.telemetry.model_labels import model_family, model_tier

# ── model_labels ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-opus-4-8", "claude"),
        ("claude-sonnet-5", "claude"),
        ("gpt-5.5", "gpt"),
        ("gemini-3-pro", "gemini"),
        ("qwen3-coder", "qwen"),
        ("acme-internal-review-llm", "other"),
        ("", None),
        (None, None),
    ],
)
def test_model_family(model: str | None, expected: str | None) -> None:
    assert model_family(model) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("databricks-claude-opus-4-8", "opus"),
        ("claude-sonnet-5", "sonnet"),
        ("claude-haiku-4-5", "haiku"),
        ("gpt-5.4-mini", "mini"),
        ("gemini-3-flash", "flash"),
        ("gemini-3-pro", "pro"),
        ("gpt-5.5", "other"),
        ("acme-project-zephyr", "other"),
        ("acme-internal-review-llm", "other"),
        (None, None),
    ],
)
def test_model_tier(model: str | None, expected: str | None) -> None:
    assert model_tier(model) == expected


def test_model_labels_never_return_the_raw_id() -> None:
    secret = "acme-project-zephyr-internal-endpoint"
    assert secret not in (model_family(secret), model_tier(secret))


# ── record_routing_decision ─────────────────────────────────────────────────


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Capture events handed to the pipeline instead of queueing them.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: List that collects each emitted event dataclass.
    """
    import omnigent.telemetry.routing as _routing

    events: list[Any] = []
    monkeypatch.setattr(_routing, "emit", events.append)
    monkeypatch.setattr("omnigent.telemetry.installation_id.get_installation_id", lambda: "inst-1")
    return events


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        # A routed spawn: every identity field lands on the event, and the
        # model id is reduced to family + tier before it leaves the process.
        (
            {
                "scope": "native_subagent",
                "harness": "codex",
                "action": "rewrite",
                "applied": True,
                "model": "databricks-claude-opus-4-8",
                "raw_model": "opus",
                "overrode_agent_model": False,
                "decision_id": "dec-1",
            },
            {
                "session_id": "conv_1",
                "installation_id": "inst-1",
                "scope": "native_subagent",
                "harness": "codex",
                "action": "rewrite",
                "applied": True,
                "model_family": "claude",
                "model_tier": "opus",
                "raw_model_resolved": True,
                "overrode_agent_model": False,
                "decision_id": "dec-1",
            },
        ),
        # No model was picked: the derived labels stay null rather than
        # inventing a family, and no raw model was resolved.
        (
            {
                "scope": "native_subagent",
                "harness": "claude-native",
                "action": "allow",
                "applied": False,
                "model": None,
                "raw_model": None,
                "overrode_agent_model": False,
                "decision_id": "dec-3",
            },
            {
                "model_family": None,
                "model_tier": None,
                "raw_model_resolved": False,
            },
        ),
    ],
)
def test_record_routing_decision_fields(
    captured: list[Any],
    kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    from omnigent.telemetry.routing import record_routing_decision

    record_routing_decision("conv_1", **kwargs)
    (event,) = captured
    assert type(event).__name__ == "RoutingDecisionEvent"
    for field, want in expected.items():
        assert getattr(event, field) == want, field


@pytest.mark.parametrize(
    ("kwargs", "expected_params", "absent_from_wire"),
    [
        # A private endpoint name must never reach the wire, and neither the
        # rationale nor the prompt is ever carried.
        (
            {
                "scope": "turn",
                "harness": "claude-native",
                "action": "allow",
                "applied": False,
                "model": "acme-project-zephyr-endpoint",
                "raw_model": None,
                "overrode_agent_model": False,
                "decision_id": "dec-2",
            },
            {"scope": "turn", "action": "allow"},
            ("acme-project-zephyr-endpoint", "rationale", "prompt"),
        ),
        # Routing fields land in ``params``; ids are promoted to top level.
        (
            {
                "scope": "child_session",
                "harness": "codex",
                "action": "redirect",
                "applied": True,
                "model": "gpt-5.4-mini",
                "raw_model": None,
                "overrode_agent_model": True,
                "decision_id": "dec-5",
            },
            {
                "scope": "child_session",
                "action": "redirect",
                "model_family": "gpt",
                "model_tier": "mini",
                "overrode_agent_model": True,
            },
            ("gpt-5.4-mini", "rationale", "prompt"),
        ),
    ],
)
def test_routing_decision_wire_format(
    captured: list[Any],
    kwargs: dict[str, Any],
    expected_params: dict[str, Any],
    absent_from_wire: tuple[str, ...],
) -> None:
    from omnigent.telemetry.client import _build_record
    from omnigent.telemetry.routing import record_routing_decision

    record_routing_decision("conv_1", **kwargs)
    record = _build_record(captured[0])
    wire = json.dumps(record)
    for needle in absent_from_wire:
        assert needle not in wire
    data = record["data"]
    assert data["event_name"] == "RoutingDecisionEvent"
    assert data["installation_id"] == "inst-1"
    assert data["session_id"] == "conv_1"
    params = json.loads(data["params"])
    for key, want in expected_params.items():
        assert params[key] == want, key
    # The session id is promoted, never duplicated into params.
    assert "session_id" not in params


def test_record_routing_decision_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.telemetry.routing as _routing

    def _boom(_event: Any) -> None:
        raise RuntimeError("gateway down")

    monkeypatch.setattr(_routing, "emit", _boom)
    _routing.record_routing_decision(
        "conv_1",
        scope="turn",
        harness=None,
        action="allow",
        applied=False,
        model=None,
        raw_model=None,
        overrode_agent_model=False,
        decision_id="dec-4",
    )


# ── record_routing_setting_changed ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "user_id"),
    [
        ("on", "alice@example.com"),
        ("off", "alice@example.com"),
        ("default", "alice@example.com"),
        # No user attached: the anon id stays absent rather than being a
        # hash of the empty string.
        ("on", None),
    ],
)
def test_record_routing_setting_changed(
    captured: list[Any], value: str, user_id: str | None
) -> None:
    from omnigent.telemetry.routing import (
        SETTING_SUBAGENT_ROUTING,
        record_routing_setting_changed,
    )

    record_routing_setting_changed(
        "conv_1",
        setting=SETTING_SUBAGENT_ROUTING,
        value=value,
        user_id=user_id,
    )
    (event,) = captured
    assert type(event).__name__ == "RoutingSettingChangedEvent"
    assert event.setting == "subagent_routing"
    assert event.value == value
    if user_id is None:
        assert event.anon_user_id is None
    else:
        assert event.anon_user_id is not None
        assert len(event.anon_user_id) == 16
        assert "alice" not in event.anon_user_id


# ── call sites ──────────────────────────────────────────────────────────────


class _StubStore:
    def append(self, _session_id: str, _items: list[Any]) -> list[Any]:
        return []


@pytest.mark.asyncio
async def test_server_decision_records_once_and_skips_parent_mirror(
    captured: list[Any],
) -> None:
    from omnigent.server.routes._sessions.helpers import _emit_server_routing_decision

    verdict = {"rationale": "cheap task", "applied": True, "raw_model": "sonnet"}
    decision_id = await _emit_server_routing_decision(
        "conv_child",
        _StubStore(),  # type: ignore[arg-type]
        "databricks-claude-sonnet-5",
        verdict,
        scope="child_session",
        harness="claude-sdk",
    )
    assert decision_id is not None
    # Mirror into the parent transcript: same decision, no second event.
    await _emit_server_routing_decision(
        "conv_parent",
        _StubStore(),  # type: ignore[arg-type]
        "databricks-claude-sonnet-5",
        verdict,
        agent="reviewer",
        scope="child_session",
        harness="claude-sdk",
        decision_id=decision_id,
    )
    (event,) = captured
    assert event.session_id == "conv_child"
    assert event.scope == "child_session"
    assert event.action == "rewrite"
    assert event.model_tier == "sonnet"
    assert event.raw_model_resolved is True
    # The judge's rationale stays in the transcript only.
    assert not hasattr(event, "rationale")


@pytest.mark.asyncio
async def test_subagent_route_records_the_decision(captured: list[Any]) -> None:
    from omnigent.runner.subagent_routing import SubagentRouteRequest, resolve_subagent_route

    class _Caps:
        routing_client = None
        routing_settings = None

    await resolve_subagent_route(
        "conv_1",
        SubagentRouteRequest(harness="claude-native", task_name="code-reviewer"),
        caps=_Caps(),
    )
    (event,) = captured
    assert event.session_id == "conv_1"
    assert event.scope == "native_subagent"
    assert event.overrode_agent_model is False
    # Free-form spawn text never reaches the pipeline.
    assert "code-reviewer" not in json.dumps(_wire(event))


def _wire(event: Any) -> dict[str, Any]:
    from omnigent.telemetry.client import _build_record

    return _build_record(event)


def test_session_created_event_carries_routing_enabled() -> None:
    from omnigent.telemetry.events import SessionCreatedEvent

    event = SessionCreatedEvent(
        installation_id=None,
        session_id="sess_1",
        agent_id="ag_1",
        harness="claude-sdk",
        surface="web",
        anon_user_id=None,
        host_installation_id=None,
        is_fork=False,
        is_sub_agent=False,
    )
    assert event.routing_enabled is False
