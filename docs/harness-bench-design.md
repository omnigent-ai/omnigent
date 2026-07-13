# Harness test bench: a standardized capability conformance suite

A pluggable bench that, given a harness, empirically reports its verdict on
every capability dimension in the harness support matrix — "is model switching
available", "is steering possible", "does policy DENY actually block a call" —
instead of a human hand-maintaining a spreadsheet and hoping it still reflects
reality.

> **Status:** shipped and in use. The bench on `main` has three transport
> drivers, six P0 probes, five report-only P1 probes, automatic live/offline
> selection, and a capability-derived matrix that has already caught and
> corrected real declaration drift. See
> [Current state](#current-state-shipped) for what is live vs. still open. The
> sections before it describe the design and the decisions behind it.

## Motivation

We maintain a capability matrix by hand (the native + SDK support
spreadsheet). It drifts the moment a harness changes and nobody re-tests every
cell. Worse, there are already **three disagreeing sources of truth** for any
given capability:

1. **The spreadsheet** — what a human believed at some point.
2. **The `Executor` capability flags** — `supports_streaming()`,
   `supports_live_message_queue()`, `supports_tool_boundary_interrupt()`,
   `supports_stepwise_internal_turns()`, `handles_tools_internally()` in
   `omnigent/inner/executor.py:541`.
3. **`omnigent/model_override.py`** — already encodes per-harness facts
   declaratively (`_SDK_MODEL_OVERRIDE_HARNESSES`, the `_*_FAMILY_HARNESSES`
   sets, single- vs multi-model rules).

The bench turns the matrix into an **executable conformance suite** that earns
each cell by running a live turn and inspecting the event stream, and then
**reconciles observed behavior against the declared flags** — so a flag that
says `✓` but behaves `✗` becomes a test failure (a `DRIFT` verdict), not a
production surprise.

## Goals

- One command produces the support matrix for a harness, with a verdict per
  dimension.
- Adding a new *official* harness needs at most a one-line registry entry plus a
  self-declared profile — never per-probe code.
- A *community* / out-of-repo harness that ships a bench profile can be probed
  with `--harness <name>` and no bench edits.
- Detect drift between what a harness *declares* and what it *does*.

## Non-goals

- Not a replacement for the existing per-harness e2e tests (those assert
  specific behaviors deeply; the bench asserts breadth across dimensions).
- Not a performance/latency benchmark. "Bench" here means conformance, not
  throughput.
- The bench does not invent model ids, credentials, or transports. Facts it
  cannot infer must be self-declared by the harness (see `BenchProfile`).

## Key constraint: registration is a hardcoded dict today

Harnesses register via a literal `_HARNESS_MODULES: dict[str, str]` mapping
harness name to module path in `omnigent/runtime/harnesses/__init__.py:34`.
There is **no entry-point / plugin discovery** mechanism. An out-of-repo harness
cannot even register without editing that file — the same shared-file conflict
pain tracked in #899, whose proposed fix was per-harness self-registration.

This constraint is what shapes the coupling decision below. It is *not* a limit
on what the bench can probe: the probes are harness-agnostic. It is only a limit
on how a harness gets *discovered*.

> **Update since this was written:** entry-point plugin discovery now exists —
> `harness_capabilities()` merges contributions from the
> `omnigent.community.harness` entry-point group, and the bench derives
> everything from it. So the bench side of option B is realized: a plugin's
> harness flows in with no bench edit. The remaining hardcoded seam is *not*
> here — it is the server's native-agent seeding (see "Plugin seamlessness").

## Decision: option B (registry-indexed now, profile-driven from day one)

Two coupling options were considered:

- **(A)** Build the bench on dynamic discovery (entry points / plugin registry)
  now. True plug-and-play, but partly blocked on self-registration (#899) before
  out-of-repo harnesses can register at all.
- **(B)** Index official harnesses from the current hardcoded registry now
  (one registry line + a profile per new harness), and swap enumeration to
  dynamic discovery when self-registration lands.

**We chose B**, with the critical rule that **all per-harness facts live on a
self-declared `BenchProfile` from day one** — never in bench or probe code. The
hardcoded list is just a convenience index of the official harnesses; it is not
a gate on what the bench can probe.

This satisfies both use cases:

- **Official harnesses** — already in `_HARNESS_MODULES`; the bench iterates the
  list and `--harness <name>` selects one.
- **Community harnesses** — `--harness <name>` resolution falls back (when the
  name is not in the registry) to any harness that exposes a `BenchProfile`
  (e.g. `--harness mypkg.myharness`, or a name resolved via an installed
  plugin). A ~10-line resolution shim, not the full discovery system.

The day self-registration lands, only the *enumeration* changes from "read the
list" to "discover"; probes, profiles, and reports are untouched.

## What is free vs. what a harness must provide

**Free (no per-harness code):**

- Every **behavioral probe** — it creates a session, runs a turn, and inspects
  the generic event stream (`TextChunk`, `ReasoningChunk`, `ToolCallRequest`,
  `TurnComplete`, elicitation events). It never names a harness. Any harness the
  bench can launch is probed on every dimension; a dimension with no probe yet
  reports `UNKNOWN`.
- **Declared-flag reconciliation** — reads the `Executor` capability methods
  that every harness inherits from the base class.

**Must be self-declared on `BenchProfile` (the bench cannot infer these):**

- A **test model** (or model family) — a probe cannot invent a valid model id.
- The **CLI binary** to skip-gate on (for subprocess / native harnesses).
- The **transport class** (see transport drivers below).
- The **static columns** the matrix records but cannot verify: Owner, Auth
  method, Implementation, "inherits preexisting configs", priority tier.

**Derived by convention (not hand-authored):** `env_prefix`
(`HARNESS_<NAME>_`), `marker` (`<NAME>_BENCH_OK`).

## Architecture

The implementation has three layers plus reporting:

```
tests/harness_bench/
  profile.py             # BenchProfile and profile-name resolution
  manifest.py            # official profiles derived from capabilities + e2e metadata
  verdict.py             # verdict vocabulary, priority, and drift reconciliation
  transport.py           # semantic Driver protocol and transport resolution
  driver.py              # sdk-inproc driver + shared TurnResult/usage helpers
  full_server.py         # shared server/runner lifecycle and registration
  full_server_driver.py  # full-server driver and session polling
  native_tui_driver.py   # native vendor CLI + host-daemon/tmux driver
  session_items.py       # shared session-item envelope parsing
  runtime_env.py         # config/credential resolution matching `omni run`
  probes/                # one module per capability dimension
  events.py              # structured progress events and plain sink
  richreport.py          # optional live Rich matrix
  bench.py               # orchestration, concurrency, and shared-server wiring
  report.py              # terminal, Markdown, and JSON rendering
```

Reusable configuration and runtime primitives live in production modules such
as `omnigent.config`, `find_free_port`, and the harness registry rather than
being reimplemented under tests.

- **Layer 0 — Profile / manifest.** Static facts and declared verdicts are
  derived from `harness_capabilities()` plus the existing e2e harness metadata.
- **Layer 1 — Offline conformance.** No network or credentials. It validates
  registration, profile shape, capability derivation, transport resolution,
  rendering, and orchestration behavior in normal CI.
- **Layer 2 — Live probes.** Drivers execute behavioral probes through the
  wrap boundary or the real server/runner session API. Missing credentials,
  vendor binaries, or vendor login produce capability-neutral skips.
- **Report.** The CLI renders the declared matrix offline or reconciles live
  observations into terminal, Markdown, and JSON reports. `DRIFT` produces a
  non-zero exit status.

### Build on `HarnessProbe`, don't reinvent it

`tests/e2e/_harness_probes.py` already gives per-harness rows
(name, model, env_prefix, marker, cli_binary) and CLI-gating that every e2e test
parametrizes over. `BenchProfile` should extend / subsume that row so adding a
harness there flows into both the existing e2e suite and the bench.

## Verdict vocabulary

Maps directly to the spreadsheet glyphs, plus two operational states and the
drift alarm.

| Verdict | Glyph | Meaning |
|---|---|---|
| `SUPPORTED` | ✓ | probe ran, behavior confirmed |
| `UNSUPPORTED` | ✗ | probe ran, capability absent (and expected absent) |
| `PARTIAL` | ~ | works with caveats (e.g. "TUI-only", "hook-DENY only") |
| `NOT_APPLICABLE` | — | dimension does not apply (e.g. model override on agy self-select) |
| `UNKNOWN` | ? | never probed / no probe written yet |
| `SKIPPED` | | CLI / creds / transport unavailable in this environment |
| `DRIFT` | !! | observed verdict disagrees with the declared flag / manifest |

Each dimension also carries a `P0` / `P1` priority (from the spreadsheet) so CI
can gate on P0 and merely report P1.

## Dimension catalog

Two classes.

### Static / declared (recorded, not probed)

Validated for presence and shape only: `Owner`, `Transport`, `Implementation`,
`Auth` method, `Inherits preexisting configs`.

### Behavioral (proven by a live turn)

| Dimension | How the probe proves it |
|---|---|
| Basic turn (P0 prerequisite) | complete a marker-echo turn and require assistant text |
| Fork replay (P1) | clone the session after Basic turn, require copied marker history, and require the clone to recall it |
| Streaming (P0) | count output-text deltas; repeated single-delta output is `PARTIAL` |
| Tool calling (P0) | provoke the transport's tool mechanism and require a surfaced call |
| Omnigent MCP (P1, native only) | call read-only `sys_session_list` through the generated `omnigent` MCP relay and require a matching function-call item |
| Policy DENY (P0) | apply a tool-call deny and require a blocked-call signal |
| Policy ALLOW (P1) | attach an explicit allow and require a non-blocked tool output; native hooks expose no positive ALLOW event |
| Policy ASK (P1) | apply ask and require an elicitation/approval request |
| Model override (P0) | validate the requested harness/model pair and complete a turn |
| Cost tracking (P1) | read priced cost or token usage from the turn/session |
| Interrupt (P0) | interrupt a long turn and require cancellation or early termination |

Planned dimensions are steering, live queue, resume, reasoning, images,
and compaction.

Every behavioral probe also reads the corresponding declared flag and returns
`DRIFT` when observed disagrees with declared.

### Illustrative probe shape

```python
class StreamingProbe(CapabilityProbe):
    name = "streaming"
    priority = P0
    applies_to = BOTH

    def declared(self, profile) -> Verdict:
        return SUPPORTED if executor_of(profile).supports_streaming() else UNSUPPORTED

    async def run(self, driver, profile) -> ProbeResult:
        deltas = await driver.count_text_chunks("Write a 3-sentence story.")
        observed = SUPPORTED if deltas > 1 else PARTIAL  # "complete-only"
        return ProbeResult(
            observed,
            note=f"{deltas} text chunks",
            drift=reconcile(observed, self.declared(profile)),
        )
```

## Transport drivers: the real ceiling on "all dimensions"

Behavioral probes call semantic driver methods such as `run_basic_turn`,
`run_tool_turn`, `run_policy_turn`, and `run_interrupt_turn`. Drivers own the
transport-specific mechanism; probes interpret a common `TurnResult`.

Three drivers exist:

- `full-server` is the SDK-family default. It drives a real server and runner,
  uses a server-dispatched builtin for tool probes, and observes fixed
  ALLOW/ASK/DENY policies.
- `native-tui` drives a resident vendor CLI in a runner-owned tmux pane through
  the server session API. It observes vendor tool calls and tool-call DENY via
  the native policy hook. ALLOW/ASK are not yet implemented.
- `sdk-inproc` drives the harness wrap directly. It is selected by `--fast` and
  provides cheaper wrap-level coverage, but no server-side policy surface.

A `SKIPPED` verdict therefore means the behavior was not measurable in that
transport or environment, not that the harness lacks the capability. A novel
transport class still requires a driver, but harnesses reusing one of these
families flow through the existing probes without per-harness probe code.

## Current state (shipped)

The bench on `main` includes:

- **Six P0 probes:** Basic turn, Streaming, Tool calling, Policy DENY, Model
  override, and Interrupt.
- **Five P1 probes:** Fork replay, Omnigent MCP, Policy ALLOW, Policy ASK, and Cost tracking. P1 verdicts
  are report-only and do not gate the same way as P0 declarations.
- **Three transport drivers:** `full-server`, `native-tui`, and `sdk-inproc`,
  selected by harness family with `--transport` and `--fast` overrides.
- **Automatic live selection:** without an explicit mode, the CLI runs live
  when credentials are resolvable and otherwise renders the declared matrix.
  `--live` and `--no-live` force either mode. Credentials are derived like
  `omni run`; `--profile` is only an override.
- **Concurrent execution and shared infrastructure:** `--jobs` runs harnesses
  concurrently while preserving report order, and full-server harnesses share
  one server/runner pair within a run.
- **Structured progress and reports:** plain or Rich live progress plus terminal,
  Markdown, JSON, and optional report-file output.
- **Capability-derived registration:** official SDK and native profiles derive
  from `harness_capabilities()` and existing e2e metadata. Session-item parsing,
  config loading, free-port selection, and polling helpers are shared rather
  than duplicated.

### Not yet wired

- Registry-driven server seeding for community native UI agents.
- Steering, live queue, resume, reasoning, images, and compaction probes.
- Automatic provisioning of vendor login/provider configuration for native
  harnesses; unavailable environments skip cleanly.

## CI integration

- **Every PR:** Layer 1 offline conformance (fast, no network, no creds).
- **Nightly / on-demand:** Layer 2 live probes (real API cost + flake surface),
  gated on CLI + creds, P0 blocking, P1 report-only. Follows the existing
  nightly/flake-stress pattern rather than blocking every PR on live turns.

## Running the bench and reading the result

```
# Declared matrix only, with no credentials.
python -m tests.harness_bench --no-live

# Auto-live when configured or ambient credentials are available.
python -m tests.harness_bench --harness codex

# Force a named profile and probe several harnesses concurrently.
python -m tests.harness_bench --profile oss --jobs 4 --rich

# A community harness that ships its own BenchProfile.
python -m tests.harness_bench --harness mypkg.harness:PROFILE --live
```

Without `--live` or `--no-live`, resolvable credentials select live mode and
missing credentials select the offline declared matrix. Native harnesses also
need their vendor CLI installed and logged in; the bench cannot provision those
accounts, so unavailable harnesses skip without aborting the run.

Offline conformance covers every registered harness in CI. Live runs are
spot-checks of observed behavior and can vary with model behavior and timing;
re-run an isolated timeout or skip before treating it as a regression. The
signals that matter most are `DRIFT` and repeatable unexpected
`UNSUPPORTED`/`PARTIAL` verdicts on a runnable harness.

## Streaming is a binary declared capability

A recurring subtlety worth stating: the `streaming` capability is **binary** —
a harness either forwards token-level deltas (`SUPPORTED`) or it does not
(`UNSUPPORTED`). `PARTIAL` is a *probe observation only*: the streaming probe
returns it for the ambiguous coalesced-single-delta case against a `SUPPORTED`
declaration. It is **never a declared value**. Declaring a non-streaming
harness as `PARTIAL` drifts against reality, because the probe reports zero
deltas as `UNSUPPORTED`, not `PARTIAL`.

**Declare `streaming=False` only from a live observation of 0 deltas** — a
static "the forwarder posts no delta" grep is *not* sufficient. That grep once
flipped seven natives to `False` in one batch; a live run then showed
pi-native streams (7 deltas) despite having no delta-posting forwarder, so the
flip was reverted. Only three natives are declared non-streaming today, each
live-verified at 0 deltas: **kiro-native, cursor-native, qwen-native**. The
rest default to `streaming=True` (the honest default: if one turns out not to
stream, the bench flags a real drift on the next run, rather than a false
`False` that silently drifts the moment the harness *does* stream).

## Which transport exercises which dimension

| Dimension | `sdk-inproc` (`--fast`) | `full-server` (SDK default) | `native-tui` |
|---|---|---|---|
| Basic turn, Streaming, Model override, Interrupt | Wrap-level observation | End-to-end server/runner observation | End-to-end server/runner/vendor observation |
| Fork replay | Not observable | Clone + copied-history replay through server/runner | Clone + copied-history replay through server/runner/vendor |
| Tool calling | Request-level wrap tool | Server-dispatched builtin | Vendor tool mirrored into session items |
| Omnigent MCP | Not applicable | Not applicable | Generated `omnigent` MCP relay when supported by the vendor |
| Policy DENY | Not observable | Fixed policy blocks the builtin | Session CEL policy triggers the native policy hook |
| Policy ALLOW / ASK | Not observable | Fixed policy; ASK observes and resolves an elicitation | Temporary session CEL policy; ASK observes and resolves an elicitation |
| Cost tracking | Completed-response usage when forwarded | Session snapshot usage/cost | Session snapshot when the vendor forwards usage |

`full-server` remains the SDK default because it covers the deployed server
path and all three policy actions. `--fast` trades that policy coverage for
lower startup cost. `native-tui` now has real Tool calling and all three policy
action probes through the native hook path.

## Plugin seamlessness: where it is and isn't

The original goal (option B) was that a *community* harness ships a
`BenchProfile` and runs with `--harness <name>` and no bench edits. For the
**bench itself, that holds**: profile resolution, capability derivation, and
`native_vendor()` all read `harness_capabilities()`, which discovers community
plugins via entry points. A plugged-in harness needs zero bench code to be
recognized.

The seam is **one level down, in the omnigent server**. A native harness is
only drivable once the server has seeded a built-in `<harness>-native-ui`
agent, and that seeding is a **hardcoded list** in
`server/app.py:_ensure_default_agents` — one `_ensure_default_<harness>_agent()`
call per harness. goose-native and hermes-native were in the capability
registry but omitted from that list, so the bench (correctly) reported them
`not auto-registered on the server` until the seeders were added.

So: **the bench is plugin-seamless; the server's native-agent seeding is not,
and the bench inherits that seam.** A community native plugin today resolves in
the bench, then fails at registration because nothing seeds its UI agent. The
clean fix is to make `_ensure_default_agents` iterate `native_agents()` from
the registry (which already includes plugins) instead of a hardcoded call list
— then native harnesses and plugins register automatically. This is the highest
-leverage remaining item: it is the difference between "the bench is plugin-
ready" and "a plugged-in native harness works end to end".

## The self-enforcing table in practice (drift case studies)

`reconcile()` turns a false capability declaration into a `DRIFT`. This is not
theoretical — the bench caught several real declaration errors this way, each
resolved by correcting the *source* (the capability model), not the bench:

- **kiro-native / streaming.** Declared `SUPPORTED`, observed 0 deltas
  (`!!✓>✗`). kiro mirrors each complete assistant message rather than streaming
  tokens. Corrected to `streaming=False`.
- **pi-native / streaming (a fixed over-correction).** A static grep had flipped
  pi to `False`; a live run showed it streams 7 deltas (`!!✗>✓`) despite having
  no delta-posting forwarder. Reverted to `True`. This is why the rule is
  "declare `False` only from a live 0-delta observation" — the grep lied.
- **cursor-native / streaming + provisioning.** cursor could not provision at
  all until the `lazy_chat` fix (its `external_session_id` is created by the
  first message, not at launch, so gating on it pre-turn deadlocked). Once
  runnable, it observed 0 deltas → `streaming=False`.
- **qwen-native / streaming.** Observed 0 deltas → `streaming=False`.

The pattern each time: the bench detects the mismatch, a live probe pins which
side is wrong, and the capability model is corrected — not the bench massaged to
agree with it.

## Open items

- **Declarative native tool-relay mechanism** — extend the harness capability
  model to distinguish generated MCP, native registration, and no relay. Derive
  the Omnigent MCP probe's applicability from that declaration instead of the
  bench's temporary `_NATIVE_OMNIGENT_MCP_HARNESSES` list.
- **Registry-driven native-agent seeding** — replace the hardcoded server
  seeding list with registry iteration so community native harnesses work end
  to end after plugin installation.
- **Per-harness native provisioning** — some vendors require login or provider
  configuration that the bench deliberately cannot create. Improve diagnostics
  where possible while retaining clean skips.
- **Additional dimensions** — steering, live queue, resume, reasoning,
  images, and compaction.
