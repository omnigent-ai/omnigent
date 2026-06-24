---
name: ap-web-i18n
description: >-
  Find and fix untranslated UI strings in the ap-web frontend and keep its
  i18next locale files in lockstep. Use this whenever the user wants to
  translate, internationalize, or "i18n" any part of ap-web; add or audit
  English/French (en/fr) translations; check that locale JSON files
  (common.json, nav.json under src/i18n/locales) have matching keys; wire a
  component to useTranslation/t(); or make tests assert translation keys
  instead of hardcoded English so no UI string escapes translation. Trigger
  it even when the user just says "this page still has English hardcoded",
  "the French is missing some strings", or "make sure nothing leaks past
  the translations" — anything about ap-web translation coverage or locale
  key parity belongs here.
---

# ap-web i18n coverage

Keep the `ap-web` frontend fully translated and its locale files honest.
The job has four parts that go together: **find** hardcoded UI strings,
**translate** them into English + French, **enforce** that every locale has
the exact same keys, and **harden tests** to assert via keys so future
English can't sneak in untranslated.

## Why this needs care (the trap)

ap-web's i18next is configured with a fallback chain: a missing key falls
back to the `en` resource, then to the raw key string. That's good for
incremental migration but it means **nothing fails loudly**. A French user
silently reads English; a typo'd key renders as `someKey.title` and no test
notices. So "looks fine in the browser" is not evidence of coverage. The
parity script and key-based tests exist to replace that false comfort with
a real signal. Lean on them.

Before doing anything, skim `src/i18n/index.ts` to confirm the current
languages, namespaces, and storage key — don't trust this doc over the
code if they disagree.

## Workflow

Work in `ap-web/`. The two bundled scripts auto-locate the repo paths, so
you can run them from anywhere.

### 1. Find candidates

```bash
python .claude/skills/ap-web-i18n/scripts/scan_hardcoded.py
```

This greps `.tsx` files for JSX text, user-facing attributes
(`placeholder`, `title`, `aria-label`, `alt`, `label`), and toast copy that
isn't already inside `t(...)`. It's a **heuristic worklist, not an oracle**
— it will flag some non-copy (an `aria-label="polite"`, a route string) and
miss some copy. Triage each hit: is this text a human reads? If the user
pointed at a specific file or page, scope to that; otherwise scan broadly
and work file by file.

### 2. Translate and wire each real hit

For every genuine UI string, do all three of these together so the repo is
never left half-migrated:

1. **Add the English key** to `src/i18n/locales/en/<ns>.json`. Reuse an
   existing key if one already says the same thing (grep first — generic
   words like Cancel/Save/Close/Retry almost certainly exist).
2. **Add the French translation** to `src/i18n/locales/fr/<ns>.json` under
   the identical key. Write a real, idiomatic French translation (the user
   chose auto-translate). Keep `{{placeholders}}` byte-identical to English.
3. **Wire the component**: `const { t } = useTranslation("<ns>")` and
   replace the literal with `t("key")` (or `t("key", { count })`). The
   English literal must be **gone** from the JSX afterward.

Namespace choice, key-naming style, French register, plurals,
interpolation, and the rich-text (prefix/suffix) pattern are all spelled
out in `references/conventions.md` — read it before writing keys so you
match the house style instead of inventing a parallel one.

### 3. Harden the tests

Any test that asserts a literal English string which is now translated must
switch to resolving that string through the app's own i18n instance:

```tsx
import i18n from "@/i18n";
const t = i18n.getFixedT(null, "nav");
expect(screen.getByRole("button", { name: t("archived") })).toBeInTheDocument();
```

Under jsdom the language resolves to `en`, so the asserted text is
unchanged — but it now comes *from the key*, so it tracks renames, proves
the string is translated, and turns a missing French key into a parity
failure instead of a green test riding the English fallback. Leave
genuinely non-translatable literals (test IDs, fixture data like
`"conv_active"`, routes) as-is. Full rationale and pattern in
`references/conventions.md`.

### 4. Verify — this is the gate, not an afterthought

```bash
python .claude/skills/ap-web-i18n/scripts/check_parity.py      # must exit 0
python .claude/skills/ap-web-i18n/scripts/find_unused_keys.py  # advisory sweep
cd ap-web && npm run type-check && npm test                    # must pass
```

`check_parity.py` enforces, per namespace: every locale has the same key
set as `en`, shared keys use the same `{{placeholders}}`, plural sets are
complete (`_one` + `_other`), and no value is empty. A non-zero exit is a
real defect — fix it, don't explain it away. Report the before/after parity
state and what you translated.

It also prints **duplicate-value warnings**: two different keys in the same
namespace whose `en` value is identical (e.g. `removeFileNamed` and
`removeFile` both `"Remove {{name}}"`). That's a missed reuse — the same
string should live under one key. These are warnings, not gate failures,
because some collisions are legitimately context-distinct (enum-ish keys
like `permMode_plan` vs `plan` happen to share English but mean different
things). For each warning, **triage**: if the two keys are genuinely the
same UI string, pick one (prefer the more generic / already-most-used key),
delete the other from **every** locale, and repoint its call sites with the
chosen key — then re-run the gate. If they're context-distinct, leave them.

`find_unused_keys.py` catches the opposite drift: **hanging keys** — a key
every locale defines but no source file uses (dead weight that survives
renames and lies about coverage). It discovers translator aliases
(`const { t: tc } = useTranslation(...)`, `getFixedT` binds), follows dynamic
prefixes (`` t(`permMode_${x}`) `` keeps the whole `permMode_*` family alive),
and strips plural suffixes. It is **advisory only — always exit 0**, never a
gate, because keys reached through a variable (`t(labelKey)`, where `labelKey`
comes from a `labelKey:`/`value:` map) can't be resolved and would otherwise
look dead. So treat its output as a worklist: **grep each reported key
yourself** before acting; if it's genuinely orphaned, delete it from **every**
locale; if it's reached dynamically, leave it. Don't blanket-delete the list.

## Scope discipline

- Don't introduce a new namespace, a new language, or `<Trans>` unless the
  user asks — those change `src/i18n/index.ts` and widen the parity surface.
- Don't add `t("key", "English default")` fallbacks — a baked-in default
  hides a missing key from the parity check, which is the whole point of
  the check.
- If the scan surfaces far more than the user asked to tackle, translate
  what they asked for, then report the remaining count rather than silently
  doing everything or silently stopping.
