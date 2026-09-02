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
