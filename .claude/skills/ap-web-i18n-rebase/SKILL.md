---
name: ap-web-i18n-rebase
description: >-
  Rebase the ap-web translation branch onto main and resolve the resulting
  conflicts the i18n-aware way — take main's incoming code as the source of
  truth, re-apply only the t() translation wiring on top, and prune locale
  keys for any UI string main deleted so no hanging keys are left. Use this
  whenever the i18n / multilanguage branch (e.g. feat/add-multilanguage-support)
  has fallen behind main and needs catching up; whenever a rebase or merge of
  that branch is throwing conflicts across many .tsx/.test.tsx files; whenever
  someone says "the translation branch is behind", "fix the i18n rebase
  conflicts", "update the i18n branch from main", "rebase the multilanguage
  work", or "there are conflicts everywhere in the translation PR". Trigger it
  even if the user only says "rebase this branch and keep the translations" —
  any rebase/merge-conflict resolution on the ap-web i18n branch belongs here.
---

# ap-web i18n rebase

Catch the `ap-web` translation branch up to `main` without losing the
translation work and without leaving the locale files lying. This is a
**conflict-resolution strategy**, not a new translation pass — when you need
to actually translate fresh strings, that is the sibling **`ap-web-i18n`**
skill, and this skill reuses its scripts (`scan_hardcoded.py`,
`find_unused_keys.py`, `check_parity.py`) rather than reinventing them.

## Why the naive rebase hurts (and the shape that makes it easy)

The translation branch is typically **one big commit** that touches dozens of
`.tsx` files — every component got `useTranslation()` + its English literals
swapped for `t("key")`. Meanwhile `main` keeps moving and edits many of those
same files. A line-by-line "merge both sides" on every hunk is the misery the
user is trying to escape.

The escape hatch comes from a fact worth confirming first: **`main` has no
i18n system of its own** — no `src/i18n/locales`, no `useTranslation`. The
entire locale layer (`locales/en/*.json`, `locales/fr/*.json`, `i18n/index.ts`)
is a *pure addition* on the branch. That means:

- The **locale JSON files never conflict** — they ride along untouched. The
  English keys and their French translations are safe.
- A translated file only conflicts when `main` *also* edited it. Files `main`
  left alone merge cleanly and keep their `t()` wiring for free.
- So the real work is a **small set of overlapping files** — usually a dozen
  or two, not the whole branch.

Confirm this shape before trusting the plan (the next section). If `main` *has*
grown its own i18n in the meantime, stop and tell the user — the locale files
would then conflict and the strategy below needs adjusting.

## The strategy in one line

For each conflicting source file: **take main's side of the conflicting hunks
only, then re-apply just the `t()` wiring on those hunks.** The keys already
live in the (clean) locale JSON, so re-wiring is mostly re-introducing
`t("existingKey")` calls — not retranslating. Then **delete keys for any string
main removed.**

The hunk-level focus is the performance lever. Git already auto-merged every
non-conflicting region of the file and **kept the branch's `t()` wiring there
for free** — only the conflict hunks lost it. So never replace the whole file
with main's version (that throws away all that surviving wiring and forces a
full-file re-translation); resolve hunk-by-hunk and re-wire only the hunks. On
a file where main touched two functions out of thirty, that is the difference
between re-reading two functions and re-reading thirty.

## Workflow

Work from the repo root. The scripts auto-locate paths.

### 0. Preflight — know the terrain, make a safety net

```bash
git status                                   # tree must be clean; stash if not
BR=$(git rev-parse --abbrev-ref HEAD)        # the i18n branch
git fetch                                    # get the freshest main
git branch backup/$BR-prerebase              # cheap, lets you bail with --abort or reset
```

Then measure the actual conflict set and confirm the "main has no i18n" shape:

```bash
python .claude/skills/ap-web-i18n-rebase/scripts/preflight.py
```

It prints: the i18n commit(s), whether `main` carries any i18n (it should not),
the count of files the branch translated, and the **overlap set** — the files
`main` also changed, i.e. the ones that will actually conflict. Read that list;
it is your Phase-2 worklist. If `main` already has i18n, the script warns —
stop and consult the user.

### 1. Start the rebase

```bash
git rebase main          # or: git rebase origin/main
```

It stops at the translation commit with the overlapping files in conflict.
`git status` shows them as `UU`. Remember the rebase direction: while a rebase
is paused, **`--ours` is `main`** (the branch you are landing on) and
**`--theirs` is the translation commit** being replayed. This is the opposite
of a merge, and getting it backwards silently throws away main's work — say it
out loud before you run a checkout.

### 2. Resolve each conflict: take main's hunks, then re-wire just those

Resolve mechanically first, then re-translate. The helper classifies the
currently-conflicted paths and resolves the unambiguous ones:

```bash
python .claude/skills/ap-web-i18n-rebase/scripts/resolve_conflicts.py
```

