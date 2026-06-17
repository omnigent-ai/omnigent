# `--omnigent` example-YAML coverage: known gaps

Companion to `test_run_omnigent_example_agents.py` and
`test_run_omnigent_adapter_rejections.py`. Tracks gaps the e2e audit
surfaced — real adapter gaps, not per-YAML mistakes. Each gap
has an xfail'd test case so when it's fixed the test flips to
pass automatically.

## Fixed

### Fixed — `cancellable_function` tool runners were rejected

Original symptom: the forward translator wrapped every local
tool in `FunctionTool(callable=...)`, but
`CancellableFunctionTool` took a **runner** — a class instance
with a `.start(args, on_complete)` method, not a plain
callable. Caused `agent_with_tools.yaml` to fail at spec-load
with *"resolves to non-callable SleepToolRunner"*.

Resolution (step (c) of the harness contract migration): the
runner-protocol shape was retired entirely. Every `@tool` /
`type: function` is a plain callable; cancellable behaviour
flows through `sys_call_async` + `sys_cancel_task`. The forward
translator now fail-loud rejects `CancellableFunctionTool` /
`type: cancellable_function` with a migration hint pointing at
the new shape. The example YAMLs and `examples/_shared/tool_functions.py`
were migrated in the same change.

### Fixed — Gap 1: harness auto-selection based on model name

Pure omnigent' CLI auto-picks a harness from the model's
prefix: `databricks-claude-*` → `claude-sdk`, `databricks-gpt-*`
→ `openai-agents`, etc. The `--omnigent` adapter's validator refused
specs without an explicit `harness:` field, so every YAML that
relied on pure-omnigent auto-selection failed at spec-load:

```
invalid agent spec synthesized from omnigent YAML:
  executor.config.harness: required when executor.type is
  'omnigent' — must be one of ['claude-sdk', 'codex',
  'openai-agents', 'pi']
```

Fix: added `_infer_harness_from_model` helper in
`omnigent.spec.omnigent` with a `_HARNESS_FOR_MODEL_PREFIX`
lookup. `_translate_executor_from_def` calls it as the final
fallback when no explicit or parent harness is present. The
omnigent CLI's `_apply_overrides_to_yaml` no longer injects a
hard-coded `_AP_DEFAULT_HARNESS = "openai-agents"` fallback — that
band-aid was preventing the adapter's auto-pick from ever firing
(it saw the YAML after the CLI had already baked in the default).

Covered by the unit tests `test_harness_auto_picks_from_model_prefix`
(parametrized over claude/gpt variants) and the e2e cases
`simple_chat` and `agent_with_subagent_session`.

### Fixed — Gap 7: Stock omnigent policy callables don't work under Omnigent

`resolve_function_policy` now detects legacy `(content, phase)` callables at
load time and wraps them in `_omnigent_legacy_shim` — a one-argument adapter
that unpacks the engine's evaluation object into `(content, phase_string)`
before forwarding to the original callable. The shim is transparent: return
values and exceptions propagate unchanged.

The motivating example callables (`examples.tool_functions.block_long_sleep`
and friends) were already rewritten to the modern single-argument signature
before this fix landed, so they no longer exercise the shim path. The shim
exists to protect any user-written omnigent-style policy callables that have
not yet been migrated.

### Fixed — Gap 2: parent-to-inline-AgentTool `harness:` propagation

Inline AgentTools in `coding_supervisor_with_forks.yaml` /
`agent_with_subagent_session.yaml` declare `prompt:`, `os_env:`,
`tools:` but omit `executor:`, expecting the parent's harness
to flow down. The adapter's `_agent_tool_to_sub_spec`
propagated `parent_profile` and `parent_os_env` but NOT
harness. Each sub-spec tripped the `harness-required` validator:

```
sub_agents['worker_a'].executor.config.harness: required when
executor.type is 'omnigent' — must be one of [...]
```

Fix: `_agent_tool_to_sub_spec` and `_translate_executor_from_def`
now accept `parent_harness`. The harness resolution order is
(1) explicit child `executor.harness`, (2) parent_harness,
(3) `_infer_harness_from_model` on the resolved model.
Importantly, the **effective** parent harness (post auto-pick)
is propagated — not the raw YAML value — so a parent that
declares only `model: databricks-gpt-5-4-mini` auto-picks
`openai-agents` and passes that concrete harness down to its
children.

