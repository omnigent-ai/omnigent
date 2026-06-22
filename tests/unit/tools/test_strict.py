"""Tests for strict-mode JSON-schema normalization."""

from __future__ import annotations

from typing import Any

from omnigent_client.tools._strict import ensure_strict_schema


def test_strict_object_schema_sets_additional_properties_false() -> None:
    """Object schemas get ``additionalProperties: false`` added."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
    }
    result = ensure_strict_schema(schema)
    assert result["additionalProperties"] is False


def test_strict_object_schema_marks_all_properties_required() -> None:
    """Strict mode forces every property into ``required``."""
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "integer"},
            "y": {"type": "string"},
        },
        "required": ["x"],  # Original only requires x.
    }
    result = ensure_strict_schema(schema)
    # Both x and y should now be required, regardless of original.
    # Use sorted comparison since ordering is not guaranteed.
    assert sorted(result["required"]) == ["x", "y"]


def test_strict_empty_object_schema_no_required() -> None:
    """An object with no properties keeps required empty (or absent)."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }
    result = ensure_strict_schema(schema)
    # No properties means no required entries to add.
    # The implementation skips writing 'required' when properties is empty;
    # callers can rely on `required` being empty/absent in this case.
    assert result.get("required", []) == []
    assert result["additionalProperties"] is False


def test_strict_does_not_mutate_input() -> None:
    """The normalizer must return a new dict, leaving input unchanged."""
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"x": {"type": "integer"}},
        "required": [],
    }
    original_required = schema["required"]
    result = ensure_strict_schema(schema)
    # The input's required list must be unchanged (we returned a new dict).
    assert schema["required"] is original_required
    assert schema["required"] == []
    # Result has the new constraints.
    assert result["required"] == ["x"]


def test_strict_recurses_into_nested_object_property() -> None:
    """Nested object schemas under properties are also normalized."""
    schema = {
        "type": "object",
        "properties": {
            "inner": {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
            },
        },
    }
    result = ensure_strict_schema(schema)
    inner = result["properties"]["inner"]
    # Inner object also gets strict-mode constraints applied.
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["a"]


def test_strict_recurses_into_array_items() -> None:
    """Object schemas inside ``items`` are normalized too."""
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"k": {"type": "string"}},
        },
    }
    result = ensure_strict_schema(schema)
    item = result["items"]
    assert item["additionalProperties"] is False
    assert item["required"] == ["k"]


def test_strict_recurses_into_anyof_oneof_allof() -> None:
    """Union variants get normalized recursively."""
    schema = {
        "anyOf": [
            {
                "type": "object",
                "properties": {"a": {"type": "integer"}},
            },
            {
                "type": "object",
                "properties": {"b": {"type": "string"}},
            },
        ]
    }
    result = ensure_strict_schema(schema)
    assert result["anyOf"][0]["additionalProperties"] is False
    assert result["anyOf"][0]["required"] == ["a"]
    assert result["anyOf"][1]["additionalProperties"] is False
    assert result["anyOf"][1]["required"] == ["b"]


def test_strict_recurses_into_defs() -> None:
    """``$defs`` referenced types are normalized."""
    schema = {
        "type": "object",
        "properties": {"r": {"$ref": "#/$defs/MyType"}},
        "$defs": {
            "MyType": {
                "type": "object",
                "properties": {"q": {"type": "integer"}},
            },
        },
    }
    result = ensure_strict_schema(schema)
    inner = result["$defs"]["MyType"]
    assert inner["additionalProperties"] is False
    assert inner["required"] == ["q"]


def test_strict_passthrough_for_non_dict_input() -> None:
    """A non-dict (e.g. boolean True/False) is returned unchanged."""
    # JSON Schema permits boolean schemas (true/false) at certain positions.
    # The normalizer should not crash on these.
    assert ensure_strict_schema(True) is True  # type: ignore[arg-type]
    assert ensure_strict_schema(False) is False  # type: ignore[arg-type]


def test_strict_passthrough_for_non_object_type() -> None:
    """Schemas with non-object types are returned essentially unchanged."""
    schema = {"type": "string", "minLength": 1}
    result = ensure_strict_schema(schema)
    # Should be a copy with the same content (no additionalProperties added
    # since type is not object).
    assert result == {"type": "string", "minLength": 1}
    assert "additionalProperties" not in result