For conflicting `.ts/.tsx` source and test files it walks the conflict markers
and keeps **main's side of each hunk** (`<<<<<<<`/`--ours`), leaving every
already-merged region — and the branch's surviving `t()` wiring there —
untouched. For any `i18n/locales/**` file it keeps **the branch's version**
(`--theirs`; defensive — those rarely conflict). It `git add`s each and prints
what it did, including the **exact line ranges** of every hunk it took from
main. It does **not** run `git rebase --continue` — you re-apply the
translations first.

(handles both 2-way and diff3 conflict markers. A file too tangled for
hunk-by-hunk resolution can be forced to whole-file with
`--whole-file path/to/File.tsx`; a file with no markers falls back to
whole-file automatically. Use `--dry-run` to preview the hunk map first.)

Now re-apply the translation wiring, but **only inside the reported line ranges**
— the rest of each file already kept its `t()` calls from the clean merge, so
don't re-read or re-touch it. The exact literal→key mapping the branch used is
recoverable — don't guess it:

```bash
git show $I18N_SHA -- path/to/File.tsx   # see how the branch wired this file
```

Re-apply that wiring onto main's side of each resolved hunk:

1. Add `const { t } = useTranslation("<ns>")` (or the alias the file uses) if
   main's side of a hunk dropped it — though if a non-conflicting region still
   declares it, you won't need to.
2. Replace each English literal main reintroduced **in the resolved hunks** with
   the **same `t("key")`** the branch used. The key almost always still exists
   in `locales/en` — grep to confirm before inventing a new one.
3. If `main` added a **new** user-facing string the branch never saw, treat it
   as fresh translation work: follow the **`ap-web-i18n`** skill — add the `en`
   key, the `fr` translation, and wire it. Do not leave it hardcoded; that is
   exactly the leak this branch exists to prevent.
4. For **test files**, re-apply the branch's switch from literal assertions to
   key-based ones (`i18n.getFixedT(null, "<ns>")`), again per the `ap-web-i18n`
   skill, on top of whatever main changed in the conflicting hunk.

Stage each file as you finish it (`git add path`). When every conflicted file
is wired and staged:

```bash
git rebase --continue
```

If there is more than one translation commit, the rebase may pause again —
repeat Phase 2 for each pause.

### 3. Catch strings main added since the branch was cut

Files that merged cleanly were *not* in the conflict set, so any **new**
hardcoded string main introduced in them slipped through untranslated. Sweep
for them:

```bash
python .claude/skills/ap-web-i18n/scripts/scan_hardcoded.py
```

Triage the hits (it is a heuristic worklist — see the `ap-web-i18n` skill) and
translate the genuine ones the normal way: `en` key + `fr` translation + `t()`.

### 4. Prune hanging keys — strings main deleted

When `main` deleted or rewrote a string, the key the branch added for it is now
dead weight. Find and remove it:

```bash
python .claude/skills/ap-web-i18n/scripts/find_unused_keys.py
```

This is the script the user means by "if a key isn't used elsewhere, delete
it." It is **alias- and dynamic-key-aware** (it protects `t(\`permMode_${x}\`)`
families and renamed translators), so it won't tell you to delete a live key
that is only reached at runtime — but it is still a *triage worklist, not a
verdict*. For each reported key: grep it yourself; if it is genuinely
unreferenced, delete it from **every** locale (`en` and `fr`, every namespace
that defines it). If it is reached through a variable or dynamic prefix, leave
it. Removing it from only one locale would break parity — so always delete the
matching key from all of them together.

### 5. Gate — this is the proof, not a formality

```bash
python .claude/skills/ap-web-i18n/scripts/check_parity.py   # must exit 0
cd ap-web && npm run type-check && npm test                 # must pass
```

`check_parity.py` confirms every locale still has the identical key set, shared
placeholders match, plural sets are complete, and nothing is empty — i.e. that
re-wiring and pruning didn't desync `en` from `fr`. A green type-check + test
run confirms the re-applied `t()` calls and key-based test assertions actually
compile and hold. Only when all three are green is the rebase truly done.

Report what happened: how many files conflicted, which keys you pruned (and
why), any strings main added that you newly translated, and the final
parity/test state.

## Scope discipline

- **Take main as the source of truth for code.** The user's intent is "use the
  incoming changes from main and just re-add the translated text." Don't try to
  preserve the branch's version of a conflicting hunk over main's logic — port
  the translation onto main's logic.
- **Don't retranslate what survived.** Keys already in the locale JSON are
  done; reuse them. Re-translating invites drift and duplicate keys.
- **Never resolve a conflict by deleting a key from one locale only** — that
  silently breaks parity. Keys are added and removed across all locales together.
- **Don't widen scope to a fresh i18n pass.** If the sweep in Phase 3 surfaces
  far more untranslated strings than the rebase introduced, finish the rebase,
  then report the backlog rather than translating the whole app under cover of
  a "rebase."
- If a conflict is **not** about i18n (main and the branch both changed real
  logic in the same place), resolve it on its merits like any rebase — this
  skill only prescribes the *translation* half of the resolution.
