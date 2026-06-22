"""
Unit tests for :class:`omnigent_ui_sdk.OverlayAction` —
per-target action keybindings registered on a sidebar
overlay.

Covers the registration-time validation: action keys must
not collide with the overlay's close keys, trigger, or other
action keys. Failures here would manifest as silent wrong
behavior at runtime (a key that should run an action
instead closes the overlay, etc.) — pinning the loud
:class:`ValueError` keeps the contract explicit.

The actual key-press dispatch lives inside the prompt-
toolkit ``Application`` constructed in
:meth:`TerminalHost._show_overlay`; that path needs a real
terminal to exercise, so it's covered by the integration
tests in ``tests/e2e/test_repl_terminal_overview_e2e.py``
(via the supervisor agent's ``o`` / ``r`` keybindings on
the live overlay).
"""

from __future__ import annotations

import pytest
from omnigent_ui_sdk import (
    Overlay,
    OverlayAction,
    OverlayTarget,
    TerminalHost,
)


async def _noop_handler(target: OverlayTarget) -> None:
    """Stub action handler that does nothing.

    :param target: Selected target — ignored.
    """
    del target


async def _noop_targets_builder() -> list[OverlayTarget]:
    """Stub targets builder that returns an empty list.

    :returns: Empty list — concrete targets aren't needed for
        registration-time validation.
    """
    return []


async def _noop_builder(target: OverlayTarget | None) -> str:
    """Stub content builder.

    :param target: Selected target — ignored.
    :returns: Empty content.
    """
    del target
    return ""


def _make_overlay(actions: tuple[OverlayAction, ...]) -> Overlay:
    """Build a minimal :class:`Overlay` with the given *actions*.

    Centralizes the boilerplate so each test focuses on the
    *actions* tuple under test.

    :param actions: Actions to register on the overlay.
    :returns: A fully-constructed :class:`Overlay`.
    """
    return Overlay(
        trigger="c-o",
        builder=_noop_builder,
        targets_builder=_noop_targets_builder,
        title="Test",
        actions=actions,
    )


def test_overlay_action_registration_succeeds_with_unique_keys() -> None:
    """
    Two actions with distinct keys both register cleanly.

    The contract: each action's key adds a binding that fires
    against the currently-selected target. Distinct keys
    means no collision and ``add_overlay`` accepts the
    overlay.
    """
    overlay = _make_overlay(
        (
            OverlayAction(key="o", label="attach", handler=_noop_handler),
            OverlayAction(key="r", label="read-only", handler=_noop_handler),
        ),
    )
    host = TerminalHost()
    # No exception → registration accepted.
    host.add_overlay(overlay)


def test_overlay_action_collides_with_close_key_raises() -> None:
    """
    An action key that's also a close key must be rejected.

    What breaks if this passes: pressing the conflicting key
    inside the overlay would close it instead of running the
    action (close keys win at the binding-add level), with
    no diagnostic for the user. Failing loud at registration
    pins the misconfiguration to the line that wrote it.
    """
    overlay = _make_overlay(
        (OverlayAction(key="q", label="attach", handler=_noop_handler),),
    )
    host = TerminalHost()
    with pytest.raises(ValueError, match="collides with a close key or trigger"):
        host.add_overlay(overlay)


def test_overlay_action_collides_with_trigger_raises() -> None:
    """
    An action key matching the trigger must be rejected.

    The trigger is a close key by convention (the SDK treats
    it as such so re-pressing toggles the overlay shut), so
    this is technically a subset of the close-key check —
    but pinning it separately keeps the failure mode named
    clearly when someone wires a trigger that happens to be
    a popular action letter.
    """
    overlay = _make_overlay(
        (OverlayAction(key="c-o", label="attach", handler=_noop_handler),),
    )
    host = TerminalHost()
    with pytest.raises(ValueError, match="collides with a close key or trigger"):
        host.add_overlay(overlay)


def test_overlay_actions_with_duplicate_keys_raise() -> None:
    """
    Two actions sharing a key must be rejected.

    What breaks if this passes: only the second binding wins
    and the first action becomes unreachable. The user has
    no visible signal that one of their hotkeys is ignored —
    the footer hint even renders both, making the failure
    silent. Failing at registration with a clear message is
    the only safe option.
    """
    overlay = _make_overlay(
        (
            OverlayAction(key="o", label="attach", handler=_noop_handler),
            OverlayAction(key="o", label="other", handler=_noop_handler),
        ),
    )
    host = TerminalHost()
    with pytest.raises(ValueError, match="registered twice"):
        host.add_overlay(overlay)


def test_overlay_with_no_actions_remains_valid() -> None:
    """
    An overlay without actions still registers — actions are
    opt-in and the empty tuple is the sentinel for "I'm a
    plain content / sub-agent overlay, no per-target keys."

    Pinning so a future refactor that accidentally requires
    a non-empty ``actions`` tuple would break every existing
    overlay (the chat REPL has many).
    """
    overlay = _make_overlay(())
    host = TerminalHost()
    host.add_overlay(overlay)
