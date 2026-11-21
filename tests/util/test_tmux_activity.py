"""When a person last typed into a managed tmux pane."""

import pytest

from omnigent.util import tmux_activity


@pytest.mark.parametrize(
    ("clients", "stamp", "expected"),
    [
        # tmux keeps whole seconds, so a directly attached key may be a second later.
        ("0 1795227026\n", "", 1795227027.0),
        ("0 1795227026\n1 1795227050\n", "", 1795227027.0),
        ("1 1795227050\n", "1795227030.25", 1795227030.25),
        ("0 1795227026\n", "1795227030.25", 1795227030.25),
        ("", "", None),
        ("1 1795227050\n", "not a time", None),
    ],
    ids=[
        "attached-terminal",
        "control-clients-are-not-timed",
        "web-terminal-stamp",
        "latest-wins",
        "nobody",
        "unreadable-stamp",
    ],
)
def test_last_human_input_at_reads_attached_terminals_and_the_web_stamp(
    monkeypatch: pytest.MonkeyPatch, clients: str, stamp: str, expected: float | None
) -> None:
    def tmux_output(socket_path: str, *args: str) -> str:
        assert socket_path == "/tmp/example/tmux.sock"
        return clients if args[0] == "list-clients" else stamp

    monkeypatch.setattr(tmux_activity, "_tmux_output", tmux_output)

    assert tmux_activity.last_human_input_at("/tmp/example/tmux.sock", "main") == expected
