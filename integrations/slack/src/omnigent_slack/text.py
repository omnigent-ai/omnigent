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


def truncate_for_slack(text: str, limit: int = 39000) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix
