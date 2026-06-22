"""Tests for the ``@tool`` decorator and metadata.

Tools must be defined at module scope (the decorator's
:class:`TypeError` enforces this — see ``test_decorator_rejects_nested_function``),
so each fixture function below lives at module level and tests
introspect them.
"""

from __future__ import annotations

import asyncio

import pytest
from omnigent_client.tools import (
    TOOL_MARKER_ATTR,
    ToolMetadata,
    get_tool_metadata,
    tool,
)
from omnigent_client.tools._decorator import _validate_decorator_target

# ─── Module-level tool fixtures ──────────────────────────────────────


@tool
def _t_word_count(text: str) -> int:
    """Count words."""
    return len(text.split())


@tool
def _t_greet(name: str) -> str:
    """Greet someone by name."""
    return f"hi {name}"


@tool
def _t_with_default(text: str, count: int = 1) -> str:
    """Process text."""
    return text * count


@tool
def _t_strict_default_int(x: int) -> int:
    """Doc."""
    return x


@tool(strict=False)
def _t_strict_off(x: int) -> int:
    """Doc."""
    return x


@tool
def _t_returns_str(x: int) -> str:
    """Doc."""
    return str(x)


@tool
def _t_no_return_annotation(x: int):  # type: ignore[no-untyped-def]
    """Doc."""
    return x


@tool
def _t_async_callable(x: int) -> int:
    """Async pass-through (sync body)."""
    return x


@tool
async def _t_async_def_double(x: int) -> int:
    """Double the input."""
    return x * 2


@tool
def _t_add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


# ─── name / description / schema derivation ──────────────────────────


def test_decorator_derives_name_from_function() -> None:
    """The tool name is the function's ``__name__``."""
    md = get_tool_metadata(_t_word_count)
    assert md is not None
    assert md.name == "_t_word_count"


def test_decorator_derives_description_from_docstring() -> None:
    """The function-level description comes from the docstring."""
    md = get_tool_metadata(_t_greet)
    assert md is not None
    assert md.description == "Greet someone by name."


def test_decorator_attaches_json_schema_to_marker() -> None:
    """The schema is reachable via the marker attribute."""
    md = get_tool_metadata(_t_with_default)
    assert md is not None
    schema = md.json_schema
    assert schema["type"] == "object"
    assert schema["properties"]["text"]["type"] == "string"
    assert schema["properties"]["count"]["type"] == "integer"


def test_decorator_strict_default_true() -> None:
    """Bare ``@tool`` defaults to strict=True (additionalProperties false)."""
    md = get_tool_metadata(_t_strict_default_int)
    assert md is not None
    assert md.strict is True
    # Verify the schema actually had strict normalization applied.
    assert md.json_schema["additionalProperties"] is False


def test_decorator_strict_false_opt_out() -> None:
    """``@tool(strict=False)`` skips the strict-mode normalization."""
    md = get_tool_metadata(_t_strict_off)
    assert md is not None
    assert md.strict is False
    assert "additionalProperties" not in md.json_schema


# ─── return-type capture ─────────────────────────────────────────────


def test_decorator_captures_return_annotation() -> None:
    """The function's return type is preserved in metadata."""
    md = get_tool_metadata(_t_returns_str)
    assert md is not None
    assert md.return_annotation is str


def test_decorator_no_return_annotation_is_none() -> None:
    """Missing return annotation produces ``None`` in metadata."""
    md = get_tool_metadata(_t_no_return_annotation)
    assert md is not None
    assert md.return_annotation is None


# ─── target validation (G30) ─────────────────────────────────────────


def test_decorator_accepts_module_level_def() -> None:
    """Plain ``def`` at module level is accepted."""
    assert get_tool_metadata(_t_async_callable) is not None


def test_decorator_accepts_module_level_async_def() -> None:
    """Plain ``async def`` at module level is accepted."""
    assert get_tool_metadata(_t_async_def_double) is not None


def test_decorator_rejects_lambda() -> None:
    """Applying ``@tool`` to a lambda raises ``TypeError``."""
    # Lambdas have __name__ == "<lambda>"; the decorator catches this.
    with pytest.raises(TypeError, match="lambda"):
        tool(lambda x: x)  # type: ignore[arg-type,return-value]


