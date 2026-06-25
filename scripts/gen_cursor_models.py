#!/usr/bin/env python3
"""Generate ``_CURSOR_BASE_MODELS`` for ``omnigent/cursor_native.py``.

The cursor-native web picker needs *base* model ids — the namespace that (a)
launches via ``cursor-agent --model <id>``, (b) injects via ``/model <id>`` in
the TUI, and (c) is what ``meta.lastUsedModel`` reports back to the web mirror.
``cursor-agent models`` (== ``--list-models``) instead prints ~80 *compound*
ids that flatten ``model x effort`` (``gpt-5.2-high``) and spell the claude
4.5/4.6 family with reversed word order + a dotted version
(``claude-4.6-opus-high`` vs the canonical ``claude-opus-4-6``).

This script derives the base-id catalog from that output: it strips the trailing
effort/thinking group to recover the base id, applies ``_BASE_ID_OVERRIDES`` for
the irregular claude spellings, drops ``_DENYLIST_PREFIXES`` (prefix-collision /
unoffered tiers), derives a clean display name, and carries the ``(default)``
tag. ``(current)`` is ignored — it is per-session state, not a catalog property.

Usage::

    cursor-agent models | python scripts/gen_cursor_models.py
    python scripts/gen_cursor_models.py        # shells out to ``cursor-agent models``

Re-run when cursor ships new models and paste the printed literal into
``omnigent/cursor_native.py`` (between the ``# >>> generated`` markers). Review
the diff: a brand-new model with a *new* irregular spelling needs a one-line
``_BASE_ID_OVERRIDES`` entry, and the script warns when it sees an unmapped
claude reordering so the gap never lands silently.
"""

from __future__ import annotations

import re
import subprocess
import sys

# Trailing effort / thinking tokens cursor appends to a base id. Order matters:
# multi-word ``extra-high`` must be tried before ``high``. The group repeats so
# ``claude-opus-4-8-thinking-high`` and ``claude-4.6-opus-high-thinking`` (the
# two orderings cursor uses) both strip fully.
_EFFORT_TOKENS = ("extra-high", "thinking", "xhigh", "medium", "high", "low", "none", "max")
_EFFORT_SUFFIX_RE = re.compile(r"(?:-(?:" + "|".join(_EFFORT_TOKENS) + r"))+$")

# Irregular cursor spellings -> canonical base id (verified to inject via
# ``/model`` and to match what ``meta.lastUsedModel`` reports). Only the claude
# 4.5/4.6 family reverses order + dots the version; 4.7/4.8 already use the
# canonical ``claude-opus-4-N`` form and need no override.
_BASE_ID_OVERRIDES: dict[str, str] = {
    "claude-4.6-opus": "claude-opus-4-6",
    "claude-4.6-sonnet": "claude-sonnet-4-6",
    "claude-4.5-opus": "claude-opus-4-5",
    "claude-4.5-sonnet": "claude-sonnet-4-5",
}

# Base-id prefixes to exclude from the picker, each with its reason. Matched
# against the *derived* base id, so a prefix kills a whole family.
_DENYLIST_PREFIXES: dict[str, str] = {
    "gpt-5.1": "prefix-collision: /model gpt-5.1 mis-ranks to 'Codex 5.1 Max'",
    "gpt-5-mini": "low-value tier, not offered in the picker",
    "gpt-5.4-mini": "low-value tier, not offered in the picker",
    "gpt-5.4-nano": "low-value tier, not offered in the picker",
    "gemini-3-flash": "flash tier not offered in the picker",
    "gemini-3.5-flash": "flash tier not offered in the picker",
    "claude-4-sonnet": "Sonnet 4: base-id injection not yet verified",
}

# Trailing display-name words that describe effort/context rather than the
# model, stripped to recover a clean label ("Opus 4.8 1M Extra High" -> "Opus 4.8").
_DISPLAY_STRIP_WORDS = {"1m", "none", "low", "medium", "high", "max", "thinking", "extra"}

# Family display order (claude opus, claude sonnet, gpt, codex, gemini), with
# ``auto`` and the account default pinned to the top.
_FAMILY_RANK = {
    "auto": 0,
    "composer": 1,
    "opus": 2,
    "sonnet": 3,
    "gpt": 4,
    "codex": 5,
    "gemini": 6,
}

_LINE_RE = re.compile(r"^(?P<id>\S+)\s+-\s+(?P<name>.+?)(?:\s+\((?P<tags>[^)]*)\))?$")


def _base_id(compound_id: str) -> str:
    """Recover the canonical base id from a compound ``--list-models`` id."""
    stripped = _EFFORT_SUFFIX_RE.sub("", compound_id)
    return _BASE_ID_OVERRIDES.get(stripped, stripped)


def _clean_display(name: str) -> str:
    """Strip trailing effort/context words from a model's display name."""
    words = name.split()
    while words and words[-1].lower() in _DISPLAY_STRIP_WORDS:
        words.pop()
    return " ".join(words)


def _family(base_id: str) -> str:
    if base_id == "auto":
        return "auto"
    if base_id.startswith("composer"):
        return "composer"
    if "opus" in base_id:
        return "opus"
    if "sonnet" in base_id:
        return "sonnet"
    if "codex" in base_id:
        return "codex"
    if base_id.startswith("gpt"):
        return "gpt"
    if base_id.startswith("gemini"):
        return "gemini"
    return "other"


def _version(base_id: str) -> tuple[float, ...]:
    """Numeric version for descending sort within a family (4.8 before 4.7)."""
    nums = [int(n) for n in re.findall(r"\d+", base_id)]
    return tuple(nums) if nums else (0,)


def _denied(base_id: str) -> str | None:
    for prefix, reason in _DENYLIST_PREFIXES.items():
        if base_id == prefix or base_id.startswith(prefix + "-"):
            return reason
    return None


def main() -> int:
    if sys.stdin.isatty():
        raw = subprocess.run(
            ["cursor-agent", "models"], capture_output=True, text=True, check=True
        ).stdout
    else:
        raw = sys.stdin.read()

    # Derive base id -> (display, is_default), first compound variant wins for
    # the display name; is_default is OR-ed across the family's variants.
    derived: dict[str, dict[str, object]] = {}
    for line in raw.splitlines():
        m = _LINE_RE.match(line.strip())
        if not m or " " in m.group("id"):
            continue
        compound = m.group("id")
        tags = {t.strip() for t in (m.group("tags") or "").split(",") if t.strip()}
        base = _base_id(compound)
        # Warn on an unmapped claude reordering so a new one never lands silently.
        if base.startswith("claude-4."):
            print(
                f"WARNING: unmapped irregular claude id {compound!r} -> {base!r}; "
                f"add a _BASE_ID_OVERRIDES entry",
                file=sys.stderr,
            )
        entry = derived.setdefault(
            base, {"display": _clean_display(m.group("name")), "default": False}
        )
        if "default" in tags:
            entry["default"] = True

    rows = []
    for base, entry in derived.items():
        reason = _denied(base)
        if reason:
            print(f"  skip {base:<22} ({reason})", file=sys.stderr)
            continue
        rows.append((base, str(entry["display"]), bool(entry["default"])))

    rows.sort(
        key=lambda r: (_FAMILY_RANK.get(_family(r[0]), 99), tuple(-n for n in _version(r[0])))
    )

    print("_CURSOR_BASE_MODELS: list[dict[str, Any]] = [")
    for base, display, is_default in rows:
        suffix = ', "isDefault": True' if is_default else ""
        print(f'    {{"id": "{base}", "displayName": "{display}"{suffix}}},')
    print("]")
    print(f"\n# {len(rows)} models", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
