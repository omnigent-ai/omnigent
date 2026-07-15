from __future__ import annotations

import re

from markdown_to_mrkdwn import SlackMarkdownConverter

MENTION_RE = re.compile(r"<@([A-Z0-9]+)(?:\|[^>]+)?>")
WHITESPACE_RE = re.compile(r"\s+")

# Slack renders its own `mrkdwn` dialect, not standard Markdown (e.g. *bold* is
# single-asterisk, links are <url|text>). Reuse one converter instance — it
# compiles regex patterns on init.
_MRKDWN_CONVERTER = SlackMarkdownConverter()


def to_mrkdwn(text: str) -> str:
    """Convert standard Markdown to Slack's mrkdwn dialect for display."""
    return str(_MRKDWN_CONVERTER.convert(text))


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    if bot_user_id:
        text = re.sub(rf"<@{re.escape(bot_user_id)}(?:\|[^>]+)?>", " ", text)
    else:
        text = MENTION_RE.sub(" ", text, count=1)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


# Slack accepts up to 40,000 characters in a message `text`, but its own
# guidance is to keep messages under 4,000 so they render without a "Show more"
# fold. Stay at that best-practice ceiling and split longer answers across
# replies (see `split_for_slack`).
SLACK_MESSAGE_CHAR_LIMIT = 4000


def truncate_for_slack(text: str, limit: int = SLACK_MESSAGE_CHAR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def split_for_slack(text: str, limit: int = SLACK_MESSAGE_CHAR_LIMIT) -> list[str]:
    """Split ``text`` into chunks no longer than ``limit`` characters.

    Preserves every character so a long assistant answer (code blocks,
    reports) is delivered in full across multiple messages instead of being
    truncated. Prefers to break after a newline, then a space, and only
    hard-cuts a run with no whitespace (e.g. a long URL). A blank string
    yields ``[""]`` so the caller always has a message to post.
    """
    if limit <= 0:
        raise ValueError("limit must be positive")
    if not text:
        return [""]

    chunks: list[str] = []
    start = 0
    length = len(text)
    while start < length:
        end = start + limit
        if end >= length:
            chunks.append(text[start:])
            break
        window = text[start:end]
        boundary = window.rfind("\n")
        if boundary == -1:
            boundary = window.rfind(" ")
        # Include the delimiter in the current chunk; hard-cut if none found.
        cut = end if boundary <= 0 else start + boundary + 1
        chunks.append(text[start:cut])
        start = cut
    return chunks
