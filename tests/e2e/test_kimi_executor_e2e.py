"""End-to-end tests for :class:`omnigent.inner.kimi_executor.KimiExecutor`.

Real-binary tests gated on:

- ``OMNIGENT_E2E_KIMI=1`` in the environment, and
- the ``kimi`` binary (or whichever ``HARNESS_KIMI_PATH`` points at)
  present on PATH.

When either gate fails the test is skipped — keeps CI green without the
upstream binary while still letting maintainers run the happy path locally
with ``OMNIGENT_E2E_KIMI=1 uv run pytest tests/e2e/test_kimi_executor_e2e.py``.

Mirrors ``tests/e2e/test_cursor_executor_e2e.py`` / ``test_pi_executor_e2e.py``
in shape.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from typing import Any

import pytest

from omnigent.inner.executor import TextChunk, TurnComplete
from omnigent.inner.kimi_executor import KimiExecutor, _resolve_kimi_binary


def _kimi_e2e_enabled() -> bool:
    if os.environ.get("OMNIGENT_E2E_KIMI") != "1":
        return False
    return shutil.which(_resolve_kimi_binary()) is not None


pytestmark = pytest.mark.skipif(
    not _kimi_e2e_enabled(),
    reason=(
        "Real-binary e2e: requires OMNIGENT_E2E_KIMI=1 and the ``kimi`` (or "
        "HARNESS_KIMI_PATH) binary on PATH. Install via "
        "`curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash` and "
        "run ``kimi login`` once, then re-run with OMNIGENT_E2E_KIMI=1."
    ),
)


async def _collect_events(executor: KimiExecutor, prompt: str) -> list[Any]:
    out: list[Any] = []
    async for event in executor.run_turn(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system_prompt="",
    ):
        out.append(event)
    return out


def test_kimi_run_turn_streams_text_against_real_binary() -> None:
    """Real kimi-cli driven by KimiExecutor produces a text response.

    Asks for a one-word answer to keep the run fast and the assertion
    deterministic without relying on exact wording (auth + model
    variability).
    """
    executor = KimiExecutor()
    events = asyncio.run(_collect_events(executor, "Reply with the single word: pong"))

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    turn_completes = [e for e in events if isinstance(e, TurnComplete)]
    assert text_chunks, "kimi produced no TextChunk events"
    assert turn_completes, "kimi did not emit TurnComplete"
    assert executor._session_id, "kimi did not surface a resume session id on stderr"


def test_kimi_run_turn_session_resume_carries_history() -> None:
    """A second run_turn with the same executor should see the prior turn.

    Verifies the executor captured the kimi UUID from the first turn's
    stderr footer and threaded it via ``--session`` on the next spawn.
    """
    executor = KimiExecutor()
    asyncio.run(_collect_events(executor, "Remember the word cactus. Reply with: ok."))
    first_session_id = executor._session_id
    assert first_session_id, "first turn did not surface a session id"

    events = asyncio.run(_collect_events(executor, "What single word did I ask you to remember?"))

    text_chunks = [e for e in events if isinstance(e, TextChunk)]
    response = " ".join(c.text for c in text_chunks).lower()
    assert "cactus" in response, f"second turn lost prior context: {response!r}"
    # The session id should be the same (resume reused the existing kimi session).
    assert executor._session_id == first_session_id
