"""Built-in context-management policies.

Helps agents keep their working context lean.  The guiding principle:
the goal is not fewer tokens *used*, but fewer tokens *wasted* —
sprawling context filled with stale tool results from a prior task
degrades quality without adding value.

The recommended response to a denial is to start a fresh session for
the new task rather than compacting or summarising in place.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from omnigent.policies.schema import PolicyCallable, PolicyEvent, PolicyResponse

_log = logging.getLogger(__name__)

# ── detect_task_switch ────────────────────────────────────────────────────────

_TASK_SWITCH_HISTORY_KEY = "_task_switch_history"

_DEFAULT_TASK_SWITCH_PROMPT = """\
You are a conversation-continuity classifier for a coding assistant.

You are given the user's recent messages (the "prior context") and their
latest message. Decide whether the latest message is a continuation of the
same task or the start of a clearly different, unrelated task.

Guidelines:
- CONTINUATION: the latest message follows naturally from prior work — a
  follow-up question, a refinement, a related sub-task, or asking about
  something mentioned earlier.
- TASK_SWITCH: the latest message starts a completely different topic or
  codebase concern with no meaningful connection to what came before.
- When in doubt, prefer CONTINUATION — false positives (blocking a legitimate
  continuation) are more harmful than false negatives.

Return strict JSON only:
{"verdict": "CONTINUATION" | "TASK_SWITCH"}
"""

_TASK_SWITCH_SCHEMA: dict[str, Any] = {
    "format": {
        "type": "json_schema",
        "name": "task_switch_verdict",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": ["CONTINUATION", "TASK_SWITCH"],
                },
            },
            "required": ["verdict"],
            "additionalProperties": False,
        },
    },
}


def _strip_code_fences(text: str) -> str:
    """Strip markdown code fences from LLM output.

    Even with structured output, some providers wrap JSON in
    triple-backtick fences. This strips the outermost fence
    so ``json.loads`` succeeds.

    :param text: Raw LLM response text.
    :returns: Text with code fences removed.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3].rstrip()
    return stripped


def _extract_text(response: Any) -> str:
    """Pull plain text out of a PolicyLLMClient response."""
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()
    output = getattr(response, "output", None)
    if not isinstance(output, list) or not output:
        return ""
    content = getattr(output[0], "content", None)
    if not isinstance(content, list) or not content:
        return ""
    return getattr(content[0], "text", "") or ""


