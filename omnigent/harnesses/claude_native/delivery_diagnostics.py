"""Content-free, in-memory diagnostics for Claude terminal message delivery."""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import secrets
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ParamSpec, TypedDict, TypeVar, cast

_logger = logging.getLogger(__name__)
_InjectionFunction = TypeVar("_InjectionFunction", bound=Callable[..., Any])

_Parameters = ParamSpec("_Parameters")
_TMUX_OPERATIONS = frozenset({"send-keys", "load-buffer", "paste-buffer", "kill-session"})
# Claude Code's composer placeholder while a submitted message waits for a busy turn.
_QUEUED_PLACEHOLDER = "Press up to edit queued messages"
# Written by the statusLine wrapper (see ``status.py``); read here without the bridge.
_CONTEXT_FILE = "context.json"
_LEADING_CHAR_CLASSES = {"/": "slash", "!": "bang", "@": "at", "#": "hash", "?": "question"}


def _best_effort(function: Callable[_Parameters, None]) -> Callable[_Parameters, None]:
    @functools.wraps(function)
    def wrapped(*args: _Parameters.args, **kwargs: _Parameters.kwargs) -> None:
        with contextlib.suppress(Exception):
            function(*args, **kwargs)

    return wrapped


@dataclass
class _PromptDeliveryTrace:
    delivery_id: str
    session_id: str | None
    started: float
    stage_started: float
    stage: str = "lock_wait"
    stage_seconds: dict[str, float] = field(default_factory=dict)
    attempts: list[dict[str, object]] = field(default_factory=list)
    input_attributes: dict[str, object] = field(default_factory=dict)
    tmux_failure: dict[str, object] = field(default_factory=dict)


@_best_effort
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


@_best_effort
def record_details(**attributes: object) -> None:
    trace = _prompt_delivery_trace.get()
    if trace is not None and trace.attempts:
        trace.attempts[-1].update(attributes)


@_best_effort
def record_tmux_failure(
    operation: str,
    *,
    returncode: int | None = None,
    timeout_s: float | None = None,
    os_errno: int | None = None,
) -> None:
    """Retain the first failing operation without command arguments or terminal output."""
    trace = _prompt_delivery_trace.get()
    if trace is None or trace.tmux_failure:
        return
    trace.tmux_failure = {
        "tmux_operation": operation if operation in _TMUX_OPERATIONS else "other",
    }
    for name, value in (
        ("tmux_returncode", returncode),
        ("tmux_timeout_s", timeout_s),
        ("tmux_os_errno", os_errno),
    ):
        if value is not None:
            trace.tmux_failure[name] = value


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
            try:
                # Hooks import this module on every invocation; keep the sink lazy.
                from omnigent.debug_logging import current_session_id, debug_event
                from omnigent.native.input_diagnostics import current_input_attributes

                started = time.monotonic()
                trace = _PromptDeliveryTrace(
                    delivery_id=secrets.token_hex(8),
                    session_id=current_session_id() or session_id_reader(bridge_dir),
                    started=started,
                    stage_started=started,
                    input_attributes=current_input_attributes(),
                )
                token = _prompt_delivery_trace.set(trace)
            except Exception:  # noqa: BLE001 — trace setup must not prevent delivery
                return function(bridge_dir, content=content, **kwargs)
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
                            **trace.input_attributes,
                            "delivery_id": trace.delivery_id,
                            "stage": trace.stage,
                            "attempt": len(trace.attempts),
                            "elapsed_ms": round((time.monotonic() - trace.started) * 1000),
                            "content_bytes": len(content.encode("utf-8")),
                            "newline_count": normalized.count("\n"),
                            "leading_blank_line": bool(normalized)
                            and not normalized.split("\n", 1)[0].strip(),
                            **_content_shape(content),
                            **_claude_code_version(bridge_dir),
                            "outcome": outcome,
                            "verification": verification,
                            "delivery_outcome": (
                                "draft_cleared"
                                if outcome == "returned" and verification == "draft_absent"
                                else "unknown"
                            ),
                            **last_attempt,
                            **{
                                f"stage_{stage}_ms": round(seconds * 1000)
                                for stage, seconds in trace.stage_seconds.items()
                            },
                            **trace.tmux_failure,
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


