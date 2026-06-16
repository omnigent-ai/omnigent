"""Tests for llms.adapters.base — ABC enforcement."""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from omnigent.llms.adapters.base import BaseAdapter


def test_cannot_instantiate_base_adapter() -> None:
    """BaseAdapter is abstract and cannot be instantiated directly."""
    with pytest.raises(TypeError, match="abstract"):
        BaseAdapter()  # type: ignore[abstract]


def test_subclass_must_implement_chat_completions() -> None:
    """A subclass that does not implement chat_completions cannot be instantiated."""

    class IncompleteAdapter(BaseAdapter):
        pass

    with pytest.raises(TypeError, match="abstract"):
        IncompleteAdapter()  # type: ignore[abstract]


def test_concrete_subclass_can_be_instantiated() -> None:
    """A complete subclass that implements chat_completions can be instantiated."""

    class ConcreteAdapter(BaseAdapter):
        async def chat_completions(
            self,
            messages: list[dict[str, Any]],
            model: str,
            tools: list[dict[str, Any]] | None,
            stream: bool,
            extra: dict[str, Any],
            *,
            connection_params: dict[str, str] | None = None,
            timeout: int | None = None,
        ) -> dict[str, Any] | AsyncIterator[dict[str, Any]]:
            return {"choices": []}

    adapter = ConcreteAdapter()
    assert isinstance(adapter, BaseAdapter)
