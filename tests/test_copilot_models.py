"""Tests for the pre-launch Copilot model catalog (``copilot_models.py``)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from omnigent.copilot_models import copilot_model_options


@pytest.fixture(autouse=True)
def _isolate_copilot_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Deterministic auth resolution: empty config home, no ambient tokens."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)


class _Policy:
    def __init__(self, state: str) -> None:
        self.state = state


class _Info:
    def __init__(
        self,
        id: str,
        name: str | None = None,
        *,
        policy: _Policy | None = None,
        efforts: list[str] | None = None,
    ) -> None:
        self.id = id
        self.name = name
        self.policy = policy
        self.supported_reasoning_efforts = efforts


def _install_fake_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    infos: list[_Info] | None = None,
    list_exc: Exception | None = None,
) -> dict[str, Any]:
    """Install a fake ``copilot`` module; return observed call state."""
    state: dict[str, Any] = {"client_kwargs": [], "started": 0, "stopped": 0}

    class _FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            state["client_kwargs"].append(kwargs)

        async def start(self) -> None:
            state["started"] += 1

        async def stop(self) -> None:
            state["stopped"] += 1

        async def list_models(self) -> list[_Info]:
            if list_exc is not None:
                raise list_exc
            return list(infos or [])

    module = types.ModuleType("copilot")
    module.CopilotClient = _FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "copilot", module)
    return state


@pytest.mark.asyncio
async def test_maps_backend_models_to_picker_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend ``ModelInfo`` rows map to the picker option shape, in order."""
    state = _install_fake_sdk(
        monkeypatch,
        infos=[
            _Info("claude-sonnet-4.5", "Claude Sonnet 4.5"),
            _Info("gpt-5.4", None),
        ],
    )
    options = await copilot_model_options()
    assert options == [
        {
            "id": "claude-sonnet-4.5",
            "model": "claude-sonnet-4.5",
            "displayName": "Claude Sonnet 4.5",
            "isDefault": False,
        },
        {"id": "gpt-5.4", "model": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": False},
    ]
    assert state["started"] == 1
    # The bundled CLI subprocess must always be reaped.
    assert state["stopped"] == 1


@pytest.mark.asyncio
async def test_policy_filters_only_explicitly_blocked_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only an explicit non-enabled policy filters a model out.

    A ``policy`` object exists only for models GitHub tracks a terms opt-in
    for; ``auto`` and e.g. ``gpt-5.3-codex`` ship without one and are usable,
    so absence must allow (an enabled-only allowlist would drop them).
    """
    _install_fake_sdk(
        monkeypatch,
        infos=[
            _Info("auto", "Auto"),
            _Info("claude-sonnet-5", "Claude Sonnet 5", policy=_Policy("enabled")),
            _Info("gpt-5.3-codex", "GPT-5.3-Codex"),
            _Info("gpt-5.4", "GPT-5.4", policy=_Policy("disabled")),
            _Info("gemini-3.6-flash", "Gemini 3.6 Flash", policy=_Policy("unconfigured")),
        ],
    )
    options = await copilot_model_options()
    assert [option["id"] for option in options] == ["auto", "claude-sonnet-5", "gpt-5.3-codex"]


@pytest.mark.asyncio
async def test_auto_is_marked_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """``auto`` carries the default marker; ``models.list`` itself has none."""
    _install_fake_sdk(
        monkeypatch,
        infos=[_Info("auto", "Auto"), _Info("gpt-5.4", "GPT-5.4")],
    )
    options = await copilot_model_options()
    assert [(option["id"], option["isDefault"]) for option in options] == [
        ("auto", True),
        ("gpt-5.4", False),
    ]


@pytest.mark.asyncio
async def test_efforts_are_emitted_in_ladder_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-model efforts ride along in canonical ladder order.

    The backend's own order is not contractual, and unknown values append
    after the known ladder instead of being dropped. A model without efforts
    (``efforts=None``) omits the key entirely.
    """
    _install_fake_sdk(
        monkeypatch,
        infos=[
            _Info("claude-sonnet-5", efforts=["max", "low", "xhigh", "medium", "high"]),
            _Info("gpt-5.6-terra", efforts=["high", "supersonic", "none"]),
            _Info("claude-haiku-4.5"),
        ],
    )
    options = await copilot_model_options()
    assert options[0]["supportedReasoningEfforts"] == [
        {"reasoningEffort": "low"},
        {"reasoningEffort": "medium"},
        {"reasoningEffort": "high"},
        {"reasoningEffort": "xhigh"},
        {"reasoningEffort": "max"},
    ]
    assert options[1]["supportedReasoningEfforts"] == [
        {"reasoningEffort": "none"},
        {"reasoningEffort": "high"},
        {"reasoningEffort": "supersonic"},
    ]
    assert "supportedReasoningEfforts" not in options[2]


@pytest.mark.asyncio
async def test_auto_login_when_no_token_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    """No configured or ambient token: the client keeps the SDK auto-login.

    ``github_token=None`` is what leaves ``use_logged_in_user`` on, so the
    catalog lists what the Copilot CLI's logged-in user may use, the working
    path for GitHub Enterprise data-residency seats.
    """
    state = _install_fake_sdk(monkeypatch, infos=[])
    await copilot_model_options()
    assert state["client_kwargs"][0]["github_token"] is None


@pytest.mark.asyncio
async def test_ambient_token_is_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no stored token, an ambient ``GH_TOKEN`` reaches the client."""
    state = _install_fake_sdk(monkeypatch, infos=[])
    monkeypatch.setenv("GH_TOKEN", "gho_ambient")
    await copilot_model_options()
    assert state["client_kwargs"][0]["github_token"] == "gho_ambient"


@pytest.mark.asyncio
async def test_backend_failure_still_reaps_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``list_models`` failure propagates but never orphans the CLI subprocess."""
    state = _install_fake_sdk(monkeypatch, list_exc=RuntimeError("not authenticated"))
    with pytest.raises(RuntimeError):
        await copilot_model_options()
    assert state["stopped"] == 1
