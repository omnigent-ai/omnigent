from omnigent_slack.models import ThreadKey


def test_channel_thread_keys_on_root_ts_and_threads_replies() -> None:
    # A channel app_mention/reply keys on the thread root ts; replies thread there.
    root = ThreadKey.from_event("T1", {"channel": "C1", "ts": "100.1"})
    assert root.thread_ts == "100.1"
    assert root.reply_ts == "100.1"

    reply = ThreadKey.from_event("T1", {"channel": "C1", "thread_ts": "100.1", "ts": "101.9"})
    assert reply.thread_ts == "100.1"  # same session as the root
    assert reply.reply_ts == "100.1"


def test_dm_keys_per_thread_like_a_channel() -> None:
    # A DM maps one session PER THREAD (like a channel), NOT one per DM channel.
    # A top-level message keys on its own ts (a new thread/session); a threaded
    # reply keys on the thread root (reuses it).
    first = ThreadKey.from_event("T1", {"channel": "D1", "channel_type": "im", "ts": "100.1"})
    second = ThreadKey.from_event("T1", {"channel": "D1", "channel_type": "im", "ts": "200.2"})
    threaded = ThreadKey.from_event(
        "T1", {"channel": "D1", "channel_type": "im", "thread_ts": "100.1", "ts": "300.3"}
    )
    # Two distinct top-level DMs are DIFFERENT sessions.
    assert first != second
    assert first.thread_ts == "100.1" and second.thread_ts == "200.2"
    # A reply under the first thread's root reuses the first session.
    assert threaded == first
    # Replies always thread under the session root ts.
    assert first.reply_ts == "100.1"
    assert threaded.reply_ts == "100.1"
    # All are recognized as DMs (by the channel id's "D" prefix).
    assert first.is_dm and second.is_dm and threaded.is_dm


def test_dm_detected_by_channel_id_prefix_without_channel_type() -> None:
    # Some events omit channel_type; a "D"-prefixed channel id still means a DM,
    # and it keys per-thread on the message ts.
    key = ThreadKey.from_event("T1", {"channel": "D9", "ts": "100.1"})
    assert key.thread_ts == "100.1"
    assert key.is_dm
    assert key.reply_ts == "100.1"


def test_channel_key_is_not_dm() -> None:
    key = ThreadKey.from_event("T1", {"channel": "C1", "ts": "100.1"})
    assert not key.is_dm
