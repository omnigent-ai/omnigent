"""Tests for the claude-native "don't ask again" pure helpers.

The persistent allow-rule affordance for non-edit native tools relies
on two synchronous helpers in the sessions route:

- ``_allow_remember_eligible`` — server-side gate (stamp + verdict)
  deciding which tool/mode combinations may offer/honor the rule.
- ``_claude_native_remember_host`` — derives the WebFetch domain host
  the rule scopes to, with a tool-wide fallback.

Both are exercised end-to-end in the integration suite; these unit
tests pin the gating and URL-parsing edge cases directly.
"""

from __future__ import annotations

from omnigent.server.routes.sessions import (
    _allow_remember_eligible,
    _claude_native_remember_host,
)


class TestAllowRememberEligible:
    """Which (tool, mode) pairs may carry a persistent allow rule."""

    def test_webfetch_default_mode_eligible(self) -> None:
        assert _allow_remember_eligible("WebFetch", "default") is True

    def test_bash_default_mode_eligible(self) -> None:
        assert _allow_remember_eligible("Bash", "default") is True

    def test_eligible_with_absent_mode(self) -> None:
        # ``permission_mode`` can be absent on the payload; only
        # bypassPermissions (never prompts) is excluded.
        assert _allow_remember_eligible("WebFetch", None) is True

    def test_webfetch_eligible_under_accept_edits(self) -> None:
        # acceptEdits auto-approves only edit tools; a WebFetch still
        # prompts under it, so the rule is meaningful.
        assert _allow_remember_eligible("WebFetch", "acceptEdits") is True

    def test_bypass_permissions_not_eligible(self) -> None:
        # bypassPermissions never prompts (the hook doesn't fire), so a
        # rule would be inert.
        assert _allow_remember_eligible("WebFetch", "bypassPermissions") is False

    def test_edit_tools_not_eligible(self) -> None:
        # Edit tools take the acceptEdits/setMode path instead.
        for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
            assert _allow_remember_eligible(tool, "default") is False

    def test_exit_plan_mode_not_eligible(self) -> None:
        # ExitPlanMode has its own plan-review card.
        assert _allow_remember_eligible("ExitPlanMode", "plan") is False

    def test_ask_user_question_not_eligible(self) -> None:
        # AskUserQuestion is an interactive form, not a yes/no gate.
        assert _allow_remember_eligible("AskUserQuestion", "default") is False


class TestClaudeNativeRememberHost:
    """Domain derivation for WebFetch rule scoping."""

    def test_webfetch_simple_host(self) -> None:
        assert (
            _claude_native_remember_host("WebFetch", {"url": "https://github.com/cli/cli"})
            == "github.com"
        )

    def test_host_is_lowercased_and_port_stripped(self) -> None:
        # urlparse().hostname lowercases and drops the port.
        assert (
            _claude_native_remember_host("WebFetch", {"url": "https://GitHub.com:8443/x"})
            == "github.com"
        )

    def test_non_webfetch_tool_returns_none(self) -> None:
        # Only WebFetch gets a domain scope; everything else is tool-wide.
        assert _claude_native_remember_host("Bash", {"url": "https://github.com"}) is None

    def test_missing_url_returns_none(self) -> None:
        assert _claude_native_remember_host("WebFetch", {"prompt": "summarize"}) is None

    def test_non_dict_input_returns_none(self) -> None:
        assert _claude_native_remember_host("WebFetch", None) is None

    def test_url_without_scheme_returns_none(self) -> None:
        # No scheme → urlparse puts everything in ``path`` and hostname
        # is empty → tool-wide fallback.
        assert _claude_native_remember_host("WebFetch", {"url": "github.com/cli"}) is None

    def test_non_string_url_returns_none(self) -> None:
        assert _claude_native_remember_host("WebFetch", {"url": 123}) is None

    def test_userinfo_is_stripped(self) -> None:
        # Credentials in the authority must not leak into the rule host.
        assert (
            _claude_native_remember_host(
                "WebFetch", {"url": "https://user:pass@GitHub.com:8443/x"}
            )
            == "github.com"
        )

    def test_ipv6_host_brackets_stripped(self) -> None:
        # urlparse().hostname returns the IPv6 literal without brackets.
        assert (
            _claude_native_remember_host("WebFetch", {"url": "https://[2001:db8::1]:8443/x"})
            == "2001:db8::1"
        )

    def test_non_http_scheme_returns_none(self) -> None:
        # WebFetch domain rules are HTTP(S)-oriented; other schemes fall
        # back to a tool-wide rule rather than persisting a domain that
        # could never match a real fetch.
        assert _claude_native_remember_host("WebFetch", {"url": "ftp://github.com/x"}) is None
        assert _claude_native_remember_host("WebFetch", {"url": "file://host/path"}) is None
