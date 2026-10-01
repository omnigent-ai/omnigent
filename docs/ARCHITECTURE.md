# Omnigent architecture

Omnigent runs AI agents ("harnesses" such as Claude Code, Codex, or an SDK
executor) on machines you control and lets you drive them from web, desktop,
mobile, or the terminal. This page is the map of the Python package: what each
part owns, how a turn flows through them, and where new code goes.
`tests/test_architecture.py` enforces the rules below.

## Processes

```
 web / desktop / mobile / CLI / SDK clients
                  │  HTTPS + SSE
                  ▼
 ┌──────────────────────────────┐
 │ server   (control plane)     │  sessions, agents, auth, policies, storage
 └──────────────┬───────────────┘
                │  WebSocket tunnel
                ▼
 ┌──────────────────────────────┐
 │ host     (one per machine)   │  connects the machine, spawns runners
 └──────────────┬───────────────┘
                ▼
 ┌──────────────────────────────┐
 │ runner   (data plane)        │  one session's tools, MCP, terminals, sandbox
 └──────┬───────────────┬───────┘
        │               │
        ▼               ▼
  SDK harness       native harness
  subprocess        vendor TUI in tmux
  (Executor)        (bridge + forwarder)
        │               │
        └──────► model provider (directly or via the model signer)
```

A local `omnigent` invocation starts the same pieces on your laptop; a
deployment runs the server remotely and hosts on each machine.

## Package map

| Package | Owns |
| --- | --- |
| `core/` | Agent definition types (`datamodel`), the `Executor` contract every harness implements, and the YAML loader; the library API re-exported by `omnigent/__init__.py`. |
| `util/` | Small shared helpers: platform flags (`portability`), subprocess spawning and teardown, how to spell the CLI, runner identity env vars, attachment files, the installed package root. |
| `observability/` | Process logging, the debug event sink, OpenTelemetry setup and tracing, startup milestones. (`telemetry/` is product analytics.) |
| `spec/` | Parsing and validating agent images (`AGENTS.md`, `config.yaml`, bundles). |
| `models/`, `llms/` | Model catalog, resolution, inference profiles, gateway detection, the model signer (`models/signer/`); `llms/` is the multi-provider LLM client. |
| `policies/` | Pure policy evaluators. The stateful engine that runs them per session is `runtime/policies/`. |
| `entities/`, `db/`, `stores/` | Domain entities, SQLAlchemy models and migrations, and the store interfaces with their SQL implementations. |
| `sandbox/`, `environments/`, `terminals/` | OS sandboxes (Seatbelt, bubblewrap, Job Objects, egress proxy, credential proxy), OS environments for agent subprocesses, and managed tmux terminals. |
| `tools/`, `runtime/` | Built-in tools and MCP bridging; the per-session turn runtime (workflow, prompt composition, compaction, pending inputs, streams). |
| `harnesses/` | Everything harness-specific — see below. |
| `runner/`, `host/`, `server/` | The three long-lived services from the diagram. |
| `cli/`, `repl/`, `onboarding/` | Terminal front-ends: the `omnigent` command, the interactive REPL, first-run setup. |

## Harnesses

Every harness lives in exactly one package under `omnigent/harnesses/`:

- **SDK / headless harnesses** (`claude_sdk/`, `codex/`, `pi/`, `acp/`, `openai_agents/`, …)
  hold an `executor.py` (an `omnigent.core.executor.Executor`) and a
  `harness.py` whose `create_app()` wraps it with
  `harnesses.runtime._executor_adapter.ExecutorAdapter`. The runner spawns that
  app as a subprocess through `harnesses/runtime/` (process manager, scaffold,
  subprocess entry point).
- **Native harnesses** (`claude_native/`, `codex_native/`, `pi_native/`, …) wrap a
  vendor's own terminal UI: `main.py` launches it, `bridge.py` owns the
  per-session bridge directory, a transcript forwarder (usually `forwarder.py`)
  mirrors its output, hooks enforce policy, and `executor.py`/`harness.py`
  deliver web turns into it.
  Shared plumbing lives in `harnesses/native/`; the runner-side terminal
  orchestration lives in `runner/native/`.
- **The registry** (`harnesses/registry.py`, `aliases.py`, `capabilities.py`,
  `availability.py`, `install_spec.py`, `wrapper_labels.py`, `startup_config.py`) is import-light
  metadata: ids, aliases, capabilities, install hints, and the dotted paths the
  runner resolves at dispatch time. Community plugins contribute rows through
  the `omnigent.community.harness` entry point (`designs/harness-plugin-interface.md`).

Adding a harness means adding one package plus its registry rows.

## Layering

Imports should point down this list: a package imports from its own layer or
any layer below it, never above.

| Layer | Packages |
| --- | --- |
| 0 foundation | `core`, `util`, `entities`, root `errors` / `version` / `config` |
| 1 libraries | `spec`, `policies`, `models`, `llms`, `observability`, `telemetry`, `db`, `stores`, `connections`, `extensions`, harness registry modules |
| 2 execution | `sandbox`, `environments`, `terminals`, `tools`, `runtime`, `harnesses` |
| 3 services | `runner`, `host`, `server` |
| 4 front-ends | `cli`, `repl`, `onboarding` |

The tree does not fully meet this yet. Existing exceptions are listed in
`KNOWN_LAYER_VIOLATIONS` in `tests/test_architecture.py`, and that list only
shrinks: the test fails on a new upward import and on an entry that no longer
occurs.

## Where new code goes

- Harness-specific code goes in that harness's package, never in `runner/`,
  `server/`, or `cli/` behind an `if harness == ...` branch.
- New top-level modules are not allowed; pick the package that owns the concept.
- Locate the installed package with `omnigent.util.package_root.PACKAGE_ROOT`,
  not `Path(__file__).parents[n]`, so files can move without breaking.

## Compatibility paths

Some old module paths must keep importing because something outside the
checkout names them:

- `omnigent/inner/` is a frozen namespace. `inner/nessie/policies.py` stays
  forever because database rows store its policy handler paths; the other files
  are aliases for the old library API (`omnigent.inner.datamodel`, `executor`,
  `tools`, `policies`, `loader`).
- `omnigent/runtime/harnesses/` and the root `harness_plugins.py` /
  `harness_install_spec.py` alias the old plugin-facing paths.
- `omnigent.host.service_entry` (launchd/systemd units) and
  `omnigent.git_credential_github` (sandbox `~/.gitconfig`) are written into
  files outside the checkout and must not move.

Aliases other than the nessie policy shim are removed in 0.19.0.

## Rebasing a branch across the reorganization

`dev/refactor/module_moves.toml` records every module move. To update an
in-flight branch after rebasing onto it:

```bash
python dev/refactor/relocate_modules.py rewrite --map dev/refactor/module_moves.toml
```
