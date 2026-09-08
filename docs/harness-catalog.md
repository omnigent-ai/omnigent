# Harness catalog

What each harness Omnigent can launch actually supports, what is installed on
which host, and which of those claims survive contact with the running system.

Two sources feed this document and they disagree in useful ways:

- **Declared** — `HarnessCapabilities` in the plugin registry
  (`omnigent/harness_capabilities.py`), one row per harness. This is what
  Omnigent believes.
- **Observed** — probes against the live deployment on 2026-09-08: the running
  server at `oz-poweredge-r820.taild18d9.ts.net:10443` (Omnigent v0.12.0,
  commit 619b5b32), `GET /v1/hosts`, and real sessions created through the
  public API.

Where they disagree, the observed column wins and the gap is written down.

## Summary of what was found

| # | Finding | Evidence |
|---|---|---|
| 1 | `initial_items` are stored but never dispatched to a native TUI — **now warned about** | A/B on one session, below |
| 2 | A workspace under `/tmp` kills the `claude-native` terminal | `required_terminal_exited`, below |
| 3 | A malformed client body returns HTTP 500, not 422 | `POST /v1/sessions`, below |
| 4 | `GET /v1/harnesses` exposed 9 of 26 harnesses — **fixed here** | registry vs. endpoint, below |
| 5 | The picker offers uninstalled harnesses and hides installed ones | availability table, below |

## Installed versions

Harness binaries live on the **host**, not in the Omnigent container. Probing
inside `omnigent-r820-omnigent-1` finds none of them; runners execute on r820
itself. Anything reasoning about harness availability has to ask the host.

| binary | r820 path | version |
|---|---|---|
| `claude` | `/home/oz/.local/bin/claude` | 2.1.263 |
| `codex` | `/home/oz/.local/bin/codex` | codex-cli 0.153.4 |
| `agy` | `/home/oz/.local/bin/agy` | 1.1.27 |

Not on the host `PATH`: `opencode`, `pi`, `cursor-agent`, `kiro`, `goose`,
`hermes`, `qwen`, `kimi`, `grok`, `devin`, `antigravity`, `amp`.

`agy` (Antigravity CLI) is installed and reported available to Omnigent as
`agy` / `agy-native` / `native-agy`, but has no `HarnessCapabilities` row and
no picker entry, so it is invisible to the catalog API.

## Model selection

`GET /v1/hosts/{host}/harnesses/{harness}/model-options`, against r820:

| harness | model options |
|---|---|
| `claude-native` | `sonnet` → claude-sonnet-5, `opus` → claude-opus-5 |
| `codex-native` | `gpt-6-astra` and one more, with upgrade/availability metadata |
| `pi-native` | endpoint answers, lists are empty (`models: []`) |
| `opencode-native` | `model options are unsupported for harness 'opencode-native'` |
| `antigravity-native` | `model options are unsupported for harness 'antigravity-native'` |

Note the three distinct shapes — populated, empty, and explicitly unsupported.
A caller that treats "unsupported" and "empty" the same will render an empty
model picker for `pi-native` rather than saying why it is empty.

### Declared capabilities

