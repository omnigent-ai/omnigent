"""E2E: prose dollar signs render literally instead of turning into math.

Regression guard for the "vertical letter-by-letter math soup" bug. The
renderer used to accept a single ``$`` as a LaTeX delimiter, so an assistant
message that mentioned money, rates, or shell variables had any two of its
dollar signs paired up and everything between them handed to KaTeX — a
sentence like "about $5/PR versus $2/session" rendered its middle as a stack
of italic math variables. ``web/src/components/ai-elements/
streamdown-security.ts`` now requires ``$$`` to open math.

Two deterministic assistant messages (seeded via ``external_assistant_message``
— no LLM run) cover both halves of the contract:

  - The **prose** paragraph keeps every dollar sign as text and renders **no**
    KaTeX at all, with the sentence intact.
  - The **math** message still renders KaTeX, so turning single-dollar math off
    did not cost us real formulas — one inline span from the ``\\(...\\)`` form
    that agents actually emit, plus one ``$$``-fenced display block.

Asserting on the absence of ``.katex`` (rather than on the text alone) is what
ties this to the bug: KaTeX rewrites the characters into positioned spans, so a
message that "contains the right text" can still be unreadable soup. The prose
assertion is scoped to its own paragraph because the transcript groups adjacent
assistant messages into a single bubble.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

_AGENT_NAME = "hello_world"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# The reported sentence shape. `$/PR` and `$/session` are the load-bearing
# tokens: a `$` followed by a slash is neither currency-with-a-digit nor a
# SCREAMING_CASE variable, so the escaping heuristics this fix replaced never
# caught them and they paired up into one math span.
_PROSE_TEXT = (
    "Cost check: $/PR is down but $/session is up, a 60% saving overall, "
    "and it still reads $LLM_API_KEY from the environment."
)

# Real math must survive: an explicit inline TeX span (what agents emit for
# inline math) and a `$$`-fenced display block.
_MATH_TEXT = "\n".join(
    [
        r"The quadratic roots are \(x = 1\), from:",
        "",
        r"$$",
        r"x = \frac{-b \pm \sqrt{b^2 - 4ac}}{2a}",
        r"$$",
    ]
)

_PROSE_MARKER = "Cost check:"
_MATH_MARKER = "The quadratic roots are"


def _seed_message(base_url: str, session_id: str, text: str) -> None:
    """Append a committed assistant message to the session transcript.

    :param base_url: The live server's base URL.
    :param session_id: The session to append to.
    :param text: The assistant message body.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": _AGENT_NAME, "text": text},
        },
        timeout=10.0,
    )
    resp.raise_for_status()


@pytest.fixture
def dollar_session(seeded_session: tuple[str, str]) -> Iterator[tuple[str, str]]:
    """Seed a session with one prose message and one real-math message.

    :param seeded_session: ``(base_url, session_id)`` for a runner-bound session.
    :returns: the same ``(base_url, session_id)`` after both replies are seeded.
    """
    base_url, session_id = seeded_session
    _seed_message(base_url, session_id, _PROSE_TEXT)
    _seed_message(base_url, session_id, _MATH_TEXT)
    yield (base_url, session_id)


def test_prose_dollar_signs_are_not_rendered_as_math(
    page: Page,
    dollar_session: tuple[str, str],
) -> None:
    """Dollars in prose stay literal text; genuine math still renders."""
    base_url, session_id = dollar_session
    page.goto(f"{base_url}/c/{session_id}")

    bubble = page.locator(_ASSISTANT, has_text=_MATH_MARKER).first
    expect(bubble).to_be_visible(timeout=30_000)

    prose = page.locator(f"{_ASSISTANT} p", has_text=_PROSE_MARKER).first
    expect(prose).to_be_visible(timeout=30_000)

    # The bug: the span between two dollar signs became KaTeX math.
    assert prose.locator(".katex").count() == 0, "prose dollar signs were rendered as math"

    # And the sentence survives intact, dollar signs and all.
    expect(prose).to_have_text(_PROSE_TEXT)

    # The counterpart: real math must still render, or the fix went too far.
    # Exactly one display block, and the inline \(...\) span stays inline
    # rather than being promoted to a second centered block.
    expect(bubble.locator(".katex").first).to_be_visible(timeout=30_000)
    assert bubble.locator(".katex").count() == 2, (
        "expected one inline TeX span and one display block to render"
    )
    assert bubble.locator(".katex-display").count() == 1, (
        "expected the $$-fenced block to be the only display-mode formula"
    )
