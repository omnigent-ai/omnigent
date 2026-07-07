# Harness test bench: a standardized capability conformance suite

A pluggable bench that, given a harness, empirically reports its verdict on
every capability dimension in the harness support matrix — "is model switching
available", "is steering possible", "does policy DENY actually block a call" —
instead of a human hand-maintaining a spreadsheet and hoping it still reflects
reality.

> **Status:** shipped and in use. The MVP plus most of phase-2 is on `main` —
> three transport drivers, the six P0 probes, and a capability-derived matrix
> that has already caught and corrected real declaration drift. See
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

Three layers plus a report step.

```
tests/harness_bench/
  profile.py         # BenchProfile: per-harness self-declared facts
  manifest.py        # registry of official BenchProfiles (the spreadsheet as data)
  verdict.py         # Verdict enum, ProbeResult, priority (P0/P1)
  transports/        # transport drivers keyed by class
    _base.py         #   TransportDriver: launch/session/turn against a harness
    sdk_inproc.py    #   in-proc HTTP (reuses existing e2e server helpers)
    tmux_tui.py      #   (phase 2)
    app_server.py    #   (phase 2)
    http_sse.py      #   (phase 2)
  probes/            # one module per dimension
    _base.py         #   CapabilityProbe: name, priority, applies_to, declared(), run()
    basic_turn.py
    streaming.py
    tool_calling.py  #   incl. "connects to Omnigent MCP"
    interrupt.py
    policy_deny.py
    model_override.py
    ...              #   (phase 2: steering, live_queue, resume_fork, elicitation,
                     #    reasoning, images, cost, compaction)
  bench.py           # driver: iterate probes x harnesses -> matrix
  report.py          # render Markdown + JSON, with a DRIFT column
  test_bench.py      # pytest wrapper (parametrized) for CI
```

- **Layer 0 — Profile / manifest.** The spreadsheet, as data. Source of truth
  for the static columns and the *expected* verdicts for behavioral ones.
- **Layer 1 — Offline conformance** (no network, always in CI). Harness
  registers, `create_app()` builds, required routes exist, `Executor` flags are
  internally consistent, a `BenchProfile` exists. Fast, catches structural
  regressions.
- **Layer 2 — Live probes** (gated on CLI + creds; reuses
  `skip_if_harness_cli_missing`). Runs the behavioral table against a live
  server, exactly like the existing e2e tests
  (`/v1/sessions` + `send_user_message_to_session` +
  `poll_session_until_terminal` + `final_assistant_text`).
- **Report.** `python -m tests.harness_bench --harness codex` prints one
  harness's matrix; no filter regenerates the whole sheet with a `DRIFT` column
  diffing declared vs observed.

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
| Basic turn (prereq) | ask model to reply with `<marker>`, assert marker in final text |
| Connects to Omnigent MCP | expose an Omnigent tool, ask model to call it, assert `ToolCallRequest` dispatched through the relay |
| Streaming | count `TextChunk` events: >1 delta = `deltas`, single blob = `complete-only` |
| Model override | launch with a chosen model, assert routing (gateway request / `TurnComplete` usage model); cross-family reject verified via `model_family_mismatch` |
| Policy: DENY | set DENY on a tool, ask model to call it, assert the call is blocked + surfaced |
| Policy: ASK -> Elicitation | set ASK, assert an elicitation event is emitted upstream (web-surfaceable) |
| Interrupt | start a long turn, call `interrupt_session`, assert it stops promptly |
| Live queue (concurrent) | `enqueue_session_message` mid-turn, assert accepted (not rejected) |
| Tool-boundary steer | inject steering text at a tool boundary, assert the next turn reflects it |
| Resume/fork from transcript | run a convo, resume in a fresh session, assert prior context present; fork = branch diverges |
| Compaction | assert `CompactionComplete` surfaced when triggered |
| Reasoning | reasoning-heavy prompt, assert `ReasoningChunk` emitted |
| Images | send an image, assert the model describes it |
| Cost tracking | assert `TurnComplete` carries usage / cost |

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

Behavioral probes run through a **transport driver** resolved from the
harness *family* plus flags: SDK harnesses default to `full-server` (`--fast`
picks `sdk-inproc`), natives use `native-tui`, and `--transport NAME` overrides
the family for any harness. A probe calls
*semantic* methods on the driver (`run_basic_turn`, `run_streaming_turn`,
`run_tool_turn(deny=...)`, `run_interrupt_turn`); the driver owns the
*mechanism* and the probe owns the *interpretation*, so one probe runs across
transports that reach the same capability by different means.

