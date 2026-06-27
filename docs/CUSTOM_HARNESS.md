# Adding a custom harness

A **harness** is the per-conversation subprocess that actually runs an agent's
model loop. When you point a spec at `executor.harness: cursor`, Omnigent looks
up the name `"cursor"` in a registry, spawns the mapped Python module as a
FastAPI service over a Unix socket, and proxies the conversation's turns
through it. The harness speaks the same Pydantic models the Omnigent REST API
serves to external clients — there is no separate harness protocol.

This is a focused howto for adding a new harness (wrapping a new SDK or CLI).
It walks the five pieces a contributor needs, cites the real files to copy from,
and ends with a checklist.

## The five pieces

| # | Piece | File | What it does |
|---|-------|------|--------------|
| 1 | Executor bridge | `omnigent/inner/<name>_executor.py` | Subclasses `Executor`, implements `run_turn` against the underlying SDK/CLI. |
| 2 | Harness wrap | `omnigent/inner/<name>_harness.py` | Exports `create_app() -> FastAPI`; reads `HARNESS_<NAME>_*` env vars and builds an `ExecutorAdapter`. |
| 3 | Registry entry | `omnigent/runtime/harnesses/__init__.py` | Maps the harness name to the wrap module path. |
| 4 | Optional dependency | `pyproject.toml` | An opt-in `[extra]` so a bare install stays lean; the wrap imports the SDK lazily. |
| 5 | Spec field | `executor.harness: <name>` in an agent YAML | Selects the harness at run time. |

Steps 1 and 2 are the only ones with real logic. The rest are wiring.

> **Harness vs. executor naming.** The `*_harness.py` module is the thin
> FastAPI wrap; the `*_executor.py` module is the SDK bridge it drives. This
> matters because the runner serves *harnesses*, but the model loop lives in
> the *executor*. Swapping which SDK backs a harness is a one-module change.

## 1. The executor bridge

