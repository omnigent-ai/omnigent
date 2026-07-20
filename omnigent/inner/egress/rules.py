"""Egress rule parsing and matching.

Rules use a simple DSL: ``METHODS host/path/pattern``

Examples::

    "GET api.github.com/orgs/myorg/**"       # GET only, specific path
    "GET,POST api.github.com/repos/myorg/**"  # Multiple methods
    "* pypi.org/**"                           # Any method
    "GET *.github.com/**"                     # Wildcard subdomain

- **Methods**: comma-separated HTTP verbs, or ``*`` for any.
- **Host**: exact match, or ``*.domain`` for wildcard subdomains.
- **Path**: glob — ``*`` matches one segment, ``**`` matches any depth.
- Default deny: requests not matching any rule are blocked.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# Strict DNS hostname grammar: ASCII letters, digits, dot, hyphen.
#
# Every byte outside this set is rejected — that single allowlist
# subsumes the full set of parser-differential / smuggling vectors
# called out by Anthropic's Claude Code sandbox-runtime CVE-class fix
# in 0.0.43:
#
# - ``\x00``: libc ``getaddrinfo`` truncates at NUL while Python
#   ``str.endswith`` treats it as an ordinary code unit, letting
#   ``attacker.com\x00.allowed.com`` slip past a ``*.allowed.com``
#   wildcard rule and then resolve to ``attacker.com``.
# - ``%``: percent-encoding smuggling. Many HTTP clients percent-
#   decode authority parts inconsistently from the rule layer, so
#   a host like ``attacker.com%2e.allowed.com`` opens another
#   client-vs-proxy parser differential.
# - ``\r`` / ``\n``: HTTP header / request smuggling via embedded
#   newlines in the host (CRLF injection class).
# - Whitespace, brackets, colons, ``@``, ``?``, ``#``, ``/``: any
#   byte that could feed a downstream URL / authority parser quirk.
#
# Trade-off: IPv6 literals (``[::1]``) and Unicode IDNs are rejected
# by this check. The proxy never supported IPv6 literals end-to-end
# (``_parse_host_port`` uses ``rsplit(':', 1)``), and IDN clients are
# expected to punycode (``xn--``) before egress — both are documented
# limitations, not regressions.
_DNS_SAFE_HOST_RE = re.compile(r"\A[A-Za-z0-9.\-]+\Z")


def is_dns_safe_host(host: str) -> bool:
    """Return ``True`` iff *host* contains only DNS-safe ASCII bytes.

    Rejects any character outside ``[A-Za-z0-9.-]``. See the module-
    level comment on ``_DNS_SAFE_HOST_RE`` for the full list of
    parser-differential smuggling vectors this single allowlist
    forecloses.

    :param host: The hostname extracted from a CONNECT target or an
        ``urlparse``-d HTTP URL.
    :returns: Whether *host* is safe to feed to the rule matcher and
        to ``socket.getaddrinfo``.
    """
    return bool(host) and _DNS_SAFE_HOST_RE.match(host) is not None


@dataclass(frozen=True)
class EgressRule:
    """A single parsed egress rule.

    :param methods: Allowed HTTP methods as upper-case strings, or
        ``{"*"}`` for any method.
    :param host_pattern: Host match pattern, e.g. ``"api.github.com"``
        or ``"*.github.com"`` for wildcard subdomains.
    :param path_pattern: Path glob, e.g. ``"/repos/myorg/**"``.
    """

    methods: frozenset[str]
    host_pattern: str
    path_pattern: str

    def matches(self, method: str, host: str, path: str) -> bool:
        """Return True if *method*, *host*, and *path* match this rule.

        :param method: HTTP method, e.g. ``"GET"``.
        :param host: Request hostname, e.g. ``"api.github.com"``.
        :param path: Request path, e.g. ``"/repos/myorg/repo"``.
        :returns: Whether this rule allows the request.
        """
        if not self._method_matches(method):
            return False
        if not self._host_matches(host):
            return False
        return self._path_matches(path)

    def _method_matches(self, method: str) -> bool:
        if "*" in self.methods:
            return True
        return method.upper() in self.methods

    def _host_matches(self, host: str) -> bool:
        # Defense-in-depth canonicalization: reject hosts that carry
        # any byte outside the DNS hostname grammar before doing the
        # wildcard ``endswith`` check. See ``is_dns_safe_host`` for
        # the full rationale — this is the redundant rule-layer guard
        # so any future caller of ``check_host`` / ``check_request``
        # that bypasses the proxy entry points still fails closed.
        if not is_dns_safe_host(host):
            return False
        host = host.lower()
        pattern = self.host_pattern.lower()
        if pattern.startswith("*."):
            suffix = pattern[1:]  # e.g. ".github.com"
            return host.endswith(suffix) or host == pattern[2:]
        return host == pattern

    def _path_matches(self, path: str) -> bool:
        if not path.startswith("/"):
            path = "/" + path
        return _glob_match(self.path_pattern, path)


_VALID_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "TRACE", "*"}
)


def parse_rule(rule_str: str) -> EgressRule:
    """Parse a rule string into an :class:`EgressRule`.

    :param rule_str: A rule like ``"GET api.github.com/repos/org/**"``.
    :returns: The parsed :class:`EgressRule`.
    :raises ValueError: On invalid syntax (empty rule, bad methods,
        missing host, etc.).
    """
    rule_str = rule_str.strip()
    if not rule_str:
        raise ValueError("Empty egress rule")

    parts = rule_str.split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"Egress rule must be 'METHODS host/path', got: {rule_str!r}")

    methods_str, url_part = parts

    method_parts = [m.strip() for m in methods_str.split(",")]
    if any(not m for m in method_parts):
        raise ValueError(f"Empty HTTP method in rule: {rule_str!r}")
    methods = frozenset(m.upper() for m in method_parts)
    if not methods:
        raise ValueError(f"No methods specified in rule: {rule_str!r}")
    bad = methods - _VALID_METHODS
    if bad:
        raise ValueError(f"Invalid HTTP method(s) {bad} in rule: {rule_str!r}")

    slash_idx = url_part.find("/")
    if slash_idx == -1:
        host_pattern = url_part
        path_pattern = "/**"
    else:
        host_pattern = url_part[:slash_idx]
        path_pattern = url_part[slash_idx:]

    if not host_pattern:
        raise ValueError(f"Empty host in rule: {rule_str!r}")

    if not path_pattern.startswith("/"):
        path_pattern = "/" + path_pattern

    return EgressRule(
        methods=methods,
        host_pattern=host_pattern,
        path_pattern=path_pattern,
    )


def parse_rules(rule_strings: Sequence[str]) -> list[EgressRule]:
    """Parse a list of rule strings.

    :param rule_strings: Iterable of rule DSL strings.
    :returns: List of parsed :class:`EgressRule` objects.
    :raises ValueError: On first invalid rule.
    """
    return [parse_rule(s) for s in rule_strings]


def check_request(rules: Sequence[EgressRule], method: str, host: str, path: str) -> bool:
    """Return True if *any* rule allows the request (default deny).

    :param rules: Parsed egress rules.
    :param method: HTTP method, e.g. ``"GET"``.
    :param host: Request hostname.
    :param path: Request path.
    :returns: Whether the request is allowed.
    """
    return any(r.matches(method, host, path) for r in rules)


def check_host(rules: Sequence[EgressRule], host: str) -> bool:
    """Return True if *any* rule could match this host (ignoring method/path).

    Useful for fast-rejecting CONNECT before TLS handshake.

    :param rules: Parsed egress rules.
    :param host: Hostname to check.
    :returns: Whether any rule matches this host.
    """
    return any(r._host_matches(host) for r in rules)


# ------------------------------------------------------------------
# Glob helpers
# ------------------------------------------------------------------

_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """Convert a path glob to a compiled regex.

    ``*``  matches one path segment (no slashes).
    ``**`` matches zero or more segments (including slashes).
    """
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached

    parts: list[str] = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if i + 1 < len(pattern) and pattern[i + 1] == "*":
                parts.append(".*")
                i += 2
                if i < len(pattern) and pattern[i] == "/":
                    i += 1
            else:
                parts.append("[^/]*")
                i += 1
        elif c == "?":
            parts.append("[^/]")
            i += 1
        else:
            parts.append(re.escape(c))
            i += 1

    regex = re.compile("^" + "".join(parts) + "$")
    _GLOB_CACHE[pattern] = regex
    return regex


def _glob_match(pattern: str, value: str) -> bool:
    return _glob_to_regex(pattern).match(value) is not None
