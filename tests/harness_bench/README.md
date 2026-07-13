# Harness test bench

A pluggable conformance suite that probes harness behavior and reconciles the
observed verdicts with the capability model to surface drift. Design and
rationale: [`docs/harness-bench-design.md`](../../docs/harness-bench-design.md).

## Run it

```bash
# List official harnesses (name, resolved transport, model).
python -m tests.harness_bench --list

# Force the declared-only matrix: no turns and no credentials required.
python -m tests.harness_bench --no-live

# Probe one harness. Credentials are resolved like `omni run`.
python -m tests.harness_bench --harness codex

# Override the configured/ambient Databricks profile.
python -m tests.harness_bench --harness codex --profile my-profile

# Probe several harnesses concurrently with the live table.
python -m tests.harness_bench --jobs 4 --rich
```

Without `--live` or `--no-live`, the CLI runs live when gateway credentials are
resolvable and otherwise renders the declared matrix offline. Credential
resolution follows `omni run`: existing ambient `OPENAI_*` routing is
preserved; otherwise `--profile` overrides the configured profile. A
non-zero exit means at least one `DRIFT` cell was found.

### Flags

- `--live` / `--no-live` -- force live probing or the declared-only matrix.
  `--live` requires resolvable gateway credentials.
- `--profile NAME` -- optional Databricks profile override; it is not required
  when config or ambient `OPENAI_*` already supplies credentials.
- `--harness NAME` -- probe one harness (repeatable). Accepts an official name
  or a `module:attr` / `module.ATTR` reference to a community `BenchProfile`.
  Defaults to every official harness.
- `--fast` -- run SDK harnesses on `sdk-inproc` instead of the `full-server`
  default. This skips server startup, but policy ALLOW/ASK/DENY are not
  observable and tool/cost verdicts are limited to what the wrap forwards.
  It has no effect on native harnesses and is mutually exclusive with
  `--transport`.
- `--transport NAME` -- force `sdk-inproc`, `full-server`, or `native-tui`,
  overriding the harness-family default.
- `--jobs N` / `-j N` -- run up to N harnesses concurrently (default 1).
  Probes within one harness remain sequential and report order is stable.
- `--rich` / `--no-rich` -- force or disable the live progress table. Auto mode
  uses Rich on a TTY and plain per-line output otherwise.
- `--report PATH` -- also write the final matrix. Format follows `--json` or
  `--markdown`, then the filename extension.

### Output formats

- Default: aligned terminal table plus Notes for every non-supported cell.
  Color disables automatically when piped or with `--no-color`.
- `--markdown`: GitHub-flavored table for docs and pull requests.
- `--json`: machine-readable output for diffing runs or regenerating docs.

Each row includes the transport that actually ran it, such as
`claude-sdk [full-server]` or `kimi-native [native]`. Under `--rich`, the live
table is rendered on stderr; the stdout report avoids printing the grid twice,
but redirected output remains self-contained.

## Transport selection

A profile's `transport` is a harness-family marker. The resolved driver is:

- **SDK family:** `full-server` by default. This runs through a real server and
  runner and observes server-dispatched tools plus fixed ALLOW/ASK/DENY policy
  behavior. `--fast` selects the cheaper wrap-direct `sdk-inproc` driver.
- **Native family:** `native-tui`, which drives a resident vendor CLI in a
  runner-owned tmux pane through the server session API.
- `--transport NAME` overrides the family default when the driver supports the
  selected harness.

## Dimensions

| Probe | What it verifies | Priority |
| --- | --- | --- |
| **Basic turn** | A turn completes and returns assistant text. | P0 |
| **Fork replay** | A cloned session copies the source history and can use it on its first turn. | P1 |
| **Streaming** | More than one output-text delta is emitted; a repeated single delta is `PARTIAL`. | P0 |
| **Tool calling** | A tool call is surfaced and the turn closes after its result. | P0 |
| **Omnigent MCP** | A native harness calls the read-only `sys_session_list` relay tool through its generated `omnigent` MCP server. | P1 |
| **Policy DENY** | A tool-call policy blocks the call. | P0 |
| **Policy ALLOW** | A tool call proceeds while an explicit allow policy is attached. | P1 |
| **Policy ASK** | An ask policy raises an approval elicitation. | P1 |
| **Model override** | The harness accepts and completes with the requested model. | P0 |
| **Cost tracking** | A completed turn reports priced cost (`SUPPORTED`) or tokens only (`PARTIAL`). | P1 |
| **Interrupt** | A running turn stops after interruption. | P0 |

