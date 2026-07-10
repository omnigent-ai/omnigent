# Harness test bench

A standardized, pluggable conformance suite that probes a harness and
reports a verdict per capability dimension, reconciling observed behavior
against a self-declared profile to surface drift. Design and rationale:
[`docs/harness-bench-design.md`](../../docs/harness-bench-design.md).

## Run it

```bash
# List official harnesses (name, resolved transport, model).
python -m tests.harness_bench --list

# Offline (declared) matrix -- no turns, no creds.
python -m tests.harness_bench

# Live probe one harness against a gateway profile.
python -m tests.harness_bench --harness codex --profile my-profile

# Live probe every official harness, several at a time, with a live table.
python -m tests.harness_bench --profile my-profile --jobs 4 --rich
```

A non-zero exit means a `DRIFT` cell was found (observed behavior disagrees
with the declared matrix).

### Flags

- `--profile NAME` -- Databricks gateway profile. Enables the live layer;
  without it the bench renders the declared matrix offline.
- `--harness NAME` -- probe one harness (repeatable). An official name, or a
  `module:attr` / `module.ATTR` reference to a community `BenchProfile`.
  Defaults to every official harness.
- `--fast` -- run SDK harnesses on `sdk-inproc` instead of the `full-server`
  default: skips the server boot for a quicker run, at the cost of the Tool
  calling + Policy DENY dimensions (they report `·`). No effect on natives.
  Mutually exclusive with `--transport`.
- `--transport NAME` -- force a specific transport driver (`sdk-inproc`,
  `full-server`, `native-tui`), overriding the family default.
- `--jobs N` / `-j N` -- run up to N harnesses concurrently (default 1).
  Probes within a harness stay sequential; 3-4 is a reasonable ceiling on one
  host. Report order always matches input order.
- `--rich` / `--no-rich` -- force / disable the live progress table (auto: rich
  on a TTY, plain per-line output otherwise).
- `--report PATH` -- also write the final matrix to PATH; format follows
  `--json` / `--markdown`, else inferred from the extension.

### Output formats (mutually exclusive)

- default: an aligned, ANSI-colored terminal table (color auto-disables when
  piped or with `--no-color`), followed by a Notes section explaining every
  non-supported cell so a `·` is never opaque.
- `--markdown`: the GitHub-flavored table for docs / PRs.
- `--json`: machine-readable, for diffing runs or regenerating docs.

Each row is labelled with the transport that actually ran it, e.g.
`claude-sdk [full-server]`, `kimi-native [native]`.

Under `--rich`, the live table (on stderr) already shows the grid, so the
stdout report drops the grid and prints only the legend + notes -- no duplicate
table. When stdout is redirected to a file, the report keeps the full grid so
the file is self-contained.

### Example output

A live `--rich` run of the four SDK harnesses on the `oss` profile:

```console
$ uv run --no-sync python -m tests.harness_bench --profile oss --rich \
    --harness claude-sdk --harness codex --harness pi --harness openai-agents

                              Harness capability matrix (live)
┌───────────────────────────┬────────────┬───────────┬──────────────┬─────────────┬────────────────┬───────────┐
│ Harness                   │ Basic turn │ Streaming │ Tool calling │ Policy DENY │ Model override │ Interrupt │
├───────────────────────────┼────────────┼───────────┼──────────────┼─────────────┼────────────────┼───────────┤
│ claude-sdk  [full-server] │     ✓      │     ✓     │      ✓       │     ✓       │       ✓        │     ✓     │
│ codex       [full-server] │     ✓      │     ✓     │      ✓       │     ·       │       ✓        │     ✓     │
│ pi          [full-server] │     ✓      │     ✓     │      ✓       │     ✓       │       ✓        │     ✓     │
│ openai-agents [full-server] │   ✓      │     ✓     │      ✓       │     ✓       │       ✓        │     ✓     │
└───────────────────────────┴────────────┴───────────┴──────────────┴─────────────┴────────────────┴───────────┘
Legend: `✓` SUPPORTED · `~` PARTIAL · `✗` UNSUPPORTED · `—` NOT_APPLICABLE · `?` UNKNOWN · `·` SKIPPED · `!!` DRIFT

Notes:
- codex / Policy DENY: · model never attempted the tool; tool-call DENY path not exercised
```

The `·` in codex / Policy DENY is a clean SKIP, not a failure: the model simply
never reached for the gated tool, so the deny path had nothing to block (see the
Notes line). Every non-`✓` cell gets a matching Notes entry.

## Transport selection

A profile's `transport` field is the harness *family* marker, not the literal
driver the run uses:

