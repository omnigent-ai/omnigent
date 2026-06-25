"""Tests for ``scripts/gen_cursor_models.py`` base-id derivation.

These pin the parsing rules that turn ``cursor-agent models`` compound ids into
the canonical base-id catalog: effort-suffix stripping, the irregular-claude
override map, the prefix denylist, display-name cleaning, and ``(default)`` vs
``(current)`` handling. They feed a fixed fixture through the script's stdin, so
they run in CI without the ``cursor-agent`` binary.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_cursor_models.py"

# Representative slice of ``cursor-agent models`` output exercising every rule:
# bare ids, the (default)/(current) tags, simple + multi-token + two-ordering
# effort suffixes, the claude 4.6 reversal override, and three denylist reasons.
_FIXTURE = """Available models

auto - Auto
composer-2.5 - Composer 2.5 (default)
gpt-5.2 - GPT-5.2 (current)
gpt-5.2-high - GPT-5.2 High
gpt-5.5-extra-high - GPT-5.5 1M Extra High
claude-opus-4-8-thinking-high - Opus 4.8 1M Thinking
claude-opus-4-8-max - Opus 4.8 1M Max
claude-4.6-opus-high - Opus 4.6 1M
claude-4.6-opus-high-thinking - Opus 4.6 1M Thinking
claude-4.6-sonnet-medium - Sonnet 4.6 1M
gpt-5.1 - GPT-5.1
gpt-5.1-codex-max-low - Codex 5.1 Max Low
gpt-5.4-mini-none - GPT-5.4 Mini None
gemini-3-flash - Gemini 3 Flash
claude-4-sonnet - Sonnet 4
"""


def _run(fixture: str) -> tuple[dict[str, dict], str]:
    """Run the generator on *fixture* via stdin; return {id: row} and stderr."""
    proc = subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=fixture,
        capture_output=True,
        text=True,
        check=True,
    )
    rows: dict[str, dict] = {}
    for m in re.finditer(
        r'\{"id": "([^"]+)", "displayName": "([^"]+)"(, "isDefault": True)?\}', proc.stdout
    ):
        rows[m.group(1)] = {"displayName": m.group(2), "isDefault": bool(m.group(3))}
    return rows, proc.stderr


def test_derives_canonical_base_ids() -> None:
    """Effort suffixes strip; the claude 4.6 reversal maps to the canonical id."""
    rows, _ = _run(_FIXTURE)
    # Bare ids kept; compound ids collapse to their base.
    assert "gpt-5.2" in rows
    assert "gpt-5.5" in rows  # from gpt-5.5-extra-high (multi-token effort)
    assert "claude-opus-4-8" in rows  # both -thinking-high and -max collapse here
    # Irregular claude 4.6 spelling -> canonical injectable base id.
    assert "claude-opus-4-6" in rows
    assert "claude-sonnet-4-6" in rows


def test_never_emits_compound_or_reordered_ids() -> None:
    """The flattened/effort and reordered-claude spellings never leak through."""
    rows, _ = _run(_FIXTURE)
    for bad in ("gpt-5.2-high", "gpt-5.5-extra-high", "claude-4.6-opus", "claude-4.6-opus-high"):
        assert bad not in rows


def test_denylisted_families_are_dropped() -> None:
    """Prefix-collision, low-value tiers, and unverified spellings are excluded."""
    rows, stderr = _run(_FIXTURE)
    assert "gpt-5.1" not in rows  # prefix-collision
    assert "gpt-5.1-codex" not in rows  # from gpt-5.1-codex-max-low, dropped by gpt-5.1 prefix
    assert "gpt-5.4-mini" not in rows
    assert "gemini-3-flash" not in rows
    assert "claude-4-sonnet" not in rows
    # Sonnet 4.6 must NOT be collateral damage of a too-greedy sonnet prefix.
    assert "claude-sonnet-4-6" in rows
    assert "skip" in stderr  # the denylist reasons are logged for review


def test_default_tag_carried_current_tag_ignored() -> None:
    """``(default)`` -> isDefault; ``(current)`` (per-session) is ignored."""
    rows, _ = _run(_FIXTURE)
    assert rows["composer-2.5"]["isDefault"] is True
    assert rows["gpt-5.2"]["isDefault"] is False  # tagged (current), not (default)
    assert [mid for mid, r in rows.items() if r["isDefault"]] == ["composer-2.5"]


def test_display_names_strip_effort_and_context_words() -> None:
    """Trailing effort/context words are stripped to a clean label."""
    rows, _ = _run(_FIXTURE)
    assert rows["gpt-5.5"]["displayName"] == "GPT-5.5"  # "GPT-5.5 1M Extra High" cleaned
    assert rows["claude-opus-4-8"]["displayName"] == "Opus 4.8"  # "Opus 4.8 1M Thinking" cleaned
    assert rows["claude-sonnet-4-6"]["displayName"] == "Sonnet 4.6"  # "Sonnet 4.6 1M" cleaned
    assert rows["auto"]["displayName"] == "Auto"


def test_unmapped_irregular_claude_spelling_warns() -> None:
    """A new reversed-claude id with no override is flagged, not silently mangled."""
    _, stderr = _run("Available models\n\nclaude-4.9-opus-high - Opus 4.9 1M\n")
    assert "unmapped irregular claude id" in stderr
