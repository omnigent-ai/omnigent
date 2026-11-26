"""Read-only code host patterns: the config, parsing, matching and the overview they mark."""

from __future__ import annotations

import logging

import pytest

from omnigent.server import server_config
from omnigent.server.read_only_code_hosts import (
    CODE_HOST_WRITE_CAPABILITIES,
    is_read_only_pull_request,
    mark_read_only,
    parse_patterns,
    selected_pull_request,
)

PATTERNS = ("*.code.example.com", "git.example.org")


def _pr(host: str) -> str:
    return f"https://{host}/example/app/pull/7"


@pytest.mark.parametrize(
    "pr_url",
    [
        _pr("one.code.example.com"),
        _pr("A.B.Code.Example.COM"),
        _pr("git.example.org"),
        _pr("git.example.org:8443"),
    ],
)
def test_a_pull_request_on_a_matching_host_is_read_only(pr_url: str) -> None:
    assert is_read_only_pull_request(pr_url, PATTERNS)


@pytest.mark.parametrize(
    "pr_url",
    [
        _pr("code.example.com"),
        _pr("github.com"),
        _pr("git.example.org.example.net"),
        "https://[not-a-host/pull/7",
        "",
        None,
        7,
    ],
)
def test_other_pull_requests_are_writable(pr_url: object) -> None:
    assert not is_read_only_pull_request(pr_url, PATTERNS)


def test_no_patterns_leave_every_pull_request_writable() -> None:
    assert not is_read_only_pull_request(_pr("one.code.example.com"), ())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([" *.Code.Example.com ", "", 7, "git.example.org", "git.example.org"], PATTERNS),
        ("*.code.example.com", ()),
        (None, ()),
        ({"host": "git.example.org"}, ()),
    ],
)
def test_patterns_are_parsed_from_a_list_of_strings(value: object, expected: tuple) -> None:
    assert parse_patterns(value) == expected


def test_a_marked_overview_offers_no_writes_and_keeps_its_reads_and_account_choice() -> None:
    capabilities = {
        **dict.fromkeys(CODE_HOST_WRITE_CAPABILITIES, True),
        "commit_history": True,
        "account_switching": True,
    }
    info = {"object": "session.github.info", "capabilities": capabilities}

    marked = mark_read_only(info)

    assert marked["read_only"] is True
    assert marked["capabilities"] == {
        **dict.fromkeys(CODE_HOST_WRITE_CAPABILITIES, False),
        "commit_history": True,
        "account_switching": True,
    }
    assert info["capabilities"] == capabilities
    assert {"merging", "comment_posting", "attachment_uploads"} <= CODE_HOST_WRITE_CAPABILITIES


@pytest.mark.parametrize(
    ("info", "expected"),
    [
        ({"selected_pr_url": _pr("a.example"), "pr": {"url": _pr("b.example")}}, _pr("a.example")),
        ({"pr": {"url": _pr("b.example")}}, _pr("b.example")),
        ({"pr": None}, None),
    ],
)
def test_an_overview_is_about_its_selected_pull_request(info: dict, expected: object) -> None:
    assert selected_pull_request(info) == expected


def test_the_config_lists_its_read_only_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    config = {"read_only_code_hosts": ["*.Code.Example.com", "git.example.org"]}
    monkeypatch.setattr(server_config, "load_server_config", lambda: config)

    assert server_config.read_only_code_hosts() == PATTERNS


def test_a_config_value_that_isnt_a_list_is_ignored_with_a_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        server_config, "load_server_config", lambda: {"read_only_code_hosts": "git.example.org"}
    )

    with caplog.at_level(logging.WARNING, logger=server_config.logger.name):
        assert server_config.read_only_code_hosts() == ()

    assert "read_only_code_hosts" in caplog.text
