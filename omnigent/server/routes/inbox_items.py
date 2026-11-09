"""Routes for Inbox items: suggestions an agent posts to its owner's Inbox.

``POST /v1/inbox/items`` is for agents only: the request must come from a
session the caller owns, through that session's runner, and the session's
agent must enable the ``user_inbox`` tool group. ``GET`` and ``PATCH`` serve
the owner's Inbox.
"""

from __future__ import annotations

import asyncio
import dataclasses
import re
import time
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities import InboxItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.inbox_dedupe import (
    DedupeInputs,
    canonical_link,
    dedupe_key,
    find_duplicate,
    log_post_decision,
    normalize_topic,
)
from omnigent.server.routes._agent_origin import require_agent_origin
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._errors import session_not_found
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.inbox_item_store import (
    DuplicateInboxItemError,
    InboxItemStore,
    NewInboxItem,
)

# A run that posts more than this is producing noise, not suggestions.
MAX_ITEMS_PER_SESSION = 10
_POST_ACTION = "Inbox posts"
_ITEM_ID = re.compile(r"^[0-9a-f]{32}$")

InboxItemKind = Literal["followup", "info", "reminder"]
InboxItemState = Literal["unread", "read", "done", "dismissed"]
InboxItemFeedback = Literal["none", "useful", "not_useful"]


