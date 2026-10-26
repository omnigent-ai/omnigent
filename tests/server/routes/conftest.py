"""Shared fixtures for the server route tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from omnigent.server.routes import imports as imports_module


@pytest.fixture(autouse=True)
def _forget_interrupted_imports() -> Iterator[None]:
    """An interrupted local import remembers ids to skip on its re-run, per host."""
    imports_module._CONTINUE_SKIP_IDS.clear()
    yield
    imports_module._CONTINUE_SKIP_IDS.clear()
