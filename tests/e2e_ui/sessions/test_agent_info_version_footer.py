"""Browser e2e for the agent-info version footer.

The top-right agent-info popover ends with a small version footer:
``server <version>``, plus ``· host <version>`` when the session is bound to
a connected host. This e2e proves the footer renders in the real UI and
surfaces the omnigent server version (from the boot capabilities probe).

The host segment is intentionally not asserted here: the e2e harness binds a
runner but no host tunnel, so the session is not host-bound and the footer
correctly shows the server version alone. Host-version plumbing is covered by
the backend (`tests/server/test_app.py`) and frontend unit suites.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect


def test_agent_info_version_footer_shows_server_version(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The header info popover shows the server version in its footer.

    Failure modes this catches:

    - The version footer never renders (AgentInfoContent dropped it, or the
      ``server_version`` field didn't reach the SPA via ``/v1/info``).
    - The footer renders but carries no ``server <version>`` text (the boot
      capabilities probe returned null and the guard let a blank line
      through).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")

    page.get_by_test_id("agent-info-trigger").click()

    footer = page.get_by_test_id("agent-info-versions")
    expect(footer).to_be_visible()
    # Footer leads with "server <version>"; assert the label is present and a
    # non-empty version follows it (not a blank "server " line).
    expect(footer).to_contain_text("server ")
    footer_text = footer.text_content() or ""
    server_segment = footer_text.split(" · ")[0]
    assert server_segment.startswith("server "), f"unexpected footer text: {footer_text!r}"
    server_version = server_segment[len("server ") :].strip()
    assert server_version, f"server version missing from footer: {footer_text!r}"
