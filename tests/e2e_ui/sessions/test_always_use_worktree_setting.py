"""E2E: Settings → Git "Always use a random worktree" default.

The toggle (``AlwaysUseWorktreeControl`` on ``pages/SettingsPage.tsx``) is a
Switch under Settings → Git. Turning it on writes
``localStorage["omnigent:always-use-worktree"] = "true"``; turning it off
removes the key (absence = off). The composer reads this when it settles a git
workspace and, when on, seeds a fresh worktree branch — see the composer-side
behavior in ``start_session/test_project_config_prefill.py``.

This file covers just the control: default off, persists on, clears on off, and
survives a reload. No LLM turn is needed.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

STORAGE_KEY = "omnigent:always-use-worktree"


def _stored(page: Page) -> str | None:
    """The persisted default, or None when unset (off)."""
    return page.evaluate(f"() => window.localStorage.getItem('{STORAGE_KEY}')")


def _open_git_settings(page: Page, base_url: str) -> None:
    """Navigate to Settings → Git and wait for the worktree toggle."""
    page.goto(f"{base_url}/settings/git")
    expect(page.get_by_test_id("settings-always-use-worktree-toggle")).to_be_visible(
        timeout=30_000
    )


def test_always_use_worktree_defaults_off_persists_and_clears(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """Off by default; toggling on persists and toggling off clears the key."""
    base_url, _session_id = seeded_session
    _open_git_settings(page, base_url)
    toggle = page.get_by_test_id("settings-always-use-worktree-toggle")

    # Fresh context → the toggle is off and nothing is stored.
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert _stored(page) is None, "a fresh load should store no worktree default"

    # Turn it on → persists as the literal "true".
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "true")
    assert _stored(page) == "true"

    # A reload keeps it on (seeded from storage).
    page.reload()
    toggle = page.get_by_test_id("settings-always-use-worktree-toggle")
    expect(toggle).to_have_attribute("aria-checked", "true", timeout=30_000)
    assert _stored(page) == "true", "the worktree default did not survive a reload"

    # Turn it off → the key is removed (absence is the off state).
    toggle.click()
    expect(toggle).to_have_attribute("aria-checked", "false")
    assert _stored(page) is None, "turning the default off should clear the storage key"
