"""Pin the ``SessionItem`` API variants to the entity data models.

``GET /v1/sessions/{id}/items`` returns ``ConversationItem.to_api_dict()``
records: the store-assigned fields plus the item's data model spread
flat. Each ``SessionItem`` variant must therefore declare exactly the
fields of its ``ITEM_TYPE_TO_DATA_CLS`` model, or the OpenAPI document
silently drifts from the payload.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.entities import ConversationItem, FunctionCallData, MessageData
from omnigent.entities.conversation import ITEM_TYPE_TO_DATA_CLS
from omnigent.server.schemas import SessionItem

_FLAT_COMMON_FIELDS = {"id", "response_id", "type", "status", "created_at", "created_by"}


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


@pytest.mark.parametrize("item_type", sorted(ITEM_TYPE_TO_DATA_CLS))
def test_session_item_variant_mirrors_entity_fields(item_type: str) -> None:
    data_cls = ITEM_TYPE_TO_DATA_CLS[item_type]
    entity_fields = set(data_cls.model_json_schema(mode="serialization")["properties"])
    variant = _session_item_variants()[item_type]
    assert set(variant["properties"]) == entity_fields | _FLAT_COMMON_FIELDS, (
        f"SessionItem variant for {item_type!r} drifted from {data_cls.__name__}"
    )


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
