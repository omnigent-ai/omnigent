# Harness acceptance checklist

Users should be able to complete the same supported workflows with every SDK
and native harness. This checklist connects the existing harness bench and
feature recipes; it is not another test runner or a claim that every harness
has passed. Pi has the same acceptance criteria as Claude and Codex. Capability
differences can justify exceptions; maintainer assignment or support tier cannot.

## Sub-features

P0 is the usable-session baseline; P1 covers recovery and additional supported
features. These are checklist priorities, not changes to the bench's probe
priorities or CI gates. Scope a change to affected rows and consumers, and
record omissions rather than treating unselected checks as passed.

| ID | Priority | Observable acceptance |
| --- | --- | --- |
| `launch-reply` | P0 | Launch through the supported entry point; streamed output and the final answer appear once. Usage is reported where supported. |
| `working-status` | P0 | A demonstrably active turn stays working through quiet model/tool work. Supported blocked states explain what input is needed. Completion, interruption and process exit settle the indicator; the next turn works. Opening or closing a terminal viewer does not change the reported state. |
| `tool-history` | P0 | A real tool returns a value; a later turn uses that value without repeating the tool. Tool events and results remain visible. |
| `model-effort` | P0 | Pre-launch and in-session selections reach the intended model/request. Supported effort survives a tool round trip and a later change without losing history; rejected changes do not falsely appear applied. |
| `interrupt-continue` | P0 | Interrupt a demonstrably active turn, then complete another turn in the same session without stale work or a stuck running state. |
| `reload-resume` | P1 | Reload/reattach and supported resume retain history and settings; a subsequent turn works without missing or duplicated output. |
| `auth-recovery` | P1 | The normal credential path works; renewal during tools/later turns works where supported. Authentication failure is clear and redacted; after repair the supported continuation path works. |
| `approvals` | P1 | Supported allow, deny and ask decisions reach the tool; denied side effects do not occur and the session can continue. |
| `steering-queue` | P1 | Supported immediate steering and queued messages arrive once, in the intended order, including around interruption. |
| `compaction` | P1 | Supported compaction finishes and a later turn works with retained context; pending messages are not lost. |
| `images` | P1 | A supported image reaches the model and its reply uses the image; unsupported inputs are rejected without losing the draft. |
| `fork` | P1 | A supported fork uses copied history on its first turn and remains separate from its parent. |
| `mcp` | P1 | A configured, supported Omnigent MCP connection can call a harmless tool and show its result. |

## How to get to it (user POV)

Use a separate evidence row for each applicable surface; a shared implementation
does not make the surfaces interchangeable.

