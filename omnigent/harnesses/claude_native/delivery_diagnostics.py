"""Content-free, in-memory diagnostics for Claude terminal message delivery."""

from __future__ import annotations

import functools
import json
import logging
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypedDict, TypeVar, cast

_logger = logging.getLogger(__name__)
_InjectionFunction = TypeVar("_InjectionFunction", bound=Callable[..., Any])


@dataclass
class _PromptDeliveryTrace:
    delivery_id: str
    session_id: str | None
    started: float
    stage_started: float
    stage: str = "lock_wait"
    stage_seconds: dict[str, float] = field(default_factory=dict)
    attempts: list[dict[str, object]] = field(default_factory=list)


def set_stage(stage: str) -> None:
    """Accumulate the previous stage duration and mark the next stage; no logging or I/O."""
    trace = _prompt_delivery_trace.get()
    if trace is not None:
        now = time.monotonic()
        trace.stage_seconds[trace.stage] = (
            trace.stage_seconds.get(trace.stage, 0.0) + now - trace.stage_started
        )
        trace.stage = stage
        trace.stage_started = now


_prompt_delivery_trace: ContextVar[_PromptDeliveryTrace | None] = ContextVar(
    "claude_prompt_delivery_trace", default=None
)


def record_details(**attributes: object) -> None:
    trace = _prompt_delivery_trace.get()
    if trace is not None and trace.attempts:
        trace.attempts[-1].update(attributes)


def trace_delivery(
    *,
    session_id_reader: Callable[[Path], str | None],
    cancelled_error: type[Exception],
) -> Callable[[_InjectionFunction], _InjectionFunction]:
    """Collect one delivery summary without importing the bridge or its dependencies."""

    def decorate(function: _InjectionFunction) -> _InjectionFunction:
        """Collect metadata in memory; emit one summary after the injection lock is released."""

        @functools.wraps(function)
        def wrapped(bridge_dir: Path, *, content: str, **kwargs: Any) -> Any:
            # Hooks import this module on every invocation; keep the sink lazy.
            from omnigent.debug_logging import current_session_id, debug_event

            started = time.monotonic()
            trace = _PromptDeliveryTrace(
                delivery_id=secrets.token_hex(8),
                session_id=current_session_id() or session_id_reader(bridge_dir),
                started=started,
                stage_started=started,
            )
            token = _prompt_delivery_trace.set(trace)
            outcome = "returned"
            error_type = None
            try:
                return function(bridge_dir, content=content, **kwargs)
            except BaseException as exc:
                outcome = "interrupted" if isinstance(exc, cancelled_error) else "error"
                error_type = type(exc).__name__
                raise
            finally:
                try:
                    set_stage(trace.stage)
                    last_attempt = trace.attempts[-1] if trace.attempts else {}
                    verification = last_attempt.get("verification", "not_started")
                    uncertain = any(
                        attempt.get("verification") != "draft_absent" for attempt in trace.attempts
                    )
                    level = logging.WARNING if outcome != "returned" or uncertain else logging.INFO
                    if _logger.isEnabledFor(level):
                        normalized = content.replace("\r\n", "\n").replace("\r", "\n")
                        fields = {
                            "delivery_id": trace.delivery_id,
                            "stage": trace.stage,
                            "attempt": len(trace.attempts),
                            "elapsed_ms": round((time.monotonic() - trace.started) * 1000),
                            "content_bytes": len(content.encode("utf-8")),
                            "newline_count": normalized.count("\n"),
                            "leading_blank_line": bool(normalized)
                            and not normalized.split("\n", 1)[0].strip(),
                            "outcome": outcome,
                            "verification": verification,
                            **last_attempt,
                            **{
                                f"stage_{stage}_ms": round(seconds * 1000)
                                for stage, seconds in trace.stage_seconds.items()
                            },
                        }
                        if error_type is not None:
                            fields["error_type"] = error_type
                        if len(trace.attempts) > 1:
                            fields["attempts"] = json.dumps(trace.attempts)
                        extra = debug_event(
                            "claude_native_delivery_finished", session_id=trace.session_id
                        )
                        extra["attributes"] = fields
                        _logger.log(
                            level,
                            "claude_native_delivery_finished %s",
                            json.dumps(fields),
                            extra=extra,
                        )
                except Exception:  # noqa: BLE001 — diagnostics must not change delivery outcomes
                    pass
                finally:
                    _prompt_delivery_trace.reset(token)

        return cast(_InjectionFunction, wrapped)

    return decorate


class DraftObservation(TypedDict):
    capture_empty: bool
    prompt_glyph_visible: bool
    needle_visible_below_prompt: bool
    pane_rows: int
    pane_max_columns: int


def draft_observation(pane: str, needle: str, *, prompt_glyph: str) -> DraftObservation:
    """Summarize the last prompt row without retaining terminal or prompt text."""
    lines = pane.splitlines()
    prompt_rows = [index for index, line in enumerate(lines) if prompt_glyph in line]
    last_prompt = prompt_rows[-1] if prompt_rows else None
    return {
        "capture_empty": not pane.strip(),
        "prompt_glyph_visible": last_prompt is not None,
        "needle_visible_below_prompt": bool(needle)
        and last_prompt is not None
        and needle in "\n".join(lines[last_prompt + 1 :]),
        "pane_rows": len(lines),
        "pane_max_columns": max((len(line) for line in lines), default=0),
    }


def start_attempt() -> None:
    trace = _prompt_delivery_trace.get()
    if trace is not None:
        trace.attempts.append({"verification": "not_started", "submit_sent": False, "retries": 0})


def record_verification(
    verification: str,
    *,
    start: float,
    retries: int,
    polls: int,
    observation: DraftObservation,
) -> None:
    record_details(
        verification=verification,
        submit_wait_ms=round((time.monotonic() - start) * 1000),
        retries=retries,
        submit_polls=polls,
        **{f"submit_{key}": value for key, value in observation.items()},
    )
