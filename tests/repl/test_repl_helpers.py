"""Unit tests for small stateful and lookup helpers in ``omnigent.repl._repl``.

Covers the approval state machine, the ``/help`` listing, and the ``/model``
provider-default and catalog-validation helpers.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from rich.console import Console

from omnigent.repl._repl import (
    COMMANDS,
    _ApprovalState,
    _ApprovalVerdict,
    _cmd_help,
    _model_validation_warning,
    _resolve_provider_default_model,
)

# ── approval state ───────────────────────────────────────────


async def test_approval_starts_idle_and_ignores_verdicts() -> None:
    state = _ApprovalState()

    assert state.pending is False
    assert state.resolve_verdict(_ApprovalVerdict.APPROVE_ONCE) is False


@pytest.mark.parametrize(
    ("verdict", "approved"),
    [
        (_ApprovalVerdict.APPROVE_ONCE, True),
        (_ApprovalVerdict.APPROVE_ALWAYS, True),
        (_ApprovalVerdict.REFUSE, False),
    ],
)
async def test_resolve_verdict_completes_the_pending_future(
    verdict: _ApprovalVerdict, approved: bool
) -> None:
    state = _ApprovalState()
    future = state.begin("policy_a", "tool_call")
    assert state.pending is True

    assert state.resolve_verdict(verdict) is True

    assert future.result() is approved
    assert state.pending is False
    # Resolving is one-shot: a second verdict finds nothing pending.
    assert state.resolve_verdict(verdict) is False


async def test_approve_always_caches_only_that_policy_and_phase() -> None:
    state = _ApprovalState()
    state.begin("policy_a", "tool_call")
    state.resolve_verdict(_ApprovalVerdict.APPROVE_ALWAYS)

    assert state.is_pre_approved("policy_a", "tool_call") is True
    assert state.is_pre_approved("policy_a", "request") is False
    assert state.is_pre_approved("policy_b", "tool_call") is False


@pytest.mark.parametrize("verdict", [_ApprovalVerdict.APPROVE_ONCE, _ApprovalVerdict.REFUSE])
async def test_other_verdicts_do_not_populate_the_always_cache(verdict: _ApprovalVerdict) -> None:
    state = _ApprovalState()
    state.begin("policy_a", "tool_call")
    state.resolve_verdict(verdict)

    assert state.is_pre_approved("policy_a", "tool_call") is False


async def test_begin_refuses_a_superseded_pending_approval() -> None:
    state = _ApprovalState()
    first = state.begin("policy_a", "tool_call")

    second = state.begin("policy_b", "request")

    assert first.result() is False
    assert second is not first and not second.done()
    # The replacement carries its own identity for an "always" answer.
    state.resolve_verdict(_ApprovalVerdict.APPROVE_ALWAYS)
    assert state.is_pre_approved("policy_b", "request") is True
    assert state.is_pre_approved("policy_a", "tool_call") is False


async def test_cancel_refuses_pending_approval_but_keeps_always_cache() -> None:
    state = _ApprovalState()
    state.remember_always("policy_a", "tool_call")
    future = state.begin("policy_b", "request")

    state.cancel()

    assert future.result() is False
    assert state.pending is False
    assert state.is_pre_approved("policy_a", "tool_call") is True
    # Cancelling with nothing pending is harmless.
    state.cancel()


# ── /help ────────────────────────────────────────────────────


class _Host:
    def __init__(self) -> None:
        self.outputs: list[object] = []

    def output(self, renderable: object) -> None:
        self.outputs.append(renderable)


class _Fmt:
    muted = "dim"
    accent = "cyan"


async def _help_text() -> str:
    host = _Host()
    await _cmd_help("", None, None, host, _Fmt())  # type: ignore[arg-type]
    console = Console(width=200, no_color=True, file=None)
    with console.capture() as cap:
        for renderable in host.outputs:
            console.print(renderable)
    return cap.get()


async def test_help_groups_commands_and_hides_aliases() -> None:
    text = await _help_text()
    lines = text.splitlines()

    for heading in ("Chat", "Context", "Display", "Diagnostics", "Help"):
        assert heading in [line.strip() for line in lines]
    for name in ("/new", "/switch", "/model", "/theme", "/logs", "/help", "/quit"):
        assert name in text
    # Aliases are accepted at the prompt but not listed.
    assert not any(line.split() and line.split()[0] in ("/?", "/exit") for line in lines)


async def test_help_lists_ungrouped_commands_under_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(COMMANDS, "/zzz-new", ("A brand new command", lambda *a: None))

    text = await _help_text()

    other = text.split("Other", 1)[1]
    assert "/zzz-new" in other and "A brand new command" in other


# ── /model helpers ───────────────────────────────────────────


def _key_provider(family: str, default_model: str | None) -> dict[str, object]:
    block: dict[str, object] = {
        "base_url": "https://api.example.com/v1",
        "api_key_ref": f"env:{family.upper()}_KEY",
    }
    if default_model is not None:
        block["models"] = {"default": default_model}
    return {"kind": "key", family: block}


def test_provider_default_model_prefers_anthropic_family() -> None:
    entry = _key_provider("anthropic", "claude-x")
    entry["openai"] = {
        "base_url": "https://api.example.com/v1",
        "api_key_ref": "env:OPENAI_KEY",
        "models": {"default": "gpt-x"},
    }

    assert _resolve_provider_default_model({"providers": {"mixed": entry}}, "mixed") == "claude-x"


def test_provider_default_model_falls_back_to_openai_family() -> None:
    config = {"providers": {"oa": _key_provider("openai", "gpt-x")}}

    assert _resolve_provider_default_model(config, "oa") == "gpt-x"


def test_provider_default_model_is_none_without_pin_or_provider() -> None:
    config = {"providers": {"bare": _key_provider("openai", None)}}

    assert _resolve_provider_default_model(config, "bare") is None
    assert _resolve_provider_default_model(config, "missing") is None
    assert _resolve_provider_default_model({}, "bare") is None


@pytest.fixture()
def stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve a fixed anthropic catalog (tests disable the real catalog lookup)."""
    monkeypatch.setattr(
        "omnigent.onboarding.providers.get_chat_models",
        lambda provider: [SimpleNamespace(name="claude-known")] if provider == "anthropic" else [],
    )


def test_model_validation_accepts_catalog_models(stub_catalog: None) -> None:
    assert _model_validation_warning("anthropic/claude-known") is None


def test_model_validation_warns_for_unlisted_model_of_known_provider(stub_catalog: None) -> None:
    warning = _model_validation_warning("anthropic/definitely-not-a-real-model")

    assert warning is not None
    assert "isn't in the local model catalog" in warning
    assert "'anthropic/definitely-not-a-real-model'" in warning


def test_model_validation_skips_providers_without_a_catalog(stub_catalog: None) -> None:
    # An empty catalog means "nothing to validate against", not "unknown model".
    assert _model_validation_warning("openai/anything") is None


def test_model_validation_informs_for_unknown_provider_prefix(stub_catalog: None) -> None:
    warning = _model_validation_warning("gateway-only/some-model")

    assert warning is not None
    assert "isn't a catalog model" in warning
