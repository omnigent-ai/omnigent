<!--
For AI-written descriptions:
- Follow this template (Related issue, Summary, Test Plan, Demo, Type of change, Test coverage, Coverage notes, Release notes, Changelog).
- Keep it concise; explain the cause, fix, and proof once, in plain language.
- For non-trivial changes, open the Summary with a 1–2 sentence ELI5; add a diagram when it
  makes a relationship or sequence easier to follow.
- Keep every section and checkbox row in place, except Changelog when instructed below.
- Choose exactly one Release notes checkbox; if Yes, write the entry in Changelog.
- For UI changes (the "UI / frontend change" box below), fill in the Demo
  section: attach a screenshot or screen recording of the new behaviour.
-->

## Related issue

<!--
Link the issue this PR addresses with a closing keyword so GitHub auto-links it
(and closes it on merge): e.g. `Closes #123`. One issue per PR. Linking also
gives this PR the issue's priority in the review queue. If an older, still-open
community PR already closes the same issue, the newer one may be auto-closed as
a duplicate (maintainer PRs are exempt).

If this is either a `Refactor / chore`, `Docs`, or `Test / CI` *Type of change* 
below, then no issue is required to be associated. 
-->

Closes #

## Summary

<!-- What changed and why, in 1-3 bullets or a short paragraph. -->

## Test Plan

<!-- How was this change tested? Describe the steps, commands, or scenarios used to verify it. Include a screenshot or recording where helpful. -->

## Demo

<!--
Choose the evidence format that applies. UI / frontend changes require video or
images. For non-visual changes, put reproducible evidence here or in Test Plan.
-->

- [ ] Visual demo attached below
- [ ] Non-visual evidence provided below or in Test Plan
- [ ] Not applicable — no behavioral change

## Type of change

- [ ] Bug fix
- [ ] Feature
- [ ] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change

## Test coverage

<!-- Check all that apply. Be honest: reviewers and agents use this to spot coverage gaps. -->

- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [ ] Not applicable

## Coverage notes

<!--
Optional — but required if you checked "Manual verification completed" or
"Not applicable" above. Describe what you verified manually, or why automated
test coverage is not needed for this change.
-->

## Release notes

Should this change be included in the release notes? Choose exactly one.

- [ ] No — no noteworthy user-facing change.
- [ ] Yes — include the entry in the Changelog section.

<!--
Choose Yes only for outstanding user-facing features, bug fixes, UX changes,
and breaking changes. Breaking changes must choose Yes and explain the
compatibility impact in Changelog. Features behind a feature flag are eligible
only once the flag is enabled for users.
Choose No for small fixes or improvements, features behind disabled flags, and
internal changes such as CI, refactors, test-only changes, or dependency bumps
with no user impact. Keep this section and both checkboxes.
This records the author's recommendation; maintainers curate the release notes.
-->

## Changelog

<!--
One line, in the user's voice, describing the user-facing change. The category
is taken from the "Type of change" boxes above (e.g. UI / frontend change renders
as "[UI] <your line>"), so don't repeat it here — just describe the change. The
PR link and author credit are added for you.

If you chose Yes in Release notes, replace the placeholder with your entry.
If you chose No, DELETE THIS WHOLE SECTION. The complete changelog will still
use the PR title and credit the author. A Breaking change must always keep
this section.

Example:  `omnigent run --watch` reruns an agent when files change
-->

<Add a line to describe the change, else delete this section>
