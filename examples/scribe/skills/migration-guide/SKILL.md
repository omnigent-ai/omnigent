---
name: migration-guide
description: Turn a breaking change (an API rename, removed flag, changed default, or moved module) into concrete upgrade steps with before/after examples. Use when the user asks how to migrate, upgrade, or adapt to a breaking change.
---

# migration-guide — write upgrade steps for a breaking change

Produce a guide that lets a user on the old version land on the new one with the
least friction.

## Pin down what actually broke

Identify the precise breaking change before writing a word. Get it from the diff
and history, not from the description alone:
- `git diff <old>..<new>` on the affected surface, and `gh pr view <n>` for the
  PR that introduced it.
- Dispatch the researcher (`purpose: explore`) to confirm the exact old and new
  shapes: the old name/signature/default, the new one, and whether a
  compatibility shim or deprecation window exists.

Never document a rename or signature change from memory — the exact symbols are
what users will copy.

## Structure

    # Migrating to <version>

    ## What changed
    <one paragraph: the change and why, in user terms>

    ## Upgrade steps
    1. <ordered, mechanical steps>

    ## Before / after
    ```
    # before
    ...
    # after
    ...
    ```

    ## If you can't upgrade yet
    <deprecation window, shim, or flag to opt out — if one exists>

## Write the steps

- Make each step mechanical and verifiable ("rename `--foo` to `--bar`", not
  "update your flags"). A user should be able to follow it without rereading the
  whole guide.
- Show a minimal, real before/after for each distinct change. Keep examples
  copy-pasteable.
- State explicitly when there is no automated path and a manual edit is
  required.

## Verify

Route the finished guide through the `reviewer` (`purpose: review`). Migration
guides are acted on directly, so a wrong flag name or step is high-cost — the
cross-vendor fact-check is worth it here.
