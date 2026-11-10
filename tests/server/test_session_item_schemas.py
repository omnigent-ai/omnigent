"""Pin the ``SessionItem`` API variants to the entity data models.

``GET /v1/sessions/{id}/items`` returns ``ConversationItem.to_api_dict()``
records: the store-assigned fields plus the item's data model spread
flat. Each ``SessionItem`` variant must therefore declare exactly the
fields of its ``ITEM_TYPE_TO_DATA_CLS`` model, with the same types and
requiredness, or the OpenAPI document silently drifts from the payload.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from omnigent.entities import ConversationItem, FunctionCallData, MessageData
from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS
from omnigent.server.schemas import SessionItem

_FLAT_COMMON_FIELDS = {"id", "response_id", "type", "status", "created_at", "created_by"}
# ``created_by`` is omitted for agent, tool and system items, so it stays optional.
_FLAT_COMMON_REQUIRED = _FLAT_COMMON_FIELDS - {"created_by"}
# Block lists are the only properties the API variants refine: the entity keeps
# free-form dicts, the document types them as ``SessionContentBlock``.
_TYPED_BLOCK_FIELDS = {"message": {"content"}, "reasoning": {"summary", "content"}}
_BLOCK_ITEMS = {"$ref": "#/$defs/SessionContentBlock"}


def _session_item_variants() -> dict[str, dict[str, Any]]:
    """Map each ``type`` literal to its variant schema in the ``SessionItem`` union."""
    schema = SessionItem.model_json_schema(mode="serialization")
    variants: dict[str, dict[str, Any]] = {}
    for ref in schema["oneOf"]:
        variant = schema["$defs"][ref["$ref"].rsplit("/", 1)[-1]]
        variants[variant["properties"]["type"]["const"]] = variant
    return variants


def test_session_item_union_covers_every_item_type() -> None:
    variants = _session_item_variants()
    assert set(variants) == set(ITEM_TYPE_TO_DATA_CLS), (
        "SessionItem variants and ITEM_TYPE_TO_DATA_CLS disagree; a new item "
        "type needs a SessionItem variant so the API document describes it"
    )


def _with_typed_blocks(prop: dict[str, Any]) -> dict[str, Any]:
    """The entity's block-list schema with its array items retyped as ``SessionContentBlock``."""
    typed = copy.deepcopy(prop)
    arrays = [branch for branch in typed.get("anyOf", [typed]) if branch.get("type") == "array"]
    assert arrays, f"not a block list: {prop!r}"
    for branch in arrays:
        branch["items"] = _BLOCK_ITEMS
    return typed


@pytest.mark.parametrize("item_type", sorted(ITEM_TYPE_TO_DATA_CLS))
def test_session_item_variant_mirrors_entity_fields(item_type: str) -> None:
    data_cls = ITEM_TYPE_TO_DATA_CLS[item_type]
    entity = data_cls.model_json_schema(mode="serialization")
    variant = _session_item_variants()[item_type]
    drift = f"SessionItem variant for {item_type!r} drifted from {data_cls.__name__}"
    assert set(variant["properties"]) == set(entity["properties"]) | _FLAT_COMMON_FIELDS, drift
    assert set(variant.get("required", [])) == (
        set(entity.get("required", [])) | _FLAT_COMMON_REQUIRED
    ), drift
    typed_blocks = _TYPED_BLOCK_FIELDS.get(item_type, set())
    for name, entity_prop in entity["properties"].items():
        expected = _with_typed_blocks(entity_prop) if name in typed_blocks else entity_prop
        assert variant["properties"][name] == expected, f"{drift}: property {name!r}"


@pytest.mark.parametrize(
    "item",
    [
        ConversationItem(
            id="msg_user",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=1,
            created_by="alice@example.com",
            data=MessageData(role="user", content=[{"type": "input_text", "text": "hi"}]),
        ),
        ConversationItem(
            id="msg_assistant",
            type="message",
            status="completed",
            response_id="resp_1",
            created_at=2,
            data=MessageData(
                role="assistant",
                agent="my-agent",
                content=[{"type": "output_text", "text": "hello"}],
            ),
        ),
        ConversationItem(
            id="fc_1",
            type="function_call",
            status="completed",
            response_id="resp_1",
            created_at=3,
            data=FunctionCallData(agent="my-agent", name="search", arguments="{}", call_id="c1"),
        ),
    ],
    ids=["user_message", "assistant_message", "function_call"],
)
def test_flattened_item_keys_are_documented(item: ConversationItem) -> None:
    payload = item.to_api_dict()
    variant = _session_item_variants()[payload["type"]]
    undocumented = set(payload) - set(variant["properties"])
    assert not undocumented, f"to_api_dict() emits undocumented keys {sorted(undocumented)}"
    absent_required = set(variant.get("required", [])) - set(payload)
    assert not absent_required, (
        f"schema requires {sorted(absent_required)} but to_api_dict() omits them"
    )
