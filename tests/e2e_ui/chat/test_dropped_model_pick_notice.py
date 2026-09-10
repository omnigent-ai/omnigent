"""E2E (hermetic): a dropped Fable pick must not silently run Opus 4.8.

On a managed Omnigent deployment (claude-native), the model picker offers
"Fable" even where the gateway cannot serve it. When a user picks
Fable, the runner launch gate finds the pick unservable, launches the provider
default (Opus 4.8) instead, and only writes a server-side log warning — the SPA
receives the harness's real model report (Opus) with NO event telling it the
pick was dropped. The result the user sees: their saved pick is Fable, the
session runs Opus, and nothing in the UI acknowledges the substitution.

This test drives the silent-substitution half on the ``web`` surface using the same hermetic
idiom as ``test_model_flows_contract.py``: the browser's view of one real
server-backed session is shaped into a claude-native snapshot whose saved pick
(``model_override="fable"``) diverges from the model the harness reports running
(``llm_model="system.ai.claude-opus-4-8"``) — the harness boundary, not the
driven surface. Driving the real ``claude`` CLI to the same launched+reported
state is not reachable in this sandbox (the CLI does not boot and report a model
within the credential-proxy per-call wall-clock cap), so the harness's model
report is injected at the snapshot boundary exactly as the repo's other
in-session model-contract rows do.

The assertion encodes the target behaviour, mirroring
``test_row15_failed_switch_surfaces_error_and_keeps_the_reported_model``: a pick
the harness did not honour must surface a user-visible signal. Row 15 covers the
mid-session *switch* path, which already publishes ``model_change_not_applied``
and shows the error pill; this covers the *launch* path, which stays silent. So
this test is RED on unmodified main (the substitution is silent) and turns green
once a fix surfaces the divergence.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native

#: The user's saved pick, offered by the managed picker but not
#: served by the gateway.
_FABLE_PICK = "fable"

#: The model the launch gate actually runs when the Fable pick is unservable:
#: the provider default (Opus 4.8), reported verbatim by the harness.
_SUBSTITUTED_MODEL = "system.ai.claude-opus-4-8"
_SUBSTITUTED_LABEL = "Opus 4.8"

#: The managed gateway catalog the bound session exposes: Opus 4.8 (default),
#: Sonnet 5, Haiku 4.5 — and notably NO Fable row (the gateway cannot serve it).
_MANAGED_CATALOG = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-8",
        "displayName": "Opus 4.8",
        "isDefault": True,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": False,
    },
    {
        "id": "haiku",
        "model": "system.ai.claude-haiku-4-5",
        "displayName": "Haiku 4.5",
        "isDefault": False,
    },
]


def _substitution_is_signaled(page: Page) -> tuple[bool, str]:
    """Whether the SPA surfaces any signal that the Fable pick was not honoured.

    A correct build must not silently substitute the pick. The two shapes a
    signal can take, either of which satisfies the contract:

    * the app's standard error surface (the ``error-headline`` pill), as the
      mid-session switch path already raises for ``model_change_not_applied``;
    * any visible notice naming Fable — e.g. "Fable is unavailable, running
      Opus 4.8 instead".

    :param page: The driven Playwright page, on the session view.
    :returns: ``(signaled, reason)`` — ``signaled`` True when a divergence
        signal is visible; ``reason`` describes what was (not) found.
    """
    error_pills = page.get_by_test_id("error-headline")
    if error_pills.count() > 0 and error_pills.first.is_visible():
        return True, f"error pill present: {error_pills.first.inner_text()!r}"

    fable_notice = page.get_by_text(re.compile(r"fable", re.IGNORECASE))
    if fable_notice.count() > 0 and fable_notice.first.is_visible():
        return True, f"fable notice present: {fable_notice.first.inner_text()!r}"

    return False, "no error pill and no visible Fable-unavailable notice"


def test_fable_pick_silently_runs_opus_without_any_signal(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A saved Fable pick that runs Opus 4.8 must not be presented silently.

    Shape a claude-native session whose saved pick is Fable but whose harness
    reports running Opus 4.8 (the launch-gate fallback), against a managed
    catalog that does not serve Fable. Load the session and observe: the chip
    shows Opus (the substitution really happened), and — the bug — the SPA
    surfaces no signal that the user's Fable pick was dropped.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session

    # The harness boundary: the saved pick (fable) diverges from the model the
    # harness reports running (opus-4-8), against a catalog with no Fable row.
    _patch_session_as_claude_native(
        page,
        session_id,
        model_override=_FABLE_PICK,
        model_options=_MANAGED_CATALOG,
        llm_model=_SUBSTITUTED_MODEL,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # The substitution really happened: the composer chip reports the model the
    # pane runs — Opus 4.8 — not the Fable the user picked.
    chip = page.get_by_test_id("composer-model-effort-label")
    chip.wait_for(state="visible", timeout=15_000)
    chip_text = chip.inner_text()
    assert _SUBSTITUTED_LABEL.split()[0] in chip_text, (
        "precondition: expected the chip to report the substituted running model "
        f"({_SUBSTITUTED_LABEL!r}); got {chip_text!r}"
    )
    assert "Fable" not in chip_text, (
        f"precondition: the chip should report the running model, not the pick; got {chip_text!r}"
    )

    # Give any error pill / notice the same settle window the switch-path row15
    # test allows before deciding the substitution went unsignalled.
    page.wait_for_timeout(1_500)

    signaled, reason = _substitution_is_signaled(page)
    assert signaled, (
        "silent substitution reproduced: the session's saved model pick is Fable, "
        f"the harness runs {_SUBSTITUTED_LABEL} ({_SUBSTITUTED_MODEL}), and the SPA "
        f"surfaces NO signal of the substitution ({reason}). The composer chip "
        f"silently shows {chip_text!r}. Unlike the mid-session switch path "
        "(model_change_not_applied → error pill), the launch-time fallback is "
        "silent — the user is never told their Fable pick was dropped."
    )
