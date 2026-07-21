from __future__ import annotations

import re

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
WHITESPACE_RE = re.compile(r"\s+")

# Generic user-facing failure. Raw error detail (exception strings, in-band
# ``response.error`` / ``turn.failed`` messages, server bodies) can carry stack
# traces or internal paths, and a Slack thread is visible to the whole channel —
# so the detail is logged server-side and only this generic line is shown (the
# "server error bodies are never echoed" rule in DESIGN.md). Lives here (a
# dependency-free leaf) so the streaming, notification, and service layers share
# one wording.
GENERIC_FAILURE_TEXT = (
    ":warning: Something went wrong on the Omnigent server. Please try again; if it "
    "keeps happening, contact your Omnigent operator."
)


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", " ", text)
    else:
        text = MENTION_RE.sub(" ", text, count=1)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


# Default cap for one-shot messages (session titles, short guidance replies).
# Streamed answers are not subject to this — Slack owns chunking for streams.
SLACK_MESSAGE_CHAR_LIMIT = 4000


def truncate_for_slack(text: str, limit: int = SLACK_MESSAGE_CHAR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


# Block Kit caps a static_select option's text and value at 75 chars; some
# packed values (e.g. an elicitation question key) use a larger app-defined cap.
SLACK_OPTION_CHAR_LIMIT = 75


def truncate_option(text: str, limit: int = SLACK_OPTION_CHAR_LIMIT) -> str:
    """Fit ``text`` within a Block Kit option's char cap, eliding with ``…``."""
    return text if len(text) <= limit else text[: limit - 1] + "…"
