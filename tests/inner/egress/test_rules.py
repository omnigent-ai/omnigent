"""Tests for omnigent.inner.egress.rules — DSL parsing and matching."""

from __future__ import annotations

import pytest

from omnigent.inner.egress.rules import (
    check_host,
    check_request,
    parse_rule,
    parse_rules,
)

# ------------------------------------------------------------------
# parse_rule — happy paths
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_str,expected_methods,expected_host,expected_path",
    [
        (
            "GET api.github.com/repos/org/**",
            frozenset({"GET"}),
            "api.github.com",
            "/repos/org/**",
        ),
        (
            "GET,POST pypi.org/**",
            frozenset({"GET", "POST"}),
            "pypi.org",
            "/**",
        ),
        (
            "* *.amazonaws.com/**",
            frozenset({"*"}),
            "*.amazonaws.com",
            "/**",
        ),
        # Host-only (no path) defaults to /**
        (
            "GET example.com",
            frozenset({"GET"}),
            "example.com",
            "/**",
        ),
        # Leading/trailing whitespace stripped
        (
            "  DELETE  api.example.com/v1/items/*  ",
            frozenset({"DELETE"}),
            "api.example.com",
            "/v1/items/*",
        ),
    ],
)
def test_parse_rule_valid(
    rule_str: str,
    expected_methods: frozenset[str],
    expected_host: str,
    expected_path: str,
) -> None:
    rule = parse_rule(rule_str)
    # Methods are uppercased and stored as frozenset
    assert rule.methods == expected_methods
    assert rule.host_pattern == expected_host
    assert rule.path_pattern == expected_path


# ------------------------------------------------------------------
# parse_rule — error paths
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    "rule_str,expected_fragment",
    [
        ("", "Empty egress rule"),
        ("GET", "must be 'METHODS host/path'"),
        ("INVALID api.github.com/**", "Invalid HTTP method"),
        ("GET,INVALID api.github.com/**", "Invalid HTTP method"),
        ("FETCH api.github.com/**", "Invalid HTTP method"),
        ("GET, api.github.com/**", "Empty HTTP method"),
        ("GET,,POST api.github.com/**", "Empty HTTP method"),
        ("POST! api.github.com/**", "Invalid HTTP method"),
    ],
)
def test_parse_rule_invalid(rule_str: str, expected_fragment: str) -> None:
    with pytest.raises(ValueError, match=expected_fragment):
        parse_rule(rule_str)


# ------------------------------------------------------------------
# parse_rules
# ------------------------------------------------------------------


def test_parse_rules_list() -> None:
    rules = parse_rules(
        [
            "GET api.github.com/repos/**",
            "* pypi.org/**",
        ]
    )
    assert len(rules) == 2
    assert rules[0].host_pattern == "api.github.com"
    assert rules[1].host_pattern == "pypi.org"


def test_parse_rules_fails_on_first_invalid() -> None:
    with pytest.raises(ValueError, match="Empty egress rule"):
        parse_rules(["GET api.github.com/**", ""])


# ------------------------------------------------------------------
# EgressRule.matches
# ------------------------------------------------------------------


def test_matches_exact_host_and_path() -> None:
    rule = parse_rule("GET api.github.com/repos/myorg/**")
    # Allowed: GET to matching host+path
    assert rule.matches("GET", "api.github.com", "/repos/myorg/repo1") is True
    # Allowed: nested path
    assert rule.matches("GET", "api.github.com", "/repos/myorg/repo1/issues") is True
    # Denied: wrong method
    assert rule.matches("POST", "api.github.com", "/repos/myorg/repo1") is False
    # Denied: wrong host
    assert rule.matches("GET", "github.com", "/repos/myorg/repo1") is False
    # Denied: wrong path prefix
    assert rule.matches("GET", "api.github.com", "/users/myorg") is False


def test_matches_wildcard_subdomain() -> None:
    rule = parse_rule("* *.amazonaws.com/**")
    assert rule.matches("GET", "s3.amazonaws.com", "/bucket/key") is True
    assert rule.matches("PUT", "ec2.amazonaws.com", "/") is True
    # Bare domain without subdomain also matches
    assert rule.matches("GET", "amazonaws.com", "/x") is True
    # Non-matching suffix
    assert rule.matches("GET", "evil.com", "/") is False
    # Suffix lookalikes must not match: the wildcard requires a dot boundary.
    assert rule.matches("GET", "evilamazonaws.com", "/") is False
    assert rule.matches("GET", "amazonaws.com.evil.com", "/") is False


