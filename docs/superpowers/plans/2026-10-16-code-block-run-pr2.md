# Code Block Run PR 2 (Runner and Server) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the session owner start and stop a code run through the server, run it on the runner in a dedicated sandbox helper that streams output, persist the `started` and `finished` items, and stream live output, all behind the `code_block_run` flag.

**Architecture:** A new helper op `shell_stream` streams a command's output as JSON lines from a per-run sandbox helper. A runner `CodeRunRegistry` owns active runs keyed by `(session_id, run_id)`, coalesces output into `session.code_run.output` events on the existing per-session runner stream, and posts the `finished` item through `external_conversation_item` with a `source_id`. The server routes run the spec's checks in order, evaluate TOOL_CALL policy as `sys_os_shell` with a fresh engine, forward to the runner, append the `started` item, and publish typed events; the subscribe snapshot asks the runner for running runs.

**Tech Stack:** Python 3.12, FastAPI, httpx, pydantic v2, pytest (asyncio), bubblewrap.

**Spec:** `docs/superpowers/specs/2026-10-16-code-block-run-design.md` (sections "Server API", "Runner", "Live updates", "Permissions"; flag and audit).

## Global Constraints

- Flag key `code_block_run`, off by default, frontend visible, exposed in `/v1/info` `features`.
- New routes return 404 when the flag is off and require a JSON content type on mutating requests.
- Start checks, in order: flag 404; owner (usual access error); `language` in `shell | python | node` else 400; `code` non-empty, at most 64 KiB, and `block_hash` is its SHA-256 hex, else 400; `code` appears verbatim in the stored assistant message `message_id`, else 400; no copy-on-write roots, else 409; `timeout` 1 to 600, default 120, else 400; fewer than 4 running runs, else 429.
- Command derivation: shell strips leading `$ ` and `% ` prompts; `console` blocks drop non-prompt lines; Python and Node pass through.
- Policy: TOOL_CALL, tool `sys_os_shell`, arguments `{"command", "timeout"}`, actor `{"run_as": <owner>, "initiated_by": "user"}`. DENY 403 `{"code": "policy_denied", "reason"}`; ASK without `approved` 409 `{"code": "needs_approval", "reason"}`; ASK with `approved` proceeds, `approved_by` = owner.
- Success: forward to runner, append `started` item, 202 `{"run_id"}`. Stop: proxied like terminal close; unknown or finished run returns 200.
- Runner: at most 4 running per session; output events about every 200 ms or 16 KiB; bounded head+tail copy up to `CODE_RUN_OUTPUT_MAX_CHARS` (100,000); `finished` posted with deterministic `source_id`, retried with backoff for up to 5 minutes; `cleanup_session` stops a session's runs.
- Events: `session.code_run.started` (saved item), `session.code_run.output` `{run_id, offset, stream, text}`, `session.code_run.finished` (saved item), `session.code_run.snapshot` `{run_id, offset, output}`.
- Audit: start adds run id, language, command length, policy decision, `approved_by`; stop adds run id; the finished post adds exit code, status, duration, truncated.
- Out of scope: agent delivery (PR 3), all web UI including the web `FeatureKey` (PR 4), the existing `/shell` route.
- Every commit `git commit -s`. No U+2014 in added lines. Never commit a rewritten `uv.lock`.

## File Structure

