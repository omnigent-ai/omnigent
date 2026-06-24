#!/usr/bin/env python3
"""
Find "hanging" i18n keys: keys defined in ap-web's locale files that no
longer appear to be used anywhere in the source.

Parity (check_parity.py) keeps the *locales* honest against each other. This
script keeps the locales honest against the *code* -- a key that every locale
defines but no component calls is dead weight: it survives renames, bloats the
bundle, and lies about coverage. We scan the reference language's key set and
subtract everything the source appears to reference.

Why this is a heuristic, not an oracle (read before trusting it):
  * Keys are reached dynamically. `t(`permMode_${mode.value}`)` resolves at
    runtime to permMode_plan, permMode_default, ... We capture the literal
    PREFIX before `${` and treat any key starting with it as used. So a whole
    family stays "used" even though no single literal appears.
  * Keys are reached through variables: `t(key!)`, `t(labelKey)`. We CANNOT
    know which key those hit. When such calls exist we say so and soften the
    verdict -- a reported key may simply be reached through a variable.
  * Usage is matched namespace-agnostically (a literal `foo` used anywhere
    counts as using `foo` in every namespace that defines it). This errs
    toward NOT flagging, which is the safe direction: better to miss a dead
    key than to tell someone to delete a live one.

So: treat the output as a triage worklist. For each reported key, grep it
yourself; if it is genuinely orphaned, delete it from EVERY locale. If it is
reached dynamically, leave it (and ideally widen this script's prefix list).

Exit code is always 0 -- this is an advisory sweep, never a CI gate, because
the variable-key blind spot makes false positives unavoidable.

Usage:
    python find_unused_keys.py [SRC_DIR] [--locales DIR] [--ref en]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Translator aliases. The default name is `t`, but components rename it
# (`const { t: tc } = useTranslation("common")`) or bind it from getFixedT
# (`const t = i18n.getFixedT(...)`). We DISCOVER these names per scan so an
# alias like `tc(...)` counts as usage -- and so unrelated helpers that merely
# start with "t" (e.g. a `tok(...)` tokenizer) are NOT mistaken for one.
ALIAS_RENAME_RE = re.compile(r"""\bt\s*:\s*([A-Za-z_$][\w$]*)\s*\}\s*=\s*useTranslation\b""")
ALIAS_FIXEDT_RE = re.compile(r"""\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:\w+\.)?getFixedT\b""")
DEFAULT_TRANSLATORS = {"t"}

# i18next plural/ordinal suffixes -- a key foo_other is reached via t("foo").
PLURAL_SUFFIX_RE = re.compile(r"_(zero|one|two|few|many|other|\d+)$")


def translator_regexes(names: set[str]) -> tuple[re.Pattern, re.Pattern, re.Pattern]:
    """Build (literal, template-prefix, variable-arg) matchers for these
    translator function names. Captures, respectively: a string-literal key
    (t("k")/'k'/`k`), the literal prefix before the first ${ in a template
    key (t(`permMode_${x}`) -> "permMode_"), and any call whose first arg is a
    bare identifier (an unresolvable variable key)."""
    alt = "|".join(sorted(map(re.escape, names), key=len, reverse=True))
    return (
        re.compile(rf"""\b(?:{alt})\(\s*(['"`])([\w.:-]+)\1"""),
        re.compile(rf"""\b(?:{alt})\(\s*`([\w.:-]*)\$\{{"""),
        re.compile(rf"""\b(?:{alt})\(\s*[A-Za-z_$][\w$]*\s*[!,)]"""),
    )
# i18next plural/ordinal suffixes -- a key foo_other is reached via t("foo").
PLURAL_SUFFIX_RE = re.compile(r"_(zero|one|two|few|many|other|\d+)$")

SOURCE_GLOBS = ("*.ts", "*.tsx")


def find_dir(arg: str | None, *tail: str) -> Path:
    if arg:
        return Path(arg).resolve()
    here = Path(__file__).resolve()
    for parent in [here, *here.parents]:
        candidate = parent.joinpath(*tail)
        if candidate.is_dir():
            return candidate
    return Path(*tail).resolve()


def flatten(obj: object, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def strip_ns(key: str) -> str:
    """Drop a leading `ns:` qualifier, e.g. nav:archived -> archived."""
    return key.split(":", 1)[1] if ":" in key else key


def base_key(key: str) -> str:
    m = PLURAL_SUFFIX_RE.search(key)
    return key[: m.start()] if m else key


def source_files(src_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in SOURCE_GLOBS:
        for path in src_dir.rglob(pattern):
            if any(part in {"node_modules", "dist", "build"} for part in path.parts):
                continue
            files.append(path)
    return files


def scan_source(src_dir: Path) -> tuple[set[str], list[str], bool, set[str]]:
    """Return (literal keys used, template prefixes, any variable-key calls,
    discovered translator names)."""
    texts = {p: p.read_text(encoding="utf-8", errors="ignore") for p in source_files(src_dir)}

    # Pass 1: discover translator names (default `t` + any aliases).
    names = set(DEFAULT_TRANSLATORS)
    for text in texts.values():
        names.update(ALIAS_RENAME_RE.findall(text))
        names.update(ALIAS_FIXEDT_RE.findall(text))
    literal_re, template_re, var_re = translator_regexes(names)

    # Pass 2: collect usages with the alias-aware matchers.
    literals: set[str] = set()
    prefixes: list[str] = []
    has_var_calls = False
    for text in texts.values():
        for _, key in literal_re.findall(text):
            literals.add(strip_ns(key))
        for prefix in template_re.findall(text):
            if prefix:
                prefixes.append(strip_ns(prefix))
        if var_re.search(text):
            has_var_calls = True
    return literals, prefixes, has_var_calls, names


def is_used(key: str, literals: set[str], prefixes: list[str]) -> bool:
    if key in literals or base_key(key) in literals:
        return True
    return any(key.startswith(p) for p in prefixes)


def main() -> int:
    ap = argparse.ArgumentParser(description="Find unused ap-web i18n keys.")
    ap.add_argument("src_dir", nargs="?", default=None)
    ap.add_argument("--locales", default=None)
    ap.add_argument("--ref", default="en", help="Reference language (default: en)")
    args = ap.parse_args()

    src_dir = find_dir(args.src_dir, "ap-web", "src")
    locales_dir = find_dir(args.locales, "ap-web", "src", "i18n", "locales")
    ref_dir = locales_dir / args.ref
    if not src_dir.is_dir():
        print(f"Source dir not found: {src_dir}")
        return 0
    if not ref_dir.is_dir():
        print(f"Reference locale '{args.ref}' not found under {locales_dir}")
        return 0

    literals, prefixes, has_var_calls, names = scan_source(src_dir)
    print(f"Source: {src_dir}")
    print(f"Locales: {locales_dir} (reference: {args.ref})")
    print(f"Translator names: {', '.join(sorted(names))}")
    print(
        f"Found {len(literals)} literal key(s), {len(set(prefixes))} dynamic "
        f"prefix(es){', variable-key calls present' if has_var_calls else ''}.\n"
    )

    total_unused = 0
    for ns_file in sorted(ref_dir.glob("*.json")):
        ns = ns_file.stem
        keys = flatten(json.loads(ns_file.read_text(encoding="utf-8")))
        unused = sorted(k for k in keys if not is_used(k, literals, prefixes))
        if unused:
            total_unused += len(unused)
            print(f"[{ns}] {len(unused)} key(s) with no detected usage:")
            for k in unused:
                print(f"    - {k}")

    print()
    if total_unused == 0:
        print("OK -- every reference key appears to be used.")
        return 0
    print(f"{total_unused} key(s) look unused -- TRIAGE, not a verdict.")
    if has_var_calls:
        print(
            "Note: variable-key t(...) calls exist, so some of these may be "
            "reached dynamically. grep each before deleting; remove confirmed "
            "dead keys from EVERY locale."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
