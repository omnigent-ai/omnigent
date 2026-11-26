from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from omnigent.db.db_models import current_workspace_id, workspace_scope
from omnigent.entities import MessageData, NewConversationItem
from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_MAX_PROMPT_CHARS,
    CAP_LONG_BACKGROUND_TITLE_INSTRUCTIONS,
    FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION,
    LEGACY_BACKGROUND_TITLE_INSTRUCTIONS_MAX_CHARS,
)
from omnigent.server import background_session_titles
from omnigent.server.background_session_titles import (
    BACKGROUND_SESSION_TITLES_HEADER,
    BACKGROUND_TITLE_MAX_CHARS,
    CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
    REFINED_TITLE_MESSAGE_MAX_CHARS,
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    RunnerBackgroundTitleGenerator,
    background_session_titles_enabled,
    compose_session_title_instructions,
    normalize_background_title,
    prepare_background_session_title,
    refined_title_prompt,
    runner_title_instructions,
    saved_workflow_run_title,
)
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.server.schemas import BackgroundSessionTitleRequest, SessionEventInput
from omnigent.server.server_config import SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


def _seed_session(store: SqlAlchemyConversationStore, title: str) -> str:
    conversation = store.create_conversation(kind="default", title=title)
    return conversation.id


async def test_prepare_background_title_for_eligible_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is not None


async def test_prepare_background_title_skips_when_user_setting_is_disabled(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
        enabled=False,
    )

    assert pending is None


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({}, True),
        ({BACKGROUND_SESSION_TITLES_HEADER: "on"}, True),
        ({BACKGROUND_SESSION_TITLES_HEADER: "off"}, False),
    ],
)
async def test_background_session_titles_enabled_defaults_on(
    headers: dict[str, str],
    expected: bool,
) -> None:
    assert background_session_titles_enabled(headers) is expected


async def test_prepare_background_title_from_message(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = uuid.uuid4().hex
    conversation = store.create_conversation(kind="default", agent_id=agent_id)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    pending = prepare_background_session_title(
        coordinator=coordinator,
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "please investigate"},
                    {"type": "input_text", "text": "the authentication timeout"},
                ],
            },
        ),
    )

    assert pending is not None
    assert pending.request == BackgroundTitleRequest(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        agent_id=agent_id,
    )


async def test_slash_command_background_title_uses_persisted_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Review migration plan"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="slash_command",
            data={"kind": "skill", "name": "grill-me", "arguments": "review this plan"},
        ),
    )

    assert pending is not None
    assert pending.request.prompt == "/grill-me review this plan"
    persisted = store.update_conversation(conversation.id, title="grill-me review this plan")
    assert persisted is not None
    pending.schedule(expected_seed_title=persisted.title)
    await pending.coordinator.wait_for_idle()

    assert store.get_conversation(conversation.id).title == "Review migration plan"


_WORKFLOW_RUN = f'Run the saved workflow "Blah borgus" (ID {"0" * 32}).'


@pytest.mark.parametrize(
    "event",
    [
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": f"/workflow {_WORKFLOW_RUN}\n\nFast"}],
            },
        ),
        SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": f"$workflow {_WORKFLOW_RUN}"}],
            },
        ),
        SessionEventInput(
            type="slash_command",
            data={"kind": "skill", "name": "workflow", "arguments": _WORKFLOW_RUN},
        ),
    ],
    ids=["message", "codex-message", "slash-command"],
)
async def test_saved_workflow_runs_keep_the_workflow_name_title(
    db_uri: str, event: SessionEventInput
) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Run Saved Workflow"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=store.create_conversation(kind="default"),
        event=event,
    )

    assert pending is None


