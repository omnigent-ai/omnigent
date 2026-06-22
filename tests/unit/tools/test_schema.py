"""Tests for schema derivation from typed function signatures."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal

import pytest
from omnigent_client.tools._schema import build_function_schema
from pydantic import BaseModel, Field

# ─── primitives ─────────────────────────────────────────────────────


def test_schema_primitive_str_param() -> None:
    """A single ``str`` param produces a string-typed required property."""

    def fn(text: str) -> str:
        """Echo the text."""
        return text

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["type"] == "object"
    assert schema["properties"]["text"]["type"] == "string"
    assert "text" in schema["required"]


def test_schema_primitive_int_with_default() -> None:
    """A param with a default is NOT marked required (in non-strict)."""

    def fn(count: int = 5) -> int:
        """Return the count."""
        return count

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["properties"]["count"]["type"] == "integer"
    # Default value should be reflected in the schema.
    assert schema["properties"]["count"].get("default") == 5
    # Non-strict: defaulted params are not required.
    assert "count" not in schema.get("required", [])


def test_schema_multiple_primitive_params() -> None:
    """Multiple params with mixed types build correct properties."""

    def fn(name: str, age: int, active: bool = True) -> str:
        """Format a record."""
        return f"{name} {age} {active}"

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["properties"]["name"]["type"] == "string"
    assert schema["properties"]["age"]["type"] == "integer"
    assert schema["properties"]["active"]["type"] == "boolean"
    assert sorted(schema["required"]) == ["age", "name"]


# ─── pydantic models ─────────────────────────────────────────────────


class _Person(BaseModel):
    """A person record (test fixture)."""

    name: str
    age: int
    email: str | None = None


def test_schema_pydantic_model_param() -> None:
    """A ``BaseModel`` param produces a ``$ref`` into ``$defs``."""

    def fn(person: _Person) -> str:
        """Format the person."""
        return person.name

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    # The person property should reference the model's schema.
    person_schema = schema["properties"]["person"]
    assert "$ref" in person_schema or "type" in person_schema
    # The Person model definition should appear in $defs.
    assert "$defs" in schema
    assert "_Person" in schema["$defs"]
    person_def = schema["$defs"]["_Person"]
    assert person_def["type"] == "object"
    assert "name" in person_def["properties"]
    assert "age" in person_def["properties"]
    assert "email" in person_def["properties"]


# ─── Annotated / Field / Literal / Optional ──────────────────────────


def test_schema_annotated_string_description() -> None:
    """``Annotated[str, "desc"]`` populates the description."""

    def fn(name: Annotated[str, "the user's name"]) -> str:
        """Greet."""
        return name

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["properties"]["name"]["description"] == "the user's name"


def test_schema_annotated_field_description() -> None:
    """``Annotated[T, Field(description=...)]`` populates the description."""

    def fn(value: Annotated[int, Field(description="numeric input")]) -> int:
        """Process."""
        return value

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["properties"]["value"]["description"] == "numeric input"


def test_schema_literal_becomes_enum() -> None:
    """``Literal["a", "b"]`` produces an enum schema."""

    def fn(mode: Literal["read", "write", "append"]) -> None:
        """Operate."""

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    mode = schema["properties"]["mode"]
    # Could be {enum: [...]} or {const: "..."} depending on Pydantic
    # version; canonical form is "enum" for >1 value.
    assert mode["enum"] == ["read", "write", "append"]


def test_schema_optional_str() -> None:
    """``Optional[str] = None`` is nullable with default null."""

    def fn(label: str | None = None) -> None:
        """Maybe label."""

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    label = schema["properties"]["label"]
    # Pydantic represents Optional via anyOf with a null variant, OR
    # via a `type` list including "null". Either is acceptable as long
    # as the schema permits null.
    if "anyOf" in label:
        types_in_union = {variant.get("type") for variant in label["anyOf"]}
        assert "null" in types_in_union
    elif isinstance(label.get("type"), list):
        assert "null" in label["type"]
    else:
        # Pydantic 2.x sometimes inlines as anyOf — fail loud if neither path.
        pytest.fail(f"Optional[str] schema should permit null but got {label!r}")


def test_schema_str_or_none_pep604() -> None:
    """``str | None`` (PEP 604 union) is handled identically to Optional."""

    def fn(label: str | None = None) -> None:
        """Maybe label."""

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    label = schema["properties"]["label"]
    if "anyOf" in label:
        types_in_union = {variant.get("type") for variant in label["anyOf"]}
        assert "null" in types_in_union
    elif isinstance(label.get("type"), list):
        assert "null" in label["type"]


# ─── docstring ───────────────────────────────────────────────────────


def test_schema_description_from_docstring() -> None:
    """The function-level description comes from the docstring."""

    def fn(x: int) -> int:
        """The first paragraph of the docstring."""
        return x

    result = build_function_schema(fn, strict=False)
    assert result.description == "The first paragraph of the docstring."


def test_schema_param_description_from_docstring_args() -> None:
    """``Args:`` entries populate the per-property descriptions."""

    def fn(text: str, count: int) -> str:
        """
        Process input.

        Args:
            text: The text to process.
            count: Number of times to repeat.
        """
        return text * count

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["properties"]["text"]["description"] == "The text to process."
    assert schema["properties"]["count"]["description"] == "Number of times to repeat."


def test_schema_annotated_overrides_docstring() -> None:
    """``Annotated[..., Field(description=...)]`` wins over docstring."""

    def fn(x: Annotated[int, Field(description="annotated wins")]) -> int:
        """
        Process.

        Args:
            x: docstring loses.
        """
        return x

    result = build_function_schema(fn, strict=False)
    assert result.parameters_json_schema["properties"]["x"]["description"] == "annotated wins"


# ─── return type ─────────────────────────────────────────────────────


def test_schema_return_annotation_captured() -> None:
    """The return annotation is exposed on the result."""

    def fn(x: int) -> str:
        """Stringify."""
        return str(x)

    result = build_function_schema(fn, strict=False)
    assert result.return_annotation is str


def test_schema_no_return_annotation_is_none() -> None:
    """Missing return annotation produces ``None``."""

    def fn(x: int):
        """Process."""
        return x

    result = build_function_schema(fn, strict=False)
    assert result.return_annotation is None


# ─── strict mode ─────────────────────────────────────────────────────


def test_schema_strict_mode_default_true() -> None:
    """``strict=True`` (the default) adds ``additionalProperties: false``."""

    def fn(x: int) -> int:
        """Process."""
        return x

    result = build_function_schema(fn)  # default strict=True
    assert result.parameters_json_schema["additionalProperties"] is False


def test_schema_strict_false_does_not_force_additional_properties() -> None:
    """``strict=False`` leaves ``additionalProperties`` unset."""

    def fn(x: int) -> int:
        """Process."""
        return x

    result = build_function_schema(fn, strict=False)
    assert "additionalProperties" not in result.parameters_json_schema


# ─── permissive types ────────────────────────────────────────────────


def test_schema_warns_on_any_param(caplog: pytest.LogCaptureFixture) -> None:
    """``Any`` parameter logs an INFO warning."""

    def fn(payload: Any) -> str:
        """Process."""
        return str(payload)

    with caplog.at_level(logging.INFO, logger="omnigent_client.tools._schema"):
        build_function_schema(fn, strict=False)

    # Find the warning by content — must name function and parameter.
    warning_messages = [r.message for r in caplog.records]
    assert any(
        "fn" in msg and "payload" in msg and "permissive" in msg.lower()
        for msg in warning_messages
    ), f"Expected an Any-warning naming 'fn' and 'payload', got: {warning_messages}"


def test_schema_warns_on_object_param(caplog: pytest.LogCaptureFixture) -> None:
    """``object`` parameter logs an INFO warning."""

    def fn(thing: object) -> str:
        """Process."""
        return str(thing)

    with caplog.at_level(logging.INFO, logger="omnigent_client.tools._schema"):
        build_function_schema(fn, strict=False)

    warning_messages = [r.message for r in caplog.records]
    assert any(
        "fn" in msg and "thing" in msg and "permissive" in msg.lower() for msg in warning_messages
    )


def test_schema_warns_on_missing_annotation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing annotation (resolves to Any) logs an INFO warning."""

    def fn(x):  # type: ignore[no-untyped-def]
        """Process."""
        return x

    with caplog.at_level(logging.INFO, logger="omnigent_client.tools._schema"):
        build_function_schema(fn, strict=False)

    warning_messages = [r.message for r in caplog.records]
    assert any(
        "fn" in msg and "x" in msg and "permissive" in msg.lower() for msg in warning_messages
    )


def test_schema_no_warning_for_concrete_type(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Concrete annotations do NOT log a warning."""

    def fn(x: int) -> int:
        """Process."""
        return x

    with caplog.at_level(logging.INFO, logger="omnigent_client.tools._schema"):
        build_function_schema(fn, strict=False)

    warning_messages = [r.message for r in caplog.records]
    assert not any("permissive" in msg.lower() for msg in warning_messages), (
        f"Concrete int param should not produce a permissive-type warning, got: {warning_messages}"
    )


# ─── zero-arg tools ──────────────────────────────────────────────────


def test_schema_zero_arg_tool() -> None:
    """A function with no parameters produces an empty-object schema."""

    def fn() -> str:
        """Return a constant."""
        return "constant"

    result = build_function_schema(fn, strict=False)
    schema = result.parameters_json_schema
    assert schema["type"] == "object"
    assert schema["properties"] == {}
    assert schema.get("required", []) == []


def test_schema_zero_arg_tool_strict_mode() -> None:
    """A zero-arg tool in strict mode still has additionalProperties false."""

    def fn() -> str:
        """Return a constant."""
        return "constant"

    result = build_function_schema(fn, strict=True)
    schema = result.parameters_json_schema
    assert schema["additionalProperties"] is False
