# Where tests belong

Start with the nearby tests for the production component or user journey. Extend
an existing scenario and reuse its setup when that expresses the behavior clearly.
The directories below also determine fixture inheritance and CI selection.

| Behavior under test | Location | Execution boundary |
| --- | --- | --- |
| Harness adapter configuration, hooks, bridges, forwarding | `harnesses/<adapter>/` | Default pytest suite; may include local processes |
| Shared native infrastructure in `omnigent/native/` | `native/` | Default pytest suite |
| Runtime scaffold, process manager, policies | `runtime/` | Default suite; directory fixtures isolate ambient discovery |
| Runner and server components | `runner/`, `server/` | Default suite; `server/integration/` includes mock-provider integration |
| CLI, stores, SDKs and other components | Existing owning directory | Follow nearby fixtures and production ownership |
| Server/runner/CLI journeys | `e2e/` | Explicit pytest target; inspect fixture and provider prerequisites |
| Per-harness journeys | `integration/` | Explicit target; mock mode or authorized real credentials |
| Built SPA with mocked backend contracts | `browser_ui/<feature>/` | Browser lane; no Omnigent server/runner |
| Browser journeys through the real server/runner | `e2e_ui/<feature>/` | Browser E2E lane |
| Deployed-app journeys | `e2e_live/` | Explicit live-app target |

Frontend component tests remain colocated under `web/`. Harness adapter tests
mirror `omnigent/harnesses/<adapter>/`; `runtime/harnesses/` owns the runtime
scaffold, not every test whose name contains "harness". A filename containing
`integration` or `e2e` alone does not determine its execution requirements.

## Shared setup

- `tests/_helpers/` holds reusable operations such as session upload and local
  server lifecycle. Keep caller-specific inputs, assertions and cleanup visible.
- `tests/helpers/` currently holds UI configuration, recording and timing support.
- Keep support used by one component beside its tests. Use `conftest.py` for
  fixtures that should apply to that directory; avoid widening an autouse
  fixture's scope just to share it.

## Moving or splitting existing tests

Preserve the execution lane and inherited fixtures. Check imports (including
subprocess module strings), path-relative assets, CI selectors, feature maps and
documented commands. Record old-to-new test paths for reviewers and historical
failures; node IDs and shard assignment can change after a move.

Compare collected test names, parameter IDs, markers and fixture definitions
before and after, normalizing only the intended path changes. Then run the moved
tests and affected fixture consumers with retries disabled. Collection alone
does not validate setup, real processes or cleanup. Keep scenario deletion and
behavior changes separate from organization work.

For the Claude-native adapter, run:

```sh
uv run --no-sync pytest tests/harnesses/claude_native --reruns 0 -n 4 --dist loadfile
```