- Modify `omnigent/server/feature_flags.py`: `Feature.CODE_BLOCK_RUN` + definition.
- Modify `omnigent/inner/os_env.py`: `shell_stream` helper op, `_HelperProcessClient.stream` / `send_control`, `CallerProcessOSEnvironment.shell_stream` / `stop_shell_stream`.
- Modify `omnigent/runner/tool_dispatch.py`: extract `_dedicated_os_environment` and `_needs_shared_environment` from `_execute_os_env_tool` so code runs build their helper the same way.
- Create `omnigent/runner/code_runs.py`: `CodeRunOutput`, `CodeRunRegistry`, `register_code_run_routes`.
- Modify `omnigent/runner/resource_registry.py`: `set_code_run_stopper`, called from `cleanup_session`.
- Modify `omnigent/runner/app.py`: build the registry and register routes.
- Modify `omnigent/server/schemas.py`: four event classes in `ServerStreamEvent`.
- Modify `omnigent/server/routes/_sessions/helpers.py`: `_publish_code_run_item`, branch in `_publish_external_conversation_item`.
- Create `omnigent/server/routes/sessions/routes_code_runs.py`: start and stop routes, command derivation, policy evaluation.
- Modify `omnigent/server/routes/sessions/__init__.py`: register routes.
- Modify `omnigent/server/routes/sessions/routes_events.py`: snapshot on subscribe.
- Regenerate `openapi.json`.
- Tests: `tests/server/test_feature_flags.py`, `tests/server/integration/test_utility_endpoints.py`, `tests/inner/test_os_env.py`, `tests/inner/sandbox/test_sandbox_behavior.py`, new `tests/runner/test_code_runs.py`, `tests/server/test_stream_events.py`, new `tests/server/routes/test_code_runs_routes.py`.

---

### Task 1: `code_block_run` feature flag

**Files:**
- Modify: `omnigent/server/feature_flags.py`
- Test: `tests/server/test_feature_flags.py`, `tests/server/integration/test_utility_endpoints.py`

**Interfaces:**
- Produces: `Feature.CODE_BLOCK_RUN == "code_block_run"`; `request.app.state.feature_flags.enabled(Feature.CODE_BLOCK_RUN)`.

- [ ] **Step 1: Write the failing test**

```python
def test_code_block_run_is_a_frontend_visible_feature() -> None:
    flags = resolve_feature_flags({FEATURES_ENV_VAR: "code_block_run"})

    assert flags.enabled(Feature.CODE_BLOCK_RUN)
    assert flags.frontend_dict()["code_block_run"] is True
```

Add `"code_block_run": False` to the expected `features` dict in `tests/server/integration/test_utility_endpoints.py`.

- [ ] **Step 2: Run to verify it fails**: `.venv/bin/python -m pytest tests/server/test_feature_flags.py -q` fails with `AttributeError: CODE_BLOCK_RUN`.
- [ ] **Step 3: Implement**

```python
    CODE_BLOCK_RUN = "code_block_run"
...
    FeatureDefinition(
        feature=Feature.CODE_BLOCK_RUN,
        description="Run shell, Python, and Node code blocks from agent messages",
        owner="web",
        review_by_release="0.22.0",
    ),
```

- [ ] **Step 4: Run** both test files; PASS.
- [ ] **Step 5: Commit** `feat(server): add code_block_run feature flag (#10519)`.

---

### Task 2: `shell_stream` helper op

**Files:**
- Modify: `omnigent/inner/os_env.py`
- Test: `tests/inner/test_os_env.py`, `tests/inner/sandbox/test_sandbox_behavior.py`

**Interfaces:**
- Produces:
  - Helper request `{"op": "shell_stream", "command": str, "language": "shell" | "python" | "node", "timeout": int}`; the helper writes `{"stream": "stdout" | "stderr", "text": str}` lines, then a final `{"exit_code": int | None, "timed_out": bool, "truncated": bool}` (or `{"error": str}`). A `{"op": "stop"}` line (or EOF) on the helper's stdin during the run kills the process group.
  - `async CallerProcessOSEnvironment.shell_stream(command: str, *, language: str, timeout: int, on_output: Callable[[str, str], None]) -> OpResult` (``on_output(stream, text)`` is called on a worker thread).
  - `CallerProcessOSEnvironment.stop_shell_stream() -> None`.

- [ ] **Step 1: Write the failing tests** (sandbox `none`, real helper):

