#!/usr/bin/env python3
"""Repair conversation message watermarks through the configured store."""

from __future__ import annotations

import argparse
import importlib
import json
from collections.abc import Callable, Sequence
from typing import Any, cast

from omnigent.db.db_models import workspace_scope
from omnigent.stores.conversation_store import ConversationStore
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
    parser.add_argument("--after-workspace-id", type=int)
    parser.add_argument("--after-conversation-id")
    parser.add_argument("--conversation-batch-limit", type=_positive, default=100)
    parser.add_argument("--item-batch-limit", type=_positive, default=1000)
    parser.add_argument(
        "--max-pages",
        type=_positive,
        help="stop after this many bounded calls and return status 2 if unfinished",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if (args.after_workspace_id is None) != (args.after_conversation_id is None):
        _parser().error("--after-workspace-id and --after-conversation-id must be paired")
    if args.after_workspace_id is not None and args.after_workspace_id != args.workspace_id:
        _parser().error("after workspace must match --workspace-id")

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

    cursor = (
        (args.after_workspace_id, args.after_conversation_id)
        if args.after_workspace_id is not None
        else None
    )
    pages = 0
    with workspace_scope(args.workspace_id):
        while True:
            result = store.reconcile_last_message_watermarks(
                after=cursor,
                conversation_batch_limit=args.conversation_batch_limit,
                item_batch_limit=args.item_batch_limit,
            )
            pages += 1
            print(
                json.dumps(
                    {
                        "complete": result.complete,
                        "next_after": result.next_after,
                    },
                    separators=(",", ":"),
                )
            )
            if result.complete:
                return 0
            if args.max_pages is not None and pages >= args.max_pages:
                return 2
            cursor = result.next_after


if __name__ == "__main__":
    raise SystemExit(main())
