"""E2E: the session's GitHub PR link is a chip in the gray bar above the composer.

A session with an associated GitHub PR shows a ``#<pr>`` link (GitHub icon +
number) in the composer area. It belongs in the gray directory/branch bar
above the composer (``composer-workspace-controls``), alongside the
working-directory and branch chips — never floating in the strip below the
composer card.

GitHub data comes from the runner-backed ``/resources/github*`` endpoints,
stubbed with canned JSON exactly like the rest of this directory
(:mod:`tests.e2e_ui.github.test_github_tab`), so the test exercises the
frontend layout without a real ``gh``/``git`` PR. No message is sent, so it
stays fast and LLM-free.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.github.test_github_tab import _PR_NUMBER, _stub_github


def test_pr_link_is_chip_in_workspace_bar_above_composer(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The ``#<pr>`` link renders in the gray bar above the composer, not below it."""
    base_url, session_id = seeded_session
    _stub_github(page)
    page.goto(f"{base_url}/c/{session_id}")

    # The session page is ready when the composer input mounts; the PR link
    # appears once the stubbed GitHub info resolves (a PR is associated).
    composer_input = page.get_by_role("textbox", name="Message the agent")
    expect(composer_input).to_be_visible(timeout=30_000)
    pr_link = page.get_by_test_id("composer-pr-link")
    expect(pr_link).to_be_visible(timeout=30_000)
    expect(pr_link).to_contain_text(f"#{_PR_NUMBER}")

    # The gray directory/branch bar sits above the composer card.
    workspace_bar = page.get_by_test_id("composer-workspace-controls")
    expect(workspace_bar).to_be_visible()

    # Let the layout settle so the bounding boxes below are stable.
    page.wait_for_timeout(1_500)

    pr_box = pr_link.bounding_box()
    input_box = composer_input.bounding_box()
    bar_box = workspace_bar.bounding_box()
    assert pr_box is not None and input_box is not None and bar_box is not None

    # Position: the PR link must sit entirely ABOVE the composer input, in
    # the gray-bar region — nothing PR-related may render underneath the
    # composer card.
    pr_bottom = pr_box["y"] + pr_box["height"]
    assert pr_bottom <= input_box["y"], (
        f"PR link renders below the composer (link bottom y={pr_bottom:.0f}, "
        f"composer input top y={input_box['y']:.0f}); it should be a chip in "
        f"the gray directory/branch bar above the composer "
        f"(bar top y={bar_box['y']:.0f})"
    )

    # Placement: the chip lives inside the gray bar, next to the
    # working-directory and branch chips. Strict-mode visibility also
    # guarantees no second, leftover PR link elsewhere on the page.
    expect(
        workspace_bar.get_by_test_id("composer-pr-link"),
        "the PR link should be a chip inside the gray directory/branch bar",
    ).to_be_visible()
