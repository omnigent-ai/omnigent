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
from collections.abc import Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.entities import InboxItem
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.inbox_dedupe import (
    DedupeInputs,
    InvalidLinkError,
    canonical_link,
    dedupe_key,
    find_duplicate,
    log_post_decision,
    normalize_topic,
)
from omnigent.server.inbox_media import MAX_MEDIA_BLOCKS, InboxMediaBlock, stored_media
from omnigent.server.routes._agent_origin import require_agent_origin
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.inbox_item_store import (
    INBOX_ITEMS_PER_SESSION,
    DuplicateInboxItemError,
    InboxItemStore,
    InboxItemTooLargeError,
    InboxSessionCapError,
    NewInboxItem,
)

MAX_ITEMS_PER_SESSION = INBOX_ITEMS_PER_SESSION
MAX_ACTIONS_PER_ITEM = 3
# The unread badge counts up to this many; GET reports when the count is capped.
UNREAD_COUNT_LIMIT = 100
_POST_ACTION = "Inbox posts"
_ITEM_ID = re.compile(r"[0-9a-f]{32}")
_CURSOR = re.compile(r"([0-9]{1,19}):([0-9a-f]{32})")
# Largest value a signed 64-bit (BIGINT) timestamp column holds.
MAX_EPOCH = 2**63 - 1
# Matches the ``topic`` column, measured after normalization.
MAX_TOPIC_CHARS = 128

InboxItemKind = Literal["followup", "info", "reminder"]
InboxItemStatus = Literal["unread", "read", "done", "dismissed"]
InboxItemResolution = Literal[
    "none", "action_taken", "already_done", "useful", "not_relevant", "incorrect", "not_now"
]
# The status each resolution moves an item to; "none" reopens it.
_RESOLUTION_STATUS: dict[str, str] = {
    "none": "read",
    "action_taken": "done",
    "already_done": "done",
    "useful": "dismissed",
    "not_relevant": "dismissed",
    "incorrect": "dismissed",
    "not_now": "dismissed",
}


class InboxItemLink(BaseModel):
    """One evidence link on an item."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["session", "pr", "url"]
    ref: str = Field(min_length=1, max_length=2048)
    label: str | None = Field(default=None, max_length=200)


class InboxItemAction(BaseModel):
    """An agent-defined action: a button that opens a session prefilled with ``prompt``."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=60)
    prompt: str = Field(min_length=1, max_length=4000)


class CreateInboxItemRequest(BaseModel):
    """Body for ``POST /v1/inbox/items``."""

    model_config = ConfigDict(extra="forbid")

    kind: InboxItemKind
    title: str = Field(min_length=1, max_length=200)
    body_md: str = Field(min_length=1, max_length=8000)
    links: list[InboxItemLink] = Field(default_factory=list, max_length=10)
    actions: list[InboxItemAction] = Field(default_factory=list, max_length=MAX_ACTIONS_PER_ITEM)
    media: list[InboxMediaBlock] = Field(default_factory=list, max_length=MAX_MEDIA_BLOCKS)
    topic: str = Field(min_length=1, max_length=100)
    expires_at: int | None = Field(default=None, gt=0, le=MAX_EPOCH)


class UpdateInboxItemRequest(BaseModel):
    """Body for ``PATCH /v1/inbox/items/{item_id}``.

    ``status`` marks an open item read or unread. ``resolution`` closes an item
    (or reopens it with ``"none"``) and moves its status to match;
    ``"action_taken"`` also needs ``resolution_action``, the action's index.
    """

    model_config = ConfigDict(extra="forbid")

    status: Literal["unread", "read"] | None = None
    resolution: InboxItemResolution | None = None
    resolution_action: int | None = Field(default=None, ge=0, lt=MAX_ACTIONS_PER_ITEM)
    feedback_text: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _check_combination(self) -> UpdateInboxItemRequest:
        if self.status is not None and self.resolution is not None:
            raise ValueError("set status or resolution, not both")
        if (self.resolution == "action_taken") != (self.resolution_action is not None):
            raise ValueError("resolution_action goes only with resolution action_taken")
        return self


def _sub_agent_spec(spec: Any, name: str) -> Any | None:
    """Return the sub-agent of *spec* called *name*, or ``None``."""
    for sub_agent in getattr(spec, "sub_agents", None) or []:
        if getattr(sub_agent, "name", None) == name:
            return sub_agent
    return None


def _item_json(item: InboxItem) -> dict[str, Any]:
    return dataclasses.asdict(item)


def _parse_cursor(after: str | None) -> tuple[int, str] | None:
    if after is None:
        return None
    match = _CURSOR.fullmatch(after)
    if match is None or int(match.group(1)) > MAX_EPOCH:
        raise OmnigentError("Invalid Inbox cursor", code=ErrorCode.INVALID_INPUT)
    return int(match.group(1)), match.group(2)


def create_inbox_items_router(
    store: InboxItemStore,
    *,
    enabled: Callable[[str], bool],
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    agent_cache: Any,
    permission_store: PermissionStore | None = None,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the Inbox items router.

    :param store: Store for Inbox items.
    :param enabled: Whether Inbox items are on for the caller, given the caller's
        user id; every route returns 404 when it is false.
    :param conversation_store: Store used to load the posting session.
    :param agent_store: Store used to load that session's agent.
    :param agent_cache: Cache used to load the agent's spec.
    :param permission_store: Permission store; ``None`` when auth is off.
    :param auth_provider: Auth provider; ``None`` when auth is off.
    :returns: The configured router.
    """

    def _owner(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    async def require_enabled(request: Request) -> None:
        if not await asyncio.to_thread(enabled, _owner(request)):
            raise HTTPException(status_code=404, detail="not found")

    router = APIRouter(dependencies=[Depends(require_enabled)])

    def _posting_agent(source_session_id: str) -> tuple[str, str | None]:
        """Return the source session's agent id and bundle hash, if it may post."""
        conversation = conversation_store.get_conversation(source_session_id)
        agent = (
            agent_store.get(conversation.agent_id)
            if conversation is not None and conversation.agent_id
            else None
        )
        if agent is None:
            raise OmnigentError(
                "Inbox posts require the session's agent", code=ErrorCode.FORBIDDEN
            )
        spec = agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
        # A named child session runs its sub-agent's spec, whose tool groups
        # are the ones the runner registered.
        if conversation.sub_agent_name:
            spec = _sub_agent_spec(spec, conversation.sub_agent_name) or spec
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
        source_session_id = await asyncio.to_thread(
            lambda: require_agent_origin(
                request,
                action=_POST_ACTION,
                owner=owner,
                auth_provider=auth_provider,
                conversation_store=conversation_store,
                permission_store=permission_store,
                required=True,
            )
        )
        assert source_session_id is not None
        agent_id, prompt_version = await asyncio.to_thread(_posting_agent, source_session_id)

        links = [link.model_dump(exclude_none=True) for link in body.links]
        try:
            # Every link is validated; the first one names what the item is about.
            canonical = [canonical_link(link) for link in links]
        except InvalidLinkError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        primary_link = canonical[0] if canonical else None
        inputs = DedupeInputs(
            kind=body.kind, primary_link=primary_link, topic=normalize_topic(body.topic)
        )
        if not inputs.topic or len(inputs.topic) > MAX_TOPIC_CHARS:
            raise OmnigentError(
                f"topic must contain a word and be at most {MAX_TOPIC_CHARS} characters "
                "once lowercased",
                code=ErrorCode.INVALID_INPUT,
            )
        match = await asyncio.to_thread(
            lambda: find_duplicate(store, owner, inputs, now=int(time.time()))
        )
        log_post_decision(inputs, match)
        if match.existing is not None:
            return {"item": _item_json(match.existing), "duplicate": True}
        try:
            media = stored_media(body.media)
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        new_item = NewInboxItem(
            user_id=owner,
            source_session_id=source_session_id,
            agent_id=agent_id,
            prompt_version=prompt_version,
            kind=body.kind,
            title=body.title,
            body_md=body.body_md,
            links=links,
            actions=[action.model_dump() for action in body.actions],
            media=media,
            primary_link=inputs.primary_link,
            topic=inputs.topic,
            dedupe_key=dedupe_key(inputs),
            expires_at=body.expires_at,
        )
        retire = match.expired.id if match.expired is not None else None
        try:
            item = await asyncio.to_thread(lambda: store.create(new_item, retire_item_id=retire))
        except DuplicateInboxItemError:
            # A concurrent post with the same key won the insert.
            existing = await asyncio.to_thread(store.get_by_dedupe_key, owner, new_item.dedupe_key)
            if existing is None:
                raise
            return {"item": _item_json(existing), "duplicate": True}
        except InboxSessionCapError as exc:
            raise OmnigentError(
                f"This session already posted {MAX_ITEMS_PER_SESSION} Inbox items",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        except InboxItemTooLargeError as exc:
            raise OmnigentError(
                "The item's body, links, actions, or media are too large to store",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        return {"item": _item_json(item), "duplicate": False}

    @router.get("/inbox/items")
    async def list_inbox_items(
        request: Request,
        status: Annotated[list[InboxItemStatus] | None, Query()] = None,
        created_after: int | None = Query(default=None, ge=0, le=MAX_EPOCH),
        after: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict[str, Any]:
        """List the caller's items, newest first, with a bounded unread count."""
        owner = _owner(request)
        cursor = _parse_cursor(after)
        items, next_cursor = await asyncio.to_thread(
            lambda: store.list_for_user(
                owner,
                statuses=frozenset(status) if status else None,
                created_after=created_after,
                after=cursor,
                limit=limit,
            )
        )
        unread = await asyncio.to_thread(
            lambda: store.count_unread(owner, limit=UNREAD_COUNT_LIMIT)
        )
        return {
            "data": [_item_json(item) for item in items],
            "next_cursor": f"{next_cursor[0]}:{next_cursor[1]}" if next_cursor else None,
            "unread_count": unread,
            "unread_count_capped": unread >= UNREAD_COUNT_LIMIT,
        }

    @router.patch("/inbox/items/{item_id}")
    async def update_inbox_item(
        request: Request, item_id: str, body: UpdateInboxItemRequest
    ) -> dict[str, Any]:
        """Record the caller's response to one of their items."""
        owner = _owner(request)
        if _ITEM_ID.fullmatch(item_id) is None:
            raise OmnigentError("Inbox item not found", code=ErrorCode.NOT_FOUND)
        if body.resolution_action is not None:
            current = await asyncio.to_thread(store.get, item_id, owner)
            if current is not None and body.resolution_action >= len(current.actions):
                raise OmnigentError("No such action on this item", code=ErrorCode.INVALID_INPUT)
        status = (
            _RESOLUTION_STATUS[body.resolution] if body.resolution is not None else body.status
        )
        try:
            item = await asyncio.to_thread(
                lambda: store.update(
                    item_id,
                    owner,
                    status=status,
                    resolution=body.resolution,
                    resolution_action=body.resolution_action,
                    feedback_text=body.feedback_text,
                    now=int(time.time()),
                )
            )
        except InboxItemTooLargeError as exc:
            raise OmnigentError(
                "feedback_text is too large to store", code=ErrorCode.INVALID_INPUT
            ) from exc
        if item is None:
            raise OmnigentError("Inbox item not found", code=ErrorCode.NOT_FOUND)
        return _item_json(item)

    return router
