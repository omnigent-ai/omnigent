"""Fixtures confined to the Codex session tests."""

from __future__ import annotations

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _stub_catalog_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.models.model_catalog.resolve_catalog_model",
        lambda provider_name, *, family, **kwargs: SimpleNamespace(
            model_id=f"catalog-{provider_name}-{family}-default"
        ),
    )
