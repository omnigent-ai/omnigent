"""Coverage for harness-agnostic stuck-pane diagnostics.

The point of these two measurements is that they say something useful about a
screen *nobody has written a marker for*, which is the case a cause table can
never cover.
"""

from __future__ import annotations

from omnigent.util.pane_diagnostics import (
    diagnose_pane,
    pane_fingerprint,
    pane_shape,
)

# The same screen as two different people would report it: different home
# directory, different OAuth nonce, different port.
_SSO_PANE_A = (
    "dbcert: Updated kubeconfig at path /home/ada.lovelace/.kube/config\n"
    "dbcert: Logging in via SSO...\n"
    "dbcert: If the browser does not open automatically, please open the following URL:\n"
    "\thttps://example.okta.com/oauth2/v1/authorize?client_id=0oa1rm2&code_challenge=s9TslT\n"
)
_SSO_PANE_B = (
    "dbcert: Updated kubeconfig at path /Users/grace.hopper/.kube/config\n"
    "dbcert: Logging in via SSO...\n"
    "dbcert: If the browser does not open automatically, please open the following URL:\n"
    "\thttps://example.okta.com/oauth2/v1/authorize?client_id=9zz4qq1&code_challenge=Kd0p2X\n"
)


def test_same_screen_fingerprints_alike_across_users() -> None:
    """Paths, ids and nonces must not split one screen into many groups.

    This is the whole value of the fingerprint: without it, grouping by pane
    text gives one row per person and nothing rises to the top.
    """
    assert pane_fingerprint(_SSO_PANE_A) == pane_fingerprint(_SSO_PANE_B)


def test_wrapped_lines_fingerprint_alike() -> None:
    """A narrower terminal wraps the same text; it is still the same screen."""
    wide = "Generating claude-code MCP client config...\nNo changes made to /h/u/.claude.json.\n"
    narrow = (
        "Generating claude-code MCP client\nconfig...\nNo changes made to\n/h/u/.claude.json.\n"
    )
    assert pane_fingerprint(wide) == pane_fingerprint(narrow)


def test_different_screens_fingerprint_differently() -> None:
    """Distinct failures must not collapse into one group."""
    other = "Managed settings need to be updated. You will be prompted for your password.\n"
    assert pane_fingerprint(_SSO_PANE_A) != pane_fingerprint(other)


def test_blank_pane_is_named_not_hashed() -> None:
    """A torn read is its own answer, not an opaque digest."""
    assert pane_fingerprint("") == "blank"
    assert pane_fingerprint("  \n \n") == "blank"
    assert pane_shape("") == ("blank",)


def test_shape_separates_a_missing_tui_from_a_drawn_one() -> None:
    """The most useful single fact about an unrecognized screen.

    No frame means the CLI never drew its UI, so it very likely never started —
    the pane is still showing whatever ran before it.
    """
    launcher_output = "Generating claude-code MCP client config...\nNo changes made to x.\n"
    assert "no-tui-frame" in pane_shape(launcher_output)

    framed = (
        "╭──────────────────────────────╮\n"
        "│ Claude Code                  │\n"
        "╰──────────────────────────────╯\n"
    )
    assert "tui-frame" in pane_shape(framed)


def test_shape_flags_an_unrecognized_screen_waiting_on_input() -> None:
    """A novel screen with no marker still says it wants a keypress."""
    novel = "Some future setup step nobody has seen\nContinue with the migration? [y/N]\n"
    shape = pane_shape(novel)
    assert "awaiting-input" in shape
    assert "no-tui-frame" in shape


def test_shape_flags_error_text_and_links() -> None:
    """Two more cause-independent hints: it crashed, or it wanted a browser."""
    crash = "Traceback (most recent call last):\n  File 'x.py', line 1\nRuntimeError: boom\n"
    assert "error-text" in pane_shape(crash)
    # The SSO screen ends on the link itself, so it reads as url-shown rather
    # than awaiting-input — the link is the affordance.
    assert "url-shown" in pane_shape(_SSO_PANE_A)


def test_diagnose_pane_renders_a_single_line_summary() -> None:
    """The form the readiness error and its log line embed."""
    described = diagnose_pane(_SSO_PANE_A).describe()
    assert described.startswith("shape=")
    assert " pane=" in described
    # A digest, never the person's pane text.
    assert "okta.com" not in described
    assert "ada.lovelace" not in described
