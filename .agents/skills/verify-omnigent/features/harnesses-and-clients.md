# Harnesses and clients

## Sub-features

- Native and SDK Claude, Codex, Cursor, Pi, Hermes, and other adapters
- Runner/host registration, tunnel, resume, interrupt, and approvals
- Python SDK and web client event parsing
- Polly/Debby orchestration and sub-agent wakeups

## How to get to it

Choose a harness while creating a session or launch it from the CLI, send a
turn, respond to any native prompt, interrupt or resume, and return to the
session from another supported client.

## Driving it with Playwright

Start with `tests/harness_bench --no-live` and the harness-specific project E2E
skill. For a real canary, use an isolated server and credential source, launch
the actual adapter, then use Playwright to prove the session's messages, status,
prompts, and metadata render correctly. Keep provider and model time outside
Omnigent latency claims; the performance profile uses a zero-latency mock LLM
to isolate Omnigent overhead.

The observable end state is a completed persisted turn whose UI blocks and
metadata match the native or SDK events, plus clean runner/host teardown.

## Gotchas

- Never substitute a mocked adapter for the primary subject under test.
- Unsupported or unavailable harnesses must report skipped, not passed.
- Server source, embedded UI, and Lakebox runner/host wheels can all be on
  different versions downstream.
- Native harness process startup is vendor-controlled and excluded from the
  Omnigent performance benchmark.