Three drivers exist today (see "Current state" above): `sdk-inproc`,
`full-server`, `native-tui`. Two consequences fall out of this design:

- A dimension is only observable where a driver exercises it. Tool calling and
  Policy DENY need `full-server`; on `sdk-inproc`/`native-tui` they report `·`.
  A `·` therefore often means "this transport can't exercise it here," not "the
  harness lacks it" (see "Which transport exercises which dimension").
- A harness that invents a *novel* transport (neither wrap-subprocess, full
  server, nor native tmux) would degrade its transport-dependent probes to
  `SKIPPED`/`UNKNOWN` until a driver for that class exists.

So "run the bench, see all verdicts, zero code" is true *for any harness
reusing a known transport class*, and honest about the cases where a dimension
or a transport is not yet wired.

## Current state (shipped)

The MVP and most of phase-2 are landed. What exists on `main` today:

- **Layer 0/1/2** — profile/manifest, offline conformance (runs in CI via the
  `misc` pytest group), and the six P0 live probes (basic turn, streaming,
  tool calling, policy DENY, model override, interrupt) with the `DRIFT`
  column.
- **Three transport drivers**, selected by harness *family* with flag overrides:
  - `sdk-inproc` — drives a harness wrap subprocess directly (the four P0 SDK
    harnesses: claude-sdk, codex, pi, openai-agents).
  - `full-server` — a real server + runner; the only transport that exercises
    **Tool calling** and **Policy DENY** as server-dispatched, policy-gated
    calls (SDK harnesses only — it registers via an agent bundle).
  - `native-tui` — a resident vendor CLI in a runner-owned tmux pane, driven
    over the session HTTP surface via a host daemon.

  SDK harnesses default to **`full-server`** — the fullest coverage, and a
  strict superset of what `sdk-inproc` observes (everything sdk-inproc does,
  *plus* Tool calling + Policy DENY). `--fast` opts the SDK family down to
  `sdk-inproc` when you want to skip the server boot (those two dimensions then
  report `·`). Native harnesses have a single transport `--fast` does not touch.
  An explicit `--transport NAME` overrides the family default for any harness
  and is mutually exclusive with `--fast`.
- **Capability-derived matrix** — descriptive columns and declared verdicts
  come from `harness_capabilities()` (the seam; see
  `designs/harness-capabilities-bench-seam.md`), so a harness added to the
  registry — in-repo *or* a community plugin — flows into the bench with no
  bench edit.
- **Native harnesses auto-derived** — every `NATIVE_TUI` harness is registered
  and drivable by name; `native_vendor()` derives what the driver needs from
  capabilities, with no per-vendor table.

### Not yet wired

- **Bench observation of Tool calling / Policy DENY on `native-tui`** — a
  *driver gap, not a native-harness limitation*. Native harnesses do call tools
  and enforce permissions; the bench cannot yet observe it on this transport.
  A native tool call is the vendor's own tool (Bash/Read/...), not a
  server-dispatched `function_call_output` the bench can force, and a native
  deny is a vendor permission decision, not a server-side policy evaluation the
  probe can assert against. So both cells show `·` (not measured), never `✗`.
  Wiring the observation needs new driver work. (SDK harnesses get these via
  `full-server`.)
- **P1 dimensions** — steering, live-queue, resume/fork, elicitation ASK,
  reasoning, images, cost, compaction. Probes not written yet (report
  `UNKNOWN`).
- **Server-side native-agent seeding is a hardcoded list** — see the
  plugin-seamlessness note below; this is the main gap between "the bench is
  plugin-ready" and "a plugged-in native harness just works end to end".

## CI integration

- **Every PR:** Layer 1 offline conformance (fast, no network, no creds).
- **Nightly / on-demand:** Layer 2 live probes (real API cost + flake surface),
  gated on CLI + creds, P0 blocking, P1 report-only. Follows the existing
  nightly/flake-stress pattern rather than blocking every PR on live turns.

## Running the bench and reading the result

```
# Offline: the declared matrix, no creds, every harness. Fast.
python -m tests.harness_bench

# Live: probe one harness against a gateway profile.
python -m tests.harness_bench --harness codex-native --profile oss

# Live: probe every official harness (SDK + native) sequentially.
python -m tests.harness_bench --profile oss

# A community harness that ships its own BenchProfile.
python -m tests.harness_bench --harness mypkg.harness:PROFILE --profile oss
```

