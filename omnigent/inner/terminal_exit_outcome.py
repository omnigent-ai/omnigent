"""How a terminal's launched command ended, and how that travels to clients.

A shell kept on ``remain-on-exit`` leaves a dead pane whose wait status tmux
reports. The outcome rides two wires: the attach close reason, so the client
learns at once, and the ``session.resource.deleted`` event, which every route
receives. An unknown outcome sends nothing definitive, so clients keep the pane.
"""

from __future__ import annotations

import re
import signal as signals
from dataclasses import dataclass
from typing import Final

TERMINAL_ENDED_REASON: Final[str] = "terminal session ended"
# WebSocket close reasons are capped at 123 UTF-8 bytes.
_MAXIMUM_CLOSE_REASON_BYTES: Final[int] = 123
_SIGNAL_NAME = re.compile(r"^[A-Z][A-Z0-9+-]{0,31}$")


def _normalized_signal(raw: str) -> str:
    """tmux's signal text as a ``SIG`` name: macOS tmux 3.5a says ``kill``, others ``9``."""
    text = raw.strip()
    if not text:
        return text
    if text.isdigit():
        try:
            return signals.Signals(int(text)).name
        except ValueError:
            return text
    name = text.upper()
    return name if name.startswith("SIG") else f"SIG{name}"


@dataclass(frozen=True)
class TerminalExitOutcome:
    """The launched command's end, as tmux observed it.

    :param status: Exit code from ``#{pane_dead_status}``, e.g. ``0``, or ``None``
        when tmux reported none (still unreaped, or killed by a signal).
    :param signal: Signal name from ``#{pane_dead_signal}``, e.g. ``"SIGKILL"``.
    """

    status: int | None
    signal: str | None

    @property
    def definitive(self) -> bool:
        """Whether tmux reported either an exit code or a signal."""
        return self.status is not None or self._wire_signal() is not None

    def _wire_signal(self) -> str | None:
        if self.signal is None:
            return None
        name = _normalized_signal(self.signal)
        return name if _SIGNAL_NAME.match(name) else None

    def close_reason(self) -> str:
        """The attach close reason, with a parseable suffix when the end is known.

        Clients match ``exit=status:<n>`` or ``exit=signal:<NAME>``; anything else
        reads as unknown.
        """
        signal = self._wire_signal()
        if signal is not None:
            reason = f"{TERMINAL_ENDED_REASON}; exit=signal:{signal}"
        elif self.status is not None:
            reason = f"{TERMINAL_ENDED_REASON}; exit=status:{self.status}"
        else:
            return TERMINAL_ENDED_REASON
        if len(reason.encode("utf-8")) > _MAXIMUM_CLOSE_REASON_BYTES:
            return TERMINAL_ENDED_REASON
        return reason

    def event_payload(self) -> dict[str, int | str | None]:
        """The ``exit`` object of a ``session.resource.deleted`` event."""
        return {"status": self.status, "signal": self._wire_signal()}
