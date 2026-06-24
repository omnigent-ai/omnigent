#!/usr/bin/env python3
"""
Verify locale key parity for ap-web's i18n resources.

The app's fallback chain (missing key -> `en` resource -> raw key) hides
missing translations at runtime: a French user silently sees English, or
worse, a bare key. This script makes that drift visible by treating the
set of keys as a contract every locale must satisfy *identically* per
namespace.

Checks, per namespace (common, nav, ...):
  1. Key parity     -- every language has exactly the same key set as the
                       reference language (default: en). Reports keys that
                       are missing from a locale or present only in it.
  2. Interpolation  -- a key shared across locales must use the same
                       {{placeholders}}. A French string that drops
                       {{count}} will throw or render wrong at runtime.
  3. Plurals        -- i18next plural keys (foo_one / foo_other) must come
                       as a complete set in every language, so counts
                       render in all locales.
  4. Empty values   -- flags blank strings, which usually mean a
                       half-finished translation.
  5. Duplicate vals -- (warning only) two different keys in the same
                       namespace whose reference-language value is identical.
                       Usually a missed reuse: collapse to one key and update
                       the call sites. Reported but does NOT fail the gate,
                       since some collisions are legitimately context-distinct
                       (enum-ish keys like permMode_plan vs plan).

Exit code is 0 when every locale is in lockstep, 1 otherwise -- so this
doubles as a CI / pre-commit gate.

Usage:
    python check_parity.py [LOCALES_DIR] [--ref en]

LOCALES_DIR defaults to ap-web/src/i18n/locales, resolved relative to the
repo root regardless of where the script is invoked from.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

PLACEHOLDER_RE = re.compile(r"\{\{\s*([\w.-]+)")
# i18next plural/ordinal suffixes (CLDR categories + the _N exact form).
PLURAL_SUFFIX_RE = re.compile(r"_(zero|one|two|few|many|other|\d+)$")


def find_locales_dir(arg: str | None) -> Path:
    if arg:
        return Path(arg).resolve()
    here = Path(__file__).resolve()
    # Walk upward looking for the known locales path; lets the script run
    # from the skill dir, the repo root, or anywhere in between.
    for parent in [here, *here.parents]:
        candidate = parent / "ap-web" / "src" / "i18n" / "locales"
        if candidate.is_dir():
            return candidate
    # Fall back to CWD-relative so the error message is actionable.
    return Path("ap-web/src/i18n/locales").resolve()


def flatten(obj: object, prefix: str = "") -> dict[str, object]:
    """Flatten nested JSON into dotted keys -> leaf value."""
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}." if prefix == "" else f"{prefix}{k}."))
    else:
        out[prefix.rstrip(".")] = obj
    return out


def placeholders(value: object) -> set[str]:
    if not isinstance(value, str):
        return set()
    return set(PLACEHOLDER_RE.findall(value))


def plural_base(key: str) -> str | None:
    """Return the stem if `key` is a plural variant, else None."""
    m = PLURAL_SUFFIX_RE.search(key)
    return key[: m.start()] if m else None


def load_namespaces(locales_dir: Path) -> dict[str, dict[str, dict[str, object]]]:
    """language -> namespace -> {flat key: value}."""
    data: dict[str, dict[str, dict[str, object]]] = {}
    for lang_dir in sorted(p for p in locales_dir.iterdir() if p.is_dir()):
        ns_map: dict[str, dict[str, object]] = {}
        for ns_file in sorted(lang_dir.glob("*.json")):
            try:
                raw = json.loads(ns_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"  [!] {ns_file}: invalid JSON -- {e}")
                raise SystemExit(2)
            ns_map[ns_file.stem] = flatten(raw)
        data[lang_dir.name] = ns_map
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="Check i18n locale key parity for ap-web.")
    ap.add_argument("locales_dir", nargs="?", default=None)
    ap.add_argument("--ref", default="en", help="Reference language (default: en)")
    args = ap.parse_args()

    locales_dir = find_locales_dir(args.locales_dir)
    if not locales_dir.is_dir():
        print(f"Locales dir not found: {locales_dir}")
        return 2

    data = load_namespaces(locales_dir)
    if args.ref not in data:
        print(f"Reference language '{args.ref}' not found under {locales_dir}")
        return 2

    languages = list(data.keys())
    ref = args.ref
    print(f"Locales: {locales_dir}")
    print(f"Languages: {', '.join(languages)} (reference: {ref})\n")

    problems = 0
    warnings = 0
    # Union of namespaces across all languages -- a namespace present in one
    # locale but absent in another is itself a parity failure.
    all_ns = sorted({ns for langs in data.values() for ns in langs})

    for ns in all_ns:
        ref_keys = set(data[ref].get(ns, {}))
        for lang in languages:
            lang_keys = set(data[lang].get(ns, {}))

            if ns not in data[lang]:
                print(f"[{ns}] {lang}: namespace file is MISSING entirely")
                problems += 1
                continue

            if lang == ref:
                # Still worth flagging empty values + incomplete plurals in ref.
                pass

            missing = ref_keys - lang_keys
            extra = lang_keys - ref_keys
            if missing:
                problems += 1
                print(f"[{ns}] {lang}: missing {len(missing)} key(s) present in {ref}:")
                for k in sorted(missing):
                    print(f"    - {k}")
            if extra:
                problems += 1
                print(f"[{ns}] {lang}: has {len(extra)} key(s) not in {ref}:")
                for k in sorted(extra):
                    print(f"    + {k}")

            # Interpolation consistency on shared keys.
            for k in sorted(ref_keys & lang_keys):
                ref_ph = placeholders(data[ref][ns][k])
                lang_ph = placeholders(data[lang][ns][k])
                if ref_ph != lang_ph:
                    problems += 1
                    print(
                        f"[{ns}] {lang}: key '{k}' placeholders {sorted(lang_ph)} "
                        f"!= {ref} {sorted(ref_ph)}"
                    )

            # Empty values.
            for k, v in data[lang].get(ns, {}).items():
                if isinstance(v, str) and v.strip() == "":
                    problems += 1
                    print(f"[{ns}] {lang}: key '{k}' is empty")

        # Plural-set completeness, per language, within this namespace.
        for lang in languages:
            keys = set(data[lang].get(ns, {}))
            stems: dict[str, set[str]] = {}
            for k in keys:
                base = plural_base(k)
                if base is not None:
                    stems.setdefault(base, set()).add(k)
            for base, variants in stems.items():
                # i18next requires at least _one and _other for plural lookup.
                has_other = any(v.endswith("_other") for v in variants)
                if not has_other:
                    problems += 1
                    print(
                        f"[{ns}] {lang}: plural '{base}' is missing the _other "
                        f"variant (have {sorted(variants)})"
                    )

        # Duplicate values in the reference language: two distinct keys with
        # the same (trimmed) string. A missed reuse opportunity -- pick one
        # key and update its call sites. Warning only, never fails the gate.
        by_value: dict[str, list[str]] = {}
        for k, v in data[ref].get(ns, {}).items():
            if isinstance(v, str) and v.strip():
                by_value.setdefault(v.strip(), []).append(k)
        for value, keys in by_value.items():
            if len(keys) > 1:
                warnings += 1
                print(
                    f"[{ns}] {ref}: WARNING duplicate value across keys "
                    f"{sorted(keys)} -> {value!r}; collapse to one key and "
                    f"update its usages"
                )

    print()
    if warnings:
        print(f"({warnings} duplicate-value warning(s) -- triage, not a gate failure.)")
    if problems == 0:
        print("OK -- all locales are in lockstep.")
        return 0
    print(f"FAIL -- {problems} parity problem(s) found.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