@pytest.mark.parametrize(
    ("text", "title"),
    [
        (f"/workflow {_WORKFLOW_RUN}", "Blah borgus"),
        (f'/workflow Run the saved workflow "{"n" * 80}" (ID {"0" * 32}).', "n" * 80),
        (f'/workflow Run the saved workflow "{"n" * 81}" (ID {"0" * 32}).', None),
        # Names are literal, not message markup.
        (
            f'/workflow Run the saved workflow "[Attached: report.txt]" (ID {"0" * 32}).',
            "[Attached: report.txt]",
        ),
        (
            f'/workflow Run the saved workflow "[Attached: /tmp/r.txt]" (ID {"0" * 32}).',
            "[Attached: /tmp/r.txt]",
        ),
        (f"Please /workflow {_WORKFLOW_RUN}", None),
        (f"/workflow {_WORKFLOW_RUN} Then stop.", None),
    ],
)
async def test_saved_workflow_run_title(text: str, title: str | None) -> None:
    assert saved_workflow_run_title([{"type": "input_text", "text": text}]) == title


@pytest.mark.parametrize("excluded_session", ["titled", "child"])
async def test_prepare_background_title_skips_non_initial_sessions(
    db_uri: str,
    excluded_session: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(kind="default")
    conversation = (
        store.create_conversation(kind="default", title="Existing title")
        if excluded_session == "titled"
        else store.create_conversation(
            kind="sub_agent",
            title="researcher:auth",
            parent_conversation_id=parent.id,
        )
    )

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is None


@pytest.mark.parametrize("harness_override", ["pi"])
async def test_prepare_background_title_skips_unsupported_explicit_harnesses(
    db_uri: str,
    harness_override: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(
        title=None,
        agent_id=uuid.uuid4().hex,
    )
    conversation.harness_override = harness_override

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is None


async def test_prepare_background_title_supports_codex_native(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(
        title=None,
        agent_id=uuid.uuid4().hex,
    )
    conversation.harness_override = "codex-native"

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    pending = prepare_background_session_title(
        coordinator=BackgroundSessionTitleCoordinator(store, generator),
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        ),
    )

    assert pending is not None
    assert pending.request.harness_override == "codex-native"


async def test_default_seed_wait_allows_slow_native_session_startup(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)

    assert coordinator._seed_wait_seconds == 15.0


async def test_schedule_returns_before_delayed_generator_finishes(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(request: BackgroundTitleRequest) -> str:
        assert request.prompt == "please investigate the authentication timeout"
        assert request.harness_override == "claude-sdk"
        assert request.model_override == "claude-sonnet-4-6"
        assert request.additional_instructions == "Prefix titles with the current date."
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Prefix titles with the current date.",
    )
    started = time.perf_counter()
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="claude-sdk",
        model_override="claude-sonnet-4-6",
    )
    schedule_elapsed = time.perf_counter() - started

    assert schedule_elapsed < 0.05
    assert store.get_conversation(session_id).title == "Investigate authentication timeout"

    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_unsupported_harness_preserves_deterministic_seed(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def unsupported(_request: BackgroundTitleRequest) -> None:
        return None

    coordinator = BackgroundSessionTitleCoordinator(store, unsupported)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
        harness_override="pi",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generated_title_is_normalized_before_rename(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return '  "Debug   authentication timeout."  \nIgnored explanation'

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_title_normalizer_rejects_empty_and_oversized_output() -> None:
    assert normalize_background_title(None) is None
    assert normalize_background_title("   \n  ") is None
    assert normalize_background_title("x" * (BACKGROUND_TITLE_MAX_CHARS + 1)) is None


async def test_custom_title_instructions_allow_longer_output(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Review configurable title limits")
    generated = "x" * CUSTOM_BACKGROUND_TITLE_MAX_CHARS

    async def generator(_request: BackgroundTitleRequest) -> str:
        return generated

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Use a detailed structured title.",
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="review configurable title limits",
        expected_seed_title="Review configurable title limits",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == generated
    assert (
        normalize_background_title(
            "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS + 1),
            max_chars=CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
        )
        is None
    )


async def test_custom_title_instructions_truncate_oversized_output(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Review configurable title limits")

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS + 1)

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Use a detailed structured title.",
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="review configurable title limits",
        expected_seed_title="Review configurable title limits",
    )
    await coordinator.wait_for_idle()

    expected = "x" * (CUSTOM_BACKGROUND_TITLE_MAX_CHARS - 1) + "…"
    assert store.get_conversation(session_id).title == expected
    assert len(expected) == CUSTOM_BACKGROUND_TITLE_MAX_CHARS


async def test_manual_rename_wins_background_title_race(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    release = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    store.update_conversation(session_id, title="My manual title")
    release.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "My manual title"


async def test_generator_waits_for_deterministic_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation(kind="default")
    generator_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        generator_started.set()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=conversation.id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await asyncio.sleep(0.1)
    assert not generator_started.is_set()

    store.update_conversation(
        conversation.id,
        title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert generator_started.is_set()
    assert store.get_conversation(conversation.id).title == "Debug authentication timeout"


async def test_timeout_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        await asyncio.sleep(1)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        timeout_seconds=0.01,
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


async def test_generator_failure_preserves_deterministic_title(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")

    async def generator(_request: BackgroundTitleRequest) -> str:
        raise RuntimeError("fake generator failed")

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Investigate authentication timeout"


_FIRST_MESSAGE = "look into https://example.com/issues/42"
_SEED_TITLE = "Look into https://example.com/issues/42"


def _append_message(
    store: SqlAlchemyConversationStore, session_id: str, role: str, text: str
) -> None:
    if role == "user":
        data = MessageData(role="user", content=[{"type": "input_text", "text": text}])
    else:
        data = MessageData(
            role="assistant", agent="worker", content=[{"type": "output_text", "text": text}]
        )
    store.append(
        session_id, [NewConversationItem(response_id="turn-1", type="message", data=data)]
    )


def _first_turn_session(store: SqlAlchemyConversationStore) -> str:
    session_id = _seed_session(store, _SEED_TITLE)
    _append_message(store, session_id, "user", _FIRST_MESSAGE)
    return session_id


class _TitlesByPrompt:
    """Title the first message and the first reply differently, recording each prompt."""

    def __init__(self, initial: str | None = "Review linked issue") -> None:
        self.initial = initial
        self.prompts: list[str] = []
        self.refining = asyncio.Event()
        self.release_refinement = asyncio.Event()
        self.release_refinement.set()

    async def __call__(self, request: BackgroundTitleRequest) -> str | None:
        self.prompts.append(request.prompt)
        if request.prompt == _FIRST_MESSAGE:
            return self.initial
        self.refining.set()
        await self.release_refinement.wait()
        return (
            "Fix flaky upload retries" if "flaky upload" in request.prompt else "Open linked issue"
        )


@pytest.mark.parametrize("initial", ["Review linked issue", None])
async def test_first_reply_refines_the_generated_title(db_uri: str, initial: str | None) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt(initial)
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)

    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    await coordinator.wait_for_idle()
    assert store.get_conversation(session_id).title == (initial or _SEED_TITLE)

    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Fix flaky upload retries"
    assert generator.prompts[1] == (
        f"User request:\n{_FIRST_MESSAGE}\n\nAgent reply:\nThe issue is a flaky upload retry."
    )


@pytest.mark.parametrize(
    ("rename_at", "inferences"),
    [("before_initial_title", 0), ("after_initial_title", 1), ("during_refinement", 2)],
)
async def test_refinement_keeps_a_rename_from_the_first_turn(
    db_uri: str, rename_at: str, inferences: int
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    generator.release_refinement.clear()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)

    if rename_at == "before_initial_title":
        store.update_conversation(session_id, title="Renamed by agent")
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    await coordinator.wait_for_idle()
    if rename_at == "after_initial_title":
        store.update_conversation(session_id, title="Renamed by agent")
    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")
    coordinator.turn_ended(session_id)
    if rename_at == "during_refinement":
        await generator.refining.wait()
        store.update_conversation(session_id, title="Renamed by agent")
    generator.release_refinement.set()
    await coordinator.wait_for_idle()

    assert store.get_conversation(session_id).title == "Renamed by agent"
    assert len(generator.prompts) == inferences


async def test_refinement_runs_once_per_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")

    # A turn end for the same session id in another workspace leaves this one pending.
    with workspace_scope(current_workspace_id() + 1):
        coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()
    assert generator.prompts == [_FIRST_MESSAGE]
    for _ in range(2):
        coordinator.turn_ended(session_id)
        await coordinator.wait_for_idle()
    # Turn ends for sessions that never scheduled a title are ignored.
    coordinator.turn_ended("conv_untitled")
    await coordinator.wait_for_idle()

    assert len(generator.prompts) == 2
    assert store.get_conversation(session_id).title == "Fix flaky upload retries"


async def test_refinement_waits_for_a_settled_first_reply(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    await coordinator.wait_for_idle()

    # An idle edge before any reply is persisted leaves the refinement pending.
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()
    # So does an idle lull that resumes running before the reply is read.
    _append_message(store, session_id, "assistant", "Opening the issue now.")
    monkeypatch.setitem(_session_status_cache, session_id, "running")
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()
    assert generator.prompts == [_FIRST_MESSAGE]
    assert store.get_conversation(session_id).title == "Review linked issue"

    # A follow-up sent during the settle delay ends the first turn; its own reply
    # must not be paired with the first request.
    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")
    _append_message(store, session_id, "user", "now draft the release notes")
    _append_message(store, session_id, "assistant", "Release notes drafted.")
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()

    assert generator.prompts[-1].endswith("Agent reply:\nThe issue is a flaky upload retry.")
    assert store.get_conversation(session_id).title == "Fix flaky upload retries"


async def test_refinement_follows_a_first_reply_that_changes_after_a_paused_idle(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    await coordinator.wait_for_idle()

    # A pane paused on a permission prompt looks idle with only progress text persisted.
    _append_message(store, session_id, "assistant", "Opening the issue now.")
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()
    assert store.get_conversation(session_id).title == "Open linked issue"

    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")
    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()
    assert store.get_conversation(session_id).title == "Fix flaky upload retries"

    # Once a follow-up ends the first turn, later turn ends cost no inference.
    _append_message(store, session_id, "user", "now draft the release notes")
    _append_message(store, session_id, "assistant", "Release notes drafted.")
    for _ in range(2):
        coordinator.turn_ended(session_id)
        await coordinator.wait_for_idle()
    assert len(generator.prompts) == 3
    assert store.get_conversation(session_id).title == "Fix flaky upload retries"


async def test_refinement_settles_after_the_latest_idle_edge(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=1.0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    await coordinator.wait_for_idle()

    _append_message(store, session_id, "assistant", "Opening the issue now.")
    coordinator.turn_ended(session_id)  # a mid-turn lull
    await asyncio.sleep(0.8)
    coordinator.turn_ended(session_id)  # the turn's real end restarts the settle delay
    # Past the lull's settle deadline, before the real end's.
    # custom-lint: disable-next=sleep-then-assert -- the window straddles both settle deadlines
    await asyncio.sleep(0.3)
    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")
    await coordinator.wait_for_idle()

    assert generator.prompts[-1].endswith("Agent reply:\nThe issue is a flaky upload retry.")
    assert store.get_conversation(session_id).title == "Fix flaky upload retries"


async def test_refinement_is_dropped_when_the_first_turn_is_too_long_to_scan(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(background_session_titles, "_FIRST_TURN_SCAN_PAGES", 1)
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    # The one-page scan ends on a progress message; the reply lies beyond it.
    for _ in range(100):
        _append_message(store, session_id, "assistant", "Opening the issue now.")
    _append_message(store, session_id, "assistant", "The issue is a flaky upload retry.")

    coordinator.turn_ended(session_id)
    await coordinator.wait_for_idle()

    assert generator.prompts == [_FIRST_MESSAGE]
    assert store.get_conversation(session_id).title == "Review linked issue"


async def test_refinement_is_dropped_when_the_first_turn_left_no_reply(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _first_turn_session(store)
    generator = _TitlesByPrompt()
    coordinator = BackgroundSessionTitleCoordinator(store, generator, turn_settle_seconds=0)
    coordinator.schedule(
        session_id=session_id, prompt=_FIRST_MESSAGE, expected_seed_title=_SEED_TITLE
    )
    _append_message(store, session_id, "user", "now draft the release notes")
    _append_message(store, session_id, "assistant", "Release notes drafted.")

    for _ in range(2):
        coordinator.turn_ended(session_id)
        await coordinator.wait_for_idle()

    assert generator.prompts == [_FIRST_MESSAGE]
    assert store.get_conversation(session_id).title == "Review linked issue"


async def test_refined_title_prompt_fits_the_runner_prompt_limit() -> None:
    prompt = refined_title_prompt("m" * 5_000, "r" * 5_000)

    assert len(prompt) == BACKGROUND_TITLE_MAX_PROMPT_CHARS
    assert prompt.startswith(f"User request:\n{'m' * REFINED_TITLE_MESSAGE_MAX_CHARS}\n\n")
    assert prompt.endswith("r" * 100)


class _FakeRunnerResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return self._payload


class _FakeRunnerClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, object]]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
    ) -> _FakeRunnerResponse:
        assert timeout == 65.0
        self.requests.append((url, json))
        return _FakeRunnerResponse(
            {"status": "generated", "title": "Debug authentication timeout"}
        )


class _FakeRoutedRunner:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.runner_id = "runner_test"
        self.client = client


class _FakeRunnerRouter:
    def __init__(
        self,
        client: _FakeRunnerClient,
        capabilities: tuple[str, ...] = (CAP_LONG_BACKGROUND_TITLE_INSTRUCTIONS,),
    ) -> None:
        self.client = client
        self.capabilities = capabilities
        self.session_ids: list[str] = []

    def client_for_existing_conversation(self, session_id: str) -> _FakeRoutedRunner:
        self.session_ids.append(session_id)
        return _FakeRoutedRunner(self.client)

    def runner_has_capability(self, runner_id: str, capability: str) -> bool:
        assert runner_id == "runner_test"
        return capability in self.capabilities


async def test_runner_generator_posts_session_configuration() -> None:
    client = _FakeRunnerClient()
    router = _FakeRunnerRouter(client)
    generator = RunnerBackgroundTitleGenerator(router)  # type: ignore[arg-type]

    title = await generator(
        BackgroundTitleRequest(
            session_id="conv_test",
            prompt="please investigate the authentication timeout",
            agent_id="agent_test",
            harness_override="claude-sdk",
            model_override="claude-sonnet-4-6",
            additional_instructions="Use the requested slug format.",
        )
    )

    assert title == "Debug authentication timeout"
    assert router.session_ids == ["conv_test"]
    assert client.requests == [
        (
            "/v1/sessions/conv_test/background-title",
            {
                "prompt": "please investigate the authentication timeout",
                "agent_id": "agent_test",
                "harness_override": "claude-sdk",
                "model_override": "claude-sonnet-4-6",
                "sub_agent_name": None,
                "additional_instructions": (
                    "Use the requested slug format.\n"
                    "Unless another language is explicitly requested, write the title "
                    "in the same primary language as the user's message."
                ),
            },
        )
    ]


async def test_runner_generator_posts_language_rule_without_operator_customization() -> None:
    client = _FakeRunnerClient()
    generator = RunnerBackgroundTitleGenerator(_FakeRunnerRouter(client))  # type: ignore[arg-type]

    await generator(BackgroundTitleRequest(session_id="conv_test", prompt="请修复登录超时"))

    assert client.requests[0][1]["additional_instructions"] == (
        FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION
    )


async def test_runner_accepts_combined_server_and_owner_instructions_at_their_limits() -> None:
    client = _FakeRunnerClient()
    generator = RunnerBackgroundTitleGenerator(_FakeRunnerRouter(client))  # type: ignore[arg-type]
    combined = compose_session_title_instructions(
        "s" * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS, "u" * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
    )

    await generator(
        BackgroundTitleRequest(
            session_id="conv_test", prompt="hello", additional_instructions=combined
        )
    )

    body = BackgroundSessionTitleRequest.model_validate(client.requests[0][1])
    assert body.additional_instructions == f"{combined}\n{FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION}"


_LIMIT = LEGACY_BACKGROUND_TITLE_INSTRUCTIONS_MAX_CHARS
_SERVER_RULE = "s" * 3000
_OWNER_RULE = "u" * 900


@pytest.mark.parametrize(
    ("combined", "owner", "expected"),
    [
        pytest.param("Short rule.", "Short rule.", "Short rule.\n{language}", id="fits"),
        pytest.param("u" * _LIMIT, "u" * _LIMIT, "u" * _LIMIT, id="drops-language-rule"),
        pytest.param(
            compose_session_title_instructions(_SERVER_RULE, _OWNER_RULE),
            _OWNER_RULE,
            _OWNER_RULE + "\n{language}",
            id="drops-server-guidance",
        ),
        pytest.param(
            compose_session_title_instructions("s" * _LIMIT, "u" * _LIMIT),
            "u" * _LIMIT,
            "u" * _LIMIT,
            id="keeps-owner-guidance-only",
        ),
    ],
)
async def test_legacy_runner_instructions_fit_the_old_limit(
    combined: str, owner: str, expected: str
) -> None:
    request = BackgroundTitleRequest(
        session_id="conv_test",
        prompt="hello",
        additional_instructions=combined,
        owner_instructions=owner,
    )

    fitted = runner_title_instructions(request, long_supported=False)

    assert fitted == expected.format(language=FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION)
    assert len(fitted) <= _LIMIT


async def test_old_runner_still_titles_with_owner_instructions_at_the_limit(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    client = _FakeRunnerClient()
    owner_rule = "u" * SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
    coordinator = BackgroundSessionTitleCoordinator(
        store,
        RunnerBackgroundTitleGenerator(_FakeRunnerRouter(client, capabilities=())),  # type: ignore[arg-type]
        user_instructions=lambda _session_id: owner_rule,
    )

    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()
    renamed = await coordinator.format_agent_title(
        BackgroundTitleRequest(session_id=session_id, prompt="Auth timeout")
    )

    assert store.get_conversation(session_id).title == "Debug authentication timeout"
    assert renamed == "Debug authentication timeout"
    assert [body["additional_instructions"] for _url, body in client.requests] == [
        owner_rule,
        owner_rule,
    ]


async def test_schedule_is_one_shot_per_session(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    calls = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    for _ in range(2):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title="Investigate authentication timeout",
        )
    await coordinator.wait_for_idle()

    assert calls == 1
    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_generation_concurrency_is_bounded(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [
        _seed_session(store, f"Investigate authentication timeout {index}") for index in range(3)
    ]
    release = asyncio.Event()
    running = 0
    peak_running = 0
    two_generators_started = asyncio.Event()

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_generators_started.set()
        await release.wait()
        running -= 1
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)
    for index, session_id in enumerate(session_ids):
        coordinator.schedule(
            session_id=session_id,
            prompt="please investigate the authentication timeout",
            expected_seed_title=f"Investigate authentication timeout {index}",
        )
    await asyncio.wait_for(two_generators_started.wait(), timeout=1.0)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()

    assert all(
        store.get_conversation(session_id).title == "Debug authentication timeout"
        for session_id in session_ids
    )


async def test_seed_polling_is_bounded_by_generation_slots(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_ids = [store.create_conversation(kind="default").id for _ in range(3)]
    release = asyncio.Event()
    two_waiters_started = asyncio.Event()
    running = 0
    peak_running = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused title"

    coordinator = BackgroundSessionTitleCoordinator(store, generator, max_concurrency=2)

    async def wait_for_seed(*, session_id: str, expected_seed_title: str) -> bool:
        del session_id, expected_seed_title
        nonlocal peak_running, running
        running += 1
        peak_running = max(peak_running, running)
        if running == 2:
            two_waiters_started.set()
        await release.wait()
        running -= 1
        return False

    coordinator._wait_for_seed = wait_for_seed  # type: ignore[method-assign]
    for session_id in session_ids:
        coordinator.schedule(
            session_id=session_id,
            prompt="hello",
            expected_seed_title="hello",
        )

    await asyncio.wait_for(two_waiters_started.wait(), timeout=1.0)
    await asyncio.sleep(0.05)
    assert peak_running == 2

    release.set()
    await coordinator.wait_for_idle()


@pytest.mark.parametrize(
    ("server", "user", "expected"),
    [
        (None, None, None),
        ("  ", "", None),
        ("Server rule.", None, "Server rule."),
        (None, " Owner rule. ", "Owner rule."),
        (
            "Server rule.",
            "Owner rule.",
            "Server rule.\n\nThe session owner's personal title requirements follow. "
            "Where they conflict with the requirements above, follow these.\nOwner rule.",
        ),
    ],
)
async def test_compose_session_title_instructions(
    server: str | None, user: str | None, expected: str | None
) -> None:
    assert compose_session_title_instructions(server, user) == expected


async def test_background_title_uses_session_owner_instructions(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    resolved: list[str] = []
    seen: list[str | None] = []

    def owner_instructions(requested_session_id: str) -> str:
        resolved.append(requested_session_id)
        return "Start titles with an emoji."

    async def generator(request: BackgroundTitleRequest) -> str:
        seen.append(request.additional_instructions)
        return "🔐 Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Prefix titles with the team name.",
        user_instructions=owner_instructions,
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert resolved == [session_id]
    assert seen == [
        compose_session_title_instructions(
            "Prefix titles with the team name.", "Start titles with an emoji."
        )
    ]
    assert store.get_conversation(session_id).title == "🔐 Debug authentication timeout"


async def test_owner_lookup_failure_falls_back_to_server_instructions(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_session(store, "Investigate authentication timeout")
    seen: list[str | None] = []

    def failing_lookup(_session_id: str) -> str:
        raise RuntimeError("settings database unavailable")

    async def generator(request: BackgroundTitleRequest) -> str:
        seen.append(request.additional_instructions)
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store,
        generator,
        additional_instructions="Prefix titles with the team name.",
        user_instructions=failing_lookup,
    )
    coordinator.schedule(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
        expected_seed_title="Investigate authentication timeout",
    )
    await coordinator.wait_for_idle()

    assert seen == ["Prefix titles with the team name."]
    assert store.get_conversation(session_id).title == "Debug authentication timeout"


async def test_agent_title_uses_owner_instructions_without_server_default(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    seen: list[BackgroundTitleRequest] = []

    async def generator(request: BackgroundTitleRequest) -> str:
        seen.append(request)
        return "🔐 Auth timeout"

    coordinator = BackgroundSessionTitleCoordinator(
        store, generator, user_instructions=lambda _session_id: "Start titles with an emoji."
    )
    title = await coordinator.format_agent_title(
        BackgroundTitleRequest(session_id="conv_owner", prompt="Auth timeout")
    )

    assert title == "🔐 Auth timeout"
    assert [request.additional_instructions for request in seen] == ["Start titles with an emoji."]
    assert [request.owner_instructions for request in seen] == ["Start titles with an emoji."]
