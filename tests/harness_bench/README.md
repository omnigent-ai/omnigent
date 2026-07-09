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
  coverage, and a strict superset of what `sdk-inproc` observes (everything
  sdk-inproc does, plus Tool calling + Policy DENY as server-dispatched,
  policy-gated calls). `--fast` opts them down to `sdk-inproc`.
- **native** harnesses use `native-tui` (a resident vendor CLI in a
  runner-owned tmux pane); `--fast` does not apply.
- `--transport NAME` overrides the family default for any harness.

## What it reports (P0 dimensions)

The six P0 dimensions are `basic_turn`, `streaming`, `tool_calling`,
`policy_deny`, `model_override`, `interrupt`. Each probe drives one real turn
and watches what the harness does:

| Probe | In plain terms |
| --- | --- |
| **Basic turn** | Can it hold a conversation at all? Asks it to echo a marker and checks the marker comes back -- the "is it alive" test. |
| **Streaming** | Does the answer arrive incrementally (word-by-word) or all at once at the end? Counts the token-level chunks. |
| **Tool calling** | Can it use a tool (run a command, read a file) mid-answer, not just chat? |
| **Policy DENY** | If a policy says "block that tool", does the harness actually enforce it? |
| **Model override** | If you ask for a specific model, does it actually run that one? |
| **Interrupt** | If you stop it mid-answer, does it actually stop? |

Each cell is a verdict: **✓** works, **✗** doesn't, **·** couldn't be tested
here (see below), **!!** the harness *declared* it works but the probe observed
otherwise (drift).

A `·` always means "the bench did not measure this here", never "the harness
lacks it". In particular Tool calling and Policy DENY only get a real verdict
on `full-server` (a server-dispatched builtin under a spec-baked deny policy),
so they show `·` on `sdk-inproc` and `native-tui`:

- **Native harnesses show `·` for Tool calling / Policy DENY, and that is not a
  native limitation.** Native harnesses do call tools and enforce permissions;
  the bench just cannot observe it on `native-tui` yet. A native tool call is
  the vendor's own tool (Bash/Read/...), not a server-dispatched builtin the
  bench can force, and a native deny is a vendor permission decision, not a
  server-side policy evaluation the probe can assert against. Giving those two
  cells a real verdict needs new driver work (an open item), not a change to
  the harnesses.
- **SDK harnesses show `·` only under `--fast`** (the wrap-direct `sdk-inproc`
  path has no tool-call policy hook); the default `full-server` run proves both
  as `✓`. That is why full-server is the SDK default.

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

Live today: the six P0 dimensions above; all three transports (`sdk-inproc`,
`full-server`, `native-tui`); the four official SDK harnesses (claude-sdk,
codex, pi, openai-agents) plus every registered native. Not yet wired (see the
design doc's open items): **bench observation** of Tool calling / Policy DENY
on `native-tui` (a driver gap, not a native-harness limitation),
registry-driven server-side native-agent seeding, and the P1 dimensions
(steering, live-queue, resume/fork, elicitation, reasoning, images, cost,
compaction).