Covered by the unit tests `test_inline_agent_tool_without_executor_inherits_parent_harness`
and `test_inline_agent_tool_explicit_harness_wins_over_parent`,
and the e2e cases `agent_with_subagent_session` and
`coding_supervisor_with_forks`.

## Outstanding gaps

### Gap 3 — `examples/agent_with_os_env.yaml` requires Linux

Not an adapter bug: the example ships with `sandbox:
linux_bwrap` which only works on Linux hosts with bwrap
installed. Orthogonal to `--omnigent`. Needs either an
environment-aware example or a per-OS test override.

### Gap 4 — `agent_with_uc_tools.yaml` untested

UC catalog functions may or may not round-trip through the
adapter cleanly. Needs a minimal real-UC-function fixture
before an e2e can land.

### Gap 5 — `terminal_workers.yaml` harness unknown

Uses `harness: open-responses`, which isn't in the adapter's
validator allowlist (`['claude-sdk', 'codex', 'openai-agents',
'pi']`). Expected to fail at spec-load with a harness-required
error, but the adapter reads `harness` as a plain string so
the actual error is unclear. Needs investigation.

### Gap 8 — Can't ban a specific sub-agent by name

If your omnigent YAML declares a sub-agent tool named
`worker` and you want a policy that denies calls to it, the
natural thing to write is `match_tools: [worker]`. That doesn't
work under Omnigent. Sub-agents are invoked through a generic
`sys_session_send` tool, not by their name directly, so the
policy engine sees `sys_session_send` as the tool name and the
`worker` filter never matches.

Today's workaround: write the policy against `sys_session_send`
and read the sub-agent name from the call's arguments. That
works but it's non-obvious and authors following the omnigent
docs won't expect it.

**Fix options**: (1) rewrite `match_tools: [worker]` at
translation time into a policy that filters on
`sys_session_send` + an arguments check, or (2) expose each
sub-agent as its own first-class tool name so the call sites
match the YAML. Option 2 is cleaner but touches more of the
spawn path.

### Fixed — Gap 6: `--omnigent` hung when FunctionTools were registered on the top-level agent

**Root cause**: the adapter synthesizes `LocalToolInfo.language =
"omnigent-python-callable"` for every user FunctionTool /
CancellableFunctionTool in the YAML. `load_local_python_tools`
intentionally skips entries where `language != "python"` (it
only loads tools that live on disk as `tools/python/*.py`), so
these tools were **advertised** to the inner harness via
`_build_omnigent_tool_schemas` but never **registered** on
Omnigent' `ToolManager`. When the harness's LLM invoked one,
the `_tool_executor` bridge called `context.call_tool`, which
fell through to `await_tool_output` (the client-side tunneling
path) and parked forever waiting for a client that doesn't exist.

**Fix** (see `omnigent/runtime/executors/omnigent.py`):
`_make_tool_executor_bridge` now takes the `AgentDef` and
dispatches user tools directly:

- `FunctionTool` → call `tool.callable(**args)` directly
  (sync: on the thread pool; async: awaited on the current loop).
- `CancellableFunctionTool` → adapt the runner's
  `start(args, on_complete)` callback-style API to an awaitable
  via `asyncio.Future`, guarded by a 300s timeout.
- Agent-plane builtins (`check_task`, `sys_session_send`, etc.) →
  still route through `context.call_tool` → `ToolManager`.

TOOL_CALL policy enforcement is preserved: the bridge now calls
`context.enforce_tool_call_policy(tool_name, args)` **before**
dispatching, so the same guardrails Omnigent' native tool
loop applies also apply here. On DENY, the sentinel returns as
the tool output instead of invoking the real callable.
`ExecutorContext` got a new `enforce_tool_call_policy` field;
the workflow wires it to `_enforce_tool_call_policy` with the
already-built `PolicyEngine`.

Verified by:
- `examples/agent_with_tools.yaml` (xfail removed, passes
  end-to-end through `--omnigent` with the claude-sdk harness).
- `tests/e2e/omnigent/test_run_omnigent_policy_enforcement.py::test_policy_denies_tool_call_by_name`
  (real `omnigent run --omnigent -p "what is 6 + 6?"` subprocess
  with a `type: function` policy narrowed to `calculate` via `action: deny`
  policy; asserts the LLM acknowledged the denial AND "12"
  never appears in output — the tool was short-circuited, not
  dispatched).
- The full rejection + happy-path e2e sweep: 11/11 passing in
  ~125s against the real Databricks gateway.
