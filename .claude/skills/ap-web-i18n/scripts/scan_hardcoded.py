#!/usr/bin/env python3
"""
Surface CANDIDATE user-facing strings in ap-web .tsx files that aren't
going through i18n yet.

This is a heuristic finder, not an oracle. Detecting "is this string shown
to a human" perfectly would need a real TS/JSX parser plus type info; a
regex pass instead casts a wide net and accepts some false positives. Treat
the output as a worklist to triage, not a list of guaranteed bugs -- the
model reading this must still judge each hit (e.g. an aria-label of "polite"
or a `to="/login"` route are not translatable copy).

What it flags:
  * JSX text nodes:           <button>Save changes</button>
  * User-facing attributes:   placeholder/title/aria-label/alt/label=
  * Common toast/error calls: toast.error("Something broke")

What it deliberately skips to keep the noise down:
  * strings already inside t(...) / <Trans>
  * files under test (*.test.tsx) and the i18n setup itself
  * className / data-* / key / id / href / to / role / type / name=
  * strings with no letters, single words that are obvious enums
    (UPPER_CASE, kebab tokens, camelCaseIdentifiers with no space)

Usage:
    python scan_hardcoded.py [SRC_DIR]

SRC_DIR defaults to ap-web/src (auto-located relative to the repo).
Output is grouped by file with line numbers, so each hit is click-to-open.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Attributes whose string value is shown to (or read to) a human.
UI_ATTRS = ("placeholder", "title", "aria-label", "alt", "label", "aria-description")
ATTR_RE = re.compile(
    r'\b(' + "|".join(re.escape(a) for a in UI_ATTRS) + r')\s*=\s*"([^"]+)"'
)
# JSX text node: ">  Some words here  <" with no embedded tag/brace.
JSX_TEXT_RE = re.compile(r">([^<>{}]*[A-Za-z]{2,}[^<>{}]*)<")
# Toast / notification copy.
TOAST_RE = re.compile(r'\b(?:toast|notify|message)\.\w+\(\s*"([^"]+)"')

# A literal that is almost certainly an identifier/enum/token, not prose.
IDENT_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
# Fragments that betray a JSX expression caught by the text regex, not copy:
# `{i > 0 && x}`, `a === b`, `() => foo`. Real UI strings don't contain these.
CODE_TOKENS = ("&&", "||", "=>", "===", "!==", "==", ";", "{", "}")


def looks_like_copy(s: str) -> bool:
    s = s.strip()
    if len(s) < 2:
        return False
    if not any(c.isalpha() for c in s):
        return False
    # Pure identifier / route / token with no whitespace -> not prose.
    if " " not in s and IDENT_RE.match(s):
        return False
    # Leaked JSX expression fragment rather than human-readable copy.
    if any(tok in s for tok in CODE_TOKENS):
        return False
    return True


def find_src_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent / "ap-web" / "src"
        if candidate.is_dir():
            return candidate
    return Path("ap-web/src").resolve()


def line_has_t_call(line: str) -> bool:
    # Cheap guard: if the line already calls t(...) or uses <Trans, assume the
    # author is mid-translation and skip its literals to avoid double-flagging.
    return bool(re.search(r"\bt\(", line)) or "<Trans" in line


def scan_file(path: Path) -> list[tuple[int, str, str]]:
    hits: list[tuple[int, str, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith(("import ", "//", "*", "/*")):
            continue
        if line_has_t_call(line):
            continue

        for m in ATTR_RE.finditer(line):
            val = m.group(2)
            if looks_like_copy(val):
                hits.append((i, f"@{m.group(1)}", val))

        for m in TOAST_RE.finditer(line):
            val = m.group(1)
            if looks_like_copy(val):
                hits.append((i, "toast", val))

        for m in JSX_TEXT_RE.finditer(line):
            val = m.group(1).strip()
            if looks_like_copy(val):
                hits.append((i, "jsx-text", val))
    return hits


def main() -> int:
    ap = argparse.ArgumentParser(description="Find candidate untranslated strings in ap-web.")
    ap.add_argument("src_dir", nargs="?", default=None)
    args = ap.parse_args()

    src = find_src_dir(args.src_dir)
    if not src.is_dir():
        print(f"Source dir not found: {src}")
        return 2

    total = 0
    files = 0
    for path in sorted(src.rglob("*.tsx")):
        if path.name.endswith(".test.tsx"):
            continue
        if "i18n" in path.parts:
            continue
        hits = scan_file(path)
        if not hits:
            continue
        files += 1
        rel = path.relative_to(src.parent)
        print(f"\n{rel}")
        for line_no, kind, val in hits:
            total += 1
            shown = val if len(val) <= 80 else val[:77] + "..."
            print(f"  {line_no:>4}  [{kind}]  {shown}")

    print(f"\n{total} candidate string(s) across {files} file(s).")
    print("Heuristic output -- triage each hit before translating.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
