from omnigent_slack.notifications import (
    format_output_file,
    format_policy_denied,
    format_todos,
)
from omnigent_slack.omnigent import OutputFile


def test_format_todos_renders_marks_and_active_form() -> None:
    text = format_todos(
        [
            {"content": "Write tests", "status": "completed", "activeForm": "Writing tests"},
            {"content": "Ship it", "status": "in_progress", "activeForm": "Shipping it"},
            {"content": "Celebrate", "status": "pending", "activeForm": "Celebrating"},
        ]
    )
    assert text is not None
    assert ":white_check_mark: Write tests" in text
    # In-progress uses the gerund (activeForm).
    assert ":hourglass_flowing_sand: Shipping it" in text
    assert ":white_large_square: Celebrate" in text
    assert text.startswith("*Plan*")


def test_format_todos_empty_is_none() -> None:
    assert format_todos([]) is None
    # Entries with no usable label are skipped, leaving nothing to show.
    assert format_todos([{"status": "pending"}]) is None


def test_format_output_file_prefers_filename() -> None:
    assert "report.pdf" in format_output_file(OutputFile(file_id="f1", filename="report.pdf"))
    # Falls back to the id when unnamed.
    assert "f1" in format_output_file(OutputFile(file_id="f1"))


def test_format_policy_denied() -> None:
    text = format_policy_denied("No shell commands allowed.")
    assert "Blocked by policy" in text
    assert "No shell commands allowed." in text


def test_session_web_link_plain_server() -> None:
    """A plain server URL links straight to its /c/<id> conversation route."""
    import logging

    from omnigent_slack.notifications import SlackNotifier

    notifier = SlackNotifier(server_url="http://localhost:6767/", logger=logging.getLogger("test"))
    assert notifier._session_web_link("conv_abc") == "http://localhost:6767/c/conv_abc"


def test_session_web_link_maps_workspace_api_mount_to_ui() -> None:
    """A workspace-hosted API mount links to the /omnigent web UI, keeping ?o=.

    The bot is configured with the API proxy mount
    (``https://<ws>/api/2.0/omnigent``), but that mount answers JSON — the
    conversation page lives on the workspace SPA mount. The ``?o=<org>``
    selector must survive so multi-workspace hosts open the right workspace.
    """
    import logging

    from omnigent_slack.notifications import SlackNotifier

    notifier = SlackNotifier(
        server_url="https://ws.databricks.com/api/2.0/omnigent?o=123",
        logger=logging.getLogger("test"),
    )
    assert (
        notifier._session_web_link("conv_abc")
        == "https://ws.databricks.com/omnigent/c/conv_abc?o=123"
    )


async def test_context_disclosure_names_the_read_it_came_from() -> None:
    """The two reads say different true things, and neither claims the other.

    A first read forwards what was said before the bot arrived; a catch-up
    forwards what was said while it was quiet. Both are public in-thread, so the
    people whose words were forwarded see it rather than only the mentioner.
    """
    import logging

    from omnigent_slack.models import ThreadKey
    from omnigent_slack.notifications import SlackNotifier

    class Recorder:
        def __init__(self) -> None:
            self.posts: list[dict[str, object]] = []

        async def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
            self.posts.append(kwargs)
            return {"ok": True, "ts": "1"}

    notifier = SlackNotifier(server_url="http://s", logger=logging.getLogger("test"))
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    client = Recorder()

    await notifier.post_context_disclosure(client, key, 3, catch_up=False)  # type: ignore[arg-type]
    await notifier.post_context_disclosure(client, key, 2, catch_up=True)  # type: ignore[arg-type]
    # Nothing was forwarded, so nothing is claimed.
    await notifier.post_context_disclosure(client, key, 0, catch_up=True)  # type: ignore[arg-type]

    assert [post["text"] for post in client.posts] == [
        ":speech_balloon: 3 earlier message(s) from this thread were included as "
        "context for this session.",
        ":speech_balloon: 2 additional message(s) from this thread were included as "
        "context for this session.",
    ]
    assert all(post["thread_ts"] == "100.1" for post in client.posts)


async def test_a_failed_context_disclosure_never_raises() -> None:
    """Best-effort: the forwarding already happened, and the turn must not die
    over a notice about it."""
    import logging

    from omnigent_slack.models import ThreadKey
    from omnigent_slack.notifications import SlackNotifier

    class Broken:
        def __init__(self) -> None:
            self.attempts: list[dict[str, object]] = []

        async def chat_postMessage(self, **kwargs: object) -> dict[str, object]:
            self.attempts.append(kwargs)
            raise RuntimeError("slack is down")

    notifier = SlackNotifier(server_url="http://s", logger=logging.getLogger("test"))
    key = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")
    client = Broken()

    await notifier.post_context_disclosure(client, key, 3, catch_up=True)  # type: ignore[arg-type]

    # Not-raising is only half of it: a no-op would satisfy that too. The post
    # must actually have been attempted, with the disclosure on it.
    assert len(client.attempts) == 1
    assert "3 additional message(s)" in str(client.attempts[0]["text"])
