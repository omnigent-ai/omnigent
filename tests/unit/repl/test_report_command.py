"""Unit tests for the TUI ``/report`` slash command.

Covers two surfaces:
- ``_build_github_issue_url`` — pure URL builder, tested directly.
- ``handle_slash_command("/report", ...)`` — the command handler,
  tested with a mocked browser and version calls.
"""

from __future__ import annotations

from urllib.parse import parse_qs, unquote, urlparse

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import (
    COMMANDS,
    _build_github_issue_url,
    handle_slash_command,
)
from tests.unit.repl.helpers import CapturingHost

_Host = CapturingHost

# ---------------------------------------------------------------------------
# Shared stubs
# ---------------------------------------------------------------------------


class _Session:
    """Minimal stub matching the surface ``/report`` reads.

    Concrete class so an unexpected attribute access fails loudly.
    """

    def __init__(
        self,
        *,
        session_id: str | None = "sess_test123",
        model: str = "test-agent",
    ) -> None:
        self.session_id = session_id
        self.model = model


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decode_url(url: str) -> dict[str, str]:
    """Parse a pre-filled GitHub new-issue URL into its query params."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return {k: unquote(v[0]) for k, v in qs.items()}


# ---------------------------------------------------------------------------
# _build_github_issue_url — pure function
# ---------------------------------------------------------------------------


def test_build_github_issue_url_base_structure() -> None:
    """URL targets the omnigent-ai/omnigent issues/new endpoint.

    Failure: the URL would open the wrong repo or be unparseable,
    meaning users file issues in the wrong place.
    """
    url = _build_github_issue_url("sess_abc", "my-agent", "")
    assert url.startswith("https://github.com/omnigent-ai/omnigent/issues/new")


def test_build_github_issue_url_title_and_labels() -> None:
    """Title is ``[Bug] TUI issue`` and labels include the triaging set.

    Failure: issues land without triage labels or with wrong title,
    making them hard to route.
    """
    url = _build_github_issue_url("sess_abc", "my-agent", "")
    params = _decode_url(url)
    assert params["title"] == "[Bug] TUI issue"
    assert "bug" in params["labels"]
    assert "area/harnesses" in params["labels"]


def test_build_github_issue_url_session_id_in_body() -> None:
    """Session ID appears in the issue body for triage lookup.

    Failure: the session ID is absent, blocking engineers from
    reproducing the report with server-side logs.
    """
    url = _build_github_issue_url("sess_abc123", "my-agent", "")
    params = _decode_url(url)
    assert "sess_abc123" in params["body"]


def test_build_github_issue_url_description_in_body() -> None:
    """Free-text description lands in the 'What happened' section.

    Failure: the description is lost, so the pre-filled form is
    empty and the user must re-type it.
    """
    url = _build_github_issue_url("sess_x", "my-agent", "agent stopped responding mid-turn")
    params = _decode_url(url)
    assert "agent stopped responding mid-turn" in params["body"]


def test_build_github_issue_url_includes_version_and_os_when_provided() -> None:
    """Version and OS appear when supplied, aiding reproducibility.

    Failure: the fields are absent so engineers can't correlate the
    report to a specific release or platform.
    """
    url = _build_github_issue_url(
        "sess_x",
        "my-agent",
        "",
        version="1.2.3",
        os_info="macOS 14.4",
    )
    params = _decode_url(url)
    assert "1.2.3" in params["body"]
    assert "macOS 14.4" in params["body"]


def test_build_github_issue_url_omits_version_and_os_when_none() -> None:
    """Version and OS lines are absent when not provided.

    Failure: placeholder text or 'None' would pollute the issue body.
    """
    url = _build_github_issue_url("sess_x", "my-agent", "", version=None, os_info=None)
    params = _decode_url(url)
    assert "**Version:**" not in params["body"]
    assert "**OS:**" not in params["body"]


def test_build_github_issue_url_session_none_renders_not_started() -> None:
    """'not started' appears when no session exists yet.

    Failure: 'None' or an empty field would appear, confusing readers
    who don't know to interpret it as a pre-session report.
    """
    url = _build_github_issue_url(None, "my-agent", "")
    params = _decode_url(url)
    assert "not started" in params["body"]


# ---------------------------------------------------------------------------
# /report command — via handle_slash_command
# ---------------------------------------------------------------------------


def test_report_command_registered() -> None:
    """/report appears in the COMMANDS registry.

    Failure: the command is invisible to /help and the tab-completer,
    so users can't discover it.
    """
    assert "/report" in COMMANDS
    assert "GitHub issue" in COMMANDS["/report"][0] or "report" in COMMANDS["/report"][0].lower()


@pytest.mark.asyncio
async def test_report_command_opens_browser_and_confirms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful browser open produces a confirmation message.

    Failure: the user has no feedback that anything happened, and may
    repeat the command thinking it didn't work.
    """
    opened_urls: list[str] = []

    def _fake_open(url: str) -> bool:
        opened_urls.append(url)
        return True  # browser opened successfully

    monkeypatch.setattr("webbrowser.open", _fake_open)

    host = _Host()
    session = _Session(session_id="sess_abc", model="my-agent")
    await handle_slash_command("/report", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]

    assert "browser" in host.text.lower(), (
        f"Expected browser confirmation message, got: {host.text!r}"
    )
    # Exactly one URL was opened.
    assert len(opened_urls) == 1, f"Expected 1 URL opened, got {opened_urls!r}"
    assert "github.com" in opened_urls[0], "Opened URL should point to GitHub"


@pytest.mark.asyncio
async def test_report_command_prints_url_when_browser_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When webbrowser.open() returns False the URL is printed instead.

    Failure: users on headless machines see no output and cannot file
    the issue at all.
    """
    monkeypatch.setattr("webbrowser.open", lambda url: False)

    host = _Host()
    session = _Session(session_id="sess_abc", model="my-agent")
    await handle_slash_command("/report", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]

    assert "github.com" in host.text, (
        f"Expected GitHub URL in output when browser unavailable, got: {host.text!r}"
    )


@pytest.mark.asyncio
async def test_report_command_includes_description_arg_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Text after /report is included in the pre-filled issue body.

    Failure: the description is silently dropped, so the user's
    carefully typed context never reaches the issue.
    """
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url) or True)

    host = _Host()
    session = _Session(session_id="sess_abc", model="my-agent")
    await handle_slash_command(
        "/report agent crashed on startup",
        session,
        None,
        host,
        RichBlockFormatter(),
    )  # type: ignore[arg-type]

    assert len(opened_urls) == 1
    body = _decode_url(opened_urls[0])["body"]
    assert "agent crashed on startup" in body, f"Description not found in issue body: {body!r}"


@pytest.mark.asyncio
async def test_report_command_includes_version_in_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omnigent version is included in the issue body when available.

    Failure: the version field is blank, making it impossible to tell
    which release a report came from.
    """
    opened_urls: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url) or True)
    # Patch importlib.metadata.version to return a known string.
    monkeypatch.setattr(
        "importlib.metadata.version",
        lambda pkg: "9.8.7" if pkg == "omnigent" else "0.0.0",
    )

    host = _Host()
    session = _Session()
    await handle_slash_command("/report", session, None, host, RichBlockFormatter())  # type: ignore[arg-type]

    assert len(opened_urls) == 1
    body = _decode_url(opened_urls[0])["body"]
    assert "9.8.7" in body, f"Version not found in issue body: {body!r}"
