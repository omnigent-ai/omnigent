"""Tests for the onboarding ``list_builtin_tools`` helper's optional-extra gates.

The helper keeps its own hand-maintained table of builtins (it must not import
``omnigent.tools.builtins``), so its optional-extra gates can drift from the
real registry. These tests pin the gates.
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Generator
from importlib.machinery import ModuleSpec

import pytest

import omnigent.onboarding.agent.tools.python.list_builtin_tools as list_tools_mod


def _reload_with_specs(
    monkeypatch: pytest.MonkeyPatch,
    present: set[str],
) -> None:
    """Reload the helper with only ``present`` optional SDKs visible.

    ``find_spec`` is patched rather than the packages installed/uninstalled,
    because the gates probe via :func:`importlib.util.find_spec` (no import).

    :param monkeypatch: pytest monkeypatch fixture.
    :param present: Optional SDK module names that should appear installed.
    """
    real_find_spec = importlib.util.find_spec
    optional = {"nimble_python", "hindsight_client"}

    def fake_find_spec(name: str, package: str | None = None) -> ModuleSpec | None:
        if name in optional:
            return ModuleSpec(name, None) if name in present else None
        return real_find_spec(name, package)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    importlib.reload(list_tools_mod)


@pytest.fixture(autouse=True)
def _restore_module() -> Generator[None, None, None]:
    """Reload the helper against the real environment after each test."""
    yield
    importlib.reload(list_tools_mod)


def test_nimble_tools_absent_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the ``nimble`` extra neither Nimble tool is advertised.

    ``nimble_extract`` needs no client at runtime, but it needs the same Nimble
    account as ``nimble_research``, so the extra gates the pair: otherwise the
    assistant recommends a tool the agent author has no credentials for.
    """
    _reload_with_specs(monkeypatch, present=set())

    assert "nimble_extract" not in list_tools_mod._TOOL_CLASSES
    assert "nimble_research" not in list_tools_mod._TOOL_CLASSES


def test_nimble_tools_present_when_sdk_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the ``nimble`` extra installed both Nimble tools are advertised."""
    _reload_with_specs(monkeypatch, present={"nimble_python"})

    assert "nimble_extract" in list_tools_mod._TOOL_CLASSES
    assert "nimble_research" in list_tools_mod._TOOL_CLASSES


def test_hindsight_tools_gated_on_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Hindsight gate keys off ``hindsight-client``, independently of Nimble."""
    hindsight = {"hindsight_retain", "hindsight_recall", "hindsight_reflect"}

    _reload_with_specs(monkeypatch, present=set())
    assert not hindsight & set(list_tools_mod._TOOL_CLASSES)

    _reload_with_specs(monkeypatch, present={"hindsight_client"})
    assert hindsight <= set(list_tools_mod._TOOL_CLASSES)
    assert "nimble_extract" not in list_tools_mod._TOOL_CLASSES


def test_unconditional_tools_always_advertised(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tools with no optional dependency survive every gate combination."""
    _reload_with_specs(monkeypatch, present=set())

    assert {"web_search", "web_fetch", "list_files", "upload_file"} <= set(
        list_tools_mod._TOOL_CLASSES
    )
