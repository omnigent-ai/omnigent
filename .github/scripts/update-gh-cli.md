# GitHub CLI release updater

Dependabot's Docker ecosystem updates base-image references, not the version and
checksum arguments used by `RUN curl` downloads. `update-gh-cli.yml` fills that
gap for GitHub CLI only; other direct-download tools are not covered.

The workflow runs daily and selects the highest newer stable `cli/cli` release
published at least seven days ago, matching the repository's dependency cooldown.
It downloads the Linux amd64 and arm64 tarballs from the official release,
computes SHA-256, and requires the sizes and digests to match GitHub release-asset
metadata. Missing assets/digests, download errors, checksum failures, malformed
pins or disagreement between Dockerfiles fail without changing either pin block.
Archives are streamed for hashing, never extracted or executed. This verifies
consistency with GitHub's official release, not independent provenance of the
upstream build.

Only `GH_VERSION`, `GH_SHA256_AMD64` and `GH_SHA256_ARM64` in
`deploy/docker/Dockerfile` and `deploy/docker/Dockerfile.ubi` are committed. The
workflow opens one PR on `bot/update-gh-cli` and leaves an existing open PR alone
so it cannot invalidate reviews or overwrite reviewer edits. Close or merge that
PR before another candidate can be proposed. There is no auto-approval or merge;
review release notes and wait for normal image-build/test/review gates.

## Credentials

The existing `OMNIGENT_BOT_APP_ID` variable and `OMNIGENT_BOT_APP_KEY` secret must
identify an App installed on this repository with Contents and Pull requests
write permissions. The write token is minted only after verified pin changes
exist and is scoped to this repository. Read-only checks use `GITHUB_TOKEN`.
There is deliberately no write fallback to `GITHUB_TOKEN`: PRs created with it
would not trigger normal CI. Forks and dispatches from non-main branches do not
run the job.

## Verify or run manually

After merging, open **Actions → Update GitHub CLI pins → Run workflow**, select
`main`, and leave **dry_run** enabled. Logs should report either no eligible
release or a verified version and both digests, without pushing a branch or
opening a PR. An existing open updater PR is reported and preserved.

To create a PR, run again with **dry_run** disabled. For an urgent security
update only, explicitly enable **bypass_cooldown** to consider releases younger
than seven days. This does not bypass digest verification, CI or review.

Locally, with GitHub CLI authenticated:

```sh
python .github/scripts/update_gh_cli.py                # verify only; no writes
python .github/scripts/update_gh_cli.py --min-age-days 0  # verify latest stable
```

The script calls `gh` directly; to use a separate GitHub CLI profile, set
`GH_CONFIG_DIR` when launching it. `--write --body-file /tmp/gh-cli-pr.md` applies
the six pin edits and writes a template-compliant PR description, but does not
commit, push, or create a PR locally.

Offline tests:

```sh
uv run --no-sync pytest tests/scripts/test_update_gh_cli.py --reruns 0 -q
```
