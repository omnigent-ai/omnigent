"""Text helpers and the Discord size limits everything else budgets against.

Discord's caps are hard API errors, not soft truncations, so every outbound
string passes through one of the helpers here rather than being trimmed ad hoc
at each call site.
"""

from __future__ import annotations

import re

# ``<@123>``, the legacy nickname form ``<@!123>``, and the role form
# ``<@&123>`` — Discord's autocomplete offers a bot's managed role alongside
# the bot itself, so both reach us as an address to the bot.
MENTION_RE = re.compile(r"<@[!&]?(\d+)>")
WHITESPACE_RE = re.compile(r"\s+")

# Generic user-facing failure. Raw error detail (exception strings, in-band
# ``response.error`` / ``turn.failed`` messages, server bodies) can carry stack
# traces or internal paths, and a Discord channel is visible to everyone in it —
# so the detail is logged server-side and only this generic line is shown (the
# "server error bodies are never echoed" rule in DESIGN.md). Lives here (a
# dependency-free leaf) so the streaming, notification, and service layers share
# one wording.
GENERIC_FAILURE_TEXT = (
    "⚠️ Something went wrong on the Omnigent server. Please try again; if it "
    "keeps happening, contact your Omnigent operator."
)

# Hard caps from the Discord API. Exceeding one is a 400, so these are budgets,
# not style guidance.
MESSAGE_CHAR_LIMIT = 2000
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_VALUE_LIMIT = 1024
# Select-menu option label/value and button label caps.
OPTION_LABEL_LIMIT = 100
OPTION_VALUE_LIMIT = 100
# A select menu holds at most 25 options and a view at most 5 action rows.
MAX_SELECT_OPTIONS = 25
MAX_ACTION_ROWS = 5


def strip_bot_mention(text: str, bot_user_id: str | None) -> str:
    """Remove the bot's own @-mention from a message and normalize whitespace.

    Discord delivers a mention as ``<@id>`` (or ``<@!id>`` from older clients),
    and a mention of the bot's managed role as ``<@&id>`` — both of which count
    as addressing the bot. Without a known bot id, the first mention of any kind
    is stripped, matching how the message reached us in the first place.
    """
    if bot_user_id:
        text = re.sub(rf"<@!?{re.escape(bot_user_id)}>", " ", text)
        # The bot's managed role carries its own id, not the bot's, so strip a
        # leading role mention too — otherwise the raw ``<@&id>`` rides along
        # into the prompt.
        text = re.sub(r"^\s*<@&\d+>", " ", text)
    else:
        text = MENTION_RE.sub(" ", text, count=1)
    return normalize_whitespace(text)


def normalize_whitespace(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def truncate_for_message(text: str, limit: int = MESSAGE_CHAR_LIMIT) -> str:
    """Fit ``text`` inside one Discord message, marking that it was cut.

    Used for one-shot messages (notices, session titles, error replies).
    Streamed answers are NOT truncated — they roll into a continuation message
    instead (see :class:`omnigent_discord.streaming._LiveReply`).
    """
    if len(text) <= limit:
        return text
    suffix = "\n\n[truncated]"
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def truncate_option(text: str, limit: int = OPTION_LABEL_LIMIT) -> str:
    """Fit ``text`` within a component's char cap, eliding with ``…``."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def split_for_messages(text: str, limit: int = MESSAGE_CHAR_LIMIT) -> list[str]:
    """Split ``text`` into chunks that each fit one Discord message.

    Prefers a paragraph break, then a line break, then a space, and only cuts
    mid-token when a single run of text exceeds the limit — so a long answer
    reads as continuous prose across messages rather than being sliced through
    words. An empty input yields an empty list (nothing to send).
    """
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
        # No break in the whole window: a single unbroken run (a URL, a base64
        # blob) — cut at the limit rather than looping forever.
        if cut <= 0:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks
