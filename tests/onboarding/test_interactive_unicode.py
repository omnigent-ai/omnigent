"""Tests for the _SafeConsole UnicodeEncodeError safety wrapper.

See: https://github.com/omnigent-ai/omnigent/issues/2270
"""

import io
import sys
from unittest.mock import patch

import pytest
from rich.console import Console

from omnigent.onboarding.interactive import _SafeConsole


class TestSafeConsole:
    """Test _SafeConsole handles UnicodeEncodeError gracefully."""

    def test_safe_str_replaces_emoji_when_needed(self):
        """_safe_str replaces emoji with ASCII fallbacks when encoding fails."""
        # Simulate a terminal that can't encode emoji
        text = "\U0001f511 key provider"  # 🔑 key provider
        result = _SafeConsole._safe_str(text)
        assert "[key]" in result
        assert "key provider" in result

    def test_safe_str_preserves_ascii_text(self):
        """_safe_str returns ASCII text unchanged."""
        text = "Hello, world!"
        result = _SafeConsole._safe_str(text)
        assert result == text

    def test_safe_str_replaces_ticket_emoji(self):
        """_safe_str replaces 🎟️ with [ticket]."""
        text = "\U0001f39f\ufe0f subscription"
        result = _SafeConsole._safe_str(text)
        assert "[ticket]" in result

    def test_safe_str_replaces_globe_emoji(self):
        """_safe_str replaces 🌐 with [web]."""
        text = "\U0001f310 gateway"
        result = _SafeConsole._safe_str(text)
        assert "[web]" in result

    def test_safe_str_replaces_desktop_emoji(self):
        """_safe_str replaces 🖥️ with [local]."""
        text = "\U0001f5a5\ufe0f local"
        result = _SafeConsole._safe_str(text)
        assert "[local]" in result

    def test_print_handles_unicode_encode_error(self):
        """Console.print() retries with safe text on UnicodeEncodeError."""
        buf = io.StringIO()
        # Create a console with ASCII encoding to force UnicodeEncodeError
        console = _SafeConsole(file=buf, force_terminal=True)

        # Mock the parent print to raise UnicodeEncodeError on first call
        original_print = Console.print
        call_count = [0]

        def mock_print(self_inner, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Simulate what happens on cp1252 Windows
                raise UnicodeEncodeError(
                    "cp1252",
                    "\U0001f511",
                    0,
                    1,
                    "character maps to <undefined>",
                )
            return original_print(self_inner, *args, **kwargs)

        with patch.object(Console, "print", mock_print):
            console.print("\U0001f511 key provider")

        # Should have retried with safe text
        assert call_count[0] == 2

    def test_print_preserves_rich_markup(self):
        """Console.print() preserves Rich markup in output."""
        buf = io.StringIO()
        console = _SafeConsole(file=buf, force_terminal=True)
        console.print("[bold]Hello[/bold] world")
        output = buf.getvalue()
        assert "Hello" in output
        assert "world" in output

    def test_console_is_singleton_type(self):
        """The shared console is a _SafeConsole instance."""
        from omnigent.onboarding.interactive import console

        assert isinstance(console, _SafeConsole)

    def test_safe_str_handles_mixed_content(self):
        """_safe_str handles text with both emoji and ASCII."""
        text = "\U0001f511 OpenAI API Key"
        result = _SafeConsole._safe_str(text)
        assert "[key]" in result
        assert "OpenAI API Key" in result

    def test_safe_str_empty_string(self):
        """_safe_str handles empty string."""
        assert _SafeConsole._safe_str("") == ""

    def test_safe_str_all_ascii(self):
        """_safe_str returns all-ASCII text unchanged (fast path)."""
        text = "Pure ASCII text"
        assert _SafeConsole._safe_str(text) == text
