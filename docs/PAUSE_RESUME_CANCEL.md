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

`same-thread` describes the normal Pause. `goose` and `qwen` fall back to
terminating their subprocess when the `session/cancel` send fails; that kills
the vendor thread, so that degraded path behaves as `new-turn`. The generic ACP
harnesses (`acp`, `grok`, `jcode`) only send `session/cancel` and report a
failed send as an undelivered Pause. `kimi` and `hermes` kill their CLI process
but keep the cached session id, so the next turn resumes the same vendor thread.
A client should treat `same-thread` as "the thread survives a Pause that
lands", not as a guarantee against a vendor that is already broken.

**`cancel`** — always `true`. Cancel is a framework floor, not a per-vendor
feature: Omnigent's own turn teardown records the cancellation and its cause
even when the vendor ignores the interrupt.

## Why nothing suspends

Every harness stops and abandons the turn, so `ends-run` is the honest value
across the board. A true suspend needs the vendor to checkpoint a partially
executed turn and continue it — an interrupt plus a resumable thread is not the
same thing, because the abandoned request must not silently continue. That
distinction is exactly why the `new-turn` harnesses (`antigravity`,
`claude-sdk`, `codex`, `copilot`, `cursor`, `pi`) drop the vendor session: a
resumed vendor thread receives only the latest user message and would skip the
runner's `[System: interrupted]` marker, quietly picking the abandoned request
back up.

## Confirming the stop

Pause waits for the agent to be *observed* stopping rather than assuming it did.
For Codex, `turn/interrupt` returning only proves the app server received the
ask, so `CodexExecutor.interrupt_session` then waits for the turn to be seen
ending — off the reader task's own markers, which keep arriving even when
nothing is consuming `run_turn`. A turn the reader already saw end gets no
interrupt at all.

What the caller sees:

- The harness answers the runner's interrupt with 204 when a harness turn was
  cancelled or the inner agent confirmed its stop, 404 when neither a harness
  turn nor a running inner agent exists, and 409 when there was no harness
  turn and the inner agent was told to stop but not seen stopping. The runner
  treats a 404 as "nothing running" and a 409 as a refusal, which it answers
  the server with as its own 409.
- The server surfaces that 409 on both Pause and Stop as a conflict ("the
  agent did not accept the interrupt and may still be running; try again")
  and does not emit `session.interrupted`. A runner that is unreachable or
  fails outright is handled as before: the session is reported interrupted and
  the fence is lifted so the turn's remaining output is not dropped.
- When a harness turn was cancelled but the inner agent did not confirm, the
  harness turn is torn down regardless, the answer is still 204 and the
  unconfirmed stop is only logged.
- The inner agent reports its outcome before it reaps the session, so the
  response does not wait for the process reap. The reap keeps running in the
  background, a second Pause in that window waits for it instead of
  interrupting again, and the next turn waits (bounded) for it to finish.
- `session.interrupted` is published once the runner has accepted the
  interrupt, so it follows the forward rather than preceding it.
- Harnesses older than this contract answer 404 when they have no turn; the
  runner reads that as idle, so mixed versions do not turn Pause or Stop into
  an error.

A native agent also keeps generating in its own process after the runner's turn
object is gone. "No runner turn" therefore does not mean "not busy": when the
session still reports an in-flight status, the interrupt is forwarded to the
harness so it reaches the vendor's own cancel. Two consequences of forwarding
late:

- On the SDK harnesses that drop their session on interrupt, a Pause that
  lands while the runner's end-of-turn marker is still being flushed reaches
  the harness and drops the warm session, so the next turn starts cold.
- The forward targets whatever the harness is running at that moment; in the
  narrow window where the user has already started a new turn in a native
  agent's own UI, that is the turn that gets cancelled.
- On `antigravity-native`, agy's cancel is a no-op for a step that is waiting
  on a user interaction (ask-question / command-permission). Such a Pause is
  accepted but the step stays blocked until the question is answered or denied
  through the interaction bridge.

## Per-harness matrix

| harness            | integration mode | pause    | resume_after_pause | cancel |
|--------------------|------------------|----------|--------------------|--------|
| acp                | acp-subprocess   | ends-run | same-thread        | true   |
| antigravity        | sdk-in-process   | ends-run | new-turn           | true   |
| antigravity-native | native-tui       | ends-run | same-thread        | true   |
| claude-native      | native-tui       | ends-run | same-thread        | true   |
| claude-sdk         | sdk-in-process   | ends-run | new-turn           | true   |
| codex              | cli-subprocess   | ends-run | new-turn           | true   |
| codex-native       | native-tui       | ends-run | same-thread        | true   |
| copilot            | sdk-in-process   | ends-run | new-turn           | true   |
| cursor             | sdk-in-process   | ends-run | new-turn           | true   |
| cursor-native      | native-tui       | ends-run | same-thread        | true   |
| devin-native       | native-tui       | ends-run | same-thread        | true   |
| goose              | acp-subprocess   | ends-run | same-thread        | true   |
| goose-native       | native-tui       | ends-run | same-thread        | true   |
| grok               | acp-subprocess   | ends-run | same-thread        | true   |
| hermes             | cli-subprocess   | ends-run | same-thread        | true   |
| hermes-native      | native-tui       | ends-run | same-thread        | true   |
| jcode              | acp-subprocess   | ends-run | same-thread        | true   |
| kimi               | cli-subprocess   | ends-run | same-thread        | true   |
| kimi-native        | native-tui       | ends-run | same-thread        | true   |
| kiro-native        | native-tui       | ends-run | same-thread        | true   |
| open-responses     | sdk-in-process   | ends-run | same-thread        | true   |
| openai-agents      | sdk-in-process   | ends-run | same-thread        | true   |
| opencode-native    | native-server    | ends-run | same-thread        | true   |
| pi                 | cli-subprocess   | ends-run | new-turn           | true   |
| pi-native          | native-tui       | ends-run | same-thread        | true   |
| qwen               | acp-subprocess   | ends-run | same-thread        | true   |
| qwen-native        | native-tui       | ends-run | same-thread        | true   |

Regenerate from the declarations rather than editing by hand:

```sh
python -c "
from omnigent.harness_plugins import harness_capabilities
for h, c in sorted(harness_capabilities().items()):
    d = c.as_dict()
    print(h, d['integration_mode'], d['pause'], d['resume_after_pause'], str(d['cancel']).lower())
"
```
