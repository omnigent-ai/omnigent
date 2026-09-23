"""Process-global registry of native prompts parked on a human.

A native TUI parked on its own approval, permission or question prompt prints
nothing and reports no runner turn, so every liveness signal reads idle while
a human-answerable card is still live. The in-runner mirrors that surface
those prompts open a *park* when the prompt becomes visible and close it when
it goes away; the native pane reaper never reaps a session with an open park
younger than its approval ceiling.

Parks follow the visible prompt, not the card POST: a mirror whose card POST
failed keeps its park open, because the prompt is still on screen.

Keys are ``"<owner>:<id>"`` strings, e.g. ``"goose:3"`` or
``"relay-policy:9f2c..."``, unique within a session. Timestamps are monotonic.
Thread-safe: the claude-native tool relay opens parks from its HTTP threads.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

_logger = logging.getLogger(__name__)
_lock = threading.Lock()
# session_id -> {park key -> monotonic time opened}
_parks: dict[str, dict[str, float]] = {}
_clock: Callable[[], float] = time.monotonic


def open_park(session_id: str, key: str) -> None:
    """Open a park for a prompt that just became visible (idempotent).

    Re-opening an already-open key keeps its original timestamp, so a mirror
    that re-asserts every poll cannot refresh its age.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param key: ``"<owner>:<id>"``, e.g. ``"kiro:req_7"``.
    """
    with _lock:
        _parks.setdefault(session_id, {}).setdefault(key, _clock())


def close_park(session_id: str, key: str) -> None:
    """Close one park; a no-op when it is not open.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param key: The key passed to :func:`open_park`.
    """
    with _lock:
        session_parks = _parks.get(session_id)
        if session_parks is None:
            return
        session_parks.pop(key, None)
        if not session_parks:
            _parks.pop(session_id, None)


def close_parks(session_id: str, prefix: str) -> None:
    """Close every park whose key starts with *prefix*, e.g. on mirror exit.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param prefix: Key prefix, e.g. ``"goose:"``.
    """
    with _lock:
        session_parks = _parks.get(session_id)
        if session_parks is None:
            return
        for key in [k for k in session_parks if k.startswith(prefix)]:
            del session_parks[key]
        if not session_parks:
            _parks.pop(session_id, None)


@contextmanager
def hold(session_id: str, key: str) -> Iterator[None]:
    """Keep a park open for the duration of a ``with`` block.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param key: ``"<owner>:<id>"``, unique for this wait.
    """
    open_park(session_id, key)
    try:
        yield
    finally:
        close_park(session_id, key)


class _Released:
    """Closes a mirror's parks on exit; usable with ``with`` and ``async with``."""

    def __init__(self, session_id: str, prefix: str) -> None:
        self._session_id = session_id
        self._prefix = prefix

    def __enter__(self) -> None:
        return None

    def __exit__(self, exc_type: type[BaseException] | None, *_rest: object) -> None:
        self._exit(exc_type)

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: type[BaseException] | None, *_rest: object) -> None:
        self._exit(exc_type)

    def _exit(self, exc_type: type[BaseException] | None) -> None:
        if exc_type is not None and issubclass(exc_type, Exception):
            # The mirror crashed, but its prompts may still be on screen and
            # nothing else will report them: keep them as holds, which expire
            # at the approval ceiling.
            kept = [key for key in open_keys(self._session_id) if key.startswith(self._prefix)]
            if kept:
                _logger.warning(
                    "prompt mirror %s for %s stopped with prompts open; keeping %d as holds",
                    self._prefix.rstrip(":"),
                    self._session_id,
                    len(kept),
                    extra={"session_id": self._session_id},
                )
            return
        close_parks(self._session_id, self._prefix)


def released(session_id: str, prefix: str) -> _Released:
    """Close every *prefix* park when a mirror's loop ends or is cancelled.

    A mirror that crashes leaves its parks open instead: its prompts may still
    be on screen with nothing else to report them.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param prefix: The mirror's key prefix, e.g. ``"goose:"``.
    """
    return _Released(session_id, prefix)


def oldest_open_age_s(session_id: str) -> float | None:
    """Age in seconds of the session's oldest open park, or ``None``.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    """
    with _lock:
        session_parks = _parks.get(session_id)
        if not session_parks:
            return None
        oldest = min(session_parks.values())
        return max(0.0, _clock() - oldest)


def open_keys(session_id: str) -> tuple[str, ...]:
    """Keys of the session's open parks, oldest first (for logs and tests).

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    """
    with _lock:
        session_parks = _parks.get(session_id, {})
        return tuple(sorted(session_parks, key=session_parks.__getitem__))


def clear_session(session_id: str) -> None:
    """Forget every park for a session whose pane was torn down.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    """
    with _lock:
        _parks.pop(session_id, None)