- **SDK-family** harnesses default to **`full-server`** -- the fullest
  coverage, and a superset of what `sdk-inproc` observes: it drives every
  dimension through the real server API the web UI uses, and adds Policy DENY
  (a server-dispatched builtin under a spec-baked deny policy), which the
  wrap-direct `sdk-inproc` path cannot see. `--fast` opts them down to
  `sdk-inproc` (Policy DENY then reports `·`; see "What a ✓ actually means").
- **native** harnesses use `native-tui` (a resident vendor CLI in a
  runner-owned tmux pane); `--fast` does not apply.
- `--transport NAME` overrides the family default for any harness.

## What it reports (dimensions)

Each probe drives one real turn and watches what the harness does:

| Probe | In plain terms |
| --- | --- |
| **Basic turn** | Can it hold a conversation at all? Asks it to echo a marker and checks the marker comes back -- the "is it alive" test. |
| **Streaming** | Does the answer arrive incrementally (word-by-word) or all at once at the end? Counts the token-level chunks. |
| **Tool calling** | Can it use a tool (run a command, read a file) mid-answer, not just chat? |
| **Policy DENY** | If a policy says "block that tool", does the harness actually enforce it? |
| **Policy ALLOW** | If a policy explicitly allows the tool, does the call actually go through (not just "wasn't blocked")? |
| **Policy ASK** | If a policy says "ask first", does the tool call pause for an approval prompt (an elicitation) the way the web UI would show? |
| **Model override** | If you ask for a specific model, does it actually run that one? |
| **Cost tracking** | Does a completed turn report what it spent? `✓` a priced USD cost, `~` tokens only (unpriced model), `·` no usage surfaced. Gates any future cost *policy* -- a cost budget is a no-op without this. |
| **Interrupt** | If you stop it mid-answer, does it actually stop? |

Each cell is a verdict: **✓** works, **✗** doesn't, **·** couldn't be tested
here (see below), **!!** the harness *declared* it works but the probe observed
otherwise (drift).

## What a ✓ actually means (read before trusting a green cell)

A ✓ is only as strong as the *layer the probe drove it through*, and that layer
depends on the transport. There are three:

- **`full-server`** (SDK-family default) and **`native-tui`** (native default)
  both drive a turn through a **real Omnigent server + runner over the same HTTP
  API the web UI uses** -- `POST /v1/sessions/{id}/events` to send and
  `GET /v1/sessions/{id}/stream` (SSE) to observe. So a ✓ here means the
  capability works end-to-end through the **exact server contract the browser
  depends on** -- real server, real runner, real harness subprocess.
- **`sdk-inproc`** (only under `--fast`) drives the **harness *wrap* subprocess
  directly**, bypassing the server + runner. A ✓ here means "the harness wrap
  can do it", *not* "it works through the deployed server path the web UI hits".
  This is the documented trade-off of `--fast`, and why full-server is the SDK
  default.

**The one caveat that applies to every transport:** the bench is a *headless
HTTP client* of that API. It validates the server-side contract the web UI
consumes (endpoints accept the turn, the harness runs it, the right SSE events
flow) -- it does **not** run the browser / React layer, so it cannot catch a UI
that mis-*renders* a correct event. For "does the pixel show up", that is the
Playwright `tests/e2e_ui/` suite, not this bench.

### Per-dimension, per-transport: what a ✓ verifies

Rows are the dimensions; columns are the three transports. "server API"
(full-server / native-tui) means the ✓ was observed over the same
`/v1/sessions/...` surface the web UI uses; "wrap" (sdk-inproc, `--fast`) means
it was observed at the harness-wrap boundary, below the server.

| Dimension | `full-server` (SDK default) | `native-tui` (native default) | `sdk-inproc` (`--fast`) |
| --- | --- | --- | --- |
| **Basic turn** | ✓ = turn completes over the server API | ✓ = turn completes over the server API | ✓ = turn completes at the wrap (no server) |
| **Streaming** | ✓ = token deltas seen on the server SSE stream | ✓ = token deltas seen on the server SSE stream | ✓ = deltas seen on the wrap SSE (no server) |
| **Tool calling** | ✓ = a server-dispatched builtin call was made + turn closed | ✓ = the vendor's own tool call surfaced as a server `function_call` item | ✓ = a request-level tool call surfaced at the wrap |
| **Policy DENY** | ✓ = a spec-baked `tool_call` deny policy blocked the call | ✓ = a session-attached CEL deny fired `response.policy_denied` | `·` = the wrap path has no server-side policy hook |
| **Policy ALLOW** | ✓ = a spec-baked `allow` policy let the call proceed (non-blocked output) | `·` = CEL allow/ask attach is a follow-up | `·` = no server-side policy hook |
| **Policy ASK** | ✓ = a spec-baked `ask` policy raised `response.elicitation_request` (then resolved) | `·` = follow-up | `·` = no elicitation surface |
| **Model override** | ✓ = the requested model was the one used | ✓ = the requested model was the one used | ✓ = the requested model was the one used |
| **Cost tracking** | ✓/~ = usage read from the session snapshot (`total_cost_usd` / `last_total_tokens`) | ✓/~ = usage from the snapshot (native `session.usage`) | ✓/~ if the wrap forwards `usage` on `response.completed`, else `·` |
| **Interrupt** | ✓ = a mid-turn cancel stopped the turn (server marker) | ✓ = a mid-turn cancel surfaced `session.interrupted` | ✓ = a mid-turn cancel stopped the wrap turn |

