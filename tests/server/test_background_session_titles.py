from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from omnigent.entities import MessageData, NewConversationItem, SlashCommandData
from omnigent.runner.background_titles.service import FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION
from omnigent.server.background_session_titles import (
    BACKGROUND_SESSION_TITLES_HEADER,
    BACKGROUND_TITLE_MAX_CHARS,
    CUSTOM_BACKGROUND_TITLE_MAX_CHARS,
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    RunnerBackgroundTitleGenerator,
    background_session_titles_enabled,
    normalize_background_title,
    prepare_background_session_title,
    schedule_background_child_task_summary_for_event,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


def _seed_session(store: SqlAlchemyConversationStore, title: str) -> str:
    conversation = store.create_conversation(kind="default", title=title)
    return conversation.id


def _seed_child_session(store: SqlAlchemyConversationStore) -> str:
    parent = store.create_conversation(kind="default", title="parent")
    child = store.create_conversation(
        kind="default",
        title="researcher:researcher-1",
        parent_conversation_id=parent.id,
    )
    return child.id


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
        self.client = client


class _FakeRunnerRouter:
    def __init__(self, client: _FakeRunnerClient) -> None:
        self.client = client
        self.session_ids: list[str] = []

    def client_for_existing_conversation(self, session_id: str) -> _FakeRoutedRunner:
        self.session_ids.append(session_id)
        return _FakeRoutedRunner(self.client)


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


async def test_task_summary_retry_uses_latest_prompt_without_seed(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    prompts: list[str] = []

    async def generator(request: BackgroundTitleRequest) -> str | None:
        prompts.append(request.prompt)
        if len(prompts) == 1:
            return None
        return "Investigate authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule_task_summary(
        session_id=session_id,
        prompt="please investigate the authentication timeout",
    )
    await coordinator.wait_for_idle()
    coordinator.schedule_task_summary(
        session_id=session_id,
        prompt="continue investigating",
    )
    await coordinator.wait_for_idle()

    assert prompts == [
        "please investigate the authentication timeout",
        "continue investigating",
    ]
    conversation = store.get_conversation(session_id)
    assert conversation is not None
    assert conversation.task_summary == "Investigate authentication timeout"


async def test_task_summary_retry_uses_earliest_persisted_seed_prompt(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "Ignore me"}],
                    agent="researcher",
                ),
            ),
            NewConversationItem(
                type="slash_command",
                response_id="seed",
                data=SlashCommandData(
                    agent="researcher",
                    kind="skill",
                    name="investigate",
                    arguments="auth timeout",
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="seed",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Later user prompt"}],
                ),
            ),
        ],
    )
    prompts: list[str] = []

    async def generator(request: BackgroundTitleRequest) -> str:
        prompts.append(request.prompt)
        return "Investigate authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule_task_summary(session_id=session_id, prompt="Fallback prompt")
    await coordinator.wait_for_idle()

    assert prompts == ["/investigate auth timeout"]


async def test_task_summary_ignores_undelivered_persisted_prompt(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    store.append(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="turn_rejected",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Rejected prompt"}],
                ),
            )
        ],
    )
    prompts: list[str] = []

    async def generator(request: BackgroundTitleRequest) -> str:
        prompts.append(request.prompt)
        return "Investigate accepted prompt"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule_task_summary(session_id=session_id, prompt="Accepted prompt")
    await coordinator.wait_for_idle()

    assert prompts == ["Accepted prompt"]


async def test_task_summary_prefers_first_accepted_prompt_over_persisted_skill(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    prompts: list[str] = []

    async def generator(request: BackgroundTitleRequest) -> str:
        prompts.append(request.prompt)
        return "Investigate authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    coordinator.schedule_task_summary(session_id=session_id, prompt="Native pending prompt")
    store.append(
        session_id,
        [
            NewConversationItem(
                type="slash_command",
                response_id="turn_later",
                data=SlashCommandData(
                    agent="researcher",
                    kind="skill",
                    name="investigate",
                    arguments="later task",
                ),
            )
        ],
    )
    await coordinator.wait_for_idle()

    assert prompts == ["Native pending prompt"]


async def test_completed_task_summary_skips_stale_reschedule(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    stale_conversation = store.get_conversation(session_id)
    assert stale_conversation is not None
    calls = 0

    async def generator(_request: BackgroundTitleRequest) -> str:
        nonlocal calls
        calls += 1
        return "Investigate authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    assert schedule_background_child_task_summary_for_event(
        coordinator=coordinator,
        conversation=stale_conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "Initial prompt"}],
            },
        ),
    )
    await coordinator.wait_for_idle()
    assert schedule_background_child_task_summary_for_event(
        coordinator=coordinator,
        conversation=stale_conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "user",
                "content": [{"type": "input_text", "text": "Follow-up prompt"}],
            },
        ),
    )
    await coordinator.wait_for_idle()

    assert calls == 1


async def test_concurrent_task_summary_writers_keep_first_completed_summary(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    first_started = asyncio.Event()
    second_started = asyncio.Event()
    first_can_finish = asyncio.Event()
    second_can_finish = asyncio.Event()
    prompts: list[str] = []

    async def first_generator(request: BackgroundTitleRequest) -> str:
        prompts.append(request.prompt)
        first_started.set()
        await first_can_finish.wait()
        return f"Summary for {request.prompt}"

    async def second_generator(request: BackgroundTitleRequest) -> str:
        prompts.append(request.prompt)
        second_started.set()
        await second_can_finish.wait()
        return f"Summary for {request.prompt}"

    first = BackgroundSessionTitleCoordinator(store, first_generator)
    second = BackgroundSessionTitleCoordinator(store, second_generator)
    first.schedule_task_summary(session_id=session_id, prompt="Initial prompt")
    await asyncio.wait_for(first_started.wait(), timeout=5)
    second.schedule_task_summary(session_id=session_id, prompt="Follow-up prompt")
    await asyncio.wait_for(second_started.wait(), timeout=5)
    second_can_finish.set()
    await second.wait_for_idle()
    first_can_finish.set()
    await first.wait_for_idle()

    conversation = store.get_conversation(session_id)
    assert conversation is not None
    assert prompts == ["Initial prompt", "Follow-up prompt"]
    assert conversation.task_summary == "Summary for Follow-up prompt"


async def test_child_task_summary_ignores_assistant_message(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    session_id = _seed_child_session(store)
    conversation = store.get_conversation(session_id)
    assert conversation is not None

    async def generator(_request: BackgroundTitleRequest) -> str:
        return "Unused summary"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
    scheduled = schedule_background_child_task_summary_for_event(
        coordinator=coordinator,
        conversation=conversation,
        event=SessionEventInput(
            type="message",
            data={
                "role": "assistant",
                "content": [{"type": "input_text", "text": "Do not summarize me"}],
            },
        ),
    )

    assert scheduled is False


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