Subclass `omnigent.inner.executor.Executor` and implement `run_turn`, which
yields a stream of `ExecutorEvent`s — most commonly `TextChunk`,
`ReasoningChunk`, `ToolCallRequest`, `TurnComplete`, and `ExecutorError`
(`ToolCallComplete` and `TurnCancelled` also exist; `ToolCallComplete` is emitted
by the Session layer but may be yielded by an executor for observed tool calls
the inner SDK already ran natively). Everything else on the base class
(`close_session`, `interrupt_session`, `enqueue_session_message`, `close`) has a
no-op default you override only if your backend keeps per-session state —
`enqueue_session_message` is the one to override if you want mid-turn steering
(the adapter's injection watcher relies on it).

The signature is fixed:

```python
from omnigent.inner.executor import (
    Executor, ExecutorConfig, ExecutorEvent, Message, ToolSpec,
)

class MySdkExecutor(Executor):
    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ):  # -> AsyncIterator[ExecutorEvent]
        ...
```

`CursorExecutor` (`omnigent/inner/cursor_executor.py`) is a good template: it
holds one persistent SDK client per conversation, issues one SDK call per
turn, and translates the SDK's streamed messages into `ExecutorEvent`s. It
also shows how to bridge Omnigent's spec-declared tools back into the SDK
in-process via a `_tool_executor` callback (so the agent can call `sys_*`
tools, orchestrate sub-agents, and respect policies — first-party parity).
`CodexExecutor` and `PiExecutor` show the CLI-binary variant, including path
discovery and gateway/auth wiring.

## 2. The harness wrap

The wrap is a thin module that exports `create_app() -> FastAPI`. It does not
implement the model loop — it builds an `ExecutorAdapter` around a *factory*
that constructs your executor lazily on the first turn. Lazy construction
matters: a missing SDK install or binary should surface as a request-time
error, not a FastAPI app-boot crash.

Configuration flows in via env vars the parent process sets before spawning the
subprocess (not via the request body). The convention is `HARNESS_<NAME>_*`,
centralized as module-level constants so a misconfiguration is a single grep
target. Here is the full shape, abridged from `cursor_harness.py`:

```python
from __future__ import annotations
import os
from fastapi import FastAPI
from omnigent.inner.my_sdk_executor import MySdkExecutor
from omnigent.inner.executor import Executor
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

_ENV_MODEL = "HARNESS_MYSDK_MODEL"
_ENV_CWD = "HARNESS_MYSDK_CWD"
_ENV_API_KEY = "HARNESS_MYSDK_API_KEY"

def _build_my_sdk_executor() -> Executor:
    # Called lazily by the adapter on the first turn.
    return MySdkExecutor(
        cwd=os.environ.get(_ENV_CWD) or None,
        model=os.environ.get(_ENV_MODEL) or None,
        api_key=os.environ.get(_ENV_API_KEY) or None,
    )

def create_app() -> FastAPI:
    adapter = ExecutorAdapter(executor_factory=_build_my_sdk_executor)
    return adapter.build()
```

`ExecutorAdapter` (`omnigent/runtime/harnesses/_executor_adapter.py`) handles
everything the wraps share: lazy executor construction, per-turn translation of
Omnigent requests into inner `Message` lists + `ExecutorConfig`, translation of
`ExecutorEvent`s into typed SSE events, forwarding spec-declared tools + wiring
the `_tool_executor` callback, cancellation propagation, and per-conversation
cleanup. You do not reimplement any of that — you only supply the factory.

Two env-var helpers recur across the existing wraps and are worth copying
verbatim so operators learn one set of conventions:

- `_resolve_os_env()` decodes `HARNESS_<NAME>_OS_ENV` (a JSON-encoded `OSEnvSpec`,
  serialized via `dataclasses.asdict`) and falls back to
  `caller_process + sandbox=none` when unset. See `cursor_harness._resolve_os_env`.
- `_resolve_skills_filter()` decodes `HARNESS_<NAME>_SKILLS_FILTER`
  (`"all" | "none" | list[str]`, JSON) and falls back to `"all"`. See
  `codex_harness._resolve_skills_filter`.

Boolean env vars use the shared `_TRUTHY_STRINGS = ("1", "true", "yes")`
convention with a `_parse_truthy(env_var, default)` helper (see
`codex_harness._parse_truthy`).

## 3. Register the harness name

Add one line to `_HARNESS_MODULES` in `omnigent/runtime/harnesses/__init__.py`:

```python
_HARNESS_MODULES: dict[str, str] = {
    ...
    "my-sdk": "omnigent.inner.my_sdk_harness",
}
```

The key is what users write in `executor.harness`; the value is the importable
module path that exports `create_app()`. The runner (`_runner.py`) receives the
module path from the parent process and imports it — the registry is the single
source of truth in the parent. Aliases are fine (the registry has `"claude"`
pointing at the same module as `"claude-sdk"`); add one if users will know your
harness by two names.

## 4. Add the optional dependency

If your harness pulls in a third-party SDK, declare an opt-in extra in
`pyproject.toml` so a bare `omnigent` install stays lean, then import the SDK
lazily inside the executor (not at module top level). The `cursor` extra is the
model to follow:

```toml
[project.optional-dependencies]
# MySDK harness (`harness: my-sdk`). The harness module imports the SDK
# lazily on first turn, so only `--harness my-sdk` users need this extra.
my-sdk = ["my-sdk>=0.1,<1"]
```

Installers then opt in with `uv sync --extra my-sdk` (or `pip install
"omnigent[my-sdk]"`). The existing wraps mark these extras in their module
docstrings; do the same so the install hint is discoverable.

Two existing extras are no-op aliases retained for backward compatibility
(`claude-sdk`, `openai-agents`); you do not need those for a new harness.

## 5. Point a spec at it

Select the harness with the `executor.harness` field in an agent YAML:

```yaml
name: my_agent
prompt: |
  You are a concise assistant.

executor:
  harness: my-sdk
  model: my-sdk-flagship
```

Start from a bundled example such as `examples/polly/config.yaml` or
`examples/debby/config.yaml` and trim what you don't need. See
[Agent YAML spec](AGENT_YAML_SPEC.md) for the full field reference.

## How to test it

The test suite injects registry entries by direct dict mutation, so you can
test a harness without landing it in the registry first. The existing harness
tests (`tests/inner/test_cursor_harness.py` is the smallest) cover three
things worth copying:

1. **Registry entry.** Assert the name maps to your module:
   `assert _HARNESS_MODULES.get("my-sdk") == "omnigent.inner.my_sdk_harness"`.
2. **App shape.** Call `create_app()` and assert the required routes exist
   (`/health` and `/v1/sessions/{conversation_id}/events`).
3. **Env-var → executor wiring.** Patch your executor's `__init__` to capture
   kwargs, set `HARNESS_<NAME>_*` env vars via `monkeypatch.setenv`, call the
   factory, and assert the kwargs resolved as expected (including the default
   `os_env = caller_process + sandbox=none` when unset).

For an end-to-end dispatch test (spawning the subprocess), see
`tests/runner/test_runner_dispatch.py`: it injects a test-only harness via
`_HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE` and pops it on
teardown so it doesn't leak into other tests.

## Validation tips

- Keep the wrap thin. Behavior goes in the executor; the wrap only reads env
  vars and hands the adapter a factory.
- Construct the executor lazily (inside the factory, not at module import), so
  a missing SDK/binary fails on the first turn rather than at app boot.
- Reuse the shared env-var helpers (`_resolve_os_env`, `_resolve_skills_filter`,
  `_parse_truthy`) so operators learn one set of conventions across harnesses.
- Import the third-party SDK inside the executor, not at the wrap's top level,
  and pair that with an opt-in `pyproject.toml` extra.
- Run the spec before publishing:

  ```bash
  omnigent run path/to/agent.yaml -p "Say hello"
  ```