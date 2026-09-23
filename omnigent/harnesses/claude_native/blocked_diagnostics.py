"""Bounded observations of native blockers, independent of pane control."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from omnigent.debug_logging import debug_event
from omnigent.process_logging import redact_log_text

_logger = logging.getLogger(__name__)
_PERSISTENT_AFTER_S = 60.0
_MAX_DIALOG_EXCERPT = 600
_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_CREDENTIAL_SURFACE = re.compile(
    r"\b(?:password|passwd|secret|credential|authorization|bearer|api[ _-]?key|[\w_-]*token)\b",
    re.IGNORECASE,
)
_BLOCKED_REASONS = {"permission prompt", "input needed", "dialog open"}
_CLAUDE_BANNER_VERSION = re.compile(r"Claude Code v(\d+\.\d+\.\d+)\b")


def terminal_locator_id(socket_path: str, target: str) -> str:
    """Join runner and harness observations without logging filesystem paths."""
    return hashlib.sha256(f"{socket_path}\0{target}".encode()).hexdigest()[:24]


@dataclass(frozen=True)
class DialogObservation:
    kind: str
    excerpt: str | None = None


def describe_dialog(pane: str) -> DialogObservation | None:
    """Classify only the current input surface; never excerpt transcript text."""
    from omnigent.harnesses.claude_native import bridge

    if bridge.auto_mode_billing_notice_visible(pane):
        return DialogObservation("auto_mode_classifier_billing_notice")
    if bridge._user_prompt_visible(pane):
        # A permission/question can contain the full command or user prompt.
        return DialogObservation("user_prompt")
    if bridge._terminal_dialog_headline(pane) is None:
        return None
    lines = pane.splitlines()
    rules = [index for index, line in enumerate(lines) if bridge._is_box_rule(line)]
    if not rules:
        return DialogObservation("unknown")
    region = lines[rules[-1] + 1 :]
    footers = [index for index, line in enumerate(region) if bridge._DIALOG_FOOTER_RE.search(line)]
    if not footers or any(line.strip() for line in region[footers[-1] + 1 :]):
        return DialogObservation("unknown")
    # Credential-entry surfaces can wrap secrets across lines; retain no excerpt.
    text = " ".join(line.strip().strip("│").strip() for line in region[: footers[-1] + 1])
    text = _URL.sub("[URL]", text)
    if _CREDENTIAL_SURFACE.search(text):
        return DialogObservation("unknown", "[credential dialog omitted]")
    # Redact before truncation, including URL query credentials.
    text = redact_log_text(text, include_whitespace_credentials=True)
    return DialogObservation("unknown", text[:_MAX_DIALOG_EXCERPT])


class NativeBlockedDiagnostics:
    """Record one blocker episode and one persistence snapshot, never every tick."""

    def __init__(
        self,
        *,
        session_id: str | None,
        socket_path: str,
        tmux_target: str,
        observation_source: str,
        terminal_instance_id: str | None = None,
        context: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self._session_id = session_id
        self._identity: dict[str, object] = {
            "harness": "claude-native",
            "terminal_locator_id": terminal_locator_id(socket_path, tmux_target),
            "terminal_instance_id": terminal_instance_id or "unknown",
            "observation_source": observation_source,
        }
        self._context = context
        self._episode_id: str | None = None
        self._started = 0.0
        self._persistent = False
        self._kind: str | None = None
        self._cli_version: str | None = None
        self._permission_mode: str | None = None
        self._mode_observed_at: float | None = None

    def observe(
        self,
        pane: str | None,
        *,
        raw_status: str | None = None,
        status_updated_at: int | None = None,
        status_file_state: str = "unavailable",
        blocked_on: str | None = None,
        capture_age_ms: int | None = 0,
    ) -> None:
        """Best-effort observation; failure cannot alter native session behavior."""
        with contextlib.suppress(Exception):
            self._observe(
                pane,
                raw_status=raw_status,
                status_updated_at=status_updated_at,
                status_file_state=status_file_state,
                blocked_reason=(blocked_on if blocked_on in _BLOCKED_REASONS else "unknown"),
                capture_age_ms=capture_age_ms,
            )

    def _observe(self, pane: str | None, **observation: object) -> None:
        from omnigent.harnesses.claude_native.bridge import (
            _composer_row,
            _permission_mode_from_pane,
            claude_pane_text_ready,
        )

        age = observation["capture_age_ms"]
        capture_state = "missing" if not pane else "ok"
        if pane and (age is None or (isinstance(age, int) and age > 2000)):
            capture_state = "stale"
        dialog = describe_dialog(pane) if pane and capture_state == "ok" else None
        version = _CLAUDE_BANNER_VERSION.search(pane) if pane and capture_state == "ok" else None
        if version is not None:
            self._cli_version = version.group(1)
        raw_status = observation["raw_status"]
        file_readable = observation["status_file_state"] == "readable"
        blocked = dialog is not None or (file_readable and raw_status == "waiting")
        cleared = not blocked and (
            (file_readable and raw_status is not None)
            or (bool(pane) and capture_state == "ok" and claude_pane_text_ready(pane or ""))
        )
        now = time.monotonic()
        mode = (
            _permission_mode_from_pane(pane)
            if pane and capture_state == "ok" and _composer_row(pane) is not None
            else None
        )
        if mode is not None:
            self._permission_mode = mode
            self._mode_observed_at = now
        attrs = {
            **observation,
            "capture_status": capture_state,
            "dialog_excerpt": dialog.excerpt if dialog is not None else None,
            "observed_dialog_kind": dialog.kind if dialog is not None else "unknown",
            "native_cli_version": self._cli_version or "unknown",
            "native_cli_version_source": "pane_banner" if self._cli_version else "unknown",
            "native_permission_mode": self._permission_mode or "unknown",
            "permission_mode_source": "pane_footer" if self._permission_mode else "unknown",
            "permission_mode_observation_age_ms": (
                round((now - self._mode_observed_at) * 1000)
                if self._mode_observed_at is not None
                else None
            ),
        }
        if self._episode_id is None:
            if not blocked:
                return
            self._episode_id = uuid.uuid4().hex
            self._started = now
            self._kind = dialog.kind if dialog is not None else "unknown"
            self._persistent = False
            self._emit("entered", now, attrs)
        elif cleared:
            self._emit("cleared", now, attrs)
            self._episode_id = None
            self._kind = None
        elif dialog is not None and dialog.kind != "unknown" and dialog.kind != self._kind:
            previous_kind = self._kind
            self._kind = dialog.kind
            self._emit(
                "identified" if previous_kind == "unknown" else "changed",
                now,
                {**attrs, "previous_dialog_kind": previous_kind},
            )
        elif not self._persistent and now - self._started >= _PERSISTENT_AFTER_S:
            self._persistent = True
            self._emit("persistent", now, attrs)

    def _emit(self, phase: str, now: float, observation: dict[str, object]) -> None:
        context: dict[str, object] = {"context_status": "unavailable"}
        if self._context is not None:
            with contextlib.suppress(Exception):
                context = self._context()
        extra = debug_event("native_blocked_state", session_id=self._session_id)
        extra["attributes"] = {
            **self._identity,
            **context,
            **observation,
            "phase": phase,
            "block_episode_id": self._episode_id,
            "dialog_kind": self._kind,
            "blocked_elapsed_ms": round((now - self._started) * 1000),
        }
        _logger.info("Claude native blocker %s", phase, extra=extra)
