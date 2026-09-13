"""Shared utility for auto-filling MCP elicitation ``content`` from
``requestedSchema``.

Used by both the runner's inline elicitation callback
(:mod:`omnigent.runner.mcp_manager`) and the REPL's
``_handle_elicitation`` (:mod:`omnigent.repl._repl`).
"""

from __future__ import annotations

from typing import Any

#: Content values MCP allows in an ``ElicitResult``.
ElicitContent = dict[str, str | int | float | bool | list[str] | None]


def _matches_declared_type(value: object, prop_type: object, prop: dict[str, Any]) -> bool:
    """
    Whether a value is of one declared JSON-Schema ``type``.

    :param value: The answered value, e.g. ``"prod"``.
    :param prop_type: The declared type name, e.g. ``"string"`` or ``"null"``.
    :param prop: The property schema, for ``items`` checks on arrays.
    :returns: ``True`` when the value is of the declared type.
    """
    # ``bool`` is an ``int`` subclass in Python, so exclude it explicitly
    # from the numeric types — a boolean is not an answer to a number.
    if prop_type == "string":
        return isinstance(value, str)
    if prop_type == "boolean":
        return isinstance(value, bool)
    if prop_type == "integer":
        return not isinstance(value, bool) and isinstance(value, int)
    if prop_type == "number":
        return not isinstance(value, bool) and isinstance(value, int | float)
    if prop_type == "null":
        return value is None
    if prop_type == "array":
        if not isinstance(value, list):
            return False
        items = prop.get("items")
        if isinstance(items, dict):
            item_enum = items.get("enum")
            if isinstance(item_enum, list) and item_enum:
                if not all(v in item_enum for v in value):
                    return False
            item_type = items.get("type")
            if item_type is not None and not all(
                _matches_declared_type(v, item_type, {}) for v in value
            ):
                return False
        min_items = prop.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            return False
        max_items = prop.get("maxItems")
        if isinstance(max_items, int) and len(value) > max_items:
            return False
        return True
    # An unknown or absent type declares nothing to check.
    return True


def _within_value_bounds(value: object, prop: dict[str, Any]) -> bool:
    """
    Whether a value satisfies the property's numeric and length bounds.

    :param value: The answered value, e.g. ``42`` or ``"release/2.4"``.
    :param prop: The property schema, e.g. ``{"type": "integer", "maximum": 100}``.
    :returns: ``True`` when every declared bound holds.
    """
    if isinstance(value, int | float) and not isinstance(value, bool):
        minimum = prop.get("minimum")
        if isinstance(minimum, int | float) and value < minimum:
            return False
        maximum = prop.get("maximum")
        if isinstance(maximum, int | float) and value > maximum:
            return False
    if isinstance(value, str):
        min_length = prop.get("minLength")
        if isinstance(min_length, int) and len(value) < min_length:
            return False
        max_length = prop.get("maxLength")
        if isinstance(max_length, int) and len(value) > max_length:
            return False
    return True


def _value_matches_property(value: object, prop: dict[str, Any]) -> bool:
    """
    Whether one answered value conforms to one ``requestedSchema`` property.

    Checks the declared ``type`` (including ``anyOf`` unions such as
    ``str | None`` optional fields), ``enum`` membership, ``const`` /
    ``oneOf`` consts, numeric bounds, and string/array length bounds.
    A property with no declared type accepts any MCP-primitive value.

    :param value: The answered value, e.g. ``"prod"``.
    :param prop: The property schema, e.g. ``{"type": "string", "enum": [...]}``.
    :returns: ``True`` when the value fits what the property declared.
    """
    if isinstance(value, list) and not all(isinstance(v, str) for v in value):
        return False
    if not isinstance(value, str | int | float | bool | list) and value is not None:
        return False

    prop_type = prop.get("type")
    if prop_type is not None and not _matches_declared_type(value, prop_type, prop):
        return False

    # An ``anyOf`` union (e.g. a ``str | None`` optional field) declares its
    # types per-branch; the value must satisfy at least one branch fully.
    any_of = prop.get("anyOf")
    if isinstance(any_of, list) and any_of:
        branches = [b for b in any_of if isinstance(b, dict)]
        if branches and not any(_value_matches_property(value, b) for b in branches):
            return False

    if not _within_value_bounds(value, prop):
        return False

    if "const" in prop and value != prop["const"]:
        return False

    allowed = prop.get("enum")
    if isinstance(allowed, list) and allowed:
        # Direct membership covers scalars and enum-of-list members alike.
        if value not in allowed:
            # A list answer to an array-typed enum selects several members.
            if not (
                prop_type == "array"
                and isinstance(value, list)
                and all(v in allowed for v in value)
            ):
                return False

    one_of = prop.get("oneOf")
    if isinstance(one_of, list) and one_of:
        consts = [o.get("const") for o in one_of if isinstance(o, dict) and "const" in o]
        if consts and value not in consts:
            return False
    return True


