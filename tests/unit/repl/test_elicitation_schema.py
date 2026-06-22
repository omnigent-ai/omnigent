"""Tests for ``build_accept_content_from_schema`` in
``omnigent.tools._elicitation_schema``.

Covers auto-fill logic for MCP elicitation ``requestedSchema`` objects:
booleans, oneOf enums, plain enums, defaults, free-form fields, and
mixed schemas.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.tools._elicitation_schema import (
    build_accept_content_from_schema as _build_elicitation_content_from_schema,
)

# ── Empty / missing properties ────────────────────────────────


@pytest.mark.parametrize(
    "schema,description",
    [
        pytest.param({}, "empty schema dict", id="empty"),
        pytest.param({"type": "object"}, "object type with no properties key", id="no-props-key"),
        pytest.param(
            {"type": "object", "properties": {}},
            "object type with empty properties dict",
            id="empty-props",
        ),
    ],
)
def test_returns_none_for_empty_or_missing_properties(
    schema: dict[str, Any],
    description: str,
) -> None:
    """
    Schemas without actionable properties return ``None`` — the
    caller should treat this as a binary approve/decline with no
    content payload.

    If this fails: the function is returning a non-None value for
    a schema that has no properties to auto-fill, which would send
    a bogus content dict to the MCP server.
    """
    result = _build_elicitation_content_from_schema(schema)
    # None signals "no content needed" for binary elicitation.
    assert result is None, (
        f"Expected None for {description}, got {result!r}. "
        "Schemas without properties should not produce content."
    )


# ── Boolean properties ────────────────────────────────────────


def test_boolean_property_auto_fills_true() -> None:
    """
    Boolean properties are auto-filled with ``True`` (approve).

    If this fails: the boolean branch in the function is broken,
    and boolean-only elicitations will return None (can't auto-fill)
    instead of auto-approving.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "flag": {"type": "boolean"},
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "Boolean properties should be auto-fillable"
    # Boolean fields default to True (auto-approve).
    assert result["flag"] is True, (
        f"Boolean property should be True, got {result['flag']!r}. "
        "If False, the auto-approve logic is inverted."
    )


# ── oneOf enum properties ─────────────────────────────────────


def test_oneof_enum_picks_allow_when_present() -> None:
    """
    When a oneOf enum has an ``"allow"`` const, it is picked.

    If this fails: the "allow" preference in the oneOf branch is
    broken, and the function either returns None or picks the wrong
    const.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "oneOf": [
                    {"const": "allow"},
                    {"const": "deny"},
                ],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "oneOf enum with 'allow' should be auto-fillable"
    # "allow" is preferred over other const values.
    assert result["decision"] == "allow", (
        f"Expected 'allow', got {result['decision']!r}. "
        "The oneOf branch should prefer 'allow' when present."
    )


def test_oneof_enum_picks_first_const_without_allow() -> None:
    """
    When a oneOf enum has no ``"allow"`` const, the first const
    value is picked.

    If this fails: the fallback in the oneOf branch is broken —
    it either returns None (can't auto-fill) or picks a wrong value.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "oneOf": [
                    {"const": "approve"},
                    {"const": "reject"},
                ],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "oneOf enum should be auto-fillable even without 'allow'"
    # First const is the fallback when "allow" is absent.
    assert result["decision"] == "approve", (
        f"Expected first const 'approve', got {result['decision']!r}. "
        "Without 'allow', the function should pick the first const."
    )


def test_oneof_enum_returns_none_when_no_const() -> None:
    """
    When a oneOf list has entries but none have ``const``, the
    function returns ``None`` (can't determine a value).

    If this fails: the function is picking a bogus value from
    oneOf entries that don't define ``const``.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is None, "oneOf entries without 'const' keys should not be auto-fillable"


# ── Plain enum properties ─────────────────────────────────────


def test_plain_enum_picks_allow_when_present() -> None:
    """
    When a plain ``enum`` list contains ``"allow"``, it is picked.

    If this fails: the plain-enum branch doesn't check for "allow"
    and falls through to the first value.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["deny", "allow", "abstain"],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "Plain enum with 'allow' should be auto-fillable"
    # "allow" is preferred regardless of its position in the list.
    assert result["decision"] == "allow", (
        f"Expected 'allow', got {result['decision']!r}. "
        "The plain-enum branch should prefer 'allow' when present."
    )


def test_plain_enum_picks_first_value_without_allow() -> None:
    """
    When a plain ``enum`` list has no ``"allow"`` entry, the first
    value is picked.

    If this fails: the fallback in the plain-enum branch is wrong.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "decision": {
                "type": "string",
                "enum": ["approve", "reject"],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "Plain enum should be auto-fillable"
    # First value is the fallback.
    assert result["decision"] == "approve", (
        f"Expected first enum value 'approve', got {result['decision']!r}"
    )


# ── Default values ─────────────────────────────────────────────


def test_string_with_default_uses_default() -> None:
    """
    A property with a ``default`` value uses that default, even
    for free-form types like string.

    If this fails: the default-value branch is not reached, and
    the function falls through to the "can't auto-fill" return.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "default": "world"},
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "Properties with defaults should be auto-fillable"
    # Default value is used verbatim.
    assert result["name"] == "world", (
        f"Expected default 'world', got {result['name']!r}. "
        "The default-value branch must use the schema's default."
    )


# ── Free-form fields (no default, no enum) ─────────────────────


@pytest.mark.parametrize(
    "prop_schema,description",
    [
        pytest.param({"type": "string"}, "bare string", id="string"),
        pytest.param({"type": "number"}, "bare number", id="number"),
        pytest.param({"type": "integer"}, "bare integer", id="integer"),
    ],
)
def test_freeform_without_default_returns_none(
    prop_schema: dict[str, Any],
    description: str,
) -> None:
    """
    Free-form properties (string/number/integer) without a default
    or enum return ``None`` because they require user input.

    If this fails: the function is generating a value for a
    free-form field, which would send an arbitrary/wrong value to
    the MCP server.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "value": prop_schema,
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is None, f"Free-form {description} without default should not be auto-fillable"


# ── Mixed schemas ──────────────────────────────────────────────


def test_mixed_boolean_and_enum_auto_fills_both() -> None:
    """
    A schema with both a boolean and an enum property auto-fills
    both fields.

    If this fails: one of the branches (boolean or enum) is not
    reached when both are present in the same schema.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "confirm": {"type": "boolean"},
            "level": {
                "type": "string",
                "enum": ["low", "medium", "high"],
            },
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is not None, "Mixed boolean + enum should be auto-fillable"
    # Boolean auto-fills to True.
    assert result["confirm"] is True, f"Boolean field should be True, got {result['confirm']!r}"
    # Enum without "allow" falls back to first value.
    assert result["level"] == "low", (
        f"Enum field should be first value 'low', got {result['level']!r}"
    )


def test_mixed_fillable_and_freeform_returns_none() -> None:
    """
    A schema with one auto-fillable field and one free-form field
    returns ``None`` because the whole schema can't be auto-filled.

    If this fails: the function returns a partial content dict with
    the fillable fields but missing the free-form one, which would
    send incomplete data to the MCP server.
    """
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "confirm": {"type": "boolean"},
            "reason": {"type": "string"},  # free-form, no default
        },
    }
    result = _build_elicitation_content_from_schema(schema)
    assert result is None, (
        "Mixed fillable + free-form should return None; "
        "partial auto-fill would send incomplete data"
    )