```python
def _stream(os_env, command, *, language="shell", timeout=30, stop_after=None):
    chunks: list[tuple[str, str]] = []

    def on_output(stream: str, text: str) -> None:
        chunks.append((stream, text))
        if stop_after is not None and stop_after in text:
            os_env.stop_shell_stream()

    result = asyncio.run(
        os_env.shell_stream(command, language=language, timeout=timeout, on_output=on_output)
    )
    return result, chunks


def test_shell_stream_emits_stdout_and_stderr_in_order(tmp_path: Path) -> None:
    os_env = _none_env(tmp_path)
    try:
        result, chunks = _stream(os_env, "echo out; sleep 0.2; echo err >&2; sleep 0.2; echo done")
    finally:
        os_env.close()
    assert result == {"exit_code": 0, "timed_out": False, "truncated": False}
    assert [stream for stream, _ in chunks] == ["stdout", "stderr", "stdout"]
    assert "".join(text for _, text in chunks) == "out\nerr\ndone\n"


def test_shell_stream_feeds_python_code_on_stdin(tmp_path: Path) -> None: ...
    # language="python", command "import sys\nprint(sys.argv)" -> stdout "['-']\n"

def test_shell_stream_timeout_kills_process_group(tmp_path: Path) -> None: ...
    # "sleep 30 & echo $! > child.pid; wait" with timeout=1 -> timed_out True,
    # exit_code None, and the background pid is gone.

def test_shell_stream_stop_kills_process_group(tmp_path: Path) -> None: ...
    # "echo ready; sleep 30" stopped on "ready" -> exit_code None, timed_out False,
    # returns in well under 30 s.

def test_shell_stream_output_cap_sets_truncated(...) -> None: ...
    # monkeypatch helper cap via max_output in the request -> truncated True.
```

Add a bwrap-backed variant in `tests/inner/sandbox/test_sandbox_behavior.py` using `active_sandbox_spec_factory`: stop kills a background descendant.

- [ ] **Step 2: Run to verify they fail** (`AttributeError: shell_stream`).
- [ ] **Step 3: Implement** in the helper: `_shell_stream_argv` (`_shell_argv` for shell, `["python3", "-"]`, `["node", "-"]`), `Popen(..., cwd, env=_child_shell_env(), stdin=PIPE or DEVNULL, start_new_session=True)`; a writer thread feeds code and closes stdin; a `selectors` loop reads stdout/stderr with incremental UTF-8 decoders and the helper's own stdin for `stop`; deadline and stop call `os.killpg(pgid, SIGKILL)`; after the child exits and pipes reach EOF, return the final dict. Client side: `stream()` writes the request under the lock and reads lines until a line without `stream`; `send_control()` writes a line without the lock. `stop_shell_stream()` sends `{"op": "stop"}`.
- [ ] **Step 4: Run** `tests/inner/test_os_env.py` and `tests/inner/sandbox`; PASS.
- [ ] **Step 5: Commit** `feat(os_env): add streaming shell_stream helper op (#10519)`.

---

### Task 3: Runner run registry and routes

**Files:**
- Create: `omnigent/runner/code_runs.py`
- Modify: `omnigent/runner/tool_dispatch.py`, `omnigent/runner/resource_registry.py`, `omnigent/runner/app.py`
- Test: `tests/runner/test_code_runs.py`