| harness | mode | elicitation | resume | models | auth | subagents | stream | interrupt | fork history | instructions |
|---|---|---|---|---|---|---|---|---|---|---|
| `acp` | acp-subprocess | sse-permission | cold-only | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `antigravity` | sdk-in-process | none | cold-only | gemini | own-auth | no | yes | yes | none | composed-per-turn |
| `antigravity-native` ★ | native-tui | none | warm-reattach | gemini | own-auth | no | yes | yes | none | not-delivered |
| `claude-native` ★ | native-tui | hook | warm-reattach | claude | omnigent-credential | yes | yes | yes | rebuild | agent-startup-additive |
| `claude-sdk` | sdk-in-process | none | cold-only | claude | omnigent-credential | no | yes | yes | none | composed-session-snapshot |
| `codex` | cli-subprocess | jsonrpc | warm-reattach | gpt | omnigent-credential | no | yes | yes | none | composed-per-turn |
| `codex-native` ★ | native-tui | jsonrpc | warm-reattach | gpt | omnigent-credential | yes | yes | yes | rebuild | agent-startup-additive |
| `copilot` | sdk-in-process | none | cold-only | multi | own-auth | no | yes | yes | none | composed-per-turn |
| `cursor` | sdk-in-process | none | warm-reattach | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `cursor-native` ★ | native-tui | approval-mirror | warm-reattach | multi | own-auth | no | no | yes | preamble | not-delivered |
| `devin` | acp-subprocess | sse-permission | cold-only | multi | own-auth | yes | yes | yes | none | first-user-prefix |
| `goose` | acp-subprocess | sse-permission | cold-only | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `goose-native` ★ | native-tui | approval-mirror | warm-reattach | multi | own-auth | no | yes | yes | none | not-delivered |
| `grok` | acp-subprocess | sse-permission | cold-only | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `hermes` | cli-subprocess | hook | cold-only | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `hermes-native` ★ | native-tui | approval-mirror | warm-reattach | multi | own-auth | no | yes | yes | rebuild | not-delivered |
| `kimi` | cli-subprocess | none | warm-reattach | multi | session-scoped-config | no | yes | yes | none | not-delivered |
| `kimi-native` ★ | native-tui | hook | warm-reattach | multi | session-scoped-config | no | yes | yes | none | not-delivered |
| `kiro-native` ★ | native-tui | approval-mirror | warm-reattach | multi | own-auth | no | no | yes | none | not-delivered |
| `open-responses` | sdk-in-process | none | cold-only | multi | omnigent-credential | no | yes | yes | none | composed-per-turn |
| `openai-agents` | sdk-in-process | none | cold-only | multi | omnigent-credential | no | yes | yes | none | composed-per-turn |
| `opencode-native` ★ | native-server | sse-permission | warm-reattach | multi | own-auth | yes | yes | yes | preamble | composed-per-turn |
| `pi` | cli-subprocess | none | cold-only | multi | omnigent-credential | no | yes | yes | none | composed-per-turn |
| `pi-native` ★ | native-tui | none | warm-reattach | multi | session-scoped-config | no | yes | yes | rebuild | not-delivered |
| `qwen` | acp-subprocess | sse-permission | cold-only | multi | own-auth | no | yes | yes | none | first-user-prefix |
| `qwen-native` ★ | native-tui | approval-mirror | warm-reattach | multi | own-auth | no | no | yes | rebuild | not-delivered |

### Availability on Oz's hosts

| harness | r820 | p16v | tpe | in UI picker |
|---|---|---|---|---|
| `acp` | no | no | no | **no** |
| `antigravity` | yes | yes | yes | yes |
| `antigravity-native` ★ | yes | no | no | **no** |
| `claude-native` ★ | yes | needs-auth | needs-auth | **no** |
| `claude-sdk` | yes | yes | yes | yes |
| `codex` | yes | yes | yes | yes |
| `codex-native` ★ | yes | yes | yes | **no** |
| `copilot` | no | no | yes | yes |
| `cursor` | no | no | no | yes |
| `cursor-native` ★ | binary-missing | binary-missing | binary-missing | **no** |
| `devin` | no | no | no | yes |
| `goose` | no | no | no | **no** |
| `goose-native` ★ | no | no | no | **no** |
| `grok` | no | no | no | yes |
| `hermes` | no | no | no | yes |
| `hermes-native` ★ | no | no | no | **no** |
| `kimi` | no | no | no | **no** |
| `kimi-native` ★ | no | no | no | **no** |
| `kiro-native` ★ | no | no | no | **no** |
| `open-responses` | yes | yes | yes | **no** |
| `openai-agents` | yes | yes | yes | **no** |
| `opencode-native` ★ | needs-auth | yes | yes | **no** |
| `pi` | binary-missing | binary-missing | binary-missing | yes |
| `pi-native` ★ | binary-missing | binary-missing | binary-missing | **no** |
| `qwen` | no | no | no | **no** |
| `qwen-native` ★ | no | no | no | **no** |

★ marks a native-TUI wrapper — the ones Omnigent launches as a resident vendor
terminal, and the ones Oz's fleet actually uses.

`—` means the host does not report that spelling at all. Hosts report harness
availability under several aliases (`claude-native`, `native-claude`), and the
table above resolves both spellings to one row.

## Usability tests from the API

Run 2026-09-08 against the live server as `admin`, harness `claude-native`,
host r820. Each row is a real session, not a simulation.

| Test | Result | Evidence |
|---|---|---|
| Create session | **pass** | `POST /v1/sessions` → 201, session bound to runner and host |
| Send a prompt | **pass** | `POST /v1/sessions/{id}/events` → 202 `{"queued":true}` |
| Observe streaming | **pass** | `GET /v1/sessions/{id}/stream` → 200; `session.heartbeat`, `session.presence`, `session.skills`, `session.model_options`, `session.changed_files.invalidated` |
| Tool execution | **pass** | `Bash` call `echo omnigent-bench-ok` → `function_call_output` `omnigent-bench-ok` |
| Cost / budget display | **pass** | `total_cost_usd` 0.0 → 0.157812, `last_total_tokens` 38614, `usage_by_model` keyed by `claude-opus-5[1m]`, `context_window` 1000000 |
| Deliver `initial_items` | **fail** | see below |
| Permission elicitation | **not exercised** | 0 elicitations fired at `permission_level: 4`; provoking one needs an agent whose policy asks |
| Resume after runner restart | **not exercised** | restarting a runner disturbs other lanes' live sessions; needs a scheduled window |

