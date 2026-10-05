#!/usr/bin/env python3
"""Repair conversation message watermarks through the configured store."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable, Sequence
from typing import Any, cast

from omnigent.db.db_models import workspace_scope
from omnigent.stores.conversation_store import (
    ConversationStore,
    WatermarkReconciliationCursor,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

StoreFactory = Callable[[str, str | None], ConversationStore]


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _factory(path: str) -> StoreFactory:
    if ":" in path:
        module_name, _, attribute = path.partition(":")
    else:
        module_name, separator, attribute = path.rpartition(".")
        if not separator:
            module_name = attribute = ""
    if not module_name or not attribute:
        raise ValueError("--store-factory must be MODULE:ATTRIBUTE")
    value: Any = getattr(importlib.import_module(module_name), attribute)
    if not callable(value):
        raise TypeError(f"store factory {path!r} is not callable")
    return cast(StoreFactory, value)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Reconcile visible-message watermarks through ConversationStore; "
            "payloads are decoded by the configured store."
        )
    )
    parser.add_argument("--storage-location", required=True)
    parser.add_argument("--conversation-storage-location")
    parser.add_argument(
        "--store-factory",
        help=(
            "custom MODULE:ATTRIBUTE factory receiving storage and conversation "
            "locations; use this for encrypted/custom decoders"
        ),
    )
    parser.add_argument("--workspace-id", type=int, default=0)
    parser.add_argument(
        "--cursor",
        help="JSON checkpoint returned by a previous bounded call",
    )
    parser.add_argument("--item-batch-limit", type=_positive, default=1000)
    parser.add_argument(
        "--max-pages",
        type=_positive,
        help="stop after this many bounded calls and return status 2 if unfinished",
    )
    return parser


def _parse_cursor(
    raw: str | None,
    workspace_id: int,
    parser: argparse.ArgumentParser,
) -> WatermarkReconciliationCursor | None:
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"--cursor must be valid JSON: {exc.msg}")
    if not isinstance(value, dict):
        parser.error("--cursor must be a JSON object")
    required = {
        "workspace_id",
        "conversation_id",
        "item_position",
        "max_visible_message_at",
    }
    if set(value) != required:
        parser.error("--cursor must contain exactly the four checkpoint fields")
    cursor_workspace = value["workspace_id"]
    conversation_id = value["conversation_id"]
    item_position = value["item_position"]
    max_visible = value["max_visible_message_at"]
    if not isinstance(cursor_workspace, int) or isinstance(cursor_workspace, bool):
        parser.error("cursor workspace_id must be an integer")
    if cursor_workspace != workspace_id:
        parser.error("cursor workspace_id must match --workspace-id")
    if conversation_id is not None and not isinstance(conversation_id, str):
        parser.error("cursor conversation_id must be a string or null")
    if item_position is not None and (
        not isinstance(item_position, int) or isinstance(item_position, bool) or item_position < 0
    ):
        parser.error("cursor item_position must be a non-negative integer or null")
    if item_position is not None and conversation_id is None:
        parser.error("a continuing cursor requires conversation_id")
    if max_visible is not None and (
        not isinstance(max_visible, int) or isinstance(max_visible, bool)
    ):
        parser.error("cursor max_visible_message_at must be an integer or null")
    return WatermarkReconciliationCursor(
        cursor_workspace,
        conversation_id,
        item_position,
        max_visible,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    cursor = _parse_cursor(args.cursor, args.workspace_id, parser)

    store: ConversationStore
    if args.store_factory:
        store = _factory(args.store_factory)(
            args.storage_location,
            args.conversation_storage_location,
        )
    else:
        store = SqlAlchemyConversationStore(
            args.storage_location,
            args.conversation_storage_location,
        )

    pages = 0
    with workspace_scope(args.workspace_id):
        while True:
            result = store.reconcile_last_message_watermarks(
                cursor=cursor,
                item_batch_limit=args.item_batch_limit,
            )
            pages += 1
            print(
                json.dumps(
                    {
                        "complete": result.complete,
                        "next_cursor": (
                            result.next_cursor._asdict()
                            if result.next_cursor is not None
                            else None
                        ),
                    },
                    separators=(",", ":"),
                )
            )
            if result.complete:
                return 0
            if args.max_pages is not None and pages >= args.max_pages:
                return 2
            cursor = result.next_cursor


if __name__ == "__main__":
    raise SystemExit(main())