def _content_shape(content: str) -> dict[str, object]:
    """Classify a message's shape (leading character, words, non-ASCII) without its text."""
    stripped = content.strip()
    leading = stripped[:1]
    if not leading:
        leading_class = "none"
    elif not leading.isascii():
        leading_class = "non_ascii"
    elif leading.isalpha():
        leading_class = "letter"
    elif leading.isdigit():
        leading_class = "digit"
    else:
        leading_class = _LEADING_CHAR_CLASSES.get(leading, "other")
    return {
        "content_leading_char": leading_class,
        "content_word_count": len(stripped.split()),
        "content_non_ascii": not content.isascii(),
    }


def _claude_code_version(bridge_dir: Path) -> dict[str, object]:
    """Return the Claude Code version the statusLine last reported, when one is on disk."""
    with contextlib.suppress(OSError, ValueError, AttributeError):
        version = json.loads((bridge_dir / _CONTEXT_FILE).read_text(encoding="utf-8")).get(
            "claude_code_version"
        )
        if isinstance(version, str) and version:
            return {"claude_code_version": version}
    return {}


class DraftObservation(TypedDict):
    capture_empty: bool
    prompt_glyph_visible: bool
    prompt_row_framed: bool
    needle_visible_below_prompt: bool
    queued_placeholder_visible: bool
    pane_rows: int
    pane_max_columns: int


def _draft_observation(pane: str, needle: str, *, prompt_glyph: str) -> DraftObservation:
    """Summarize the last prompt row without retaining terminal or prompt text."""
    lines = pane.splitlines()
    prompt_rows = [index for index, line in enumerate(lines) if prompt_glyph in line]
    last_prompt = prompt_rows[-1] if prompt_rows else None
    rule_above = lines[last_prompt - 1].strip() if last_prompt else ""
    return {
        "capture_empty": not pane.strip(),
        "prompt_glyph_visible": last_prompt is not None,
        # A rule right above the row marks Claude's input box, not a dialog option or echo.
        "prompt_row_framed": bool(rule_above) and set(rule_above) == {"─"},
        "needle_visible_below_prompt": bool(needle)
        and last_prompt is not None
        and needle in "\n".join(lines[last_prompt + 1 :]),
        "queued_placeholder_visible": _QUEUED_PLACEHOLDER in pane,
        "pane_rows": len(lines),
        "pane_max_columns": max((len(line) for line in lines), default=0),
    }


@_best_effort
def start_attempt() -> None:
    trace = _prompt_delivery_trace.get()
    if trace is not None:
        trace.attempts.append({"verification": "not_started", "submit_sent": False, "retries": 0})


@_best_effort
def record_draft(
    *,
    pane: str,
    needle: str,
    prompt_glyph: str,
    draft_seen: bool,
    start: float,
    polls: int,
    empty_captures: int,
) -> None:
    record_details(
        draft_seen=draft_seen,
        needle_available=bool(needle),
        draft_wait_ms=round((time.monotonic() - start) * 1000),
        draft_polls=polls,
        draft_empty_captures=empty_captures,
        **{
            f"draft_{key}": value
            for key, value in _draft_observation(pane, needle, prompt_glyph=prompt_glyph).items()
        },
    )


@_best_effort
def record_verification(
    *,
    draft_still_present: bool,
    pane: str,
    needle: str,
    prompt_glyph: str,
    start: float,
    retries: int,
    polls: int,
) -> None:
    observation = _draft_observation(pane, needle, prompt_glyph=prompt_glyph)
    verification = (
        "draft_still_present"
        if draft_still_present
        else (
            "inconclusive_capture"
            if observation["capture_empty"] or not observation["prompt_glyph_visible"]
            else "draft_absent"
        )
    )
    record_details(
        verification=verification,
        submit_wait_ms=round((time.monotonic() - start) * 1000),
        retries=retries,
        submit_polls=polls,
        **{f"submit_{key}": value for key, value in observation.items()},
    )
