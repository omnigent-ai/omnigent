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
    refuses_change,
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
        # Forms the session's host normalizes to a matching host before it writes.
        "\u00a0" + _pr("git.example.org"),
        " " + _pr("git.example.org") + "\n",
    ],
)
def test_a_pull_request_on_a_matching_host_is_read_only(pr_url: str) -> None:
    assert is_read_only_pull_request(pr_url, PATTERNS)


def test_a_legacy_alias_is_matched_by_the_host_it_resolves_to() -> None:
    legacy = "https://example.visualstudio.com/project/_git/repo/pullrequest/7"

    assert is_read_only_pull_request(legacy, ("dev.azure.com",))
    assert is_read_only_pull_request(legacy, ("*.visualstudio.com",))


@pytest.mark.parametrize("pr_url", ["https://[not-a-host/pull/7", "not a url", "https:///pull/7"])
def test_a_pull_request_whose_host_cant_be_told_is_read_only_while_any_host_is(
    pr_url: str,
) -> None:
    assert is_read_only_pull_request(pr_url, PATTERNS)
    assert not is_read_only_pull_request(pr_url, ())


@pytest.mark.parametrize(
    "pr_url",
    [
        _pr("code.example.com"),
        _pr("github.com"),
        _pr("git.example.org.example.net"),
        "",
        None,
        7,
    ],
)
def test_other_pull_requests_are_writable(pr_url: object) -> None:
    assert not is_read_only_pull_request(pr_url, PATTERNS)


@pytest.mark.parametrize(
    ("pattern", "host", "matches"),
    [
        ("git?.example.org", "git1.example.org", False),
        ("git[12].example.org", "git1.example.org", False),
        ("*.example.org", "a.b.example.org", True),
        ("a.b.example", "aXb.example", False),
    ],
)
def test_only_a_star_is_a_wildcard(pattern: str, host: str, matches: bool) -> None:
    assert is_read_only_pull_request(_pr(host), (pattern,)) is matches


@pytest.mark.parametrize(
    ("target", "patterns", "refused"),
    [
        (None, PATTERNS, True),
        ("", PATTERNS, True),
        (None, (), False),
        (_pr("git.example.org"), PATTERNS, True),
        (_pr("github.com"), PATTERNS, False),
    ],
    ids=["no-target", "empty-target", "no-patterns", "read-only", "writable"],
)
def test_a_change_with_no_target_is_refused_only_while_a_host_is_read_only(
    target: object, patterns: tuple[str, ...], refused: bool
) -> None:
    assert refuses_change(target, patterns) is refused


def test_no_patterns_leave_every_pull_request_writable() -> None:
    assert not is_read_only_pull_request(_pr("one.code.example.com"), ())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ([" *.Code.Example.com ", "", 7, "git.example.org", "git.example.org"], PATTERNS),
        (
            ["https://git.example.org", "git.example.org/team", "*.code.example.com"],
            ("*.code.example.com",),
        ),
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
        # A newer host's control this server doesn't know may write to the code host.
        "draft_toggling": True,
    }
    info = {"object": "session.github.info", "capabilities": capabilities}

    marked = mark_read_only(info)

    assert marked["read_only"] is True
    assert marked["capabilities"] == {
        **dict.fromkeys(CODE_HOST_WRITE_CAPABILITIES, False),
        "commit_history": True,
        "account_switching": True,
        "draft_toggling": False,
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


def test_the_config_lists_its_read_only_hosts(caplog: pytest.LogCaptureFixture) -> None:
    config = {"read_only_code_hosts": ["*.Code.Example.com", "git.example.org"]}

    with caplog.at_level(logging.WARNING, logger=server_config.logger.name):
        assert server_config.read_only_code_hosts(config) == PATTERNS

    assert caplog.text == ""


@pytest.mark.parametrize(
    "entry",
    [123, None, "  ", "https://code.example.com", "code.example.com/team"],
    ids=["number", "null", "blank", "url", "path"],
)
def test_a_config_entry_that_isnt_a_host_name_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture, entry: object
) -> None:
    """So an operator can tell a policy that's on from one a typo left empty."""
    config = {"read_only_code_hosts": [entry, "git.example.org"]}

    with caplog.at_level(logging.WARNING, logger=server_config.logger.name):
        assert server_config.read_only_code_hosts(config) == ("git.example.org",)

    assert "read_only_code_hosts" in caplog.text
    assert repr(entry) in caplog.text


@pytest.mark.parametrize(
    "value", ["git.example.org", None, ("git.example.org",)], ids=["string", "null", "tuple"]
)
def test_a_config_value_that_isnt_a_list_is_ignored_with_a_warning(
    caplog: pytest.LogCaptureFixture, value: object
) -> None:
    with caplog.at_level(logging.WARNING, logger=server_config.logger.name):
        assert server_config.read_only_code_hosts({"read_only_code_hosts": value}) == ()

    assert "read_only_code_hosts" in caplog.text


def test_no_read_only_hosts_configured_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=server_config.logger.name):
        assert server_config.read_only_code_hosts({}) == ()

    assert caplog.text == ""