### Finding 1 — `initial_items` never reach a native TUI

The strongest result here, because it is an A/B on a **single session** rather
than a comparison across two.

Session `7146919f`, `claude-native`, host r820, workspace `/home/oz/bench-ws`.

1. Created with `initial_items` carrying one user message:
   *"Use the Bash tool to run exactly: echo omnigent-bench-ok"*.
   → 201. The message is stored as a session item with `response_id: "seed"`.
   For the next **60 seconds** the session stayed `status: "idle"` with
   `items: 2` (the seeded message and the terminal resource event). The prompt
   never ran.
2. The **identical message** was then posted to
   `POST /v1/sessions/{id}/events`.
   → 202 `{"queued": true}`, `status: "running"` within 12s, back to `idle` by
   36s, `items` 2 → 7, the `Bash` tool called, `omnigent-bench-ok` returned,
   `total_cost_usd` 0.0 → 0.157812, 38614 tokens.

So `initial_items` are persisted as conversation history and never dispatched
to the harness. A client that creates a session with a prompt and waits gets
silence — no error, no rejection, just an idle session holding a message that
looks delivered.

**Partly addressed** in this branch. The full fix — dispatching seeded items
once the runner comes up — is a real change to session creation that cannot be
verified without a deploy, so it stays open as issue #2. What is fixed is the
*silence*: the create response now carries an
`initial_items_seeded_not_dispatched` warning and the server logs one, so the
downgrade is visible when it happens instead of being inferred from a session
that never leaves `idle`.

### Finding 2 — a `/tmp` workspace kills the `claude-native` terminal

Session `de255488`, identical request except `workspace` pointed at a
directory under `/tmp/claude-1000/...`:

```
status: failed
last_task_error.code: required_terminal_exited
"Required terminal exited unexpectedly; the session runtime is no longer available.
 terminal: claude:main
 command: claude (6 args; argv omitted because terminal args may contain secrets)
          (exited with status 1)"
```

The same request with `workspace: /home/oz/bench-ws` launched cleanly. The
launch is workspace-path dependent, and the failure surfaces only after the
terminal has already died. `argv` is redacted in the diagnostic — correct, since
terminal args may carry secrets — but it means the actual cause is not
recoverable from the session record.

### Finding 3 — a malformed body returns 500, not 422

`POST /v1/sessions` with `initial_items[].role` and `.content` at the top level
instead of nested under `data` returns:

```
HTTP 500 {"error":{"code":"internal_error","message":"An internal error occurred."}}
```

The real cause appears only in the container log:

```
ValidationError: 2 validation errors for MessageData
role     Field required [type=missing, input_value={'type': 'message'}]
content  Field required [type=missing, input_value={'type': 'message'}]
```

Extra top-level keys are dropped before `data` is validated, so the request
arrives as `{'type': 'message'}` and fails deep in the route rather than at the
schema boundary. A client mistake is reported as a server fault, and the field
names — the only useful part — are visible only to whoever can read the logs.

### Finding 4 — the catalog API exposes 9 of 26 harnesses

`harness_capabilities()` returns rows for **all 26** harnesses. `GET
/v1/harnesses` returns **9** — the picker catalog only. Absent from it: every
native-TUI wrapper, which is to say every harness Oz's fleet actually launches
(`claude-native`, `codex-native`, `opencode-native`, `pi-native`,
`cursor-native`, `kiro-native`, `goose-native`, `hermes-native`,
`antigravity-native`, `qwen-native`, `kimi-native`), plus `open-responses` and
`openai-agents`.

The data is computed and then not served. Nothing in the UI could answer "does
this harness support subagents?" for a harness the UI can launch.

**Fixed** in this branch: the endpoint now also returns a `capabilities` map
keyed by harness spelling, covering all 26 — the same shape and reasoning as
the existing `setup_steps` map. The picker catalog (`data`) is unchanged.

### Finding 5 — the picker inverts availability

Cross-referencing the availability table against picker membership:

- **Installed but hidden from the picker:** `claude-native`, `codex-native`,
  `antigravity-native`, `open-responses`, `openai-agents` (all `yes` on r820),
  and `opencode-native` (`needs-auth`, i.e. installed, awaiting login).