So for the harnesses users actually reach through the web UI (SDK on
`full-server`, natives on `native-tui`), **a ✓ does mean the capability works
end-to-end through the server API the UI relies on** -- minus the browser render
layer. The cells that read `·` purely for a transport reason are the policy
verdicts where a transport has no server-side policy surface: Policy DENY under
`--fast`, and Policy ALLOW / ASK anywhere but `full-server` (their `native-tui`
attach is a follow-up). On the default SDK run (`full-server`) all three policy
verdicts are real `✓`.

### Why a `·` appears

A `·` always means "the bench did not measure this *here*", never "the harness
lacks it". Two causes:

- **Transport can't observe it** -- Policy DENY under `--fast` (the wrap path has
  no server-side policy hook). Run without `--fast` for a real verdict.
- **The run diagnosed a clean skip** -- e.g. the model never reached for the
  offered tool (Tool calling / Policy DENY can't be judged if no call happened),
  a vendor CLI is not installed / not logged in, or creds could not be resolved.
  Every `·` gets a matching Notes line explaining which.

## Layout

| File | Role |
| --- | --- |
| `verdict.py` | `Verdict` / `Priority` / `ProbeResult` and the `reconcile` drift check |
| `profile.py` | `BenchProfile` (per-harness self-declaration) + name resolution |
| `manifest.py` | Official profiles, derived from the capability model + `tests/e2e/_harness_probes.py` |
| `transport.py` | `Driver` protocol, driver registry, family/flag transport resolution |
| `driver.py` | `SdkInprocDriver` (harness wrap over SSE) + `ProvisioningError` |
| `full_server.py` | `SharedFullServer` -- real server+runner lifecycle + agent/session registration |
| `full_server_driver.py` | `FullServerDriver` -- runs probes against a (owned or shared) full server |
| `native_tui_driver.py` | `NativeTuiDriver` -- vendor CLI in tmux via a host daemon; native vendor auto-derivation |
| `probes/` | One module per dimension; `ALL_PROBES` is the registry |
| `events.py` | Structured `BenchEvent`s + `ProgressSink` / `LineSink` |
| `richreport.py` | `rich.Live` progress table (optional-dep; falls back to `LineSink`) |
| `bench.py` | Orchestrator: probes x harnesses -> `BenchMatrix`, `--jobs`, shared-server wiring |
| `report.py` | Terminal / Markdown / JSON renderers |
| `test_bench.py` | Offline conformance (always) + live layer (gated on `--profile`) |

## Add a harness

- **Official SDK:** add a `BenchProfile` to `manifest.py` (base fields come
  from `_harness_probes.HARNESS_PROBES`). No probe or driver edits.
- **Native:** nothing to add -- every harness the capability model marks
  `NATIVE_TUI` (in-repo or a community plugin) is auto-derived into the matrix
  and drivable by name; `native_vendor()` derives what the driver needs.
- **Community / out-of-repo:** ship a `BenchProfile` and select it by
  reference: `--harness mypkg.harness:PROFILE`. No bench edits.

## Add a dimension

Add a `CapabilityProbe` subclass under `probes/`, list it in
`probes/__init__.py:ALL_PROBES`, and add its declared verdict to the profiles
(or derive it from the capability model in `manifest.py`). Probes are
harness-agnostic -- they only call the driver's semantic methods.

## Scope

Live today: the dimensions in the table above (including `cost_tracking` and
Policy ALLOW / ASK on `full-server`); all three transports (`sdk-inproc`,
`full-server`, `native-tui`); the four official SDK harnesses (claude-sdk,
codex, pi, openai-agents) plus every registered native. Tool calling and Policy
DENY are observed on `native-tui` too (landed separately). Not yet wired (see
the design doc's open items): Policy ALLOW / ASK on `native-tui` (a CEL
allow/ask attach, the way native DENY works), registry-driven server-side
native-agent seeding, the MCP-tool vs harness-native-tool distinction, and
further dimensions (steering, live-queue, resume/fork, elicitation, reasoning,
images, compaction).