Verdicts are `SUPPORTED` (`✓`), `PARTIAL` (`~`), `UNSUPPORTED` (`✗`),
`NOT_APPLICABLE` (`—`), `UNKNOWN` (`?`), `SKIPPED` (`·`), and `DRIFT` (`!!`).
A skip means the bench could not measure the behavior in that environment or
transport; it does not claim the harness lacks the capability.

### Coverage by transport

| Dimension | `full-server` | `native-tui` | `sdk-inproc` (`--fast`) |
| --- | --- | --- | --- |
| Basic turn, Streaming, Model override, Interrupt | End-to-end through server + runner | End-to-end through server + runner + vendor CLI | Wrap boundary only |
| Fork replay | Clone + copied-history replay through server + runner | Clone + copied-history replay through server + runner + vendor CLI | Not observable |
| Tool calling | Server-dispatched builtin | Vendor tool mirrored as a session item | Request-level wrap tool |
| Omnigent MCP | Not applicable | Generated `omnigent` MCP relay when supported by the vendor | Not applicable |
| Policy DENY | Fixed policy in the agent spec | Session CEL policy + native policy hook | Not observable |
| Policy ALLOW / ASK | Fixed policy; ASK observes and resolves an elicitation | Temporary session CEL policy; ASK observes and resolves an elicitation | Not observable |
| Cost tracking | Session snapshot | Session snapshot when the vendor forwards usage | Completed-response usage when forwarded |

The bench is a headless client of the server API. It verifies the contract the
web application consumes, not browser rendering; UI presentation belongs in
`tests/e2e_ui/`.

## Layout

| File | Role |
| --- | --- |
| `verdict.py` | Verdicts, priorities, probe results, and drift reconciliation |
| `profile.py` | `BenchProfile` and profile-name resolution |
| `manifest.py` | Official profiles derived from the capability registry and e2e probe metadata |
| `transport.py` | Driver protocol, registry, and transport resolution |
| `driver.py` | `SdkInprocDriver`, shared `TurnResult`, and usage helpers |
| `full_server.py` | Shared server/runner lifecycle and agent/session registration |
| `full_server_driver.py` | Full-server probe implementation and shared polling |
| `native_tui_driver.py` | Native vendor CLI provisioning and native probe implementation |
| `session_items.py` | Shared parsing for session-item envelope shapes |
| `runtime_env.py` | Credential/config resolution shared with the normal runtime behavior |
| `probes/` | One module per dimension; `ALL_PROBES` defines display and run order |
| `events.py` / `richreport.py` | Structured progress events and optional Rich rendering |
| `bench.py` | Orchestration, concurrency, prerequisite handling, and shared-server wiring |
| `report.py` | Terminal, Markdown, and JSON renderers |

Reusable production helpers live in `omnigent.config` and the existing runtime
utility modules rather than being duplicated in the bench.

## Extending the bench

### Add a harness

- **Official SDK:** register the harness normally; base probe metadata and
  capabilities flow into the manifest without a new driver.
- **Native:** every harness marked `NATIVE_TUI` is derived automatically;
  `native_vendor()` derives its launch metadata from capabilities.
- **Community:** ship a `BenchProfile` and select it with
  `--harness mypkg.harness:PROFILE`.

A community native harness is recognized by the bench, but the server still
needs a seeded native UI agent. Registry-driven native-agent seeding remains an
open platform item.

### Add a dimension

Add a `CapabilityProbe` under `probes/`, register it in
`probes/__init__.py:ALL_PROBES`, add a semantic method to the driver protocol,
and derive or declare the expected verdict. Keep transport-specific mechanics
inside drivers so probes remain harness-agnostic.

## Current gaps

- Native agent seeding in the server is still hardcoded rather than driven by
  the harness registry, which limits community-native end-to-end execution.
- Some native harnesses require vendor login/provider setup that the bench
  cannot provision and therefore skip cleanly.
- Steering, live queue, resume, reasoning, images, and compaction do not
  yet have probes.
