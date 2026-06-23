"""Import external-harness chat transcripts into an Omnigent conversation store.

Public surface:

- :class:`TranscriptAdapter` — the per-harness adapter interface.
- :class:`ParsedTranscript`, :class:`TranscriptRef` — adapter outputs.
- :func:`import_transcript`, :func:`import_all` — the core import functions.
- :func:`get_adapter`, :func:`available_harnesses` — registry access.
- :data:`IMPORTED_FROM_LABEL_KEY` — conversation-label key marking imports.

Adapters live in per-harness modules (``claude_code``, ``codex``); registering
a new one is a single entry in :mod:`omnigent.importers.registry`.
"""

from __future__ import annotations

from omnigent.importers.base import (
    IMPORTED_FROM_LABEL_KEY,
    ParsedTranscript,
    TranscriptAdapter,
    TranscriptRef,
)
from omnigent.importers.registry import (
    available_harnesses,
    get_adapter,
    import_all,
    import_transcript,
    persist_transcript,
)

__all__ = [
    "IMPORTED_FROM_LABEL_KEY",
    "ParsedTranscript",
    "TranscriptAdapter",
    "TranscriptRef",
    "available_harnesses",
    "get_adapter",
    "import_all",
    "import_transcript",
    "persist_transcript",
]
