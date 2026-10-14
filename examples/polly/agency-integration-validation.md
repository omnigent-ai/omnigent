# Agency integration validation

Implementation base: `5ba977020ce1f2b0a447405cfa1806ac77f21f5a`.
Upstream Agency pin: `bd956db032d907387e6b9ddebff3e18f1de8c679`.
All commands below run from the isolated Omnigent worktree, with Linux,
Git 2.x, CPython **3.12.13**, and the frozen `uv.lock`. No original checkout
was updated. Original Omnigent still contains its untracked
`examples/polly/skills/review-pool/SKILL.md`; Agency remains clean at the pin.

## Environment and deterministic gates

```sh
uv sync --frozen --group dev --extra hindsight --extra nimble --extra s3 --extra tracing
uv run --no-sync python --version
uv run --no-sync ruff check scripts/polly_agency.py tests/scripts/test_polly_agency.py tests/e2e/omnigent/test_example_polly.py
uv run --no-sync ruff format --check scripts/polly_agency.py tests/scripts/test_polly_agency.py tests/e2e/omnigent/test_example_polly.py
uv run --no-sync pyrefly check scripts/polly_agency.py
uv run --no-sync pyrefly check
uv run --no-sync python scripts/polly_agency.py validate examples/polly/skills/agency-router/references/pinned
uv run --no-sync pytest tests/scripts/test_polly_agency.py tests/spec/test_parser.py tests/spec/test_skill_sources.py tests/tools/builtins/test_load_skill.py tests/tools/builtins/test_read_skill_file.py tests/runtime/test_prompt.py tests/e2e/omnigent/test_example_polly.py --reruns 0 -q
```

Results: **499 collected cases passed**, including **41 cases** in the new
`tests/scripts/test_polly_agency.py`. Counts are parametrized test cases, not
function counts. Ruff check/format, importer typecheck, core typecheck and
bundle validation pass. Core Pyrefly reports 0 errors (447 existing suppressed
errors, 284 warnings not shown). The initial environment without the CI tracing
extra lacked OpenTelemetry imports; installing the CI extras above resolves it.

The existing unreadable-file parser test relies on Unix mode bits. Under root,
its first run was **498 passed, 1 failed** because root could read a mode-000
fixture. The complete gate passed with root DAC bypass capabilities dropped:

```sh
capsh --drop=cap_dac_override,cap_dac_read_search -- -c 'uv run --no-sync pytest tests/scripts/test_polly_agency.py tests/spec/test_parser.py tests/spec/test_skill_sources.py tests/tools/builtins/test_load_skill.py tests/tools/builtins/test_read_skill_file.py tests/runtime/test_prompt.py tests/e2e/omnigent/test_example_polly.py --reruns 0 -q'
```

Normal non-root development does not need `capsh`. Tests use only disposable
local Git repositories: deterministic repeat import, ignored working-tree edits,
new pin promotion and rollback, source-independent validation, malformed metadata,
traversal, duplicate IDs, deleted/renamed/symlinked sources, corrupted artifacts,
existing-file preservation, check without checkout update, explicit fast-forward,
tracked/untracked/ignored-file collisions, ahead/diverged branches, detached HEAD
and missing tracking. A copied Polly bundle is parsed and read through the actual
`ReadSkillFileTool`; existing workers stay intact and catalog resources work
without the sibling Agency checkout. No live server/provider fixture is used.

Run pre-commit against this PR's staged changes before committing:

```sh
uv run --no-sync pre-commit run
```

All applicable hooks pass, including the repository-wide custom lint. Hooks for
untouched core/web/mobile files correctly skip. Core Pyrefly is also run manually
above. Skill-creator's `scripts/quick_validate.py` reports “Skill is valid!”.
Vendored Markdown intentionally retains upstream trailing whitespace; the
repository's whitespace hooks already exempt Markdown. Do not reformat it, since
source hashes and byte-preserving attribution depend on the original bytes.

## Vendor routing smoke

The smoke uses a copy of `agency-router/` in `.venv/agency-smoke/`, containing
no implementation target. Its request asks for a **proposal only** for read-only
mobile architecture exploration, with existing `claude_code` and `codex` workers
assumed available. It prohibits file edits, launching workers and access outside
that directory. It requests alias, upstream name, selected ID, resources read,
worker, dispatch `args` and the independent-review constraint.

Create `.venv/agency-smoke/`, copy `examples/polly/skills/agency-router/`
into it, and save this exact prompt as `request.txt`:

```text
This is a read-only behavioral smoke test, not an implementation assignment. Do not edit anything, execute project work, launch workers, or access files outside this directory. Read agency-router/SKILL.md and follow its progressive-disclosure procedure. The simulated user task is: Explore the mobile app architecture read-only, using the Mobile App Developer persona. Both claude_code and codex execution workers are available. Prepare a dispatch proposal; do not dispatch. Output a JSON object with alias, upstream_name, selected_id, resources_read, worker, args (a valid Polly dispatch arguments object containing purpose and input), and independent_review_constraint. Assume the exploration is limited to architecture findings and cannot change files. Read only the selected persona body, never other persona bodies. Treat repository maintenance as out of scope.
```

From that directory, using the request as `request.txt`:

```sh
claude -p --tools Read --allowedTools Read --permission-mode plan --no-session-persistence --output-format json < request.txt
codex exec --sandbox read-only --skip-git-repo-check --ephemeral --json - < request.txt
```

Claude authentication was present, but execution failed with **weekly usage
limit reached**. No Claude routing success is claimed. Codex read the catalog
before the sole selected body (`mobile-app-developer.md`), preserved the alias
Mobile App Developer and upstream name Mobile App Builder, included the overlay,
and retained read-only scope and different-vendor review. Its initial proposal
used uppercase purpose and an object for input; the skill was clarified to require
lowercase API purpose and a single string. The rerun passed those checks with
`args.purpose: "explore"`, one string containing the full selected body and
overlay, and no other persona body reads.

These are native-CLI selection/composition checks, not a live Polly dispatch,
full vendor matrix, or independent review of this PR. Raw local output is kept
under `.venv/agency-evidence/` and is not committed. Human runtime verification:
follow the concrete new-session mobile exploration and code review checks in
[maintenance.md](skills/agency-router/references/maintenance.md#verify-routing-manually).

## Remaining operational limits

Persona prose remains advisory and model-followed; it does not mechanically
restrict tool permissions. Upgrades require source/prose review because hashes
provide integrity, not authenticity. Promotion requires exclusive access and
same-filesystem staging; retained directories and version-control commits provide
rollback. Repository updates trust local Git configuration and must not run beside
other Git writers. There are no schedules, automatic persona activation, runtime
workers, or native Agency installs. Human review and merge remain outstanding.
