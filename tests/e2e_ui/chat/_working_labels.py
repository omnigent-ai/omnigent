"""Shared mirror of the working-indicator verb pool.

Mirror of ``WORKING_MESSAGES`` in
``web/src/components/chat/chatBubbleParts.tsx``. Which verb shows depends on the
wall-clock bucket the turn lands on, so tests accept any of them. Kept in one
place so the pool can't drift between the tests that assert on it.
"""

from __future__ import annotations

import re

WORKING_LABELS = (
    "Working…",
    "Cooking…",
    "Crunching…",
    "Tinkering…",
    "Pondering…",
    "Brewing…",
    "Noodling…",
    "Wrangling…",
    "Conjuring…",
    "Assembling…",
    "Percolating…",
    "Untangling…",
    "Scheming…",
    "Finagling…",
    "Whirring…",
    "Puzzling…",
)

WORKING_LABEL_RE = re.compile("|".join(re.escape(label) for label in WORKING_LABELS))
