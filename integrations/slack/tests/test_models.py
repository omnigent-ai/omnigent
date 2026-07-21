from omnigent_slack.models import ThreadKey


def test_channel_thread_keys_on_root_ts_and_threads_replies() -> None:
    # A channel app_mention/reply keys on the thread root ts; replies thread there.
    root = ThreadKey.from_event("T1", {"channel": "C1", "ts": "100.1"})
    assert root.thread_ts == "100.1"
    assert root.reply_ts == "100.1"

    reply = ThreadKey.from_event("T1", {"channel": "C1", "thread_ts": "100.1", "ts": "101.9"})
    assert reply.thread_ts == "100.1"  # same session as the root
    assert reply.reply_ts == "100.1"


def test_dm_keys_on_channel_so_all_messages_share_one_session() -> None:
    # Every message in a 1:1 DM — top-level or threaded, whatever its ts — keys on
    # the CHANNEL, so it maps to a single session (not one per message).
    first = ThreadKey.from_event("T1", {"channel": "D1", "channel_type": "im", "ts": "100.1"})
    second = ThreadKey.from_event("T1", {"channel": "D1", "channel_type": "im", "ts": "200.2"})
    threaded = ThreadKey.from_event(
        "T1", {"channel": "D1", "channel_type": "im", "thread_ts": "100.1", "ts": "300.3"}
    )
    assert first == second == threaded
    assert first.thread_ts == "D1"
    # DM replies post top-level (the channel id is not a valid thread_ts).
    assert first.reply_ts is None


def test_dm_detected_by_channel_id_prefix_without_channel_type() -> None:
    # Some events omit channel_type; a "D"-prefixed channel id still means a DM.
    key = ThreadKey.from_event("T1", {"channel": "D9", "ts": "100.1"})
    assert key.thread_ts == "D9"
    assert key.reply_ts is None
