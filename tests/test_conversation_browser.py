"""Tests for browser-opening helpers used by CLI frontends."""

from __future__ import annotations

import subprocess

import pytest

import omnigent.conversation_browser as browser


@pytest.mark.parametrize(
    "url,expected",
    [
        # Databricks workspace API mount → the recognizable /omnigent SPA URL.
        (
            "https://e2-dogfood.staging.cloud.databricks.com/api/2.0/omnigent",
            "https://e2-dogfood.staging.cloud.databricks.com/omnigent",
        ),
        # A trailing ``?o=<org>`` selector on the API base is dropped.
        (
            "https://ws.databricks.com/api/2.0/omnigent?o=123",
            "https://ws.databricks.com/omnigent",
        ),
        # Trailing slash on the API mount still maps cleanly.
        (
            "https://ws.databricks.com/api/2.0/omnigent/",
            "https://ws.databricks.com/omnigent",
        ),
        # Non-Databricks URLs pass through unchanged (sans trailing slash).
        ("http://127.0.0.1:6767", "http://127.0.0.1:6767"),
        ("https://omnigent-02m5.onrender.com/", "https://omnigent-02m5.onrender.com"),
    ],
)
def test_display_server_url_maps_databricks_api_mount(url: str, expected: str) -> None:
    """
    ``display_server_url`` rewrites the Databricks API mount to the SPA URL.

    What this proves: the startup banner shows the workspace ``/omnigent``
    URL a user recognizes instead of the internal ``/api/2.0/omnigent``
    proxy path, while every other target is shown verbatim. A regression
    that stopped mapping would leak the API path back into the banner.

    :returns: None.
    """
    assert browser.display_server_url(url) == expected


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://ws.databricks.com/api/2.0/omnigent", True),
        ("https://ws.databricks.com/api/2.0/omnigent/", True),
        ("https://ws.databricks.com/omnigent", False),  # the SPA URL, not the API mount
        ("http://127.0.0.1:6767", False),
        ("https://omnigent-02m5.onrender.com", False),
    ],
)
def test_is_workspace_hosted_url(url: str, expected: bool) -> None:
    """
    ``is_workspace_hosted_url`` is true only for the workspace API mount.

    What this proves: the predicate the banner uses to suppress the
    server-version row fires for ``/api/2.0/omnigent`` and nothing else, so
    non-Databricks targets keep showing their version.

    :returns: None.
    """
    assert browser.is_workspace_hosted_url(url) is expected


def test_conversation_url_quotes_session_id() -> None:
    """
    Conversation URLs percent-encode ids before appending them to the base URL.

    :returns: None.
    """
    url = browser.conversation_url(
        "https://example.com/app/",
        "conv with/slash?query",
    )

    assert url == "https://example.com/app/c/conv%20with%2Fslash%3Fquery"


def test_open_conversation_url_uses_macos_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    macOS launches use the platform ``open`` command with the URL as one argv.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    calls: list[tuple[list[str], object, object, bool]] = []

    def fake_run(
        args: list[str],
        *,
        stdout: object,
        stderr: object,
        check: bool,
    ) -> subprocess.CompletedProcess[bytes]:
        """
        Capture the subprocess launch request.

        :param args: Command argv, e.g. ``["open", "http://..."]``.
        :param stdout: Captured stdout target.
        :param stderr: Captured stderr target.
        :param check: Whether non-zero exits should raise.
        :returns: Completed process object for the fake ``open`` command.
        """
        calls.append((args, stdout, stderr, check))
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(browser.sys, "platform", "darwin")
    monkeypatch.setattr(browser.subprocess, "run", fake_run)

    opened = browser.open_conversation_url("http://127.0.0.1:8000/c/conv_abc")

    assert opened is True
    assert calls == [
        (
            ["open", "http://127.0.0.1:8000/c/conv_abc"],
            browser.subprocess.DEVNULL,
            browser.subprocess.DEVNULL,
            False,
        )
    ]