def detect_task_switch(
    *,
    min_turns: int = 1,
    history_window: int = 10,
    action: str = "ASK",
    classification_prompt: str = _DEFAULT_TASK_SWITCH_PROMPT,
) -> PolicyCallable:
    """Factory: detect when the user switches to an unrelated task.

    Fires on ``request`` events.  Maintains a rolling window of recent
    user messages in ``session_state`` and, once ``min_turns`` prior
    messages have accumulated, asks the server-level LLM to classify the
    latest message as ``CONTINUATION`` or ``TASK_SWITCH``.

    On ``TASK_SWITCH`` the policy returns *action* with a message
    recommending a fresh session — not compaction or summarisation.  The
    window is reset to contain only the switching message so the new
    task can accumulate its own history from a clean state (``DENY``
    path only — the ``ASK`` path cannot write state on decline, so the
    window advances only once the user approves and the next request
    arrives).
    On ``CONTINUATION`` the policy records the new message into state
    and abstains, letting the request through.

    Requires the server to have an ``llm:`` config block; abstains
    (fail-open) when no LLM client is available.

    .. note::
        ``action="DENY"`` is not a security control — user messages are
        interpolated into the classifier prompt and a determined user can
        craft a message that forces a ``CONTINUATION`` verdict (prompt
        injection).  Use this policy for context-hygiene guidance, not
        for access control.

    :param min_turns: Number of prior messages to accumulate before the
        classifier starts firing.  With the default of ``1`` the
        classifier fires on the **second** user message (one prior
        message is enough context to detect a switch).  Set to ``0`` to
        classify from the very first message.
    :param history_window: Maximum number of recent user messages kept
        in state as prior context for the classifier.  Defaults to
        ``10``.  Older messages are dropped as the window slides.
    :param action: Response when a task switch is detected.
        ``"ASK"`` (default) escalates to the user; ``"DENY"`` blocks
        the request outright.  Defaults to ``"ASK"`` because
        false-positive task-switch classifications are more harmful than
        false negatives.
    :param classification_prompt: System prompt for the classifier LLM
        call.  Must instruct the model to return
        ``{"verdict": "CONTINUATION"|"TASK_SWITCH"}``; the schema is
        enforced via structured output regardless.
    :returns: An async policy callable that fires on ``request`` events.
    """
    normalised_action = action.upper() if isinstance(action, str) else "ASK"
    if normalised_action not in {"DENY", "ASK"}:
        _log.warning(
            "detect_task_switch: unknown action %r — defaulting to ASK",
            action,
        )
        normalised_action = "ASK"

    async def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """Classify the new user message and flag task switches.

        Reads ``session_state[_TASK_SWITCH_HISTORY_KEY]`` for prior
        context and writes the updated window back via
        ``state_updates``.

        :param event: Policy event dict.
        :returns: *action* when a task switch is detected; ``None``
            (abstain) otherwise.
        """
        if event.get("type") != "request":
            return None

        new_message = event.get("data", "")
        if not isinstance(new_message, str) or not new_message.strip():
            return None

        state = event.get("session_state") or {}
        history: list[str] = state.get(_TASK_SWITCH_HISTORY_KEY) or []

        # Slide the window: append new message, keep last history_window entries.
        updated_history = [*history, new_message[:500]][-history_window:]

        # Not enough prior turns yet — record and pass through.
        if len(history) < min_turns:
            _log.debug(
                "detect_task_switch: history_len=%d < min_turns=%d — accumulating",
                len(history),
                min_turns,
            )
            return {
                "result": "ALLOW",
                "state_updates": [
                    {
                        "key": _TASK_SWITCH_HISTORY_KEY,
                        "action": "set",
                        "value": updated_history,
                    }
                ],
            }

        # ── Classify ────────────────────────────────────────────────────
        llm_client = event.get("llm_client")
        if llm_client is None:
            _log.warning(
                "detect_task_switch: no llm_client — server has no llm: config. Abstaining."
            )
            return None

        prior_context = "\n".join(f"- {msg}" for msg in history[-history_window:])
        user_prompt = f"Prior messages:\n{prior_context}\n\nLatest message:\n{new_message[:500]}"

        try:
            response = await llm_client.create(
                instructions=classification_prompt,
                input=[
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_prompt}],
                    }
                ],
                text=_TASK_SWITCH_SCHEMA,
            )
            raw = _extract_text(response)
            if not raw:
                return None
            raw = _strip_code_fences(raw)
            verdict_obj = json.loads(raw)
        except Exception:  # noqa: BLE001 — fail-open
            _log.exception("detect_task_switch: classifier call failed")
            return None

        verdict = verdict_obj.get("verdict", "") if isinstance(verdict_obj, dict) else ""

        if verdict == "TASK_SWITCH":
            _log.info("detect_task_switch: TASK_SWITCH detected — action=%s", normalised_action)
            # Reset the window to the switching message alone so the new
            # task accumulates fresh history from here.  state_updates on a
            # DENY are applied immediately; state_updates on an ASK are only
            # applied if the user approves — on decline the window stays
            # pinned, so the next message will be re-classified against the
            # pre-switch context (which is the safe / over-prompting direction).
            return {
                "result": normalised_action,
                "reason": (
                    "This message looks like the start of a new, unrelated task. "
                    "The current session carries context from prior work that will "
                    "waste capacity without helping here. "
                    "Start a fresh session for this task to keep context lean."
                ),
                "state_updates": [
                    {
                        "key": _TASK_SWITCH_HISTORY_KEY,
                        "action": "set",
                        "value": [new_message[:500]],
                    }
                ],
            }

        if verdict == "CONTINUATION":
            # Update history and let the request through.
            return {
                "result": "ALLOW",
                "state_updates": [
                    {
                        "key": _TASK_SWITCH_HISTORY_KEY,
                        "action": "set",
                        "value": updated_history,
                    }
                ],
            }

        # Unrecognised verdict — fail open.
        return None

    return evaluate  # type: ignore[return-value]


# ── Registry ──────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.context.detect_task_switch",
        "kind": "factory",
        "name": "Detect Task Switch",
        "description": (
            "Uses the server-level LLM to classify each user message as a "
            "continuation of the current task or the start of a new, unrelated "
            "one. On a detected task switch, asks (or denies) with a recommendation "
            "to start a fresh session. Implements the 'Keep Context Lean' strategy: "
            "start fresh sessions when switching tasks rather than accumulating "
            "stale context. Requires an llm: config block on the server; "
            "abstains (fail-open) when no LLM client is available."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "min_turns": {
                    "type": "integer",
                    "default": 2,
                    "description": (
                        "Number of prior user messages to accumulate before "
                        "the classifier starts firing. Defaults to 2."
                    ),
                },
                "history_window": {
                    "type": "integer",
                    "default": 4,
                    "description": (
                        "Maximum number of recent user messages kept as prior "
                        "context for the classifier. Older messages are dropped "
                        "as the window slides. Defaults to 10."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["ASK", "DENY"],
                    "default": "ASK",
                    "description": (
                        "Response when a task switch is detected. "
                        "ASK escalates to the user (default); "
                        "DENY blocks the request outright."
                    ),
                },
                "classification_prompt": {
                    "type": "string",
                    "description": (
                        "System prompt for the classifier. Must instruct the "
                        'model to return {"verdict": "CONTINUATION"|"TASK_SWITCH"}; '
                        "the output schema is enforced via structured output."
                    ),
                },
            },
            "required": [],
        },
    },
]
