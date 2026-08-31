---
name: audit-rubric
description: Mechanically test the seven recurring failure patterns in an untrusted fix pass.
---
# Audit rubric
For every claim, read the claiming diff and the relevant user path, and record
the exact evidence. Check these patterns mechanically:

1. Capability built, nothing reaches it: search the new symbol and trace a user
path to it. 2. Test asserts the defect: if an expectation changed, prove the
new expectation independently. 3. Claim wider than diff: search for omitted
“piece 1” cases and all named record types. 4. One configuration only: on a
template-driven platform, flag hard-coded entity/field/record-type names.
5. Guard hides a wrong value: inspect fallbacks, defaults, and swallowed
errors. 6. New data only: inspect migrations and demand an additive repair path
for existing tenants. 7. Regression by the fix: enumerate new failure modes,
such as an unguarded regex compile turning malformed input into a 500.

Return claim id, commit, file:line, defect rationale, evidence read, confidence,
and the observation or test that would settle uncertainty. A green unit test
does not prove reachability.

Commands: `git show --format= --unified=0 <commit> -- '*.py' | grep -E '^\+[^+]'` finds new symbols; `git grep -n 'new_symbol' <commit> -- ':!tests'` finds callers. Trace imports/routes to a user entry point. Check tests with `git diff <commit>^ <commit> -- '*test*'`; compare new expectations to the requirement. Compare nouns to `git show --stat <commit>` and find hard-coding with `git grep -nE '"(customer|field|record_type)"' <commit>`. Find guards with `git grep -nE 'except|fallback|default|or None' <commit>`, inspect migrations with `git show <commit> -- migrations`, and run malformed-input cases for regressions.