def test_open_conversation_link_skips_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Disabled automatic browser opens return before building or opening a URL.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    warnings: list[str] = []

    def fail_open(url: str) -> bool:
        """
        Fail if the disabled guard calls the opener.

        :param url: Browser URL, e.g. ``"http://localhost/c/conv_abc"``.
        :returns: Never returns; this stub always fails the test.
        :raises AssertionError: Always raised when called.
        """
        raise AssertionError(f"disabled guard should not open {url}")

    monkeypatch.setattr(browser, "open_conversation_url", fail_open)

    browser.open_conversation_link_if_enabled(
        base_url="http://127.0.0.1:8000/",
        conversation_id="conv abc",
        enabled=False,
        warn=warnings.append,
    )

    assert warnings == []


def test_open_conversation_link_warns_when_opener_declines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Failed opener attempts surface a warning with the conversation URL.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    warnings: list[str] = []

    def fake_open(url: str) -> bool:
        """
        Simulate a platform opener that declines the URL.

        :param url: Browser URL, e.g. ``"http://localhost/c/conv_abc"``.
        :returns: ``False`` to signal that no opener accepted the URL.
        """
        return False

    monkeypatch.setattr(browser, "open_conversation_url", fake_open)

    browser.open_conversation_link_if_enabled(
        base_url="http://127.0.0.1:8000/",
        conversation_id="conv abc",
        enabled=True,
        warn=warnings.append,
    )

    assert warnings == [
        "Warning: no browser opener accepted conversation URL http://127.0.0.1:8000/c/conv%20abc"
    ]


def test_open_conversation_link_warns_when_opener_raises_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    OSError from the platform opener is surfaced through the warning callback.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    warnings: list[str] = []

    def fail_open(url: str) -> bool:
        """
        Simulate an opener executable that cannot be started.

        :param url: Browser URL, e.g. ``"http://localhost/c/conv_abc"``.
        :returns: Never returns because the opener raises.
        :raises OSError: Always raised to simulate a missing opener.
        """
        raise OSError("missing opener")

    monkeypatch.setattr(browser, "open_conversation_url", fail_open)

    browser.open_conversation_link_if_enabled(
        base_url="http://127.0.0.1:8000/",
        conversation_id="conv abc",
        enabled=True,
        warn=warnings.append,
    )

    assert warnings == [
        "Warning: failed to open conversation URL "
        "http://127.0.0.1:8000/c/conv%20abc: missing opener"
    ]


def test_conversation_url_maps_workspace_hosted_server_to_ui_mount(tmp_path, monkeypatch) -> None:
    """Workspace-hosted servers link to the SPA mount with the org selector.

    The server base is the API proxy (``/api/2.0/omnigent``) — linking
    there returns JSON, not the web UI. The browser URL must land on
    ``/omnigent`` and carry ``?o=<org>`` recorded by ``omnigent
    login`` so multi-org workspaces open in the right one.
    """
    from omnigent.cli_auth import store_databricks_auth
    from omnigent.conversation_browser import conversation_url

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    server = "https://example.databricks.com/api/2.0/omnigent"
    store_databricks_auth(
        server,
        "https://example.databricks.com",
        org_id="2850744067564480",
    )

    url = conversation_url(server, "conv_abc123")

    assert url == ("https://example.databricks.com/omnigent/c/conv_abc123?o=2850744067564480")


def test_conversation_url_workspace_hosted_without_org_record(tmp_path, monkeypatch) -> None:
    """No recorded org id → SPA mount link without the ?o selector.

    Single-org workspaces resolve fine without it; inventing an org id
    would be worse than omitting it.
    """
    from omnigent.conversation_browser import conversation_url

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )

    url = conversation_url("https://example.databricks.com/api/2.0/omnigent", "conv_abc123")

    assert url == "https://example.databricks.com/omnigent/c/conv_abc123"


def test_conversation_url_plain_server_unchanged(tmp_path, monkeypatch) -> None:
    """Non-workspace servers keep the plain /c/<id> link shape."""
    from omnigent.conversation_browser import conversation_url

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )

    assert (
        conversation_url("http://127.0.0.1:6767", "conv_abc123")
        == "http://127.0.0.1:6767/c/conv_abc123"
    )
