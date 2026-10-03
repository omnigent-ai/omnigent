"""Typed list-item guard for the checked-in OpenAPI artifact.

:mod:`tests.server.test_openapi_drift` pins :file:`openapi.json` to the
live ``scripts/dump_openapi.py`` output, so asserting on the artifact
asserts on the running server's schema. This test checks that the
paginated list operations whose records external consumers depend on
declare an item schema, rather than the bare ``{}`` the shared
``PaginatedList`` envelope emits for ``list[Any]`` (which generated
clients type as ``data: Any[]``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_OPENAPI_JSON_PATH: Path = _REPO_ROOT / "openapi.json"


def _load_spec() -> dict[str, Any]:
    assert _OPENAPI_JSON_PATH.exists(), (
        f"openapi.json not found at {_OPENAPI_JSON_PATH}; regenerate with "
        f"`python scripts/dump_openapi.py`."
    )
    with _OPENAPI_JSON_PATH.open(encoding="utf-8") as fh:
        return json.load(fh)


def _resolve(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = spec["components"]["schemas"][name]
    return schema


def _variants(spec: dict[str, Any], schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Concrete object schemas *schema* admits, one per ``oneOf``/``anyOf`` alternative."""
    schema = _resolve(spec, schema)
    alternatives = schema.get("oneOf") or schema.get("anyOf")
    if not alternatives:
        return [schema]
    return [variant for alternative in alternatives for variant in _variants(spec, alternative)]


def _declared_properties(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """Property schemas declared by one object schema, merged across ``allOf`` parts."""
    schema = _resolve(spec, schema)
    properties = dict(schema.get("properties", {}))
    for part in schema.get("allOf", []):
        properties.update(_declared_properties(spec, part))
    return properties


def _array_items(spec: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any] | None:
    for candidate in _variants(spec, schema):
        if candidate.get("type") == "array":
            return candidate.get("items", {})
    return None


def _list_item_schema(spec: dict[str, Any], path: str) -> dict[str, Any]:
    response = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"]
    envelope = _resolve(spec, response["schema"])
    data = envelope.get("properties", {}).get("data")
    assert data is not None, f"GET {path} 200 response declares no `data` property"
    items = _array_items(spec, data)
    assert items is not None, f"GET {path} 200 response `data` is not an array"
    return items


def test_session_items_response_documents_record_fields() -> None:
    spec = _load_spec()
    items = _list_item_schema(spec, "/v1/sessions/{session_id}/items")
    required_fields = {"id", "type", "role", "content"}
    message_records = [
        properties
        for properties in (_declared_properties(spec, v) for v in _variants(spec, items))
        if required_fields <= properties.keys()
    ]
    assert message_records, (
        f"GET /v1/sessions/{{session_id}}/items documents its records as "
        f"{items!r}, so generated clients receive data: Any[]; no record "
        f"variant documents all of {sorted(required_fields)}"
    )
    content_items = _array_items(spec, message_records[0]["content"])
    assert content_items is not None, "session-item `content` is not documented as an array"
    assert "text" in _declared_properties(spec, content_items), (
        "session-item content[].text is not documented"
    )


def test_agents_response_documents_record_fields() -> None:
    spec = _load_spec()
    items = _list_item_schema(spec, "/v1/agents")
    properties = _declared_properties(spec, items)
    missing = {"id", "name"} - properties.keys()
    assert not missing, (
        f"GET /v1/agents documents its records as {items!r}, so generated "
        f"clients receive data: Any[]; undocumented fields: {sorted(missing)}"
    )