**Interfaces:**
- Consumes: Task 2 `shell_stream` / `stop_shell_stream`; `CODE_RUN_OUTPUT_MAX_CHARS`.
- Produces:
  - `CodeRunOutput(limit=CODE_RUN_OUTPUT_MAX_CHARS)`: `.append(stream, text)`, `.segments() -> list[dict]`, `.total: int`, `.truncated: bool`.
  - `CodeRunRegistry(publish_event, server_client, environment_factory, *, sleep=asyncio.sleep, finish_post_deadline_s=300.0)`; `start(session_id, run_id, language, command, timeout)` raising `CodeRunLimitError`; `stop(session_id, run_id) -> bool`; `running(session_id) -> list[dict]` (`{run_id, offset, output}`); `async stop_session(session_id)`; `async wait(session_id, run_id)`.
  - Runner routes: `POST /v1/sessions/{sid}/code-runs` body `{run_id, language, command, timeout}` -> 202 `{run_id}`, 409 `{"code": "copy_on_write"}`, 429 `{"code": "too_many_runs"}`; `POST .../{run_id}/stop` -> 200 `{"stopped": bool}`; `GET .../code-runs?status=running` -> `{"object": "list", "data": [...], "copy_on_write": bool}`.
  - Output event `{"type": "session.code_run.output", "run_id", "offset", "stream", "text"}`; finished post body `{"type": "external_conversation_item", "data": {"item_type": "code_run", "item_data": {...finished...}, "response_id": run_id, "source_id": f"code_run:{run_id}:finished"}}`.
  - `tool_dispatch._effective_runner_os_env_spec` reuse via `_dedicated_os_environment(agent_spec, conversation_id, runner_workspace, resource_registry)` and `_needs_shared_environment(effective_spec)`.

- [ ] **Step 1: Write the failing tests** with a fake environment (records `stop_shell_stream`, emits scripted chunks) and a recording server client:
  - output buffer keeps start and end, marks truncated past the cap;
  - chunks coalesce into one event under 16 KiB inside 200 ms, and a 16 KiB burst flushes immediately; offsets are cumulative;
  - fifth concurrent run raises `CodeRunLimitError`;
  - `finished` post retries after `httpx.ConnectError` and a 503 with the same `source_id`, then stops;
  - `stop` on unknown run returns `False`; `stop` on a running run yields status `stopped`;
  - `running()` lists output so far; a finished run is not listed;
  - `cleanup_session` on `SessionResourceRegistry` stops the session's runs;
  - routes: 202, 409 copy-on-write, 429, stop 200 for unknown;
  - a run survives a dropped viewer stream: open `GET /v1/sessions/{sid}/stream` on a real runner app, read the first frame, close it, and the run still posts `finished` (real `none` sandbox helper).
- [ ] **Step 2: Run to verify they fail** (`ModuleNotFoundError: omnigent.runner.code_runs`).
- [ ] **Step 3: Implement** the module, refactor `_execute_os_env_tool` onto `_dedicated_os_environment`, wire the registry in `create_runner_app` (factory resolves `_resolve_session_agent_spec` and `_session_runtime_cwd`), and call the stopper from `cleanup_session`.
- [ ] **Step 4: Run** `tests/runner/test_code_runs.py` plus `tests/runner/test_resource_registry.py tests/runner/test_copy_on_write.py tests/runner/test_session_resources.py` and tunnel streaming tests; PASS.
- [ ] **Step 5: Commit** `feat(runner): run code blocks in a per-run sandbox helper (#10519)`.

---

### Task 4: Stream events and item publishing

**Files:**
- Modify: `omnigent/server/schemas.py`, `omnigent/server/routes/_sessions/helpers.py`, `openapi.json`
- Test: `tests/server/test_stream_events.py`

**Interfaces:**
- Produces: `SessionCodeRunStartedEvent(type, item)`, `SessionCodeRunOutputEvent(type, run_id, offset, stream, text)`, `SessionCodeRunFinishedEvent(type, item)`, `SessionCodeRunSnapshotEvent(type, run_id, offset, output: list[CodeRunOutputSegment])`; `_publish_code_run_item(session_id, item: ConversationItem) -> None` publishes `session.code_run.<phase>` and, for `finished`, adds audit attrs `code_run_id`, `code_run_status`, `code_run_exit_code`, `code_run_duration_ms`, `code_run_truncated`. `_publish_external_conversation_item` routes `code_run` items there instead of `response.output_item.done`.

