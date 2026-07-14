import asyncio

from omnigent_slack.dispatcher import ThreadTurnDispatcher
from omnigent_slack.models import SlackTurn, ThreadKey


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
        await dispatcher.enqueue(
            SlackTurn(
                key=key,
                text=text,
                user_id="U",
                create_if_missing=False,
                title="title",
                slack_client=object(),
            )
        )

    await asyncio.wait_for(done.wait(), timeout=1)
    await dispatcher.shutdown()

    assert seen == ["one", "two", "three"]
