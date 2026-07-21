from __future__ import annotations

import re

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
WHITESPACE_RE = re.compile(r"\s+")


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