**You do not need to live-probe every harness on every host — and you cannot.**
Each native harness needs its own vendor CLI logged in (a login the bench
cannot provision), so no single host has them all. The two layers split the
work:

- **Offline conformance** already covers every harness in CI — registration,
  the declared matrix, capability derivation. No host access needed.
- **Live probes** only answer "does observed behavior match the declaration?"
  You get value from live-probing a harness where the declaration is unverified
  or might be wrong — not from chasing 100% coverage on one box.

Run the full set on whatever host you have (`--profile oss`); harnesses whose
vendor CLI is absent or logged out **skip cleanly** (they do not fail or abort
the run). Read two signals only: any `!!` DRIFT, and any harness you *can* run
that shows an unexpected `✗` / `·`. A single live run is a spot-check, not a
gate — live probes are non-deterministic (model behavior, timing), so re-run
before treating one `·`/timeout as a regression. Drift coverage is cumulative:
each host that has harness X logged in contributes a live check for X.

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

Not every dimension is observable on every transport, so a `·` (SKIPPED) in a
run always means "the bench did not measure this here," never "the harness
lacks it." Two dimensions in particular only get a real verdict on the
`full-server` transport:

| Dimension | sdk-inproc (`--fast`) | full-server (default) | native-tui |
|---|---|---|---|
| Basic turn, Streaming, Model override, Interrupt | ✓ | ✓ | ✓ |
| **Tool calling** | · (harness dispatches tools internally) | ✓ (server-dispatched builtin) | · (bench can't observe vendor tools yet) |
| **Policy DENY** | · (wrap-direct: no tool-call policy hook) | ✓ (spec-baked deny, enforced) | · (bench can't observe vendor deny yet) |

The `native-tui` `·` is a *bench observation gap, not a native-harness
limitation*: native harnesses do call tools and enforce permissions, but a
native tool call is the vendor's own (Bash/Read/...) and a native deny is a
vendor permission decision, neither of which is the server-dispatched,
policy-gated call the probe watches for. Giving those cells a real verdict
needs new driver work, not a change to the harnesses.

Because `full-server` sees everything `sdk-inproc` does *plus* these two, it is
the **default** for SDK harnesses — a plain live run proves Tool calling and
Policy DENY out of the box:

```
python -m tests.harness_bench --harness claude-sdk --profile oss
```

Live-verified: `claude-sdk` completes the full matrix on `full-server` —
Tool calling `✓` and Policy DENY `✓` (the deny is delivered and the blocked
call does not stall the turn). Add `--fast` to trade that coverage for a quicker
run on `sdk-inproc`; those two columns then show `·`, since neither `sdk-inproc`
nor `native-tui` (for natives) routes a tool call through a server policy
evaluation.

`full-server` covers **SDK harnesses only** — it registers the harness via an
agent bundle, which is the SDK-wrap path; native harnesses need the host-daemon
provisioning the `native-tui` driver owns. So Tool calling / Policy DENY on
native harnesses are not observed by *any* transport yet — a bench follow-up,
not a native-harness gap — distinct from the `--fast` (sdk-inproc) `·`, which
is a transport limitation the default `full-server` run already answers for SDK
harnesses.

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

- **Registry-driven native-agent seeding** (highest leverage) — replace the
  hardcoded `_ensure_default_*_agent()` list in `server/app.py` with a loop over
  `native_agents()`, so any native harness (in-repo or plugin) registers
  automatically. This is the fix for the plugin-seamlessness seam above.
- **Bench observation of Tool calling / Policy DENY on `native-tui`** — a
  driver gap, not a native-harness limitation: native harnesses call tools and
  enforce permissions, but a native tool call is the vendor's own and a native
  deny is a vendor permission decision, not the server-dispatched
  `function_call_output` the probe watches for. The cells show `·` (not
  measured), never `✗`. Needs new driver work. (SDK harnesses get these via
  `full-server`.)
- **Per-harness native provisioning gaps** the bench has surfaced but not yet
  resolved: goose-native returns a 500 on the terminal-ensure endpoint;
  hermes-native's forwarder does not wire up (a lazy-chat / first-turn gate to
  confirm); kimi-native and own-auth natives need a vendor provider setup the
  bench cannot provision (kimi in particular has no gateway path — it routes
  via `kimi provider add`, out of band).
- **P1 dimensions + their probes** — steering, live-queue, resume/fork,
  elicitation ASK, reasoning, images, cost, compaction.
- Exact `BenchProfile` field set and whether it subsumes `HarnessProbe` or wraps
  it; whether the manifest fully retires the spreadsheet or diffs against an
  exported CSV during transition.