def validate_content_against_schema(
    content: ElicitContent | None,
    schema: dict[str, Any] | None,
) -> ElicitContent | None:
    """
    Keep answered ``content`` only when it fits the ``requestedSchema``.

    The resolve payload reaches the runner from a browser, so it is checked
    rather than trusted: keys must be ones the schema named, values must be
    the primitives MCP allows and match each property's declared type, an
    enum must be answered with one of its own members, and every field the
    schema marks ``required`` must be present. Anything else returns ``None``
    so the caller can fail closed instead of putting a body on the wire that
    the server's own schema rejects.

    :param content: The content the person's verdict carried, or ``None``.
    :param schema: The elicitation's ``requestedSchema``, or ``None``.
    :returns: The content when it conforms, otherwise ``None``. ``None`` means
        "nothing to forward": either the verdict carried no content, or what it
        carried did not fit the schema. A caller that must tell those apart
        checks the supplied content's own truthiness — an answer that was given
        but rejected fails closed, while no answer at all falls back.
    """
    if not content:
        return None
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict):
        return None
    for key, value in content.items():
        prop = properties.get(key)
        if not isinstance(prop, dict):
            return None
        if not _value_matches_property(value, prop):
            return None
    required = schema.get("required") if isinstance(schema, dict) else None
    if isinstance(required, list) and any(
        isinstance(name, str) and name not in content for name in required
    ):
        return None
    return content


def schema_requires_fields(schema: dict[str, Any] | None) -> bool:
    """
    Whether a ``requestedSchema`` names fields it requires.

    A schema with no properties is a bare consent prompt, and one whose
    properties are all optional legally accepts an answer with no content —
    only a non-empty ``required`` list makes an empty accept malformed, so a
    surface that collected nothing should decline rather than send one the
    server will reject.

    :param schema: The elicitation's ``requestedSchema``, or ``None``.
    :returns: ``True`` when the schema declares at least one required field.
    """
    if not isinstance(schema, dict):
        return False
    properties = schema.get("properties")
    if not (isinstance(properties, dict) and properties):
        return False
    required = schema.get("required")
    return isinstance(required, list) and bool(required)


def build_accept_content_from_schema(
    schema: dict[str, Any],
) -> dict[str, str | int | float | bool | list[str] | None] | None:
    """
    Build ``content`` for an MCP elicitation ``accept`` from a
    ``requestedSchema``.

    Returns a dict when every schema property can be auto-filled
    (booleans → ``True``, enums → ``"allow"`` or first option,
    properties with ``default`` → the default). Returns ``None``
    when the schema has properties that require free-form user
    input (strings, numbers without defaults) — the caller should
    decline or direct the user to the web UI.

    Returns ``None`` (no content needed) when the schema has no
    properties (binary approve/decline elicitation).

    :param schema: The ``requestedSchema`` dict from the
        elicitation event. May be empty ``{}``.
    :returns: A flat ``{field: value}`` dict, or ``None``.
    """
    properties = schema.get("properties")
    if not properties or not isinstance(properties, dict):
        return None
    content: dict[str, str | int | float | bool | list[str] | None] = {}
    for key, prop in properties.items():
        if not isinstance(prop, dict):
            return None
        # Enum with oneOf — pick "allow" or the first const.
        one_of = prop.get("oneOf")
        if isinstance(one_of, list) and one_of:
            allow_val = next(
                (o["const"] for o in one_of if isinstance(o, dict) and o.get("const") == "allow"),
                None,
            )
            if allow_val is not None:
                content[key] = allow_val
            else:
                first = next(
                    (o["const"] for o in one_of if isinstance(o, dict) and "const" in o),
                    None,
                )
                if first is None:
                    return None
                content[key] = first
            continue
        # Enum with plain enum list.
        enum_vals = prop.get("enum")
        if isinstance(enum_vals, list) and enum_vals:
            allow_val = next((v for v in enum_vals if v == "allow"), None)
            content[key] = allow_val if allow_val is not None else enum_vals[0]
            continue
        prop_type = prop.get("type", "string")
        if prop_type == "boolean":
            content[key] = True
            continue
        # Has a default — use it.
        if "default" in prop:
            content[key] = prop["default"]
            continue
        # Free-form input required — can't auto-fill.
        return None
    return content