def test_decorator_rejects_nested_function() -> None:
    """A function defined inside another function is rejected.

    Closures do not survive subprocess invocation; the decorator
    enforces module-level definition to keep authors honest.
    """

    def outer() -> None:
        def inner(x: int) -> int:
            """Doc."""
            return x

        # Apply the decorator manually to the nested function.
        tool(inner)

    with pytest.raises(TypeError, match="nested"):
        outer()


def test_decorator_rejects_class_method() -> None:
    """Applying ``@tool`` to a class-defined method is rejected.

    Class methods would include ``self`` in the schema — the LLM
    has no way to fill that.
    """

    class C:
        def method(self, x: int) -> int:
            """Doc."""
            return x

    # C.method has __qualname__ == "test_decorator_rejects_class_method.<locals>.C.method"
    # which the validator catches via the qualname-mismatch check.
    with pytest.raises(TypeError, match="nested"):
        tool(C.method)


def test_decorator_rejects_staticmethod() -> None:
    """staticmethod is rejected explicitly."""

    def fn(x: int) -> int:
        return x

    sm = staticmethod(fn)
    with pytest.raises(TypeError, match="staticmethod"):
        tool(sm)  # type: ignore[arg-type,return-value]


def test_decorator_rejects_classmethod() -> None:
    """classmethod is rejected explicitly."""

    def fn(cls, x: int) -> int:  # type: ignore[no-untyped-def]
        return x

    cm = classmethod(fn)
    with pytest.raises(TypeError, match="classmethod"):
        tool(cm)  # type: ignore[arg-type,return-value]


def test_decorator_rejects_non_callable() -> None:
    """Applying ``@tool`` to a non-callable raises ``TypeError``."""
    with pytest.raises(TypeError, match="functions"):
        tool(42)  # type: ignore[arg-type,return-value]


def test_validate_decorator_target_directly() -> None:
    """Direct call to validator: module-level function passes."""
    # Should not raise.
    _validate_decorator_target(_t_word_count)


# ─── preserves callable behavior ────────────────────────────────────


def test_decorator_preserves_sync_callable() -> None:
    """The decorated function is still callable with its original behavior."""
    # Call as a normal function — decoration must not change the call result.
    assert _t_add(2, 3) == 5


def test_decorator_preserves_async_callable() -> None:
    """An async-decorated function is still awaitable."""
    result = asyncio.run(_t_async_def_double(7))
    assert result == 14


# ─── metadata access ────────────────────────────────────────────────


def test_marker_attribute_name_constant() -> None:
    """The marker attr is exposed as a module constant for the loader."""
    assert isinstance(TOOL_MARKER_ATTR, str)
    assert TOOL_MARKER_ATTR  # non-empty


def test_get_tool_metadata_returns_none_for_undecorated() -> None:
    """Plain functions return None from get_tool_metadata."""

    def fn(x: int) -> int:
        return x

    assert get_tool_metadata(fn) is None


def test_get_tool_metadata_returns_metadata_object() -> None:
    """Decorated functions return a real ToolMetadata instance."""
    md = get_tool_metadata(_t_word_count)
    assert isinstance(md, ToolMetadata)


def test_metadata_marker_attribute_present() -> None:
    """The decorator attaches metadata via the marker attribute."""
    assert hasattr(_t_word_count, TOOL_MARKER_ATTR)
    assert isinstance(getattr(_t_word_count, TOOL_MARKER_ATTR), ToolMetadata)


# ─── docstring documents the outermost-decorator rule (G34) ─────────


def test_decorator_documents_outermost_requirement() -> None:
    """The decorator's docstring mentions the outermost-decorator rule."""
    # Reading the docstring guarantees the rule survives refactors.
    assert tool.__doc__ is not None
    doc_lower = tool.__doc__.lower()
    assert "outermost" in doc_lower or "wraps" in doc_lower, (
        "The decorator docstring should document the outermost-decorator rule."
    )