def test_matches_wildcard_method() -> None:
    rule = parse_rule("* pypi.org/**")
    assert rule.matches("GET", "pypi.org", "/simple/pkg") is True
    assert rule.matches("POST", "pypi.org", "/upload") is True


def test_matches_case_insensitive_host() -> None:
    rule = parse_rule("GET API.GitHub.COM/repos/**")
    assert rule.matches("GET", "api.github.com", "/repos/x") is True
    assert rule.matches("get", "API.GITHUB.COM", "/repos/x") is True


def test_matches_single_segment_wildcard() -> None:
    rule = parse_rule("GET api.example.com/v1/*/details")
    # Single-segment wildcard matches one segment only
    assert rule.matches("GET", "api.example.com", "/v1/item123/details") is True
    assert rule.matches("GET", "api.example.com", "/v1/a/b/details") is False


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/repos/issues", True),
        ("/repos/org/issues", True),
        ("/repos/org/project/issues", True),
        ("/repos/org/project/issues/1", False),
        ("/repos/org/project/pulls", False),
    ],
)
def test_matches_double_star_path_glob_depth(path: str, expected: bool) -> None:
    """``**`` spans zero or more path segments but still honors the
    remaining literal suffix.
    """
    rule = parse_rule("GET api.example.com/repos/**/issues")
    assert rule.matches("GET", "api.example.com", path) is expected


@pytest.mark.parametrize(
    "path,expected",
    [
        ("/files/report.txt", True),
        ("/files/.txt", True),
        ("/files/nested/report.txt", False),
        ("/files/report.md", False),
    ],
)
def test_matches_single_star_path_glob_stays_in_one_segment(path: str, expected: bool) -> None:
    rule = parse_rule("GET api.example.com/files/*.txt")
    assert rule.matches("GET", "api.example.com", path) is expected


# ------------------------------------------------------------------
# check_request / check_host — multi-rule evaluation
# ------------------------------------------------------------------


def test_check_request_any_rule_matches() -> None:
    rules = parse_rules(
        [
            "GET api.github.com/repos/**",
            "POST api.github.com/repos/**",
        ]
    )
    # GET matches first rule
    assert check_request(rules, "GET", "api.github.com", "/repos/x") is True
    # POST matches second rule
    assert check_request(rules, "POST", "api.github.com", "/repos/x") is True
    # DELETE matches neither — default deny
    assert check_request(rules, "DELETE", "api.github.com", "/repos/x") is False


def test_check_request_default_deny_empty_rules() -> None:
    # No rules => deny all
    assert check_request([], "GET", "example.com", "/") is False


def test_check_host_fast_reject() -> None:
    rules = parse_rules(["GET api.github.com/repos/**"])
    # Host matches at least one rule
    assert check_host(rules, "api.github.com") is True
    # Host doesn't match any rule
    assert check_host(rules, "evil.com") is False


