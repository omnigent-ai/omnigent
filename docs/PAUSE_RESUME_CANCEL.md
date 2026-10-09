# Pause, Resume and Cancel across harnesses

What Pause actually does depends on the harness. This page is the published
contract so a client — the web UI, or an orchestrator driving sessions through
the REST API — can tell what will happen *before* it presses Pause, instead of
learning it from an incident.

The same three axes are served per harness on `GET /v1/harnesses`, under each
row's `capabilities`, so a client can read them at runtime rather than pinning
this table:

```json
{"pause": "ends-run", "resume_after_pause": "same-thread", "cancel": true}
```

They are declared in `omnigent/harness_plugins.py` and typed in
`omnigent/harness_capabilities.py` (`PauseSemantics`, `PauseResume`).

## What the values mean

**`pause`** — what Pause does to the vendor agent.

| value | meaning |
|---------------|--------------------------------------------------------------|
| `suspends`    | The turn freezes in place and can be continued later. **Reserved — no harness earns this today**, and a guard test keeps it that way so nothing can quietly claim it. |
| `ends-run`    | The turn is stopped and abandoned. Work in flight at the moment of Pause is lost. |
| `unsupported` | Pause does not reach the vendor agent at all. |

**`resume_after_pause`** — what the *next* turn continues.

| value | meaning |
|---------------|--------------------------------------------------------------|
| `same-thread` | The vendor keeps the conversation it already had, so the next turn lands in it. |
| `new-turn`    | The vendor session was dropped; the next turn rebuilds it from Omnigent's transcript. The conversation is preserved — the vendor's own thread is not. |
| `unsupported` | There is nothing to resume. |

`same-thread` describes the normal Pause. The ACP harnesses (`goose`, `qwen`,
`grok`, `jcode`, …) fall back to terminating the subprocess when Pause arrives
before the session handshake finished, or when the `session/cancel` send itself
fails; that kills the vendor thread, so those degraded paths behave as
`new-turn`. A client should treat `same-thread` as "the thread survives a Pause
that lands", not as a guarantee against a vendor that is already broken.

**`cancel`** — always `true`. Cancel is a framework floor, not a per-vendor
feature: Omnigent's own turn teardown records the cancellation and its cause
even when the vendor ignores the interrupt.

## Why nothing suspends

Every harness stops and abandons the turn, so `ends-run` is the honest value
across the board. A true suspend needs the vendor to checkpoint a partially
executed turn and continue it — an interrupt plus a resumable thread is not the
same thing, because the abandoned request must not silently continue. That
distinction is exactly why the `new-turn` harnesses drop the vendor session: a
resumed vendor thread receives only the latest user message and would skip the
runner's `[System: interrupted]` marker, quietly picking the abandoned request
back up.

## Confirming the stop

Pause waits for the agent to be *observed* stopping rather than assuming it did.
For Codex, `turn/interrupt` returning only proves the app server received the
ask, so `CodexExecutor.interrupt_session` then waits for the turn to be seen
ending — off the reader task's own markers, which keep arriving even when
nothing is consuming `run_turn`. An unconfirmed stop is logged and returned as
`False`, so a Pause that did not land is reported rather than assumed.

A native agent also keeps generating in its own process after the runner's turn
object is gone. "No runner turn" therefore does not mean "not busy": when the
session still reports an in-flight status, the interrupt is forwarded to the
harness so it reaches the vendor's own cancel.

## Per-harness matrix

| harness            | integration mode | pause    | resume_after_pause | cancel    |
|--------------------|------------------|----------|--------------------|-----------|
| acp                | acp-subprocess   | ends-run | same-thread        | supported |
| antigravity        | sdk-in-process   | ends-run | new-turn           | supported |
| antigravity-native | native-tui       | ends-run | same-thread        | supported |
| claude-native      | native-tui       | ends-run | same-thread        | supported |
| claude-sdk         | sdk-in-process   | ends-run | new-turn           | supported |
| codex              | cli-subprocess   | ends-run | new-turn           | supported |
| codex-native       | native-tui       | ends-run | same-thread        | supported |
| copilot            | sdk-in-process   | ends-run | new-turn           | supported |
| cursor             | sdk-in-process   | ends-run | new-turn           | supported |
| cursor-native      | native-tui       | ends-run | same-thread        | supported |
| devin-native       | native-tui       | ends-run | same-thread        | supported |
| goose              | acp-subprocess   | ends-run | same-thread        | supported |
| goose-native       | native-tui       | ends-run | same-thread        | supported |
| grok               | acp-subprocess   | ends-run | same-thread        | supported |
| hermes             | cli-subprocess   | ends-run | new-turn           | supported |
| hermes-native      | native-tui       | ends-run | same-thread        | supported |
| jcode              | acp-subprocess   | ends-run | same-thread        | supported |
| kimi               | cli-subprocess   | ends-run | new-turn           | supported |
| kimi-native        | native-tui       | ends-run | same-thread        | supported |
| kiro-native        | native-tui       | ends-run | same-thread        | supported |
| open-responses     | sdk-in-process   | ends-run | same-thread        | supported |
| openai-agents      | sdk-in-process   | ends-run | same-thread        | supported |
| opencode-native    | native-server    | ends-run | same-thread        | supported |
| pi                 | cli-subprocess   | ends-run | new-turn           | supported |
| pi-native          | native-tui       | ends-run | same-thread        | supported |
| qwen               | acp-subprocess   | ends-run | same-thread        | supported |
| qwen-native        | native-tui       | ends-run | same-thread        | supported |

Regenerate from the declarations rather than editing by hand:

```sh
python -c "
from omnigent.harness_plugins import harness_capabilities
for h, c in sorted(harness_capabilities().items()):
    print(h, c.pause.value, c.resume_after_pause.value, c.cancel)
"
```
