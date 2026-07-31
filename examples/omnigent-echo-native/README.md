# omnigent-echo-native — example native harness plugin

The **reference community native-harness contribution** for Omnigent. It shows
the minimum a package must ship to register a native (terminal/TUI) harness
through the `NativeHarnessProvider` seam — the contract that landed in Phase 1
(runner/resume/interrupt/seeding behind the seam) and Phase 2.1/2.2 (validator
accepts native contributions; `/v1/harnesses` publishes them).

## What it demonstrates

- A `get_contribution()` entry point (`omnigent.community.harness` group) that
  pairs a `NativeCodingAgent` (identity) with a `NativeHarnessProvider`
  (behavior hook paths), both keyed `echo`, under the reserved
  `omnigent.community.harness.echo.*` namespace.
- The provider hooks core resolves lazily: `run_native`, `auto_create_terminal`,
  `spawn_env_builder`, `materialize_agent_spec`.
- A `capabilities` record incl. the `fork_history` axis and bench shell-tool
  fields.

## What is real vs. stubbed

| Piece | State |
|---|---|
| `get_contribution()` / registry rows | **real** — passes the community validator, merges into the accessors |
| `materialize_echo_agent_spec` | **real** — writes a loadable `echo-native-ui` spec |
| `build_echo_native_spawn_env` | **real** — pure env mapping |
| `run_echo_native`, `launch_echo`, `create_app` | **stub** — `NotImplementedError` with a pointer to the built-in `pi_native` / `claude_native` real implementation |

The stubs are marked `TODO(real-harness)`. A production native plugin fills them
in against a live vendor CLI (spawn the TUI in a runner terminal, tail its
transcript, mirror output). That is what makes it fully `--live` benchable.

## Try it

```bash
# From an omnigent checkout, install the example against it (sibling source).
uv pip install -e examples/omnigent-echo-native

# It now registers through the real entry-point loader:
uv run python -c "from omnigent.harness_plugins import valid_harnesses; print('echo-native' in valid_harnesses())"
uv run python -c "from omnigent.harness_plugins import native_provider_for_key; print(native_provider_for_key('echo'))"
```

The end-to-end registry behavior is covered (without an install step) by
`tests/test_example_echo_native_plugin.py` in the core repo.

## See also

- `designs/harness-plugin-interface.md` § "Native TUI Harnesses" — the checklist
  this package implements.
- `omnigent/pi_native.py`, `omnigent/runner/native/orchestration.py` — the real
  built-in native harness the stubs point at.
