"""Effort (thought_level) config-option switch for the generic ACP executor.

Mirrors the model-switch tests in test_acp_executor.py. Fixtures follow Grok's
live ``session/new`` shape: a ``model`` option plus a ``reasoning_effort`` option
with ``category:"thought_level"`` and values ``{xhigh, high, medium, low}``.
"""

from __future__ import annotations

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor


def _ex() -> AcpExecutor:
    return AcpExecutor(AcpAgentConfig(command="x"))


def _grok_options() -> list[dict]:
    return [
        {
            "id": "model",
            "category": "model",
            "type": "select",
            "currentValue": "grok-4.6",
            "options": [{"value": "grok-4.6"}, {"value": "grok-4.5"}],
        },
        {
            "id": "reasoning_effort",
            "category": "thought_level",
            "type": "select",
            "currentValue": "medium",
            "options": [
                {"value": "xhigh"},
                {"value": "high"},
                {"value": "medium"},
                {"value": "low"},
            ],
        },
    ]


def test_note_config_options_captures_category_and_choices() -> None:
    ex = _ex()
    ex._note_config_options(_grok_options())
    assert ex._config_id_for_category("thought_level") == "reasoning_effort"
    effort = ex._config_options["reasoning_effort"]
    assert effort["category"] == "thought_level"
    assert [c["value"] for c in effort["options"]] == ["xhigh", "high", "medium", "low"]
    # Model bookkeeping still works (unchanged path).
    assert "model" in ex._config_option_ids and ex._active_model == "grok-4.6"


@pytest.mark.asyncio
async def test_apply_effort_sets_thought_level_via_set_config_option() -> None:
    ex = _ex()
    ex._note_config_options(_grok_options())
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        calls.append({"method": method, "params": params})
        return {
            "result": {
                "configOptions": [
                    {
                        "id": "reasoning_effort",
                        "category": "thought_level",
                        "currentValue": params["value"],
                    }
                ]
            }
        }

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "high")
    assert calls == [
        {
            "method": "session/set_config_option",
            "params": {"sessionId": "s1", "configId": "reasoning_effort", "value": "high"},
        }
    ]
    assert ex._effort_switch_supported is True


@pytest.mark.asyncio
async def test_apply_effort_noop_when_agent_has_no_thought_level_option() -> None:
    # Devin case: only a model option, effort encoded in the model id.
    ex = _ex()
    ex._note_config_options(
        [{"id": "model", "category": "model", "currentValue": "claude-opus-5-high"}]
    )
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        calls.append(params)
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "high")
    assert calls == []  # nothing sent
    assert ex._effort_switch_supported is False  # latched: options known, none is thought_level


@pytest.mark.asyncio
async def test_apply_effort_skips_value_the_agent_does_not_offer() -> None:
    ex = _ex()
    ex._note_config_options(_grok_options())
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        calls.append(params)
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "ultra")  # not in {xhigh,high,medium,low}
    assert calls == []
    assert ex._effort_switch_supported is True  # skipping an unoffered value must not latch


@pytest.mark.asyncio
async def test_apply_effort_noop_when_already_current() -> None:
    ex = _ex()
    ex._note_config_options(_grok_options())  # current effort = "medium"
    calls: list[dict] = []

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        calls.append(params)
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "medium")
    assert calls == []


@pytest.mark.asyncio
async def test_apply_effort_records_value_when_setter_echoes_nothing() -> None:
    # Some agents accept the switch without echoing configOptions; record the
    # requested value locally so the same effort isn't re-sent every turn.
    ex = _ex()
    ex._note_config_options(_grok_options())
    calls: list[str] = []

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        calls.append(params["value"])
        return {"result": {}}  # accepted, no echoed configOptions

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "high")
    assert ex._config_options["reasoning_effort"]["currentValue"] == "high"
    # A second apply of the now-current value is a no-op (no second RPC).
    await ex._apply_effort_override("s1", "high")
    assert calls == ["high"]


@pytest.mark.asyncio
async def test_apply_effort_latches_off_on_rejection() -> None:
    ex = _ex()
    ex._note_config_options(_grok_options())

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        return {"error": {"code": -32002, "message": "nope"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_effort_override("s1", "high")
    assert ex._effort_switch_supported is False