| Surface | Entry point and owning recipe |
| --- | --- |
| Web new session | Pick a native harness or SDK custom agent, host/workspace, model and effort, then send. See [Composer](./composer.md) and [Custom agents](./custom-agents.md). |
| Web existing session | Send follow-ups, edit configuration, interrupt, answer approvals, reload or fork. See [Composer](./composer.md), [Sessions](./sessions.md) and [Native harnesses](./native-harnesses.md). |
| Native terminal / CLI | Launch using the harness's CLI entry, use supported native controls, and switch Terminal ↔ Chat. See [Native harnesses](./native-harnesses.md) and [Terminals](./terminals.md). |
| SDK CLI / API | Launch a custom agent with `omnigent run` or the session API; verify the server/runner contract. The [bench](../tests/harness_bench/README.md#transport-selection) uses this boundary, not browser rendering. |
| Alternate UI surfaces | Exercise mobile, fork-dialog configuration and Inbox approvals when the change affects them. Use the owning recipes above, not desktop Chat as a substitute. |

## Driving it with the repro environment

Preconditions: read the [verification skill](./skills/verify-omnigent/SKILL.md)
and the owning feature recipes. Use only an isolated instance you started.
Record the exact commit, CLI/SDK versions and CI pins, OS, provider/model,
transport, relevant feature gates and sandbox state. Keep native and SDK
results separate, including `pi-native` versus `pi`. OSS using a Databricks
gateway is still OSS; Managed means Databricks-deployed Omnigent and needs its
own deployment evidence.

### Select harnesses and existing checks

Use the capability registry through the bench rather than maintaining another
list of harness names or capabilities:

```sh
uv run --no-sync python -m tests.harness_bench --list
uv run --no-sync python -m tests.harness_bench --list-dimensions
uv run --no-sync python -m tests.harness_bench --no-live --markdown
```

The offline matrix is **declarations only**, not acceptance evidence. Select
the affected SDK/native harnesses, existing/default configurations and other
consumers of shared code. A capability declaration selects relevant checks; it
does not prove behavior. Missing credentials or an unobservable transport are
blocked/missing evidence, not reasons to mark a supported feature N/A.

### Existing coverage and gaps

Bench dimensions below are reusable checks, not complete coverage of a row.
Use the [bench transport matrix](../tests/harness_bench/README.md#coverage-by-transport)
and each harness's [mapped tests](./native-harnesses.md#driving-it-with-the-repro-environment)
to identify the actual boundary. A dash means no shared bench probe, not that
no harness-specific tests exist. No passing per-harness results are implied.

| Checklist ID | Bench dimensions | Existing automation / remaining gap |
| --- | --- | --- |
| `launch-reply` | `basic_turn`, `streaming`, `cost_tracking` | Also check authored instructions for custom agents via the [native instruction recipe](./native-harnesses.md#driving-it-with-the-repro-environment); bench replies alone do not prove delivery. Server/runner observations; browser launch/rendering and exact-once presentation need the owning UI tests. |
| `working-status` | — | No shared status-continuity probe. Use the [native working-status recipe](./native-harnesses.md#driving-it-with-the-repro-environment) for Claude OSC/file fallback and UI status routing; SDK lifecycle and other natives need their own evidence. A completed basic turn does not prove quiet-work or blocked-state accuracy. |
| `tool-history` | `tool_calling` | Tool round trip only. Later-turn recall is not a shared probe; reuse the Pi scenarios below and identify other harness coverage explicitly. |
| `model-effort` | `model_override`, `reasoning` | Native-TUI model override is currently skipped; SDK routing/reasoning observations are narrower than selection persistence. Reasoning output does not prove outgoing effort, tool continuity, High→Low or UI state. |
| `interrupt-continue` | `interrupt` | Cancellation signal/early termination; follow-up recovery needs a separate assertion in the same session. |
| `reload-resume` | — | No shared resume/reconnect probe. Existing native resume/disconnect tests and session/terminal recipes cover specific harnesses and boundaries only. |
| `auth-recovery` | — | No shared auth-renewal probe. Reuse the owning harness credential tests; distinguish injected rotation from real elapsed expiry. |
| `approvals` | `policy_allow`, `policy_deny`, `policy_ask` | Supported policy paths through the chosen transport; Chat/Inbox presentation and post-decision continuation need their own evidence. |
| `steering-queue` | — | No shared probe. Map the harness-specific steer/queue scenarios from Composer and Native harnesses. |
| `compaction` | — | No shared probe. Use the Composer compaction recipe and the harness's supported entry points. |
| `images` | — | No shared probe. Attachment UI alone does not establish delivery to a real harness/model. |
| `fork` | `fork_replay` | Copied-history use through server/runner; fork dialog, alternate targets and parent isolation need the Sessions recipe. |
| `mcp` | `omnigent_mcp` | Read-only relay tool for supported natives; Settings inventory alone is not a tool-execution check. |

Start the tool/history/effort continuity work by extending compatible scenarios,
not by copying their server/runner setup:

- SDK Pi: `tests/e2e/omnigent/test_pi_retry_session_sync_e2e.py::test_pi_session_stays_in_sync_after_retry_recovery`.
- Native Pi: `tests/e2e/test_pi_native_model_scope_e2e.py::test_generic_provider_delivers_effort_through_native_tool_turn`.
- Browser selection: the [Composer model/effort recipes](./composer.md#driving-it-with-the-repro-environment).

These are starting points, not proof for other harnesses or providers. When
generalizing a scenario, keep assertions shared and transport-specific setup
in the existing drivers/[primitives](../tests/_helpers/README.md). Keep input
matrices at the lowest reliable layer; retain browser/process cases for distinct
boundaries. Do not move or replace working regression coverage just to fit the
checklist.

### Pi SDK bundled resources (own environment)

For `launch-reply`, launch an uploaded Pi agent bundle through the server/runner,
then run `omnigent run <bundle>` from a sibling workspace under Linux Bubblewrap.
Include an extension,
a skill and bundle-root instructions; verify all reach the provider request and
that tools still use the session workspace. Existing scenarios:
`tests/e2e/omnigent/test_pi_bundle_extensions_cwd_e2e.py`,
`tests/e2e/omnigent/test_pi_bundle_project_resources_e2e.py`, and
`tests/e2e/omnigent/test_pi_managed_extensions_e2e.py`.
The managed-extensions filename refers to extension provisioning, not Managed
Omnigent. The uploaded-bundle and existing global-extension CLI scenarios cover
unsandboxed startup;
the separate CLI bundle case covers real Bubblewrap isolation; a macOS skip does not
validate that boundary. Native Pi resource discovery is a separate path.

Run the scoped `launch-reply` checks (own environments; real Pi, scripted provider):

```sh
uv run --no-sync pytest tests/e2e/omnigent/test_pi_bundle_extensions_cwd_e2e.py tests/e2e/omnigent/test_pi_bundle_project_resources_e2e.py tests/e2e/omnigent/test_pi_managed_extensions_e2e.py --reruns 0 -q
```


### Pi user resources (own environments)

For `launch-reply` and `reload-resume`, configure Pi skills/extensions in the
user agent root, with relative local-package paths and disabled-resource patterns.
`tests/e2e/omnigent/test_pi_managed_extensions_e2e.py` drives SDK CLI startup with
default and relative custom roots; inspect provider requests for loaded skills,
package markers, excluded extensions, and unchanged SDK tool restrictions.
`tests/e2e/omnigent/test_pi_context_files_e2e.py` checks that context/prompt controls
and `skills: none` still apply. Native startup/resume is covered by
`tests/e2e/test_pi_native_cold_resume_model_rewrite_e2e.py` through server/host/runner.
A host custom root uses the existing explicit
`OMNIGENT_RUNNER_ENV_PASSTHROUGH=PI_CODING_AGENT_DIR` setting.

These scenarios use real Pi with scripted providers or prompt-observer extensions;
they do not establish marketplace compatibility, browser rendering, live MCP OAuth,
OS sandbox or Managed deployment acceptance. Linked resources are shared writable
files; global instruction files, model credentials and session state stay separate.

### Deterministic checks versus live canaries

| Lane | Use | What a pass does not prove |
| --- | --- | --- |
| Deterministic regression | Components, real processes with scripted providers, and existing browser tests. Run relevant existing targets with retries disabled. | Live provider compatibility or elapsed credential expiry. |
| Authorized live canary | Exact versions/model through the real runtime; bounded prompts and harmless tools. Repeat the selected workflow, not just an isolated HTTP request. | Every model, OS, deployment or browser surface. |

The verification environment configures mock Claude/Codex, not every harness.
Other harnesses use the owning scenario's isolated setup. A missing vendor CLI
or credential must remain visible. Do not change global rollout settings or
reuse a developer daemon to obtain a passing result.

For mock journeys, verify that the test's unique user prompt reaches the mock
under the intended model. A reply that echoes an expected token, or a nonzero
background-request count, is insufficient. Native Claude mock sessions reject
conflicting machine-managed routing before launch; isolated user directories
do not override machine policy. Run on a clean container/CI host and record the
blocked local attempt separately (see [native prerequisites](./native-harnesses.md#driving-it-with-the-repro-environment)).

For an **authorized** live bench slice, use an explicit harness/model and require
observations. This example runs SDK Pi; select `pi-native` separately for native:

```sh
uv run --no-sync python -m tests.harness_bench --live --require-complete \
  --harness pi=system.ai.deepseek-v4-1-flash \
  --dimension basic_turn,streaming,tool_calling \
  --json --report /tmp/pi-harness-results.json
```

Choose a model actually served by your provider; the example is not an endpoint
availability guarantee. The current native-TUI driver does not apply the bench
model override: configure and verify its actual selected model separately, and
do not claim `--harness pi-native=MODEL` pinned the request. A complete,
drift-free bench report covers only its
selected dimensions/transport. Check the acceptance assertions above as well:
an expected `PARTIAL` or `UNSUPPORTED` verdict is not a pass for a required
workflow. This checklist adds no repository-wide or Resolve gate, scheduler,
paid live run, or automatic rollout requirement.

### Record per-harness evidence

Copy this table into the PR's Test Plan or linked evidence. Expand it for **every
affected harness × checklist ID × entry point/configuration**; do not combine
SDK/native, mock/live or OSS/Managed results. Include gaps even where no test
exists. Add an owner/next step for unresolved rows.

| Harness / transport / model | Checklist ID / entry point | Coverage | Run result | Evidence / gap / owner |
| --- | --- | --- | --- | --- |
| Fill exact configuration | Fill ID and surface | Covered / Partial / Missing / N/A | Pass / Fail / Blocked / Not run / N/A | Test or manual steps, command, artifact and remaining action |

- **Coverage** describes available checks, not their outcome. Covered means all
  assertions for that row/surface have a test or explicit manual recipe; Partial
  lists missing assertions; Missing has no matching check yet. N/A needs a
  capability/surface reason, never a support-tier exemption.
- **Run result** describes this exact revision/environment. Missing setup,
  skipped probes and inconclusive observations are Blocked, with the reason;
  unexecuted checks are Not run. Preserve the bench's raw verdicts beside the
  acceptance result. Failures must not be relabeled as coverage gaps.
- **Evidence** includes the version/configuration record, actual command and
  result, redacted request fields or process/session transcript, and media for
  user-visible claims following the [PR demo standard](README.md#pr-demo-standard).
  Put the recording and its evidence caption in Demo; link it from the relevant
  table rows without treating one recording as proof for unshown workflows.
  Keep failed attempts; do not publish tokens.

## Gotchas

- A matrix row, a linked test, an offline report or a green docs validator is
  not proof that the harness ran. `--require-complete` prevents silent missing
  bench observations; it does not validate this entire checklist or browser UI.
- Tool, reasoning and interrupt probes are independent checks, not a substitute
  for a single session surviving tools, effort changes and follow-ups.
- For Pi gateway reasoning, include a model whose metadata exists only in its
  vendor catalog (for example GPT), alongside a Databricks-catalog model (Gemini).
  Observe outgoing effort through a tool result and follow-up; a pre-enriched
  model fixture or a visible High label alone misses catalog fallback failures.
- Browser reload is not runner restart; neither is credential renewal. Test
  the boundary affected by the change and name unrun boundaries explicitly.
- Use the same baseline for all affected harnesses. Until a gap is covered,
  report it with an owner rather than reducing acceptance for a tier-2 harness.
