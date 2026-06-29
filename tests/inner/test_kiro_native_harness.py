"""Tests for the kiro-native harness app scaffold."""

from __future__ import annotations

from fastapi import FastAPI

from omnigent.runtime.harnesses import _HARNESS_MODULES


def test_kiro_native_harness_module_is_registered() -> None:
    """Runtime registry points at the importable Kiro native harness module."""
    assert _HARNESS_MODULES["kiro-native"] == "omnigent.inner.kiro_native_harness"


def test_kiro_native_harness_create_app_imports() -> None:
    """The harness module exports the required FastAPI app factory."""
    from omnigent.inner.kiro_native_harness import create_app

    assert isinstance(create_app(), FastAPI)
