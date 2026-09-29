# Test suite selection

`select-test-suites.py` is shared by CI, backend E2E, UI E2E, Integration,
and Windows through `select-test-suites.yml`. On PRs it skips product tests
only when every changed path belongs to the explicit Polly review allowlist
(optionally with `CHANGELOG.md`). CI then runs the four Polly script-test files;
lint and security checks keep their existing behavior. Partial runs do not
publish product coverage.

The selector checks both names of renames, paginates the file list, and verifies
the event's base/head SHAs and file count before and after fetching. Unknown
paths, missing data, API failures, stale events, and potentially truncated diffs
run the existing suites. Push, scheduled, and manual runs also keep their
existing full-suite behavior. Each selection job explains its decision in the
Actions summary.

Keep the allowlist narrow: add an automation component only after identifying
its consumers and wiring the tests that replace the skipped product suites.
Do not broadly exclude `.github/`, shared fixtures, or dependency files.

To validate selection locally:

```bash
uv run --no-project --with pytest --with pyyaml pytest --confcutdir=tests/scripts \
  -o addopts= tests/scripts/test_ci_suite_selection.py -q
```

To verify in Actions after merging, open a PR changing only
`.github/scripts/polly-review-summary.py`. Expect `Polly script tests` and lint
to run, with product suites skipped. Add an `omnigent/` change to the same PR
and confirm product suites run again. Changes to the selector itself always
retain broad testing.