- **In the picker but not installed anywhere:** `cursor`, `devin`, `grok`,
  `hermes` (`no` on all three hosts) and `pi` (`binary-missing` on all three).

So the picker offers five harnesses that cannot run and hides six that can.

## Known traps

Behaviours confirmed here or carried from fleet-reality notes, worth knowing
before trusting a harness:

- **`initial_items` are inert for native TUIs.** Finding 1. Send the first
  prompt as a separate `POST .../events` after creation.
- **Workspace path affects launch.** Finding 2. Keep workspaces under the
  runner user's home.
- **Harness binaries are not in the container.** Probing the Omnigent container
  for `claude`/`codex` finds nothing; they live on the host.
- **`instruction_delivery: not-delivered`** is declared for `antigravity-native`,
  `cursor-native`, `goose-native`, `hermes-native`, `kimi-native`,
  `kiro-native`, `opencode-native` and `qwen-native` — `AgentSpec.instructions`
  do not reach those vendors at all. An agent's instructions silently do nothing.
- **`cursor-native` declares `streaming: no`** — the only native wrapper that
  does. Expect a single blob, not deltas.
- **`needs-auth` is not `no`.** `claude-native` on p16v/tpe and
  `opencode-native` on r820 are installed but unauthenticated; they will appear
  launchable and then fail.
- **The web toolchain needs Node ≥ 20.19.** r820's host Node is 20.7.0, so
  `vitest` and `tsc` fail there with a `styleText` import error. Run them in a
  `node:20-bookworm` container.

## What Omnigent should add

Ranked by how much they change what a user can actually do, from the evidence
above. The first two are bugs whose absence silently costs work; the rest are
capabilities the fleet has no substitute for today.

**1. Deliver `initial_items` (issue #2, design in #7).** Today a session created
with a first prompt never runs it, with no error. Every programmatic client has
to know the folklore — create, then post the prompt separately — and one that
does not simply hangs. This is the single cheapest fix with the widest blast
radius, and the design seam already exists (`_on_runner_connect`).

**2. Make the harness picker reflect the selected host (issue #6).** The picker
currently offers five harnesses installed on no host and hides six that are
installed, so the primary way a user chooses a harness is close to
anti-correlated with what will start. `GET /v1/hosts` already returns the truth
per host; the picker just does not consult it. `needs-auth` should be its own
actionable state ("log in") rather than being collapsed into unavailable.

**3. Surface pacing delay per session and per workstream.** Chat already shows
*this* session's wait ("Waiting 12.3s for quota"), and the new panel shows
window fullness — but nothing answers "why is my session the one waiting?" The
next LLMQ release adds `workstream_pacing[]` to `/v1/snapshot` with
`delay_seconds_for_default_estimate` per (window, workstream), which is exactly
the missing number. Rendering it turns the panel from a fuel gauge into an
explanation, and it is additive: the reduction in `quota_status.py` ignores
unknown keys, so it can be adopted without a controller lockstep.

**4. Attest agent identity at registration (issue #8).** Registry `agent_id`s
are self-asserted, and on 2026-09-08 two sessions signed as each other for
hours. Agents in this fleet hand off work, claim exclusive roles, and grant each
other authority to push — a name anyone can assert is not a sound basis for any
of that. This is the one item here that is a correctness problem for
multi-agent operation rather than a convenience.

**5. Expose harness capabilities in the UI, not just the API.** The registry
knows, for all 26 harnesses, whether each supports subagents, resume,
elicitation, streaming and image input — and since this branch the API serves
it. Nothing renders it. A user picking a harness cannot see that
`cursor-native` will not stream, or that eight harnesses declare
`instruction_delivery: not-delivered`, meaning an agent's instructions silently
do nothing. The data is already on the wire; this is a rendering job.

Two things deliberately **not** on this list. Elicitation and
resume-after-restart are marked "not exercised" above rather than proposed as
work: until they are actually tested, any recommendation about them would be a
guess. And the `/tmp` workspace failure (issue #3) is a real bug but a narrow
one — it has an obvious workaround once known, which is why it ranks below
items with no workaround.

## Reproducing

Capability matrix, straight from the registry:

```python
from omnigent import harness_plugins as hp
{h: c.as_dict() for h, c in hp.harness_capabilities().items()}
```

Per-host availability:

```
GET /v1/hosts                                        # configured_harnesses per host
GET /v1/hosts/{host_id}/harnesses/{harness}/model-options
```

The usability tests are plain API calls: `POST /v1/sessions` (with `host_id`
and a workspace under the runner user's home), then `POST
/v1/sessions/{id}/events` for the prompt, `GET /v1/sessions/{id}` to poll, and
`GET /v1/sessions/{id}/stream` for events.
