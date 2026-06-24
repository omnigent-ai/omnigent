"""
Tests for :func:`omnigent.runner.app._resolve_compaction_context`.

The runner sizes its compaction budget against the model a turn will
ACTUALLY run on (a per-turn ``/model`` override, else the spec model). The
cache must recompute whenever that effective model changes — in BOTH
directions. The headline regression here is the *clear-override* direction:
after a user pins ``/model small-200k`` and then clears it, the budget must
revert to the spec's declared window instead of staying pinned to the stale
override window (which under-sized the budget and over-compacted).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.llms import context_window
from omnigent.runner.app import _resolve_compaction_context

_SPEC_MODEL = "claude-opus-4-8"
_DECLARED_WINDOW = 1_000_000
_OVERRIDE_MODEL = "small-200k-model"
_OVERRIDE_WINDOW = 200_000
_COMPACTION_CFG = SimpleNamespace(name="cfg-sentinel")


def _fake_spec() -> SimpleNamespace:
    """A duck-typed spec exposing the fields the helper reads."""
    return SimpleNamespace(
        executor=SimpleNamespace(model=_SPEC_MODEL, context_window=_DECLARED_WINDOW),
        compaction=_COMPACTION_CFG,
    )


@pytest.fixture(autouse=True)
def _stub_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve the override model's window via a stubbed catalog lookup.

    The declared-window fast path never hits the catalog; only an active
    override does (it bypasses the declared window). Map the override model to
    its real window and fail loudly on any unexpected lookup.
    """

    def _catalog(model: str) -> int:
        if model == _OVERRIDE_MODEL:
            return _OVERRIDE_WINDOW
        raise AssertionError(f"unexpected catalog lookup for {model!r}")

    monkeypatch.setattr(context_window, "get_model_context_window", _catalog)


def test_cache_miss_builds_from_spec_window() -> None:
    """No cached entry → build from the declared spec window + spec model."""
    entry = _resolve_compaction_context(None, _fake_spec(), "body-model", None)
    assert entry == {
        "context_window": _DECLARED_WINDOW,
        "model": _SPEC_MODEL,
        "config": _COMPACTION_CFG,
    }


def test_override_set_resizes_to_override_window() -> None:
    """Pinning an override mid-session sizes against the override window."""
    cached = {"context_window": _DECLARED_WINDOW, "model": _SPEC_MODEL, "config": _COMPACTION_CFG}
    entry = _resolve_compaction_context(cached, _fake_spec(), "body-model", _OVERRIDE_MODEL)
    assert entry == {
        "context_window": _OVERRIDE_WINDOW,
        "model": _OVERRIDE_MODEL,
        "config": _COMPACTION_CFG,
    }


def test_override_cleared_reverts_to_declared_window() -> None:
    """Clearing an override reverts the budget to the declared spec window.

    The reviewer's bug: the old guard only recomputed while an override was
    active, so a cleared override (``turn_override=None``) kept budgeting
    against the stale 200K window forever. The effective model now reverts to
    the spec model, which differs from the cached override → recompute fires.
    """
    cached = {
        "context_window": _OVERRIDE_WINDOW,
        "model": _OVERRIDE_MODEL,
        "config": _COMPACTION_CFG,
    }
    entry = _resolve_compaction_context(cached, _fake_spec(), "body-model", None)
    assert entry is not None
    assert entry["context_window"] == _DECLARED_WINDOW, "must revert to the declared window"
    assert entry["model"] == _SPEC_MODEL


def test_no_change_returns_cached_entry_unchanged() -> None:
    """Effective model unchanged → return the same object, no recompute."""
    cached = {"context_window": _DECLARED_WINDOW, "model": _SPEC_MODEL, "config": _COMPACTION_CFG}
    entry = _resolve_compaction_context(cached, _fake_spec(), "body-model", None)
    assert entry is cached


def test_no_spec_falls_back_to_body_model() -> None:
    """With no spec, the body model drives the effective model (and window)."""
    # body model resolves via the catalog; reuse the override-model mapping.
    entry = _resolve_compaction_context(None, None, _OVERRIDE_MODEL, None)
    assert entry == {
        "context_window": _OVERRIDE_WINDOW,
        "model": _OVERRIDE_MODEL,
        "config": None,
    }
