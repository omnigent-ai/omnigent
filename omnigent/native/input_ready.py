"""Shared helpers for native-harness ``input_ready_probe`` hooks.

The runner's terminal watcher polls each native terminal's probe on every tick
until it reports the TUI accepts input (see ``NativeHarnessProvider``). Most
harnesses recognize a prompt marker in a single pane snapshot; the ones whose
TUI renders no fixed marker instead treat a pane that stops changing as ready,
which needs a little state across ticks.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance


class PaneSettledProbe:
    """Report ready once a terminal's non-empty pane is unchanged for N ticks.

    Mirrors the harness-side ``_settle_pane`` delivery gate, but driven by the
    watcher's own captures (``instance.last_pane_text()``) instead of fresh
    ``capture-pane`` calls. State is keyed per terminal instance and dropped
    once ready, since the watcher stops probing after the first ``True``.
    """

    def __init__(self, stable_polls: int) -> None:
        """
        :param stable_polls: Consecutive unchanged ticks that mark the pane
            settled, e.g. ``3``.
        """
        self._stable_polls = stable_polls
        self._lock = threading.Lock()
        self._panes: dict[str, tuple[str, int]] = {}

    def __call__(self, session_id: str, instance: TerminalInstance) -> bool:
        """
        :param session_id: Omnigent conversation id (unused; readiness is a
            property of the pane).
        :param instance: The live native terminal.
        :returns: Whether the pane has settled.
        """
        del session_id
        pane = instance.last_pane_text()
        key = instance.diagnostic_id
        with self._lock:
            if not pane:
                self._panes.pop(key, None)
                return False
            previous, stable = self._panes.get(key, ("", 0))
            stable = stable + 1 if pane == previous else 0
            if stable >= self._stable_polls:
                self._panes.pop(key, None)
                return True
            self._panes[key] = (pane, stable)
            return False
