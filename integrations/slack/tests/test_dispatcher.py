import asyncio

from omnigent_slack.dispatcher import ThreadTurnDispatcher
from omnigent_slack.models import SlackTurn, ThreadKey


def _turn(key: ThreadKey, text: str) -> SlackTurn:
    return SlackTurn(
        key=key,
        text=text,
        user_id="U",
        create_if_missing=False,
        title="title",
        slack_client=object(),
    )


async def test_dispatcher_runs_turns_in_thread_order() -> None:
    seen: list[str] = []
    done = asyncio.Event()

    async def worker(turn: SlackTurn) -> None:
        await asyncio.sleep(0)
        seen.append(turn.text)
        if len(seen) == 3:
            done.set()

    dispatcher = ThreadTurnDispatcher(worker, idle_timeout_seconds=0.1)
    key = ThreadKey(team_id="T", channel_id="C", thread_ts="1")

    for text in ["one", "two", "three"]:
        await dispatcher.enqueue(_turn(key, text))

    await asyncio.wait_for(done.wait(), timeout=1)
    await dispatcher.shutdown()

    assert seen == ["one", "two", "three"]


async def test_enqueue_during_idle_teardown_is_not_wedged() -> None:
    """A turn that arrives while an idle worker is tearing down must still run.

    Reproduces the race where ``_run_queue`` times out on an empty queue and
    decides to exit, then ``enqueue`` slips a turn in before the teardown
    ``finally`` reacquires the lock. The queue stays registered, so no new
    worker is ever spawned and the turn is stranded.
    """
    seen: list[str] = []
    processed = asyncio.Event()

    async def worker(turn: SlackTurn) -> None:
        seen.append(turn.text)
        processed.set()

    dispatcher = ThreadTurnDispatcher(worker, idle_timeout_seconds=0.05)
    key = ThreadKey(team_id="T", channel_id="C", thread_ts="1")

    # Gate the teardown's lock acquisition so a concurrent enqueue wins the
    # race: the worker has decided to exit but has not yet run its finally.
    original_lock = dispatcher._lock
    teardown_reached = asyncio.Event()
    release_teardown = asyncio.Event()

    class _GatedLock:
        def __init__(self) -> None:
            self._enter_count = 0

        async def __aenter__(self) -> None:
            self._enter_count += 1
            # The first acquisition after startup is enqueue's; the teardown
            # acquisition is the one we stall so enqueue can slip ahead.
            if self._enter_count == 2:
                teardown_reached.set()
                await release_teardown.wait()
            await original_lock.acquire()

        async def __aexit__(self, *exc: object) -> None:
            original_lock.release()

    dispatcher._lock = _GatedLock()  # type: ignore[assignment]

    # Let the worker spawn and hit its idle timeout → enter teardown.
    await dispatcher.enqueue(_turn(key, "first"))
    await asyncio.wait_for(processed.wait(), timeout=1)
    processed.clear()
    await asyncio.wait_for(teardown_reached.wait(), timeout=1)

    # Enqueue arrives before teardown finishes. Restore the real lock so the
    # new enqueue path (and any re-armed worker) runs unhindered.
    dispatcher._lock = original_lock  # type: ignore[assignment]
    await dispatcher.enqueue(_turn(key, "second"))
    release_teardown.set()

    await asyncio.wait_for(processed.wait(), timeout=1)
    await dispatcher.shutdown()

    assert seen == ["first", "second"]