class InboxItemLink(BaseModel):
    """One evidence link on an item."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["session", "pr", "url"]
    ref: str = Field(min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=200)


class CreateInboxItemRequest(BaseModel):
    """Body for ``POST /v1/inbox/items``."""

    model_config = ConfigDict(extra="forbid")

    kind: InboxItemKind
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1, max_length=8000)
    links: list[InboxItemLink] = Field(default_factory=list, max_length=10)
    topic: str = Field(min_length=1, max_length=100)
    suggested_prompt: str | None = Field(default=None, max_length=4000)
    expires_at: int | None = Field(default=None, gt=0)


class UpdateInboxItemRequest(BaseModel):
    """Body for ``PATCH /v1/inbox/items/{item_id}``."""

    model_config = ConfigDict(extra="forbid")

    state: InboxItemState | None = None
    feedback: InboxItemFeedback | None = None
    feedback_text: str | None = Field(default=None, max_length=2000)


def _item_json(item: InboxItem) -> dict[str, Any]:
    return dataclasses.asdict(item)


def _parse_cursor(after: str | None) -> tuple[int, str] | None:
    if after is None:
        return None
    created_at, _, item_id = after.partition(":")
    if not created_at.isdigit() or _ITEM_ID.match(item_id) is None:
        raise OmnigentError("Invalid Inbox cursor", code=ErrorCode.INVALID_INPUT)
    return int(created_at), item_id


def create_inbox_items_router(
    store: InboxItemStore,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    agent_cache: Any,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the Inbox items router.

    :param store: Store for Inbox items.
    :param conversation_store: Store used to load the posting session.
    :param agent_store: Store used to load that session's agent.
    :param agent_cache: Cache used to load the agent's spec.
    :param permission_store: Permission store; ``None`` when auth is off.
    :param auth_provider: Auth provider; ``None`` when auth is off.
    :returns: The configured router.
    """
    router = APIRouter()

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    def _posting_agent(source_session_id: str) -> tuple[str, str | None]:
        """Return the source session's agent id and bundle hash, if it may post."""
        conversation = conversation_store.get_conversation(source_session_id)
        if conversation is None:
            raise session_not_found()
        agent = agent_store.get(conversation.agent_id) if conversation.agent_id else None
        if agent is None:
            raise OmnigentError(
                "Inbox posts require the session's agent", code=ErrorCode.FORBIDDEN
            )
        spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
        if not spec.tool_groups.user_inbox:
            raise OmnigentError(
                "Inbox posts require an agent with the user_inbox tool group",
                code=ErrorCode.FORBIDDEN,
            )
        return agent.id, agent.bundle_location.rsplit("/", 1)[-1] or None

    @router.post("/inbox/items")
    async def create_inbox_item(request: Request, body: CreateInboxItemRequest) -> dict[str, Any]:
        """Post an item to the caller's Inbox, or return the item it repeats."""
        owner = _owner(request)
        source_session_id = require_agent_origin(
            request,
            action=_POST_ACTION,
            owner=owner,
            auth_provider=auth_provider,
            conversation_store=conversation_store,
            permission_store=permission_store,
            required=True,
        )
        assert source_session_id is not None
        agent_id, prompt_version = await asyncio.to_thread(_posting_agent, source_session_id)

        links = [link.model_dump(exclude_none=True) for link in body.links]
        inputs = DedupeInputs(
            kind=body.kind,
            primary_link=canonical_link(links[0]) if links else None,
            topic=normalize_topic(body.topic),
        )
        if not inputs.topic:
            raise OmnigentError("topic must contain a word", code=ErrorCode.INVALID_INPUT)
        now = int(time.time())
        match = await asyncio.to_thread(find_duplicate, store, owner, inputs, now=now)
        log_post_decision(inputs, match)
        if match.existing is not None:
            return {"item": _item_json(match.existing), "duplicate": True}
        posted = await asyncio.to_thread(store.count_for_session, owner, source_session_id)
        if posted >= MAX_ITEMS_PER_SESSION:
            raise OmnigentError(
                f"This session already posted {MAX_ITEMS_PER_SESSION} Inbox items",
                code=ErrorCode.INVALID_INPUT,
            )
        new_item = NewInboxItem(
            user_id=owner,
            source_session_id=source_session_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
            kind=body.kind,
            title=body.title,
            body_md=body.body_md,
            links=links,
            suggested_prompt=body.suggested_prompt,
            primary_link=inputs.primary_link,
            topic=inputs.topic,
            dedupe_key=dedupe_key(inputs),
            expires_at=body.expires_at,
        )
        retire = match.expired.id if match.expired is not None else None
        try:
            item = await asyncio.to_thread(store.create, new_item, retire_item_id=retire)
        except DuplicateInboxItemError:
            # A concurrent post with the same key won the insert.
            existing = await asyncio.to_thread(store.get_by_dedupe_key, owner, new_item.dedupe_key)
            if existing is None:
                raise
            return {"item": _item_json(existing), "duplicate": True}
        return {"item": _item_json(item), "duplicate": False}

    @router.get("/inbox/items")
    async def list_inbox_items(
        request: Request,
        state: Annotated[list[InboxItemState] | None, Query()] = None,
        created_after: int | None = Query(default=None, ge=0),
        after: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        """List the caller's items, newest first, with the unread count."""
        owner = _owner(request)
        cursor = _parse_cursor(after)
        items, next_cursor = await asyncio.to_thread(
            store.list_for_user,
            owner,
            states=frozenset(state) if state else None,
            created_after=created_after,
            after=cursor,
            limit=limit,
        )
        unread = await asyncio.to_thread(store.count_unread, owner)
        return {
            "data": [_item_json(item) for item in items],
            "next_cursor": f"{next_cursor[0]}:{next_cursor[1]}" if next_cursor else None,
            "unread_count": unread,
        }

    @router.patch("/inbox/items/{item_id}")
    async def update_inbox_item(
        request: Request, item_id: str, body: UpdateInboxItemRequest
    ) -> dict[str, Any]:
        """Record the caller's response to one of their items."""
        owner = _owner(request)
        if _ITEM_ID.match(item_id) is None:
            raise OmnigentError("Inbox item not found", code=ErrorCode.NOT_FOUND)
        item = await asyncio.to_thread(
            store.update,
            item_id,
            owner,
            state=body.state,
            feedback=body.feedback,
            feedback_text=body.feedback_text,
            now=int(time.time()),
        )
        if item is None:
            raise OmnigentError("Inbox item not found", code=ErrorCode.NOT_FOUND)
        return _item_json(item)

    return router
