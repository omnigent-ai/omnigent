"""Item-schema guard for the paginated list operations in the checked-in OpenAPI artifact.

``GET /v1/sessions/{session_id}/items`` and ``GET /v1/agents`` return pages
of records, so their documented 200 response must describe a record. A shared
``data: list[Any]`` envelope serialises to ``items: {}``, which generated
clients turn into an untyped element: transcript readers and agent-name
resolvers cannot validate the fields they read, and the document cannot
detect response-shape drift. ``tests/server/test_openapi_drift.py`` pins
``openapi.json`` to the live ``generate_spec()`` output, so asserting on the
artifact is equivalent to asserting on the running server's document.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_OPENAPI_JSON_PATH: Path = _REPO_ROOT / "openapi.json"

_SESSION_ITEMS_PATH = "/v1/sessions/{session_id}/items"
_AGENTS_PATH = "/v1/agents"


def _load_spec() -> dict[str, Any]:
    assert _OPENAPI_JSON_PATH.exists(), (
        f"openapi.json not found at {_OPENAPI_JSON_PATH}; regenerate with "
        f"`python scripts/dump_openapi.py`."
    )
    return json.loads(_OPENAPI_JSON_PATH.read_text(encoding="utf-8"))


def _resolve(spec: dict[str, Any], node: Any) -> Any:
    while isinstance(node, dict) and "$ref" in node:
        node = spec["components"]["schemas"][node["$ref"].rsplit("/", 1)[-1]]
    return node


def _variants(spec: dict[str, Any], node: Any) -> list[dict[str, Any]]:
    """Expand a schema into the concrete object schemas a value may match.

    Follows ``$ref``, flattens ``oneOf``/``anyOf`` (dropping ``null``
    branches) and merges ``allOf`` so a discriminated union and a flat
    object are checked the same way.
    """
    node = _resolve(spec, node)
    if not isinstance(node, dict):
        return []
    if "allOf" in node:
        merged: dict[str, Any] = {"properties": {}}
        for part in node["allOf"]:
            for variant in _variants(spec, part):
                merged["properties"].update(variant.get("properties", {}))
        return [merged]
    for key in ("oneOf", "anyOf"):
        if key in node:
            return [
                v
                for branch in node[key]
                if not (isinstance(branch, dict) and branch.get("type") == "null")
                for v in _variants(spec, branch)
            ]
    return [node]


def _item_variants(spec: dict[str, Any], path: str) -> list[dict[str, Any]]:
    schema = spec["paths"][path]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    envelopes = _variants(spec, schema)
    assert envelopes, f"GET {path}: 200 response has no object schema: {schema!r}"
    items: list[dict[str, Any]] = []
    for envelope in envelopes:
        data = envelope.get("properties", {}).get("data")
        assert data is not None, f"GET {path}: 200 schema declares no `data` property"
        for array in _variants(spec, data):
            items.extend(_variants(spec, array.get("items", {})))
    return items


def _properties(variant: dict[str, Any]) -> set[str]:
    return set(variant.get("properties", {}))


def test_session_items_operation_documents_item_fields() -> None:
    """Items declare ``id``/``type``; messages also ``role`` and ``content[].text``."""
    spec = _load_spec()
    variants = _item_variants(spec, _SESSION_ITEMS_PATH)
    assert variants, f"GET {_SESSION_ITEMS_PATH}: data.items resolves to no object schema"
    for variant in variants:
        missing = {"id", "type"} - _properties(variant)
        assert not missing, (
            f"GET {_SESSION_ITEMS_PATH}: data.items variant declares "
            f"{sorted(_properties(variant))} and lacks {sorted(missing)}; the record "
            f"schema is untyped (`items: {{}}`), so generated clients receive `Any[]`. "
            f"Document the session item shape (e.g. SessionItemList -> SessionItem) and "
            f"regenerate openapi.json."
        )
    message_variants = [v for v in variants if {"role", "content"} <= _properties(v)]
    assert message_variants, (
        f"GET {_SESSION_ITEMS_PATH}: no data.items variant declares both `role` and "
        f"`content`; message items must be documented."
    )
    for variant in message_variants:
        blocks = [
            block
            for array in _variants(spec, variant["properties"]["content"])
            for block in _variants(spec, array.get("items", {}))
        ]
        assert any("text" in _properties(block) for block in blocks), (
            f"GET {_SESSION_ITEMS_PATH}: message `content[]` blocks declare "
            f"{sorted({p for b in blocks for p in _properties(b)})}; `text` must be documented."
        )


def test_agents_operation_documents_item_fields() -> None:
    """Every documented agent record declares ``id`` and ``name``."""
    spec = _load_spec()
    variants = _item_variants(spec, _AGENTS_PATH)
    assert variants, f"GET {_AGENTS_PATH}: data.items resolves to no object schema"
    for variant in variants:
        missing = {"id", "name"} - _properties(variant)
        assert not missing, (
            f"GET {_AGENTS_PATH}: data.items variant declares "
            f"{sorted(_properties(variant))} and lacks {sorted(missing)}; the record "
            f"schema is untyped (`items: {{}}`), so generated clients receive `Any[]`. "
            f"Document the agent shape (e.g. AgentList -> AgentObject) and regenerate "
            f"openapi.json."
        )
