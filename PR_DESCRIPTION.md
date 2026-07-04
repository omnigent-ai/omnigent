## Related issue

N/A

## Summary

Adds an in-tree **`droid`** coding harness that wraps Factory AI's `droid` CLI
(`droid exec --output-format acp`) over the Agent Client Protocol — the same
in-tree pattern as the existing `goose` / `qwen` ACP harnesses.

- `omnigent/inner/droid_executor.py` — `DroidExecutor`: ACP JSON-RPC 2.0
  transport (initialize / session/new / session/prompt / session/update /
  request_permission / fs delegation), cloned from `goose_executor.py`. Its
  `run_turn()` yields `TextChunk` / `ReasoningChunk` (`agent_thought_chunk`) /
  `ToolCallRequest` / `ToolCallComplete` (`tool_call` / `tool_call_update`) /
  `TurnComplete` / `ExecutorError`, plus `interrupt_session()` (ACP
  `session/cancel`). Because Droid runs its own tool loop,
  `handles_tools_internally()` returns True.
- `omnigent/inner/droid_harness.py` — env parsing + `create_app()` via
  `ExecutorAdapter`, mirroring `goose_harness.py`. Env keys:
  `HARNESS_DROID_MODEL`, `HARNESS_DROID_CWD`, `HARNESS_DROID_PATH`,
  `HARNESS_DROID_OS_ENV`, `HARNESS_DROID_AUTO`, `HARNESS_DROID_REASONING`.
- `omnigent/harness_plugins.py` — registers `droid` (additive, localized):
  `valid_harnesses`, `harness_modules`, `model_env_keys`, and a
  `_BUILTIN_CAPABILITIES` entry (`ACP_SUBPROCESS`, `SSE_PERMISSION`,
  `COLD_ONLY`, `EffortFamily.NONE`, `ModelFamily.MULTI`, `OWN_AUTH`,
  `subagents=False`, `interrupt=True`, `streaming=True`).
- `docs/droid-spike.md` — captured spike evidence.

### Spike outcome (verified vs unverified)

The Factory CLI (v0.164.0) **installed** here and the **ACP `initialize`
handshake was verified live**: JSON-RPC 2.0, `protocolVersion: 1`, image prompt
support, `FACTORY_API_KEY` auth. `droid exec --output-format acp` is real; all
launch flags (`--auto`, `-m`, `-r`, `--cwd`) coexist with it.

`session/new` requires a Factory account (no creds available here), so the
per-turn stream shapes (`session/prompt`, `session/update` payloads,
`request_permission`, fs delegation, `session/cancel`) **could not be captured
live**. They follow the ACP spec + goose/qwen shapes and are marked
`# UNVERIFIED` at each site in code and enumerated in `docs/droid-spike.md`.

**Status: functional harness, wired and registered, but pending a live
`session/prompt` verification against Factory credentials** to confirm the
`# UNVERIFIED` stream shapes.

## Test Plan

- `pytest tests/test_harness_capabilities.py tests/test_harness_plugins.py` —
  registry/capability invariants (every harness declares caps, no stray keys,
  model_family matches model_override sets, subagents matches native wrappers).
- `pytest tests/inner/test_droid_executor.py` — 71 tests mirroring the goose
  suite plus droid-specific coverage (reasoning chunk, tool-call
  request/complete, `interrupt_session` → `session/cancel`, launch argv,
  `handles_tools_internally`).
- `ruff check` / `ruff format --check` clean on new + edited files.
- mypy: `droid_executor.py` has the exact same single-file diagnostic profile as
  the reference `goose_executor.py` (ACP-idiom `explicit-any` / `unused-ignore`
  that only surface under single-file resolution). Note: the task requested
  `pyrefly`, but this repo's configured typechecker is mypy; pyrefly is not
  installed or referenced anywhere in the repo.

All gates green (`88 passed` across the three suites).

## Demo

N/A (backend harness; no UI surface).

## Type of change

- [ ] Bug fix
- [x] Feature
- [ ] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change

## Test coverage

- [x] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [x] Manual verification completed
- [x] Existing tests cover this change
- [ ] Not applicable

## Coverage notes

Manual verification: installed the Factory CLI and drove the ACP `initialize`
handshake live (see `docs/droid-spike.md`); the `session/new` auth wall blocked
a full `session/prompt` round-trip, so the per-turn stream shapes are covered by
unit tests against their assumed (ACP-standard) shapes and flagged
`# UNVERIFIED` pending Factory credentials.
