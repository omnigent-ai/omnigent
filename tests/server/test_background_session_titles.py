from __future__ import annotations

import asyncio
import time
import uuid

import pytest

from omnigent.server.background_session_titles import (
    BackgroundSessionTitleCoordinator,
    BackgroundTitleRequest,
    RunnerBackgroundTitleGenerator,
    background_session_titles_enabled,
    normalize_background_title,
    prepare_background_session_title,
)
from omnigent.server.schemas import SessionEventInput
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _enable_background_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_SESSION_RENAME", "1")


def _seed_session(store: SqlAlchemyConversationStore, title: str) -> str:
    conversation = store.create_conversation(kind="default", title=title)
    return conversation.id


async def test_prepare_background_title_is_disabled_by_default(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_SESSION_RENAME")
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

    assert pending is None


@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", "banana", " 1 "])
async def test_background_titles_require_exact_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_SESSION_RENAME", value)

    assert background_session_titles_enabled() is False


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
    assert pending.expected_seed_title == "please investigate the authentication timeout"


async def test_prepare_background_title_from_slash_command(db_uri: str) -> None:
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
    assert pending.expected_seed_title == "/grill-me review this plan"


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


@pytest.mark.parametrize("harness_override", ["codex-native", "pi"])
async def test_prepare_background_title_skips_unsupported_explicit_harnesses(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    harness_override: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_SESSION_RENAME", "1")
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
        await release.wait()
        return "Debug authentication timeout"

    coordinator = BackgroundSessionTitleCoordinator(store, generator)
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
    assert normalize_background_title("x" * 61) is None


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
            },
        )
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
