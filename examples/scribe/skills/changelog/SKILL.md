---
name: changelog
description: Turn a range of commits or merged PRs into a changelog entry grouped by change type. Use when the user asks for release notes, a changelog, or "what changed" between two points.
---

# changelog — write a changelog from a commit/PR range

Turn a range of history into a clear changelog entry that a user can read to
decide whether and how to upgrade.

## Gather the range

Establish the range first. Honor an explicit one ("since v1.2.0", "the last 10
commits", "PRs merged this week"); otherwise default to commits since the most
recent tag (`git describe --tags --abbrev=0` then `git log <tag>..HEAD`).

Collect the raw material yourself with `sys_os_shell`:
- `git log <range> --no-merges --pretty=format:'%h %s'` for the commit subjects.
- `git log <range> --merges` or `gh pr list --search "merged:>=<date>"` for PRs.
- `git diff <range> --stat` to see the surface area, and `gh pr view <n>` for a
  PR's intent when a subject line is terse.

When a subject is unclear about user impact, dispatch the researcher
(`purpose: explore`) to read the diff and report what actually changed for
users — do not guess from the subject alone.

## Group by change type

Use the Keep a Changelog categories, dropping any that are empty:

    ## <version or range> — <YYYY-MM-DD>

    ### Added
    ### Changed
    ### Deprecated
    ### Removed
    ### Fixed
    ### Security

## Write each entry

- One line per user-visible change, in the imperative or past tense, describing
  the effect on the user — not the internal mechanics.
- Lead with the change, link the PR or commit in parentheses at the end.
- Omit pure-internal churn (refactors, test-only changes, CI) unless it changes
  behavior. A changelog is for users, not a commit dump.
- Call out breaking changes explicitly and point to the migration-guide skill
  if upgrade steps are needed.

## Verify

Before finalizing, route the draft through the `reviewer` (`purpose: review`)
when the changelog will ship — version numbers, "removed"/"breaking" claims, and
flag names are exactly what a fact-check catches.
