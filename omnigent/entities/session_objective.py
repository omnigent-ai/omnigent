"""Objectives an agent records for its session, and the outcome of each.

An agent records what it sets out to do (an objective) and later how that went
(an outcome: whether it reached the objective, an emoji and a few words). A
session holds any number of objectives in order. The latest one is the
session's current objective, and an outcome recorded without naming an
objective resolves it. Objectives recorded while an automation run is being
served carry that run's ID, so the run's history shows how it went.
"""

from __future__ import annotations

from dataclasses import dataclass

# Bounds on what an agent records. An emoji can span several code points (skin
# tones, ZWJ sequences, flags), so its bound is in code points.
OBJECTIVE_TEXT_MAX_CHARS = 200
OUTCOME_EMOJI_MAX_CHARS = 16
OUTCOME_TEXT_MAX_CHARS = 80
# A session's objectives are listed a page at a time; this caps one page.
OBJECTIVES_PAGE_MAX = 100

# Unicode's Extended_Pictographic ranges (emoji-data.txt): the code points that
# can start an emoji. General symbol categories also hold math, box-drawing and
# currency signs, which are not emoji.
_EXTENDED_PICTOGRAPHIC: tuple[tuple[int, int], ...] = (
    (0x00A9, 0x00A9), (0x00AE, 0x00AE), (0x203C, 0x203C), (0x2049, 0x2049),
    (0x2122, 0x2122), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x2388, 0x2388), (0x23CF, 0x23CF),
    (0x23E9, 0x23F3), (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB),
    (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE), (0x2600, 0x2605),
    (0x2607, 0x2612), (0x2614, 0x2685), (0x2690, 0x2705), (0x2708, 0x2712),
    (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721),
    (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747),
    (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2767), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0),
    (0x27BF, 0x27BF), (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C),
    (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030), (0x303D, 0x303D),
    (0x3297, 0x3297), (0x3299, 0x3299), (0x1F000, 0x1F0FF), (0x1F10D, 0x1F10F),
    (0x1F12F, 0x1F12F), (0x1F16C, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1AD, 0x1F1E5), (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F), (0x1F249, 0x1F3FA),
    (0x1F400, 0x1F53D), (0x1F546, 0x1F64F), (0x1F680, 0x1F6FF), (0x1F774, 0x1F77F),
    (0x1F7D5, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F), (0x1F85A, 0x1F85F),
    (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1FAFF), (0x1FC00, 0x1FFFD),
)  # fmt: skip
_EMOJI_ZWJ = "\u200d"
_EMOJI_KEYCAP = "\u20e3"
_EMOJI_VARIATION_SELECTORS = frozenset("\ufe0e\ufe0f")
_EMOJI_KEYCAP_BASES = frozenset("0123456789#*")


def _is_extended_pictographic(cp: int) -> bool:
    return any(start <= cp <= end for start, end in _EXTENDED_PICTOGRAPHIC)


_EMOJI_SKIN_TONES = range(0x1F3FB, 0x1F400)
# Tag characters spell a subdivision flag after the black flag, e.g. Scotland.
_EMOJI_TAGS = range(0xE0020, 0xE0080)
_EMOJI_BLACK_FLAG = 0x1F3F4


def is_single_emoji(value: str) -> bool:
    """Whether *value* is one emoji, counting a ZWJ or keycap sequence or a flag as one.

    Approximates one emoji grapheme without a Unicode segmentation dependency.
    Each part of the sequence is an Extended_Pictographic character, a keycap
    base followed by its keycap mark, or a regional-indicator pair, optionally
    followed by one variation selector and one skin tone; tag characters may
    follow only the black flag. Parts are joined by single ZWJs, but a flag or
    a keycap is never part of a joined sequence.
    Extended_Pictographic also contains a few symbols that rarely render as
    emoji (for example ``☉`` and ``🂠``); they are accepted, since the value is
    only shown, never interpreted.
    """
    if not value or len(value) > OUTCOME_EMOJI_MAX_CHARS:
        return False
    parts = value.split(_EMOJI_ZWJ)
    if len(parts) > 1 and any(not part for part in parts):
        return False
    if not all(_is_emoji_part(part) for part in parts):
        return False
    # A ZWJ does not join a flag or a keycap to another emoji: that is two emoji.
    return len(parts) == 1 or not any(_is_flag(part) or _is_keycap(part) for part in parts)


