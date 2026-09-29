"""Regression: background session titles must use the persisted seed.

The background title coordinator's compare-and-swap guard
(``expected_seed_title``) must be the exact deterministic title the
conversation store persisted for the first turn. Deriving the guard
independently from the raw prompt diverges from the persisted seed for
multi-block first-turn content: a native session that attaches a file sends
the user's typed text plus a standalone ``"[Attached: <path>]"``
``input_text`` block (see ``omnigent/inner/*_native_executor.py`` /
``omnigent/inner/native_attachments.py``), and
``synthesize_conversation_title`` drops a line that is *exactly* an
attachment marker, so:

* the store persists the seed with the marker block dropped
  (``"review this build failure"``), while
* a guard re-derived from the *joined* prompt keeps the marker on the same
  line (``"review this build failure [Attached: ...]"``).

When the guard and the persisted seed diverge, ``_wait_for_seed`` sees a
non-``None`` title that never equals the guard, returns ``False``, and the
coordinator silently skips generation (and the later
``rename_conversation_if_title_matches`` compare-and-swap is keyed on the
same wrong guard anyway). The session then keeps its deterministic
truncated-prompt title forever and the generated title is dropped.

This exercises the real coordinator + real ``SqlAlchemyConversationStore`` +
real ``prepare_background_session_title`` + real seed path
(``_seed_missing_title_from_user_message``), scheduling with the persisted
title exactly as the /events route does. Only the title generator is
stubbed, standing in for the runner/LLM call -- the established seam in
``tests/server/test_background_session_titles.py``.
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    prepare_background_session_title,
)
from omnigent.server.routes._sessions.helpers import _seed_missing_title_from_user_message
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio

# The first-turn message shape a native session produces when a file is
# attached: the user's typed text followed by a standalone "[Attached: <path>]"
# marker block injected by the native executor.
_USER_TEXT = "review this build failure"
_ATTACHMENT_MARKER = "[Attached: /tmp/omnigent-bridge/build.log]"
_FIRST_TURN_CONTENT: list[dict[str, str]] = [
    {"type": "input_text", "text": _USER_TEXT},
    {"type": "input_text", "text": _ATTACHMENT_MARKER},
]

# What the title model would return for this session.
_GENERATED_TITLE = "Review The Build Failure"


async def test_background_title_uses_persisted_seed_for_attachment_first_turn(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default", agent_id=uuid.uuid4().hex)
    # A native harness both injects the "[Attached: <path>]" marker block and is
    # one of the harnesses that runs background title inference.
    conversation.harness_override = "claude-native"

    event = SessionEventInput(
        type="message",
        data={"role": "user", "content": _FIRST_TURN_CONTENT},
    )

    async def generator(_request: BackgroundTitleRequest) -> str:
        return _GENERATED_TITLE

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        # Keep the seed wait short so a genuine miss fails fast instead of
        # blocking the suite; the seed is persisted before scheduling below, so
        # a correct build resolves immediately.
        seed_wait_seconds=2.0,
    )

    # 1) Prepare the title attempt exactly as the /events route does, from the
    #    raw first-turn event.
    pending = prepare_background_session_title(
        coordinator=coordinator,
        conversation=conversation,
        event=event,
    )
    assert pending is not None, "native first-turn message should schedule a title attempt"

    # 2) Persist the deterministic seed the same way the /events route does,
    #    from the same first-turn content. This is the value that actually lands
    #    in the store; we do not hand-craft it. The helper also updates
    #    ``conversation.title`` in place, which is what the route passes on.
    item = NewConversationItem(
        type="message",
        response_id="resp_first_turn",
        data=MessageData(role="user", content=_FIRST_TURN_CONTENT),
    )
    await _seed_missing_title_from_user_message(conversation, item, store)
    persisted_seed = store.get_conversation(conversation.id).title
    assert persisted_seed is not None, "store should seed a deterministic title"

    # 3) Run the coordinator's one guarded attempt against the real store,
    #    scheduling with the persisted title exactly as the route does.
    pending.schedule(expected_seed_title=conversation.title)
    await coordinator.wait_for_idle()

    # The generated title must win. On a build whose compare-and-swap guard is
    # re-derived from the joined prompt (keeping the attachment marker) instead
    # of the persisted seed (marker block dropped), generation is skipped and
    # the session keeps its deterministic truncated-prompt title.
    final_title = store.get_conversation(conversation.id).title
    assert final_title == _GENERATED_TITLE, (
        f"background title was dropped: expected {_GENERATED_TITLE!r}, session "
        f"still titled {final_title!r}; persisted seed was {persisted_seed!r}"
    )