@pytest.mark.parametrize(
    "smuggled_host,why",
    [
        # NUL byte: libc ``getaddrinfo`` truncates while Python
        # ``str.endswith`` treats it as an ordinary code unit. Same
        # parser differential as the Anthropic sandbox-runtime CVE
        # fix in 0.0.43.
        pytest.param(
            "attacker.example.com\x00.allowed.com",
            "NUL byte",
            id="nul-byte-middle",
        ),
        pytest.param("\x00api.allowed.com", "leading NUL", id="nul-byte-leading"),
        pytest.param("api.allowed.com\x00", "trailing NUL", id="nul-byte-trailing"),
        # Percent: URL-percent-encoded smuggling. ``%2e`` is ``.``,
        # ``%00`` is NUL, ``%2f`` is ``/`` — many HTTP clients decode
        # before sending while the rule layer matches raw bytes,
        # creating a fresh client-vs-proxy parser differential.
        pytest.param(
            "attacker.example.com%2e.allowed.com",
            "percent-encoded dot",
            id="percent-encoded-dot",
        ),
        pytest.param(
            "attacker.example.com%00.allowed.com",
            "percent-encoded NUL",
            id="percent-encoded-nul",
        ),
        # CR / LF: HTTP header / request smuggling via embedded
        # newlines in the host. ``readline()`` filters these from the
        # request line in practice, but the rule layer must still
        # fail closed for any future caller that bypasses
        # request-line parsing.
        pytest.param(
            "attacker.example.com\r.allowed.com",
            "CR injection",
            id="cr",
        ),
        pytest.param(
            "attacker.example.com\n.allowed.com",
            "LF injection",
            id="lf",
        ),
        pytest.param(
            "attacker.example.com\r\n.allowed.com",
            "CRLF injection",
            id="crlf",
        ),
        # Whitespace: would split the CONNECT target at request-line
        # parsing time, but ``check_host`` itself must reject it so
        # any future code path (e.g. ``Host:`` header lookup) is safe.
        pytest.param(
            "attacker example.com.allowed.com",
            "embedded space",
            id="whitespace",
        ),
        pytest.param(
            "attacker.example.com\t.allowed.com",
            "embedded tab",
            id="tab",
        ),
        # Brackets / colons: IPv6-literal-shaped smuggling. The proxy
        # does not support IPv6 literals (``_parse_host_port`` uses
        # ``rsplit(':', 1)``), so this is a documented trade-off, not
        # a regression.
        pytest.param(
            "attacker.example.com[.allowed.com]",
            "brackets",
            id="brackets",
        ),
        # ``@`` would smuggle as URL userinfo via ``urlparse``.
        pytest.param(
            "attacker@allowed.com",
            "URL userinfo",
            id="userinfo-at",
        ),
        # Empty host.
        pytest.param("", "empty host", id="empty"),
    ],
)
def test_unsafe_host_never_matches_any_rule(smuggled_host: str, why: str) -> None:
    """A host carrying any byte outside the DNS grammar ``[A-Za-z0-9.-]``
    must not match any rule, even one whose wildcard would otherwise
    apply under ``str.endswith`` semantics.

    Mirrors the canonicalization layer shipped in Anthropic sandbox-
    runtime 0.0.43. The proxy entry points reject these hosts before
    any rule match or DNS lookup; this is the redundant rule-layer
    guard so any future caller of ``check_host`` / ``check_request``
    that bypasses the entry points still fails closed.
    """
    rules = parse_rules(["* *.allowed.com/**"])
    # Sanity: the legitimate host matches.
    assert check_host(rules, "api.allowed.com") is True
    # The unsafe host must NOT match, regardless of suffix.
    assert check_host(rules, smuggled_host) is False, (
        f"{why}: {smuggled_host!r} should be rejected by the rule layer"
    )
    assert check_request(rules, "GET", smuggled_host, "/exfil") is False, (
        f"{why}: {smuggled_host!r} should be rejected at request layer"
    )


def test_str_endswith_would_have_matched_smuggled_hosts() -> None:
    """Document the underlying parser differential the canonicalization
    defends against.

    Pure-Python ``str.endswith`` happily reports True for hosts that
    libc's ``getaddrinfo`` truncates or that an HTTP-aware client
    interprets differently — this is the exact behavior that makes the
    explicit ``is_dns_safe_host`` check load-bearing. If any of these
    flip to False on a future Python, the defense's threat model has
    changed and this test will fail loudly.
    """
    suffix = ".allowed.com"
    smuggled = [
        "attacker.example.com\x00.allowed.com",
        "attacker.example.com\r.allowed.com",
        "attacker.example.com\n.allowed.com",
        "attacker.example.com\r\n.allowed.com",
        "attacker.example.com%2e.allowed.com",
        "attacker.example.com%00.allowed.com",
    ]
    for h in smuggled:
        assert h.endswith(suffix) is True, (
            f"str.endswith({h!r}, {suffix!r}) must be True — that is "
            f"the parser differential ``is_dns_safe_host`` is defending "
            f"against. If this assertion now fails, the threat model "
            f"has shifted and the defense rationale needs revisiting."
        )
