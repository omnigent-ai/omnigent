"""Classify file-descriptor exhaustion so pollers treat it as one outage, not a per-poll defect."""

from __future__ import annotations

import errno

_FD_EXHAUSTION_ERRNOS = frozenset({errno.EMFILE, errno.ENFILE})
# Bound the cause walk against pathological cause cycles.
_MAX_CAUSE_DEPTH = 10


def fd_exhaustion_errno(exc: BaseException) -> int | None:
    """Return ``EMFILE``/``ENFILE`` if *exc* or its explicit cause chain is fd exhaustion.

    Walks explicit causes only (``raise ... from exc``, e.g. httpx wrapping the
    socket error), not implicit context, so an unrelated error raised while
    handling an fd failure is not misclassified.

    :param exc: The exception a poll loop caught.
    :returns: The matching errno, or ``None`` for any other failure.
    """
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH):
        if current is None:
            return None
        if isinstance(current, OSError) and current.errno in _FD_EXHAUSTION_ERRNOS:
            return current.errno
        current = current.__cause__
    return None
