"""Shared Layer 2 summarization helpers.

These are self-contained utilities with no AP-server or runtime
dependencies. Both ``omnigent.runtime.compaction`` (AP server)
and ``omnigent.runner.app`` (runner process) import from here so
the summarization prompt and response-parsing logic stay in one place.
"""

from __future__ import annotations

from typing import Any

from omnigent.inner.native_attachments import expand_framework_notices

_SUMMARIZATION_BASE_PROMPT = (
    "Summarize the conversation above so that a future assistant can continue\n"
    "the work without access to the original messages.\n\n"
    "Include: the user's goals, key decisions and why they were made, tool\n"
    "results that matter going forward (paths, values, errors), and any\n"
    "outstanding commitments or next steps.\n\n"
    "Exclude: verbose tool output, redundant exchanges, and intermediate\n"
    "reasoning that led to a final decision — keep the decision, not the path.\n\n"
    "Do not incorporate knowledge from outside this conversation. Do not\n"
    "invent facts. Write in plain text with no markup."
)

# Trailing user turn appended to the conversation so providers that
# reject conversations ending in an assistant message (e.g. Databricks
# Claude, other no-prefill models) accept the summarization request.
_SUMMARIZATION_TRIGGER_MESSAGE = (
    "Produce the summary now, following the instructions in the system message."
)


def _extract_first_text(messages: list[dict[str, Any]]) -> str:
    """
    Return the text of the first content block in *messages*.

    Used to detect progressive summarization: if the conversation
    already starts with a prior summary, the prompt instructs the
    model to incorporate it rather than discard it.

    :param messages: Messages list to inspect.
    :returns: Text of the first content block, or ``""`` if absent.
    """
    if not messages:
        return ""
    content = messages[0].get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
                text = block.get("text", "")
                return text if isinstance(text, str) else ""
    return content if isinstance(content, str) else ""


def build_summarization_prompt(messages: list[dict[str, Any]]) -> str:
    """
    Build the Layer 2 summarization system prompt.

    Detects whether *messages* starts with a prior summary block
    (progressive summarization) and prepends a continuation
    instruction when it does.

    :param messages: The messages that will be summarized, in
        Responses API input format.
    :returns: The assembled system prompt string.
    """
    first = _extract_first_text(messages)
    if "[This is an automatically generated summary" in first:
        return (
            "The conversation starts with a summary of earlier context. "
            "Incorporate it into your new summary — do not discard it.\n\n"
        ) + _SUMMARIZATION_BASE_PROMPT
    return _SUMMARIZATION_BASE_PROMPT


def build_summarization_input(
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Build the Responses API ``input`` for the Layer 2 summarization call.

    Appends a trailing user turn that triggers the summary so the
    resulting chat-completions message list ends with ``role: user``,
    which providers like Databricks Claude require (they reject
    assistant-message prefill). Skipped when *messages* already ends
    with a user message, since some providers reject consecutive
    same-role turns and the existing user turn is already a valid
    final position.

    :param messages: The conversation messages to summarize, in
        Responses API input format.
    :returns: A new list with the trigger user message appended,
        or a copy of *messages* unchanged if it already ends with
        a user message.
    """
    normalized = expand_framework_notices(messages)
    if normalized and normalized[-1].get("role") == "user":
        return normalized
    return [*normalized, {"role": "user", "content": _SUMMARIZATION_TRIGGER_MESSAGE}]


def extract_summary_text(resp: Any) -> str:
    """
    Extract plain text from an LLM Responses API response object.

    Iterates over ``resp.output`` items and concatenates all text
    blocks found in their ``content`` attributes.

    :param resp: Response object from ``llm_client.responses.create()``.
    :returns: Concatenated summary text, or ``""`` if no text blocks
        are present.
    """
    text = ""
    for item in resp.output:
        if hasattr(item, "content"):
            for block in item.content:
                if hasattr(block, "text"):
                    text += block.text
    return text
