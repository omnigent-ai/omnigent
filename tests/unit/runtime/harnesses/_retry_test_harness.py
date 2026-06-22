"""
Controllable fixture harness for the Phase 5 retry integration tests.

Implements the harness contract via :class:`HarnessApp` so the
runner subprocess can serve it like any production wrap. Behavior
is steered through env vars + a file-based counter (cross-process
state because each retry attempt spawns a fresh subprocess):

- ``RETRY_HARNESS_COUNTER_FILE``: path to a file holding a
  decimal integer. Incremented on every ``run_turn`` invocation.
  Initial value 0; first call sees 1 after increment.
- ``RETRY_HARNESS_BEHAVIOR``: ``"die_first_succeed_after"`` or
  ``"always_succeed"``. The first variant kills its own subprocess
  with ``SIGKILL`` on the first call (counter == 1) and emits
  a normal response thereafter; this exercises the L2-retry path
  in the AP-side workflow. The second variant always emits a
  normal response — used by the "no L2 retry on healthy harness"
  pin.

The "normal response" is two events: an ``OutputTextDeltaEvent``
carrying the text (so the streaming-pipeline branch fires for
:class:`TextChunk`) and the scaffold-emitted terminal
``response.completed``. Tests assert on the captured
``response.output_text.delta`` SSE events — that's the exact
end-to-end pipeline the user sees in the REPL.
"""

from __future__ import annotations

import logging
import os
import signal
from pathlib import Path

from fastapi import FastAPI

from omnigent.runtime.harnesses._scaffold import HarnessApp, TurnContext
from omnigent.server.schemas import CreateResponseRequest, OutputTextDeltaEvent

_logger = logging.getLogger(__name__)

# Env-var keys for steering behavior.
_ENV_COUNTER_FILE = "RETRY_HARNESS_COUNTER_FILE"
_ENV_BEHAVIOR = "RETRY_HARNESS_BEHAVIOR"

# Behavior tokens.
_BEHAVIOR_DIE_FIRST = "die_first_succeed_after"
_BEHAVIOR_ALWAYS_SUCCEED = "always_succeed"


def _bump_counter() -> int:
    """
    Atomically increment the cross-process call counter and
    return the post-increment value.

    File-based because each L2 retry attempt spawns a fresh
    subprocess; in-memory state would reset to zero on each
    spawn and the "die-then-succeed" sequence couldn't fire.

    :returns: The counter value AFTER increment, e.g. ``1`` on
        the first ``run_turn``.
    """
    path = os.environ.get(_ENV_COUNTER_FILE)
    if path is None:
        # No counter file configured — return 1 every time so
        # behaviors that key off "first call" still fire on the
        # first invocation per spawn. Test fixtures always set
        # the env var; this path is a defensive fallback.
        return 1
    counter_path = Path(path)
    if not counter_path.exists():
        counter_path.write_text("0", encoding="utf-8")
    current = int(counter_path.read_text(encoding="utf-8").strip() or "0")
    current += 1
    counter_path.write_text(str(current), encoding="utf-8")
    return current


class _RetryTestHarness(HarnessApp):
    """
    :class:`HarnessApp` subclass driven by env-var-configured
    behavior. See module docstring.
    """

    async def run_turn(
        self,
        request: CreateResponseRequest,
        ctx: TurnContext,
    ) -> None:
        """
        Drive one turn according to the configured behavior.

        :param request: The decoded request body. Ignored —
            behavior is environment-driven, not request-driven.
        :param ctx: The per-turn :class:`TurnContext` for
            emitting events.
        """
        del request  # behavior is env-driven
        count = _bump_counter()
        behavior = os.environ.get(_ENV_BEHAVIOR, _BEHAVIOR_ALWAYS_SUCCEED)

        if behavior == _BEHAVIOR_DIE_FIRST and count == 1:
            # SIGKILL the subprocess. The scaffold has already
            # opened the streaming response on AP's side; the
            # AP-side ``aiter_text`` raises ``ReadError`` once
            # the UDS socket closes, which the L2 retry layer
            # catches and re-issues against a fresh harness.
            _logger.warning(
                "_RetryTestHarness: BEHAVIOR=%s count=%d -> SIGKILL self",
                behavior,
                count,
            )
            os.kill(os.getpid(), signal.SIGKILL)
            return  # unreachable

        # Normal success path — emit one text delta then return.
        # The scaffold emits the terminal ``response.completed``
        # automatically once ``run_turn`` returns.
        ctx.emit(
            OutputTextDeltaEvent(
                type="response.output_text.delta",
                delta=f"ok-from-attempt-{count}",
            )
        )


def create_app() -> FastAPI:
    """
    Build the retry-test fixture harness's FastAPI app.

    Required entry point per the harness contract. Wires the
    scaffold around :class:`_RetryTestHarness`.

    :returns: A :class:`FastAPI` instance with the harness
        contract routes (POST /v1/responses, PATCH, cancel,
        etc.) backed by the test-controllable harness.
    """
    return _RetryTestHarness().build()
