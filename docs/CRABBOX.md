# Crabbox Remote Validation

Omnigent has repo-local Crabbox jobs for running the same high-cost
validation loops on an OpenClaw Crabbox lease that GitHub Actions runs on
hosted runners.

Crabbox is documented at <https://crabbox.sh/>. The Omnigent integration is
kept in:

- `.crabbox.yaml` for job definitions and sync/env policy
- `scripts/crabbox/` for the commands each job executes
- `.github/workflows/crabbox-hydrate.yml` for lease hydration
- `.github/workflows/crabbox.yml` for config validation and manual live runs

## Jobs

```bash
crabbox job list
crabbox job run --dry-run e2e
crabbox job run e2e
```

Available jobs:

- `lint`: pre-commit plus `ap-web` type-checking
- `pytest`: the non-live pytest suite, matching the default pytest excludes
- `islo-smoke`: a minimal Crabbox provider smoke that leases via `provider=islo`
  and writes a proof artifact without Databricks credentials
- `anthropic-smoke`: a minimal Omnigent `claude-sdk` smoke using
  `ANTHROPIC_API_KEY`
- `e2e`: live Databricks-backed E2E tests under `tests/e2e/`
- `e2e-ui`: Playwright UI E2E tests under `tests/e2e_ui/`
- `release-smoke`: build distributions and smoke-install the wheels
- `pr-gates`: lint, pytest, e2e, and e2e-ui in sequence

Live `e2e` and `e2e-ui` jobs require `LLM_API_KEY` and `GATEWAY_BASE_URL`
because they run through the Databricks harness. `islo-smoke` requires
`ISLO_API_KEY` for the Crabbox Islo provider, and `anthropic-smoke` requires
`ANTHROPIC_API_KEY` for the Claude SDK harness. Crabbox only forwards
variables named in `.crabbox.yaml`, so keep new secrets out of the repo and
add only their names to `env.allow` when a job needs them.

## GitHub Actions

The `Crabbox` workflow runs on PRs that touch the Crabbox integration and
validates every configured job with `crabbox job run --dry-run`. Maintainers
can manually dispatch the same workflow with `live=true` to lease a box and
run one job from GitHub Actions.

The workflow builds Crabbox from source pinned to
[openclaw/crabbox#428](https://github.com/openclaw/crabbox/pull/428) rather than
downloading a release: the `v0.32.0` release sends default image/workdir/capacity
fields on Islo sandbox creation, which makes Islo boxes fail to start. Once a
tagged Crabbox release includes that fix, swap the source build back to a
checksum-verified `gh release download` (see `CRABBOX_PR`/`CRABBOX_REF` in
`.github/workflows/crabbox.yml`).

Repository secrets used by the live workflow:

- `ANTHROPIC_API_KEY`
- `CRABBOX_BROKER_URL`
- `CRABBOX_TOKEN`
- `ISLO_API_KEY`
- `LLM_API_KEY`
- `GATEWAY_BASE_URL`

If the broker secrets are omitted, the workflow uses whatever provider and
credentials the runner already has configured.
