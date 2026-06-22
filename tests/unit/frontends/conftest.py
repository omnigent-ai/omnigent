"""Shared fixtures for frontend tests."""

from __future__ import annotations

import os

import pytest

# --llm-api-key is declared at the top-level tests/conftest.py so it's
# shared across tests/e2e/ and tests/frontends/. This conftest only
# installs the resulting key into OPENAI_API_KEY when present.


@pytest.fixture(autouse=True)
def _set_api_key(request: pytest.FixtureRequest) -> None:
    """Set OPENAI_API_KEY from --llm-api-key if provided."""
    key = request.config.getoption("--llm-api-key", default=None)
    if key is not None:
        os.environ["OPENAI_API_KEY"] = key