- [ ] **Step 1: Write failing tests**: each event round-trips through `TypeAdapter(ServerStreamEvent)`; an external `code_run` finished item publishes `session.code_run.finished` (patched `session_stream.publish`) and no `response.output_item.done`.
- [ ] **Step 2: Run to verify they fail.**
- [ ] **Step 3: Implement** and regenerate `openapi.json` with `.venv/bin/python scripts/dump_openapi.py`.
- [ ] **Step 4: Run** `tests/server/test_stream_events.py tests/server/test_schemas.py tests/server/test_openapi_drift.py`; PASS.
- [ ] **Step 5: Commit** `feat(server): add session.code_run stream events (#10519)`.

---

### Task 5: Server start and stop routes

**Files:**
- Create: `omnigent/server/routes/sessions/routes_code_runs.py`
- Modify: `omnigent/server/routes/sessions/__init__.py`, `openapi.json`
- Test: `tests/server/routes/test_code_runs_routes.py`

**Interfaces:**
- Consumes: Task 1 flag, Task 3 runner routes, Task 4 `_publish_code_run_item`.
- Produces: `derive_code_run_command(language: str, code: str, *, console: bool) -> str`; `is_console_block(message_text: str, code: str) -> bool`; `register_code_runs_routes(router, *, conversation_store, agent_store, auth_provider, permission_store)`; module seam `_evaluate_run_policy(...) -> PolicyResult | None`.

- [ ] **Step 1: Write failing tests** reusing the stubs shape of `test_shell_permission_gate.py` (stores, recording runner client, `UnifiedAuthProvider(source="header", local_single_user=False)`), with the flag set on `application.state.feature_flags`:
  - flag off 404 before auth; read-only, edit, unauthenticated refused before the runner is called; owner and admin accepted;
  - language 400, empty/oversize code 400, hash mismatch 400, code not in message 400, non-assistant message 400;
  - copy-on-write 409 (runner preflight), timeout 0/601 400, default 120, 4 running 429;
  - derivation: `$ ` and `% ` stripped; console drops output lines; Python unchanged;
  - DENY 403 `policy_denied`, ASK 409 `needs_approval`, approved ASK 202 with `approved_by`; actor carries `initiated_by: "user"`; a real engine with no policies allows;
  - success appends one `started` item, publishes `session.code_run.started`, forwards `{run_id, language, command, timeout}`;
  - stop proxies to the runner and returns 200 for unknown runs; stop refuses non-owners.
- [ ] **Step 2: Run to verify they fail** (404 on the unregistered route).
- [ ] **Step 3: Implement** the routes with manual parsing so the checks run in order; reuse `_require_access_and_level`, `_get_runner_client_for_resource_access`, `build_policy_engine`, `any_policies_apply`, `_build_evaluation_context`; runner calls use the runner client directly with explicit short timeouts (they return immediately); add audit attrs; regenerate `openapi.json`.
- [ ] **Step 4: Run** the new test file plus `tests/server/routes/test_shell_permission_gate.py`; PASS.
- [ ] **Step 5: Commit** `feat(server): add code-runs start and stop routes (#10519)`.

---

### Task 6: Snapshot on subscribe

**Files:**
- Modify: `omnigent/server/routes/sessions/routes_events.py`
- Test: `tests/server/routes/test_code_runs_routes.py`

**Interfaces:**
- Consumes: runner `GET .../code-runs?status=running`.
- Produces: one `session.code_run.snapshot` per running run in `_resource_snapshot` when the flag is on.

- [ ] **Step 1: Write the failing test**: subscribe hook emits one snapshot per listed run, none when the flag is off.
- [ ] **Step 2: Run to verify it fails.**
- [ ] **Step 3: Implement** a time-boxed GET beside the terminals gather.
- [ ] **Step 4: Run**; PASS.
- [ ] **Step 5: Commit** `feat(server): replay running code runs on stream subscribe (#10519)`.