def _is_flag(part: str) -> bool:
    return len(part) == 2 and all(0x1F1E6 <= ord(ch) <= 0x1F1FF for ch in part)


def _is_keycap(part: str) -> bool:
    return part[:1] in _EMOJI_KEYCAP_BASES


def _is_emoji_part(part: str) -> bool:
    """One emoji without ZWJs: a base, then at most one selector, one tone, or tags."""
    if _is_flag(part):
        return True
    base, rest = part[0], part[1:]
    if base in _EMOJI_KEYCAP_BASES:
        return rest in (_EMOJI_KEYCAP, "\ufe0f" + _EMOJI_KEYCAP)
    if not _is_extended_pictographic(ord(base)):
        return False
    if rest[:1] in _EMOJI_VARIATION_SELECTORS:
        rest = rest[1:]
    if rest[:1] and ord(rest[0]) in _EMOJI_SKIN_TONES:
        rest = rest[1:]
    if rest and ord(base) == _EMOJI_BLACK_FLAG:
        return all(ord(ch) in _EMOJI_TAGS for ch in rest)
    return not rest


def _collapsed_text(name: str, value: str, max_chars: int) -> str:
    """Check *value* is valid Unicode, collapse its whitespace and bound its length."""
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:  # e.g. an unpaired surrogate from raw JSON
        raise ValueError(f"{name} must be valid Unicode text") from exc
    value = " ".join(value.split())
    if not value:
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_chars:
        raise ValueError(f"{name} must be at most {max_chars} characters")
    return value


def normalize_objective(objective: str) -> str:
    """Collapse an objective's whitespace and check it, returning the value to store.

    :param objective: One line on what the agent sets out to do, e.g.
        ``"Get the nightly build green"``.
    :returns: The objective with runs of whitespace collapsed to single spaces.
    :raises ValueError: If it is not valid Unicode text, is empty, or is longer
        than :data:`OBJECTIVE_TEXT_MAX_CHARS`.
    """
    return _collapsed_text("objective", objective, OBJECTIVE_TEXT_MAX_CHARS)


def normalize_outcome(emoji: str, text: str) -> tuple[str, str]:
    """Trim and check an outcome's emoji and text, returning the values to store.

    :param emoji: Exactly one emoji, e.g. ``"🚀"``; surrounding spaces are dropped.
    :param text: A few words; runs of whitespace collapse to single spaces.
    :returns: The trimmed emoji and the collapsed text.
    :raises ValueError: If either value is not valid Unicode text, the emoji is
        not exactly one emoji, or the text is empty or longer than
        :data:`OUTCOME_TEXT_MAX_CHARS`.
    """
    try:
        emoji.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("emoji must be valid Unicode text") from exc
    emoji = emoji.strip()
    if not is_single_emoji(emoji):
        raise ValueError("emoji must be exactly one emoji, e.g. '🚀'")
    return emoji, _collapsed_text("text", text, OUTCOME_TEXT_MAX_CHARS)


@dataclass(frozen=True)
class SessionObjectiveOutcome:
    """How an agent says an attempt at an objective went.

    :param success: Whether it reached the objective. Independent of the
        session's or run's status, which only say whether turns finished.
    :param emoji: One emoji for the outcome, e.g. ``"🚀"``.
    :param text: A few words, e.g. ``"Opened 2 PRs"``.
    :param recorded_at: Unix epoch microseconds of the latest recording.
    """

    success: bool
    emoji: str
    text: str
    recorded_at: int


@dataclass(frozen=True)
class SessionObjective:
    """One objective an agent recorded for its session.

    :param id: Objective ID, a bare 32-character hex UUID.
    :param conversation_id: The session it belongs to.
    :param created_at: Unix epoch microseconds when it was recorded.
    :param objective: What the agent set out to do; ``None`` when the agent
        recorded an outcome without stating an objective first.
    :param scheduled_task_run_id: The automation run the session was serving
        when the objective was recorded, or ``None``.
    :param outcome: The latest outcome recorded for it, or ``None``.
    """

    id: str
    conversation_id: str
    created_at: int
    objective: str | None = None
    scheduled_task_run_id: str | None = None
    outcome: SessionObjectiveOutcome | None = None


@dataclass(frozen=True)
class SessionObjectivePage:
    """One page of a session's objectives, oldest first.

    :param data: The objectives on this page.
    :param next_cursor: Opaque cursor for the next page, or ``None`` at the end.
    """

    data: list[SessionObjective]
    next_cursor: str | None
