# OpenCode v2 Native Harness Implementation Plan

> **Implemented.** Kept as the record of how the harness was built. Stage 0's
> recon script and `recon-findings.md` were throwaway and are not in the repo;
> the findings are in the [design](opencode-v2-native-harness.md).

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `opencode-native` harness drive OpenCode 2.0.x (replacing 1.17/1.18 support) with live text, reasoning, and tool-output streaming.

**Architecture:** The runner still spawns one private `opencode serve` per conversation, an SSE forwarder mirrors OpenCode events into the Omnigent session, and a typed HTTP client injects web turns. Every seam that touches the OpenCode wire (client, process launcher, prompt payload, forwarder handler table, `opencode.json` builder, policy plugin, permission parser, credential copy, resume/fork/compact dispatch, session import) is rewritten for the v2 contract; the Omnigent-side contracts (`external_*` session events, policy evaluator, elicitation hooks, state.json) are reused.

**Tech Stack:** Python 3.12 (`httpx`, `asyncio`, FastAPI), pytest with `httpx.MockTransport` fakes, OpenCode `@opencode/cli` 2.0.x (Bun binary, HTTP + SSE at `/api/*`, ES-module plugins via `@opencode/plugin`), tmux for the TUI leg.

**Spec:** `designs/opencode-v2-native-harness.md`

## Global Constraints

- Version gate: `OPENCODE_MIN_VERSION = "2.0.0"`, `OPENCODE_MAX_VERSION_EXCLUSIVE = "3.0.0"`; `OMNIGENT_OPENCODE_SKIP_VERSION_CHECK` escape hatch is kept.
- Install pin: `@opencode/cli@~2.0.18`, binary `opencode`, login step `opencode auth login`.
- Launch: `opencode serve --hostname 127.0.0.1 --port <port> --stdio`; `OPENCODE_PASSWORD` and `OPENCODE_SERVER_PASSWORD` both set to the per-session secret; `OPENCODE_DB` under the bridge dir; readiness `GET /api/info`.
- One `opencode serve` process per Omnigent conversation; `--service` mode is never used.
- TUI leg: `opencode --server <url> --session <ses_id> <workspace>`; no `attach`.
- All HTTP responses unwrap `body["data"]`; SSE frames are `data: <json>` with `{id, type, data, location}` and `: heartbeat` comments.
- Permission replies use `decision` in `{"once", "reject"}`; never `"always"`.
- `opencode.json` always carries `permissions: [{"action": "*", "resource": "*", "effect": "ask"}]`; the relay MCP entry sets `"codemode": false`.
- v1 code paths are deleted, not kept behind a switch.
- Comments are short and describe the scenario; no PR or issue numbers.
- Every task runs its tests with `uv run pytest ...`; pre-commit (`uvx pre-commit run --files ...`) must pass before each commit.
- Stages 1 through 4 land as one PR (v1 deletions in Stage 1 break forwarder/runner call sites that Stages 2 and 4 rewrite). The commit steps that say `SKIP=pyrefly` are the only ones allowed to skip the type check, and only while `.venv/bin/pyrefly check` reports errors solely at the ledger lines listed in Stage 1; Stage 4's last task and Stage 5 must commit with the full hook set green.
- Task numbering is global across stages (1-4, 10-22, 23-46, 47-63, 64-82, 83-91); a "Stage N" mention names the stage's task range above.

## Review Focus

1. **SSE heartbeat and reconnect:** a `: heartbeat` comment or a dropped connection mid-turn must not produce a spurious idle or lose the text buffered since the last `session.text.ended`. Test in the client parser task and the forwarder reconnect task.
2. **Interleaved ordinals:** two `session.text.delta` streams with different `ordinal` values in one step (text, tool, text) must flush as separate assistant messages in order, not concatenate. Test in the forwarder text task.
3. **Permission reply failure:** a non-2xx from `/permission/{rid}/reply` must be surfaced (status forward) rather than swallowed, since OpenCode blocks the turn until answered. Test in the forwarder permission task.
4. **Form field types:** a `form.created` with a `boolean` or `number` field, or a `string` field without `options`, must render a usable card and map the answer back typed (`true`, `3`, free text), not fail on a missing options list. Test in the forwarder form task.
5. **Credential absence:** a user with only v2 SQLite credentials and no `auth.json` must get a clear readiness hint (`opencode auth login`) rather than a server that boots and then fails the first turn with a provider auth error. Test in the readiness task.

---
## Stage 0: Recon spike + committed fixtures

Produces the fixtures every later stage's tests are built from:
`tests/fixtures/opencode_v2/{openapi.json,events.ndjson,messages.json,recon-findings.md}`
and the `tests/opencode_v2_fixtures.py` loader. Nothing in `omnigent/` is
touched in this stage — it is pure tooling + fixtures + a fixture-integrity
test.

Ground truth used below (verified against the extracted `v2.0.18` source and
the installed CLI on this machine):

- `opencode serve --help`: flags are `--hostname string`, `--port integer`,
  `--cors string`, `--service`, `--stdio` (no defaults printed; omitting
  `--port` lets the server bind an ephemeral port).
- `packages/cli/src/server-process.ts:163`: in `--stdio` mode the process
  prints exactly one JSON line, `{"url": "<base_url>"}`, to stdout once
  bound, then blocks until stdin closes (`waitForStdinClose`, line ~200).
- `packages/cli/src/env.ts:10-11`: the server password comes from
  `OPENCODE_PASSWORD`, falling back to `OPENCODE_SERVER_PASSWORD`.
- `packages/server/src/middleware/authorization.ts:17-27`: auth is HTTP
  Basic, `base64("opencode:" + password)` (or `?auth_token=` query with the
  same base64 blob) — matches
  `omnigent/harnesses/opencode_native/bridge.py`'s existing
  `OPENCODE_DEFAULT_USERNAME = "opencode"` / `auth_headers_for_secret`.
- `packages/protocol/openapi.json` `paths`: confirms every endpoint used
  below exists exactly as named: `GET /openapi.json`, `GET /api/info`,
  `GET /api/event`, `POST /api/session`, `POST /api/session/{sessionID}/prompt`,
  `POST /api/session/{sessionID}/synthetic`,
  `POST /api/session/{sessionID}/compact`,
  `POST /api/session/{sessionID}/permission/{requestID}/reply`,
  `POST /api/session/{sessionID}/form/{formID}/reply`,
  `GET /api/session/{sessionID}/message`.
- Request bodies (from the same `openapi.json`, `requestBody` per path):
  - `POST /api/session`: `{id?, title?, agent?, model?: {id, providerID, variant?}, location?: {directory}, metadata?, permissions?}`.
  - `POST .../prompt`: `{id?, text, files?: [{uri, name?, description?, mention?}], agents?, skills?, metadata?, delivery?: "steer"|"queue", resume?: boolean}`, `text` required.
  - `POST .../synthetic`: `{id?, text, description?, metadata?, delivery?, resume?}`, `text` required.
  - `POST .../compact`: `{id?, delivery?}`, no `model` field.
  - `POST .../permission/{requestID}/reply`: `{decision: "once"|"always"|"reject", message?}`.
  - `POST .../form/{formID}/reply`: `Form.Reply = {answer: Form.Answer}` where `Form.Answer` is `Record<string, Form.Value>`.
  - `GET .../message` response: `SessionMessagesResponse = {data: Session.Message.Info[], cursor: {previous, next}}`.
- `packages/schema/src/config.ts`: `Config.Info` has top-level
  `permissions: Permission.Ruleset` (array), `mcp: {servers: Record<string, ServerConfig>}`,
  `plugins: Array<string | {package, options?}>`, and
  `instructions: Schema.String.pipe(Schema.Array, optional)` — **an array of
  paths/URLs**, not a raw string. The recon script therefore writes the
  marker text to a file and puts that file's path in the `instructions`
  array (this is exactly open item 1: whether that file's contents actually
  reach the model as system context).
- `packages/schema/src/mcp.ts` `LocalConfig`: `{type: "local", command: string[], cwd?, environment?, disabled?, codemode?, timeout?, protocol?}`.
- `packages/schema/src/permission.ts`: `Permission.Request = {id, sessionID, action, resources: string[], save?, metadata?, source?, message?}`; `Reply = "once" | "always" | "reject"`; `Rule = {action, resource, effect: "allow"|"deny"|"ask"}`.
- `packages/schema/src/event.ts` `PayloadBase`: every `/api/event` frame decodes to `{id, type, created, data, location?, metadata?}` plus `durable?` for durable event types — matches the cross-stage fixture contract exactly.
- `packages/plugin/src/promise/plugin.ts`: `Plugin.define({id, setup(ctx)})`, `setup` returns an optional cleanup. `packages/plugin/src/promise/session.ts` confirms `ctx.session.hook("prompt", ...)`; `packages/plugin/src/promise/tool.ts` confirms `ctx.tool.hook("execute.after", ...)`; `packages/plugin/src/promise/permission.ts` confirms `ctx.permission.hook("evaluate", ...)`.
- `opencode session list --help`: flags are `--standalone`, `--server string`, `--max-count/-n integer`, `--format choice (table|json)` — so `opencode session list --server <url> --format json` is the exact probe for open item 6.
- Reused, unmodified, already-committed test fixture:
  `tests/tools/fixtures/echo_stdio_mcp_server.py` (a `FastMCP("echo-test")`
  server with one tool, `echo(text: str) -> str`) is used as the "trivial
  local stdio MCP server" the brief asks for — no need to author a new one.
- Reusable existing helper, read-only reference (not imported, to keep this
  stage's script decoupled from harness code Stage 1 rewrites):
  `omnigent/harnesses/opencode_native/bridge.py:280-289`,
  `auth_headers_for_secret(secret) -> dict[str, str]`, builds the same
  `Authorization: Basic base64("opencode:" + secret)` header the recon
  script needs; the script reimplements the two-line base64 call directly
  instead of importing, per the design note above.

---

### Task 1: Recon script — argument parsing, config synthesis, redaction

**Files:**
- Create: `dev/opencode_v2_recon.py`
- Test: `tests/dev/test_opencode_v2_recon.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (first task of Stage 0).
- Produces: `build_arg_parser() -> argparse.ArgumentParser`,
  `redact_secrets(text: str, secrets_to_redact: list[str]) -> str`,
  `build_recon_opencode_config(*, instructions_path: Path, mcp_server_command: list[str], plugin_path: Path) -> dict[str, Any]`,
  module constants `ASK_ALL_PERMISSIONS`, `_INSTRUCTIONS_MARKER`,
  `_PLUGIN_TEMPLATE` — all consumed by Task 2 in the same file.

- [ ] **Step 1: Write the failing test**

```python
"""Tests for the pure (network-free) helpers in dev/opencode_v2_recon.py."""

from __future__ import annotations

from pathlib import Path

from dev.opencode_v2_recon import (
    ASK_ALL_PERMISSIONS,
    build_arg_parser,
    build_recon_opencode_config,
    redact_secrets,
)


def test_arg_parser_requires_model() -> None:
    parser = build_arg_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args([])


def test_arg_parser_defaults() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--model", "anthropic/claude-sonnet-4-5"])
    assert args.model == "anthropic/claude-sonnet-4-5"
    assert args.opencode_path is None
    assert args.keep_workdir is False
    assert args.out_dir == Path("tests/fixtures/opencode_v2").resolve() or args.out_dir.name == "opencode_v2"


def test_redact_secrets_replaces_every_occurrence() -> None:
    text = "password=hunter2 path=/tmp/hunter2/foo secret=hunter2"
    redacted = redact_secrets(text, ["hunter2"])
    assert "hunter2" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redact_secrets_prefers_longer_matches_first() -> None:
    text = "/tmp/opencode-v2-recon-abc123/nested"
    redacted = redact_secrets(text, ["/tmp/opencode-v2-recon-abc123", "abc123"])
    assert redacted == "<redacted>/nested"


def test_redact_secrets_ignores_blank_entries() -> None:
    assert redact_secrets("hello world", ["", None]) == "hello world"  # type: ignore[list-item]


def test_build_recon_opencode_config_shape(tmp_path: Path) -> None:
    instructions_path = tmp_path / "instructions.txt"
    plugin_path = tmp_path / "plugin.js"
    config = build_recon_opencode_config(
        instructions_path=instructions_path,
        mcp_server_command=["python3", "server.py"],
        plugin_path=plugin_path,
    )
    assert config["permissions"] == ASK_ALL_PERMISSIONS
    assert config["instructions"] == [str(instructions_path)]
    assert config["mcp"]["servers"]["recon-echo"] == {
        "type": "local",
        "command": ["python3", "server.py"],
        "codemode": False,
    }
    assert config["plugins"] == [str(plugin_path)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/dev/test_opencode_v2_recon.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'dev.opencode_v2_recon'`

- [ ] **Step 3: Write minimal implementation**

```python
#!/usr/bin/env python3
"""Recon spike for OpenCode v2: drive a real ``opencode serve`` server through
one turn and capture the wire fixtures the ``opencode-native`` v2 migration
(Stages 1-5) is built and tested from.

Boots a throwaway ``opencode serve --hostname 127.0.0.1 --port 0 --stdio`` in
an isolated ``XDG_DATA_HOME``/``XDG_CONFIG_HOME``, drives one turn that
provokes a shell tool call, an MCP tool call, a permission prompt, a question
form, and reasoning, auto-answers the permission/form prompts, runs
``/compact``, and writes everything captured to
``tests/fixtures/opencode_v2/``:

    openapi.json        GET /openapi.json
    events.ndjson        every decoded /api/event frame seen during the run
    messages.json         GET /api/session/{id}/message for that session
    recon-findings.md     yes/no answers to the six open design questions

Requires a real ``opencode`` CLI, >=2.0.0 <3.0.0, on PATH, and real model
credentials for the provider passed via --model (the harness calls a real
provider; there is no mock path). Run:

    uv run python dev/opencode_v2_recon.py --model anthropic/claude-sonnet-4-5

The generated server password and every temp path are redacted from the
fixtures before they're written.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
_FIXTURES_DIR = _REPO_ROOT / "tests" / "fixtures" / "opencode_v2"
# Reuses the existing echo MCP fixture (tests/tools/fixtures/echo_stdio_mcp_server.py)
# instead of authoring a new trivial stdio MCP server.
_ECHO_MCP_SERVER = _REPO_ROOT / "tests" / "tools" / "fixtures" / "echo_stdio_mcp_server.py"

# Written to a temp file and referenced from config `instructions`, to answer
# open item 1: whether v2's `instructions` (an array of paths/URLs per
# Config.Info in packages/schema/src/config.ts) reaches the model as system
# context, or a `synthetic` seed message is needed instead.
_INSTRUCTIONS_MARKER = "OMNIGENT_RECON_MARKER_1f6f6c6f-opencode-v2"

# v2 permission rules are {action, resource, effect}; "ask" on every action/
# resource forces every tool call to raise `permission.asked` so the recon
# run actually exercises the permission-reply path.
ASK_ALL_PERMISSIONS: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "ask"}]

# A minimal Plugin.define plugin (packages/plugin/src/promise/plugin.ts) that
# logs the three hooks the omnigent-policy plugin (Stage 3) will use:
# ctx.session.hook("prompt"), ctx.tool.hook("execute.after"), and
# ctx.permission.hook("evaluate") (packages/plugin/src/promise/{session,tool,permission}.ts).
_PLUGIN_TEMPLATE = '''\
import { Plugin } from "@opencode/plugin"
import { appendFileSync } from "node:fs"

const LOG_FILE = process.env.OMNIGENT_RECON_PLUGIN_LOG

function log(event, data) {
  if (!LOG_FILE) return
  appendFileSync(LOG_FILE, JSON.stringify({ event, data }) + "\\n")
}

export default Plugin.define({
  id: "omnigent-recon",
  setup: async (ctx) => {
    await ctx.session.hook("prompt", async (event) => {
      log("session.prompt", { sessionID: event.sessionID, messageID: event.messageID })
    })
    await ctx.tool.hook("execute.after", async (event) => {
      log("tool.execute.after", { tool: event.tool, sessionID: event.sessionID, status: event.status })
    })
    await ctx.permission.hook("evaluate", async (event) => {
      log("permission.evaluate", { sessionID: event.sessionID, action: event.action, resources: event.resources })
    })
  },
})
'''


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the recon script's argument parser."""
    parser = argparse.ArgumentParser(
        description=(
            "Drive one live turn against a real `opencode serve` v2 server "
            "and capture tests/fixtures/opencode_v2/. Requires real model "
            "credentials for --model's provider."
        ),
    )
    parser.add_argument(
        "--model",
        required=True,
        help="provider/model to prompt, e.g. anthropic/claude-sonnet-4-5",
    )
    parser.add_argument(
        "--opencode-path",
        default=None,
        help="Explicit opencode executable (default: resolve from PATH).",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=_FIXTURES_DIR,
        help="Directory the fixtures are written to.",
    )
    parser.add_argument(
        "--keep-workdir",
        action="store_true",
        help="Don't delete the temp XDG/workspace dirs on exit (for debugging).",
    )
    return parser


def redact_secrets(text: str, secrets_to_redact: list[str]) -> str:
    """
    Replace every occurrence of each secret in *text* with ``"<redacted>"``.

    Longer secrets are redacted first, so a short secret that happens to be a
    substring of a longer one (e.g. a random suffix nested inside a temp dir
    path) doesn't leave a partial match behind.

    :param text: Text to scrub.
    :param secrets_to_redact: Literal strings to remove (the generated server
        password, absolute temp paths); blank/``None`` entries are ignored.
    :returns: *text* with every secret replaced.
    """
    result = text
    for secret in sorted({s for s in secrets_to_redact if s}, key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    return result


def build_recon_opencode_config(
    *,
    instructions_path: Path,
    mcp_server_command: list[str],
    plugin_path: Path,
) -> dict[str, Any]:
    """
    Build the v2 ``opencode.json`` the recon server is launched with.

    :param instructions_path: File containing :data:`_INSTRUCTIONS_MARKER`;
        referenced from v2's ``instructions`` array
        (``Config.Info.instructions`` in ``packages/schema/src/config.ts``).
    :param mcp_server_command: Argv for a trivial local stdio MCP server
        (the recon run uses ``tests/tools/fixtures/echo_stdio_mcp_server.py``).
    :param plugin_path: Path to the recon test plugin (``Plugin.define``).
    :returns: A v2-shaped config dict, ready for ``json.dump``.
    """
    return {
        "$schema": "https://opencode.ai/config.json",
        "permissions": ASK_ALL_PERMISSIONS,
        "instructions": [str(instructions_path)],
        "mcp": {
            "servers": {
                "recon-echo": {
                    "type": "local",
                    "command": mcp_server_command,
                    "codemode": False,
                }
            }
        },
        "plugins": [str(plugin_path)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_recon(args))  # defined in Task 2


if __name__ == "__main__":
    raise SystemExit(main())
```

Also create `tests/dev/__init__.py` if it does not already exist (`tests/dev/lint/` already exists as a package under `tests/dev/`, so check first: `ls tests/dev/__init__.py`; it is already present, so no new `__init__.py` is needed here).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/dev/test_opencode_v2_recon.py -v`
Expected: PASS (6 passed) — `run_recon` is referenced only inside `main()`'s body, which none of these tests call, so the `NameError` it would raise (not defined until Task 2) never fires.

- [ ] **Step 5: Commit**

```bash
git add dev/opencode_v2_recon.py tests/dev/test_opencode_v2_recon.py
git commit -m "feat(opencode-native): add opencode v2 recon script arg parsing and config synthesis"
```

---

### Task 2: Recon script — process launch, event capture, turn driving, fixture writing

**Files:**
- Modify: `dev/opencode_v2_recon.py` (append below `build_recon_opencode_config`, replace the placeholder `main()` from Task 1)
- Test: `tests/dev/test_opencode_v2_recon.py` (append)

**Interfaces:**
- Consumes: `build_arg_parser`, `redact_secrets`, `build_recon_opencode_config`,
  `ASK_ALL_PERMISSIONS`, `_INSTRUCTIONS_MARKER`, `_PLUGIN_TEMPLATE`,
  `_ECHO_MCP_SERVER` (Task 1).
- Produces: `run_recon(args: argparse.Namespace) -> int` and `main()` wired to
  it; writes `tests/fixtures/opencode_v2/{openapi.json,events.ndjson,messages.json,recon-findings.md}`
  when run against a live server (verified manually in Task 3 — this task's
  automated test only covers the argv/import surface, since driving a real
  provider isn't unit-testable without live credentials).

- [ ] **Step 1: Write the failing test**

```python
def test_main_help_exits_zero_and_documents_model_flag(capsys: object) -> None:
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        from dev.opencode_v2_recon import main

        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "--model" in captured.out


def test_run_recon_is_importable_and_callable() -> None:
    from dev.opencode_v2_recon import run_recon

    assert callable(run_recon)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/dev/test_opencode_v2_recon.py -v`
Expected: FAIL with `ImportError: cannot import name 'run_recon' from 'dev.opencode_v2_recon'`

- [ ] **Step 3: Write minimal implementation**

Append to `dev/opencode_v2_recon.py`, replacing the Task-1 `main()`/`if __name__` block:

```python
async def _wait_for_url(proc: subprocess.Popen[bytes], timeout: float = 30.0) -> str:
    """
    Read `opencode serve --stdio`'s one-line ``{"url": "..."}`` stdout frame.

    Per ``packages/cli/src/server-process.ts:163``, ``--stdio`` mode prints
    exactly this JSON line once the server is bound, then blocks on stdin.
    """
    assert proc.stdout is not None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        line = await asyncio.to_thread(proc.stdout.readline)
        if not line:
            if proc.poll() is not None:
                raise RuntimeError(f"opencode serve exited early with code {proc.returncode}")
            continue
        text = line.decode("utf-8", "replace").strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if "url" in payload:
            return payload["url"]
    raise TimeoutError("opencode serve did not print {url} within timeout")


async def _stream_events(
    client: httpx.AsyncClient, out: list[dict[str, Any]], stop: asyncio.Event
) -> None:
    """
    Append every decoded ``/api/event`` frame to *out* until *stop* is set.

    Each SSE frame's ``data:`` line(s) decode to the full v2 event payload
    ``{id, type, created, data, location?, durable?}``
    (``packages/schema/src/event.ts`` ``PayloadBase``); ``:`` lines are
    heartbeat comments and are skipped.
    """
    async with client.stream("GET", "/api/event") as response:
        buffer: list[str] = []
        async for line in response.aiter_lines():
            if stop.is_set():
                return
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                buffer.append(line[len("data:") :].strip())
                continue
            if line == "" and buffer:
                try:
                    out.append(json.loads("".join(buffer)))
                except json.JSONDecodeError:
                    pass
                buffer = []


async def _auto_answer_prompts(
    client: httpx.AsyncClient,
    session_id: str,
    events: list[dict[str, Any]],
    findings: dict[str, str],
) -> None:
    """Poll captured events and auto-answer the first permission ask and form."""
    answered_permission = False
    answered_form = False
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120.0
    while loop.time() < deadline and not (answered_permission and answered_form):
        await asyncio.sleep(0.5)
        for event in list(events):
            if event.get("type") == "permission.asked" and not answered_permission:
                request_id = event["data"]["id"]
                reply = await client.post(
                    f"/api/session/{session_id}/permission/{request_id}/reply",
                    json={"decision": "once", "message": "omnigent-recon"},
                )
                findings["permission_reply_status"] = str(reply.status_code)
                answered_permission = True
            if event.get("type") == "form.created" and not answered_form:
                form = event["data"]["form"]
                answer = {
                    field["key"]: (field.get("options", ["A"])[0] if field.get("type") == "string" else "A")
                    for field in form.get("fields", [])
                }
                reply = await client.post(
                    f"/api/session/{session_id}/form/{form['id']}/reply",
                    json={"answer": answer},
                )
                findings["form_reply_status"] = str(reply.status_code)
                answered_form = True


def _fill_recon_findings(
    events: list[dict[str, Any]], messages: dict[str, Any], findings: dict[str, str]
) -> None:
    """Answer the six open design-spec questions (spec section 'Open items') from captured data."""
    instructions_seen = any(
        _INSTRUCTIONS_MARKER in json.dumps(event.get("data", {})) for event in events
    ) or _INSTRUCTIONS_MARKER in json.dumps(messages)
    findings["1_instructions_applies_system_prompt"] = "yes" if instructions_seen else "no"

    progress_events = [e for e in events if e.get("type") == "session.tool.progress"]
    findings["3_tool_progress_has_incremental_output"] = (
        "no data captured (no session.tool.progress events seen)"
        if not progress_events
        else ("yes" if any(e.get("data", {}).get("metadata") for e in progress_events) else "no")
    )

    mcp_calls = [
        e
        for e in events
        if e.get("type") == "session.tool.called" and "recon-echo" in str(e.get("data", {}).get("name", ""))
    ]
    mcp_permission_asks = [
        e
        for e in events
        if e.get("type") == "permission.asked" and "recon-echo" in json.dumps(e.get("data", {}))
    ]
    findings["4_codemode_false_mcp_raises_permission_asked"] = (
        "no mcp call captured" if not mcp_calls else ("yes" if mcp_permission_asks else "no")
    )

    actions = sorted({e["data"]["action"] for e in events if e.get("type") == "permission.asked"})
    findings["5_safety_action_names"] = ", ".join(actions) if actions else "none captured"


def _write_fixtures(
    out_dir: Path,
    openapi: dict[str, Any],
    events: list[dict[str, Any]],
    messages: dict[str, Any],
    findings: dict[str, str],
    secrets_to_redact: list[str],
) -> None:
    """Write the four committed fixtures, redacting secrets from every one."""
    out_dir.mkdir(parents=True, exist_ok=True)

    openapi_text = redact_secrets(json.dumps(openapi, indent=2, sort_keys=True), secrets_to_redact)
    (out_dir / "openapi.json").write_text(openapi_text + "\n", encoding="utf-8")

    events_text = "\n".join(
        redact_secrets(json.dumps(event, sort_keys=True), secrets_to_redact) for event in events
    )
    (out_dir / "events.ndjson").write_text(events_text + ("\n" if events_text else ""), encoding="utf-8")

    messages_text = redact_secrets(json.dumps(messages, indent=2, sort_keys=True), secrets_to_redact)
    (out_dir / "messages.json").write_text(messages_text + "\n", encoding="utf-8")

    lines = ["# OpenCode v2 recon findings", ""]
    for key, value in findings.items():
        lines.append(f"- **{key}**: {redact_secrets(value, secrets_to_redact)}")
    (out_dir / "recon-findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


async def run_recon(args: argparse.Namespace) -> int:
    """Drive one live turn against a real opencode v2 server and write the fixtures."""
    opencode_path = args.opencode_path or shutil.which("opencode")
    if not opencode_path:
        print("opencode CLI not found on PATH", file=sys.stderr)
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="opencode-v2-recon-"))
    xdg_data = workdir / "xdg-data"
    xdg_config = workdir / "xdg-config"
    workspace = workdir / "workspace"
    for directory in (xdg_data, xdg_config, workspace):
        directory.mkdir(parents=True, exist_ok=True)

    password = secrets.token_urlsafe(32)
    marker_path = workdir / "recon-instructions.txt"
    marker_path.write_text(_INSTRUCTIONS_MARKER, encoding="utf-8")
    plugin_path = workdir / "omnigent-recon-plugin.js"
    plugin_path.write_text(_PLUGIN_TEMPLATE, encoding="utf-8")
    plugin_log = workdir / "plugin.log.ndjson"

    config = build_recon_opencode_config(
        instructions_path=marker_path,
        mcp_server_command=[sys.executable, str(_ECHO_MCP_SERVER)],
        plugin_path=plugin_path,
    )
    config_dir = xdg_config / "opencode"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "opencode.json").write_text(json.dumps(config, indent=2), encoding="utf-8")

    env = dict(os.environ)
    env.update(
        {
            "XDG_DATA_HOME": str(xdg_data),
            "XDG_CONFIG_HOME": str(xdg_config),
            "OPENCODE_PASSWORD": password,
            "OPENCODE_SERVER_PASSWORD": password,
            "OMNIGENT_RECON_PLUGIN_LOG": str(plugin_log),
        }
    )
    secrets_to_redact = [password, str(workdir)]
    findings: dict[str, str] = {}
    events: list[dict[str, Any]] = []

    proc = subprocess.Popen(
        [opencode_path, "serve", "--hostname", "127.0.0.1", "--port", "0", "--stdio"],
        cwd=workspace,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        base_url = await _wait_for_url(proc)
        auth = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0) as client:
            info_resp = await client.get("/api/info")
            info_resp.raise_for_status()
            print("server info:", info_resp.json())

            openapi = (await client.get("/openapi.json")).json()

            stop = asyncio.Event()
            stream_task = asyncio.create_task(_stream_events(client, events, stop))

            provider_id, model_id = args.model.split("/", 1)
            create_resp = await client.post(
                "/api/session",
                json={
                    "title": "opencode-v2-recon",
                    "location": {"directory": str(workspace)},
                    "permissions": ASK_ALL_PERMISSIONS,
                    "model": {"id": model_id, "providerID": provider_id},
                },
            )
            create_resp.raise_for_status()
            session_id = create_resp.json()["data"]["id"]

            resume_false_resp = await client.post(
                f"/api/session/{session_id}/prompt",
                json={"text": "(seeded context, recon probe)", "resume": False},
            )
            findings["2_prompt_resume_false_records_without_running"] = (
                "yes" if resume_false_resp.status_code < 300 else f"no ({resume_false_resp.status_code})"
            )
            if resume_false_resp.status_code >= 300:
                synthetic_resp = await client.post(
                    f"/api/session/{session_id}/synthetic",
                    json={"text": "(seeded context, recon probe)"},
                )
                findings["2b_synthetic_fallback_status"] = str(synthetic_resp.status_code)

            prompt_resp = await client.post(
                f"/api/session/{session_id}/prompt",
                json={
                    "text": (
                        "Think step by step about what to do, then run `echo opencode-v2-recon` "
                        "in the shell, then call the recon-echo MCP tool's echo tool with "
                        "text='mcp-check', then ask me to choose between option A and option B "
                        "before continuing."
                    ),
                    "delivery": "steer",
                },
            )
            prompt_resp.raise_for_status()

            await _auto_answer_prompts(client, session_id, events, findings)
            await asyncio.sleep(2.0)  # drain trailing events once the turn settles

            compact_resp = await client.post(f"/api/session/{session_id}/compact", json={})
            findings["compact_status"] = str(compact_resp.status_code)
            await asyncio.sleep(2.0)

            stop.set()
            stream_task.cancel()

            messages = (await client.get(f"/api/session/{session_id}/message")).json()

        session_list = subprocess.run(
            [opencode_path, "session", "list", "--server", base_url, "--format", "json"],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        findings["6_session_list_format_json"] = (
            "yes" if session_list.returncode == 0 else f"no ({session_list.returncode}: {session_list.stderr})"
        )

        _fill_recon_findings(events, messages, findings)
        _write_fixtures(args.out_dir, openapi, events, messages, findings, secrets_to_redact)
        print(f"wrote fixtures to {args.out_dir}")
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_recon(args))


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/dev/test_opencode_v2_recon.py -v`
Expected: PASS (8 passed) — none of these tests start a real server or need
credentials; `run_recon`'s live-network body is only exercised by the manual
run in Task 3.

- [ ] **Step 5: Commit**

```bash
git add dev/opencode_v2_recon.py tests/dev/test_opencode_v2_recon.py
git commit -m "feat(opencode-native): drive a live opencode v2 turn and capture recon fixtures"
```

---

### Task 3: Human runs the recon spike and commits the fixtures

This is not code — it produces the committed data files Task 4's test
depends on. It must be done by a human with real model credentials; there is
no way to automate a real provider call in CI for this repo.

**Files:**
- Create (by running the script, not by hand):
  `tests/fixtures/opencode_v2/openapi.json`,
  `tests/fixtures/opencode_v2/events.ndjson`,
  `tests/fixtures/opencode_v2/messages.json`,
  `tests/fixtures/opencode_v2/recon-findings.md`

- [ ] **Step 1:** Ensure `opencode` v2.0.18 (`opencode --version`) is on
  `PATH` and you have live credentials for at least one provider (e.g. run
  `opencode auth login` once against your **real** global config beforehand
  if you haven't already — the recon script's per-run XDG dirs are isolated
  from that, but the harness needs the provider account to exist somewhere
  the recon config's `providers`/env can reach; simplest is to export the
  provider's API key, e.g. `ANTHROPIC_API_KEY`, in the shell the script runs
  in, since `dev/opencode_v2_recon.py` passes through `ANTHROPIC_`/`OPENAI_`/
  `GEMINI_`/`GOOGLE_`/`DATABRICKS_`-prefixed env vars via `os.environ` copy).

- [ ] **Step 2:** Run:

  ```bash
  uv run python dev/opencode_v2_recon.py --model anthropic/claude-sonnet-4-5
  ```

  (swap in whatever `provider/model` you have live credentials for). Watch
  the printed `server info:` line and the final `wrote fixtures to ...`
  line. If it times out waiting for a permission ask or form, the model
  didn't take the bait — rerun with a more insistent prompt tweak in
  `run_recon`'s `prompt_resp` call, or add `--keep-workdir` and inspect
  `<workdir>/plugin.log.ndjson` / the server's stderr to see what happened.

- [ ] **Step 3:** Inspect the four written files under
  `tests/fixtures/opencode_v2/`:
  - `openapi.json` parses and contains `/api/session/{sessionID}/prompt`.
  - `events.ndjson` has one JSON object per line and includes at least
    `session.text.delta`, `session.reasoning.delta`, `session.tool.called`,
    `permission.asked`, `permission.replied`, `form.created`,
    `form.replied`, `session.compaction.started`, `session.compaction.ended`,
    `session.usage.updated` (grep for `"type"` values: `grep -o '"type":"[^"]*"' tests/fixtures/opencode_v2/events.ndjson | sort -u`).
  - `messages.json` has `data` (array) and `cursor` keys.
  - `recon-findings.md` has a yes/no (or short factual) line for each of the
    six open items from the spec's "Open items resolved by the recon spike"
    section — re-read `designs/opencode-v2-native-harness.md`
    section by that name and confirm every item is answered, not just
    present as a key.
  - Confirm no secrets leaked: `grep -riE "password|/tmp/opencode-v2-recon" tests/fixtures/opencode_v2/*` should return nothing (the redaction in `_write_fixtures` should have caught it, but verify by hand since this data is about to be committed).

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/opencode_v2/
git commit -m "test(opencode-native): commit opencode v2 recon fixtures from a live 2.0.18 server"
```

---

### Task 4: Fixture loader + fixture-integrity test

**Files:**
- Create: `tests/opencode_v2_fixtures.py`
- Test: `tests/test_opencode_v2_fixtures.py`

**Interfaces:**
- Consumes: the committed fixtures from Task 3
  (`tests/fixtures/opencode_v2/{openapi.json,events.ndjson,messages.json}`).
- Produces: `load_events() -> list[dict]`, `events_of_type(type_: str) -> list[dict]`,
  `load_messages() -> dict`, module constants `EVENTS_PATH`, `MESSAGES_PATH`,
  `OPENAPI_PATH` — the exact names Stage 1-2's forwarder/client tests
  (`tests/test_opencode_native_client.py`, `tests/test_opencode_native_forwarder.py`)
  import per the cross-stage interface contract.

- [ ] **Step 1: Write the failing test**

```python
"""Fixture-integrity tests for tests/fixtures/opencode_v2/.

These four files are committed, not generated in CI: a human runs
dev/opencode_v2_recon.py against a real, credentialed opencode v2 server and
commits the result whenever the OpenCode wire protocol this harness targets
changes. This module's only job is to fail loudly, with an actionable
message, if the fixtures are ever missing or malformed, so Stage 1+ tests
don't silently run against nothing.
"""

from __future__ import annotations

import json

from tests.opencode_v2_fixtures import (
    EVENTS_PATH,
    MESSAGES_PATH,
    OPENAPI_PATH,
    events_of_type,
    load_events,
    load_messages,
)

_REQUIRED_EVENT_TYPES = [
    "session.text.delta",
    "session.reasoning.delta",
    "session.tool.called",
    "permission.asked",
    "permission.replied",
    "form.created",
    "form.replied",
    "session.compaction.started",
    "session.compaction.ended",
    "session.usage.updated",
]


def test_fixture_files_exist() -> None:
    missing = [path for path in (OPENAPI_PATH, EVENTS_PATH, MESSAGES_PATH) if not path.is_file()]
    assert not missing, (
        f"Missing OpenCode v2 recon fixtures: {missing}. Run "
        "`uv run python dev/opencode_v2_recon.py --model <provider/model>` "
        "with real credentials and commit tests/fixtures/opencode_v2/ "
        "(see Stage 0, Task 3)."
    )


def test_openapi_fixture_parses_and_has_expected_paths() -> None:
    payload = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert "/api/session/{sessionID}/prompt" in payload["paths"]
    assert "/api/session/{sessionID}/permission/{requestID}/reply" in payload["paths"]


def test_events_fixture_has_required_types() -> None:
    events = load_events()
    assert events, "events.ndjson is empty"
    seen_types = {event["type"] for event in events}
    missing_types = [t for t in _REQUIRED_EVENT_TYPES if t not in seen_types]
    assert not missing_types, f"events.ndjson is missing event types: {missing_types}"


def test_events_of_type_filters_by_type() -> None:
    deltas = events_of_type("session.text.delta")
    assert deltas
    assert all(event["type"] == "session.text.delta" for event in deltas)
    assert events_of_type("no.such.type") == []


def test_messages_fixture_parses_as_session_messages_response() -> None:
    messages = load_messages()
    assert "data" in messages
    assert "cursor" in messages
    assert isinstance(messages["data"], list)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_v2_fixtures.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'tests.opencode_v2_fixtures'`

- [ ] **Step 3: Write minimal implementation**

```python
"""Loaders for the committed OpenCode v2 recon fixtures.

Fixtures live in ``tests/fixtures/opencode_v2/`` (captured by
``dev/opencode_v2_recon.py`` against a real 2.0.x ``opencode serve``) and
back every ``opencode-native`` v2 unit test from Stage 1 onward.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "opencode_v2"
OPENAPI_PATH = _FIXTURES_DIR / "openapi.json"
EVENTS_PATH = _FIXTURES_DIR / "events.ndjson"
MESSAGES_PATH = _FIXTURES_DIR / "messages.json"


def load_events() -> list[dict[str, Any]]:
    """Parse ``events.ndjson`` into decoded event dicts, in capture order."""
    text = EVENTS_PATH.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def events_of_type(type_: str) -> list[dict[str, Any]]:
    """Return every captured event whose ``type`` equals *type_*, in capture order."""
    return [event for event in load_events() if event.get("type") == type_]


def load_messages() -> dict[str, Any]:
    """Parse ``messages.json`` (a ``SessionMessagesResponse``: ``{data, cursor}``)."""
    return json.loads(MESSAGES_PATH.read_text(encoding="utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_v2_fixtures.py -v`
Expected: PASS (5 passed) — requires Task 3's fixtures to already be
committed; if they aren't, `test_fixture_files_exist` fails first with the
actionable message above rather than every other test failing with a raw
`FileNotFoundError`.

- [ ] **Step 5: Commit**

```bash
git add tests/opencode_v2_fixtures.py tests/test_opencode_v2_fixtures.py
git commit -m "test(opencode-native): add opencode v2 fixture loader and fixture-integrity test"
```

---

## How to verify Stage 0 end-to-end

1. `uv run pytest tests/dev/test_opencode_v2_recon.py tests/test_opencode_v2_fixtures.py -v` — all pass, no network/credentials needed once fixtures are committed.
2. `cat tests/fixtures/opencode_v2/recon-findings.md` — read it yourself; confirm each of the six open items has a real, specific answer (not "TBD" or a stack trace). Stage 3's `provider.py`/`permissions.py`/`bridge.py` design decisions (which action names `safety.py` accepts, whether `instructions` or a `synthetic` seed carries the system prompt, whether Code Mode `codemode:false` actually gates MCP calls behind `permission.asked`) are read directly from this file — an ambiguous answer here should be resolved by rerunning the recon script with a more targeted prompt before Stage 1 starts, not guessed at later.
3. `git log --oneline -4` — four commits: arg parsing/config, orchestration, fixtures, loader+test.
## Stage 1: Client, app server, prompt transport, version gate, install

This stage rewrites the process launcher, HTTP client, and prompt transport for OpenCode 2.0.x. The v1 routes are deleted in the same tasks. Tasks 10-22 run in order, and later tasks assume the earlier ones have landed. Line numbers are for the files as they are before this stage starts. Once an earlier task in the stage has edited a file, find the code by the quoted text, not by line number.

### Evidence (checked against the `v2.0.18` source in `scratchpad/v2/` and the installed CLI)

- **Version string.** `opencode --version` prints `opencode v2.0.18`. The existing `_VERSION_RE` (`app_server.py:97`) already pulls `2.0.18` out of that, so no regex change is needed. Only the bounds change.
- **`--stdio` launch.** `opencode serve --help` lists `--hostname`, `--port`, `--cors`, `--service` and `--stdio`.
  - `packages/cli/src/commands/handlers/serve.ts`: `mode: input.service ? "service" : input.stdio ? "stdio" : "default"`.
  - `packages/cli/src/server-process.ts`: `return yield* options.mode === "service" ? server.shutdown : options.mode === "stdio" ? waitForStdinClose() : Effect.never`. So the server exits when stdin closes.
  - The same file handles the password in stdio mode: `if (options.mode === "stdio") { delete process.env.OPENCODE_PASSWORD; delete process.env.OPENCODE_SERVER_PASSWORD }`. The password therefore does not leak into tools the server spawns.
  - In stdio mode it prints `JSON.stringify({ url })` on stdout. We pass an explicit `--port`, so stdout can stay on DEVNULL.
- **Password env names.**
  - `packages/cli/src/env.ts`: `Config.redacted("OPENCODE_PASSWORD").pipe(Config.orElse(() => Config.redacted("OPENCODE_SERVER_PASSWORD")))`. The comment there says it is "sent by clients connecting to an explicit --server". The TUI started with `--server` therefore reads the same variables.
  - `packages/server/src/auth.ts`: `Layer.succeed(this, this.of({ ...input, username: "opencode" }))`. The username is fixed, and v2 code never reads `OPENCODE_SERVER_USERNAME`. It appears only in the stale v1 docs under `packages/web/`.
  - `packages/server/src/process.ts`: `if (!password) return yield* Effect.fail(new Error("Missing server password"))`.
- **Database path.** `packages/cli/src/database-path.ts`: `process.env.OPENCODE_DB ?? … ; return … path.resolve(data, filename)`. An absolute `OPENCODE_DB` is used as given.
- **`OPENCODE_CONFIG_DIR`.** `server-process.ts`: `Global.layerWith(process.env.OPENCODE_CONFIG_DIR ? { config: process.env.OPENCODE_CONFIG_DIR } : {})` and `config: { directory: process.env.OPENCODE_CONFIG_DIR, … }`. An inherited value would therefore point the server at a global config directory.
- **Readiness.** `packages/server/src/process.ts`:
  - `/api/info` answers before the app is ready.
  - The body is `{ version, pid: process.pid, urls: urls(), paths: { tmp } }` with status `state.type === "ready" ? 200 : state.type === "failed" ? 500 : 503`, and `retry-after: 1` while starting.
  - Unauthorized requests get `unauthorizedResponse` (401).
  - `openapi.json`: `GET /api/info` 200 is `ServerInfo {version, pid, urls, paths}`. It is **bare, not `{data}`**, and so is `POST /api/session/{id}/interrupt` → `SessionInterruptResponse {interrupted: boolean}`. Every other route this stage uses returns `{data}`, or `{location, data}` for `GET /api/model` and `GET /api/provider`. `GET /api/session/{id}/message` returns `{data, cursor: {previous, next}}`. `_unwrap` therefore returns `body["data"]` when that key is present and returns the body unchanged otherwise.
- **Request bodies in `openapi.json`.**

  | Endpoint | Body | Response |
  |---|---|---|
  | `POST /api/session` | `{id?, title?, agent?, model?: {id, providerID, variant?}, location?: {directory}, metadata?, permissions?: [{action, resource, effect: allow\|deny\|ask}]}` | — |
  | `POST /api/session/{id}/prompt` | `{id?: "msg_…", text (required), files?: [{uri (required), name?, mention?}], agents?, skills?, metadata?, delivery?: "steer"\|"queue", resume?: boolean}` | — |
  | `POST …/synthetic` | `{id?, text, metadata?, delivery?, resume?}` | — |
  | `POST …/model` | `{model: {id, providerID, variant?}}` | 204 |
  | `POST …/compact` | `{id?, delivery?}` (no model) | — |
  | `POST …/fork` | `{before?: "msg_…"}` | — |
  | `POST …/permission/{requestID}/reply` | `{decision: once\|always\|reject, message?}` | — |
  | `POST …/form/{formID}/reply` | `{answer: {key: string\|number\|boolean\|string[]}}` | — |
  | `DELETE …/form/{formID}` | — | — |
  | `POST /api/integration/{integrationID}/connect/key` | `{key, answer?, label?}` | — |

  `GET …/message` takes the query params `limit`, `order: asc|desc`, `cursor` and `type`. Its `cursor` "Do not combine with order".
- **Directory header.** `packages/server/src/location.ts`: `query.get("location[directory]") || (request.headers["x-opencode-directory"] ? decode(request.headers["x-opencode-directory"]) : process.cwd())`, where `decode` is `decodeURIComponent`. Non-ASCII paths therefore survive if they are URL-quoted.
- **SSE stream.**
  - `packages/server/src/handlers/event.ts`: the first frame is `{id, type: "server.connected", data: {}}`, and `Stream.tick("15 seconds").pipe(Stream.map(() => ": heartbeat\n\n"))` sends the heartbeat.
  - `packages/server/src/event-feed.ts`: `frame = (event) => \`data: ${JSON.stringify(event)}\n\n\``. There is no `id:` or `event:` line; the id and type are inside the JSON.
- **Root sessions.** `openapi.json` documents `GET /api/session` with the query params `limit`, `order`, `search`, `parentID` ("Use null to return only root sessions"), `directory`, `project`, `subpath` and `cursor`. It returns `SessionsResponse` `{data, cursor}`.
- **Reject feedback.** `packages/core/src/permission.ts:248-252` says: "A decline WITH feedback (CorrectedError) intentionally stays typed so the leaf can turn it into ToolFailure and the model continues." `reply_permission` therefore sends no `message` unless the caller passes one.
- **Attachments.** `packages/core/src/session/prompt.ts` handles only two URI kinds; anything else fails with "Unsupported attachment URI":

  | URI kind | Handling |
  |---|---|
  | `data:` | decoded for any MIME type |
  | `file:` | read from disk |

  After decoding, `to-llm-message.ts` sends images and PDFs as media and inlines `text/plain` as text (`attachmentContent`). Non-`data:` URLs are therefore dropped client-side.
- **TUI.** `opencode --help` shows `USAGE opencode <subcommand> [flags] [<directory>]`, `--server string Connect to a server URL`, `--session, -s string` and `--auto`. There is no `attach` subcommand in the list.
- **`opencode models`.** `packages/cli/src/commands/handlers/models.ts` resolves `ServerConnection.resolve({server, standalone})`, which means the background service when `--server` is absent. It then prints `GET /api/model` as `providerID/id`. There is no `--refresh` flag (see `opencode models --help`). See ledger item L6.
- **npm EEXIST.** The installed npm has `node_modules/bin-links/lib/check-bin.js`:
  - A global bin whose symlink resolves outside the installing package fails with `failEEXIST` unless `force`.
  - `lib/utils/error-message.js` prints "Remove the existing file and try again, or run npm with --force to overwrite files recklessly."
  - `opencode-ai` (v1) and `@opencode/cli` (v2) both declare the `opencode` bin. The install therefore runs `npm rm -g opencode-ai` first (failure ignored).
  - `--force` was not chosen. It also overrides other npm safety checks, and it leaves the v1 package installed, where a later `npm i -g opencode-ai` would silently take the bin back.

### Cross-stage ledger (what this stage deliberately leaves broken, and who fixes it)

`pyrefly` runs over the whole project (`.pre-commit-config.yaml:22-27`, `pass_filenames: false`). The call sites below live in files other stages own. After the listed task they are type errors, runtime errors, or both. They are listed rather than patched so that Stage 1 does not overlap Stage 2 or Stage 4.

**Recommendation:** land Stage 1 and Stage 2 (and Stage 4's call-site items L3-L6) together in one PR.

**Committing in between:** while a ledger item is open, the commit steps in Tasks 16, 20 and 21 need `SKIP=pyrefly git commit …`. Use it only after `.venv/bin/pyrefly check` shows errors **only** at the ledger lines below.

**Decision needed:** the coordinator should either confirm this, or move L1/L2 into Stage 1.

| # | After task | Broken call site | Fixed by |
|---|---|---|---|
| L1 | 20, 21 | `omnigent/harnesses/opencode_native/forwarder.py:380` `self._opencode.events()`, and every `event.properties` read (`:413, :603, :624, :821, :925, :973, :988, :1052-1061, :1229, :1233`) | Stage 2 |
| L2 | 20, 21 | `forwarder.py:1004` `reply_permission(request_id, {...})`, and `:1143, :1155, :1165` `reply_question`/`reject_question` | Stage 2 |
| L3 | 16 | `omnigent/runner/native/orchestration.py:2135-2147` `seed_context(…, provider_id=…, model_id=…)`, and `tests/runner/test_opencode_resume.py:34-84` (fake and assertion) | Stage 4 |
| L4 | 16 | `omnigent/runner/app.py:6917-6933` `client.summarize(…)` (compact dispatch plus `_resolve_opencode_compact_model`) | Stage 4 |
| L5 | 20 | `omnigent/runner/app.py:6975-6982`: `client.list_models()` now returns v2 `Model.Info` rows (`{id, providerID, name, …}`), not option dicts | Stage 4 |
| L6 | — | `app_server.py:201-264` `list_opencode_cli_model_options` still runs `opencode models --refresh`, which v2 rejects, against the background service. Delete it together with `runner/app.py:6945-6972` and `tests/test_opencode_native_app_server.py:386-412` | Stage 4 |
| L7 | 21 | `tests/test_opencode_native_forwarder.py:73, 559`, `tests/test_opencode_forwarder_reconnect.py:75` and `tests/e2e_ui/approvals/test_opencode_question.py:53` build `OpenCodeEvent(properties=…, raw=…)` | Stage 2 (rewrites these tests) |
| L8 | 17 | The composed system prompt is no longer sent with each turn: the executor's `_gate_system_prompt` override is deleted and v2 has no `system` field | Stage 3 (config `instructions`) |
| L9 | — | `omnigent/runner/tool_dispatch.py:2782` prints `npm install -g {package}` for a missing CLI. For opencode that command hits EEXIST when v1 is still installed | Stage 5 (copy) |

Interface note: the `OpenCodeClient` constructor keeps its existing keyword `headers=`. Callers in `app_server.py:548-552`, `:586-590` and `runner/app.py:6975` use that name. The brief's `auth_headers` describes the value; it is not a rename.

---

### Task 10: Version gate → OpenCode 2.x

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py:32-36`
- Modify: `omnigent/harnesses/opencode_native/app_server.py:101-103, 128-134, 137-145, 170-173`
- Modify: `omnigent/onboarding/harness_install.py:904`
- Test: `tests/test_opencode_native_app_server.py:25-47, 290-362`
- Test: `tests/onboarding/test_harness_install.py:1299, 1322-1348, 1506-1519`
- Test: `tests/onboarding/test_harness_readiness.py:109-113`
- Test: `tests/onboarding/test_native_harnesses_windows_unavailable.py:38`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `OPENCODE_MIN_VERSION = "2.0.0"`
  - `OPENCODE_MAX_VERSION_EXCLUSIVE = "3.0.0"`
  - `parse_opencode_version(text: str) -> str | None` accepts `"opencode v2.0.18"`.
  - `check_opencode_version(version: str) -> None` accepts 2.x and rejects 1.x and 3.x.

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_app_server.py`, replace lines 25-42 (`test_parse_opencode_version`, `test_check_version_in_range`, `test_check_version_out_of_range_raises`) with:

```python
def test_parse_opencode_version() -> None:
    assert parse_opencode_version("opencode v2.0.18") == "2.0.18"
    assert parse_opencode_version("2.0.18") == "2.0.18"
    assert parse_opencode_version("v2.1.0-beta.1") == "2.1.0-beta.1"
    assert parse_opencode_version("no version here") is None


def test_check_version_in_range() -> None:
    check_opencode_version("2.0.0")
    check_opencode_version("2.0.18")
    check_opencode_version("2.9.99")


@pytest.mark.parametrize("version", ["1.17.7", "1.18.16", "1.99.0", "3.0.0"])
def test_check_version_out_of_range_raises(version: str) -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version(version)
```

Make three more edits in the same file:
- In `test_start_raises_on_unsupported_version_without_env` (line 294), change `lambda _path: "1.19.0"` to `lambda _path: "1.18.16"`.
- In `test_start_skips_version_gate_when_env_set` (lines 322 and 343), make the same change: `"1.19.0"` becomes `"1.18.16"`, and the assertion becomes `assert server.version == "1.18.16"`.
- In `test_resolve_opencode_version_parses` (lines 354-362), use the real v2 output:

```python
def test_resolve_opencode_version_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        appsrv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a, 0, stdout="opencode v2.0.18\n", stderr=""
        ),
    )
    assert appsrv.resolve_opencode_version("/x/opencode") == "2.0.18"
```

In `tests/onboarding/test_harness_install.py`:
- Line 1299: `(hi.OPENCODE_KEY, "1.17.7", "1.19.0"),` → `(hi.OPENCODE_KEY, "2.0.0", "3.0.0"),`
- Lines 1322-1331: replace the parametrize block with:

```python
@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.18.16", False),  # v1 line, below min
        ("3.0.0", False),  # at max exclusive
        ("3.1.0", False),  # above max
        ("2.0.0", True),  # min inclusive
        ("opencode v2.0.18", True),  # real v2 --version output
    ],
)
```

- Line 1341: `# OpenCode's supported range is [1.17.7, 1.19.0).` → `# OpenCode's supported range is [2.0.0, 3.0.0).`
- Line 1514: `out = "1.17.8\n"` → `out = "opencode v2.0.18\n"`

In `tests/onboarding/test_harness_readiness.py:109-113`, update the comment and the stub version:

```python
            # OpenCode's declared range is [2.0.0, 3.0.0); Cursor uses calendar
            # versions and needs a build after 2026-06-01; everything else is
            # fine with a generous semver placeholder.
            if argv[0].endswith("opencode"):
                version = "opencode v2.0.18\n"
```

In `tests/onboarding/test_native_harnesses_windows_unavailable.py:38`, change `version = "1.17.7\n"` to `version = "opencode v2.0.18\n"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_app_server.py::test_check_version_in_range tests/onboarding/test_harness_install.py::test_versioned_specs_declare_bounds -v`
Expected: FAIL with `OpenCodeVersionError: Unsupported OpenCode version 2.0.0: requires >=1.17.7,<1.19.0` and `assert '1.17.7' == '2.0.0'`.

- [ ] **Step 3: Write minimal implementation**

`omnigent/harnesses/opencode_native/client.py:32-36`. Replace:

```python
# Supported OpenCode CLI/API version range. Accepts 1.17.7+ through the
# entire 1.18.x line (validated against 1.18.x event/API shapes); refuses
# 1.19+ until validated against that release.
OPENCODE_MIN_VERSION = "1.17.7"
OPENCODE_MAX_VERSION_EXCLUSIVE = "1.19.0"
```

with:

```python
# Supported OpenCode CLI/API range: the 2.x ``/api/*`` protocol.
OPENCODE_MIN_VERSION = "2.0.0"
OPENCODE_MAX_VERSION_EXCLUSIVE = "3.0.0"
```

`omnigent/harnesses/opencode_native/app_server.py`, three edits:

1. Lines 101-102, the comment: `# Escape hatch: set truthy to bypass the OpenCode CLI version gate (e.g. to` / `# try an as-yet-unvalidated 1.18+/v2 release). Mirrors OMNIGENT_NO_UPDATE_CHECK.` becomes:

```python
# Escape hatch: set truthy to bypass the OpenCode CLI version gate (e.g. to
# try an as-yet-unvalidated 3.x release). Mirrors OMNIGENT_NO_UPDATE_CHECK.
```

2. Lines 131-133 of `find_opencode_cli` become:

```python
        raise OpenCodeCliNotFoundError(
            "opencode CLI not found on PATH; install the '@opencode/cli' npm package"
        )
```

3. Lines 141-142 (docstring) become `:param text: Raw CLI output, e.g. ``"opencode v2.0.18"`` or ``"2.0.18"``.` and `:returns: The parsed version, e.g. ``"2.0.18"``, or ``None``.`

Lines 170-173 (the `check_opencode_version` raise) become:

```python
        raise OpenCodeVersionError(
            f"Unsupported OpenCode version {version}: requires >={minimum},<{maximum_exclusive}. "
            "Install a pinned '@opencode/cli' release."
        )
```

`omnigent/onboarding/harness_install.py:904`: `native CLI (e.g. an OpenCode release outside the supported 1.17.x band)` becomes `native CLI (e.g. an OpenCode release outside the supported 2.x band)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_app_server.py tests/onboarding/test_harness_install.py tests/onboarding/test_harness_readiness.py tests/onboarding/test_native_harnesses_windows_unavailable.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/client.py omnigent/harnesses/opencode_native/app_server.py omnigent/onboarding/harness_install.py tests/test_opencode_native_app_server.py tests/onboarding/test_harness_install.py tests/onboarding/test_harness_readiness.py tests/onboarding/test_native_harnesses_windows_unavailable.py
git commit -m "feat(opencode-native): gate the CLI on OpenCode 2.x"
```

---

### Task 11: Install `@opencode/cli@~2.0.18` and evict v1 `opencode-ai`

**Files:**
- Modify: `omnigent/onboarding/harness_install.py:132-135, 219-232`
- Modify: `omnigent/cli_config.py:3485-3495, 3829`
- Modify: `deploy/docker/install-harness-cli.sh:25, 40, 246`
- Test: `tests/onboarding/test_harness_install.py` (new tests after `test_install_harness_cli_runs_npm_then_rechecks`, line ~773)
- Test: `tests/deploy/test_host_image_cli_install.py:56-80`

**Interfaces:**
- Consumes: `OPENCODE_MIN_VERSION` and `OPENCODE_MAX_VERSION_EXCLUSIVE` (Task 10).
- Produces: `_HARNESS_INSTALL[OPENCODE_KEY]` with:
  - `package="@opencode/cli@~2.0.18"`
  - `install_command=("bash", "-c", "npm rm -g opencode-ai >/dev/null 2>&1 || true; npm install -g @opencode/cli@~2.0.18")`
  - `install_hint="npm rm -g opencode-ai; npm install -g @opencode/cli@~2.0.18"`
  - Unchanged: the setup step `opencode auth login` (`harness_install.py:548-555`).

- [ ] **Step 1: Write the failing test**

Append to `tests/onboarding/test_harness_install.py`, right after `test_install_harness_cli_runs_npm_then_rechecks`:

```python
def test_opencode_install_spec_pins_v2_cli() -> None:
    """OpenCode installs the v2 ``@opencode/cli`` package, pinned to 2.0.x."""
    spec = hi.harness_install_spec(hi.OPENCODE_KEY)
    assert spec is not None
    assert spec.binary == "opencode"
    assert spec.package == "@opencode/cli@~2.0.18"


def test_opencode_install_removes_v1_package_before_installing() -> None:
    """v1 ``opencode-ai`` owns the global ``opencode`` bin and npm refuses to
    overwrite another package's bin (EEXIST), so v1 is removed first."""
    argv = hi.harness_install_command(hi.OPENCODE_KEY)
    assert argv[:2] == ["bash", "-c"]
    script = argv[2]
    assert script.index("npm rm -g opencode-ai") < script.index(
        "npm install -g @opencode/cli@~2.0.18"
    )
    assert hi.harness_install_display(hi.OPENCODE_KEY) == (
        "npm rm -g opencode-ai; npm install -g @opencode/cli@~2.0.18"
    )


def test_install_harness_cli_runs_opencode_v1_removal_then_install(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The one-click install runs the removal and the v2 install in one argv."""
    calls: list[list[str]] = []
    state = {"installed": False}

    def _which(name: str) -> str | None:
        if name == "bash":
            return "/bin/bash"
        if name == "opencode" and state["installed"]:
            return "/usr/local/bin/opencode"
        return None

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["installed"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.shutil, "which", _which)
    monkeypatch.setattr(hi.subprocess, "run", _run)

    assert hi.install_harness_cli(hi.OPENCODE_KEY) is True
    assert calls == [
        [
            "bash",
            "-c",
            "npm rm -g opencode-ai >/dev/null 2>&1 || true; "
            "npm install -g @opencode/cli@~2.0.18",
        ]
    ]
```

In `tests/deploy/test_host_image_cli_install.py`, replace lines 56-73 (the docstring through the opencode asserts) with:

```python
def test_extra_cli_rows_match_harness_install_table() -> None:
    """install-harness-cli.sh's npm + goose rows stay in sync with _HARNESS_INSTALL.

    The script resolves ``EXTRA_HARNESS_CLIS`` names to the same npm package
    and default pin the runtime installs via ``omnigent setup``, behind a
    "keep in sync" comment. A drift would bake a package or pin the runtime
    then rejects (opencode's runtime gate bounds 2.x), so assert the two
    tables agree instead of trusting the comment.
    """
    from omnigent.onboarding import harness_install as hi

    script = (_ROOT / "deploy/docker/install-harness-cli.sh").read_text()

    opencode = hi._HARNESS_INSTALL[hi.OPENCODE_KEY]
    assert opencode.package == "@opencode/cli@~2.0.18"
    pkg, _, pin = opencode.package.rpartition("@")
    assert f"{pkg}@${{version:-{pin}}}" in script
    # v1 opencode-ai owns the same global `opencode` bin (npm EEXIST).
    assert "npm rm -g opencode-ai" in script
```

The qwen and goose asserts at lines 75-80 are kept as they are.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/onboarding/test_harness_install.py -k opencode_install tests/deploy/test_host_image_cli_install.py::test_extra_cli_rows_match_harness_install_table -v`
Expected: FAIL with `AssertionError: assert 'opencode-ai@~1.18.0' == '@opencode/cli@~2.0.18'`.

- [ ] **Step 3: Write minimal implementation**

`omnigent/onboarding/harness_install.py:132-135`. Replace:

```python
# OpenCode native harness CLI (``opencode serve`` / ``opencode attach``),
# installed via the ``opencode-ai`` npm package. No login/logout/status argv
# is wired yet — readiness is binary-only until an auth check exists.
OPENCODE_KEY = "opencode"
```

with:

```python
# OpenCode native harness CLI (``opencode serve`` plus the ``--server`` TUI),
# installed via the ``@opencode/cli`` npm package. No login/logout/status argv
# is wired yet — readiness is binary-only until an auth check exists.
OPENCODE_KEY = "opencode"
_OPENCODE_PACKAGE = "@opencode/cli@~2.0.18"
# v1 ``opencode-ai`` links the same global ``opencode`` bin and npm refuses to
# overwrite another package's bin (EEXIST), so the v1 package is removed first.
_OPENCODE_INSTALL_HINT = f"npm rm -g opencode-ai; npm install -g {_OPENCODE_PACKAGE}"
_OPENCODE_INSTALL_SCRIPT = (
    f"npm rm -g opencode-ai >/dev/null 2>&1 || true; npm install -g {_OPENCODE_PACKAGE}"
)
```

Replace lines 219-232:

```python
    # Pin the install to the supported 1.18.x range: opencode-ai's npm ``latest``
    # is a ``0.0.0-beta-*`` pre-release, so a bare ``opencode-ai`` would install a
    # version the runtime version-check (``check_opencode_version``,
    # >=1.17.7,<1.19.0) then rejects. ``~1.18.0`` resolves to the latest 1.18.x.
    # The same version bounds are enforced in setup via ``min_version`` /
    # ``max_version_exclusive`` so the install/upgrade prompt fires before
    # the runtime gate does.
    OPENCODE_KEY: HarnessInstallSpec(
        "OpenCode",
        "opencode",
        "opencode-ai@~1.18.0",
        min_version=OPENCODE_MIN_VERSION,
        max_version_exclusive=OPENCODE_MAX_VERSION_EXCLUSIVE,
    ),
```

with:

```python
    # Pin the install to the supported 2.0.x line. The same bounds are enforced
    # in setup via ``min_version`` / ``max_version_exclusive`` so the
    # install/upgrade prompt fires before the runtime gate does.
    OPENCODE_KEY: HarnessInstallSpec(
        "OpenCode",
        "opencode",
        _OPENCODE_PACKAGE,
        install_hint=_OPENCODE_INSTALL_HINT,
        install_command=("bash", "-c", _OPENCODE_INSTALL_SCRIPT),
        min_version=OPENCODE_MIN_VERSION,
        max_version_exclusive=OPENCODE_MAX_VERSION_EXCLUSIVE,
    ),
```

`omnigent/cli_config.py`: the menu should show the runnable one-liner, not the `bash -c` wrapper.
- In the function-local import at lines 3485-3490, replace `harness_install_command,` with `harness_install_display,`.
- Line 3495: `cmd = " ".join(harness_install_command(OPENCODE_KEY))` → `cmd = harness_install_display(OPENCODE_KEY)`.
- Line 3829: `_install_hint(" ".join(harness_install_command(OPENCODE_KEY))),` → `_install_hint(harness_install_display(OPENCODE_KEY)),`. That function already imports `harness_install_display` at line 3624.

`deploy/docker/install-harness-cli.sh`:
- Line 25: `# right package and default pin (e.g. opencode → opencode-ai@~1.18.0, mirroring` → `# right package and default pin (e.g. opencode → @opencode/cli@~2.0.18, mirroring`
- Line 40: `#   opencode  → npm opencode-ai (default pin ~1.18.0, as harness_install.py)` → `#   opencode  → npm @opencode/cli (default pin ~2.0.18, as harness_install.py)`
- Line 246: replace `        opencode) install_npm "opencode-ai@${version:-~1.18.0}" opencode ;;` with:

```bash
        opencode)
            # v1 opencode-ai owns the same global `opencode` bin; npm refuses
            # to overwrite another package's bin (EEXIST).
            npm rm -g opencode-ai >/dev/null 2>&1 || true
            install_npm "@opencode/cli@${version:-~2.0.18}" opencode
            ;;
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/onboarding/test_harness_install.py tests/deploy/test_host_image_cli_install.py -v && bash -n deploy/docker/install-harness-cli.sh`
Expected: PASS, and `bash -n` exits 0.

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/harness_install.py omnigent/cli_config.py deploy/docker/install-harness-cli.sh tests/onboarding/test_harness_install.py tests/deploy/test_host_image_cli_install.py
git commit -m "feat(opencode-native): install @opencode/cli 2.0.x and evict v1 opencode-ai"
```

---

### Task 12: Record `last_applied_model` in `state.json`

`bridge.py` owns the `state.json` schema (`write_bridge_state` / `read_bridge_state`, `bridge.py:568-671`). `state.py` owns only `launch.json` and is not touched.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:236-268, 581-595, 659-671, 720-744` (new function appended)
- Test: `tests/test_opencode_native_bridge.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `OpenCodeNativeBridgeState.last_applied_model: str | None = None`
  - The `state.json` key `"last_applied_model"`
  - `update_last_applied_model(bridge_dir: Path, model: str) -> bool`

- [ ] **Step 1: Write the failing test**

Add `update_last_applied_model` to the `from omnigent.harnesses.opencode_native.bridge import (...)` block in `tests/test_opencode_native_bridge.py`, sorted after `update_last_event_id`. Then append:

```python
def test_last_applied_model_round_trips(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir, last_applied_model="acme/model-a"))
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model == "acme/model-a"
    raw = json.loads((bridge_dir / "state.json").read_text(encoding="utf-8"))
    assert raw["last_applied_model"] == "acme/model-a"


def test_last_applied_model_absent_in_older_state_reads_none(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir))
    path = bridge_dir / "state.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("last_applied_model")
    path.write_text(json.dumps(raw), encoding="utf-8")
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model is None


def test_update_last_applied_model(bridge_dir: Path) -> None:
    assert update_last_applied_model(bridge_dir, "acme/model-a") is False  # no state yet
    write_bridge_state(bridge_dir, _state(bridge_dir, model_override="acme/model-a"))
    assert update_last_applied_model(bridge_dir, "acme/model-a") is True
    loaded = read_bridge_state(bridge_dir)
    assert loaded is not None
    assert loaded.last_applied_model == "acme/model-a"
    assert loaded.model_override == "acme/model-a"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k last_applied_model -v`
Expected: FAIL with `ImportError: cannot import name 'update_last_applied_model'`.

- [ ] **Step 3: Write minimal implementation**

`bridge.py`, class `OpenCodeNativeBridgeState`:
- Docstring (after `:param last_event_id: …`, line 255) gains:

```python
    :param last_applied_model: Model most recently pushed to the OpenCode
        session via ``POST /api/session/{id}/model``, e.g.
        ``"opencode/big-pickle"``; ``None`` until the first switch.
```

- After the field `last_event_id: str | None = None` (line 268), add:

```python
    last_applied_model: str | None = None
```

In `write_bridge_state`, add this entry to the dict after `"last_event_id": state.last_event_id,` (line 594):

```python
                    "last_applied_model": state.last_applied_model,
```

In `read_bridge_state`, after `last_event_id=_opt_str("last_event_id"),` (line 670), add:

```python
        last_applied_model=_opt_str("last_applied_model"),
```

In `update_model_override`, replace the docstring paragraph at lines 722-729:

```python
    opencode has no session-level model setting — the model is a per-prompt
    field — so the executor reads ``model_override`` from this bridge state on
    every web-injected prompt (see
    ``OpenCodeNativeExecutor._build_prompt_with_model_override``). Updating it
    here makes the NEXT injected turn use the new model. A blank/whitespace
    value clears the override (fall back to opencode's own default).
```

with:

```python
    Before each web-injected prompt the transport compares ``model_override``
    with ``last_applied_model`` and calls ``POST /api/session/{id}/model`` when
    they differ, so updating it here switches the model on the NEXT injected
    turn. A blank/whitespace value clears the override (OpenCode keeps the
    model it last had).
```

Append at the end of `bridge.py`:

```python
def update_last_applied_model(bridge_dir: Path, model: str) -> bool:
    """
    Record the model most recently applied to the OpenCode session.

    :param bridge_dir: Native OpenCode bridge directory.
    :param model: Qualified model id that ``POST /api/session/{id}/model``
        accepted, e.g. ``"opencode/big-pickle"``.
    :returns: ``True`` when the state existed and was updated, ``False`` when
        no bridge state is present.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return False
    import dataclasses

    write_bridge_state(bridge_dir, dataclasses.replace(state, last_applied_model=model))
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_bridge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py tests/test_opencode_native_bridge.py
git commit -m "feat(opencode-native): track the last applied model in bridge state"
```

---

### Task 13: Password env and `--server` TUI command (replaces `attach`)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:14, 46-50`
- Modify: `omnigent/harnesses/opencode_native/app_server.py:8-19, 42-50, 299-324, 335-339, 366-367, 371-386`
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:20-25, 273-287`
- Modify: `omnigent/native/native_server_transport.py:120-126, 166-170`
- Modify: `omnigent/runner/native/orchestration.py:1427-1433, 1445-1449, 1770, 1784-1791`
- Test: `tests/test_opencode_native_app_server.py:10-22, 66-104, 173-177`
- Test: `tests/test_opencode_http_transport.py:194-204`

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `bridge.OPENCODE_PASSWORD_ENV_VAR = "OPENCODE_PASSWORD"`
  - `bridge.OPENCODE_SERVER_PASSWORD_ENV_VAR = "OPENCODE_SERVER_PASSWORD"` (kept)
  - `bridge.OPENCODE_DEFAULT_USERNAME = "opencode"` (kept)
  - `app_server.build_tui_command(opencode_path: str, *, base_url: str, session_id: str, workspace: str, extra_args: Sequence[str] = ()) -> list[str]`
  - `app_server.opencode_terminal_env(secret: str, *, xdg_data_home: Path | None = None, xdg_config_home: Path | None = None) -> dict[str, str]`
- Deleted:
  - `bridge.OPENCODE_SERVER_USERNAME_ENV_VAR` (`bridge.py:48`)
  - `app_server.build_opencode_attach_args` (`app_server.py:299-324`)
  - `OpenCodeHttpTransport.build_tui_attach_command` (`http_transport.py:273-287`)
  - `NativeServerTransport.build_tui_attach_command` (`native_server_transport.py:166-170`)

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_app_server.py`, change the import list (lines 11-22). Replace `build_opencode_attach_args,` with `build_tui_command,`. Then replace `test_build_attach_args` and `test_build_attach_args_without_session` (lines 66-90) with:

```python
def test_build_tui_command() -> None:
    assert build_tui_command(
        "/usr/bin/opencode",
        base_url="http://127.0.0.1:49231",
        session_id="ses_1",
        workspace="/repo",
        extra_args=("--log-level", "debug"),
    ) == [
        "/usr/bin/opencode",
        "--server",
        "http://127.0.0.1:49231",
        "--session",
        "ses_1",
        "/repo",
        "--log-level",
        "debug",
    ]
```

Replace the password assertions in `test_filtered_server_env_sets_xdg_and_password` (lines 101-102):

```python
    assert env["OPENCODE_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"
    assert "OPENCODE_SERVER_USERNAME" not in env
```

Replace `test_terminal_env` (lines 173-177) with:

```python
def test_terminal_env_carries_password_under_both_names(tmp_path: Path) -> None:
    env = opencode_terminal_env(
        "pw", xdg_data_home=tmp_path / "data", xdg_config_home=tmp_path / "config"
    )
    assert env == {
        "OPENCODE_PASSWORD": "pw",
        "OPENCODE_SERVER_PASSWORD": "pw",
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
    }


def test_terminal_env_without_xdg_dirs() -> None:
    assert opencode_terminal_env("pw") == {
        "OPENCODE_PASSWORD": "pw",
        "OPENCODE_SERVER_PASSWORD": "pw",
    }
```

In `tests/test_opencode_http_transport.py`, delete `test_build_tui_attach_command_uses_launch_server_url` (lines 194-204). The TUI is no longer a transport concern.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_app_server.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_tui_command'`.

- [ ] **Step 3: Write minimal implementation**

`bridge.py:14`: `        auth.secret         # OPENCODE_SERVER_PASSWORD for this server` becomes `        auth.secret         # OPENCODE_PASSWORD for this server`.

`bridge.py:46-50`. Replace:

```python
# OpenCode server basic-auth env vars (see opencode ``attach``/``serve``).
OPENCODE_SERVER_PASSWORD_ENV_VAR = "OPENCODE_SERVER_PASSWORD"
OPENCODE_SERVER_USERNAME_ENV_VAR = "OPENCODE_SERVER_USERNAME"
# Default basic-auth username opencode falls back to when unset.
OPENCODE_DEFAULT_USERNAME = "opencode"
```

with:

```python
# OpenCode server password env. v2 reads OPENCODE_PASSWORD and still honors
# the legacy OPENCODE_SERVER_PASSWORD; both carry the per-session secret.
OPENCODE_PASSWORD_ENV_VAR = "OPENCODE_PASSWORD"
OPENCODE_SERVER_PASSWORD_ENV_VAR = "OPENCODE_SERVER_PASSWORD"
# The v2 server's fixed basic-auth username.
OPENCODE_DEFAULT_USERNAME = "opencode"
```

`app_server.py` module docstring, lines 12-18. Replace the bullets `- Launch ``opencode serve --hostname 127.0.0.1 --port <port>`` with a` / `random ``OPENCODE_SERVER_PASSWORD`` and the per-session XDG dirs.` and `- Build the ``opencode attach`` argv + env for the terminal takeover (the` / `Codex ``--remote`` analog).` with:

```python
- Launch ``opencode serve --hostname 127.0.0.1 --port <port>`` with a
  random ``OPENCODE_PASSWORD`` and the per-session XDG dirs.
- Build the ``opencode --server <url> --session <id>`` argv + env for the
  terminal TUI (the Codex ``--remote`` analog).
```

`app_server.py:42-50`. Replace the bridge import with:

```python
from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_PASSWORD_ENV_VAR,
    OPENCODE_SERVER_PASSWORD_ENV_VAR,
    auth_headers_for_secret,
    ensure_auth_secret,
    xdg_config_home_for_bridge_dir,
    xdg_data_home_for_bridge_dir,
)
```

`app_server.py:299-324`. Delete `build_opencode_attach_args` entirely and put this in its place:

```python
def build_tui_command(
    opencode_path: str,
    *,
    base_url: str,
    session_id: str,
    workspace: str,
    extra_args: Sequence[str] = (),
) -> list[str]:
    """
    Build the full argv for the OpenCode TUI bound to this session's server.

    The TUI connects to the runner-owned ``opencode serve`` (``--server``) and
    opens the Omnigent-owned session, so the terminal, forwarder, and web UI
    drive one OpenCode session. The password travels in the environment (see
    :func:`opencode_terminal_env`), never on argv.

    :param opencode_path: Path to the ``opencode`` binary.
    :param base_url: Server URL, e.g. ``"http://127.0.0.1:49231"``.
    :param session_id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param workspace: Directory the TUI starts in (positional argument).
    :param extra_args: User pass-through args appended last.
    :returns: ``[opencode, "--server", url, "--session", id, workspace, *extra]``.
    """
    return [
        opencode_path,
        "--server",
        base_url,
        "--session",
        session_id,
        workspace,
        *extra_args,
    ]
```

`app_server.py:335-339` (docstring of `filtered_server_env`): `config; ``OPENCODE_SERVER_PASSWORD`` secures the loopback server. Only` becomes `config; ``OPENCODE_PASSWORD`` (plus the legacy ``OPENCODE_SERVER_PASSWORD``) secures the loopback server. Only`.

`app_server.py:366-367`. Replace:

```python
    env[OPENCODE_SERVER_PASSWORD_ENV_VAR] = auth_secret
    env[OPENCODE_SERVER_USERNAME_ENV_VAR] = OPENCODE_DEFAULT_USERNAME
```

with:

```python
    env[OPENCODE_PASSWORD_ENV_VAR] = auth_secret
    env[OPENCODE_SERVER_PASSWORD_ENV_VAR] = auth_secret
```

`app_server.py:371-386`. Replace the old `opencode_terminal_env(server: OpenCodeNativeServer)` with:

```python
def opencode_terminal_env(
    secret: str,
    *,
    xdg_data_home: Path | None = None,
    xdg_config_home: Path | None = None,
) -> dict[str, str]:
    """
    Build the environment for the OpenCode TUI terminal process.

    :param secret: The per-session server password.
    :param xdg_data_home: Per-session ``XDG_DATA_HOME`` so TUI-local state stays
        out of the user's global OpenCode data dir; ``None`` leaves it unset.
    :param xdg_config_home: Per-session ``XDG_CONFIG_HOME``; ``None`` leaves it
        unset.
    :returns: Env carrying the password as ``OPENCODE_PASSWORD`` and the legacy
        ``OPENCODE_SERVER_PASSWORD``.
    """
    env = {
        OPENCODE_PASSWORD_ENV_VAR: secret,
        OPENCODE_SERVER_PASSWORD_ENV_VAR: secret,
    }
    if xdg_data_home is not None:
        env["XDG_DATA_HOME"] = str(xdg_data_home)
    if xdg_config_home is not None:
        env["XDG_CONFIG_HOME"] = str(xdg_config_home)
    return env
```

`http_transport.py:20-25`. The import becomes:

```python
from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeNativeServer,
    client_for_state,
)
```

Delete `build_tui_attach_command` (`http_transport.py:273-287`).

`native_server_transport.py`:
- Delete the `build_tui_attach_command` stub (lines 166-170).
- In the class docstring (lines 121-123), change `lifecycle, prompt injection, abort, event stream, fork, permission` / `replies, TUI attach). The shared` to `lifecycle, prompt injection, abort, event stream, fork, permission` / `replies). The shared`.

`orchestration.py:1427-1433` (docstring of `_auto_create_opencode_terminal`). Replace:

```python
    Mirrors :func:`_auto_create_codex_terminal`, substituting ``opencode
    serve`` / ``opencode attach`` for Codex's app-server/remote transport:
    boots a per-session ``opencode serve`` process, resumes-or-creates the
    OpenCode session, persists bridge state + ``external_session_id``,
    starts the SSE forwarder, then registers the ``opencode attach`` TUI as
    a streamable terminal resource attached to that server.
```

with:

```python
    Mirrors :func:`_auto_create_codex_terminal`, substituting ``opencode
    serve`` / ``opencode --server`` for Codex's app-server/remote transport:
    boots a per-session ``opencode serve`` process, resumes-or-creates the
    OpenCode session, persists bridge state + ``external_session_id``,
    starts the SSE forwarder, then registers the ``opencode --server`` TUI as
    a streamable terminal resource attached to that server.
```

`orchestration.py:1445-1449`. The import becomes:

```python
    from omnigent.harnesses.opencode_native.app_server import (
        OpenCodeNativeServer,
        build_tui_command,
        opencode_terminal_env,
    )
```

`orchestration.py:1770`. Insert this immediately before `agent_os_env = _agent_os_env_from_spec(agent_spec)`:

```python
    tui_argv = build_tui_command(
        server.opencode_path,
        base_url=server.base_url,
        session_id=opencode_session_id,
        workspace=workspace,
        extra_args=tuple(launch_config.terminal_launch_args or ()),
    )
```

`orchestration.py:1784-1791`. Replace:

```python
                command=server.opencode_path,
                args=build_opencode_attach_args(
                    server_url=server.base_url,
                    workspace=workspace,
                    session_id=opencode_session_id,
                    opencode_args=tuple(launch_config.terminal_launch_args or ()),
                ),
                env=opencode_terminal_env(server),
```

with:

```python
                command=tui_argv[0],
                args=tui_argv[1:],
                env=opencode_terminal_env(
                    server.auth_secret,
                    xdg_data_home=server.xdg_data_home,
                    xdg_config_home=server.xdg_config_home,
                ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_app_server.py tests/test_opencode_http_transport.py tests/test_opencode_native_bridge.py tests/runner/test_app_sessions_native_events_lifecycle.py -k "opencode or tui or terminal" -v && uv run python -c "import omnigent.runner.native.orchestration"`
Expected: PASS, and the import exits 0.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py omnigent/harnesses/opencode_native/app_server.py omnigent/harnesses/opencode_native/http_transport.py omnigent/native/native_server_transport.py omnigent/runner/native/orchestration.py tests/test_opencode_native_app_server.py tests/test_opencode_http_transport.py
git commit -m "feat(opencode-native): run the TUI with --server and OPENCODE_PASSWORD"
```

---

### Task 14: `serve --stdio` launch, per-session `OPENCODE_DB`, `/api/info` readiness, stdin shutdown

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py` (constants block from Task 13; new helper after `xdg_config_home_for_bridge_dir`, line ~443)
- Modify: `omnigent/harnesses/opencode_native/app_server.py:27-36, 85-95, 278-296, 327-368, 389-429, 498-504, 512-539, 554-566`
- Test: `tests/test_opencode_native_app_server.py`

**Interfaces:**
- Consumes: `OPENCODE_PASSWORD_ENV_VAR` and `OPENCODE_SERVER_PASSWORD_ENV_VAR` (Task 13).
- Produces:
  - `bridge.OPENCODE_DB_ENV_VAR = "OPENCODE_DB"`
  - `bridge.opencode_db_path_for_bridge_dir(bridge_dir: Path) -> Path` → `bridge_dir / "opencode.db"`
  - `build_opencode_serve_args(...)` now ends in `"--stdio"`.
  - `OpenCodeNativeServer.start()`:
    - spawns with `stdin=subprocess.PIPE`
    - polls `GET /api/info` until 200
    - sets `self.version` from the body
    - fails fast on 401
  - `OpenCodeNativeServer.close()` closes stdin, waits `_STDIN_CLOSE_GRACE_S = 3.0` s, then escalates to terminate, wait 10 s, then kill.
  - `OpenCodeNativeServer.__init__` accepts `user_data_store: bool = False` and stores it as `self.user_data_store`. The parameter is reserved for Stage 4's import server, which runs against the user's real data store; it is unused in this stage.
  - `filtered_server_env` drops the inherited `OPENCODE_CONFIG_DIR`, `OPENCODE_DB`, `OPENCODE_PASSWORD` and `OPENCODE_SERVER_PASSWORD`, and sets `OPENCODE_DB`. An `OPENCODE_*` value passed through `extra_env` still wins, because it is applied after the parent filter; Stage 3 relies on this.

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_app_server.py`, add `import subprocess` and `import httpx` to the top-level imports (after `import asyncio` and `import pytest` respectively). Add these module-level fakes after the imports:

```python
class _FakeStdin:
    """Stdin pipe stand-in; closing it can end the fake process."""

    def __init__(self, proc: _FakeProc) -> None:
        self._proc = proc
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self._proc.exits_on_stdin_close:
            self._proc.returncode = 0


class _FakeProc:
    """``Popen`` stand-in for a ``--stdio`` server."""

    pid = 4242

    def __init__(self, *, exits_on_stdin_close: bool = True) -> None:
        self.returncode: int | None = None
        self.terminated = False
        self.exits_on_stdin_close = exits_on_stdin_close
        self.stdin = _FakeStdin(self)

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("opencode", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def _mock_http(monkeypatch: pytest.MonkeyPatch, handler: object) -> None:
    """Route the readiness probe's ``httpx.AsyncClient`` to *handler*."""
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        appsrv.httpx,
        "AsyncClient",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),  # type: ignore[arg-type]
    )
```

Replace `test_build_serve_args_has_explicit_host_port` (lines 61-63) with:

```python
def test_build_serve_args_uses_stdio() -> None:
    args = build_opencode_serve_args(hostname="127.0.0.1", port=49231)
    assert args == ["serve", "--hostname", "127.0.0.1", "--port", "49231", "--stdio"]
```

In `test_build_argv` (line 164), the expected tail becomes `["serve", "--hostname", "127.0.0.1", "--port", "49231", "--stdio"]`.

Append these env tests:

```python
def test_filtered_server_env_sets_per_session_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["OPENCODE_DB"] == str(tmp_path / "opencode.db")


def test_filtered_server_env_drops_inherited_opencode_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A parent's config dir, DB, or password never reach the isolated server."""
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/home/user/.config/opencode")
    monkeypatch.setenv("OPENCODE_DB", "/home/user/.local/share/opencode/opencode.db")
    monkeypatch.setenv("OPENCODE_PASSWORD", "parent-secret")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "parent-secret")
    monkeypatch.setenv(
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OPENCODE_CONFIG_DIR,OPENCODE_DB,OPENCODE_PASSWORD"
    )
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert "OPENCODE_CONFIG_DIR" not in env
    assert env["OPENCODE_DB"] == str(tmp_path / "opencode.db")
    assert env["OPENCODE_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"


def test_filtered_server_env_extra_env_may_set_opencode_config(tmp_path: Path) -> None:
    """Launcher-supplied OpenCode env is applied after the parent filter."""
    env = filtered_server_env(
        bridge_dir=tmp_path,
        auth_secret="pw",
        extra_env={"OPENCODE_CONFIG": str(tmp_path / "opencode.json")},
    )
    assert env["OPENCODE_CONFIG"] == str(tmp_path / "opencode.json")
```

Replace `test_start_polls_until_ready` (lines 180-204) with:

```python
async def test_start_launches_stdio_server_with_stdin_pipe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    started: dict[str, object] = {}

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        started["argv"] = argv
        started["stdin"] = kwargs.get("stdin")
        started["env"] = kwargs.get("env")
        return _FakeProc()

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        started["ready"] = True

    monkeypatch.setattr(appsrv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    await server.start()
    assert started["ready"] is True
    assert started["argv"][1] == "serve"
    assert "--stdio" in started["argv"]
    assert started["stdin"] == subprocess.PIPE
    assert started["env"]["OPENCODE_DB"] == str(tmp_path / "opencode.db")
    assert server.process is not None
    assert server.process.pid == 4242
```

In `test_start_closes_process_when_readiness_is_cancelled` (lines 207-248) and `test_start_closes_process_when_readiness_fails` (lines 251-287):
- Delete the inline `class _FakeProc` definitions.
- Construct the process as `process = _FakeProc(exits_on_stdin_close=False)`, so these tests keep proving the terminate escalation.
- Add `assert process.stdin.closed is True` after the existing `assert process.terminated is True`.

Append the readiness and shutdown tests:

```python
async def test_wait_until_ready_polls_api_info_and_records_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    server.process = _FakeProc()  # type: ignore[assignment]
    seen: list[tuple[str, str]] = []
    statuses = iter([503, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("authorization", "")))
        status = next(statuses)
        if status != 200:
            return httpx.Response(status, json={"code": "service_starting"})
        return httpx.Response(
            200, json={"version": "2.0.18", "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}}
        )

    _mock_http(monkeypatch, handler)
    await server._wait_until_ready(attempts=3, delay=0)
    assert [path for path, _ in seen] == ["/api/info", "/api/info"]
    assert seen[0][1].startswith("Basic ")
    assert server.version == "2.0.18"


async def test_wait_until_ready_fails_fast_on_rejected_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    server.process = _FakeProc()  # type: ignore[assignment]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(401, json={"_tag": "UnauthorizedError", "message": "no"})

    _mock_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="rejected the per-session password"):
        await server._wait_until_ready(attempts=5, delay=0)
    assert calls == ["/api/info"]


async def test_close_stops_server_by_closing_stdin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    process = _FakeProc()
    server.process = process  # type: ignore[assignment]
    await server.close()
    assert process.stdin.closed is True
    assert process.terminated is False
    assert process.returncode == 0
    assert server.process is None


async def test_close_terminates_when_stdin_close_is_ignored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    process = _FakeProc(exits_on_stdin_close=False)
    server.process = process  # type: ignore[assignment]
    await server.close()
    assert process.stdin.closed is True
    assert process.terminated is True
    assert server.process is None


def test_server_reserves_user_data_store_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    default = OpenCodeNativeServer(bridge_dir=tmp_path, workspace=tmp_path, verify_version=False)
    assert default.user_data_store is False
    importer = OpenCodeNativeServer(
        bridge_dir=tmp_path, workspace=tmp_path, verify_version=False, user_data_store=True
    )
    assert importer.user_data_store is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_app_server.py -v`
Expected: FAIL with `AssertionError` on `--stdio` missing from argv, `KeyError: 'OPENCODE_DB'`, and the readiness probe hitting `/session`.

- [ ] **Step 3: Write minimal implementation**

`bridge.py`. After the `OPENCODE_DEFAULT_USERNAME` line from Task 13, add:

```python
# Per-session SQLite store ``opencode serve`` keeps sessions and credentials in.
OPENCODE_DB_ENV_VAR = "OPENCODE_DB"
_OPENCODE_DB_FILE = "opencode.db"
```

After `xdg_config_home_for_bridge_dir` (ends line 443), add:

```python
def opencode_db_path_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the per-session OpenCode SQLite path for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute ``opencode.db`` path, passed to the server as
        ``OPENCODE_DB``.
    """
    return bridge_dir / _OPENCODE_DB_FILE
```

`app_server.py:27-36`: add `import contextlib` after `import asyncio`.

The bridge import (as edited in Task 13) gains `OPENCODE_DB_ENV_VAR,` and `opencode_db_path_for_bridge_dir,`:

```python
from omnigent.harnesses.opencode_native.bridge import (
    OPENCODE_DB_ENV_VAR,
    OPENCODE_PASSWORD_ENV_VAR,
    OPENCODE_SERVER_PASSWORD_ENV_VAR,
    auth_headers_for_secret,
    ensure_auth_secret,
    opencode_db_path_for_bridge_dir,
    xdg_config_home_for_bridge_dir,
    xdg_data_home_for_bridge_dir,
)
```

`app_server.py:85-95`. Replace the `_ENV_OPENCODE_CONFIG_DENYLIST` comment and set with:

```python
# OpenCode env the parent must never leak into the isolated per-session server:
# global config paths, a foreign SQLite store, or another server's password.
# Dropped even though they match the ``OPENCODE_`` passthrough prefix; the
# launcher sets its own DB and password below.
_ENV_OPENCODE_DENYLIST = frozenset(
    {
        "OPENCODE_CONFIG",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_DB",
        "OPENCODE_PASSWORD",
        "OPENCODE_SERVER_PASSWORD",
    }
)
# How long a ``--stdio`` server gets to exit after its stdin closes.
_STDIN_CLOSE_GRACE_S = 3.0
```

In `filtered_server_env`, line 353 `if key in _ENV_OPENCODE_CONFIG_DENYLIST:` becomes `if key in _ENV_OPENCODE_DENYLIST:`. The comment under it becomes `# Never inherit the parent's OpenCode config, DB, or password.`

The tail of `filtered_server_env` (as edited in Task 13) becomes:

```python
    env.update(extra_env or {})
    env["XDG_DATA_HOME"] = str(xdg_data_home_for_bridge_dir(bridge_dir))
    env["XDG_CONFIG_HOME"] = str(xdg_config_home_for_bridge_dir(bridge_dir))
    env[OPENCODE_DB_ENV_VAR] = str(opencode_db_path_for_bridge_dir(bridge_dir))
    env[OPENCODE_PASSWORD_ENV_VAR] = auth_secret
    env[OPENCODE_SERVER_PASSWORD_ENV_VAR] = auth_secret
    return env
```

`build_opencode_serve_args` (lines 278-296) becomes:

```python
def build_opencode_serve_args(
    *,
    hostname: str,
    port: int,
    opencode_args: Sequence[str] = (),
) -> list[str]:
    """
    Build the ``opencode serve`` argv tail (after the executable).

    Always passes explicit ``--hostname``/``--port``. ``--stdio`` ties the
    server's lifetime to its stdin: it exits when the launcher closes the pipe,
    so a crashed runner never orphans it.

    :param hostname: Bind hostname, e.g. ``"127.0.0.1"``.
    :param port: Bind port.
    :param opencode_args: Extra pass-through args.
    :returns: Argv tail, e.g. ``["serve", "--hostname", "127.0.0.1",
        "--port", "49231", "--stdio"]``.
    """
    return ["serve", "--hostname", hostname, "--port", str(port), "--stdio", *opencode_args]
```

`OpenCodeNativeServer.__init__` (lines 404-429):
- Add `user_data_store: bool = False,` to the signature after `verify_version: bool = True,`.
- Add this to the class docstring (after `:param verify_version: …`):

```python
    :param user_data_store: Reserved for the session-import server, which runs
        against the user's real OpenCode data store; unused by per-session
        servers.
```

- After `self._verify_version = verify_version`, add:

```python
        # Reserved for the import server; per-session servers always isolate.
        self.user_data_store = user_data_store
```

The `Popen` call in `start()` (lines 498-504) becomes:

```python
        self.process = subprocess.Popen(
            argv,
            cwd=str(self.workspace),
            env=self.env,
            # ``--stdio`` serves until stdin closes; keep the pipe open.
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
```

Replace `_wait_until_ready` (lines 512-539) with:

```python
    async def _wait_until_ready(self, *, attempts: int = 60, delay: float = 0.5) -> None:
        """
        Poll ``GET /api/info`` until the server reports ready.

        The server answers 503 while booting and 200 once its routes are live;
        the ready body's ``version`` is recorded on :attr:`version`.

        :param attempts: Maximum readiness polls.
        :param delay: Seconds between polls.
        :raises RuntimeError: When the server rejects the password, exits early,
            or never becomes ready.
        """
        last_error = "no response"
        async with httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.auth_headers,
            timeout=httpx.Timeout(5.0, connect=2.0),
        ) as client:
            for _ in range(attempts):
                if self.process is not None and self.process.poll() is not None:
                    raise RuntimeError(
                        f"opencode serve exited early with code {self.process.returncode}"
                    )
                try:
                    response = await client.get("/api/info")
                except httpx.HTTPError as exc:
                    last_error = repr(exc)
                else:
                    if response.status_code == 200:
                        self._record_info(response)
                        return
                    if response.status_code == 401:
                        raise RuntimeError("opencode serve rejected the per-session password")
                    last_error = f"HTTP {response.status_code}"
                await asyncio.sleep(delay)
        raise RuntimeError(f"opencode serve did not become ready: {last_error}")

    def _record_info(self, response: httpx.Response) -> None:
        """
        Record the server-reported version from a ready ``/api/info`` response.

        :param response: The 200 response; its body is the bare ``ServerInfo``
            object ``{version, pid, urls, paths}``.
        """
        try:
            body = response.json()
        except ValueError:
            return
        version = body.get("version") if isinstance(body, dict) else None
        if isinstance(version, str) and version:
            self.version = version
```

Replace `close` (lines 554-566) with:

```python
    async def close(self) -> None:
        """Stop the server: close stdin (graceful ``--stdio`` exit), then escalate."""
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            with contextlib.suppress(OSError):
                process.stdin.close()
        if process.poll() is None:
            try:
                await asyncio.to_thread(process.wait, _STDIN_CLOSE_GRACE_S)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
        self.process = None
```

The worst case is 3 s + 10 s before kill. That is inside the runner's 20 s cleanup budget (`runner/app.py:203`, `_SESSION_INIT_CANCEL_TIMEOUT_S`), which `tests/server/routes/test_sessions_crud.py::test_delete_session_reaps_child_that_ignores_sigterm` exercises.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_app_server.py tests/test_opencode_native_bridge.py "tests/server/routes/test_sessions_crud.py::test_delete_session_reaps_child_that_ignores_sigterm" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py omnigent/harnesses/opencode_native/app_server.py tests/test_opencode_native_app_server.py
git commit -m "feat(opencode-native): launch opencode serve --stdio with a per-session DB"
```

---

### Task 15: Client envelope, `info`, and v2 session reads/writes

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py:1-16, 18-28, 43-82, 125-127, 142-165, 183-276`
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:197-210`
- Modify: `omnigent/runner/native/orchestration.py:1693`
- Test: `tests/test_opencode_native_client.py`
- Test: `tests/test_opencode_http_transport.py:85-87, 127-144`

**Interfaces:**
- Consumes: `OPENCODE_MIN_VERSION` and `OPENCODE_MAX_VERSION_EXCLUSIVE` (Task 10).
- Produces:
  - `_unwrap(body: object) -> object`
  - `OpenCodeClientError(message, *, status_code: int | None = None)` with a `.status_code` attribute.
  - `OpenCodeSession(id, title=None, parent_id=None, directory=None, model=None, raw={})`. `directory` is read from `location.directory`.
  - `OpenCodeClient.info() -> dict[str, object]`
  - `create_session(*, title: str, directory: str, permissions: list[dict[str, object]] | None = None, model: dict[str, object] | None = None, metadata: dict[str, object] | None = None) -> OpenCodeSession`
  - `get_session(session_id) -> OpenCodeSession | None` (404 → None)
  - `list_messages(session_id, *, after_id: str | None = None) -> list[dict[str, object]]`
  - `get_context(session_id) -> list[dict[str, object]]`
  - `list_root_sessions(*, limit: int = 100) -> list[OpenCodeSession]`, which calls `GET /api/session?parentID=null&order=desc&limit=N` and returns newest first. `openapi.json` documents `parentID`: "Use null to return only root sessions".
  - Kept unchanged for Stage 4:
    - the `OpenCodeClient(base_url, *, headers=None, directory=None, client=None)` constructor, including the `client=` injection and `self._client`
    - `client_for_state(*, base_url, auth_secret, directory=None)`
    - `OpenCodeClientError`
  - `OpenCodeSession` keeps every field except `id` defaulted. The new `model` field sits before `raw`, so construct it with keywords.
- Deleted:
  - `OpenCodeClient.get_message` (`client.py:267-276`)
  - v1 `create_session(payload)` (`:219-233`), `get_session` (`:235-252`) and `list_messages` (`:254-265`)

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_client.py`:
- Add `_unwrap` to the import from `client`.
- Delete these tests, whose v1 routes or shapes are gone:
  - `test_create_session` (29-40)
  - `test_get_session_404_returns_none` (43-49)
  - `test_get_session_found` (52-60)
  - `test_list_messages` (63-70)
  - `test_error_raises` (142-149)
  - `test_auth_and_directory_headers_applied` (152-164)
  - `test_create_session_non_object_body_raises` (210-214)
  - `test_get_message_non_dict_returns_empty` (236-239)

Then add:

```python
_SESSION = {
    "id": "ses_1",
    "projectID": "prj_1",
    "title": "omnigent:conv_1",
    "model": {"id": "big-pickle", "providerID": "opencode"},
    "location": {"directory": "/repo"},
    "cost": 0,
    "tokens": {"input": 0},
    "time": {"created": 1},
}


def test_unwrap_returns_data_or_bare_body() -> None:
    assert _unwrap({"data": [1], "cursor": {}}) == [1]
    assert _unwrap({"location": {"directory": "/r"}, "data": {"id": "m"}}) == {"id": "m"}
    assert _unwrap({"version": "2.0.18"}) == {"version": "2.0.18"}
    assert _unwrap(None) is None


async def test_info_reads_bare_server_info() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("GET", "/api/info")
        return httpx.Response(
            200, json={"version": "2.0.18", "pid": 7, "urls": [], "paths": {"tmp": "/t"}}
        )

    client = _client(handler)
    assert (await client.info())["version"] == "2.0.18"
    await client.aclose()


async def test_create_session_posts_v2_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": _SESSION})

    client = _client(handler)
    session = await client.create_session(
        title="omnigent:conv_1",
        directory="/repo",
        permissions=[{"action": "*", "resource": "*", "effect": "ask"}],
        metadata={"omnigent_conversation": "conv_1"},
    )
    assert (seen["method"], seen["path"]) == ("POST", "/api/session")
    assert seen["body"] == {
        "title": "omnigent:conv_1",
        "location": {"directory": "/repo"},
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],
        "metadata": {"omnigent_conversation": "conv_1"},
    }
    assert session == OpenCodeSession(
        id="ses_1",
        title="omnigent:conv_1",
        parent_id=None,
        directory="/repo",
        model={"id": "big-pickle", "providerID": "opencode"},
        raw=_SESSION,
    )
    await client.aclose()


async def test_create_session_passes_initial_model_only_when_given() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": _SESSION})

    client = _client(handler)
    await client.create_session(title="t", directory="/repo")
    await client.create_session(
        title="t", directory="/repo", model={"id": "big-pickle", "providerID": "opencode"}
    )
    assert bodies[0] == {"title": "t", "location": {"directory": "/repo"}}
    assert bodies[1]["model"] == {"id": "big-pickle", "providerID": "opencode"}
    await client.aclose()


async def test_create_session_non_object_body_raises() -> None:
    client = _client(lambda _r: httpx.Response(200, json={"data": ["x"]}))
    with pytest.raises(OpenCodeClientError):
        await client.create_session(title="t", directory="/repo")
    await client.aclose()


async def test_get_session_unwraps_and_reads_location() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1"
        return httpx.Response(200, json={"data": {**_SESSION, "parentID": "ses_0"}})

    client = _client(handler)
    session = await client.get_session("ses_1")
    assert session is not None
    assert session.parent_id == "ses_0"
    assert session.directory == "/repo"
    await client.aclose()


async def test_get_session_404_returns_none() -> None:
    client = _client(
        lambda _r: httpx.Response(
            404,
            json={"_tag": "SessionNotFoundError", "sessionID": "ses_x", "message": "nope"},
        )
    )
    assert await client.get_session("ses_x") is None
    await client.aclose()


async def test_list_messages_follows_cursor_pages() -> None:
    pages = {
        None: {"data": [{"id": "msg_1", "type": "user"}], "cursor": {"next": "c2"}},
        "c2": {"data": [{"id": "msg_2", "type": "assistant"}], "cursor": {"next": None}},
    }
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/message"
        seen.append(dict(request.url.params))
        return httpx.Response(200, json=pages[request.url.params.get("cursor")])

    client = _client(handler)
    messages = await client.list_messages("ses_1")
    assert [m["id"] for m in messages] == ["msg_1", "msg_2"]
    assert seen == [{"order": "asc"}, {"cursor": "c2"}]
    await client.aclose()


async def test_list_messages_after_id_returns_only_newer() -> None:
    body = {"data": [{"id": "msg_1"}, {"id": "msg_2"}, {"id": "msg_3"}], "cursor": {}}
    client = _client(lambda _r: httpx.Response(200, json=body))
    newer = await client.list_messages("ses_1", after_id="msg_1")
    assert [m["id"] for m in newer] == ["msg_2", "msg_3"]
    unknown = await client.list_messages("ses_1", after_id="msg_unknown")
    assert [m["id"] for m in unknown] == ["msg_1", "msg_2", "msg_3"]
    await client.aclose()


async def test_list_messages_stops_on_repeated_cursor() -> None:
    body = {"data": [{"id": "msg_1"}], "cursor": {"next": "same"}}
    client = _client(lambda _r: httpx.Response(200, json=body))
    assert [m["id"] for m in await client.list_messages("ses_1")] == ["msg_1", "msg_1"]
    await client.aclose()


async def test_list_root_sessions_queries_newest_roots() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={
                "data": [_SESSION, {**_SESSION, "id": "ses_0"}, "junk"],
                "cursor": {"next": None},
            },
        )

    client = _client(handler)
    sessions = await client.list_root_sessions(limit=5)
    assert (seen["method"], seen["path"]) == ("GET", "/api/session")
    assert seen["params"] == {"parentID": "null", "order": "desc", "limit": "5"}
    assert [s.id for s in sessions] == ["ses_1", "ses_0"]
    assert sessions[0].directory == "/repo"
    await client.aclose()


async def test_get_context_unwraps_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/context"
        return httpx.Response(200, json={"data": [{"id": "msg_9", "type": "compaction"}]})

    client = _client(handler)
    assert await client.get_context("ses_1") == [{"id": "msg_9", "type": "compaction"}]
    await client.aclose()


async def test_error_carries_status_code() -> None:
    client = _client(
        lambda _r: httpx.Response(500, json={"_tag": "UnknownError", "message": "boom"})
    )
    with pytest.raises(OpenCodeClientError) as exc_info:
        await client.info()
    assert exc_info.value.status_code == 500
    await client.aclose()


async def test_auth_and_directory_headers_applied() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("authorization", "")
        captured["dir"] = request.headers.get("x-opencode-directory", "")
        return httpx.Response(200, json={"data": [], "cursor": {}})

    client = _client(handler, headers={"Authorization": "Basic abc"}, directory="/repo/ünï dir")
    await client.list_messages("ses_1")
    assert captured["auth"] == "Basic abc"
    # The server URI-decodes this header, so non-ASCII paths are quoted.
    assert captured["dir"] == "/repo/%C3%BCn%C3%AF%20dir"
    await client.aclose()
```

Three existing tests stay valid and are kept:
- `test_get_session_server_error_raises` (lines 217-221)
- `test_get_session_non_object_returns_none` (lines 224-227)
- `test_list_messages_non_list_returns_empty` (lines 230-233)

`test_request_json_http_error_raises` (lines 255-259) is also kept.

In `tests/test_opencode_http_transport.py`, the fake `create_session` (lines 85-87) becomes:

```python
    async def create_session(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(("create_session", kwargs))
        return SimpleNamespace(id="ses_new")
```

`test_create_session_when_no_external_id` (lines 127-131) gains:

```python
    assert ("create_session", {"title": "omnigent:conv_1", "directory": "/w"}) in client.calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py -v`
Expected: FAIL with `ImportError: cannot import name '_unwrap'`.

- [ ] **Step 3: Write minimal implementation**

`client.py:1-16`. Replace the module docstring with:

```python
"""Typed HTTP + SSE client for an OpenCode 2.x ``opencode serve`` server.

Hand-shaped from the ``@opencode/cli`` 2.0.x OpenAPI (``/api/*`` routes). This
is a thin typed wrapper over the endpoints the Omnigent OpenCode-native harness
needs plus the SSE ``GET /api/event`` stream — not a full generated SDK.

Transport notes:

- REST + SSE over ``httpx.AsyncClient``; the server binds loopback only.
- Basic auth (``opencode:<OPENCODE_PASSWORD>``) is attached per request.
- JSON bodies arrive as ``{"data": ...}`` (or ``{"location", "data"}``);
  :func:`_unwrap` strips the envelope. ``/api/info`` and ``/interrupt``
  answer with bare objects, which :func:`_unwrap` passes through.
- SSE frames are ``data: {id, created, type, location?, data}`` lines;
  ``: heartbeat`` comments are skipped.
"""
```

`client.py:18-28`. The imports become:

```python
from __future__ import annotations

import json
import logging
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

import httpx

from omnigent.util.json_types import JsonObject as _JsonObject
```

Below `_JsonMapping: TypeAlias = Mapping[str, object]` (line 40), add:

```python
# Upper bound on message pages fetched by list_messages (guards a cursor loop).
_MAX_MESSAGE_PAGES = 1000


def _unwrap(body: object) -> object:
    """
    Strip OpenCode's response envelope.

    :param body: Decoded JSON, e.g. ``{"data": {...}}``,
        ``{"location": {...}, "data": [...]}``, or a bare ``/api/info`` object.
    :returns: ``body["data"]`` when present, else *body* unchanged.
    """
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body
```

`OpenCodeSession` (lines 43-82) becomes:

```python
@dataclass(frozen=True)
class OpenCodeSession:
    """
    An OpenCode session as returned by ``/api/session`` endpoints.

    :param id: OpenCode session id, e.g. ``"ses_abc123"``.
    :param title: Optional human-readable title.
    :param parent_id: Parent session id for child (subagent) sessions.
    :param directory: Session location directory, when reported.
    :param model: The session's ``{"id", "providerID", "variant"?}``, when set.
    :param raw: The full server payload for forward-compatibility.
    """

    id: str
    title: str | None = None
    parent_id: str | None = None
    directory: str | None = None
    model: dict[str, Any] | None = None
    raw: _JsonObject = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: _JsonMapping) -> OpenCodeSession:
        """
        Build an :class:`OpenCodeSession` from a ``Session.Info`` payload.

        :param payload: Decoded, unwrapped session object.
        :returns: Parsed session.
        :raises ValueError: When the payload has no string ``id``.
        """
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("OpenCode session payload missing string 'id'")
        title = payload.get("title")
        parent_id = payload.get("parentID")
        location = payload.get("location")
        directory = location.get("directory") if isinstance(location, Mapping) else None
        model = payload.get("model")
        return cls(
            id=session_id,
            title=title if isinstance(title, str) else None,
            parent_id=parent_id if isinstance(parent_id, str) else None,
            directory=directory if isinstance(directory, str) else None,
            model=dict(model) if isinstance(model, Mapping) else None,
            raw=dict(payload),
        )
```

`OpenCodeClientError` (lines 125-126) becomes:

```python
class OpenCodeClientError(RuntimeError):
    """
    Raised when an OpenCode REST call fails.

    :param message: Human-readable failure.
    :param status_code: HTTP status of the failing response, or ``None`` when
        the failure was not an HTTP status (e.g. a malformed body).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
```

In `OpenCodeClient.__init__` (lines 151-153), replace:

```python
        default_headers: dict[str, str] = dict(headers or {})
        if directory:
            default_headers.setdefault("x-opencode-directory", directory)
```

with:

```python
        default_headers: dict[str, str] = dict(headers or {})
        if directory:
            # The server URI-decodes this header, so non-ASCII paths survive.
            default_headers.setdefault(
                "x-opencode-directory", urllib.parse.quote(directory, safe="/")
            )
```

Replace the helpers and session methods (lines 183-276, from `# --- helpers ---` through `get_message`) with:

```python
    # --- helpers ---------------------------------------------------------

    async def _request_body(
        self,
        method: str,
        path: str,
        *,
        json_body: _JsonMapping | None = None,
        params: Mapping[str, str] | None = None,
    ) -> object:
        """
        Issue a request and return the decoded JSON body without unwrapping.

        :param method: HTTP method, e.g. ``"POST"``.
        :param path: Path relative to ``base_url``, e.g. ``"/api/session"``.
        :param json_body: Optional JSON request body.
        :param params: Optional query parameters.
        :returns: Decoded JSON, or ``None`` for an empty (e.g. 204) body.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        response = await self._client.request(
            method,
            path,
            json=dict(json_body) if json_body is not None else None,
            params=dict(params) if params is not None else None,
        )
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode {method} {path} failed: {response.status_code} {response.text[:500]}",
                status_code=response.status_code,
            )
        if not response.content:
            return None
        try:
            decoded: object = response.json()
        except json.JSONDecodeError:
            return None
        return decoded

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: _JsonMapping | None = None,
        params: Mapping[str, str] | None = None,
    ) -> object:
        """
        Issue a request and return the ``data``-unwrapped JSON body.

        :param method: HTTP method.
        :param path: Path relative to ``base_url``.
        :param json_body: Optional JSON request body.
        :param params: Optional query parameters.
        :returns: The unwrapped body (see :func:`_unwrap`).
        :raises OpenCodeClientError: On a non-2xx status.
        """
        body = await self._request_body(method, path, json_body=json_body, params=params)
        return _unwrap(body)

    # --- server ----------------------------------------------------------

    async def info(self) -> _JsonObject:
        """
        Fetch server info (``GET /api/info``).

        :returns: ``{"version": "2.0.18", "pid": ..., "urls": [...], "paths": {...}}``.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        data = await self._request_json("GET", "/api/info")
        if not isinstance(data, dict):
            raise OpenCodeClientError("OpenCode /api/info returned a non-object body")
        return data

    # --- sessions --------------------------------------------------------

    async def create_session(
        self,
        *,
        title: str,
        directory: str,
        permissions: list[_JsonObject] | None = None,
        model: _JsonObject | None = None,
        metadata: _JsonObject | None = None,
    ) -> OpenCodeSession:
        """
        Create a session (``POST /api/session``).

        :param title: Session title, e.g. ``"omnigent:conv_abc"``.
        :param directory: Workspace directory the session is located in.
        :param permissions: Session rules, e.g.
            ``[{"action": "*", "resource": "*", "effect": "ask"}]``.
        :param model: Initial ``{"id", "providerID", "variant"?}``.
        :param metadata: Free-form metadata, e.g.
            ``{"omnigent_conversation": "conv_abc"}``.
        :returns: The created session.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        body: _JsonObject = {"title": title, "location": {"directory": directory}}
        if permissions is not None:
            body["permissions"] = permissions
        if model is not None:
            body["model"] = model
        if metadata is not None:
            body["metadata"] = metadata
        data = await self._request_json("POST", "/api/session", json_body=body)
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode create_session returned a non-object body")
        return OpenCodeSession.from_payload(data)

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        """
        Fetch one session (``GET /api/session/{id}``).

        :param session_id: OpenCode session id.
        :returns: The session, or ``None`` when it does not exist (404).
        :raises OpenCodeClientError: On any other non-2xx status.
        """
        try:
            data = await self._request_json("GET", f"/api/session/{session_id}")
        except OpenCodeClientError as exc:
            if exc.status_code == 404:
                return None
            raise
        if not isinstance(data, Mapping):
            return None
        return OpenCodeSession.from_payload(data)

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[_JsonObject]:
        """
        List a session's messages, oldest first (``GET /api/session/{id}/message``).

        Follows ``cursor.next`` across pages until the server stops returning one.

        :param session_id: OpenCode session id.
        :param after_id: When set, only messages after this message id are
            returned; all messages are returned when the id is not found.
        :returns: v2 message objects, e.g. ``{"id": "msg_1", "type": "assistant", ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        path = f"/api/session/{session_id}/message"
        messages: list[_JsonObject] = []
        params: dict[str, str] = {"order": "asc"}
        seen_cursors: set[str] = set()
        for _ in range(_MAX_MESSAGE_PAGES):
            body = await self._request_body("GET", path, params=params)
            if not isinstance(body, dict):
                break
            page = body.get("data")
            if isinstance(page, list):
                messages.extend(item for item in page if isinstance(item, dict))
            cursor = body.get("cursor")
            next_cursor = cursor.get("next") if isinstance(cursor, dict) else None
            if not page or not isinstance(next_cursor, str) or next_cursor in seen_cursors:
                break
            seen_cursors.add(next_cursor)
            params = {"cursor": next_cursor}
        if after_id is None:
            return messages
        for index, message in enumerate(messages):
            if message.get("id") == after_id:
                return messages[index + 1 :]
        return messages

    async def list_root_sessions(self, *, limit: int = 100) -> list[OpenCodeSession]:
        """
        List top-level sessions, newest first (``GET /api/session?parentID=null``).

        :param limit: Maximum sessions to return.
        :returns: Root (non-subagent) sessions; malformed rows are skipped.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json(
            "GET",
            "/api/session",
            params={"parentID": "null", "order": "desc", "limit": str(limit)},
        )
        if not isinstance(data, list):
            return []
        return [
            OpenCodeSession.from_payload(item)
            for item in data
            if isinstance(item, Mapping) and isinstance(item.get("id"), str) and item.get("id")
        ]

    async def get_context(self, session_id: str) -> list[_JsonObject]:
        """
        Fetch the messages the model sees next turn (``GET .../context``).

        :param session_id: OpenCode session id.
        :returns: v2 message objects after the latest compaction boundary.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("GET", f"/api/session/{session_id}/context")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []
```

`http_transport.py:205-207`. Replace:

```python
            created = await client.create_session(
                {"title": f"omnigent:{launch.omnigent_session_id}"}
            )
```

with:

```python
            created = await client.create_session(
                title=f"omnigent:{launch.omnigent_session_id}",
                directory=launch.workspace,
            )
```

`orchestration.py:1693`. Replace `created = await client.create_session({"title": f"omnigent:{session_id}"})` with:

```python
                created = await client.create_session(
                    title=f"omnigent:{session_id}", directory=workspace
                )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py tests/runner/test_app_sessions_native_events_options.py -k "opencode or session or messages or info or unwrap or context or header" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/client.py omnigent/harnesses/opencode_native/http_transport.py omnigent/runner/native/orchestration.py tests/test_opencode_native_client.py tests/test_opencode_http_transport.py
git commit -m "feat(opencode-native): v2 client envelope, info, and session endpoints"
```

---

### Task 16: Client turn control (`prompt`, `seed_context`, `set_model`, `interrupt`, `compact`, `fork`)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py:292-303` (old `prompt`), `:331-384` (`summarize`, `seed_context`) and `:421-434` (`fork`)
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:251-259`
- Test: `tests/test_opencode_native_client.py`
- Test: `tests/test_opencode_http_transport.py:101-103, 177-183`

**Interfaces:**
- Consumes: `_request_json`, `OpenCodeClientError.status_code` and `OpenCodeSession` (Task 15).
- Produces:
  - `prompt(session_id, *, text: str, files: Sequence[Mapping[str, str]] | None = None, delivery: str = "steer", message_id: str | None = None) -> dict[str, object]`
  - `seed_context(session_id, text) -> None`
  - `set_model(session_id, *, provider_id: str, model_id: str, variant: str | None = None) -> None`
  - `interrupt(session_id) -> bool`
  - `compact(session_id) -> dict[str, object]`
  - `fork(session_id, *, before: str | None = None) -> OpenCodeSession`
- Deleted:
  - `summarize` (`client.py:331-355`)
  - v1 `prompt(session_id, payload)` (`:292-303`)
  - v1 `seed_context(..., provider_id, model_id)` (`:357-384`)
  - v1 `fork(session_id, payload)` (`:421-434`)
  - `prompt_async` and `abort` stay until Task 17 removes their last caller.
- Opens ledger items L3 and L4.

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_client.py`, delete:
- `test_fork` (107-115)
- `test_prompt_non_dict_returns_empty` (242-245)
- `test_summarize_posts_v1_endpoint_with_model` and `test_summarize_raises_on_error` (262-283)
- `test_seed_context_posts_noreply_message` and `test_seed_context_omits_model_when_absent` (286-314)

`test_fork_non_object_body_raises` (248-252) is kept. Add:

```python
async def test_prompt_posts_v2_body_and_unwraps() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"data": {"id": "msg_1", "sessionID": "ses_1", "type": "user"}}
        )

    client = _client(handler)
    result = await client.prompt(
        "ses_1",
        text="hi",
        files=[{"uri": "data:image/png;base64,AAAA", "name": "shot.png"}],
        delivery="queue",
        message_id="msg_1",
    )
    assert seen["path"] == "/api/session/ses_1/prompt"
    assert seen["body"] == {
        "text": "hi",
        "delivery": "queue",
        "files": [{"uri": "data:image/png;base64,AAAA", "name": "shot.png"}],
        "id": "msg_1",
    }
    assert result["id"] == "msg_1"
    await client.aclose()


async def test_prompt_defaults_to_steer_without_files() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "msg_1"}})

    client = _client(handler)
    await client.prompt("ses_1", text="hi", files=[])
    assert seen["body"] == {"text": "hi", "delivery": "steer"}
    await client.aclose()


async def test_seed_context_records_without_resuming() -> None:
    requests: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"data": {"id": "msg_1", "type": "user"}})

    client = _client(handler)
    await client.seed_context("ses_1", "prior transcript")
    assert requests == [
        ("/api/session/ses_1/prompt", {"text": "prior transcript", "resume": False})
    ]
    await client.aclose()


async def test_seed_context_falls_back_to_synthetic_on_rejection() -> None:
    requests: list[tuple[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, json.loads(request.content)))
        if request.url.path.endswith("/prompt"):
            return httpx.Response(
                400, json={"_tag": "InvalidRequestError", "message": "resume unsupported"}
            )
        return httpx.Response(200, json={"data": {"id": "msg_2", "type": "synthetic"}})

    client = _client(handler)
    await client.seed_context("ses_1", "ctx")
    assert [path for path, _ in requests] == [
        "/api/session/ses_1/prompt",
        "/api/session/ses_1/synthetic",
    ]
    assert requests[1][1] == {"text": "ctx", "resume": False}
    await client.aclose()


async def test_seed_context_server_error_is_not_retried() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(500, json={"_tag": "UnknownError", "message": "boom"})

    client = _client(handler)
    with pytest.raises(OpenCodeClientError):
        await client.seed_context("ses_1", "ctx")
    assert requests == ["/api/session/ses_1/prompt"]
    await client.aclose()


async def test_set_model_posts_model_ref() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = _client(handler)
    await client.set_model("ses_1", provider_id="opencode", model_id="big-pickle", variant="high")
    assert seen["path"] == "/api/session/ses_1/model"
    assert seen["body"] == {
        "model": {"id": "big-pickle", "providerID": "opencode", "variant": "high"}
    }
    await client.aclose()


@pytest.mark.parametrize("interrupted", [True, False])
async def test_interrupt_reads_bare_flag(interrupted: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/api/session/ses_1/interrupt")
        return httpx.Response(200, json={"interrupted": interrupted})

    client = _client(handler)
    assert await client.interrupt("ses_1") is interrupted
    await client.aclose()


async def test_compact_posts_without_model() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "msg_c", "type": "compaction"}})

    client = _client(handler)
    assert (await client.compact("ses_1"))["type"] == "compaction"
    assert seen == {"path": "/api/session/ses_1/compact", "body": {}}
    await client.aclose()


async def test_fork_before_message() -> None:
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/session/ses_1/fork"
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"data": {**_SESSION, "id": "ses_2"}})

    client = _client(handler)
    assert (await client.fork("ses_1", before="msg_3")).id == "ses_2"
    assert (await client.fork("ses_1")).id == "ses_2"
    assert bodies == [{"before": "msg_3"}, {}]
    await client.aclose()
```

In `tests/test_opencode_http_transport.py`, the fake `fork` (lines 101-103) becomes:

```python
    async def fork(self, session_id: str, *, before: str | None = None) -> SimpleNamespace:
        self.calls.append(("fork", (session_id, before)))
        return SimpleNamespace(id="ses_fork")
```

The two assertions in `test_fork_with_and_without_message_id` (lines 182-183) become:

```python
    assert ("fork", ("ses_1", "msg_9")) in client.calls
    assert ("fork", ("ses_1", None)) in client.calls
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py -v`
Expected: FAIL with `TypeError: OpenCodeClient.prompt() got an unexpected keyword argument 'text'` and `AttributeError: 'OpenCodeClient' object has no attribute 'set_model'`.

- [ ] **Step 3: Write minimal implementation**

`client.py`. Add `Sequence` to the `collections.abc` import:

```python
from collections.abc import AsyncIterator, Mapping, Sequence
```

Replace the v1 `prompt` (lines 292-303) with:

```python
    async def prompt(
        self,
        session_id: str,
        *,
        text: str,
        files: Sequence[Mapping[str, str]] | None = None,
        delivery: str = "steer",
        message_id: str | None = None,
    ) -> _JsonObject:
        """
        Admit a user prompt (``POST /api/session/{id}/prompt``).

        Returns once OpenCode has accepted the input; output streams over SSE.

        :param session_id: OpenCode session id.
        :param text: Prompt text.
        :param files: Attachments, each ``{"uri": "data:<mime>;base64,...", "name": ...}``.
        :param delivery: ``"steer"`` (join the active turn or start one) or
            ``"queue"`` (run after the active turn).
        :param message_id: Optional client-chosen ``msg_`` id.
        :returns: The admitted inbox entry, e.g. ``{"id": "msg_1", "type": "user", ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        body: _JsonObject = {"text": text, "delivery": delivery}
        if files:
            body["files"] = [dict(entry) for entry in files]
        if message_id is not None:
            body["id"] = message_id
        data = await self._request_json(
            "POST", f"/api/session/{session_id}/prompt", json_body=body
        )
        return data if isinstance(data, dict) else {}
```

Delete `summarize` and the v1 `seed_context` (lines 331-384). Put these in their place:

```python
    async def seed_context(self, session_id: str, text: str) -> None:
        """
        Record context in a session without running a turn.

        Used to rehydrate a fresh session with a prior transcript. Sends
        ``prompt {resume: false}``; when the server rejects that with a 4xx,
        falls back to ``POST .../synthetic {resume: false}``.

        :param session_id: OpenCode session id.
        :param text: Context to record, e.g. the rendered prior transcript.
        :raises OpenCodeClientError: When the prompt fails with a 5xx, or both
            calls fail.
        """
        body = {"text": text, "resume": False}
        try:
            await self._request_json("POST", f"/api/session/{session_id}/prompt", json_body=body)
        except OpenCodeClientError as exc:
            if exc.status_code is None or exc.status_code >= 500:
                raise
            await self._request_json(
                "POST", f"/api/session/{session_id}/synthetic", json_body=body
            )

    async def set_model(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        variant: str | None = None,
    ) -> None:
        """
        Switch the session's model (``POST /api/session/{id}/model``).

        :param session_id: OpenCode session id.
        :param provider_id: Provider id, e.g. ``"opencode"``.
        :param model_id: Model id within the provider, e.g. ``"big-pickle"``.
        :param variant: Optional model variant, e.g. ``"high"``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        model: _JsonObject = {"id": model_id, "providerID": provider_id}
        if variant is not None:
            model["variant"] = variant
        await self._request_json(
            "POST", f"/api/session/{session_id}/model", json_body={"model": model}
        )

    async def interrupt(self, session_id: str) -> bool:
        """
        Interrupt active work (``POST /api/session/{id}/interrupt``).

        :param session_id: OpenCode session id.
        :returns: ``True`` when an active execution was interrupted.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("POST", f"/api/session/{session_id}/interrupt")
        return bool(data.get("interrupted")) if isinstance(data, dict) else False

    async def compact(self, session_id: str) -> _JsonObject:
        """
        Queue a compaction (``POST /api/session/{id}/compact``).

        Progress arrives as ``session.compaction.*`` events.

        :param session_id: OpenCode session id.
        :returns: The queued compaction inbox entry.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json(
            "POST", f"/api/session/{session_id}/compact", json_body={}
        )
        return data if isinstance(data, dict) else {}
```

Replace the v1 `fork` (lines 421-434) with:

```python
    async def fork(self, session_id: str, *, before: str | None = None) -> OpenCodeSession:
        """
        Fork a session (``POST /api/session/{id}/fork``).

        :param session_id: Source OpenCode session id.
        :param before: Optional ``msg_`` id; the fork keeps history before it.
        :returns: The new forked session.
        :raises OpenCodeClientError: On a non-2xx status or a non-object body.
        """
        body: _JsonObject = {"before": before} if before is not None else {}
        data = await self._request_json(
            "POST", f"/api/session/{session_id}/fork", json_body=body
        )
        if not isinstance(data, Mapping):
            raise OpenCodeClientError("OpenCode fork returned a non-object body")
        return OpenCodeSession.from_payload(data)
```

`http_transport.py:251-259`, the transport `fork`, becomes:

```python
    async def fork(self, session_id: str, *, at_message_id: str | None = None) -> str:
        """Fork the session via ``POST /api/session/{id}/fork``."""
        client = self._client()
        try:
            forked = await client.fork(session_id, before=at_message_id)
            return forked.id
        finally:
            await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

Ledger items L3 and L4 open here. Check that `.venv/bin/pyrefly check` reports errors only at `orchestration.py:2145` and `runner/app.py:6930`.

```bash
git add omnigent/harnesses/opencode_native/client.py omnigent/harnesses/opencode_native/http_transport.py tests/test_opencode_native_client.py tests/test_opencode_http_transport.py
SKIP=pyrefly git commit -m "feat(opencode-native): v2 prompt, model switch, interrupt, compact, and fork"
```

---

### Task 17: v2 prompt payload and transport injection (removes `prompt_async` / `abort`)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:13-18, 43-126, 212-226`
- Modify: `omnigent/harnesses/opencode_native/client.py:305-329` (delete `prompt_async` and `abort`)
- Modify: `omnigent/inner/opencode_native_executor.py:56-58`
- Test: `tests/test_opencode_http_transport.py:25-67, 89-95, 147-157`
- Test: `tests/test_opencode_native_client.py:83-104`
- Test: `tests/inner/test_opencode_native_executor.py:26-42, 99-165, 196-304`

**Interfaces:**
- Consumes: `OpenCodeClient.prompt(...)` and `OpenCodeClient.interrupt(...)` (Task 16).
- Produces:
  - `PromptPayload(TypedDict)` with the fields `text: str`, `files: list[dict[str, str]]` and `delivery: str`.
  - `build_prompt_payload(text: str, attachments: Sequence[Mapping[str, object]], *, delivery: str = "steer") -> PromptPayload`, which returns `{"text", "files": [{"uri": "data:…", "name"?}], "delivery"}`. It never includes `system` or `model`.
  - `OpenCodeHttpTransport.send_prompt` reads `prompt.metadata["delivery"]`: `"queue"` gives queue, anything else gives steer.
  - `OpenCodeHttpTransport.abort` calls `client.interrupt`.
- Deleted:
  - `OpenCodeClient.prompt_async`, `OpenCodeClient.abort`
  - `http_transport._attachment_to_part`, `_mime_from_data_uri`, `_split_model`
  - `OpenCodeNativeExecutor._gate_system_prompt` (see ledger L8)

- [ ] **Step 1: Write the failing test**

`tests/test_opencode_http_transport.py`. Replace lines 25-67 (the payload tests) with:

```python
def test_build_prompt_payload_text_only() -> None:
    assert build_prompt_payload("hi", ()) == {"text": "hi", "files": [], "delivery": "steer"}


def test_build_prompt_payload_queue_delivery() -> None:
    assert build_prompt_payload("hi", (), delivery="queue")["delivery"] == "queue"


def test_build_prompt_payload_data_uri_attachments_become_files() -> None:
    body = build_prompt_payload(
        "look",
        (
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {
                "type": "input_file",
                "file_data": "data:application/pdf;base64,BBBB",
                "filename": "a.pdf",
            },
            {"type": "input_file", "url": "data:text/plain;base64,CCCC"},
            {"type": "input_image"},  # no uri → skipped
            {"type": "input_image", "image_url": "https://example.com/cat.png"},  # not inline
        ),
    )
    assert body["files"] == [
        {"uri": "data:image/png;base64,AAAA"},
        {"uri": "data:application/pdf;base64,BBBB", "name": "a.pdf"},
        {"uri": "data:text/plain;base64,CCCC"},
    ]
    assert set(body) == {"text", "files", "delivery"}
```

In `_FakeClient`, replace `prompt_async` and `abort` (lines 89-95) with:

```python
    async def prompt(self, session_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("prompt", (session_id, kwargs)))
        return {"id": "msg_1"}

    async def interrupt(self, session_id: str) -> bool:
        self.calls.append(("interrupt", session_id))
        return True
```

Replace `test_send_prompt_builds_payload_and_closes` and `test_abort` (lines 147-157) with:

```python
async def test_send_prompt_builds_payload_and_closes() -> None:
    client = _FakeClient()
    out = await _transport(client).send_prompt("ses_1", NativePrompt(text="hi"))
    assert out == {"id": "msg_1"}
    assert (
        "prompt",
        ("ses_1", {"text": "hi", "files": [], "delivery": "steer"}),
    ) in client.calls
    assert client.closed


async def test_send_prompt_honors_queue_delivery() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt(
        "ses_1", NativePrompt(text="later", metadata={"delivery": "queue"})
    )
    assert client.calls[-1] == (
        "prompt",
        ("ses_1", {"text": "later", "files": [], "delivery": "queue"}),
    )


async def test_send_prompt_drops_system_prompt() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt(
        "ses_1", NativePrompt(text="hi", system_prompt="be brief")
    )
    assert "system" not in client.calls[-1][1][1]


async def test_abort_interrupts() -> None:
    client = _FakeClient()
    assert await _transport(client).abort("ses_1") is True
    assert ("interrupt", "ses_1") in client.calls
```

`tests/test_opencode_native_client.py`: delete `test_prompt_async_posts_parts` and `test_abort_returns_bool` (lines 83-104).

`tests/inner/test_opencode_native_executor.py`. Replace `_FakeServer` (lines 26-42) with:

```python
class _FakeServer:
    """Records the requests a fake OpenCode v2 server receives."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content)
            except json.JSONDecodeError:
                body = {}
        self.requests.append((request.method, request.url.path, body))
        if request.url.path.endswith("/interrupt"):
            return httpx.Response(200, json={"interrupted": True})
        if request.url.path.endswith("/model"):
            return httpx.Response(204)
        return httpx.Response(200, json={"data": {"id": "msg_1", "type": "user"}})


def _prompts(server: _FakeServer) -> list[dict[str, Any]]:
    return [body for _, path, body in server.requests if path == "/api/session/ses_1/prompt"]
```

Replace lines 99-165 (`test_run_turn_injects_prompt_and_completes`, `test_run_turn_with_blocks`, `test_run_turn_pins_resolved_model_on_prompt`, `test_run_turn_omits_model_when_no_override`) with:

```python
async def test_run_turn_injects_prompt_and_completes(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [TurnComplete]
    assert _prompts(fake_server) == [{"text": "hello", "delivery": "steer"}]


async def test_run_turn_with_blocks(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(
        executor,
        [
            {"type": "input_text", "text": "what is this?"},
            {"type": "input_image", "image_url": _PNG_DATA_URI},
        ],
    )
    assert [type(e) for e in events] == [TurnComplete]
    body = _prompts(fake_server)[0]
    assert body["text"] == "what is this?"
    assert body["files"] == [{"uri": _PNG_DATA_URI}]
    # No inline base64 in the text.
    assert _PNG_B64 not in body["text"]
```

Replace `test_interrupt_calls_abort` and `test_enqueue_message_injects_prompt` (lines 196-213) with:

```python
async def test_interrupt_calls_interrupt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.interrupt_session("k") is True
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/interrupt"]


async def test_enqueue_message_injects_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.enqueue_session_message("k", "steer me") is True
    assert [body["text"] for body in _prompts(fake_server)] == ["steer me"]
```

Keep `_run_with_system_prompt` (lines 216-224). Delete `_prompt_system_fields` and the five system tests (lines 227-304), and add:

```python
async def test_run_turn_never_sends_system_field(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2 has no per-prompt system field; instructions ship in the config."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    events = await _run_with_system_prompt(executor, "hello", "Be concise.")
    assert [type(e) for e in events] == [TurnComplete]
    assert "system" not in _prompts(fake_server)[0]
    assert await executor.enqueue_session_message("k", "later") is True
    assert "system" not in _prompts(fake_server)[1]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_http_transport.py tests/inner/test_opencode_native_executor.py -v`
Expected: FAIL with `TypeError: build_prompt_payload() takes 1 positional argument but 2 were given`, and executor tests reporting `assert [] == [{'text': 'hello', 'delivery': 'steer'}]` (requests still go to `/prompt_async`).

- [ ] **Step 3: Write minimal implementation**

`http_transport.py:13-18`. The imports become:

```python
from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from pathlib import Path
from typing import TypeAlias, TypedDict
```

`http_transport.py:43-126`. Replace `__all__` and everything from `build_prompt_payload` through `_split_model` with:

```python
# Public surface of this transport module. ``ClientFactory`` is the documented
# annotation for ``OpenCodeHttpTransport(client_factory=...)``; export it so the
# alias reads as intended public API (its only other use is a PEP 563 stringified
# annotation, which static analysis can't see as a load).
__all__ = ["ClientFactory", "OpenCodeHttpTransport", "PromptPayload", "build_prompt_payload"]


class PromptPayload(TypedDict):
    """Keyword arguments for :meth:`OpenCodeClient.prompt`."""

    text: str
    files: list[dict[str, str]]
    delivery: str


def build_prompt_payload(
    text: str,
    attachments: Sequence[Mapping[str, object]],
    *,
    delivery: str = "steer",
) -> PromptPayload:
    """
    Build the ``POST /api/session/{id}/prompt`` fields for one prompt.

    Attachments carrying a ``data:`` URI become ``files`` entries; OpenCode
    decodes them server-side (images and PDFs as media, ``text/plain``
    inlined as text). The system prompt and model are not prompt fields in
    v2: instructions ship in the config and the model is switched per session.

    :param text: User text.
    :param attachments: ``input_image`` / ``input_file`` content blocks.
    :param delivery: ``"steer"`` for a normal turn, ``"queue"`` for an
        enqueued one.
    :returns: ``{"text": ..., "files": [{"uri": ..., "name"?: ...}], "delivery": ...}``.
    """
    files: list[dict[str, str]] = []
    for attachment in attachments:
        entry = _attachment_to_file(attachment)
        if entry is not None:
            files.append(entry)
    return {"text": text, "files": files, "delivery": delivery}


def _attachment_to_file(attachment: Mapping[str, object]) -> dict[str, str] | None:
    """
    Convert an Omnigent attachment block into an OpenCode ``files`` entry.

    :param attachment: An ``input_image`` / ``input_file`` content block.
    :returns: ``{"uri": "data:...", "name"?: ...}``, or ``None`` when the block
        has no inline ``data:`` URI (OpenCode reads only ``data:`` and local
        ``file:`` URIs, and a runner-side path is meaningless to it).
    """
    block_type = attachment.get("type")
    if block_type == "input_image":
        uri = attachment.get("image_url")
    elif block_type == "input_file":
        uri = attachment.get("file_data") or attachment.get("url")
    else:
        return None
    if not isinstance(uri, str) or not uri.startswith("data:"):
        return None
    entry = {"uri": uri}
    filename = attachment.get("filename")
    if isinstance(filename, str) and filename:
        entry["name"] = filename
    return entry
```

`http_transport.py:212-226`. Replace `send_prompt` and `abort` with:

```python
    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> _JsonMapping:
        """Inject a prompt via ``POST /api/session/{id}/prompt``."""
        delivery = "queue" if prompt.metadata.get("delivery") == "queue" else "steer"
        payload = build_prompt_payload(prompt.text, prompt.attachments, delivery=delivery)
        client = self._client()
        try:
            return await client.prompt(
                session_id,
                text=payload["text"],
                files=payload["files"],
                delivery=payload["delivery"],
            )
        finally:
            await client.aclose()

    async def abort(self, session_id: str) -> bool:
        """Interrupt active work via ``POST /api/session/{id}/interrupt``."""
        client = self._client()
        try:
            return await client.interrupt(session_id)
        finally:
            await client.aclose()
```

`client.py`: delete `prompt_async` (the block starting `async def prompt_async`, original lines 305-319) and `abort` (original lines 321-329).

`opencode_native_executor.py:56-58`: delete the `_gate_system_prompt` override. The base class then discards the system prompt; ledger L8 hands delivery to Stage 3's config `instructions`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_http_transport.py tests/inner/test_opencode_native_executor.py tests/test_opencode_native_client.py tests/test_native_server_harness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/http_transport.py omnigent/harnesses/opencode_native/client.py omnigent/inner/opencode_native_executor.py tests/test_opencode_http_transport.py tests/test_opencode_native_client.py tests/inner/test_opencode_native_executor.py
SKIP=pyrefly git commit -m "feat(opencode-native): inject web turns via v2 /prompt with data-URI files"
```

---

### Task 18: Switch the session model with `set_model` before `prompt`

**Files:**
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:26` (import), `:142-153` (`__init__`) and `send_prompt` (Task 17); add `_apply_model` and `_split_model_id`
- Modify: `omnigent/inner/opencode_native_executor.py:60-86` (docstring only)
- Test: `tests/inner/test_opencode_native_executor.py:63-79` and new model tests
- Test: `tests/test_opencode_http_transport.py` (fake `set_model` and a new test)

**Interfaces:**
- Consumes:
  - `OpenCodeClient.set_model(session_id, *, provider_id, model_id, variant=None)` (Task 16)
  - `OpenCodeNativeBridgeState.last_applied_model` and `update_last_applied_model(bridge_dir, model) -> bool` (Task 12)
- Produces: `OpenCodeHttpTransport.send_prompt` calls `set_model` first when `prompt.model` is a qualified `provider/model` that differs from the last applied model. The last applied model is read from bridge state when a `bridge_dir` is known, and otherwise from the transport's own memory. On success it records the new value.

- [ ] **Step 1: Write the failing test**

`tests/inner/test_opencode_native_executor.py`. Add `read_bridge_state` and `update_model_override` to the bridge import (lines 13-17). `_seed_state` (lines 63-79) gains a `last_applied_model` parameter:

```python
def _seed_state(
    bridge_dir: Path,
    *,
    session_id: str = "conv_1",
    opencode_session_id: str = "ses_1",
    model_override: str | None = None,
    last_applied_model: str | None = None,
) -> None:
    write_bridge_state(
        bridge_dir,
        OpenCodeNativeBridgeState(
            session_id=session_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id=opencode_session_id,
            auth_secret="pw",
            model_override=model_override,
            last_applied_model=last_applied_model,
        ),
    )
```

Add after `test_run_turn_with_blocks`:

```python
async def test_run_turn_switches_model_before_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The override reaches OpenCode via POST /model before the first prompt."""
    _seed_state(tmp_path, model_override="anthropic/claude-opus-4")
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [TurnComplete]
    assert [path for _, path, _ in fake_server.requests] == [
        "/api/session/ses_1/model",
        "/api/session/ses_1/prompt",
    ]
    assert fake_server.requests[0][2] == {
        "model": {"id": "claude-opus-4", "providerID": "anthropic"}
    }
    assert "model" not in _prompts(fake_server)[0]
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.last_applied_model == "anthropic/claude-opus-4"


async def test_run_turn_skips_model_switch_when_already_applied(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(
        tmp_path,
        model_override="anthropic/claude-opus-4",
        last_applied_model="anthropic/claude-opus-4",
    )
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "hello")
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/prompt"]


async def test_run_turn_switches_again_after_override_changes(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path, model_override="acme/one")
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "first")
    await _run(executor, "again")
    assert update_model_override(tmp_path, "acme/two") is True
    await _run(executor, "second")
    model_bodies = [body for _, path, body in fake_server.requests if path.endswith("/model")]
    assert model_bodies == [
        {"model": {"id": "one", "providerID": "acme"}},
        {"model": {"id": "two", "providerID": "acme"}},
    ]


async def test_run_turn_without_override_never_switches_model(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    await _run(executor, "hello")
    assert [path for _, path, _ in fake_server.requests] == ["/api/session/ses_1/prompt"]


async def test_run_turn_model_switch_failure_errors_without_prompting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = _FakeServer()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/model"):
            server.requests.append(("POST", request.url.path, {}))
            return httpx.Response(400, json={"_tag": "InvalidRequestError", "message": "bad"})
        return server.handler(request)

    def fake_client_for_state(
        *, base_url: str, auth_secret: str | None, directory: str | None = None
    ) -> OpenCodeClient:
        mock = httpx.AsyncClient(
            base_url="http://opencode.test", transport=httpx.MockTransport(handler)
        )
        return OpenCodeClient("http://opencode.test", client=mock)

    monkeypatch.setattr(transport_mod, "client_for_state", fake_client_for_state)
    _seed_state(tmp_path, model_override="acme/missing")
    executor = _executor(tmp_path, monkeypatch)
    events = await _run(executor, "hello")
    assert [type(e) for e in events] == [ExecutorError]
    assert _prompts(server) == []
    state = read_bridge_state(tmp_path)
    assert state is not None
    assert state.last_applied_model is None
```

`tests/test_opencode_http_transport.py`. Add to `_FakeClient`:

```python
    async def set_model(
        self,
        session_id: str,
        *,
        provider_id: str,
        model_id: str,
        variant: str | None = None,
    ) -> None:
        self.calls.append(("set_model", (session_id, provider_id, model_id)))
```

and append:

```python
async def test_send_prompt_switches_model_once_without_bridge_state() -> None:
    client = _FakeClient()
    transport = _transport(client)
    await transport.send_prompt("ses_1", NativePrompt(text="a", model="acme/model-x"))
    await transport.send_prompt("ses_1", NativePrompt(text="b", model="acme/model-x"))
    assert [c for c in client.calls if c[0] == "set_model"] == [
        ("set_model", ("ses_1", "acme", "model-x"))
    ]
    assert [c[0] for c in client.calls] == ["set_model", "prompt", "prompt"]


async def test_send_prompt_ignores_unqualified_model() -> None:
    client = _FakeClient()
    await _transport(client).send_prompt("ses_1", NativePrompt(text="a", model="just-a-name"))
    assert [c[0] for c in client.calls] == ["prompt"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/inner/test_opencode_native_executor.py tests/test_opencode_http_transport.py -k "model" -v`
Expected: FAIL with `assert ['/api/session/ses_1/prompt'] == ['/api/session/ses_1/model', '/api/session/ses_1/prompt']`.

- [ ] **Step 3: Write minimal implementation**

`http_transport.py:26`. The bridge import becomes:

```python
from omnigent.harnesses.opencode_native.bridge import (
    read_bridge_state,
    update_last_applied_model,
)
```

In `OpenCodeHttpTransport.__init__` (lines 150-153), add a fifth assignment after `self._directory = directory`:

```python
        # Last model pushed via POST /model when no bridge state records it.
        self._last_applied_model: str | None = None
```

`send_prompt` (as written in Task 17) becomes:

```python
    async def send_prompt(self, session_id: str, prompt: NativePrompt) -> _JsonMapping:
        """Switch the model if needed, then inject via ``POST /api/session/{id}/prompt``."""
        delivery = "queue" if prompt.metadata.get("delivery") == "queue" else "steer"
        payload = build_prompt_payload(prompt.text, prompt.attachments, delivery=delivery)
        client = self._client()
        try:
            if prompt.model:
                await self._apply_model(client, session_id, prompt.model)
            return await client.prompt(
                session_id,
                text=payload["text"],
                files=payload["files"],
                delivery=payload["delivery"],
            )
        finally:
            await client.aclose()

    async def _apply_model(self, client: OpenCodeClient, session_id: str, model: str) -> None:
        """
        Switch the OpenCode session to *model* when it differs from the last one applied.

        The last applied model lives in bridge state so a respawned harness
        process does not resend an unchanged switch every turn.

        :param client: Open client for the session's server.
        :param session_id: OpenCode session id.
        :param model: Qualified ``provider/model`` id, e.g. ``"opencode/big-pickle"``.
        :raises OpenCodeClientError: When OpenCode rejects the switch.
        """
        split = _split_model_id(model)
        if split is None:
            _logger.warning("opencode-native: ignoring unqualified model id %r", model)
            return
        state = read_bridge_state(self._bridge_dir) if self._bridge_dir is not None else None
        last_applied = state.last_applied_model if state is not None else self._last_applied_model
        if last_applied == model:
            return
        provider_id, model_id = split
        await client.set_model(session_id, provider_id=provider_id, model_id=model_id)
        self._last_applied_model = model
        if self._bridge_dir is not None:
            update_last_applied_model(self._bridge_dir, model)
```

Add at module level, after `_attachment_to_file`:

```python
def _split_model_id(model: str) -> tuple[str, str] | None:
    """
    Split a qualified model id at its first slash.

    :param model: e.g. ``"openrouter/acme/model-x"``.
    :returns: ``("openrouter", "acme/model-x")``, or ``None`` when *model* has
        no provider prefix.
    """
    provider, sep, model_id = model.partition("/")
    if sep and provider and model_id:
        return provider, model_id
    return None
```

`opencode_native_executor.py:60-78`. Replace the `_build_prompt_with_model_override` docstring (the code is unchanged) with:

```python
        """
        Build a prompt carrying the session's model override.

        The transport switches the OpenCode session to ``prompt.model`` via
        ``POST /api/session/{id}/model`` before admitting the prompt, but only
        when it differs from ``last_applied_model`` in bridge state, so the
        override governs the run from the first injected turn. A per-turn
        ``config.model`` still wins: the base ``run_turn`` only fills the model
        when the prompt leaves it unset.

        :param content: Executor message content (string or content blocks).
        :returns: The prompt with the resolved model applied, or ``None``
            when there is nothing to send.
        """
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/inner/test_opencode_native_executor.py tests/test_opencode_http_transport.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/http_transport.py omnigent/inner/opencode_native_executor.py tests/inner/test_opencode_native_executor.py tests/test_opencode_http_transport.py
SKIP=pyrefly git commit -m "feat(opencode-native): switch the session model via POST /model before prompting"
```

---

### Task 19: Enqueued messages use `delivery: "queue"`

**Files:**
- Modify: `omnigent/native/native_server_harness.py:1-16` (docstring), `:221-257` (`enqueue_session_message`), plus a new helper after `_with_system_prompt`
- Modify: `omnigent/inner/opencode_native_executor.py:47-50` (comment)
- Test: `tests/test_native_server_harness.py:155-158`
- Test: `tests/inner/test_opencode_native_executor.py` (`test_enqueue_message_injects_prompt`)

**Interfaces:**
- Consumes: `OpenCodeHttpTransport.send_prompt` honoring `prompt.metadata["delivery"] == "queue"` (Task 17).
- Produces: `NativeServerHarness.enqueue_session_message` sends `NativePrompt(..., metadata={**prompt.metadata, "delivery": "queue"})`. `run_turn` leaves the metadata unset, so those turns are sent with `steer`.

- [ ] **Step 1: Write the failing test**

`tests/test_native_server_harness.py:155-158` becomes:

```python
async def test_enqueue_injects_prompt() -> None:
    transport = _FakeTransport()
    assert await _harness(transport).enqueue_session_message("k", "steer") is True
    assert transport.prompts == [
        ("ses_1", NativePrompt(text="steer", metadata={"delivery": "queue"}))
    ]
```

In `tests/inner/test_opencode_native_executor.py`, `test_enqueue_message_injects_prompt` becomes:

```python
async def test_enqueue_message_injects_queued_prompt(
    fake_server: _FakeServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mid-turn web message waits for the active turn (delivery=queue)."""
    _seed_state(tmp_path)
    executor = _executor(tmp_path, monkeypatch)
    assert await executor.enqueue_session_message("k", "steer me") is True
    assert _prompts(fake_server) == [{"text": "steer me", "delivery": "queue"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_native_server_harness.py::test_enqueue_injects_prompt tests/inner/test_opencode_native_executor.py::test_enqueue_message_injects_queued_prompt -v`
Expected: FAIL with `assert [{'delivery': 'steer', 'text': 'steer me'}] == [{'text': 'steer me', 'delivery': 'queue'}]`.

- [ ] **Step 3: Write minimal implementation**

`native_server_harness.py:14-15`. The docstring bullet `- ``interrupt_session`` and ``enqueue_session_message`` route through the` / `transport's ``abort`` / ``send_prompt``.` becomes:

```python
- ``interrupt_session`` and ``enqueue_session_message`` route through the
  transport's ``abort`` / ``send_prompt``; enqueued prompts carry
  ``metadata["delivery"] = "queue"`` so the native server runs them after
  the active turn.
```

In `enqueue_session_message` (lines 221-257), replace the docstring's second paragraph:

```python
        OpenCode has no live-steer endpoint, so the message is admitted as
        a new prompt; the native server's own queue promotes it when the
        active turn finishes.
```

with:

```python
        The prompt is marked ``metadata["delivery"] = "queue"`` so a native
        server with an inbox (OpenCode) runs it after the active turn instead
        of steering it.
```

Then replace:

```python
        prompt = self._build_prompt(content)
        if prompt is None or prompt.is_empty():
            return False
```

with:

```python
        prompt = self._build_prompt(content)
        if prompt is None or prompt.is_empty():
            return False
        prompt = _with_queue_delivery(prompt)
```

Append at the end of the module:

```python
def _with_queue_delivery(prompt: NativePrompt) -> NativePrompt:
    """
    Return a copy of *prompt* marked for queued delivery.

    :param prompt: The prompt to copy.
    :returns: A prompt whose ``metadata["delivery"]`` is ``"queue"``.
    """
    import dataclasses

    return dataclasses.replace(prompt, metadata={**prompt.metadata, "delivery": "queue"})
```

`opencode_native_executor.py:47-49`. Replace the comment:

```python
            # OpenCode has no live-steer endpoint, so a mid-turn message is
            # admitted as a new prompt and the native server's own queue
            # promotes it when the active turn finishes.
```

with:

```python
            # A mid-turn web message is admitted with delivery="queue" and
            # OpenCode's inbox runs it when the active turn finishes.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_native_server_harness.py tests/inner/test_opencode_native_executor.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/native/native_server_harness.py omnigent/inner/opencode_native_executor.py tests/test_native_server_harness.py tests/inner/test_opencode_native_executor.py
SKIP=pyrefly git commit -m "feat(opencode-native): queue enqueued web messages behind the active turn"
```

---

### Task 20: Client permission, form, and catalog calls (removes the v1 permission and question APIs)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py:278-290` (`list_models`), `:386-419` (`reply_question`, `reject_question`) and `:436-465` (`list_permissions`, `reply_permission`)
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:28-34, 261-271`
- Modify: `omnigent/native/native_server_transport.py:17, 101-113, 162-164`
- Test: `tests/test_opencode_native_client.py:73-80, 118-139, 317-354`
- Test: `tests/test_opencode_http_transport.py:19-23, 105-107, 186-191`

**Interfaces:**
- Consumes: `_request_json`, `OpenCodeClientError` (Task 15).
- Produces:
  - `reply_permission(session_id: str, request_id: str, decision: str, message: str | None = None) -> bool`, where `decision` must be `"once"` or `"reject"`; anything else raises `ValueError`.
    - `message` is left out of the body when it is `None`.
    - v2 (`packages/core/src/permission.ts:250-252`) treats a decline that carries feedback as a correction, which the model continues from. A plain `{"decision": "reject"}` stops the tool.
    - The forwarder should pass `message` only when it deliberately wants the model to continue.
  - `reply_form(session_id: str, form_id: str, answer: Mapping[str, object]) -> bool`
  - `cancel_form(session_id: str, form_id: str) -> bool`
  - `list_models() -> list[dict[str, object]]` (v2 `Model.Info` rows)
  - `list_providers() -> list[dict[str, object]]`
  - `connect_provider_key(provider_id: str, api_key: str) -> bool`
- Deleted:
  - `OpenCodeClient.list_permissions`, the v1 `reply_permission`, `reply_question`, `reject_question`
  - `OpenCodeHttpTransport.reply_permission` (dead: no caller)
  - `NativeServerTransport.reply_permission`
  - `NativePermissionDecision`
- Opens ledger items L2 and L5.

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_client.py`, delete:
- `test_list_models` (73-80)
- `test_reply_permission` (118-129)
- `test_list_permissions` (132-139)
- `test_reply_question_posts_global_endpoint`, `test_reply_question_raises_on_error` and `test_reject_question_posts_global_endpoint` (317-354)

Add:

```python
async def test_reply_permission_posts_decision() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = _client(handler)
    assert await client.reply_permission("ses_1", "per_1", "once") is True
    assert seen["path"] == "/api/session/ses_1/permission/per_1/reply"
    assert seen["body"] == {"decision": "once"}
    await client.aclose()


async def test_reply_permission_plain_reject_omits_message() -> None:
    """A reject with feedback lets the model continue, so a plain reject sends none."""
    bodies: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(204)

    client = _client(handler)
    await client.reply_permission("ses_1", "per_1", "reject")
    await client.reply_permission("ses_1", "per_2", "reject", message="use the test runner")
    assert bodies == [
        {"decision": "reject"},
        {"decision": "reject", "message": "use the test runner"},
    ]
    await client.aclose()


async def test_reply_permission_refuses_always() -> None:
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(204)

    client = _client(handler)
    with pytest.raises(ValueError):
        await client.reply_permission("ses_1", "per_1", "always")
    assert requests == []
    await client.aclose()


async def test_reply_permission_http_error_raises() -> None:
    client = _client(
        lambda _r: httpx.Response(
            404,
            json={"_tag": "PermissionNotFoundError", "requestID": "per_x", "message": "gone"},
        )
    )
    with pytest.raises(OpenCodeClientError) as exc_info:
        await client.reply_permission("ses_1", "per_x", "reject", message="denied by policy")
    assert exc_info.value.status_code == 404
    await client.aclose()


async def test_reply_form_posts_typed_answer() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(204)

    client = _client(handler)
    answer = {"indent": "tabs", "count": 3, "confirm": True, "langs": ["py", "ts"]}
    assert await client.reply_form("ses_1", "frm_1", answer) is True
    assert seen["path"] == "/api/session/ses_1/form/frm_1/reply"
    assert seen["body"] == {"answer": answer}
    await client.aclose()


async def test_cancel_form_deletes() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"], seen["path"] = request.method, request.url.path
        return httpx.Response(204)

    client = _client(handler)
    assert await client.cancel_form("ses_1", "frm_1") is True
    assert seen == {"method": "DELETE", "path": "/api/session/ses_1/form/frm_1"}
    await client.aclose()


async def test_list_models_unwraps_location_envelope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/model"
        return httpx.Response(
            200,
            json={
                "location": {"directory": "/repo"},
                "data": [{"id": "big-pickle", "providerID": "opencode", "name": "Big Pickle"}],
            },
        )

    client = _client(handler)
    assert await client.list_models() == [
        {"id": "big-pickle", "providerID": "opencode", "name": "Big Pickle"}
    ]
    await client.aclose()


async def test_list_providers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/provider"
        return httpx.Response(
            200, json={"location": {"directory": "/repo"}, "data": [{"id": "opencode"}]}
        )

    client = _client(handler)
    assert await client.list_providers() == [{"id": "opencode"}]
    await client.aclose()


async def test_connect_provider_key() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"data": {"id": "crd_1"}})

    client = _client(handler)
    assert await client.connect_provider_key("anthropic", "sk-test") is True
    assert seen == {
        "path": "/api/integration/anthropic/connect/key",
        "body": {"key": "sk-test"},
    }
    await client.aclose()
```

In `tests/test_opencode_http_transport.py`:
- Remove `NativePermissionDecision,` from the import (line 21).
- Delete the fake `reply_permission` (lines 105-107).
- Delete `test_reply_permission_maps_decision` (lines 186-191).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_client.py -v`
Expected: FAIL with `TypeError: OpenCodeClient.reply_permission() takes 3 positional arguments but 4 were given` and `AttributeError: … has no attribute 'reply_form'`.

- [ ] **Step 3: Write minimal implementation**

In `client.py`, below `_MAX_MESSAGE_PAGES`, add:

```python
# Permission decisions Omnigent sends; "always" would persist an OpenCode rule
# that later tool calls bypass policy with.
_PERMISSION_DECISIONS = frozenset({"once", "reject"})
```

Replace `list_models` (original lines 278-290) with:

```python
    async def list_models(self) -> list[_JsonObject]:
        """
        List available models (``GET /api/model``).

        :returns: v2 ``Model.Info`` rows, e.g.
            ``{"id": "big-pickle", "providerID": "opencode", "name": ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("GET", "/api/model")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def list_providers(self) -> list[_JsonObject]:
        """
        List configured providers (``GET /api/provider``).

        :returns: v2 ``Provider.Info`` rows, e.g. ``{"id": "opencode", ...}``.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        data = await self._request_json("GET", "/api/provider")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        return []

    async def connect_provider_key(self, provider_id: str, api_key: str) -> bool:
        """
        Store an API key for a provider integration
        (``POST /api/integration/{id}/connect/key``).

        :param provider_id: Integration id, e.g. ``"anthropic"``.
        :param api_key: The key to store in the server's credential DB.
        :returns: ``True`` on a 2xx response.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        await self._request_json(
            "POST",
            f"/api/integration/{provider_id}/connect/key",
            json_body={"key": api_key},
        )
        return True
```

Delete `reply_question` and `reject_question` (original lines 386-419).

Replace the `# --- permissions ---` section (original lines 436-465: `list_permissions` and the v1 `reply_permission`) with:

```python
    # --- permissions and forms --------------------------------------------

    async def reply_permission(
        self,
        session_id: str,
        request_id: str,
        decision: str,
        message: str | None = None,
    ) -> bool:
        """
        Answer a permission request
        (``POST /api/session/{id}/permission/{requestID}/reply``).

        :param session_id: OpenCode session id the request belongs to.
        :param request_id: Permission request id, e.g. ``"per_abc"``.
        :param decision: ``"once"`` or ``"reject"``.
        :param message: Feedback for the model. OpenCode treats a reject that
            carries a message as a correction the model continues from; omit
            it to stop the tool call outright.
        :returns: ``True`` on a 2xx response.
        :raises ValueError: For any other decision (``"always"`` is refused).
        :raises OpenCodeClientError: On a non-2xx status.
        """
        if decision not in _PERMISSION_DECISIONS:
            raise ValueError(f"Unsupported OpenCode permission decision {decision!r}")
        body: _JsonObject = {"decision": decision}
        if message is not None:
            body["message"] = message
        await self._request_json(
            "POST",
            f"/api/session/{session_id}/permission/{request_id}/reply",
            json_body=body,
        )
        return True

    async def reply_form(
        self, session_id: str, form_id: str, answer: Mapping[str, object]
    ) -> bool:
        """
        Answer a form (``POST /api/session/{id}/form/{formID}/reply``).

        :param session_id: OpenCode session id.
        :param form_id: Form id from ``form.created``.
        :param answer: ``{field_key: value}``; values are strings, numbers,
            booleans, or string lists.
        :returns: ``True`` on a 2xx response.
        :raises OpenCodeClientError: On a non-2xx status (e.g. 409 when the
            form was already settled from the TUI).
        """
        await self._request_json(
            "POST",
            f"/api/session/{session_id}/form/{form_id}/reply",
            json_body={"answer": dict(answer)},
        )
        return True

    async def cancel_form(self, session_id: str, form_id: str) -> bool:
        """
        Cancel a form (``DELETE /api/session/{id}/form/{formID}``).

        :param session_id: OpenCode session id.
        :param form_id: Form id from ``form.created``.
        :returns: ``True`` on a 2xx response.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        await self._request_json("DELETE", f"/api/session/{session_id}/form/{form_id}")
        return True
```

`http_transport.py:28-34`. The `native_server_transport` import becomes:

```python
from omnigent.native.native_server_transport import (
    NativeEvent,
    NativeLaunchConfig,
    NativePrompt,
    NativeServerHandle,
)
```

Delete `OpenCodeHttpTransport.reply_permission` (lines 261-271).

`native_server_transport.py`:
- Line 17: `from typing import Literal, Protocol, runtime_checkable` becomes `from typing import Protocol, runtime_checkable`.
- Delete the `NativePermissionDecision` dataclass (lines 101-113) and the `reply_permission` stub (lines 162-164).
- In the class docstring (as edited in Task 13), `lifecycle, prompt injection, abort, event stream, fork, permission` / `replies). The shared` becomes `lifecycle, prompt injection, abort, event stream, fork). The shared`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py tests/test_native_server_harness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

Ledger items L2 and L5 open here. Check that `.venv/bin/pyrefly check` errors are only at `forwarder.py:1004, 1143, 1155, 1165`, `runner/app.py:6930`, `orchestration.py:2145`, and nowhere in files owned by this stage.

```bash
git add omnigent/harnesses/opencode_native/client.py omnigent/harnesses/opencode_native/http_transport.py omnigent/native/native_server_transport.py tests/test_opencode_native_client.py tests/test_opencode_http_transport.py
SKIP=pyrefly git commit -m "feat(opencode-native): v2 permission, form, and catalog client calls"
```

---

### Task 21: `OpenCodeEvent` v2 and `stream_events()` over `GET /api/event`

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py:85-122` (`OpenCodeEvent`), `:467-552` (`events`, `_parse_sse`, `_decode_event`)
- Modify: `omnigent/harnesses/opencode_native/http_transport.py:228-241`
- Test: `tests/test_opencode_native_client.py:167-207`
- Test: `tests/test_opencode_http_transport.py:109-113, 160-169`

**Interfaces:**
- Consumes: `OpenCodeClientError(status_code=...)` (Task 15).
- Produces: `OpenCodeEvent(id: str | None, type: str, data: dict[str, Any], location: dict[str, Any] | None)` with `OpenCodeEvent.from_frame(frame: Mapping[str, object]) -> OpenCodeEvent`.
  - `OpenCodeClient.stream_events() -> AsyncIterator[OpenCodeEvent]`. The first event is `server.connected`, and `: heartbeat` comments are skipped even in the middle of a frame.
  - `OpenCodeHttpTransport.events` maps each event to `NativeEvent(payload=event.data)`.
- Deleted: `OpenCodeEvent.properties`, `.raw`, `.from_envelope`, `OpenCodeClient.events`, `_decode_event`.
- Opens ledger items L1 and L7.

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_client.py`, delete `test_events_parses_sse_stream` and `test_events_skips_non_json_data` (lines 167-207) and add:

```python
async def test_stream_events_parses_v2_frames_and_skips_heartbeats() -> None:
    sse_body = (
        'data: {"id": "evt_0", "created": 1, "type": "server.connected", "data": {}}\n'
        "\n"
        ": heartbeat\n"
        "\n"
        'data: {"id": "evt_1", "created": 2, "type": "session.text.delta", '
        '"location": {"directory": "/repo"}, '
        '"data": {"sessionID": "ses_1", "ordinal": 0, "delta": "hel"}}\n'
        "\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/event"
        return httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})

    client = _client(handler)
    events = [event async for event in client.stream_events()]
    assert [e.type for e in events] == ["server.connected", "session.text.delta"]
    assert events[0].location is None
    assert events[1] == OpenCodeEvent(
        id="evt_1",
        type="session.text.delta",
        data={"sessionID": "ses_1", "ordinal": 0, "delta": "hel"},
        location={"directory": "/repo"},
    )
    await client.aclose()


async def test_stream_events_heartbeat_inside_frame_does_not_split_it() -> None:
    sse_body = (
        'data: {"id": "evt_1", "type": "session.text.delta",\n'
        ": heartbeat\n"
        'data:  "data": {"delta": "x"}}\n'
        "\n"
    )
    client = _client(lambda _r: httpx.Response(200, text=sse_body))
    events = [event async for event in client.stream_events()]
    assert [(e.id, e.type, e.data) for e in events] == [
        ("evt_1", "session.text.delta", {"delta": "x"})
    ]
    await client.aclose()


async def test_stream_events_skips_non_json_and_non_object_frames() -> None:
    sse_body = (
        "data: not-json\n\n"
        "data: [1, 2]\n\n"
        'data: {"id": "evt_2", "type": "session.status", "data": {"type": "idle"}}\n\n'
    )
    client = _client(lambda _r: httpx.Response(200, text=sse_body))
    assert [e.type async for e in client.stream_events()] == ["session.status"]
    await client.aclose()


async def test_stream_events_flushes_trailing_frame_without_blank_line() -> None:
    sse_body = 'data: {"id": "evt_3", "type": "session.idle", "data": {}}'
    client = _client(lambda _r: httpx.Response(200, text=sse_body))
    assert [e.id async for e in client.stream_events()] == ["evt_3"]
    await client.aclose()


async def test_stream_events_http_error_raises() -> None:
    client = _client(
        lambda _r: httpx.Response(401, json={"_tag": "UnauthorizedError", "message": "no"})
    )
    with pytest.raises(OpenCodeClientError) as exc_info:
        async for _event in client.stream_events():
            pass
    assert exc_info.value.status_code == 401
    await client.aclose()
```

In `tests/test_opencode_http_transport.py`, the fake `events` (lines 109-113) becomes:

```python
    async def stream_events(self) -> Any:
        self.calls.append(("stream_events", None))
        yield SimpleNamespace(
            id="evt_1",
            type="session.status",
            data={"sessionID": "ses_1", "type": "busy"},
            location={"directory": "/w"},
        )
```

`test_events_maps_to_native_event` (lines 160-169) becomes:

```python
async def test_events_maps_to_native_event() -> None:
    client = _FakeClient()
    events = [event async for event in _transport(client).events("ses_1")]
    assert len(events) == 1
    assert (events[0].id, events[0].type, events[0].payload) == (
        "evt_1",
        "session.status",
        {"sessionID": "ses_1", "type": "busy"},
    )
    assert events[0].raw["location"] == {"directory": "/w"}
    assert client.closed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_client.py -k stream_events tests/test_opencode_http_transport.py::test_events_maps_to_native_event -v`
Expected: FAIL with `AttributeError: 'OpenCodeClient' object has no attribute 'stream_events'`.

- [ ] **Step 3: Write minimal implementation**

`client.py:85-122`. Replace `OpenCodeEvent` with:

```python
@dataclass(frozen=True)
class OpenCodeEvent:
    """
    One decoded ``GET /api/event`` frame.

    :param id: Event id, e.g. ``"evt_abc"``, or ``None`` when absent.
    :param type: Event discriminator, e.g. ``"session.text.delta"``.
    :param data: The event payload object.
    :param location: ``{"directory": ...}`` for location-scoped events, else
        ``None``.
    """

    id: str | None
    type: str
    data: dict[str, Any]
    location: dict[str, Any] | None

    @classmethod
    def from_frame(cls, frame: _JsonMapping) -> OpenCodeEvent:
        """
        Build an :class:`OpenCodeEvent` from one decoded SSE frame.

        :param frame: e.g. ``{"id": "evt_1", "created": 1, "type":
            "session.text.delta", "data": {...}, "location": {...}}``.
        :returns: Parsed event; unknown shapes get ``type=""`` and ``data={}``.
        """
        event_id = frame.get("id")
        type_value = frame.get("type")
        data = frame.get("data")
        location = frame.get("location")
        return cls(
            id=event_id if isinstance(event_id, str) else None,
            type=type_value if isinstance(type_value, str) else "",
            data=dict(data) if isinstance(data, Mapping) else {},
            location=dict(location) if isinstance(location, Mapping) else None,
        )
```

Replace the `# --- events ---` section plus `_parse_sse` and `_decode_event` (original lines 467-552) with:

```python
    # --- events ----------------------------------------------------------

    async def stream_events(self) -> AsyncIterator[OpenCodeEvent]:
        """
        Stream server events (``GET /api/event``).

        The first event is ``server.connected``. The iterator ends when the
        server closes the stream; callers own reconnect and history gap-fill.

        :returns: Async iterator of decoded events.
        :raises OpenCodeClientError: On a non-2xx status.
        """
        async with self._client.stream("GET", "/api/event", timeout=None) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise OpenCodeClientError(
                    f"OpenCode /api/event failed: {response.status_code} {body[:200]!r}",
                    status_code=response.status_code,
                )
            async for event in _parse_sse(response.aiter_lines()):
                yield event


async def _parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[OpenCodeEvent]:
    """
    Parse SSE lines into :class:`OpenCodeEvent` objects.

    A frame is one or more ``data:`` lines ended by a blank line. Comment lines
    (``: heartbeat``) never end a frame, and ``id:`` / ``event:`` / ``retry:``
    are ignored because v2 carries the id and type inside the JSON.

    :param lines: Async iterator of decoded SSE text lines.
    :returns: Async iterator of parsed events.
    """
    data_lines: list[str] = []
    async for raw_line in lines:
        line = raw_line.rstrip("\r\n")
        if line == "":
            if data_lines:
                event = _decode_frame("\n".join(data_lines))
                data_lines = []
                if event is not None:
                    yield event
            continue
        if line.startswith(":"):
            continue
        field_name, _, value = line.partition(":")
        if field_name == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        event = _decode_frame("\n".join(data_lines))
        if event is not None:
            yield event


def _decode_frame(payload: str) -> OpenCodeEvent | None:
    """
    Decode one SSE ``data`` payload into an :class:`OpenCodeEvent`.

    :param payload: Raw JSON text from one or more ``data:`` lines.
    :returns: Parsed event, or ``None`` when the payload is not a JSON object.
    """
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        _logger.debug("Skipping non-JSON OpenCode SSE data: %s", payload[:200])
        return None
    if not isinstance(decoded, dict):
        return None
    return OpenCodeEvent.from_frame(decoded)
```

`http_transport.py:228-241`. Transport `events` becomes:

```python
    async def events(self, session_id: str) -> AsyncIterator[NativeEvent]:
        """Stream native events from ``GET /api/event`` (unfiltered)."""
        del session_id
        client = self._client()
        try:
            async for event in client.stream_events():
                yield NativeEvent(
                    id=event.id,
                    type=event.type,
                    payload=event.data,
                    raw={
                        "id": event.id,
                        "type": event.type,
                        "data": event.data,
                        "location": event.location,
                    },
                )
        finally:
            await client.aclose()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_client.py tests/test_opencode_http_transport.py tests/inner/test_opencode_native_executor.py tests/test_opencode_native_app_server.py tests/test_opencode_native_bridge.py tests/test_native_server_harness.py -v`
Expected: PASS. `tests/test_opencode_native_forwarder.py` and `tests/test_opencode_forwarder_reconnect.py` now fail at collection or construction (ledger L7). Stage 2 replaces them.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/client.py omnigent/harnesses/opencode_native/http_transport.py tests/test_opencode_native_client.py tests/test_opencode_http_transport.py
SKIP=pyrefly git commit -m "feat(opencode-native): stream v2 /api/event frames as OpenCodeEvent"
```

---

### Task 22: Re-point the wire-contract e2e at `@opencode/cli` 2.0.x

**Files:**
- Modify: `tests/e2e/test_opencode_native_wire_contract_e2e.py` (whole file)

**Interfaces:**
- Consumes everything from Tasks 13-21:
  - `OpenCodeNativeServer.start/close/version/client/base_url/auth_headers`
  - `OpenCodeClient.info/create_session/get_session/list_messages/get_context/stream_events/fork/interrupt/reply_permission/cancel_form`
  - `OpenCodeClientError.status_code`
- Produces: an opt-in e2e (`OMNIGENT_E2E_OPENCODE_NATIVE=1`) that proves the v2 wire contract against the real binary.

- [ ] **Step 1: Write the failing test**

Replace the whole file with:

```python
"""End-to-end test: the OpenCode-native client speaks to a REAL ``opencode serve`` 2.x.

The client (``omnigent.harnesses.opencode_native.client``) is hand-shaped from the
``@opencode/cli`` 2.0.x OpenAPI, so the rest of the suite exercises it only
against in-process fakes. This test boots a real ``opencode serve --stdio``
through :class:`~omnigent.harnesses.opencode_native.app_server.OpenCodeNativeServer`
and drives the provider-independent ``/api/*`` endpoints the harness relies on.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* Opt-in only: set ``OMNIGENT_E2E_OPENCODE_NATIVE=1`` and have ``opencode`` 2.0.x
  on ``PATH`` (``npm i -g @opencode/cli@~2.0.18``). No login or model
  credential is needed: server info, session create/get/list/context, the SSE
  stream, fork, interrupt, and the permission/form error paths are
  provider-independent.
* The heartbeat check waits up to 20 s for the server's 15 s ``: heartbeat``.
* Run it with::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 uv run pytest \
        tests/e2e/test_opencode_native_wire_contract_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeNativeServer,
    OpenCodeVersionError,
)
from omnigent.harnesses.opencode_native.client import OpenCodeClientError, OpenCodeEvent

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_OPENCODE_NATIVE") != "1" or shutil.which("opencode") is None,
    reason=(
        "opencode-native wire-contract e2e needs `opencode` 2.0.x on PATH; "
        "set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and `npm i -g @opencode/cli@~2.0.18`) to run"
    ),
)

# Session.Info keys OpenCodeSession.from_payload and the forwarder depend on.
_REQUIRED_SESSION_KEYS = {"id", "projectID", "location", "time"}


async def _first_event(server: OpenCodeNativeServer) -> OpenCodeEvent | None:
    client = server.client()
    try:
        async for event in client.stream_events():
            return event
        return None
    finally:
        await client.aclose()


async def _saw_heartbeat(server: OpenCodeNativeServer) -> bool:
    async with httpx.AsyncClient(
        base_url=server.base_url, headers=server.auth_headers, timeout=None
    ) as http:
        async with http.stream("GET", "/api/event") as response:
            async for line in response.aiter_lines():
                if line.startswith(": heartbeat"):
                    return True
    return False


async def test_opencode_native_wire_contract_against_real_server() -> None:
    """A real ``opencode serve --stdio`` answers every v2 endpoint the harness drives."""
    tmp = Path(tempfile.mkdtemp(prefix="opencode-e2e-"))
    bridge = tmp / "bridge"
    bridge.mkdir(parents=True, exist_ok=True)
    workspace = tmp / "ws"
    workspace.mkdir(parents=True, exist_ok=True)

    server = OpenCodeNativeServer(bridge_dir=bridge, workspace=workspace)
    process = None
    try:
        try:
            await server.start()
        except OpenCodeVersionError as exc:
            pytest.skip(f"installed opencode is outside the supported pin: {exc}")
        process = server.process

        # Readiness recorded the server-reported version; OPENCODE_DB landed in the bridge.
        assert server.version is not None and server.version.startswith("2.")
        assert (bridge / "opencode.db").exists()

        client = server.client()
        try:
            info = await client.info()
            assert info["version"] == server.version

            session = await client.create_session(
                title="omnigent-e2e",
                directory=str(workspace),
                metadata={"omnigent_conversation": "conv_e2e"},
            )
            assert session.id.startswith("ses")
            assert set(session.raw) >= _REQUIRED_SESSION_KEYS, (
                f"session payload missing keys: {_REQUIRED_SESSION_KEYS - set(session.raw)}"
            )
            assert session.directory is not None
            assert Path(session.directory).resolve() == workspace.resolve()

            fetched = await client.get_session(session.id)
            assert fetched is not None and fetched.id == session.id
            assert await client.get_session("ses_does_not_exist_xyz") is None

            assert isinstance(await client.list_messages(session.id), list)
            assert isinstance(await client.get_context(session.id), list)

            # The first SSE frame is server.connected; heartbeats are comments.
            event = await asyncio.wait_for(_first_event(server), timeout=10.0)
            assert event is not None and event.type == "server.connected"
            assert await asyncio.wait_for(_saw_heartbeat(server), timeout=20.0)

            forked = await client.fork(session.id)
            assert forked.id != session.id
            assert await client.interrupt(session.id) is False

            with pytest.raises(OpenCodeClientError) as perm_exc:
                await client.reply_permission(session.id, "per_missing", "reject")
            assert perm_exc.value.status_code in (400, 404)
            with pytest.raises(OpenCodeClientError):
                await client.cancel_form(session.id, "frm_missing")
        finally:
            await client.aclose()
    finally:
        await server.close()
    # Closing stdin ends a --stdio server cleanly, without terminate().
    if process is not None:
        assert process.returncode == 0
```

- [ ] **Step 2: Run test to verify it fails**

This check runs the test against a v1 binary, which proves it depends on v2-only behavior. The version gate is bypassed so a v1 binary gets past `start()`'s version check.

Run: `npx -y -p opencode-ai@1.18 which opencode` to locate a v1 binary. Then run `PATH="$(dirname <that path>):$PATH" OMNIGENT_OPENCODE_SKIP_VERSION_CHECK=1 OMNIGENT_E2E_OPENCODE_NATIVE=1 uv run pytest tests/e2e/test_opencode_native_wire_contract_e2e.py -v`
Expected: FAIL in `server.start()` with `RuntimeError: opencode serve exited early …` (v1 has no `--stdio`) or `… did not become ready: HTTP 404` (v1 has no `/api/info`). Without any `opencode` on `PATH` the test is SKIPPED.

- [ ] **Step 3: Write minimal implementation**

No production code: Tasks 13-21 are the implementation. If the run shows a contract drift, fix the matching client method and add a unit test for it before continuing. For example, `create_session` may reject `metadata`, or a missing form may return something other than 4xx.

- [ ] **Step 4: Run test to verify it passes**

Run: `OMNIGENT_E2E_OPENCODE_NATIVE=1 uv run pytest tests/e2e/test_opencode_native_wire_contract_e2e.py -v`
Expected: PASS (about 20 s, bounded by the heartbeat wait).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_opencode_native_wire_contract_e2e.py
SKIP=pyrefly git commit -m "test(opencode-native): re-point the wire-contract e2e at opencode 2.x"
```

---

### Stage 1 manual check (for the human)

1. `npm rm -g opencode-ai; npm i -g @opencode/cli@~2.0.18 && opencode --version` should print `opencode v2.0.18`.
2. `uv run omnigent setup`, then choose OpenCode:
   - the harness shows as installed;
   - with `opencode-ai` 1.18 installed instead, the upgrade offer shows `npm rm -g opencode-ai; npm install -g @opencode/cli@~2.0.18`.
3. `OMNIGENT_E2E_OPENCODE_NATIVE=1 uv run pytest tests/e2e/test_opencode_native_wire_contract_e2e.py -v` should pass.
4. While the e2e runs, `ps -ef | grep "opencode serve"` should show `--stdio`. After the test it should show no stray `opencode serve` processes.
## Stage 2: Forwarder rewrite for the v2 event model, with live streaming (Tasks 23-46)

Implements spec section 3 (`designs/opencode-v2-native-harness.md`) in `omnigent/harnesses/opencode_native/forwarder.py`: the v1 part-snapshot handlers are replaced by one handler per v2 `/api/event` type, text/reasoning/tool output stream live, permissions and forms go through the policy evaluator and web cards, subagent child sessions are mirrored, and reconnects catch up from `GET /api/session/{id}/message`.


## Evidence: v2 names and payloads (verified against OpenCode `v2.0.18` source)

All paths are under the extracted copy
`/tmp/claude-1000/-home-jason-forks-omnigent/5e0dc4f8-6022-4ed8-8d53-74a9a0fb56eb/scratchpad/v2/`.

**Frame shape** (`packages/schema/src/event.ts:101-109`, `:126-133`): every public
event is `{id: "evt_…", created, metadata?, type, durable?, location?, data}` — the
payload is under `data` (v1 used `properties`). The server writes
`data: ${JSON.stringify(event)}\n\n` (`packages/server/src/event-feed.ts:29-31`), sends
`server.connected` first and `": heartbeat\n\n"` every 15 s
(`packages/server/src/handlers/event.ts:14-24`), and only publishes types in
`EventManifest.ServerDefinitions` (`packages/protocol/src/groups/event.ts:62-68`),
which include `SessionEvent`, `Permission`, `Form` and `SessionStatusEvent` but **not**
`LegacyEventV1` or `SessionCompactionEvent` (`packages/schema/src/event-manifest.ts:41-81`).
So `message.updated`, `message.part.*`, `session.compacted` never arrive on `/api/event`.

**Session events** (`packages/schema/src/session-event.ts`), `Base = { sessionID }`:

| Event | Line | `data` fields used |
|---|---|---|
| `session.created` | 51-69 | `sessionID, parentID?, title?, agent?, model?` |
| `session.model.selected` | 83-91 | `model: Model.Ref, previous?` |
| `session.usage.updated` (ephemeral) | 170-177 | `cost: Money.USD, tokens: TokenUsage.Info` |
| `session.inbox.enqueued` / `.delivered` / `.cancelled` | 206-233 | `inboxID, item?` |
| `session.execution.started` / `.succeeded` | 243-247 | `Base` |
| `session.execution.failed` | 249-253 | `error: SessionError.Error` |
| `session.execution.interrupted` | 256-260 | `reason: "user" \| "shutdown" \| "superseded" \| "inactivity"` |
| `session.step.started` | 332-344 | `assistantMessageID, agent, model: Model.Ref, snapshot?, started` |
| `session.step.ended` | 358-372 | `assistantMessageID, finish, cost, tokens, snapshot?, files?` |
| `session.step.failed` | 375-390 | `assistantMessageID, error, cost?, tokens?` |
| `session.text.delta` (ephemeral) | 407-415 | `assistantMessageID, ordinal, delta` |
| `session.text.ended` | 418-428 | `assistantMessageID, ordinal, text, state?` |
| `session.reasoning.delta` (ephemeral) | 446-454 | `assistantMessageID, ordinal, delta` |
| `session.reasoning.ended` | 457-467 | `assistantMessageID, ordinal, text, state?` |
| `session.tool.input.started` | 479-486 | `assistantMessageID, id, name` |
| `session.tool.called` | 510-519 | `assistantMessageID, id, input, executed, state?` — **no `name`** |
| `session.tool.progress` (ephemeral) | 522-529 | `assistantMessageID, id, metadata` — "Live replacement metadata for a running tool" |
| `session.tool.success` | 533-546 | `assistantMessageID, id, content: NonEmptyArray(Tool.Content), metadata?, executed` |
| `session.tool.failed` | 554-568 | `assistantMessageID, id, error, content?, metadata?, executed` |
| `session.retry.scheduled` | 572-582 | `assistantMessageID, attempt, at, error` |
| `session.compaction.started` | 586-595 | `reason: "auto" \| "manual", recent, inputID?` |
| `session.compaction.ended` | 607-623 | `reason, text, recent, cost?, tokens?` |
| `session.compaction.failed` | 626-637 | `reason, error, cost?, tokens?` |

**Status** (`packages/schema/src/session-status-event.ts:9-43`): `session.status {sessionID,
status: {type: "idle"} | {type: "busy"} | {type: "retry", attempt, message, action?, next}}`;
`session.idle` is marked `// deprecated` (`:45-51`).

**Errors** (`packages/schema/src/session-error.ts`): `Session.StructuredError =
{type: string, message: string, status?: 100..599}`. Types come from
`packages/core/src/session/to-session-error.ts`: `"provider.auth"` for
`Authentication` (`:14-15`) and `Integration.AuthorizationError` (`:65`), `"aborted"` for
`UserInterruptedError` (`:54`), `"provider.rate-limit"`, `"provider.transport"`, … with
`status` copied from the provider HTTP status (`:69-72`).

**Tokens / money** (`packages/schema/src/token-usage.ts`, `money.ts`): `TokenUsage.Info =
{input, output, reasoning, cache: {read, write}}`, all `Schema.Finite`; `Money.USD` is a
finite number. `session.usage.updated` is the session row's cumulative total
(`packages/core/src/session/projector.ts:75-103`).

**Model** (`packages/schema/src/model.ts:131-135`): `Model.Ref = {id, providerID, variant?}`.

**Permission** (`packages/schema/src/permission.ts:189-226`):
```ts
const RequestFields = { sessionID: SessionID, action: Schema.String, resources: Schema.Array(Schema.String),
  save: Schema.Array(Schema.String).pipe(optional), metadata: Schema.Record(Schema.String, Schema.Unknown).pipe(optional),
  source: Source.pipe(optional), message: Schema.String.pipe(optional) }
export const Request = Schema.Struct({ id: ID, ...RequestFields })
export const Reply = Schema.Literals(["once", "always", "reject"])
const Asked = ephemeral({ type: "permission.asked", schema: Request.fields })
const Replied = ephemeral({ type: "permission.replied", schema: { sessionID: SessionID, requestID: ID, reply: Reply } })
```
`Source = {type: "tool", messageID, id}` (`:189-195`). There is no `permission.v2.asked`.

**Form** (`packages/schema/src/form.ts`): fields `StringField {key, title?, description?,
required?, hidden?, when?, format?, …, options?: {value, label, description?}[], custom?}`
(`:39-61`), `NumberField`/`IntegerField {minimum?, maximum?, default?}` (`:64-80`),
`BooleanField` (`:82-87`), `MultiselectField {options, minItems?, maxItems?, custom?,
default?}` (`:89-97`), `ExternalField {key, type: "external", url, title?, description?}`
(`:100-106`); `Form.Info = {id: "frm_…", sessionID, title, metadata?, fields}` (`:122-137`);
`Answer = Record<string, string | number | boolean | string[]>` (`:139-147`);
```ts
const Created = ephemeral({ type: "form.created", schema: { form: Info } })
const Replied = ephemeral({ type: "form.replied", schema: { id: ID, sessionID: Schema.String, answer: Answer } })
const Cancelled = ephemeral({ type: "form.cancelled", schema: { id: ID, sessionID: Schema.String } })
```
(`:169-171`) — note `form.created` nests `sessionID` under `data.form`. `external` fields
must be answered `true` (`packages/core/src/form.ts:235-238`); answers to inactive `when`
fields are rejected (`:239-247`). There is no `question.*` event: the `question` tool asks
through `forms.ask` with `key: "q<i>"`, `options[].value = label`, `custom: true`
(`packages/core/src/tool/plugin/question.ts:75-127`).

**Messages** (`packages/schema/src/session-message.ts`): `User {id, type: "user", text,
files?, …}` (`:73-81`); `Assistant {id, type: "assistant", agent, model, content[], cost?,
tokens?, error?, time: {created, streamed?, completed?}}` (`:211-236`); content items
`text {text}` (`:176-181`), `reasoning {text}` (`:183-192`), `tool {id, name, state}`
(`:160-174`) with `state.status` `streaming | running | completed {input, content,
metadata?} | error {input, error, content?}` (`:125-158`). `GET
/api/session/{id}/message` returns `SessionMessagesResponse {data: Session.Message.Info[],
cursor: {previous, next}}` (`packages/protocol/openapi.json`, `operationId:
session.message.list`). Text ordinals count only `text` blocks within one assistant
message (`packages/core/src/session/runner/publish-llm-event.ts:125-145`).

**Tool progress metadata in 2.0.18** (`packages/core/src/session/runner/publish-llm-event.ts:560-571`
overwrites `tool.progress = update` and publishes it): shell
`context.progress({ shellID: info.id })` (`packages/core/src/tool/plugin/shell.ts:213`);
subagent `context.progress({ sessionID: child.id, status: "running" })`
(`packages/core/src/tool/plugin/subagent.ts:201`); websearch `{ provider }`
(`packages/core/src/tool/plugin/websearch.ts:63`); codemode `{ toolCalls }`
(`packages/core/src/codemode/tool.ts:88`). No built-in tool reports incremental output.

**Coordinator correction (not Stage 2 code, verified):** v2 `instructions` config is an
array of paths/URLs — `instructions: Schema.String.pipe(Schema.Array, optional)
.annotate({ description: "Additional paths or URLs supplying ambient instructions" })`
(`packages/schema/src/config.ts:90-91`). Stage 2 does not touch config; Stage 3 owns it.


## Stage 2 notes

**How this stage was verified.** Every code block in Tasks 20–41 and 43 was executed
before writing the plan: a script rendered the forwarder and both test files cumulatively
after each task (with `OpenCodeEvent`/client/bridge stubbed to the Stage 1 contract and a
synthetic `events.ndjson`/`messages.json` in the Stage 0 shape), ran each task's new tests
against the *previous* task's code (the "Expected: FAIL" lines below are the real first
assertion errors) and the full suite against the new code, and ran `ruff check` +
`ruff format --check` with the repo config. Final state: 106 tests pass.

**Line numbers.** Task 23 cites lines of the current v1 `forwarder.py` (commit
`d2130fdfe`). Every later task cites lines of the file *as it stands at the start of that
task* (Task 23 replaces the file wholesale, so v1 line numbers stop applying). Apply each
task's Step 3 blocks in the order written — they go bottom-up so earlier line numbers stay
valid.

**Cross-stage interfaces this stage relies on.**
- Stage 0: `tests/opencode_v2_fixtures.py` with `load_events()`, `events_of_type(type_)`,
  `load_messages()`; the capture must contain at least one each of
  `session.execution.started/succeeded`, `session.step.started/ended`, `session.text.delta/ended`,
  `session.reasoning.delta/ended`, a `shell` `session.tool.input.started/called/progress/success`,
  `session.usage.updated`, `session.compaction.started/ended`, `permission.asked/replied`,
  `form.created/replied` (the brief's fixture list). Fixture-driven tests read expected
  values from the capture rather than hard-coding them.
- Stage 1: `OpenCodeEvent(id, type, data, location)`; `OpenCodeClient.stream_events()`,
  `list_messages(session_id, *, after_id=None)`, `reply_permission(session_id, request_id, decision)`
  (called **without** a message: per Stage 3 a reject message makes the v2 model continue, so
  Stage 1's `message` default must send no message), `reply_form(session_id, form_id, answer)`,
  `cancel_form(session_id, form_id)`, `OpenCodeClientError`.
- Stage 3: `parse_permission_request(data) -> OpenCodePermissionRequest | None` with
  `request_id`, `session_id`, `action`, `resources`, `metadata`, `source`;
  `normalize_for_policy(...)` returning at least `harness`, `action`, `omnigent_session_id`
  (Stage 3 adds `arguments`; extend `test_permission_asked_passes_normalized_input_to_evaluator`
  with `assert seen[0]["arguments"] == {"command": "ls"}` in Stage 3); `decision_to_reply`
  (never `"always"`); `permissions.reply_body` is deleted by Stage 3 and no longer imported here.

**What this stage produces for later stages.**
- `OpenCodeNativeForwarder.__init__` keeps its signature, so
  `omnigent/runner/native/orchestration.py:1459-1520` is untouched.
- `opencode_tool_content_text(content, *, error=None) -> str` (v2 tool result → output text)
  and `form_questions` / `form_answer` are public for Stage 4's session import.
- `opencode_tool_output_text` (current `forwarder.py:1252-1270`) is kept verbatim because
  `omnigent/session_import/local.py:34,1454` still imports it; **Stage 4 deletes it** when it
  rewrites the import parser for v2 `content[]`.

**Deliberate refinements of the spec table** (each has a task and tests):
- `session.tool.called` has no `name`; names come from `session.tool.input.started` (Task 27).
- Buffered text is also flushed right before a tool call so text precedes the call in the
  chat; everything else still flushes on `session.step.ended` (Tasks 22, 24).
- User prompts are mirrored from `session.inbox.enqueued/delivered` (Task 35): the spec
  table has no row for them, but the forwarder is the sole transcript source.
- `permission.asked` evaluation and `form.created` parking run as background tasks so a
  parked card never stalls the event loop (Tasks 33, 36).
- `session.step.failed` shares the step-end path; `session.compaction.failed` posts
  `external_compaction_status failed`.

**Known limits (documented, not fixed here).**
- 2.0.18 built-in tools never put output in progress metadata, so shell output is not
  live-streamed; it lands on `session.tool.success`. Task 28's heuristic streams plugin/MCP
  tools that report a growing `metadata.output`.
- `external_subagent_start` is the claude-native contract; the server stamps claude-native
  labels on the child row (`omnigent/server/routes/_sessions/helpers.py:3580+`). Rows render,
  but the label keys say "claude". A harness-neutral subagent start is a follow-up.
- `/api/event` has no replay; catch-up after a drop reads persisted messages, so ephemeral
  deltas emitted during the gap are not re-streamed (their final text is).


## Final handler table (spec section 3 → handler → task)

| v2 event | Handler | Omnigent output | Task |
|---|---|---|---|
| `session.execution.started`, `session.status {busy}` | `_on_execution_started`, `_on_session_status` | `external_session_status running` (`response_id` = first `assistantMessageID`) | 21 |
| `session.step.started` | `_on_step_started` | running edge, bridge active id, model record/`external_model_change` | 21, 27, 30 |
| `session.text.delta` | `_on_text_delta` | `external_output_text_delta {delta, message_id, index, final:false}` | 22 |
| `session.text.ended` | `_on_text_ended` | buffered → assistant `external_conversation_item` (`message_id` retires preview) | 22 |
| `session.reasoning.delta` / `.ended` | `_on_reasoning_delta` / `_on_reasoning_ended` | `external_output_reasoning_delta {delta, started}` | 23 |
| `session.tool.input.started` | `_on_tool_input_started` | records tool name | 24 |
| `session.tool.called` | `_on_tool_called` | `function_call` | 24 |
| `session.tool.progress` | `_on_tool_progress` | `external_tool_output_delta` (growing `metadata.output`); subagent linking | 25, 38 |
| `session.tool.success` / `.failed` | `_on_tool_success` / `_on_tool_failed` | `function_call_output` | 24 |
| `session.step.ended` / `.failed`, `session.usage.updated` | `_on_step_ended`, `_on_usage_updated` | flush + `external_session_usage` | 22, 26 |
| `session.execution.succeeded`, `session.status {idle}` | `_on_execution_succeeded`, `_on_session_status` | flush, usage, `idle` (once) | 21, 26 |
| `session.execution.failed` | `_on_execution_failed` | `failed` (+ `reauth_required`), `aborted` → idle | 28 |
| `session.execution.interrupted` | `_on_execution_interrupted` | `idle` (user) or `external_session_interrupted` + idle | 29 |
| `session.retry.scheduled`, `session.status {retry}` | `_on_retry_scheduled`, `_on_session_status` | `running` + `blocked_on` | 30 |
| `session.compaction.started` / `.ended` / `.failed` | `_on_compaction_*` | `external_compaction_status` | 31 |
| `session.model.selected` | `_on_model_selected` | `external_model_change` | 27 |
| `session.inbox.enqueued` / `.delivered` / `.cancelled` | `_on_inbox_*` | user `external_conversation_item` | 32 |
| `permission.asked` | `_on_permission_asked` | policy → `reply_permission(once\|reject)` | 33 |
| `permission.replied` | `_on_permission_replied` | `external_elicitation_resolved` (TUI first) | 34 |
| `form.created` | `_on_form_created` | `/hooks/native-permission-request` card → `reply_form` / `cancel_form` | 35, 36 |
| `form.replied` / `form.cancelled` | `_on_form_resolved` | `external_elicitation_resolved` | 37 |
| `session.created {parentID}` | `_on_session_created` | `external_subagent_start`, child routing | 38 |

**v1 code deleted in Task 23** (current `omnigent/harnesses/opencode_native/forwarder.py`):
`_message_is_complete` (134-141); v1 `seed_dedupe_from_history` (222-269) and
`catch_up_from_history` (271-322) part walks; `_cancel_question_tasks` (369-376);
`properties`/`info` filter (403-420); `_post_user_text` (500-512); `_on_message_updated`
(597-620); `_on_part_updated` with the part-type dispatch (622-650); `_accumulate_text_part`
(652-672); `_post_user_text_part` (674-690); `_handle_tool_part` (702-735);
`_handle_reasoning_part` (737-764); `_handle_file_part` (766-801); `_on_session_idle`
(826-831); `_record_assistant_usage` (833-858); `_on_session_error` (916-942);
`session.next.*` / `session.compacted` compaction handlers (944-964); `_on_model_switched`
(966-984); `permission.v2.asked` routing and the `reply_body` import (46) and call
(1003-1006); `_on_question_asked` (1041-1067); `_handle_question` (1069-1155);
`_reject_question_quietly` (1157-1171); `_on_question_replied` / `_on_question_rejected` /
`_withdraw_question` (1227-1249); the v1 `_HANDLERS` table (1276-1307). `_park_question`
(1173-1225) returns as `_park_elicitation` in Task 39; `_resolve_permission` (1014-1039)
returns in Task 36; usage posting (860-914) returns in Task 29.

## Tasks

### Task 23: Forwarder v2 skeleton: per-session state, v2 filtering, delete v1 handlers

Stage 1 changed `OpenCodeEvent` to `{id, type, data, location}` and the stream to `client.stream_events()`, so every v1 handler (which reads `event.properties`) is dead. This task rewrites the module into the v2 skeleton: per-OpenCode-session `_SessionTurn` state, a `data.sessionID` filter (with `form.created`'s nested `form.sessionID`), `stream_events()` consumption, background-task teardown, and an empty `_HANDLERS` table that later tasks fill one event family at a time.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py:1-1307` (full rewrite; lines 1252-1270 `opencode_tool_output_text` are kept verbatim; deletions listed under "v1 code deleted in Task 23" above)
- Test: `tests/test_opencode_native_forwarder.py:1-1151` (full rewrite; every v1 test is removed)
- Test: `tests/test_opencode_forwarder_reconnect.py:1-429` (full rewrite; only the `update_last_event_id` guard survives, the v2 reconnect tests arrive in Tasks 39-40)

**Interfaces:**
- Consumes: Stage 1 `OpenCodeEvent(id, type, data, location)`, `OpenCodeClient.stream_events() -> AsyncIterator[OpenCodeEvent]`; `permissions.PolicyDecision`; `tests/opencode_v2_fixtures.events_of_type(type_) -> list[dict]` (Stage 0).
- Produces: `OpenCodeForwarderState.mark(key) -> bool`; `_SessionTurn` (fields listed in code); `_str_field(data, key) -> str | None`; `_int_field(data, key) -> int | None`; `_event_session_id(event) -> str | None`; `OpenCodeNativeForwarder` (constructor signature unchanged) with `run(*, max_reconnects)`, `handle_event(event)`, `_event_targets_session(event) -> bool`, `_active_turn(event) -> _SessionTurn | None`, `_is_root(turn) -> bool`, `_key(*parts) -> str`, `_post_event(event_type, data, *, conversation_id=None) -> httpx.Response | None`, `_permission_tasks` / `_form_tasks: dict[str, asyncio.Task[None]]`; module-level `_HANDLERS` dict.

- [ ] **Step 1: Write the failing test**

Replace the whole of `tests/test_opencode_native_forwarder.py` with:

```python
"""Tests for the OpenCode v2 event -> Omnigent event forwarder translation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type

_SESSION = "ses_1"
# The captured turn's own OpenCode session id.
_FIX_SESSION: str = events_of_type("session.execution.started")[0]["data"]["sessionID"]


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []
        self.hook_response: dict[str, Any] | None = None
        self.child_conversation_id = "conv_child_1"

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        request = httpx.Request("POST", url)
        if url.endswith("/hooks/native-permission-request") and self.hook_response is not None:
            return httpx.Response(200, json=self.hook_response, request=request)
        if json.get("type") == "external_subagent_start":
            body = {"queued": False, "child_session_id": self.child_conversation_id}
            return httpx.Response(200, json=body, request=request)
        return httpx.Response(200, request=request)


class _FakeOpenCodeClient:
    """Fake v2 OpenCode client recording replies and serving history."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.after_ids: list[str | None] = []
        self.permission_replies: list[tuple[str, str, str]] = []
        self.form_replies: list[tuple[str, str, dict[str, Any]]] = []
        self.form_cancels: list[tuple[str, str]] = []
        self.stream: list[OpenCodeEvent] = []

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.after_ids.append(after_id)
        return self.messages

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> bool:
        self.permission_replies.append((session_id, request_id, decision))
        return True

    async def reply_form(self, session_id: str, form_id: str, answer: dict[str, Any]) -> bool:
        self.form_replies.append((session_id, form_id, answer))
        return True

    async def cancel_form(self, session_id: str, form_id: str) -> bool:
        self.form_cancels.append((session_id, form_id))
        return True

    async def stream_events(self) -> AsyncIterator[OpenCodeEvent]:
        for event in self.stream:
            yield event


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
    *,
    opencode_session_id: str = _SESSION,
    **kwargs: Any,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=opencode_session_id,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
        **kwargs,
    )


def _event(event_type: str, **data: Any) -> OpenCodeEvent:
    """Hand-build a v2 ``/api/event`` frame for this test's session."""
    data.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, data=data, location=None)


def _to_event(raw: dict[str, Any]) -> OpenCodeEvent:
    """Convert a captured ``/api/event`` frame into an ``OpenCodeEvent``."""
    return OpenCodeEvent(
        id=raw.get("id"),
        type=raw["type"],
        data=dict(raw.get("data") or {}),
        location=raw.get("location"),
    )


def _fixture(event_type: str, index: int = 0) -> OpenCodeEvent:
    """The *index*-th captured event of *event_type* (for ``_FIX_SESSION``)."""
    return _to_event(events_of_type(event_type)[index])


def _types(posts: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [body["type"] for _url, body in posts]


def _datas(posts: list[tuple[str, dict[str, Any]]], event_type: str) -> list[dict[str, Any]]:
    return [body["data"] for _url, body in posts if body["type"] == event_type]


def _status_edges(posts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return _datas(posts, "external_session_status")


def _items(posts: list[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    return _datas(posts, "external_conversation_item")


def _hook_post(server: _RecordingServerClient) -> dict[str, Any] | None:
    """Return the body of the native-permission-request hook POST, if any."""
    for url, body in server.posts:
        if url.endswith("/hooks/native-permission-request"):
            return body
    return None


async def _drain(fwd: fwd_mod.OpenCodeNativeForwarder) -> None:
    """Await every background permission / form task the forwarder spawned."""
    tasks = [*fwd._permission_tasks.values(), *fwd._form_tasks.values()]
    await asyncio.gather(*tasks)


def _step_started(message_id: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("agent", "build")
    data.setdefault("model", {"id": "claude-sonnet-4-5", "providerID": "anthropic"})
    data.setdefault("started", 1)
    return _event("session.step.started", assistantMessageID=message_id, **data)


def _step_ended(message_id: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("finish", "stop")
    data.setdefault("cost", 0.0)
    data.setdefault(
        "tokens", {"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}}
    )
    return _event("session.step.ended", assistantMessageID=message_id, **data)


# --- filtering / dispatch ---------------------------------------------------


async def test_unknown_event_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("some.unknown.event", foo="bar"))
    assert server.posts == []


async def test_event_for_other_session_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    event = _event("session.execution.started", sessionID="ses_OTHER")
    assert fwd._event_targets_session(event) is False
    await fwd.handle_event(event)
    assert server.posts == []


async def test_event_without_session_id_passes_filter() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    connected = OpenCodeEvent(id="evt_1", type="server.connected", data={}, location=None)
    assert fwd._event_targets_session(connected) is True


async def test_form_created_filters_on_nested_session_id() -> None:
    """``form.created`` carries the session id under ``data.form``."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    ours = OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": "frm_1", "sessionID": _SESSION}},
        location=None,
    )
    theirs = OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": "frm_2", "sessionID": "ses_X"}},
        location=None,
    )
    assert fwd._event_targets_session(ours) is True
    assert fwd._event_targets_session(theirs) is False


async def test_consume_once_dispatches_stream_events() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    seen: list[str] = []

    async def _record(event: OpenCodeEvent) -> None:
        seen.append(event.type)

    fwd.handle_event = _record  # type: ignore[method-assign]
    opencode.stream = [_event("session.status", status={"type": "busy"})]
    await fwd._consume_once()
    assert seen == ["session.status"]


async def test_run_reconnects_until_cap() -> None:
    """run() retries the SSE consume loop and stops at the reconnect cap."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    calls = {"n": 0}

    async def failing_consume() -> None:
        calls["n"] += 1
        raise httpx.ReadError("dropped", request=httpx.Request("GET", "http://x/api/event"))

    fwd._consume_once = failing_consume  # type: ignore[method-assign]

    async def _no_sleep(_seconds: float) -> None:
        return None

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=3)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]
    assert calls["n"] == 4  # initial + 3 reconnects
```

Replace the whole of `tests/test_opencode_forwarder_reconnect.py` with:

```python
"""Tests for SSE reconnect gap-fill in OpenCodeNativeForwarder.

After an SSE reconnect the forwarder re-reads v2 history past its cursor so
content produced during the disconnect window is delivered exactly once, and
content produced before the drop is never re-posted.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent

_SESSION = "ses_reconnect"


class _RecordingServerClient:
    """httpx-shaped stub recording Omnigent event POSTs."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def post(self, url: str, *, json: dict[str, Any]) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


class _FakeOpenCodeClient:
    """Fake v2 OpenCode client: one event batch per SSE connection."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.message_snapshots: list[list[dict[str, Any]]] = []
        self._message_snapshot_index = 0
        self.after_ids: list[str | None] = []
        self._event_batches: list[list[OpenCodeEvent]] = []
        self._batch_index = 0

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        self.after_ids.append(after_id)
        if self._message_snapshot_index < len(self.message_snapshots):
            messages = self.message_snapshots[self._message_snapshot_index]
            self._message_snapshot_index += 1
        else:
            messages = self.messages
        if after_id is None:
            return messages
        ids = [m.get("id") for m in messages]
        return messages[ids.index(after_id) + 1 :] if after_id in ids else messages

    async def reply_permission(
        self, session_id: str, request_id: str, decision: str, message: str | None = None
    ) -> bool:
        return True

    async def stream_events(self) -> AsyncIterator[OpenCodeEvent]:
        """Yield one batch of events per call (simulates separate SSE connections)."""
        if self._batch_index < len(self._event_batches):
            batch = self._event_batches[self._batch_index]
            self._batch_index += 1
            for event in batch:
                yield event


def _forwarder(
    server: _RecordingServerClient,
    opencode: _FakeOpenCodeClient,
    *,
    opencode_session_id: str = _SESSION,
) -> fwd_mod.OpenCodeNativeForwarder:
    return fwd_mod.OpenCodeNativeForwarder(
        session_id="conv_1",
        opencode_session_id=opencode_session_id,
        opencode_client=opencode,  # type: ignore[arg-type]
        server_client=server,  # type: ignore[arg-type]
    )


def _ev(event_type: str, **data: Any) -> OpenCodeEvent:
    data.setdefault("sessionID", _SESSION)
    return OpenCodeEvent(id=None, type=event_type, data=data, location=None)


def _assistant(
    message_id: str, *content: dict[str, Any], completed: bool = True
) -> dict[str, Any]:
    time_info: dict[str, Any] = {"created": 1}
    if completed:
        time_info["completed"] = 2
    return {
        "id": message_id,
        "type": "assistant",
        "agent": "build",
        "model": {"id": "m", "providerID": "p"},
        "content": list(content),
        "time": time_info,
    }


def _text(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def _live_text_turn(message_id: str, text: str) -> list[OpenCodeEvent]:
    """The live frames for one assistant step that says *text*."""
    return [
        _ev("session.execution.started"),
        _ev(
            "session.step.started",
            assistantMessageID=message_id,
            agent="build",
            model={"id": "m", "providerID": "p"},
            started=1,
        ),
        _ev("session.text.ended", assistantMessageID=message_id, ordinal=0, text=text),
        _ev(
            "session.step.ended",
            assistantMessageID=message_id,
            finish="stop",
            cost=0.0,
            tokens={"input": 0, "output": 0, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        ),
        _ev("session.execution.succeeded"),
    ]


async def _run(fwd: fwd_mod.OpenCodeNativeForwarder, max_reconnects: int) -> None:
    async def _no_sleep(_s: float) -> None:
        pass

    orig_sleep = fwd_mod.asyncio.sleep
    fwd_mod.asyncio.sleep = _no_sleep  # type: ignore[assignment]
    try:
        await fwd.run(max_reconnects=max_reconnects)
    finally:
        fwd_mod.asyncio.sleep = orig_sleep  # type: ignore[assignment]


def _assistant_texts(server: _RecordingServerClient) -> list[str]:
    return [
        body["data"]["item_data"]["content"][0]["text"]
        for _u, body in server.posts
        if body["type"] == "external_conversation_item"
        and body["data"]["item_data"].get("role") == "assistant"
    ]


async def test_handle_event_no_longer_calls_update_last_event_id() -> None:
    """The SSE ``Last-Event-ID`` resume path stays unused."""
    import inspect

    source = inspect.getsource(fwd_mod)
    assert "update_last_event_id" not in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -v`

Expected: FAIL — `test_unknown_event_is_ignored`, `test_event_for_other_session_ignored`, `test_event_without_session_id_passes_filter`, `test_form_created_filters_on_nested_session_id` fail with `AttributeError: 'OpenCodeEvent' object has no attribute 'properties'` (v1 filter, `forwarder.py:413`); `test_consume_once_dispatches_stream_events` fails with `AttributeError: '_FakeOpenCodeClient' object has no attribute 'events'` (v1 `_consume_once`, `forwarder.py:380`). `test_run_reconnects_until_cap` and the `update_last_event_id` guard already pass (loop shape unchanged).

- [ ] **Step 3: Write minimal implementation**

Replace the whole of `omnigent/harnesses/opencode_native/forwarder.py` with:

```python
"""SSE consumer that mirrors OpenCode v2 events into an Omnigent session.

The runner owns this forwarder (parallel to the codex-native forwarder). It
consumes the ``opencode serve`` event stream (``GET /api/event``), keeps the
events for this conversation's OpenCode session and its subagent child
sessions, and translates them into Omnigent session events posted to
``/v1/sessions/{id}/events``.

Each v2 event type maps to one ``_on_<name>`` handler in ``_HANDLERS`` at the
bottom of the module. Unknown events are logged and ignored. Durable
transcript items are deduped by stable OpenCode ids so the web UI and the TUI
driving the same session never double-post.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import PolicyDecision
from omnigent.util.json_types import JsonObject as _JsonObject

_logger = logging.getLogger(__name__)

_AGENT_NAME = "opencode"
# Omnigent session-event types (must match the server's ingestion route;
# shared with the codex-native and claude-native forwarders).
_EXTERNAL_ITEM = "external_conversation_item"
_EXTERNAL_STATUS = "external_session_status"
_EXTERNAL_COMPACTION_STATUS = "external_compaction_status"
_EXTERNAL_SESSION_USAGE = "external_session_usage"
_EXTERNAL_MODEL_CHANGE = "external_model_change"
_EXTERNAL_ELICITATION_RESOLVED = "external_elicitation_resolved"
_EXTERNAL_OUTPUT_TEXT_DELTA = "external_output_text_delta"
_EXTERNAL_OUTPUT_REASONING_DELTA = "external_output_reasoning_delta"
_EXTERNAL_TOOL_OUTPUT_DELTA = "external_tool_output_delta"
_EXTERNAL_SESSION_INTERRUPTED = "external_session_interrupted"
_EXTERNAL_SUBAGENT_START = "external_subagent_start"

_STATUS_RUNNING = "running"
_STATUS_IDLE = "idle"
_STATUS_FAILED = "failed"

# Appended to a failed edge's output when opencode reports a provider-auth
# error so the web surface can prompt a re-auth.
_OPENCODE_REAUTH_HINT = (
    "OpenCode needs you to re-authenticate. Run `opencode auth login` and retry."
)
# v2 ``Session.StructuredError.type`` values (core/src/session/to-session-error.ts).
_AUTH_ERROR_TYPE = "provider.auth"
_ABORTED_ERROR_TYPE = "aborted"
_AUTH_STATUS_CODES = frozenset({401, 403})

# Bound the dedupe set so a long-lived session can't grow it without limit.
_MAX_DEDUPE_KEYS = 8192
# Web status label cap for a retry notice.
_MAX_BLOCKED_ON_CHARS = 200

_JsonMapping: TypeAlias = Mapping[str, object]


# Policy verdict resolver: receives a normalized policy input and returns a
# verdict mapping (or None when no policy is configured / reachable).
PolicyEvaluator = Callable[[_JsonMapping], Awaitable[_JsonMapping | None]]


@dataclass
class OpenCodeForwarderState:
    """
    Mutable dedupe state shared by every mirrored OpenCode session.

    :param seen: Bounded set of dedupe keys already posted.
    """

    seen: OrderedDict[str, None] = field(default_factory=OrderedDict)

    def mark(self, key: str) -> bool:
        """
        Record *key*; return ``True`` the first time it is seen.

        :param key: Stable dedupe key, e.g. ``"opencode:ses_1:tool-call:call_1"``.
        :returns: ``True`` when newly seen, ``False`` for a duplicate.
        """
        if key in self.seen:
            return False
        self.seen[key] = None
        while len(self.seen) > _MAX_DEDUPE_KEYS:
            self.seen.popitem(last=False)
        return True


@dataclass
class _SessionTurn:
    """
    Streaming state for one mirrored OpenCode session.

    :param session_id: OpenCode session id, e.g. ``"ses_abc"``.
    :param conversation_id: Omnigent conversation the session mirrors into;
        ``None`` for a subagent child until its conversation is minted.
    """

    session_id: str
    conversation_id: str | None
    turn_active: bool = False
    # Assistant message of the step in flight (``session.step.started``).
    assistant_message_id: str | None = None
    # Id the turn's ``running`` edge went out with; stamped on every item.
    running_response_id: str | None = None
    # Model of the step in flight, ``provider/id``.
    step_model: str | None = None
    # (assistantMessageID, ordinal) -> final text awaiting the step-end flush.
    pending_text: dict[tuple[str, int], str] = field(default_factory=dict)
    # (assistantMessageID, ordinal) -> text streamed so far without an end.
    streamed_text: dict[tuple[str, int], str] = field(default_factory=dict)
    # (assistantMessageID, ordinal) -> next live-preview chunk index.
    delta_index: dict[tuple[str, int], int] = field(default_factory=dict)
    # (assistantMessageID, ordinal) reasoning blocks already opened.
    reasoning_started: set[tuple[str, int]] = field(default_factory=set)
    # Tool call id -> tool name from ``session.tool.input.started``.
    tool_names: dict[str, str] = field(default_factory=dict)
    # Tool call id -> last ``metadata.output`` streamed as a delta.
    tool_output: dict[str, str] = field(default_factory=dict)
    # Retry notice currently shown on the running edge.
    retry_label: str | None = None


def _str_field(data: Mapping[str, Any], key: str) -> str | None:
    """Return ``data[key]`` when it is a non-empty string."""
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _int_field(data: Mapping[str, Any], key: str) -> int | None:
    """Return ``data[key]`` when it is an int (not a bool)."""
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _event_session_id(event: OpenCodeEvent) -> str | None:
    """Return the OpenCode session an event belongs to (``form.created`` nests it)."""
    session_id = _str_field(event.data, "sessionID")
    if session_id is not None:
        return session_id
    form = event.data.get("form")
    if isinstance(form, Mapping):
        return _str_field(form, "sessionID")
    return None


class OpenCodeNativeForwarder:
    """
    Translate one OpenCode session's v2 event stream into Omnigent events.

    :param session_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param opencode_session_id: OpenCode session id to mirror.
    :param opencode_client: Client connected to the ``opencode serve``
        server (events, history, permission and form replies).
    :param server_client: HTTP client for the Omnigent server (event posts).
    :param bridge_dir: Native OpenCode bridge directory (active-id
        persistence). ``None`` disables bridge writes (tests).
    :param workspace: Session workspace, used for permission normalization.
    :param policy_evaluator: Optional async policy resolver. Production wires
        one that POSTs each request to ``/v1/sessions/{id}/policies/evaluate``
        (see ``omnigent.runner.native.orchestration._build_opencode_policy_evaluator``),
        where an ``ask`` verdict parks a human approval card.
    :param default_decision: Decision used when no evaluator is provided or it
        returns ``None``. Defaults to ``reject`` so an unconfigured policy
        fails closed.
    """

    def __init__(
        self,
        *,
        session_id: str,
        opencode_session_id: str,
        opencode_client: OpenCodeClient,
        server_client: httpx.AsyncClient,
        bridge_dir: Path | None = None,
        workspace: str | None = None,
        policy_evaluator: PolicyEvaluator | None = None,
        default_decision: PolicyDecision = "reject",
    ) -> None:
        self._session_id = session_id
        self._opencode_session_id = opencode_session_id
        self._opencode = opencode_client
        self._server = server_client
        self._bridge_dir = bridge_dir
        self._workspace = workspace
        self._policy_evaluator = policy_evaluator
        self._default_decision = default_decision
        self.state = OpenCodeForwarderState()
        # OpenCode session id -> streaming state; the root plus subagent children.
        self._turns: dict[str, _SessionTurn] = {
            opencode_session_id: _SessionTurn(
                session_id=opencode_session_id, conversation_id=session_id
            )
        }
        self._permission_tasks: dict[str, asyncio.Task[None]] = {}
        self._form_tasks: dict[str, asyncio.Task[None]] = {}

    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used by
            tests to bound the loop.
        """
        attempt = 0
        backoff = 0.5
        try:
            while True:
                try:
                    await self._consume_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                    _logger.warning(
                        "OpenCode forwarder SSE error for session=%s; reconnecting",
                        self._session_id,
                        exc_info=True,
                    )
                attempt += 1
                if max_reconnects is not None and attempt > max_reconnects:
                    return
                await asyncio.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
        finally:
            await self._cancel_background_tasks()

    async def _cancel_background_tasks(self) -> None:
        """Cancel and await every parked permission and form task."""
        tasks = [*self._permission_tasks.values(), *self._form_tasks.values()]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._permission_tasks.clear()
        self._form_tasks.clear()

    async def _consume_once(self) -> None:
        """Consume the event stream once, dispatching each event."""
        async for event in self._opencode.stream_events():
            await self.handle_event(event)

    async def handle_event(self, event: OpenCodeEvent) -> None:
        """
        Translate one OpenCode event into Omnigent session events.

        :param event: A decoded ``/api/event`` frame.
        """
        if not self._event_targets_session(event):
            return
        handler = _HANDLERS.get(event.type)
        if handler is None:
            _logger.debug(
                "OpenCode forwarder ignoring event type=%s for session=%s",
                event.type,
                self._session_id,
            )
            return
        await handler(self, event)

    def _event_targets_session(self, event: OpenCodeEvent) -> bool:
        """
        Return whether *event* belongs to a mirrored session.

        Events carry ``data.sessionID`` (``form.created`` nests it under
        ``form``). Events without a session id pass through.
        """
        session_id = _event_session_id(event)
        return session_id is None or session_id in self._turns

    async def _active_turn(self, event: OpenCodeEvent) -> _SessionTurn | None:
        """Return the mirrored session state an event belongs to."""
        session_id = _event_session_id(event) or self._opencode_session_id
        return self._turns.get(session_id)

    def _is_root(self, turn: _SessionTurn) -> bool:
        """Whether *turn* is this conversation's own OpenCode session."""
        return turn.session_id == self._opencode_session_id

    def _key(self, *parts: str) -> str:
        """
        Build a forwarder-scoped dedupe key.

        :param parts: Key segments, e.g. ``("tool-call", "call_1")``.
        :returns: ``"opencode:<root sessionID>:<part>:..."``.
        """
        return "opencode:" + ":".join((self._opencode_session_id, *parts))

    async def _post_event(
        self,
        event_type: str,
        data: _JsonObject,
        *,
        conversation_id: str | None = None,
    ) -> httpx.Response | None:
        """
        POST one Omnigent session event.

        :param event_type: Omnigent event type, e.g. ``"external_session_status"``.
        :param data: Event data payload.
        :param conversation_id: Target conversation; defaults to this session's.
        :returns: The HTTP response, or ``None`` on transport failure.
        """
        target = conversation_id or self._session_id
        url = f"/v1/sessions/{quote(target, safe='')}/events"
        try:
            return await self._server.post(url, json={"type": event_type, "data": data})
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode forwarder failed to post %s for session=%s",
                event_type,
                target,
                exc_info=True,
            )
            return None


def opencode_tool_output_text(state: _JsonMapping) -> str:
    """
    Extract shared durable output from a completed OpenCode tool state.

    :param state: The opencode tool part ``state`` (``output`` /
        ``metadata.output``).
    :returns: A string suitable for ``function_call_output``.
    """
    output = state.get("output")
    if isinstance(output, str) and output:
        return output
    metadata = state.get("metadata")
    if isinstance(metadata, Mapping):
        meta_out = metadata.get("output")
        if isinstance(meta_out, str) and meta_out:
            return meta_out
    if output is not None and not isinstance(output, str):
        return json.dumps(output, ensure_ascii=True)
    return ""


# Event type -> handler. Keys are v2 ``/api/event`` ``type`` discriminators
# (packages/schema/src/session-event.ts, session-status-event.ts,
# permission.ts, form.ts in OpenCode v2.0.18).
_HANDLERS: dict[str, Callable[[OpenCodeNativeForwarder, OpenCodeEvent], Awaitable[None]]] = {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -v`

Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py
git commit -m "refactor(opencode-native): replace v1 forwarder with v2 event skeleton" -m "Drops every v1 handler: message.updated, message.part.updated and part-type dispatch, session.idle, session.error, session.next.*, question.*, permission.v2.asked."
```

### Task 24: Turn lifecycle: execution.started / session.status / step.started / execution.succeeded

Spec rows `session.status {busy}`, `session.execution.started` -> `external_session_status running` (response id = `assistantMessageID` from `session.step.started`), and `session.execution.succeeded` / `session.status {idle}` -> `idle`. v2 emits both terminal signals, so `_finish_turn` makes the second a no-op. Each step has its own `assistantMessageID` (core/src/session/runner/llm.ts:207,224,288 mint a new id per step), so the turn keeps the id its `running` edge went out with.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 357); class body end (after line 330); module level above `class OpenCodeNativeForwarder` (line 163); import block (17-34)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 239)

**Interfaces:**
- Consumes: Task 23 skeleton; `bridge.update_active_message_id(bridge_dir, message_id, *, status)` (unchanged).
- Produces: `_model_ref(value) -> str | None`; `_post_status(turn, status, *, extra=None)`; `_response_id(turn, message_id) -> str`; `_begin_turn_if_needed(turn)`; `_end_turn(turn, *, status='idle', extra=None)`; `_finish_turn(turn)`; handlers `_on_execution_started`, `_on_execution_succeeded`, `_on_session_status`, `_on_step_started`.

- [ ] **Step 1: Write the failing test**

Also add `from pathlib import Path` to the test module imports (used by the bridge test).

Replace the import block `tests/test_opencode_native_forwarder.py:5-13` with:

```python
import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type
```

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- turn lifecycle ---------------------------------------------------------


async def test_lifecycle_emits_running_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_1"),
        ("idle", "msg_1"),
    ]


async def test_running_edge_deferred_until_step_started() -> None:
    """``session.status busy`` opens the turn; the edge waits for the assistant id."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.status", status={"type": "busy"}))
    assert _status_edges(server.posts) == []
    await fwd.handle_event(_step_started("msg_1"))
    running = [e for e in _status_edges(server.posts) if e["status"] == "running"]
    assert running == [{"status": "running", "response_id": "msg_1"}]


async def test_multi_step_turn_keeps_first_response_id() -> None:
    """Each step has its own assistant id; the turn keeps the id that went live."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_1"),
        ("idle", "msg_1"),
    ]


async def test_second_turn_gets_its_own_running_response_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    for msg in ("msg_a", "msg_b"):
        await fwd.handle_event(_event("session.execution.started"))
        await fwd.handle_event(_step_started(msg))
        await fwd.handle_event(_event("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [(e["status"], e["response_id"]) for e in edges] == [
        ("running", "msg_a"),
        ("idle", "msg_a"),
        ("running", "msg_b"),
        ("idle", "msg_b"),
    ]


async def test_turn_without_step_idles_with_session_fallback() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.status", status={"type": "busy"}))
    await fwd.handle_event(_event("session.status", status={"type": "idle"}))
    edges = _status_edges(server.posts)
    assert edges == [{"status": "idle", "response_id": _SESSION}]


async def test_status_idle_after_execution_succeeded_posts_one_idle() -> None:
    """v2 emits both ``execution.succeeded`` and ``status idle``; idle posts once."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    await fwd.handle_event(_event("session.status", status={"type": "idle"}))
    assert [e["status"] for e in _status_edges(server.posts)] == ["running", "idle"]


async def test_step_started_records_active_message_id_in_bridge(
    tmp_path: Path, monkeypatch: Any
) -> None:
    calls: list[tuple[str | None, str]] = []

    def _record(bridge_dir: Path, message_id: str | None, *, status: str) -> None:
        calls.append((message_id, status))

    monkeypatch.setattr(fwd_mod, "update_active_message_id", _record)
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, bridge_dir=tmp_path)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.succeeded"))
    assert calls == [("msg_1", "busy"), (None, "idle")]


async def test_fixture_turn_opens_and_closes() -> None:
    """The captured execution/step edges drive one running + one idle edge."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    step = _fixture("session.step.started")
    await fwd.handle_event(_fixture("session.execution.started"))
    await fwd.handle_event(step)
    await fwd.handle_event(_fixture("session.execution.succeeded"))
    edges = _status_edges(server.posts)
    assert [e["status"] for e in edges] == ["running", "idle"]
    assert edges[0]["response_id"] == step.data["assistantMessageID"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_lifecycle_emits_running_then_idle or test_running_edge_deferred_until_step_started or test_multi_step_turn_keeps_first_response_id or test_second_turn_gets_its_own_running_response_id or test_turn_without_step_idles_with_session_fallback or test_status_idle_after_execution_succeeded_posts_one_idle or test_step_started_records_active_message_id_in_bridge or test_fixture_turn_opens_and_closes"`

Expected: FAIL with:
  - `test_lifecycle_emits_running_then_idle: AssertionError: assert [] == [('running', ...le', 'msg_1')]`
  - `test_running_edge_deferred_until_step_started: AssertionError: assert [] == [{'response_i...': 'running'}]`
  - `test_multi_step_turn_keeps_first_response_id: AssertionError: assert [] == [('running', ...le', 'msg_1')]`
  - `test_second_turn_gets_its_own_running_response_id: AssertionError: assert [] == [('running', ...le', 'msg_b')]`
  - `test_turn_without_step_idles_with_session_fallback: AssertionError: assert [] == [{'response_i...tus': 'idle'}]`
  - `test_status_idle_after_execution_succeeded_posts_one_idle: AssertionError: assert [] == ['running', 'idle']`
  - `test_step_started_records_active_message_id_in_bridge: AttributeError: <module 'omnigent.harnesses.opencode_native.forwarder' from '/tmp/claude-1000/-home-jason-forks-omnigent/5e0dc4f8-6022-4ed8-8d53-74a9a0fb56eb/scratch`
  - `test_fixture_turn_opens_and_closes: AssertionError: assert [] == ['running', 'idle']`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 357):

```python
    "session.execution.started": OpenCodeNativeForwarder._on_execution_started,
    "session.execution.succeeded": OpenCodeNativeForwarder._on_execution_succeeded,
    "session.status": OpenCodeNativeForwarder._on_session_status,
    "session.step.started": OpenCodeNativeForwarder._on_step_started,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_post_event`, which ends at line 330):

```python
    async def _post_status(
        self, turn: _SessionTurn, status: str, *, extra: _JsonMapping | None = None
    ) -> None:
        """Publish a coarse session status edge into *turn*'s conversation."""
        data: _JsonObject = {"status": status}
        if extra:
            data.update(extra)
        await self._post_event(_EXTERNAL_STATUS, data, conversation_id=turn.conversation_id)

    def _response_id(self, turn: _SessionTurn, message_id: str | None) -> str:
        """Per-turn ``response_id``: the running edge's id, else the message id."""
        return turn.running_response_id or message_id or turn.session_id

    async def _begin_turn_if_needed(self, turn: _SessionTurn) -> None:
        """Emit the turn's id-bearing ``running`` edge once, when the id is known.

        ``session.execution.started`` / ``session.status busy`` open the turn
        before ``session.step.started`` supplies the assistant message id, so
        the edge is deferred until that id exists; the mirrored items carry
        the same id, which lets the web render in-flight tool calls live.
        """
        turn.turn_active = True
        if turn.running_response_id is None and turn.assistant_message_id is not None:
            turn.running_response_id = turn.assistant_message_id
            await self._post_status(
                turn, _STATUS_RUNNING, extra={"response_id": turn.running_response_id}
            )

    async def _end_turn(
        self,
        turn: _SessionTurn,
        *,
        status: str = _STATUS_IDLE,
        extra: _JsonMapping | None = None,
    ) -> None:
        """Post the terminal edge stamped with the turn's id and reset per-turn state."""
        turn.turn_active = False
        turn.delta_index.clear()
        turn.reasoning_started.clear()
        turn.tool_output.clear()
        turn.retry_label = None
        terminal_id = turn.running_response_id or turn.assistant_message_id
        merged: _JsonObject = {"response_id": terminal_id or turn.session_id}
        if extra:
            merged.update(extra)
        if self._bridge_dir is not None and self._is_root(turn):
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(turn, status, extra=merged)
        turn.assistant_message_id = None
        turn.running_response_id = None

    async def _finish_turn(self, turn: _SessionTurn) -> None:
        """End an active turn as idle; a second terminal signal is a no-op."""
        if not turn.turn_active:
            return
        await self._end_turn(turn)

    async def _on_execution_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.started`` — open the turn."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._begin_turn_if_needed(turn)

    async def _on_execution_succeeded(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.succeeded`` — flush, usage, idle."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._finish_turn(turn)

    async def _on_session_status(self, event: OpenCodeEvent) -> None:
        """Handle ``session.status {busy|idle}``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        status = event.data.get("status")
        if not isinstance(status, Mapping):
            return
        status_type = status.get("type")
        if status_type == "busy":
            await self._begin_turn_if_needed(turn)
        elif status_type == "idle":
            await self._finish_turn(turn)

    async def _on_step_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.started`` — record the assistant id and model."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None:
            return
        turn.assistant_message_id = message_id
        turn.step_model = _model_ref(event.data.get("model"))
        if self._is_root(turn) and self._bridge_dir is not None:
            update_active_message_id(self._bridge_dir, message_id, status="busy")
        await self._begin_turn_if_needed(turn)
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 163):

```python
def _model_ref(value: object) -> str | None:
    """Render a v2 ``Model.Ref`` ``{id, providerID, variant?}`` as ``provider/id``."""
    if not isinstance(value, Mapping):
        return None
    provider = value.get("providerID")
    model_id = value.get("id")
    if isinstance(provider, str) and provider and isinstance(model_id, str) and model_id:
        return f"{provider}/{model_id}"
    return None
```

Replace the import block (lines 17-34) with:

```python
import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.bridge import update_active_message_id
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import PolicyDecision
from omnigent.util.json_types import JsonObject as _JsonObject
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_lifecycle_emits_running_then_idle or test_running_edge_deferred_until_step_started or test_multi_step_turn_keeps_first_response_id or test_second_turn_gets_its_own_running_response_id or test_turn_without_step_idles_with_session_fallback or test_status_idle_after_execution_succeeded_posts_one_idle or test_step_started_records_active_message_id_in_bridge or test_fixture_turn_opens_and_closes"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `15 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 turn lifecycle as running/idle edges"
```

### Task 25: Live text streaming: session.text.delta / session.text.ended, flushed on step end

Spec rows `session.text.delta {ordinal, delta}` -> `external_output_text_delta` and `session.text.ended {ordinal, text}` -> buffered, flushed as an assistant `external_conversation_item` on `session.step.ended`. The deltas use the server's finalize/retire contract (omnigent/server/routes/_sessions/helpers.py:2979-3035 `_publish_external_output_text_delta` accepts `message_id`/`index`/`final`; helpers.py:3301-3327 maps an assistant item's `data.message_id` to `stream_message_id`), the same shape codex-native posts (omnigent/harnesses/codex_native/forwarder.py:6829-6867 and :6147 passes the completed item's stream id). The v1 forwarder dropped deltas because it lacked this handshake (current forwarder.py:1281-1287); here every delta and its final item share `opencode:<assistantMessageID>:text:<ordinal>`.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 471); `_end_turn` (372-393); class body end (after line 439)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 347)

**Interfaces:**
- Consumes: Tasks 20-21.
- Produces: `_stream_id(message_id, kind, ordinal) -> str` (staticmethod); `_post_assistant_text(turn, text, *, message_id, stream_id)`; `_flush_pending_text(turn)`; handlers `_on_text_delta`, `_on_text_ended`, `_on_step_ended` (flush-only for now); `_end_turn` now flushes first.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- text streaming ---------------------------------------------------------


async def test_text_delta_streams_live_preview_chunks() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for chunk in ("Hel", "lo"):
        await fwd.handle_event(
            _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta=chunk)
        )
    deltas = _datas(server.posts, "external_output_text_delta")
    assert deltas == [
        {"delta": "Hel", "message_id": "opencode:msg_1:text:0", "index": 0, "final": False},
        {"delta": "lo", "message_id": "opencode:msg_1:text:0", "index": 1, "final": False},
    ]


async def test_text_ended_flushes_on_step_end_and_retires_preview() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="Hi")
    )
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="Hi there")
    )
    assert _items(server.posts) == []  # buffered until the step ends
    await fwd.handle_event(_step_ended("msg_1"))
    items = _items(server.posts)
    assert len(items) == 1
    assert items[0]["item_data"]["role"] == "assistant"
    assert items[0]["item_data"]["content"] == [{"type": "output_text", "text": "Hi there"}]
    assert items[0]["response_id"] == "msg_1"
    # Same id as the deltas, so the server retires the live preview.
    assert items[0]["message_id"] == "opencode:msg_1:text:0"


async def test_text_flush_dedupes_repeated_step_end() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    ended = _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="once")
    await fwd.handle_event(ended)
    await fwd.handle_event(_step_ended("msg_1"))
    await fwd.handle_event(ended)
    await fwd.handle_event(_step_ended("msg_1"))
    assert len(_items(server.posts)) == 1


async def test_empty_text_is_not_persisted() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="")
    )
    await fwd.handle_event(_step_ended("msg_1"))
    assert _items(server.posts) == []


async def test_two_text_ordinals_flush_in_order() -> None:
    """Text before and after a tool call (ordinals 0 and 1) become two items, in order."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=1, text="after")
    )
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="before")
    )
    await fwd.handle_event(_step_ended("msg_1"))
    items = _items(server.posts)
    assert [i["item_data"]["content"][0]["text"] for i in items] == ["before", "after"]
    assert [i["message_id"] for i in items] == [
        "opencode:msg_1:text:0",
        "opencode:msg_1:text:1",
    ]


async def test_fixture_text_deltas_and_final_text_share_stream_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    ended = events_of_type("session.text.ended")[0]["data"]
    message_id, ordinal = ended["assistantMessageID"], ended["ordinal"]
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in events_of_type("session.text.delta"):
        data = raw["data"]
        if data["assistantMessageID"] == message_id and data["ordinal"] == ordinal:
            await fwd.handle_event(_to_event(raw))
    await fwd.handle_event(_fixture("session.text.ended"))
    await fwd.handle_event(_fixture("session.step.ended"))
    stream_id = f"opencode:{message_id}:text:{ordinal}"
    deltas = _datas(server.posts, "external_output_text_delta")
    assert deltas and {d["message_id"] for d in deltas} == {stream_id}
    assert "".join(d["delta"] for d in deltas) == ended["text"]
    item = next(i for i in _items(server.posts) if i["item_type"] == "message")
    assert item["message_id"] == stream_id
    assert item["item_data"]["content"][0]["text"] == ended["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_text_delta_streams_live_preview_chunks or test_text_ended_flushes_on_step_end_and_retires_preview or test_text_flush_dedupes_repeated_step_end or test_empty_text_is_not_persisted or test_two_text_ordinals_flush_in_order or test_fixture_text_deltas_and_final_text_share_stream_id"`

Expected: FAIL with:
  - `test_text_delta_streams_live_preview_chunks: AssertionError: assert [] == [{'delta': 'H...sg_1:text:0'}]`
  - `test_two_text_ordinals_flush_in_order: AssertionError: assert [] == ['before', 'after']`
  - `test_text_ended_flushes_on_step_end_and_retires_preview: assert 0 == 1`
  - `test_text_flush_dedupes_repeated_step_end: AssertionError: assert 0 == 1`
  - `test_fixture_text_deltas_and_final_text_share_stream_id: assert ([])`
  Already passing (guards that the new code must keep true): `test_empty_text_is_not_persisted`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 471):

```python
    "session.text.delta": OpenCodeNativeForwarder._on_text_delta,
    "session.text.ended": OpenCodeNativeForwarder._on_text_ended,
    "session.step.ended": OpenCodeNativeForwarder._on_step_ended,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_step_started`, which ends at line 439):

```python
    @staticmethod
    def _stream_id(message_id: str, kind: str, ordinal: int) -> str:
        """Live-preview id shared by text deltas and the item that retires them."""
        return f"opencode:{message_id}:{kind}:{ordinal}"

    async def _post_assistant_text(
        self, turn: _SessionTurn, text: str, *, message_id: str | None, stream_id: str
    ) -> None:
        """Persist a finalized assistant message that retires its live preview."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "message",
                "item_data": {
                    "role": "assistant",
                    "agent": _AGENT_NAME,
                    "content": [{"type": "output_text", "text": text}],
                },
                "response_id": self._response_id(turn, message_id),
                "message_id": stream_id,
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_text_delta(self, event: OpenCodeEvent) -> None:
        """Handle ``session.text.delta`` — stream a live assistant preview chunk."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        delta = event.data.get("delta")
        if message_id is None or ordinal is None or not isinstance(delta, str) or not delta:
            return
        key = (message_id, ordinal)
        await self._begin_turn_if_needed(turn)
        index = turn.delta_index.get(key, 0)
        turn.delta_index[key] = index + 1
        turn.streamed_text[key] = turn.streamed_text.get(key, "") + delta
        await self._post_event(
            _EXTERNAL_OUTPUT_TEXT_DELTA,
            {
                "delta": delta,
                "message_id": self._stream_id(message_id, "text", ordinal),
                "index": index,
                "final": False,
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_text_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.text.ended`` — buffer the full text for the step-end flush."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        text = event.data.get("text")
        if message_id is None or ordinal is None or not isinstance(text, str):
            return
        turn.streamed_text.pop((message_id, ordinal), None)
        turn.pending_text[(message_id, ordinal)] = text

    async def _flush_pending_text(self, turn: _SessionTurn) -> None:
        """Persist buffered assistant text as durable chat items, once each."""
        # Ordinal order, not arrival order: text around a tool call must read in sequence.
        for (message_id, ordinal), text in sorted(
            turn.pending_text.items(), key=lambda item: item[0][1]
        ):
            turn.pending_text.pop((message_id, ordinal), None)
            if not text:
                continue
            if not self.state.mark(self._key("text-final", message_id, str(ordinal))):
                continue
            await self._post_assistant_text(
                turn,
                text,
                message_id=message_id,
                stream_id=self._stream_id(message_id, "text", ordinal),
            )

    async def _on_step_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.ended`` — flush the step's buffered text."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        await self._flush_pending_text(turn)
```

Replace `_end_turn` (lines 372-393) with:

```python
    async def _end_turn(
        self,
        turn: _SessionTurn,
        *,
        status: str = _STATUS_IDLE,
        extra: _JsonMapping | None = None,
    ) -> None:
        """Post the terminal edge stamped with the turn's id and reset per-turn state."""
        await self._flush_pending_text(turn)
        turn.turn_active = False
        turn.delta_index.clear()
        turn.reasoning_started.clear()
        turn.tool_output.clear()
        turn.retry_label = None
        terminal_id = turn.running_response_id or turn.assistant_message_id
        merged: _JsonObject = {"response_id": terminal_id or turn.session_id}
        if extra:
            merged.update(extra)
        if self._bridge_dir is not None and self._is_root(turn):
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(turn, status, extra=merged)
        turn.assistant_message_id = None
        turn.running_response_id = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_text_delta_streams_live_preview_chunks or test_text_ended_flushes_on_step_end_and_retires_preview or test_text_flush_dedupes_repeated_step_end or test_empty_text_is_not_persisted or test_two_text_ordinals_flush_in_order or test_fixture_text_deltas_and_final_text_share_stream_id"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `20 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): stream v2 text deltas and flush final text on step end"
```

### Task 26: Reasoning streaming: session.reasoning.delta / .ended

Spec row `session.reasoning.delta` / `.ended` -> `external_output_reasoning_delta {delta, started}` (the codex-native contract, omnigent/harnesses/codex_native/forwarder.py:7099-7125). `started` is true on the first chunk of each `(assistantMessageID, ordinal)` block; `.ended` posts the whole block only when no delta streamed (a provider that sends reasoning in one piece).

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 560); class body end (after line 525)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 430)

**Interfaces:**
- Consumes: Tasks 20-22.
- Produces: handlers `_on_reasoning_delta`, `_on_reasoning_ended`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- reasoning --------------------------------------------------------------


async def test_reasoning_deltas_open_block_once() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for chunk in ("Let me", " think"):
        await fwd.handle_event(
            _event("session.reasoning.delta", assistantMessageID="msg_1", ordinal=0, delta=chunk)
        )
    await fwd.handle_event(
        _event(
            "session.reasoning.ended", assistantMessageID="msg_1", ordinal=0, text="Let me think"
        )
    )
    assert _datas(server.posts, "external_output_reasoning_delta") == [
        {"delta": "Let me", "started": True},
        {"delta": " think", "started": False},
    ]


async def test_reasoning_ended_without_deltas_posts_whole_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.reasoning.ended", assistantMessageID="msg_1", ordinal=0, text="Hmm.")
    )
    assert _datas(server.posts, "external_output_reasoning_delta") == [
        {"delta": "Hmm.", "started": True}
    ]


async def test_second_reasoning_ordinal_opens_a_new_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for ordinal in (0, 1):
        await fwd.handle_event(
            _event(
                "session.reasoning.delta", assistantMessageID="msg_1", ordinal=ordinal, delta="x"
            )
        )
    started = [d["started"] for d in _datas(server.posts, "external_output_reasoning_delta")]
    assert started == [True, True]


async def test_fixture_reasoning_streams_as_one_block() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    ended = events_of_type("session.reasoning.ended")[0]["data"]
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in events_of_type("session.reasoning.delta"):
        if raw["data"]["assistantMessageID"] == ended["assistantMessageID"]:
            await fwd.handle_event(_to_event(raw))
    await fwd.handle_event(_fixture("session.reasoning.ended"))
    deltas = _datas(server.posts, "external_output_reasoning_delta")
    assert deltas[0]["started"] is True
    assert all(d["started"] is False for d in deltas[1:])
    assert "".join(d["delta"] for d in deltas) == ended["text"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_reasoning_deltas_open_block_once or test_reasoning_ended_without_deltas_posts_whole_block or test_second_reasoning_ordinal_opens_a_new_block or test_fixture_reasoning_streams_as_one_block"`

Expected: FAIL with:
  - `test_reasoning_deltas_open_block_once: AssertionError: assert [] == [{'delta': 'L...rted': False}]`
  - `test_reasoning_ended_without_deltas_posts_whole_block: AssertionError: assert [] == [{'delta': 'H...arted': True}]`
  - `test_second_reasoning_ordinal_opens_a_new_block: assert [] == [True, True]`
  - `test_fixture_reasoning_streams_as_one_block: IndexError: list index out of range`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 560):

```python
    "session.reasoning.delta": OpenCodeNativeForwarder._on_reasoning_delta,
    "session.reasoning.ended": OpenCodeNativeForwarder._on_reasoning_ended,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_step_ended`, which ends at line 525):

```python
    async def _on_reasoning_delta(self, event: OpenCodeEvent) -> None:
        """Handle ``session.reasoning.delta`` — transient reasoning chunk."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        delta = event.data.get("delta")
        if message_id is None or ordinal is None or not isinstance(delta, str) or not delta:
            return
        key = (message_id, ordinal)
        started = key not in turn.reasoning_started
        turn.reasoning_started.add(key)
        await self._begin_turn_if_needed(turn)
        await self._post_event(
            _EXTERNAL_OUTPUT_REASONING_DELTA,
            {"delta": delta, "started": started},
            conversation_id=turn.conversation_id,
        )

    async def _on_reasoning_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.reasoning.ended`` — post the whole block if no delta streamed."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        ordinal = _int_field(event.data, "ordinal")
        text = event.data.get("text")
        if message_id is None or ordinal is None or not isinstance(text, str) or not text:
            return
        key = (message_id, ordinal)
        if key in turn.reasoning_started:
            return
        turn.reasoning_started.add(key)
        await self._begin_turn_if_needed(turn)
        await self._post_event(
            _EXTERNAL_OUTPUT_REASONING_DELTA,
            {"delta": text, "started": True},
            conversation_id=turn.conversation_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_reasoning_deltas_open_block_once or test_reasoning_ended_without_deltas_posts_whole_block or test_second_reasoning_ordinal_opens_a_new_block or test_fixture_reasoning_streams_as_one_block"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `24 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): stream v2 reasoning deltas"
```

### Task 27: Tool calls: session.tool.input.started / .called / .success / .failed

Spec rows `session.tool.called {id, name, input}` -> `function_call` and `.success {content, metadata}` / `.failed {error}` -> `function_call_output`. Correction to the spec table: `session.tool.called` carries `{assistantMessageID, id, input, executed, state?}` but **no `name`** (packages/schema/src/session-event.ts:510-519); the name arrives earlier on `session.tool.input.started {id, name}` (:479-486, always published before the call: core/src/session/runner/publish-llm-event.ts:278-301). Names pass through unchanged (`shell`, `edit`, `subagent`, MCP names). A malformed-input failure publishes `session.tool.failed` without a `called` (publish-llm-event.ts:314-339), so the call half is posted first. Buffered text is flushed before a call so text the model wrote first lands above it.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 603); class body end (after line 566); module level above `class OpenCodeNativeForwarder` (line 175)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 493)

**Interfaces:**
- Consumes: Tasks 20-22.
- Produces: `opencode_tool_content_text(content, *, error=None) -> str` (module-level, public); `_post_tool_call(turn, call_id, tool, arguments, *, message_id)`; `_post_tool_output(turn, call_id, output, *, message_id)`; handlers `_on_tool_input_started`, `_on_tool_called`, `_on_tool_success`, `_on_tool_failed`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- tools ------------------------------------------------------------------


def test_tool_content_text_joins_text_and_names_files() -> None:
    content = [
        {"type": "text", "text": "line 1"},
        {"type": "file", "uri": "file:///tmp/a.png", "mime": "image/png", "name": "a.png"},
    ]
    assert fwd_mod.opencode_tool_content_text(content) == "line 1\n[file: a.png]"


def test_tool_content_text_prefixes_errors() -> None:
    error = {"type": "tool.execution", "message": "boom"}
    assert fwd_mod.opencode_tool_content_text(None, error=error) == "[error] boom"
    partial = [{"type": "text", "text": "partial"}]
    assert fwd_mod.opencode_tool_content_text(partial, error=error) == "[error] boom\npartial"


async def test_fixture_shell_call_and_output() -> None:
    """The captured ``shell`` call posts under its v2 name with its input + output."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    started = next(
        raw
        for raw in events_of_type("session.tool.input.started")
        if raw["data"]["name"] == "shell"
    )
    call_id = started["data"]["id"]
    called = next(
        raw for raw in events_of_type("session.tool.called") if raw["data"]["id"] == call_id
    )
    success = next(
        raw for raw in events_of_type("session.tool.success") if raw["data"]["id"] == call_id
    )
    await fwd.handle_event(_fixture("session.step.started"))
    for raw in (started, called, success):
        await fwd.handle_event(_to_event(raw))
    items = _items(server.posts)
    call = next(i for i in items if i["item_type"] == "function_call")
    assert call["item_data"]["name"] == "shell"
    assert call["item_data"]["call_id"] == call_id
    assert fwd_mod.json.loads(call["item_data"]["arguments"]) == called["data"]["input"]
    out = next(i for i in items if i["item_type"] == "function_call_output")
    assert out["item_data"]["output"] == fwd_mod.opencode_tool_content_text(
        success["data"]["content"]
    )


async def test_tool_names_pass_through_unchanged() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for call_id, name in (("c1", "edit"), ("c2", "subagent"), ("c3", "omnigent_sys_session_list")):
        await fwd.handle_event(
            _event("session.tool.input.started", assistantMessageID="msg_1", id=call_id, name=name)
        )
        await fwd.handle_event(
            _event(
                "session.tool.called",
                assistantMessageID="msg_1",
                id=call_id,
                input={},
                executed=True,
            )
        )
    names = [
        i["item_data"]["name"] for i in _items(server.posts) if i["item_type"] == "function_call"
    ]
    assert names == ["edit", "subagent", "omnigent_sys_session_list"]


async def test_tool_items_share_the_running_response_id() -> None:
    """Tool items in a later step still group under the turn's live id."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_2", id="c1", name="shell")
    )
    await fwd.handle_event(
        _event(
            "session.tool.called",
            assistantMessageID="msg_2",
            id="c1",
            input={"command": "ls"},
            executed=True,
        )
    )
    call = next(i for i in _items(server.posts) if i["item_type"] == "function_call")
    assert call["response_id"] == "msg_1"


async def test_tool_failed_posts_error_output() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_1", id="c1", name="shell")
    )
    await fwd.handle_event(
        _event(
            "session.tool.called",
            assistantMessageID="msg_1",
            id="c1",
            input={"command": "x"},
            executed=True,
        )
    )
    await fwd.handle_event(
        _event(
            "session.tool.failed",
            assistantMessageID="msg_1",
            id="c1",
            error={"type": "tool.execution", "message": "boom"},
            executed=True,
        )
    )
    out = next(i for i in _items(server.posts) if i["item_type"] == "function_call_output")
    assert out["item_data"] == {"call_id": "c1", "output": "[error] boom"}


async def test_tool_failed_without_call_posts_call_first() -> None:
    """Malformed tool input fails without ``session.tool.called``; keep the pair."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.tool.input.started", assistantMessageID="msg_1", id="c1", name="edit")
    )
    await fwd.handle_event(
        _event(
            "session.tool.failed",
            assistantMessageID="msg_1",
            id="c1",
            error={"type": "tool.input-json", "message": "bad json"},
            executed=False,
        )
    )
    kinds = [(i["item_type"], i["item_data"].get("name")) for i in _items(server.posts)]
    assert kinds == [("function_call", "edit"), ("function_call_output", None)]


async def test_tool_call_and_output_dedupe() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    called = _event(
        "session.tool.called", assistantMessageID="msg_1", id="c1", input={}, executed=True
    )
    success = _event(
        "session.tool.success",
        assistantMessageID="msg_1",
        id="c1",
        content=[{"type": "text", "text": "ok"}],
        executed=True,
    )
    for event in (called, called, success, success):
        await fwd.handle_event(event)
    kinds = [i["item_type"] for i in _items(server.posts)]
    assert kinds == ["function_call", "function_call_output"]


async def test_text_before_tool_call_lands_first() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_1", ordinal=0, text="Running ls.")
    )
    await fwd.handle_event(
        _event("session.tool.called", assistantMessageID="msg_1", id="c1", input={}, executed=True)
    )
    assert [i["item_type"] for i in _items(server.posts)] == ["message", "function_call"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_tool_content_text_joins_text_and_names_files or test_tool_content_text_prefixes_errors or test_fixture_shell_call_and_output or test_tool_names_pass_through_unchanged or test_tool_items_share_the_running_response_id or test_tool_failed_posts_error_output or test_tool_failed_without_call_posts_call_first or test_tool_call_and_output_dedupe or test_text_before_tool_call_lands_first"`

Expected: FAIL with:
  - `test_tool_content_text_joins_text_and_names_files: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'opencode_tool_content_text'. Did you mean: 'opencode_tool_output_text'?`
  - `test_tool_content_text_prefixes_errors: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'opencode_tool_content_text'. Did you mean: 'opencode_tool_output_text'?`
  - `test_fixture_shell_call_and_output: StopIteration`
  - `test_tool_names_pass_through_unchanged: AssertionError: assert [] == ['edit', 'sub...session_list']`
  - `test_tool_items_share_the_running_response_id: StopIteration`
  - `test_tool_failed_posts_error_output: StopIteration`
  - `test_tool_failed_without_call_posts_call_first: AssertionError: assert [] == [('function_c...utput', None)]`
  - `test_tool_call_and_output_dedupe: AssertionError: assert [] == ['function_ca..._call_output']`
  - `test_text_before_tool_call_lands_first: AssertionError: assert [] == ['message', 'function_call']`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 603):

```python
    "session.tool.input.started": OpenCodeNativeForwarder._on_tool_input_started,
    "session.tool.called": OpenCodeNativeForwarder._on_tool_called,
    "session.tool.success": OpenCodeNativeForwarder._on_tool_success,
    "session.tool.failed": OpenCodeNativeForwarder._on_tool_failed,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_reasoning_ended`, which ends at line 566):

```python
    async def _post_tool_call(
        self,
        turn: _SessionTurn,
        call_id: str,
        tool: str,
        arguments: _JsonObject,
        *,
        message_id: str | None,
    ) -> None:
        """Mirror a tool invocation as a function_call item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call",
                "item_data": {
                    "agent": _AGENT_NAME,
                    "name": tool,
                    "arguments": json.dumps(arguments, ensure_ascii=True),
                    "call_id": call_id,
                },
                "response_id": self._response_id(turn, message_id),
            },
            conversation_id=turn.conversation_id,
        )

    async def _post_tool_output(
        self, turn: _SessionTurn, call_id: str, output: str, *, message_id: str | None
    ) -> None:
        """Mirror a tool result as a function_call_output item."""
        await self._post_event(
            _EXTERNAL_ITEM,
            {
                "item_type": "function_call_output",
                "item_data": {"call_id": call_id, "output": output},
                "response_id": self._response_id(turn, message_id),
            },
            conversation_id=turn.conversation_id,
        )

    async def _on_tool_input_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.input.started`` — remember the tool's name."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        name = _str_field(event.data, "name")
        if call_id is not None and name is not None:
            turn.tool_names[call_id] = name

    async def _on_tool_called(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.called`` — mirror the call as ``function_call``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None or not self.state.mark(self._key("tool-call", call_id)):
            return
        raw_input = event.data.get("input")
        arguments = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        await self._begin_turn_if_needed(turn)
        # Text the model wrote before the call lands above it in the chat.
        await self._flush_pending_text(turn)
        await self._post_tool_call(
            turn,
            call_id,
            turn.tool_names.get(call_id, "tool"),
            arguments,
            message_id=_str_field(event.data, "assistantMessageID"),
        )

    async def _on_tool_success(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.success`` — mirror the result as ``function_call_output``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None or not self.state.mark(self._key("tool-out", call_id)):
            return
        turn.tool_output.pop(call_id, None)
        await self._post_tool_output(
            turn,
            call_id,
            opencode_tool_content_text(event.data.get("content")),
            message_id=_str_field(event.data, "assistantMessageID"),
        )

    async def _on_tool_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.failed`` — post an ``[error]`` output.

        A malformed-input failure arrives without ``session.tool.called``, so
        the call half is posted first to keep the output paired.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        if call_id is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if self.state.mark(self._key("tool-call", call_id)):
            await self._post_tool_call(
                turn, call_id, turn.tool_names.get(call_id, "tool"), {}, message_id=message_id
            )
        if not self.state.mark(self._key("tool-out", call_id)):
            return
        turn.tool_output.pop(call_id, None)
        output = opencode_tool_content_text(
            event.data.get("content"), error=event.data.get("error")
        )
        await self._post_tool_output(turn, call_id, output, message_id=message_id)
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 175):

```python
def opencode_tool_content_text(content: object, *, error: object = None) -> str:
    """
    Flatten a v2 tool result into ``function_call_output`` text.

    :param content: ``Tool.Content[]`` (``{type:"text", text}`` /
        ``{type:"file", uri, mime, name?}``) from ``session.tool.success`` /
        ``.failed`` or a completed tool state.
    :param error: ``Session.StructuredError`` ``{type, message}`` for a failed
        tool, else ``None``.
    :returns: The output text; failures are prefixed with ``[error]``.
    """
    parts: list[str] = []
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif item.get("type") == "file":
                name = item.get("name") or item.get("uri") or item.get("mime") or "file"
                parts.append(f"[file: {name}]")
    text = "\n".join(part for part in parts if part)
    if isinstance(error, Mapping):
        message = error.get("message")
        detail = message if isinstance(message, str) and message else error.get("type")
        prefix = f"[error] {detail}" if detail else "[error]"
        return f"{prefix}\n{text}" if text else prefix
    return text
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_tool_content_text_joins_text_and_names_files or test_tool_content_text_prefixes_errors or test_fixture_shell_call_and_output or test_tool_names_pass_through_unchanged or test_tool_items_share_the_running_response_id or test_tool_failed_posts_error_output or test_tool_failed_without_call_posts_call_first or test_tool_call_and_output_dedupe or test_text_before_tool_call_lands_first"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `33 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 tool calls and results"
```

### Task 28: Tool progress: session.tool.progress -> external_tool_output_delta

Spec row `session.tool.progress {metadata}` -> `external_tool_output_delta` only when metadata carries incremental output. Heuristic: forward when `metadata.output` is a string that extends the previously seen string for that call (post only the new suffix, `{call_id, delta}` — the payload codex-native posts at omnigent/harnesses/codex_native/forwarder.py:6870-6893); a shorter or non-prefix value is a replacement and is recorded but not streamed. Evidence that built-ins never qualify in 2.0.18: `Tool.Progress` is documented as "Live replacement metadata for a running tool" (packages/schema/src/session-event.ts:522-529) and the publisher overwrites it wholesale (core/src/session/runner/publish-llm-event.ts:560-571 `tool.progress = update`); the only built-in callers send ids, not output — shell `context.progress({ shellID: info.id })` (core/src/tool/plugin/shell.ts:213), subagent `{ sessionID: child.id, status: "running" }` (core/src/tool/plugin/subagent.ts:201), websearch `{ provider }` (core/src/tool/plugin/websearch.ts:63), codemode `{ toolCalls }` (core/src/codemode/tool.ts:88). So for built-ins the progress event is dropped and output arrives on `session.tool.success`; plugin/MCP tools that report a growing `output` string stream live. If the Stage 0 recon finds a different incremental key, change only the `metadata.get("output")` lookup.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 748); class body end (after line 707)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 670)

**Interfaces:**
- Consumes: Task 27.
- Produces: handler `_on_tool_progress` (subagent linking added in Task 41).

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- tool progress ----------------------------------------------------------


async def test_tool_progress_streams_growing_output_suffix() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for output in ("line1\n", "line1\nline2\n", "line1\nline2\n"):
        await fwd.handle_event(
            _event(
                "session.tool.progress",
                assistantMessageID="msg_1",
                id="c1",
                metadata={"output": output},
            )
        )
    assert _datas(server.posts, "external_tool_output_delta") == [
        {"call_id": "c1", "delta": "line1\n"},
        {"call_id": "c1", "delta": "line2\n"},
    ]


async def test_tool_progress_replacement_output_is_not_streamed() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    for output in ("abc", "xyz-longer"):
        await fwd.handle_event(
            _event(
                "session.tool.progress",
                assistantMessageID="msg_1",
                id="c1",
                metadata={"output": output},
            )
        )
    assert _datas(server.posts, "external_tool_output_delta") == [
        {"call_id": "c1", "delta": "abc"}
    ]


async def test_fixture_shell_progress_without_output_is_dropped() -> None:
    """v2 shell progress is ``{shellID}`` only (tool/plugin/shell.ts), so nothing streams."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    progress = _fixture("session.tool.progress")
    assert "output" not in progress.data["metadata"]
    await fwd.handle_event(_fixture("session.step.started"))
    await fwd.handle_event(progress)
    assert "external_tool_output_delta" not in _types(server.posts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_tool_progress_streams_growing_output_suffix or test_tool_progress_replacement_output_is_not_streamed or test_fixture_shell_progress_without_output_is_dropped"`

Expected: FAIL with:
  - `test_tool_progress_streams_growing_output_suffix: AssertionError: assert [] == [{'call_id': ...': 'line2\n'}]`
  - `test_tool_progress_replacement_output_is_not_streamed: AssertionError: assert [] == [{'call_id': ...elta': 'abc'}]`
  Already passing (guards that the new code must keep true): `test_fixture_shell_progress_without_output_is_dropped`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 748):

```python
    "session.tool.progress": OpenCodeNativeForwarder._on_tool_progress,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_tool_failed`, which ends at line 707):

```python
    async def _on_tool_progress(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.progress`` — stream incremental tool output.

        v2 progress metadata is a replacement snapshot. Built-in tools report
        ids only (shell ``{shellID}``, subagent ``{sessionID, status}``), so
        output streams only when a tool reports a growing ``metadata.output``
        string; only the new suffix is forwarded.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        metadata = event.data.get("metadata")
        if call_id is None or not isinstance(metadata, Mapping):
            return
        output = metadata.get("output")
        if not isinstance(output, str):
            return
        previous = turn.tool_output.get(call_id, "")
        turn.tool_output[call_id] = output
        if len(output) <= len(previous) or not output.startswith(previous):
            return
        await self._post_event(
            _EXTERNAL_TOOL_OUTPUT_DELTA,
            {"call_id": call_id, "delta": output[len(previous) :]},
            conversation_id=turn.conversation_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_tool_progress_streams_growing_output_suffix or test_tool_progress_replacement_output_is_not_streamed or test_fixture_shell_progress_without_output_is_dropped"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `36 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): stream incremental tool output from v2 progress metadata"
```

### Task 29: Usage: session.step.ended / .failed / session.usage.updated -> external_session_usage

Spec row `session.step.ended {cost, tokens, files}`, `session.usage.updated` -> accumulate; `external_session_usage` with the same payload keys as today (current forwarder.py:860-914). `session.usage.updated {cost, tokens}` is the session's cumulative total read back from the session row (core/src/session/projector.ts:75-103), so it overrides the per-step sum when seen; the latest step's `input + cache.read + cache.write` still drives `context_tokens`. `session.step.failed` shares the step-end path (optional cost/tokens, session-event.ts:375-391). v2 token counts are `Schema.Finite` (token-usage.ts), so `_int_or_zero` now accepts floats. Usage is posted for the root session only.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 777); `_on_step_ended` (550-555); `_finish_turn` (426-430); class body end (after line 735); `__init__` end (line 254); module level above `class OpenCodeNativeForwarder` (line 205); import block (17-35)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 721)

**Interfaces:**
- Consumes: Tasks 20-22; `omnigent.llms.context_window.get_model_context_window(model_id)` (unchanged).
- Produces: `_AssistantUsage`, `_UsageTotals` TypedDicts; `_int_or_zero(value) -> int`; `_record_step_usage(message_id, data, model)`; `_post_session_usage()`; handler `_on_usage_updated`; final `_on_step_ended`; `_finish_turn` posts usage.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- usage ------------------------------------------------------------------


async def test_step_ended_posts_session_usage() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _step_ended(
            "msg_a",
            cost=0.012,
            tokens={
                "input": 1000,
                "output": 50,
                "reasoning": 0,
                "cache": {"read": 200, "write": 0},
            },
        )
    )
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.012
    assert usage["cumulative_input_tokens"] == 1000
    assert usage["cumulative_output_tokens"] == 50
    assert usage["cumulative_cache_read_input_tokens"] == 200
    assert usage["context_tokens"] == 1200
    assert usage["model"] == "anthropic/claude-sonnet-4-5"
    assert usage["context_window"] > 0


async def test_usage_updated_overrides_cumulative_totals() -> None:
    """``session.usage.updated`` totals win over the per-step sum (they include compaction)."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _step_ended(
            "msg_a",
            cost=0.01,
            tokens={"input": 100, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
    )
    await fwd.handle_event(
        _event(
            "session.usage.updated",
            cost=0.05,
            tokens={"input": 900, "output": 40, "reasoning": 0, "cache": {"read": 30, "write": 0}},
        )
    )
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.05
    assert usage["cumulative_input_tokens"] == 900
    assert usage["cumulative_output_tokens"] == 40
    assert usage["cumulative_cache_read_input_tokens"] == 30
    assert usage["context_tokens"] == 100  # latest step, not the totals


async def test_usage_dedupes_identical_posts() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    ended = _step_ended(
        "msg_a",
        cost=0.01,
        tokens={"input": 1, "output": 1, "reasoning": 0, "cache": {"read": 0, "write": 0}},
    )
    await fwd.handle_event(ended)
    await fwd.handle_event(ended)
    assert len(_datas(server.posts, "external_session_usage")) == 1


async def test_step_failed_flushes_text_and_records_usage() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="partial answer")
    )
    await fwd.handle_event(
        _event(
            "session.step.failed",
            assistantMessageID="msg_a",
            error={"type": "provider.transport", "message": "reset"},
            cost=0.002,
            tokens={"input": 10, "output": 2, "reasoning": 0, "cache": {"read": 0, "write": 0}},
        )
    )
    assert [i["item_data"]["content"][0]["text"] for i in _items(server.posts)] == [
        "partial answer"
    ]
    assert _datas(server.posts, "external_session_usage")[-1]["cumulative_cost_usd"] == 0.002


async def test_fixture_usage_updated_matches_captured_totals() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    raw = events_of_type("session.usage.updated")[-1]["data"]
    await fwd.handle_event(_fixture("session.usage.updated", -1))
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == round(raw["cost"], 6)
    assert usage["cumulative_input_tokens"] == int(raw["tokens"]["input"])
    assert usage["cumulative_output_tokens"] == int(raw["tokens"]["output"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_step_ended_posts_session_usage or test_usage_updated_overrides_cumulative_totals or test_usage_dedupes_identical_posts or test_step_failed_flushes_text_and_records_usage or test_fixture_usage_updated_matches_captured_totals"`

Expected: FAIL with:
  - `test_step_ended_posts_session_usage: IndexError: list index out of range`
  - `test_usage_updated_overrides_cumulative_totals: IndexError: list index out of range`
  - `test_usage_dedupes_identical_posts: AssertionError: assert 0 == 1`
  - `test_step_failed_flushes_text_and_records_usage: AssertionError: assert [] == ['partial answer']`
  - `test_fixture_usage_updated_matches_captured_totals: IndexError: list index out of range`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 777):

```python
    "session.usage.updated": OpenCodeNativeForwarder._on_usage_updated,
    "session.step.failed": OpenCodeNativeForwarder._on_step_ended,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_tool_progress`, which ends at line 735):

```python
    def _record_step_usage(
        self, message_id: str, data: Mapping[str, Any], model: str | None
    ) -> None:
        """Cache one assistant message's ``cost`` (USD) + ``tokens`` + model."""
        tokens = data.get("tokens")
        cost = data.get("cost")
        if not isinstance(tokens, Mapping) and not isinstance(cost, (int, float)):
            return
        self._usage_by_message[message_id] = {
            "cost": float(cost) if isinstance(cost, (int, float)) else 0.0,
            "tokens": {key: value for key, value in tokens.items() if isinstance(key, str)}
            if isinstance(tokens, Mapping)
            else {},
            "model": model,
            "model_id": model.split("/", 1)[1] if model else None,
        }

    async def _on_usage_updated(self, event: OpenCodeEvent) -> None:
        """Handle ``session.usage.updated`` — authoritative cumulative totals."""
        turn = await self._active_turn(event)
        if turn is None or not self._is_root(turn):
            return
        tokens = event.data.get("tokens")
        cost = event.data.get("cost")
        if not isinstance(tokens, Mapping) or not isinstance(cost, (int, float)):
            return
        self._session_totals = {
            "cost": float(cost),
            "tokens": {key: value for key, value in tokens.items() if isinstance(key, str)},
        }
        await self._post_session_usage()

    async def _post_session_usage(self) -> None:
        """Post cumulative cost/tokens + context occupancy as ``external_session_usage``.

        Cumulative fields come from ``session.usage.updated`` when seen, else
        the sum of per-step usage; the latest step's input + cache tokens drive
        the context ring. Deduped so repeated edges don't spam identical posts.
        """
        if not self._usage_by_message and self._session_totals is None:
            return
        cum_cost = 0.0
        cum_in = cum_out = cum_cache = 0
        latest: _AssistantUsage | None = None
        for entry in self._usage_by_message.values():
            cum_cost += entry["cost"]
            tokens = entry["tokens"]
            cum_in += _int_or_zero(tokens.get("input"))
            cum_out += _int_or_zero(tokens.get("output"))
            cache = tokens.get("cache")
            if isinstance(cache, Mapping):
                cum_cache += _int_or_zero(cache.get("read"))
            latest = entry
        if self._session_totals is not None:
            totals = self._session_totals["tokens"]
            cum_cost = self._session_totals["cost"]
            cum_in = _int_or_zero(totals.get("input"))
            cum_out = _int_or_zero(totals.get("output"))
            totals_cache = totals.get("cache")
            cum_cache = (
                _int_or_zero(totals_cache.get("read")) if isinstance(totals_cache, Mapping) else 0
            )
        data: _JsonObject = {
            "cumulative_cost_usd": round(cum_cost, 6),
            "cumulative_input_tokens": cum_in,
            "cumulative_output_tokens": cum_out,
            "cumulative_cache_read_input_tokens": cum_cache,
        }
        if latest is not None:
            latest_tokens = latest["tokens"]
            raw_cache = latest_tokens.get("cache")
            latest_cache = raw_cache if isinstance(raw_cache, Mapping) else {}
            ctx = (
                _int_or_zero(latest_tokens.get("input"))
                + _int_or_zero(latest_cache.get("read"))
                + _int_or_zero(latest_cache.get("write"))
            )
            if ctx > 0:
                data["context_tokens"] = ctx
            model_id = latest["model_id"]
            if model_id:
                try:
                    from omnigent.llms.context_window import get_model_context_window

                    data["context_window"] = get_model_context_window(model_id)
                except Exception:  # noqa: BLE001 - context window is best effort.
                    pass
            model = latest["model"]
            if model:
                data["model"] = model
        signature = tuple(sorted(data.items()))
        if signature == self._last_usage_signature:
            return
        self._last_usage_signature = signature
        await self._post_event(_EXTERNAL_SESSION_USAGE, data)
```

Replace `_on_step_ended` (lines 550-555) with:

```python
    async def _on_step_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.ended`` / ``.failed`` — flush text, record usage."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        await self._flush_pending_text(turn)
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None or not self._is_root(turn):
            return
        self._record_step_usage(message_id, event.data, turn.step_model)
        await self._post_session_usage()
```

Replace `_finish_turn` (lines 426-430) with:

```python
    async def _finish_turn(self, turn: _SessionTurn) -> None:
        """End an active turn as idle; a second terminal signal is a no-op."""
        if not turn.turn_active:
            return
        await self._post_session_usage()
        await self._end_turn(turn)
```

Append to the end of `OpenCodeNativeForwarder.__init__` (after line 254):

```python
        # assistantMessageID -> step usage (root session only).
        self._usage_by_message: dict[str, _AssistantUsage] = {}
        # Authoritative session totals from ``session.usage.updated``.
        self._session_totals: _UsageTotals | None = None
        self._last_usage_signature: tuple[tuple[str, object], ...] | None = None
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 205):

```python
class _AssistantUsage(TypedDict):
    cost: float
    tokens: _JsonObject
    model: str | None
    model_id: str | None


class _UsageTotals(TypedDict):
    cost: float
    tokens: _JsonObject


def _int_or_zero(value: object) -> int:
    """Coerce an OpenCode token count (int or float) to a non-negative int."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and value >= 0:
        return int(value)
    return 0
```

Replace the import block (lines 17-35) with:

```python
import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.bridge import update_active_message_id
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import PolicyDecision
from omnigent.util.json_types import JsonObject as _JsonObject
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_step_ended_posts_session_usage or test_usage_updated_overrides_cumulative_totals or test_usage_dedupes_identical_posts or test_step_failed_flushes_text_and_records_usage or test_fixture_usage_updated_matches_captured_totals"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `41 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): post v2 step and session usage"
```

### Task 30: Model tracking: session.step.started model + session.model.selected -> external_model_change

Spec rows `session.step.started {agent, model}` -> record model, `external_model_change` if changed, and `session.model.selected` -> `external_model_change`. `Model.Ref` is `{id, providerID, variant?}` (packages/schema/src/model.ts:131-135); Omnigent's model string is `provider/id`. The first model seen on a step is only recorded (it is the model the session already runs, not a switch); `session.model.selected {model, previous?}` (session-event.ts:83-91) always mirrors unless unchanged. Root session only.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 907); `_on_step_started` (485-497); class body end (after line 863); `__init__` end (line 280)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 824)

**Interfaces:**
- Consumes: Tasks 21, 26.
- Produces: `_observe_model(model, *, explicit)`; handler `_on_model_selected`; `_on_step_started` observes the step model.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- model ------------------------------------------------------------------


async def test_first_step_model_is_recorded_not_mirrored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    assert "external_model_change" not in _types(server.posts)


async def test_step_model_change_is_mirrored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_step_started("msg_2", model={"id": "gpt-5", "providerID": "openai"}))
    assert _datas(server.posts, "external_model_change") == [{"model": "openai/gpt-5"}]


async def test_model_selected_mirrors_and_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    selected = _event(
        "session.model.selected", model={"id": "claude-opus-4", "providerID": "anthropic"}
    )
    await fwd.handle_event(selected)
    await fwd.handle_event(selected)
    assert _datas(server.posts, "external_model_change") == [{"model": "anthropic/claude-opus-4"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_first_step_model_is_recorded_not_mirrored or test_step_model_change_is_mirrored or test_model_selected_mirrors_and_dedupes"`

Expected: FAIL with:
  - `test_step_model_change_is_mirrored: AssertionError: assert [] == [{'model': 'openai/gpt-5'}]`
  - `test_model_selected_mirrors_and_dedupes: AssertionError: assert [] == [{'model': 'a...aude-opus-4'}]`
  Already passing (guards that the new code must keep true): `test_first_step_model_is_recorded_not_mirrored`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 907):

```python
    "session.model.selected": OpenCodeNativeForwarder._on_model_selected,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_post_session_usage`, which ends at line 863):

```python
    async def _observe_model(self, model: str | None, *, explicit: bool) -> None:
        """Mirror a model change to Omnigent (``external_model_change``), deduped.

        The first model seen on a step is only recorded: it is the model the
        session already runs, not a switch. An explicit
        ``session.model.selected`` always mirrors.
        """
        if model is None or model == self._last_model:
            return
        previous = self._last_model
        self._last_model = model
        if previous is None and not explicit:
            return
        await self._post_event(_EXTERNAL_MODEL_CHANGE, {"model": model})

    async def _on_model_selected(self, event: OpenCodeEvent) -> None:
        """Handle ``session.model.selected`` — a TUI ``/model`` or API switch."""
        turn = await self._active_turn(event)
        if turn is None or not self._is_root(turn):
            return
        await self._observe_model(_model_ref(event.data.get("model")), explicit=True)
```

Replace `_on_step_started` (lines 485-497) with:

```python
    async def _on_step_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.started`` — record the assistant id and model."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None:
            return
        turn.assistant_message_id = message_id
        turn.step_model = _model_ref(event.data.get("model"))
        if self._is_root(turn):
            if self._bridge_dir is not None:
                update_active_message_id(self._bridge_dir, message_id, status="busy")
            await self._observe_model(turn.step_model, explicit=False)
        await self._begin_turn_if_needed(turn)
```

Append to the end of `OpenCodeNativeForwarder.__init__` (after line 280):

```python
        # Last model mirrored to Omnigent (``provider/id``), to dedupe switches.
        self._last_model: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_first_step_model_is_recorded_not_mirrored or test_step_model_change_is_mirrored or test_model_selected_mirrors_and_dedupes"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `44 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 model switches"
```

### Task 31: Failures: session.execution.failed with re-auth detection

Spec row `session.execution.failed {error}` -> `failed` status; auth-shaped errors keep the re-auth hint. The v2 error is `Session.StructuredError {type, message, status?}` (packages/schema/src/session-error.ts). The v1 detection (`ProviderAuthError`, or `APIError` with status 401/403 — current forwarder.py:936-941) maps to v2 `type == "provider.auth"` (core/src/session/to-session-error.ts:14-15,65) or `status in {401, 403}`; the v1 `MessageAbortedError` idle path maps to `type == "aborted"` (to-session-error.ts:54).

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 934); class body end (after line 889)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 853)

**Interfaces:**
- Consumes: Tasks 21, 26.
- Produces: handler `_on_execution_failed`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- execution failure ------------------------------------------------------


async def test_execution_failed_auth_posts_failed_with_reauth() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.auth", "message": "invalid api key", "status": 401},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "failed"
    assert status["reauth_required"] is True
    assert "invalid api key" in status["output"]
    assert fwd_mod._OPENCODE_REAUTH_HINT in status["output"]


async def test_execution_failed_http_403_is_auth_shaped() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.invalid-request", "message": "forbidden", "status": 403},
        )
    )
    assert _status_edges(server.posts)[-1]["reauth_required"] is True


async def test_execution_failed_generic_posts_failed_without_reauth() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "provider.internal", "message": "upstream boom", "status": 500},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "failed"
    assert status["output"] == "upstream boom"
    assert "reauth_required" not in status


async def test_execution_failed_aborted_takes_idle_path() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.execution.failed",
            error={"type": "aborted", "message": "Session interrupted by user"},
        )
    )
    status = _status_edges(server.posts)[-1]
    assert status["status"] == "idle"
    assert "output" not in status
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_execution_failed_auth_posts_failed_with_reauth or test_execution_failed_http_403_is_auth_shaped or test_execution_failed_generic_posts_failed_without_reauth or test_execution_failed_aborted_takes_idle_path"`

Expected: FAIL with:
  - `test_execution_failed_auth_posts_failed_with_reauth: IndexError: list index out of range`
  - `test_execution_failed_http_403_is_auth_shaped: IndexError: list index out of range`
  - `test_execution_failed_generic_posts_failed_without_reauth: IndexError: list index out of range`
  - `test_execution_failed_aborted_takes_idle_path: IndexError: list index out of range`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 934):

```python
    "session.execution.failed": OpenCodeNativeForwarder._on_execution_failed,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_model_selected`, which ends at line 889):

```python
    async def _on_execution_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.failed`` — failed (or re-auth) status edge.

        ``error`` is a ``Session.StructuredError`` ``{type, message, status?}``.
        ``aborted`` is a user interrupt and takes the idle path; ``provider.auth``
        or an HTTP 401/403 status carries the re-auth hint.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        error = event.data.get("error")
        _logger.warning("OpenCode session error for session=%s: %s", self._session_id, error)
        error_map: Mapping[str, Any] = error if isinstance(error, Mapping) else {}
        error_type = error_map.get("type")
        if error_type == _ABORTED_ERROR_TYPE:
            await self._end_turn(turn)
            return
        message = error_map.get("message")
        if not isinstance(message, str) or not message.strip():
            message = "OpenCode session ended with an error."
        is_auth = error_type == _AUTH_ERROR_TYPE or error_map.get("status") in _AUTH_STATUS_CODES
        extra: _JsonObject = {"output": message.strip()}
        if is_auth:
            extra["output"] = f"{message.strip()}\n\n{_OPENCODE_REAUTH_HINT}"
            extra["reauth_required"] = True
        if self._is_root(turn):
            await self._post_session_usage()
        await self._end_turn(turn, status=_STATUS_FAILED, extra=extra)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_execution_failed_auth_posts_failed_with_reauth or test_execution_failed_http_403_is_auth_shaped or test_execution_failed_generic_posts_failed_without_reauth or test_execution_failed_aborted_takes_idle_path"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `48 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): surface v2 execution failures with re-auth hint"
```

### Task 32: Interrupts: session.execution.interrupted + partial streamed text

Spec row `session.execution.interrupted {reason}` -> `idle` (user) or `external_session_interrupted`. `reason` is one of `user|shutdown|superseded|inactivity` (session-event.ts:256-260; `user` comes from `UserInterruptedError`, core/src/session/execution.ts:51-55). Non-user reasons post `external_session_interrupted {response_id}` (server: omnigent/server/routes/sessions/routes_events.py:1461-1469; codex-native equivalent at codex_native/forwarder.py:7128) and then idle. Text that streamed without a `session.text.ended` is persisted under its stream id so the live preview is retired instead of left dangling.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 964); `_end_turn` (430-452); class body end (after line 918)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 913)

**Interfaces:**
- Consumes: Tasks 21-22.
- Produces: `_persist_partial_text(turn)`; handler `_on_execution_interrupted`; final `_end_turn`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- interruption -----------------------------------------------------------


async def test_user_interrupt_posts_idle_only() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.interrupted", reason="user"))
    assert "external_session_interrupted" not in _types(server.posts)
    assert _status_edges(server.posts)[-1] == {"status": "idle", "response_id": "msg_1"}


async def test_shutdown_interrupt_posts_interrupted_then_idle() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("session.execution.started"))
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_event("session.execution.interrupted", reason="shutdown"))
    types = _types(server.posts)
    assert _datas(server.posts, "external_session_interrupted") == [{"response_id": "msg_1"}]
    assert types.index("external_session_interrupted") < len(types) - 1
    assert _status_edges(server.posts)[-1]["status"] == "idle"


async def test_interrupt_persists_partial_streamed_text() -> None:
    """Streamed text without ``text.ended`` is kept and retires its preview."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event("session.text.delta", assistantMessageID="msg_1", ordinal=0, delta="Half an")
    )
    await fwd.handle_event(_event("session.execution.interrupted", reason="user"))
    item = _items(server.posts)[-1]
    assert item["item_data"]["content"][0]["text"] == "Half an"
    assert item["message_id"] == "opencode:msg_1:text:0"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_user_interrupt_posts_idle_only or test_shutdown_interrupt_posts_interrupted_then_idle or test_interrupt_persists_partial_streamed_text"`

Expected: FAIL with:
  - `test_user_interrupt_posts_idle_only: AssertionError: assert {'response_id...s': 'running'} == {'response_id...atus': 'idle'}`
  - `test_shutdown_interrupt_posts_interrupted_then_idle: AssertionError: assert [] == [{'response_id': 'msg_1'}]`
  - `test_interrupt_persists_partial_streamed_text: IndexError: list index out of range`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 964):

```python
    "session.execution.interrupted": OpenCodeNativeForwarder._on_execution_interrupted,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_execution_failed`, which ends at line 918):

```python
    async def _persist_partial_text(self, turn: _SessionTurn) -> None:
        """Persist streamed text whose ``session.text.ended`` never arrived."""
        for (message_id, ordinal), text in list(turn.streamed_text.items()):
            turn.streamed_text.pop((message_id, ordinal), None)
            if not text:
                continue
            if not self.state.mark(self._key("text-final", message_id, str(ordinal))):
                continue
            await self._post_assistant_text(
                turn,
                text,
                message_id=message_id,
                stream_id=self._stream_id(message_id, "text", ordinal),
            )

    async def _on_execution_interrupted(self, event: OpenCodeEvent) -> None:
        """Handle ``session.execution.interrupted {reason}``.

        A ``user`` interrupt (web Stop or TUI Esc) is an ordinary idle; any
        other reason (``shutdown``/``superseded``/``inactivity``) also posts
        ``external_session_interrupted`` so the web marks the turn cut short.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        if event.data.get("reason") != "user":
            await self._post_event(
                _EXTERNAL_SESSION_INTERRUPTED,
                {"response_id": self._response_id(turn, turn.assistant_message_id)},
                conversation_id=turn.conversation_id,
            )
        await self._end_turn(turn)
```

Replace `_end_turn` (lines 430-452) with:

```python
    async def _end_turn(
        self,
        turn: _SessionTurn,
        *,
        status: str = _STATUS_IDLE,
        extra: _JsonMapping | None = None,
    ) -> None:
        """Post the terminal edge stamped with the turn's id and reset per-turn state."""
        await self._flush_pending_text(turn)
        await self._persist_partial_text(turn)
        turn.turn_active = False
        turn.delta_index.clear()
        turn.reasoning_started.clear()
        turn.tool_output.clear()
        turn.retry_label = None
        terminal_id = turn.running_response_id or turn.assistant_message_id
        merged: _JsonObject = {"response_id": terminal_id or turn.session_id}
        if extra:
            merged.update(extra)
        if self._bridge_dir is not None and self._is_root(turn):
            update_active_message_id(self._bridge_dir, None, status="idle")
        await self._post_status(turn, status, extra=merged)
        turn.assistant_message_id = None
        turn.running_response_id = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_user_interrupt_posts_idle_only or test_shutdown_interrupt_posts_interrupted_then_idle or test_interrupt_persists_partial_streamed_text"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `51 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): handle v2 interrupts and keep partial streamed text"
```

### Task 33: Retries: session.retry.scheduled / session.status retry -> transient status

Spec row `session.retry.scheduled` -> transient status forward. v2 reports a retry twice: durable `session.retry.scheduled {assistantMessageID, attempt, at, error}` (session-event.ts:572-582) and ephemeral `session.status {type: "retry", attempt, message, next}` (session-status-event.ts:13-28). Both become one `external_session_status running` edge carrying `blocked_on` (the server's "why is this running session parked" label, omnigent/server/routes/sessions/routes_events.py:1551-1559; statuses limited to idle/running/waiting/failed by _sessions/common.py:117-119), deduped by label. The next `session.step.started` clears the label with a plain `running` edge.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 999); `_on_session_status` (474-486); `_on_step_started` (488-502); class body end (after line 952)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 952)

**Interfaces:**
- Consumes: Tasks 21, 27.
- Produces: `_post_retry_status(turn, attempt, message)`; handler `_on_retry_scheduled`; final `_on_session_status` and `_on_step_started`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- retry ------------------------------------------------------------------


async def test_retry_scheduled_posts_running_with_blocked_on() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.retry.scheduled",
            assistantMessageID="msg_1",
            attempt=2,
            at=1700000000000,
            error={"type": "provider.rate-limit", "message": "429 slow down"},
        )
    )
    edge = _status_edges(server.posts)[-1]
    assert edge == {
        "status": "running",
        "response_id": "msg_1",
        "blocked_on": "Retrying (attempt 2): 429 slow down",
    }


async def test_status_retry_dedupes_with_retry_scheduled() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.retry.scheduled",
            assistantMessageID="msg_1",
            attempt=1,
            at=1,
            error={"type": "provider.rate-limit", "message": "busy"},
        )
    )
    await fwd.handle_event(
        _event(
            "session.status", status={"type": "retry", "attempt": 1, "message": "busy", "next": 1}
        )
    )
    blocked = [e for e in _status_edges(server.posts) if "blocked_on" in e]
    assert len(blocked) == 1


async def test_next_step_after_retry_clears_blocked_on() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.status", status={"type": "retry", "attempt": 1, "message": "busy", "next": 1}
        )
    )
    await fwd.handle_event(_step_started("msg_2"))
    assert _status_edges(server.posts)[-1] == {"status": "running", "response_id": "msg_1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_retry_scheduled_posts_running_with_blocked_on or test_status_retry_dedupes_with_retry_scheduled or test_next_step_after_retry_clears_blocked_on"`

Expected: FAIL with:
  - `test_retry_scheduled_posts_running_with_blocked_on: AssertionError: assert {'response_id...s': 'running'} == {'blocked_on'...s': 'running'}`
  - `test_status_retry_dedupes_with_retry_scheduled: assert 0 == 1`
  Already passing (guards that the new code must keep true): `test_next_step_after_retry_clears_blocked_on`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 999):

```python
    "session.retry.scheduled": OpenCodeNativeForwarder._on_retry_scheduled,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_execution_interrupted`, which ends at line 952):

```python
    async def _post_retry_status(
        self, turn: _SessionTurn, attempt: int | None, message: str | None
    ) -> None:
        """Show a provider retry on the running edge (``blocked_on``), deduped."""
        label = f"Retrying (attempt {attempt})" if attempt else "Retrying"
        if message:
            label = f"{label}: {message}"
        label = label[:_MAX_BLOCKED_ON_CHARS]
        if label == turn.retry_label:
            return
        turn.retry_label = label
        await self._begin_turn_if_needed(turn)
        await self._post_status(
            turn,
            _STATUS_RUNNING,
            extra={
                "response_id": self._response_id(turn, turn.assistant_message_id),
                "blocked_on": label,
            },
        )

    async def _on_retry_scheduled(self, event: OpenCodeEvent) -> None:
        """Handle ``session.retry.scheduled {attempt, at, error}``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        error = event.data.get("error")
        message = error.get("message") if isinstance(error, Mapping) else None
        await self._post_retry_status(
            turn,
            _int_field(event.data, "attempt"),
            message if isinstance(message, str) else None,
        )
```

Replace `_on_step_started` (lines 488-502) with:

```python
    async def _on_step_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.step.started`` — record the assistant id and model."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        message_id = _str_field(event.data, "assistantMessageID")
        if message_id is None:
            return
        turn.assistant_message_id = message_id
        turn.step_model = _model_ref(event.data.get("model"))
        if self._is_root(turn):
            if self._bridge_dir is not None:
                update_active_message_id(self._bridge_dir, message_id, status="busy")
            await self._observe_model(turn.step_model, explicit=False)
        await self._begin_turn_if_needed(turn)
        if turn.retry_label is not None:
            turn.retry_label = None
            await self._post_status(
                turn, _STATUS_RUNNING, extra={"response_id": self._response_id(turn, message_id)}
            )
```

Replace `_on_session_status` (lines 474-486) with:

```python
    async def _on_session_status(self, event: OpenCodeEvent) -> None:
        """Handle ``session.status {busy|idle|retry}``."""
        turn = await self._active_turn(event)
        if turn is None:
            return
        status = event.data.get("status")
        if not isinstance(status, Mapping):
            return
        status_type = status.get("type")
        if status_type == "busy":
            await self._begin_turn_if_needed(turn)
        elif status_type == "idle":
            await self._finish_turn(turn)
        elif status_type == "retry":
            message = status.get("message")
            await self._post_retry_status(
                turn,
                _int_field(status, "attempt"),
                message if isinstance(message, str) else None,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_retry_scheduled_posts_running_with_blocked_on or test_status_retry_dedupes_with_retry_scheduled or test_next_step_after_retry_clears_blocked_on"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `54 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): forward v2 provider retries as a transient status"
```

### Task 34: Compaction: session.compaction.started / .ended / .failed

Spec row `session.compaction.started` / `.ended` / `.failed` -> `external_compaction_status` (`in_progress` / `completed` / `failed`; the server accepts exactly these, _sessions/common.py:128-130). Replaces the v1 `session.next.compaction.*` / `session.compacted` handlers (current forwarder.py:944-964, 1295-1297). v2 `session.compacted` still exists but is not on the public stream (packages/schema/src/event-manifest.ts `ServerDefinitions` omits `SessionCompactionEvent`).

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1046); class body end (after line 998)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1011)

**Interfaces:**
- Consumes: Task 23.
- Produces: handlers `_on_compaction_started`, `_on_compaction_ended`, `_on_compaction_failed`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- compaction -------------------------------------------------------------


async def test_fixture_compaction_cycle_brackets_status() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    await fwd.handle_event(_fixture("session.compaction.started"))
    await fwd.handle_event(_fixture("session.compaction.ended"))
    assert _datas(server.posts, "external_compaction_status") == [
        {"status": "in_progress"},
        {"status": "completed"},
    ]


async def test_compaction_failed_posts_failed() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(
        _event(
            "session.compaction.failed",
            reason="auto",
            error={"type": "provider.transport", "message": "reset"},
        )
    )
    assert _datas(server.posts, "external_compaction_status") == [{"status": "failed"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_compaction_cycle_brackets_status or test_compaction_failed_posts_failed"`

Expected: FAIL with:
  - `test_fixture_compaction_cycle_brackets_status: AssertionError: assert [] == [{'status': '... 'completed'}]`
  - `test_compaction_failed_posts_failed: AssertionError: assert [] == [{'status': 'failed'}]`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1046):

```python
    "session.compaction.started": OpenCodeNativeForwarder._on_compaction_started,
    "session.compaction.ended": OpenCodeNativeForwarder._on_compaction_ended,
    "session.compaction.failed": OpenCodeNativeForwarder._on_compaction_failed,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_retry_scheduled`, which ends at line 998):

```python
    async def _on_compaction_started(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.started`` (auto or manual)."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "in_progress"},
                conversation_id=turn.conversation_id,
            )

    async def _on_compaction_ended(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.ended``."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "completed"},
                conversation_id=turn.conversation_id,
            )

    async def _on_compaction_failed(self, event: OpenCodeEvent) -> None:
        """Handle ``session.compaction.failed``."""
        turn = await self._active_turn(event)
        if turn is not None:
            await self._post_event(
                _EXTERNAL_COMPACTION_STATUS,
                {"status": "failed"},
                conversation_id=turn.conversation_id,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_compaction_cycle_brackets_status or test_compaction_failed_posts_failed"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `56 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 compaction lifecycle"
```

### Task 35: User prompts: session.inbox.enqueued / .delivered / .cancelled

Gap in the spec table: the forwarder is the sole transcript source for native-server harnesses (current forwarder.py:629-636 posts the user message), but v2 has no user-message event. A prompt is admitted as `session.inbox.enqueued {inboxID, item: {type: "user", payload: {text, files?}, delivery}}` (packages/schema/src/session-inbox.ts:14-59; core/src/session/inbox.ts:177) and joins the transcript on `session.inbox.delivered {inboxID}` whose id becomes the user message id (core/src/session/projector.ts:609-639). The payload is held from enqueue and posted on delivery so a queued prompt appears when it runs. Image attachments (`Prompt.FileAttachment {data, mime, name?}`, packages/schema/src/prompt.ts:26-35) become `input_image` data URIs; other files become an `[attachment: name]` note.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1079); class body end (after line 1028); `__init__` end (line 282); module level above `class OpenCodeNativeForwarder` (line 226)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1038)

**Interfaces:**
- Consumes: Task 23.
- Produces: `_user_file_block(file) -> JsonObject`; `_post_message_content(turn, role, content, *, response_id)`; `_post_user_payload(turn, message_id, payload)`; handlers `_on_inbox_enqueued`, `_on_inbox_delivered`, `_on_inbox_cancelled`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- user prompts -----------------------------------------------------------


def _enqueued(inbox_id: str, text: str, **payload: Any) -> OpenCodeEvent:
    return _event(
        "session.inbox.enqueued",
        inboxID=inbox_id,
        item={"type": "user", "payload": {"text": text, **payload}, "delivery": "steer"},
    )


async def test_user_prompt_posts_on_delivery_before_assistant() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_enqueued("msg_u", "my prompt"))
    assert _items(server.posts) == []
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    await fwd.handle_event(_step_started("msg_a"))
    await fwd.handle_event(
        _event("session.text.ended", assistantMessageID="msg_a", ordinal=0, text="hello")
    )
    await fwd.handle_event(_step_ended("msg_a"))
    items = _items(server.posts)
    assert [i["item_data"]["role"] for i in items] == ["user", "assistant"]
    assert items[0]["item_data"]["content"] == [{"type": "input_text", "text": "my prompt"}]
    assert items[0]["response_id"] == "msg_u"


async def test_user_prompt_image_attachment_becomes_input_image() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    image = {"data": "AAAA", "mime": "image/png", "source": {"type": "inline"}, "name": "a.png"}
    await fwd.handle_event(_enqueued("msg_u", "see image", files=[image]))
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    content = _items(server.posts)[0]["item_data"]["content"]
    assert content == [
        {"type": "input_text", "text": "see image"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
    ]


async def test_cancelled_inbox_prompt_is_never_posted() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_enqueued("msg_u", "never mind"))
    await fwd.handle_event(_event("session.inbox.cancelled", inboxID="msg_u"))
    await fwd.handle_event(_event("session.inbox.delivered", inboxID="msg_u"))
    assert _items(server.posts) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_user_prompt_posts_on_delivery_before_assistant or test_user_prompt_image_attachment_becomes_input_image or test_cancelled_inbox_prompt_is_never_posted"`

Expected: FAIL with:
  - `test_user_prompt_posts_on_delivery_before_assistant: AssertionError: assert ['assistant'] == ['user', 'assistant']`
  - `test_user_prompt_image_attachment_becomes_input_image: IndexError: list index out of range`
  Already passing (guards that the new code must keep true): `test_cancelled_inbox_prompt_is_never_posted`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1079):

```python
    "session.inbox.enqueued": OpenCodeNativeForwarder._on_inbox_enqueued,
    "session.inbox.delivered": OpenCodeNativeForwarder._on_inbox_delivered,
    "session.inbox.cancelled": OpenCodeNativeForwarder._on_inbox_cancelled,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_compaction_failed`, which ends at line 1028):

```python
    async def _post_message_content(
        self,
        turn: _SessionTurn,
        role: str,
        content: list[_JsonObject],
        *,
        response_id: str,
    ) -> None:
        """Persist a message item with arbitrary content blocks."""
        item_data: _JsonObject = {"role": role, "content": content}
        if role == "assistant":
            item_data["agent"] = _AGENT_NAME
        await self._post_event(
            _EXTERNAL_ITEM,
            {"item_type": "message", "item_data": item_data, "response_id": response_id},
            conversation_id=turn.conversation_id,
        )

    async def _post_user_payload(
        self, turn: _SessionTurn, message_id: str, payload: Mapping[str, Any]
    ) -> None:
        """Post a user prompt (text + attachments) once per message id."""
        content: list[_JsonObject] = []
        text = payload.get("text")
        if isinstance(text, str) and text:
            content.append({"type": "input_text", "text": text})
        files = payload.get("files")
        for file in files if isinstance(files, list) else []:
            if isinstance(file, Mapping):
                content.append(_user_file_block(file))
        if not content or not self.state.mark(self._key("user", message_id)):
            return
        await self._post_message_content(turn, "user", content, response_id=message_id)

    async def _on_inbox_enqueued(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.enqueued`` — hold a user prompt until delivered."""
        inbox_id = _str_field(event.data, "inboxID")
        item = event.data.get("item")
        if inbox_id is None or not isinstance(item, Mapping) or item.get("type") != "user":
            return
        payload = item.get("payload")
        if isinstance(payload, Mapping):
            self._inbox_items[inbox_id] = dict(payload)

    async def _on_inbox_delivered(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.delivered`` — the prompt joined the transcript.

        The delivered inbox id is the user message id, so the prompt posts in
        transcript order (a queued prompt appears when it runs, not when sent).
        """
        turn = await self._active_turn(event)
        inbox_id = _str_field(event.data, "inboxID")
        if turn is None or inbox_id is None:
            return
        payload = self._inbox_items.pop(inbox_id, None)
        if payload is not None:
            await self._post_user_payload(turn, inbox_id, payload)

    async def _on_inbox_cancelled(self, event: OpenCodeEvent) -> None:
        """Handle ``session.inbox.cancelled`` — drop a queued prompt."""
        inbox_id = _str_field(event.data, "inboxID")
        if inbox_id is not None:
            self._inbox_items.pop(inbox_id, None)
```

Append to the end of `OpenCodeNativeForwarder.__init__` (after line 282):

```python
        # inboxID -> user prompt payload, posted when the prompt is delivered.
        self._inbox_items: dict[str, _JsonObject] = {}
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 226):

```python
def _user_file_block(file: Mapping[str, Any]) -> _JsonObject:
    """Render a v2 ``Prompt.FileAttachment`` as a user content block."""
    mime = file.get("mime")
    data = file.get("data")
    if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str) and data:
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}"}
    name = file.get("name")
    label = name if isinstance(name, str) and name else (mime or "attachment")
    return {"type": "input_text", "text": f"[attachment: {label}]"}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_user_prompt_posts_on_delivery_before_assistant or test_user_prompt_image_attachment_becomes_input_image or test_cancelled_inbox_prompt_is_never_posted"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `59 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 user prompts from the session inbox"
```

### Task 36: Permissions: permission.asked -> policy -> reply_permission(once|reject)

Spec row `permission.asked` -> TOOL_CALL policy evaluation -> `reply_permission(once|reject)`; never `always`. `permission.asked` data is `Permission.Request {id, sessionID, action, resources: string[], save?, metadata?, source?: {type: "tool", messageID, id}, message?}` (packages/schema/src/permission.ts:198-217). Evaluation now runs in a background task keyed by request id: the evaluator can park on a human approval card, and running it inline would stall this session's event loop (including the `permission.replied` that Task 37 uses to withdraw the card). The reply goes to the request's own `sessionID` (a subagent child's permission is answered on the child). Per Stage 3, the reply carries no message argument (a v2 reject message tells the model to continue) and `reply_body` is gone.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1159); class body end (after line 1105); import block (17-35)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1088)

**Interfaces:**
- Consumes: Tasks 20-21; Stage 3 `parse_permission_request(data) -> OpenCodePermissionRequest | None` (fields `request_id`, `session_id`, `action`, `resources`, `metadata`, `source`), `normalize_for_policy(request, *, omnigent_session_id, workspace) -> dict` (keys `harness`, `action`, `omnigent_session_id`, plus Stage 3's `arguments`), `map_verdict_to_decision`, `decision_to_reply` (never returns `"always"`); Stage 1 `OpenCodeClient.reply_permission(session_id, request_id, decision) -> bool`.
- Produces: handler `_on_permission_asked`; `_handle_permission(request)`; `_resolve_permission(*, request_dict) -> PolicyDecision`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- permissions ------------------------------------------------------------

# Add ``from omnigent.harnesses.opencode_native.client import OpenCodeClientError``
# to the test module's imports if it is not already there.


def _asked(request_id: str, action: str = "shell", **data: Any) -> OpenCodeEvent:
    data.setdefault("resources", ["ls"])
    data.setdefault("metadata", {"command": "ls"})
    return _event("permission.asked", id=request_id, action=action, **data)


async def test_fixture_permission_rejects_when_no_policy_wired() -> None:
    """No evaluator fails closed: the captured request is rejected."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    asked = _fixture("permission.asked")
    await fwd.handle_event(asked)
    await _drain(fwd)
    assert opencode.permission_replies == [(_FIX_SESSION, asked.data["id"], "reject")]


async def test_permission_asked_rejects_when_policy_denies() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def deny(_normalized: Any) -> dict[str, Any]:
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=deny)
    await fwd.handle_event(_asked("per_2"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_2", "reject")]


async def test_permission_asked_allows_only_on_explicit_policy_allow() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow)
    await fwd.handle_event(_asked("per_a"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_a", "once")]


async def test_permission_asked_allow_always_still_replies_once() -> None:
    """Replying ``always`` would make OpenCode stop asking and bypass live policy."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def allow_always(_normalized: Any) -> dict[str, Any]:
        return {"decision": "allow_always"}

    fwd = _forwarder(server, opencode, policy_evaluator=allow_always)
    await fwd.handle_event(_asked("per_aa"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_aa", "once")]


async def test_permission_asked_rejects_when_policy_returns_ask() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def ask(_normalized: Any) -> dict[str, Any]:
        return {"decision": "ask"}

    fwd = _forwarder(server, opencode, policy_evaluator=ask)
    await fwd.handle_event(_asked("per_ask"))
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_ask", "reject")]


async def test_permission_asked_passes_normalized_input_to_evaluator() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    seen: list[Any] = []

    async def capture(normalized: Any) -> dict[str, Any]:
        seen.append(normalized)
        return {"decision": "deny"}

    fwd = _forwarder(server, opencode, policy_evaluator=capture, workspace="/work/repo")
    await fwd.handle_event(_asked("per_n"))
    await _drain(fwd)
    assert len(seen) == 1
    assert seen[0]["harness"] == "opencode-native"
    assert seen[0]["action"] == "shell"
    assert seen[0]["omnigent_session_id"] == "conv_1"


async def test_permission_asked_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    event = _asked("per_3")
    await fwd.handle_event(event)
    await fwd.handle_event(event)
    await _drain(fwd)
    assert len(opencode.permission_replies) == 1


async def test_permission_reply_failure_is_surfaced() -> None:
    """OpenCode blocks the turn until answered, so a failed reply must be visible."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def failing_reply(*_args: Any, **_kwargs: Any) -> bool:
        raise OpenCodeClientError("reply failed: 500")

    opencode.reply_permission = failing_reply  # type: ignore[method-assign]
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(_asked("per_err"))
    await _drain(fwd)
    statuses = _datas(server.posts, "external_session_status")
    assert statuses[-1]["status"] == "running"
    assert statuses[-1]["blocked_on"] == "permission reply failed for per_err"


async def test_permission_evaluation_does_not_block_the_event_loop() -> None:
    """A parked approval must not stall later events for the session."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    release = asyncio.Event()

    async def parked(_normalized: Any) -> dict[str, Any]:
        await release.wait()
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=parked)
    await fwd.handle_event(_asked("per_p"))
    await fwd.handle_event(_event("session.compaction.started", reason="auto", recent=""))
    assert _datas(server.posts, "external_compaction_status") == [{"status": "in_progress"}]
    release.set()
    await _drain(fwd)
    assert opencode.permission_replies == [(_SESSION, "per_p", "once")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_permission_rejects_when_no_policy_wired or test_permission_asked_rejects_when_policy_denies or test_permission_asked_allows_only_on_explicit_policy_allow or test_permission_asked_allow_always_still_replies_once or test_permission_asked_rejects_when_policy_returns_ask or test_permission_asked_passes_normalized_input_to_evaluator or test_permission_asked_dedupes or test_permission_reply_failure_is_surfaced or test_permission_evaluation_does_not_block_the_event_loop"`

Expected: FAIL with:
  - `test_fixture_permission_rejects_when_no_policy_wired: AssertionError: assert [] == [('ses_fixtur...1', 'reject')]`
  - `test_permission_asked_rejects_when_policy_denies: AssertionError: assert [] == [('ses_1', 'per_2', 'reject')]`
  - `test_permission_asked_allows_only_on_explicit_policy_allow: AssertionError: assert [] == [('ses_1', 'per_a', 'once')]`
  - `test_permission_asked_allow_always_still_replies_once: AssertionError: assert [] == [('ses_1', 'per_aa', 'once')]`
  - `test_permission_asked_rejects_when_policy_returns_ask: AssertionError: assert [] == [('ses_1', 'p...k', 'reject')]`
  - `test_permission_asked_passes_normalized_input_to_evaluator: assert 0 == 1`
  - `test_permission_asked_dedupes: assert 0 == 1`
  - `test_permission_reply_failure_is_surfaced: IndexError: list index out of range`
  - `test_permission_evaluation_does_not_block_the_event_loop: AssertionError: assert [] == [('ses_1', 'per_p', 'once')]`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1159):

```python
    "permission.asked": OpenCodeNativeForwarder._on_permission_asked,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_inbox_cancelled`, which ends at line 1105):

```python
    async def _on_permission_asked(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.asked`` — evaluate policy in a background task.

        The evaluator can park on a human approval card, so it never runs
        inline: that would stall this session's event loop, including the
        ``permission.replied`` that withdraws the card when the TUI answers.
        """
        request = parse_permission_request(event.data)
        if request is None:
            return
        if not self.state.mark(self._key("perm", request.request_id)):
            return
        task = asyncio.create_task(self._handle_permission(request))
        self._permission_tasks[request.request_id] = task
        task.add_done_callback(
            lambda _t, rid=request.request_id: self._permission_tasks.pop(rid, None)
        )

    async def _handle_permission(self, request: OpenCodePermissionRequest) -> None:
        """Resolve one permission and reply ``once`` or ``reject`` (never ``always``)."""
        decision = await self._resolve_permission(request_dict=request)
        # ``ask`` means no human resolution was obtained upstream: fail closed.
        reply = decision_to_reply(decision) or "reject"
        # Marked before replying so our own ``permission.replied`` echo is ignored.
        self.state.mark(self._key("perm-replied", request.request_id))
        try:
            # No reply message: in v2 a reject message tells the model to continue.
            await self._opencode.reply_permission(
                request.session_id or self._opencode_session_id, request.request_id, reply
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - surface it: OpenCode stays blocked on the request.
            _logger.warning(
                "OpenCode permission reply failed for request=%s",
                request.request_id,
                exc_info=True,
            )
            turn = self._turns.get(request.session_id or self._opencode_session_id)
            if turn is not None:
                await self._post_status(
                    turn,
                    _STATUS_RUNNING,
                    extra={"blocked_on": f"permission reply failed for {request.request_id}"},
                )

    async def _resolve_permission(
        self, *, request_dict: OpenCodePermissionRequest
    ) -> PolicyDecision:
        """
        Resolve a permission request to a normalized decision.

        :param request_dict: The parsed permission request.
        :returns: The normalized policy decision.
        """
        if self._policy_evaluator is None:
            return self._default_decision
        normalized = normalize_for_policy(
            request_dict,
            omnigent_session_id=self._session_id,
            workspace=self._workspace,
        )
        try:
            verdict = await self._policy_evaluator(normalized)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - policy errors fail closed.
            _logger.warning("OpenCode policy evaluation failed", exc_info=True)
            return "ask"
        if verdict is None:
            return self._default_decision
        return map_verdict_to_decision(verdict)
```

Replace the import block (lines 17-35) with:

```python
import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.bridge import update_active_message_id
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import (
    OpenCodePermissionRequest,
    PolicyDecision,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
)
from omnigent.util.json_types import JsonObject as _JsonObject
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_permission_rejects_when_no_policy_wired or test_permission_asked_rejects_when_policy_denies or test_permission_asked_allows_only_on_explicit_policy_allow or test_permission_asked_allow_always_still_replies_once or test_permission_asked_rejects_when_policy_returns_ask or test_permission_asked_passes_normalized_input_to_evaluator or test_permission_asked_dedupes or test_permission_reply_failure_is_surfaced or test_permission_evaluation_does_not_block_the_event_loop"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `67 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): gate v2 permission.asked through the policy evaluator"
```

### Task 37: Permission replied: first answer wins

Spec row `permission.replied` -> `external_elicitation_resolved` with the TUI first-answer-wins guard. `permission.replied` is `{sessionID, requestID, reply}` (permission.ts:218-225). The forwarder marks `perm-replied:<id>` just before sending its own reply (Task 36), so the echo of its own reply is ignored; any other `permission.replied` means the TUI answered first: cancel the parked evaluation task and post `external_elicitation_resolved {elicitation_id: requestID}` — the same cancel-then-resolve guard the v1 question path used (current forwarder.py:1235-1249).

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1233); class body end (after line 1178)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1201)

**Interfaces:**
- Consumes: Task 36.
- Produces: handler `_on_permission_replied`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- permission replied -----------------------------------------------------


async def test_own_permission_reply_echo_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_asked("per_1"))
    await _drain(fwd)
    await fwd.handle_event(_event("permission.replied", requestID="per_1", reply="reject"))
    assert "external_elicitation_resolved" not in _types(server.posts)


async def test_tui_permission_reply_cancels_parked_evaluation_and_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def parked(_normalized: Any) -> dict[str, Any]:
        await asyncio.Event().wait()
        return {"decision": "allow"}

    fwd = _forwarder(server, opencode, policy_evaluator=parked)
    await fwd.handle_event(_asked("per_t"))
    task = fwd._permission_tasks["per_t"]
    await asyncio.sleep(0)
    await fwd.handle_event(_event("permission.replied", requestID="per_t", reply="once"))
    assert task.cancelled()
    assert opencode.permission_replies == []
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "per_t"}]


async def test_fixture_permission_replied_without_pending_task_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    replied = _fixture("permission.replied")
    await fwd.handle_event(replied)
    assert _datas(server.posts, "external_elicitation_resolved") == [
        {"elicitation_id": replied.data["requestID"]}
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_own_permission_reply_echo_is_ignored or test_tui_permission_reply_cancels_parked_evaluation_and_clears_card or test_fixture_permission_replied_without_pending_task_clears_card"`

Expected: FAIL with:
  - `test_tui_permission_reply_cancels_parked_evaluation_and_clears_card: AssertionError: assert False`
  - `test_fixture_permission_replied_without_pending_task_clears_card: AssertionError: assert [] == [{'elicitation_id': 'per_1'}]`
  Already passing (guards that the new code must keep true): `test_own_permission_reply_echo_is_ignored`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1233):

```python
    "permission.replied": OpenCodeNativeForwarder._on_permission_replied,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_resolve_permission`, which ends at line 1178):

```python
    async def _on_permission_replied(self, event: OpenCodeEvent) -> None:
        """Handle ``permission.replied`` — first answer wins.

        Our own reply is marked before it is sent, so its echo is ignored.
        Otherwise the TUI answered first: cancel the still-parked evaluation
        and clear the web card.
        """
        request_id = _str_field(event.data, "requestID")
        if request_id is None or not self.state.mark(self._key("perm-replied", request_id)):
            return
        task = self._permission_tasks.pop(request_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._post_event(_EXTERNAL_ELICITATION_RESOLVED, {"elicitation_id": request_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_own_permission_reply_echo_is_ignored or test_tui_permission_reply_cancels_parked_evaluation_and_clears_card or test_fixture_permission_replied_without_pending_task_clears_card"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `70 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): withdraw the approval card when the TUI answers first"
```

### Task 38: Form field mapping: v2 Form.Field <-> web ask_user_question

Pure mapping for spec row `form.created`: `string` + options -> single select, `multiselect`, `boolean`, `number`/`integer`, `external` -> text naming the URL. Field shapes from packages/schema/src/form.ts:18-117. The web form (web/src/components/blocks/AskUserQuestionForm.tsx) always renders a free-text row and returns `{question id: label | label[]}`, so option-less `string` and numbers are free text, booleans are a Yes/No select, and `external` fields (which v2 requires be acknowledged with `true`, core/src/form.ts:235-238) are a Done acknowledgement. Answers map labels back to option `value`s (the question tool sets `value = label`, core/src/tool/plugin/question.ts:117-127, but generic forms may not); answers for fields whose `when` conditions do not hold are dropped because v2 rejects answers to inactive fields (core/src/form.ts:239-247, 252-263). `hidden` fields keep their default.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — module level above `class OpenCodeNativeForwarder` (line 244)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1240)

**Interfaces:**
- Consumes: none (pure functions).
- Produces: `FormQuestion(key, kind, question, values_by_label)` frozen dataclass; `form_questions(fields) -> list[FormQuestion] | None`; `form_answer(questions, fields, content) -> dict[str, Any] | None`; helpers `_parse_boolean`, `_parse_number`, `_field_active`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- form field mapping -----------------------------------------------------


def test_form_string_with_options_is_single_select() -> None:
    fields = [
        {
            "key": "q0",
            "type": "string",
            "title": "Formatting",
            "description": "Indent style?",
            "options": [
                {"value": "tab", "label": "Tabs", "description": "hard tabs"},
                {"value": "space", "label": "Spaces"},
            ],
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert questions[0].question == {
        "question": "Indent style?",
        "options": [{"label": "Tabs", "description": "hard tabs"}, {"label": "Spaces"}],
        "multiSelect": False,
        "id": "q0",
        "header": "Formatting",
    }
    assert fwd_mod.form_answer(questions, fields, {"q0": "Tabs"}) == {"q0": "tab"}
    # A custom typed answer passes through unchanged.
    assert fwd_mod.form_answer(questions, fields, {"q0": "two spaces"}) == {"q0": "two spaces"}


def test_form_multiselect_maps_labels_to_values() -> None:
    fields = [
        {
            "key": "tools",
            "type": "multiselect",
            "options": [{"value": "t", "label": "Tests"}, {"value": "l", "label": "Lint"}],
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None and questions[0].question["multiSelect"] is True
    assert fwd_mod.form_answer(questions, fields, {"tools": ["Tests", "Lint"]}) == {
        "tools": ["t", "l"]
    }


def test_form_boolean_number_and_integer_fields() -> None:
    fields = [
        {"key": "ok", "type": "boolean", "title": "Proceed?"},
        {"key": "ratio", "type": "number"},
        {"key": "count", "type": "integer"},
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert questions[0].question["options"] == [{"label": "Yes"}, {"label": "No"}]
    assert questions[1].question["options"] == []
    content = {"ok": "No", "ratio": "0.5", "count": "3"}
    assert fwd_mod.form_answer(questions, fields, content) == {
        "ok": False,
        "ratio": 0.5,
        "count": 3,
    }
    assert fwd_mod.form_answer(questions, fields, {"count": "3.5"}) is None
    assert fwd_mod.form_answer(questions, fields, {"ratio": "abc"}) is None


def test_form_external_field_is_acknowledged() -> None:
    fields = [
        {
            "key": "login",
            "type": "external",
            "url": "https://example.test/auth",
            "title": "Sign in",
        }
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert "https://example.test/auth" in questions[0].question["question"]
    assert questions[0].question["options"] == [{"label": "Done"}]
    assert fwd_mod.form_answer(questions, fields, {"login": "Done"}) == {"login": True}


def test_form_hidden_fields_are_skipped_and_unknown_types_reject() -> None:
    hidden = [{"key": "token", "type": "string", "hidden": True}]
    assert fwd_mod.form_questions(hidden) == []
    assert fwd_mod.form_questions([{"key": "x", "type": "date"}]) is None
    assert fwd_mod.form_questions([{"key": "m", "type": "multiselect", "options": []}]) is None


def test_form_answer_drops_inactive_conditional_fields() -> None:
    fields = [
        {
            "key": "mode",
            "type": "string",
            "options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}],
        },
        {"key": "detail", "type": "string", "when": [{"key": "mode", "op": "eq", "value": "b"}]},
    ]
    questions = fwd_mod.form_questions(fields)
    assert questions is not None
    assert fwd_mod.form_answer(questions, fields, {"mode": "A", "detail": "x"}) == {"mode": "a"}
    assert fwd_mod.form_answer(questions, fields, {"mode": "B", "detail": "x"}) == {
        "mode": "b",
        "detail": "x",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_form_string_with_options_is_single_select or test_form_multiselect_maps_labels_to_values or test_form_boolean_number_and_integer_fields or test_form_external_field_is_acknowledged or test_form_hidden_fields_are_skipped_and_unknown_types_reject or test_form_answer_drops_inactive_conditional_fields"`

Expected: FAIL with:
  - `test_form_string_with_options_is_single_select: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`
  - `test_form_multiselect_maps_labels_to_values: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`
  - `test_form_boolean_number_and_integer_fields: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`
  - `test_form_external_field_is_acknowledged: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`
  - `test_form_hidden_fields_are_skipped_and_unknown_types_reject: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`
  - `test_form_answer_drops_inactive_conditional_fields: AttributeError: module 'omnigent.harnesses.opencode_native.forwarder' has no attribute 'form_questions'`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 244):

```python
@dataclass(frozen=True)
class FormQuestion:
    """
    One v2 form field rendered as a web ``ask_user_question`` entry.

    :param key: Form field key; also the web question id.
    :param kind: v2 field type, e.g. ``"string"`` or ``"multiselect"``.
    :param question: The web question payload.
    :param values_by_label: Option label -> option value.
    """

    key: str
    kind: str
    question: _JsonObject
    values_by_label: dict[str, str]


def form_questions(fields: object) -> list[FormQuestion] | None:
    """
    Map v2 ``Form.Field`` entries onto web ``ask_user_question`` questions.

    ``string`` with options -> single select; ``multiselect`` -> multi select;
    ``boolean`` -> Yes/No; ``number``/``integer`` and option-less ``string`` ->
    free text (the web form always offers a custom text row); ``external`` ->
    a Done acknowledgement naming the URL. Hidden fields keep their default.

    :param fields: ``form.fields`` from ``form.created``.
    :returns: The questions, or ``None`` when a visible field cannot be rendered.
    """
    if not isinstance(fields, list) or not fields:
        return None
    questions: list[FormQuestion] = []
    for raw_field in fields:
        if not isinstance(raw_field, Mapping):
            return None
        key = _str_field(raw_field, "key")
        kind = _str_field(raw_field, "type")
        if key is None or kind is None:
            return None
        if raw_field.get("hidden") is True:
            continue
        title = _str_field(raw_field, "title")
        prompt = _str_field(raw_field, "description") or title or key
        options: list[_JsonObject] = []
        values: dict[str, str] = {}
        multi = False
        if kind in ("string", "multiselect"):
            raw_options = raw_field.get("options")
            if raw_options is not None and not isinstance(raw_options, list):
                return None
            for option in raw_options or []:
                if not isinstance(option, Mapping):
                    return None
                label = _str_field(option, "label")
                value = option.get("value")
                if label is None or not isinstance(value, str):
                    return None
                entry: _JsonObject = {"label": label}
                description = _str_field(option, "description")
                if description is not None:
                    entry["description"] = description
                options.append(entry)
                values[label] = value
            if kind == "multiselect":
                if not options:
                    return None
                multi = True
        elif kind == "boolean":
            options = [{"label": "Yes"}, {"label": "No"}]
        elif kind == "external":
            url = _str_field(raw_field, "url")
            if url is None:
                return None
            prompt = f"{prompt}\n\nOpen {url} and choose Done when finished."
            options = [{"label": "Done"}]
        elif kind not in ("number", "integer"):
            return None
        question: _JsonObject = {
            "question": prompt,
            "options": options,
            "multiSelect": multi,
            "id": key,
        }
        if title is not None:
            question["header"] = title
        questions.append(
            FormQuestion(key=key, kind=kind, question=question, values_by_label=values)
        )
    return questions


def _parse_boolean(raw: str) -> bool | None:
    """Parse a Yes/No answer (or typed true/false) into a bool."""
    token = raw.strip().lower()
    if token in ("yes", "y", "true"):
        return True
    if token in ("no", "n", "false"):
        return False
    return None


def _parse_number(raw: str, *, integer: bool) -> int | float | None:
    """Parse a typed number; ``None`` when it is not a valid number."""
    try:
        number = float(raw.strip())
    except ValueError:
        return None
    if integer:
        return int(number) if number.is_integer() else None
    return number


def _field_active(field: Mapping[str, Any], answer: Mapping[str, Any]) -> bool:
    """Evaluate a field's ``when`` conditions (all must hold) against *answer*."""
    conditions = field.get("when")
    if not isinstance(conditions, list):
        return True
    for condition in conditions:
        if not isinstance(condition, Mapping):
            return False
        key = condition.get("key")
        if not isinstance(key, str) or key not in answer:
            return False
        value = answer[key]
        target = condition.get("value")
        hit = target in value if isinstance(value, list) else value == target
        if (condition.get("op") == "eq") != hit:
            return False
    return True


def form_answer(
    questions: list[FormQuestion],
    fields: list[Any],
    content: Mapping[str, Any],
) -> dict[str, Any] | None:
    """
    Convert the web form result into a v2 ``Form.Answer``.

    :param questions: The questions built by :func:`form_questions`.
    :param fields: The original ``form.fields`` (for ``when`` conditions).
    :param content: ``ElicitationResult.content`` keyed by question id.
    :returns: ``{field key: value}``, or ``None`` when an answer is invalid
        (the caller cancels the form).
    """
    answer: dict[str, Any] = {}
    for question in questions:
        raw = content.get(question.key)
        if question.kind == "external":
            answer[question.key] = True
            continue
        if raw is None:
            continue
        if question.kind == "multiselect":
            items = [raw] if isinstance(raw, str) else raw
            if not isinstance(items, list):
                return None
            answer[question.key] = [
                question.values_by_label.get(item, item) for item in items if isinstance(item, str)
            ]
            continue
        if not isinstance(raw, str):
            return None
        if question.kind == "string":
            answer[question.key] = question.values_by_label.get(raw, raw)
        elif question.kind == "boolean":
            parsed_bool = _parse_boolean(raw)
            if parsed_bool is None:
                return None
            answer[question.key] = parsed_bool
        else:
            parsed_number = _parse_number(raw, integer=question.kind == "integer")
            if parsed_number is None:
                return None
            answer[question.key] = parsed_number
    by_key = {
        f["key"]: f for f in fields if isinstance(f, Mapping) and isinstance(f.get("key"), str)
    }
    return {
        key: value
        for key, value in answer.items()
        if by_key.get(key, {}).get("type") == "external"
        or _field_active(by_key.get(key, {}), answer)
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_form_string_with_options_is_single_select or test_form_multiselect_maps_labels_to_values or test_form_boolean_number_and_integer_fields or test_form_external_field_is_acknowledged or test_form_hidden_fields_are_skipped_and_unknown_types_reject or test_form_answer_drops_inactive_conditional_fields"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `76 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): map v2 form fields to web question cards"
```

### Task 39: Forms: form.created -> web question card -> reply_form / cancel_form

Spec row `form.created {form}` -> web question card via `/hooks/native-permission-request`. `form.created` is `{form: Form.Info {id, sessionID, title, metadata?, fields}}` (form.ts:122-137,169) — the session id is nested, hence the Task 23 filter. The hook body keeps the v1 question contract (`operation_type: "question"`, `agent: "OpenCode"`, `policy_name: "opencode_native_question"`, structured `ask_user_question`, current forwarder.py:1173-1225) with `elicitation_id = form.id`. An `accept` verdict's `content {field: value}` becomes `reply_form(session_id, form_id, answer)`; decline/cancel/empty/invalid -> `cancel_form`. Parking runs in a background task so the event loop never blocks. Replaces `question.asked` handling (current forwarder.py:1041-1171).

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1436); class body end (after line 1380); import block (17-42)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1346)

**Interfaces:**
- Consumes: Task 38; Stage 1 `OpenCodeClient.reply_form(session_id, form_id, answer) -> bool`, `cancel_form(session_id, form_id) -> bool`, `OpenCodeClientError`.
- Produces: handler `_on_form_created`; `_handle_form(session_id, form_id, form)`; `_cancel_form_quietly(session_id, form_id)`; `_park_elicitation(elicitation_id, *, message, payload, preview) -> dict | None`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- form.created -----------------------------------------------------------


def _sample_answer(question: fwd_mod.FormQuestion) -> str | list[str]:
    """A web-form answer the mapper accepts for *question*'s field type."""
    labels = [option["label"] for option in question.question["options"]]
    if question.kind == "multiselect":
        return labels[:1]
    if question.kind in ("number", "integer"):
        return "1"
    return labels[0] if labels else "typed answer"


async def test_fixture_form_accept_replies_with_mapped_answer() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    created = _fixture("form.created")
    form = created.data["form"]
    questions = fwd_mod.form_questions(form["fields"])
    assert questions
    content = {question.key: _sample_answer(question) for question in questions}
    server.hook_response = {"action": "accept", "content": content}
    await fwd.handle_event(created)
    await _drain(fwd)
    hook = _hook_post(server)
    assert hook is not None
    assert hook["elicitation_id"] == form["id"]
    assert hook["operation_type"] == "question"
    assert hook["agent"] == "OpenCode"
    assert hook["policy_name"] == "opencode_native_question"
    assert [q["id"] for q in hook["ask_user_question"]["questions"]] == [q.key for q in questions]
    expected = fwd_mod.form_answer(questions, form["fields"], content)
    assert expected is not None
    assert opencode.form_replies == [(_FIX_SESSION, form["id"], expected)]
    assert opencode.form_cancels == []


def _form_event(
    form_id: str, fields: list[dict[str, Any]], title: str = "Questions"
) -> OpenCodeEvent:
    return OpenCodeEvent(
        id=None,
        type="form.created",
        data={"form": {"id": form_id, "sessionID": _SESSION, "title": title, "fields": fields}},
        location=None,
    )


_SINGLE = [
    {
        "key": "q0",
        "type": "string",
        "title": "Formatting",
        "description": "Indent style?",
        "options": [{"value": "Tabs", "label": "Tabs"}, {"value": "Spaces", "label": "Spaces"}],
        "custom": True,
    }
]


async def test_form_decline_cancels_without_reply() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "decline"}
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    assert opencode.form_replies == []


async def test_form_empty_verdict_cancels() -> None:
    """An empty 200 (TUI answered / timed out) cancels so OpenCode is not wedged."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", _SINGLE))
    await _drain(fwd)
    assert _hook_post(server) is not None
    assert opencode.form_cancels == [(_SESSION, "frm_1")]


async def test_unrenderable_form_cancels_without_hook() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "x", "type": "date"}]))
    await _drain(fwd)
    assert _hook_post(server) is None
    assert opencode.form_cancels == [(_SESSION, "frm_1")]


async def test_all_hidden_form_replies_empty_answer_without_hook() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "t", "type": "string", "hidden": True}]))
    await _drain(fwd)
    assert _hook_post(server) is None
    assert opencode.form_replies == [(_SESSION, "frm_1", {})]


async def test_invalid_form_answer_cancels() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"n": "not a number"}}
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_form_event("frm_1", [{"key": "n", "type": "number"}]))
    await _drain(fwd)
    assert opencode.form_cancels == [(_SESSION, "frm_1")]
    assert opencode.form_replies == []


async def test_form_created_dedupes() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    server.hook_response = {"action": "accept", "content": {"q0": "Tabs"}}
    fwd = _forwarder(server, opencode)
    event = _form_event("frm_1", _SINGLE)
    await fwd.handle_event(event)
    task = fwd._form_tasks["frm_1"]
    await fwd.handle_event(event)
    assert fwd._form_tasks["frm_1"] is task
    await _drain(fwd)
    assert opencode.form_replies == [(_SESSION, "frm_1", {"q0": "Tabs"})]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_form_accept_replies_with_mapped_answer or test_form_decline_cancels_without_reply or test_form_empty_verdict_cancels or test_unrenderable_form_cancels_without_hook or test_all_hidden_form_replies_empty_answer_without_hook or test_invalid_form_answer_cancels or test_form_created_dedupes"`

Expected: FAIL with:
  - `test_fixture_form_accept_replies_with_mapped_answer: assert None is not None`
  - `test_form_decline_cancels_without_reply: AssertionError: assert [] == [('ses_1', 'frm_1')]`
  - `test_form_empty_verdict_cancels: assert None is not None`
  - `test_unrenderable_form_cancels_without_hook: AssertionError: assert [] == [('ses_1', 'frm_1')]`
  - `test_all_hidden_form_replies_empty_answer_without_hook: AssertionError: assert [] == [('ses_1', 'frm_1', {})]`
  - `test_invalid_form_answer_cancels: AssertionError: assert [] == [('ses_1', 'frm_1')]`
  - `test_form_created_dedupes: KeyError: 'frm_1'`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1436):

```python
    "form.created": OpenCodeNativeForwarder._on_form_created,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_permission_replied`, which ends at line 1380):

```python
    async def _on_form_created(self, event: OpenCodeEvent) -> None:
        """Handle ``form.created`` — park a web question card in the background."""
        form = event.data.get("form")
        if not isinstance(form, Mapping):
            return
        form_id = _str_field(form, "id")
        session_id = _str_field(form, "sessionID")
        if form_id is None or session_id is None:
            return
        if not self.state.mark(self._key("form", form_id)):
            return
        task = asyncio.create_task(self._handle_form(session_id, form_id, dict(form)))
        self._form_tasks[form_id] = task
        task.add_done_callback(lambda _t, fid=form_id: self._form_tasks.pop(fid, None))

    async def _handle_form(self, session_id: str, form_id: str, form: dict[str, Any]) -> None:
        """Park one form as a web card and reply with the mapped answer.

        Any outcome other than a valid ``accept`` cancels the form so the
        OpenCode turn is never wedged. ``CancelledError`` propagates: it means
        the TUI answered first (see :meth:`_on_form_resolved`).
        """
        fields = form.get("fields")
        questions = form_questions(fields)
        if questions is None or not isinstance(fields, list):
            await self._cancel_form_quietly(session_id, form_id)
            return
        try:
            if not questions:
                await self._opencode.reply_form(session_id, form_id, {})
                return
            title = _str_field(form, "title")
            first_prompt = questions[0].question["question"]
            verdict = await self._park_elicitation(
                form_id,
                message=title or "OpenCode is asking a question",
                payload={"questions": [question.question for question in questions]},
                preview=first_prompt[:1024] if isinstance(first_prompt, str) else None,
            )
            if verdict is None or verdict.get("action") != "accept":
                await self._cancel_form_quietly(session_id, form_id)
                return
            content = verdict.get("content")
            answer = form_answer(questions, fields, content if isinstance(content, dict) else {})
            if answer is None:
                await self._cancel_form_quietly(session_id, form_id)
                return
            await self._opencode.reply_form(session_id, form_id, answer)
        except asyncio.CancelledError:
            raise
        except (httpx.HTTPError, OpenCodeClientError) as exc:
            _logger.warning("OpenCode form handling failed for form=%s: %s", form_id, exc)
            await self._cancel_form_quietly(session_id, form_id)

    async def _cancel_form_quietly(self, session_id: str, form_id: str) -> None:
        """Best-effort cancel a form; a TUI answer commonly makes this 404."""
        try:
            await self._opencode.cancel_form(session_id, form_id)
        except (httpx.HTTPError, OpenCodeClientError):
            _logger.debug("OpenCode form cancel for form=%s failed", form_id, exc_info=True)

    async def _park_elicitation(
        self,
        elicitation_id: str,
        *,
        message: str,
        payload: dict[str, Any],
        preview: str | None,
    ) -> dict[str, Any] | None:
        """POST the native permission hook for a form; return the web verdict.

        Returns ``None`` for every "no answer" outcome (transport error, status
        >= 400, empty body, or non-dict JSON). The structured
        ``ask_user_question`` is the payload the web UI renders.
        """
        body: dict[str, Any] = {
            "elicitation_id": elicitation_id,
            "operation_type": "question",
            "agent": "OpenCode",
            "policy_name": "opencode_native_question",
            "message": message,
            "ask_user_question": payload,
        }
        if preview is not None:
            body["content_preview"] = preview
        url = f"/v1/sessions/{quote(self._session_id, safe='')}/hooks/native-permission-request"
        try:
            response = await self._server.post(url, json=body)
        except httpx.HTTPError:
            _logger.warning(
                "OpenCode form hook POST failed for session=%s form=%s",
                self._session_id,
                elicitation_id,
                exc_info=True,
            )
            return None
        if response.status_code >= 400:
            _logger.warning(
                "OpenCode form hook rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
            return None
        if not response.content:
            return None
        try:
            result = response.json()
        except ValueError:
            _logger.warning("OpenCode form hook returned non-JSON: %s", response.text[:512])
            return None
        return result if isinstance(result, dict) else None
```

Replace the import block (lines 17-42) with:

```python
import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias, TypedDict
from urllib.parse import quote

import httpx

from omnigent.harnesses.opencode_native.bridge import update_active_message_id
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeEvent,
)
from omnigent.harnesses.opencode_native.permissions import (
    OpenCodePermissionRequest,
    PolicyDecision,
    decision_to_reply,
    map_verdict_to_decision,
    normalize_for_policy,
    parse_permission_request,
)
from omnigent.util.json_types import JsonObject as _JsonObject
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_fixture_form_accept_replies_with_mapped_answer or test_form_decline_cancels_without_reply or test_form_empty_verdict_cancels or test_unrenderable_form_cancels_without_hook or test_all_hidden_form_replies_empty_answer_without_hook or test_invalid_form_answer_cancels or test_form_created_dedupes"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `83 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): answer v2 forms from the web question card"
```

### Task 40: Form resolution: form.replied / form.cancelled withdraw the card

Spec row `form.replied` / `form.cancelled` -> `external_elicitation_resolved`. Both carry `{id, sessionID}` (form.ts:170-171). When the TUI answered first the parked task is still pending: cancel it (so it neither double-replies nor lingers), then clear the card — the v1 `_withdraw_question` guard (current forwarder.py:1235-1249) keyed by form id.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1550); class body end (after line 1493)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1467)

**Interfaces:**
- Consumes: Task 39.
- Produces: handler `_on_form_resolved` (mapped for both event types).

- [ ] **Step 1: Write the failing test**

Also add `import contextlib` to the test module imports.

Replace the import block `tests/test_opencode_native_forwarder.py:5-14` with:

```python
import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type
```

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- form resolution --------------------------------------------------------


async def test_form_replied_cancels_pending_task_and_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)

    async def _never() -> None:
        await asyncio.sleep(3600)

    pending: asyncio.Task[None] = asyncio.create_task(_never())
    fwd._form_tasks["frm_1"] = pending
    await fwd.handle_event(_event("form.replied", id="frm_1", answer={"q0": "Tabs"}))
    assert "frm_1" not in fwd._form_tasks
    with contextlib.suppress(asyncio.CancelledError):
        await pending
    assert pending.cancelled()
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "frm_1"}]
    assert opencode.form_replies == []
    assert opencode.form_cancels == []


async def test_fixture_form_replied_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    replied = _fixture("form.replied")
    await fwd.handle_event(replied)
    assert _datas(server.posts, "external_elicitation_resolved") == [
        {"elicitation_id": replied.data["id"]}
    ]


async def test_form_cancelled_clears_card() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_event("form.cancelled", id="frm_9"))
    assert _datas(server.posts, "external_elicitation_resolved") == [{"elicitation_id": "frm_9"}]


async def test_run_awaits_cancelled_background_tasks() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    cleanup_finished = asyncio.Event()

    async def _pending() -> None:
        try:
            await asyncio.Future()
        finally:
            await asyncio.sleep(0)
            cleanup_finished.set()

    pending = asyncio.create_task(_pending())
    fwd._form_tasks["frm_1"] = pending
    await asyncio.sleep(0)
    await fwd.run(max_reconnects=0)
    assert cleanup_finished.is_set()
    assert pending.cancelled()
    assert fwd._form_tasks == {}
    assert fwd._permission_tasks == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_form_replied_cancels_pending_task_and_clears_card or test_fixture_form_replied_clears_card or test_form_cancelled_clears_card or test_run_awaits_cancelled_background_tasks"`

Expected: FAIL with:
  - `test_form_replied_cancels_pending_task_and_clears_card: AssertionError: assert 'frm_1' not in {'frm_1': <Task pending name='Task-2' coro=<test_form_replied_cancels_pending_task_and_clears_card.<locals>._never() r...-fork`
  - `test_fixture_form_replied_clears_card: AssertionError: assert [] == [{'elicitation_id': 'frm_1'}]`
  - `test_form_cancelled_clears_card: AssertionError: assert [] == [{'elicitation_id': 'frm_9'}]`
  Already passing (guards that the new code must keep true): `test_run_awaits_cancelled_background_tasks`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1550):

```python
    "form.replied": OpenCodeNativeForwarder._on_form_resolved,
    "form.cancelled": OpenCodeNativeForwarder._on_form_resolved,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_park_elicitation`, which ends at line 1493):

```python
    async def _on_form_resolved(self, event: OpenCodeEvent) -> None:
        """Handle ``form.replied`` / ``form.cancelled`` — withdraw the web card.

        When the TUI answered first the parked task is still pending: cancel
        it so it neither double-replies nor lingers, then clear the card.
        """
        form_id = _str_field(event.data, "id")
        if form_id is None:
            return
        task = self._form_tasks.pop(form_id, None)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._post_event(_EXTERNAL_ELICITATION_RESOLVED, {"elicitation_id": form_id})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_form_replied_cancels_pending_task_and_clears_card or test_fixture_form_replied_clears_card or test_form_cancelled_clears_card or test_run_awaits_cancelled_background_tasks"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `87 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): withdraw the web question card on v2 form resolution"
```

### Task 41: Subagents: session.created {parentID} -> external_subagent_start + child routing

Spec row `session.created {parentID}` -> `external_subagent_start`, and filtering by the `parentID` chain. `session.created` carries `{sessionID, parentID?, title?, agent?, ...}` (session-event.ts:51-69). The `subagent` tool creates the child with `title = input.description`, `agent = input.agent` and immediately reports `context.progress({ sessionID: child.id, status: "running" })` (core/src/tool/plugin/subagent.ts:185-201), which is how the forwarder learns the parent `tool_use_id`. The payload is the one claude-native posts (`subagent_id`, `agent_type`, `description`, `tool_use_id`; omnigent/harnesses/claude_native/forwarder.py:1693-1740) and the server returns `child_session_id` (omnigent/server/routes/sessions/routes_events.py:1830-1839, helpers.py:3580+). Child events then post to the child conversation; if a child event arrives before any progress, the child's own id stands in for `tool_use_id`. Grandchildren mint on their parent child's conversation. Usage and model mirroring stay root-only; child permissions still go through this conversation's policy evaluator and are answered on the child session.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `_HANDLERS` (closing brace at line 1567); `_active_turn` (563-566); `_event_targets_session` (553-561); `_on_tool_progress` (965-991); class body end (after line 1508); `__init__` end (line 489); module level above `class OpenCodeNativeForwarder` (line 431)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1529)

**Interfaces:**
- Consumes: Tasks 20-25, 33.
- Produces: `_PendingChild(parent_id, agent, title, tool_use_id=None)`; `_child_session_id(response) -> str | None`; handler `_on_session_created`; `_link_child_to_call(parent, child_id, call_id)`; `_start_child_conversation(child_id)`; final `_active_turn`, `_event_targets_session`, `_on_tool_progress`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- subagents --------------------------------------------------------------


def _child_created(child_id: str, parent_id: str = _SESSION) -> OpenCodeEvent:
    return OpenCodeEvent(
        id=None,
        type="session.created",
        data={
            "sessionID": child_id,
            "parentID": parent_id,
            "projectID": "prj_1",
            "location": {"directory": "/work"},
            "slug": "child",
            "title": "Explore the repo",
            "agent": "explore",
            "version": "2.0.18",
        },
        location=None,
    )


async def test_subagent_child_is_minted_with_its_tool_call() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_step_started("msg_1"))
    await fwd.handle_event(
        _event(
            "session.tool.input.started",
            assistantMessageID="msg_1",
            id="call_sub",
            name="subagent",
        )
    )
    await fwd.handle_event(_child_created("ses_child"))
    assert "external_subagent_start" not in _types(server.posts)
    await fwd.handle_event(
        _event(
            "session.tool.progress",
            assistantMessageID="msg_1",
            id="call_sub",
            metadata={"sessionID": "ses_child", "status": "running"},
        )
    )
    url, start = next((u, b) for u, b in server.posts if b["type"] == "external_subagent_start")
    assert url == "/v1/sessions/conv_1/events"
    assert start["data"] == {
        "subagent_id": "ses_child",
        "agent_type": "explore",
        "description": "Explore the repo",
        "tool_use_id": "call_sub",
    }


async def test_subagent_child_events_post_to_the_child_conversation() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    child_step = OpenCodeEvent(
        id=None,
        type="session.step.started",
        data={
            "sessionID": "ses_child",
            "assistantMessageID": "msg_c1",
            "agent": "explore",
            "model": {"id": "m", "providerID": "p"},
            "started": 1,
        },
        location=None,
    )
    await fwd.handle_event(child_step)
    start = next(b for _u, b in server.posts if b["type"] == "external_subagent_start")
    # No progress arrived first, so the child's own id stands in for the call id.
    assert start["data"]["tool_use_id"] == "ses_child"
    running = [(u, b) for u, b in server.posts if b["type"] == "external_session_status"]
    assert running == [
        (
            "/v1/sessions/conv_child_1/events",
            {
                "type": "external_session_status",
                "data": {"status": "running", "response_id": "msg_c1"},
            },
        )
    ]
    # Child steps never touch the parent's model or usage.
    assert "external_model_change" not in _types(server.posts)


async def test_grandchild_session_follows_the_parent_chain() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    grandchild = _child_created("ses_grand", parent_id="ses_child")
    assert fwd._event_targets_session(grandchild) is True
    await fwd.handle_event(grandchild)
    await fwd.handle_event(
        OpenCodeEvent(
            id=None,
            type="session.compaction.started",
            data={"sessionID": "ses_grand", "reason": "auto", "recent": ""},
            location=None,
        )
    )
    starts = [
        (u, b["data"]["subagent_id"])
        for u, b in server.posts
        if b["type"] == "external_subagent_start"
    ]
    # The child is minted first (on the root), then the grandchild on the child.
    assert starts == [
        ("/v1/sessions/conv_1/events", "ses_child"),
        ("/v1/sessions/conv_child_1/events", "ses_grand"),
    ]


async def test_unrelated_session_created_is_ignored() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    stranger = _child_created("ses_other_child", parent_id="ses_stranger")
    assert fwd._event_targets_session(stranger) is False
    await fwd.handle_event(stranger)
    assert "ses_other_child" not in fwd._turns


async def test_child_permission_replies_to_the_child_session() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    await fwd.handle_event(_child_created("ses_child"))
    await fwd.handle_event(_asked("per_c", sessionID="ses_child"))
    await _drain(fwd)
    assert opencode.permission_replies == [("ses_child", "per_c", "reject")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_subagent_child_is_minted_with_its_tool_call or test_subagent_child_events_post_to_the_child_conversation or test_grandchild_session_follows_the_parent_chain or test_unrelated_session_created_is_ignored or test_child_permission_replies_to_the_child_session"`

Expected: FAIL with:
  - `test_subagent_child_is_minted_with_its_tool_call: StopIteration`
  - `test_subagent_child_events_post_to_the_child_conversation: StopIteration`
  - `test_grandchild_session_follows_the_parent_chain: AssertionError: assert False is True`
  - `test_child_permission_replies_to_the_child_session: AssertionError: assert [] == [('ses_child'...c', 'reject')]`
  Already passing (guards that the new code must keep true): `test_unrelated_session_created_is_ignored`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add these entries at the end of `_HANDLERS`, just before its closing `}` (line 1567):

```python
    "session.created": OpenCodeNativeForwarder._on_session_created,
```

Add to the end of the `OpenCodeNativeForwarder` class body (after `_on_form_resolved`, which ends at line 1508):

```python
    async def _on_session_created(self, event: OpenCodeEvent) -> None:
        """Handle ``session.created {parentID}`` — register a subagent child session."""
        child_id = _str_field(event.data, "sessionID")
        parent_id = _str_field(event.data, "parentID")
        if child_id is None or parent_id is None or child_id in self._turns:
            return
        self._turns[child_id] = _SessionTurn(session_id=child_id, conversation_id=None)
        self._pending_children[child_id] = _PendingChild(
            parent_id=parent_id,
            agent=_str_field(event.data, "agent") or "subagent",
            title=_str_field(event.data, "title") or "",
        )

    async def _link_child_to_call(self, parent: _SessionTurn, child_id: str, call_id: str) -> None:
        """Bind a child session to its parent ``subagent`` call and mint it."""
        if child_id not in self._turns:
            self._turns[child_id] = _SessionTurn(session_id=child_id, conversation_id=None)
            self._pending_children[child_id] = _PendingChild(
                parent_id=parent.session_id, agent="subagent", title=""
            )
        pending = self._pending_children.get(child_id)
        if pending is not None and pending.tool_use_id is None:
            pending.tool_use_id = call_id
        await self._start_child_conversation(child_id)

    async def _start_child_conversation(self, child_id: str) -> None:
        """POST ``external_subagent_start`` on the parent and adopt the child id."""
        turn = self._turns.get(child_id)
        pending = self._pending_children.get(child_id)
        if turn is None or pending is None or turn.conversation_id is not None:
            return
        parent = self._turns.get(pending.parent_id)
        if parent is not None and parent.conversation_id is None:
            await self._start_child_conversation(parent.session_id)
        parent_conversation = parent.conversation_id if parent is not None else None
        response = await self._post_event(
            _EXTERNAL_SUBAGENT_START,
            {
                "subagent_id": child_id,
                "agent_type": pending.agent,
                "description": pending.title,
                "tool_use_id": pending.tool_use_id or child_id,
            },
            conversation_id=parent_conversation or self._session_id,
        )
        child_conversation = _child_session_id(response)
        if child_conversation is None:
            _logger.warning("OpenCode subagent start failed for child session=%s", child_id)
            return
        self._pending_children.pop(child_id, None)
        turn.conversation_id = child_conversation
```

Replace `_on_tool_progress` (lines 965-991) with:

```python
    async def _on_tool_progress(self, event: OpenCodeEvent) -> None:
        """Handle ``session.tool.progress`` — register subagents, stream output.

        v2 progress metadata is a replacement snapshot. Built-in tools report
        ids only (shell ``{shellID}``, subagent ``{sessionID, status}``), so
        output streams only when a tool reports a growing ``metadata.output``
        string; only the new suffix is forwarded.
        """
        turn = await self._active_turn(event)
        if turn is None:
            return
        call_id = _str_field(event.data, "id")
        metadata = event.data.get("metadata")
        if call_id is None or not isinstance(metadata, Mapping):
            return
        child_id = _str_field(metadata, "sessionID")
        if child_id is not None and turn.tool_names.get(call_id) == "subagent":
            await self._link_child_to_call(turn, child_id, call_id)
        output = metadata.get("output")
        if not isinstance(output, str):
            return
        previous = turn.tool_output.get(call_id, "")
        turn.tool_output[call_id] = output
        if len(output) <= len(previous) or not output.startswith(previous):
            return
        await self._post_event(
            _EXTERNAL_TOOL_OUTPUT_DELTA,
            {"call_id": call_id, "delta": output[len(previous) :]},
            conversation_id=turn.conversation_id,
        )
```

Replace `_active_turn` (lines 563-566) with:

```python
    async def _active_turn(self, event: OpenCodeEvent) -> _SessionTurn | None:
        """Return the event's session state once its Omnigent conversation exists."""
        session_id = _event_session_id(event) or self._opencode_session_id
        turn = self._turns.get(session_id)
        if turn is None:
            return None
        if turn.conversation_id is None:
            await self._start_child_conversation(session_id)
        return turn if turn.conversation_id is not None else None
```

Replace `_event_targets_session` (lines 553-561) with:

```python
    def _event_targets_session(self, event: OpenCodeEvent) -> bool:
        """
        Return whether *event* belongs to a mirrored session.

        Events carry ``data.sessionID`` (``form.created`` nests it under
        ``form``). The root session and known subagent children pass; a
        ``session.created`` whose ``parentID`` is mirrored passes so the child
        can be registered. Events without a session id pass through.
        """
        session_id = _event_session_id(event)
        if session_id is None or session_id in self._turns:
            return True
        if event.type == "session.created":
            parent_id = _str_field(event.data, "parentID")
            return parent_id is not None and parent_id in self._turns
        return False
```

Append to the end of `OpenCodeNativeForwarder.__init__` (after line 489):

```python
        # Child session id -> subagent start details until its conversation exists.
        self._pending_children: dict[str, _PendingChild] = {}
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 431):

```python
@dataclass
class _PendingChild:
    """
    A subagent child session awaiting its Omnigent conversation.

    :param parent_id: Parent OpenCode session id.
    :param agent: OpenCode agent the child runs, e.g. ``"explore"``.
    :param title: Child session title (the subagent task description).
    :param tool_use_id: Parent ``subagent`` tool call id, once known.
    """

    parent_id: str
    agent: str
    title: str
    tool_use_id: str | None = None


def _child_session_id(response: httpx.Response | None) -> str | None:
    """Read the minted child conversation id from an ``external_subagent_start`` ack."""
    if response is None or response.status_code >= 400 or not response.content:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    child = body.get("child_session_id") if isinstance(body, dict) else None
    return child if isinstance(child, str) and child else None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_subagent_child_is_minted_with_its_tool_call or test_subagent_child_events_post_to_the_child_conversation or test_grandchild_session_follows_the_parent_chain or test_unrelated_session_created_is_ignored or test_child_permission_replies_to_the_child_session"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `92 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py
git commit -m "feat(opencode-native): mirror v2 subagent child sessions"
```

### Task 42: History seeding from v2 messages (content[] model)

Resume/restart must not re-post history. v2 `GET /api/session/{id}/message` returns `Session.Message.Info[]` (openapi `SessionMessagesResponse {data, cursor}`; Stage 1's `list_messages` unwraps and pages). User messages are `{id, type: "user", text, files?}`; assistant messages are `{id, type: "assistant", model, content[], cost?, tokens?, time: {created, completed?}}` with `content[]` items `text {text}`, `reasoning {text}`, `tool {id, name, state: {status: streaming|running|completed|error, input, content?, error?}}` (packages/schema/src/session-message.ts:73-236). Text ordinals are per type within the message (publish-llm-event.ts:125-145), so the n-th `text` item is ordinal n. A still-running tool's `tool-out` key stays unmarked so its live result posts. The catch-up cursor (Task 43) is the last message of the settled prefix. This also restores the last model so the first live step is not reported as a switch.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `run` (522-549); class body end (after line 1606); `__init__` end (line 520); module level above `class OpenCodeNativeForwarder` (line 460)
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1661)
- Test: `tests/test_opencode_forwarder_reconnect.py` (append after line 159)

**Interfaces:**
- Consumes: Tasks 22, 24, 26, 27; Stage 1 `OpenCodeClient.list_messages(session_id, *, after_id=None) -> list[dict]`.
- Produces: `_history_message_settled(message) -> bool`; `seed_dedupe_from_history()`; `_mark_history_message(message)`; `run()` seeds on the first connection; `_last_seen_message_id`.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- history seeding --------------------------------------------------------


def _assistant_message(
    message_id: str, *content: dict[str, Any], completed: bool = True, **extra: Any
) -> dict[str, Any]:
    time_info: dict[str, Any] = {"created": 1}
    if completed:
        time_info["completed"] = 2
    return {
        "id": message_id,
        "type": "assistant",
        "agent": "build",
        "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
        "content": list(content),
        "time": time_info,
        **extra,
    }


def _tool_content(call_id: str, status: str, **state: Any) -> dict[str, Any]:
    state.setdefault("input", {"command": "ls"})
    return {
        "type": "tool",
        "id": call_id,
        "name": "shell",
        "state": {"status": status, **state},
        "time": {"created": 1},
    }


async def test_seed_marks_v2_history_keys() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant_message(
            "msg_1",
            {"type": "reasoning", "text": "think"},
            {"type": "text", "text": "answer"},
            _tool_content("call_1", "completed", content=[{"type": "text", "text": "ok"}]),
            _tool_content("call_2", "running", metadata={}),
        ),
        "not-a-mapping",
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert fwd.state.mark(fwd._key("user", "msg_u")) is False
    assert fwd.state.mark(fwd._key("text-final", "msg_1", "0")) is False
    assert fwd.state.mark(fwd._key("tool-call", "call_1")) is False
    assert fwd.state.mark(fwd._key("tool-out", "call_1")) is False
    # A still-running tool's output must still post live.
    assert fwd.state.mark(fwd._key("tool-out", "call_2")) is True


async def test_seed_swallows_history_errors() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()

    async def _boom(_sid: str, *, after_id: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("history unavailable")

    opencode.list_messages = _boom  # type: ignore[assignment]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert server.posts == []


async def test_seed_rebuilds_usage_and_model() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    tokens_1 = {"input": 1000, "output": 50, "reasoning": 0, "cache": {"read": 200, "write": 0}}
    tokens_2 = {"input": 2000, "output": 100, "reasoning": 0, "cache": {"read": 300, "write": 0}}
    opencode.messages = [
        _assistant_message("msg_1", cost=0.01, tokens=tokens_1),
        _assistant_message("msg_2", cost=0.02, tokens=tokens_2),
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    usage = _datas(server.posts, "external_session_usage")[-1]
    assert usage["cumulative_cost_usd"] == 0.03
    assert usage["cumulative_input_tokens"] == 3000
    assert usage["cumulative_output_tokens"] == 150
    assert usage["cumulative_cache_read_input_tokens"] == 500
    # The next step on the same model is not reported as a switch.
    await fwd.handle_event(_step_started("msg_3"))
    assert "external_model_change" not in _types(server.posts)


async def test_seed_cursor_stops_at_first_unsettled_message() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant_message("msg_1"),
        _assistant_message("msg_2", completed=False),
        {"id": "msg_u2", "type": "user", "text": "more", "time": {"created": 3}},
    ]
    fwd = _forwarder(server, opencode)
    await fwd.seed_dedupe_from_history()
    assert fwd._last_seen_message_id == "msg_1"
```

Append to the end of `tests/test_opencode_forwarder_reconnect.py`:

```python
async def test_run_seeds_on_initial_connect() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [_assistant("msg_old", _text("old answer"))]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=0)
    assert fwd.state.mark(fwd._key("text-final", "msg_old", "0")) is False
    assert opencode.after_ids == [None]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -v -k "test_seed_marks_v2_history_keys or test_seed_swallows_history_errors or test_seed_rebuilds_usage_and_model or test_seed_cursor_stops_at_first_unsettled_message or test_run_seeds_on_initial_connect"`

Expected: FAIL with:
  - `test_run_seeds_on_initial_connect: AssertionError: assert True is False`
  - `test_seed_marks_v2_history_keys: AttributeError: 'OpenCodeNativeForwarder' object has no attribute 'seed_dedupe_from_history'`
  - `test_seed_swallows_history_errors: AttributeError: 'OpenCodeNativeForwarder' object has no attribute 'seed_dedupe_from_history'`
  - `test_seed_rebuilds_usage_and_model: AttributeError: 'OpenCodeNativeForwarder' object has no attribute 'seed_dedupe_from_history'`
  - `test_seed_cursor_stops_at_first_unsettled_message: AttributeError: 'OpenCodeNativeForwarder' object has no attribute 'seed_dedupe_from_history'`

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add to the end of the `OpenCodeNativeForwarder` class body (after `_start_child_conversation`, which ends at line 1606):

```python
    async def seed_dedupe_from_history(self) -> None:
        """
        Pre-mark persisted history so a restart never re-posts it.

        Best effort: a history failure leaves the dedupe set empty. Rebuilds
        cumulative usage and the last mirrored model from assistant messages.
        """
        try:
            messages = await self._opencode.list_messages(self._opencode_session_id)
        except Exception:  # noqa: BLE001 - seeding is best effort.
            _logger.debug("OpenCode forwarder could not seed dedupe from history", exc_info=True)
            return
        settled_prefix = True
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            self._mark_history_message(message)
            message_id = _str_field(message, "id")
            if settled_prefix and message_id is not None and _history_message_settled(message):
                self._last_seen_message_id = message_id
            else:
                settled_prefix = False
        try:
            await self._post_session_usage()
        except Exception:  # noqa: BLE001 - usage re-post is best effort.
            _logger.debug(
                "OpenCode forwarder could not re-post usage after seeding", exc_info=True
            )

    def _mark_history_message(self, message: Mapping[str, Any]) -> None:
        """Pre-mark one history message's dedupe keys and record its usage."""
        message_id = _str_field(message, "id")
        if message_id is None:
            return
        kind = message.get("type")
        if kind == "user":
            self.state.mark(self._key("user", message_id))
            return
        if kind != "assistant":
            return
        model = _model_ref(message.get("model"))
        self._record_step_usage(message_id, message, model)
        if model is not None:
            self._last_model = model
        content = message.get("content")
        text_ordinal = 0
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text":
                self.state.mark(self._key("text-final", message_id, str(text_ordinal)))
                text_ordinal += 1
            elif item.get("type") == "tool":
                call_id = _str_field(item, "id")
                state = item.get("state")
                status = state.get("status") if isinstance(state, Mapping) else None
                if call_id is None or status == "streaming":
                    continue
                self.state.mark(self._key("tool-call", call_id))
                if status in ("completed", "error"):
                    self.state.mark(self._key("tool-out", call_id))
```

Replace `run` (lines 522-549) with:

```python
    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff and gap-fill.

        The first connection pre-marks persisted history so a restart never
        re-posts it.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used by
            tests to bound the loop.
        """
        attempt = 0
        backoff = 0.5
        try:
            while True:
                if attempt == 0:
                    await self.seed_dedupe_from_history()
                try:
                    await self._consume_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                    _logger.warning(
                        "OpenCode forwarder SSE error for session=%s; reconnecting",
                        self._session_id,
                        exc_info=True,
                    )
                attempt += 1
                if max_reconnects is not None and attempt > max_reconnects:
                    return
                await asyncio.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
        finally:
            await self._cancel_background_tasks()
```

Append to the end of `OpenCodeNativeForwarder.__init__` (after line 520):

```python
        # Newest history message known settled; the reconnect catch-up cursor.
        self._last_seen_message_id: str | None = None
```

Insert at module level directly above `class OpenCodeNativeForwarder:` (line 460):

```python
def _history_message_settled(message: Mapping[str, Any]) -> bool:
    """Return whether a v2 ``Session.Message.Info`` can no longer change."""
    kind = message.get("type")
    if kind == "assistant":
        time_info = message.get("time")
        return isinstance(time_info, Mapping) and isinstance(
            time_info.get("completed"), (int, float)
        )
    if kind == "compaction":
        return message.get("status") != "running"
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -v -k "test_seed_marks_v2_history_keys or test_seed_swallows_history_errors or test_seed_rebuilds_usage_and_model or test_seed_cursor_stops_at_first_unsettled_message or test_run_seeds_on_initial_connect"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `97 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py
git commit -m "feat(opencode-native): seed forwarder dedupe from v2 message history"
```

### Task 43: Reconnect catch-up via list_messages(after_id=...)

Spec: on SSE drop, re-fetch `GET /api/session/{id}/message` after the last seen message id (the durable `/experimental/session/{id}/log` is not used). `catch_up_from_history` passes `after_id=self._last_seen_message_id`, replays each unseen user prompt, assistant text (posted under its stream id) and tool call/output through the normal post paths, and advances the cursor only across the settled prefix so an in-flight assistant message is re-read next time. Dedupe keys make the replay idempotent against live events that arrive after the reconnect.

**Files:**
- Modify: `omnigent/harnesses/opencode_native/forwarder.py` — `run` (537-569); class body end (after line 1688)
- Test: `tests/test_opencode_forwarder_reconnect.py` (append after line 168)

**Interfaces:**
- Consumes: Tasks 32, 39.
- Produces: `catch_up_from_history()`; `_replay_history_message(turn, message)`; `_replay_tool(turn, message_id, item)`; final `run()`.

- [ ] **Step 1: Write the failing test**

Also add `from tests.opencode_v2_fixtures import load_messages` to `tests/test_opencode_forwarder_reconnect.py` imports.

Replace the import block `tests/test_opencode_forwarder_reconnect.py:10-16` with:

```python
from collections.abc import AsyncIterator
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import load_messages
```

Append to the end of `tests/test_opencode_forwarder_reconnect.py`:

```python
async def test_run_catches_up_on_reconnect_posts_gap_content() -> None:
    """msg_1 arrives live; msg_2 lands during the drop and is replayed exactly once."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    msg_1 = _assistant("msg_1", _text("hello"))
    msg_2 = _assistant("msg_2", _text("from gap"))
    opencode.message_snapshots = [[], [msg_1, msg_2]]
    opencode._event_batches = [
        _live_text_turn("msg_1", "hello"),
        _live_text_turn("msg_2", "from gap"),
    ]
    await _run(fwd, max_reconnects=1)
    assert _assistant_texts(server) == ["hello", "from gap"]


async def test_catch_up_passes_cursor_as_after_id() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [
        {"id": "msg_u", "type": "user", "text": "hi", "time": {"created": 1}},
        _assistant("msg_1", _text("answer")),
    ]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=1)
    assert opencode.after_ids == [None, "msg_1"]


async def test_catch_up_called_on_reconnect_not_initial_connect() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    seed_calls: list[int] = []
    catch_up_calls: list[int] = []

    async def _counting_seed() -> None:
        seed_calls.append(1)

    async def _counting_catch_up() -> None:
        catch_up_calls.append(1)

    fwd.seed_dedupe_from_history = _counting_seed  # type: ignore[method-assign]
    fwd.catch_up_from_history = _counting_catch_up  # type: ignore[method-assign]

    async def _failing_consume() -> None:
        raise httpx.ReadError("dropped", request=httpx.Request("GET", "http://x/api/event"))

    fwd._consume_once = _failing_consume  # type: ignore[method-assign]
    await _run(fwd, max_reconnects=2)
    assert len(seed_calls) == 1
    assert len(catch_up_calls) == 2


async def test_reconnect_catches_up_user_text_and_tool_items() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    opencode.message_snapshots = [
        [],
        [
            {"id": "msg_u", "type": "user", "text": "run the command", "time": {"created": 1}},
            _assistant(
                "msg_a",
                {
                    "type": "tool",
                    "id": "call_1",
                    "name": "shell",
                    "state": {
                        "status": "completed",
                        "input": {"command": "pwd"},
                        "content": [{"type": "text", "text": "/workspace"}],
                    },
                    "time": {"created": 1, "completed": 2},
                },
                _text("done"),
            ),
        ],
    ]
    opencode._event_batches = [[], []]
    await _run(fwd, max_reconnects=1)
    items = [b["data"] for _u, b in server.posts if b["type"] == "external_conversation_item"]
    assert [i["item_type"] for i in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0]["item_data"]["role"] == "user"
    assert items[0]["item_data"]["content"][0]["text"] == "run the command"
    assert items[1]["item_data"]["name"] == "shell"
    assert items[2]["item_data"]["output"] == "/workspace"
    assert items[3]["item_data"]["content"][0]["text"] == "done"
    assert items[3]["message_id"] == "opencode:msg_a:text:0"


async def test_catch_up_keeps_cursor_before_incomplete_message() -> None:
    """An in-flight assistant message is re-read on the next catch-up."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode)
    opencode.message_snapshots = [
        [],
        [
            _assistant("msg_done", _text("first")),
            _assistant("msg_live", _text("partial"), completed=False),
        ],
    ]
    opencode._event_batches = [[], []]
    await _run(fwd, max_reconnects=1)
    assert fwd._last_seen_message_id == "msg_done"


async def test_reconnect_does_not_repost_already_seeded_content() -> None:
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.messages = [_assistant("msg_pre", _text("before disconnect"))]
    fwd = _forwarder(server, opencode)
    opencode._event_batches = [
        _live_text_turn("msg_pre", "before disconnect"),
        _live_text_turn("msg_pre", "before disconnect"),
    ]
    await _run(fwd, max_reconnects=1)
    assert _assistant_texts(server) == []


async def test_fixture_messages_replay_on_catch_up() -> None:
    """The captured ``GET /api/session/{id}/message`` body replays every text once."""
    body = load_messages()
    messages = body["data"]
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    opencode.message_snapshots = [[], messages]
    opencode._event_batches = [[], []]
    fwd = _forwarder(server, opencode)
    await _run(fwd, max_reconnects=1)
    expected = [
        item["text"]
        for message in messages
        if message.get("type") == "assistant"
        for item in message.get("content", [])
        if item.get("type") == "text" and item.get("text")
    ]
    assert _assistant_texts(server) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_forwarder_reconnect.py -v -k "test_run_catches_up_on_reconnect_posts_gap_content or test_catch_up_passes_cursor_as_after_id or test_catch_up_called_on_reconnect_not_initial_connect or test_reconnect_catches_up_user_text_and_tool_items or test_catch_up_keeps_cursor_before_incomplete_message or test_reconnect_does_not_repost_already_seeded_content or test_fixture_messages_replay_on_catch_up"`

Expected: FAIL with:
  - `test_catch_up_passes_cursor_as_after_id: AssertionError: assert [None] == [None, 'msg_1']`
  - `test_catch_up_called_on_reconnect_not_initial_connect: assert 0 == 2`
  - `test_reconnect_catches_up_user_text_and_tool_items: AssertionError: assert [] == ['message', '...t', 'message']`
  - `test_catch_up_keeps_cursor_before_incomplete_message: AssertionError: assert None == 'msg_done'`
  - `test_fixture_messages_replay_on_catch_up: AssertionError: assert [] == ['I will run ls.', 'Done.']`
  Already passing (guards that the new code must keep true): `test_run_catches_up_on_reconnect_posts_gap_content`, `test_reconnect_does_not_repost_already_seeded_content`.

- [ ] **Step 3: Write minimal implementation**

Apply these edits to `omnigent/harnesses/opencode_native/forwarder.py` in order (bottom of the file first):

Add to the end of the `OpenCodeNativeForwarder` class body (after `_mark_history_message`, which ends at line 1688):

```python
    async def catch_up_from_history(self) -> None:
        """
        Replay history persisted after the last settled message.

        The live stream never replays missed events, so after a reconnect the
        forwarder re-reads ``GET /api/session/{id}/message`` past the cursor
        and feeds unseen content through the normal post paths; dedupe keys
        suppress anything already posted.
        """
        try:
            messages = await self._opencode.list_messages(
                self._opencode_session_id, after_id=self._last_seen_message_id
            )
        except Exception:  # noqa: BLE001 - catch-up is best effort.
            _logger.debug("OpenCode forwarder could not catch up from history", exc_info=True)
            return
        turn = self._turns[self._opencode_session_id]
        settled_prefix = True
        for message in messages:
            if not isinstance(message, Mapping):
                continue
            await self._replay_history_message(turn, message)
            message_id = _str_field(message, "id")
            if settled_prefix and message_id is not None and _history_message_settled(message):
                self._last_seen_message_id = message_id
            else:
                settled_prefix = False
        try:
            await self._post_session_usage()
        except Exception:  # noqa: BLE001 - usage re-post is best effort.
            _logger.debug(
                "OpenCode forwarder could not re-post usage after catch-up", exc_info=True
            )

    async def _replay_history_message(
        self, turn: _SessionTurn, message: Mapping[str, Any]
    ) -> None:
        """Post one history message's unseen user text, assistant text, and tools."""
        message_id = _str_field(message, "id")
        if message_id is None:
            return
        kind = message.get("type")
        if kind == "user":
            await self._post_user_payload(turn, message_id, message)
            return
        if kind != "assistant":
            return
        self._record_step_usage(message_id, message, _model_ref(message.get("model")))
        content = message.get("content")
        text_ordinal = 0
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, Mapping):
                continue
            if item.get("type") == "text":
                text = item.get("text")
                key = self._key("text-final", message_id, str(text_ordinal))
                if isinstance(text, str) and text and self.state.mark(key):
                    await self._post_assistant_text(
                        turn,
                        text,
                        message_id=message_id,
                        stream_id=self._stream_id(message_id, "text", text_ordinal),
                    )
                text_ordinal += 1
            elif item.get("type") == "tool":
                await self._replay_tool(turn, message_id, item)

    async def _replay_tool(
        self, turn: _SessionTurn, message_id: str, item: Mapping[str, Any]
    ) -> None:
        """Post a history tool's call and (when settled) its output."""
        call_id = _str_field(item, "id")
        state = item.get("state")
        if call_id is None or not isinstance(state, Mapping):
            return
        status = state.get("status")
        if status == "streaming":
            return
        name = _str_field(item, "name") or "tool"
        turn.tool_names[call_id] = name
        raw_input = state.get("input")
        arguments = dict(raw_input) if isinstance(raw_input, Mapping) else {}
        if self.state.mark(self._key("tool-call", call_id)):
            await self._post_tool_call(turn, call_id, name, arguments, message_id=message_id)
        if status == "completed" and self.state.mark(self._key("tool-out", call_id)):
            output = opencode_tool_content_text(state.get("content"))
            await self._post_tool_output(turn, call_id, output, message_id=message_id)
        elif status == "error" and self.state.mark(self._key("tool-out", call_id)):
            output = opencode_tool_content_text(state.get("content"), error=state.get("error"))
            await self._post_tool_output(turn, call_id, output, message_id=message_id)
```

Replace `run` (lines 537-569) with:

```python
    async def run(self, *, max_reconnects: int | None = None) -> None:
        """
        Run the SSE consume loop with reconnect/backoff and gap-fill.

        The first connection pre-marks persisted history; every reconnect
        replays history persisted after the last settled message.

        :param max_reconnects: Reconnect cap (``None`` = unbounded); used by
            tests to bound the loop.
        """
        attempt = 0
        backoff = 0.5
        try:
            while True:
                if attempt == 0:
                    await self.seed_dedupe_from_history()
                else:
                    await self.catch_up_from_history()
                try:
                    await self._consume_once()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 - reconnect on any transient SSE failure.
                    _logger.warning(
                        "OpenCode forwarder SSE error for session=%s; reconnecting",
                        self._session_id,
                        exc_info=True,
                    )
                attempt += 1
                if max_reconnects is not None and attempt > max_reconnects:
                    return
                await asyncio.sleep(min(backoff, 5.0))
                backoff = min(backoff * 2, 5.0)
        finally:
            await self._cancel_background_tasks()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_forwarder_reconnect.py -v -k "test_run_catches_up_on_reconnect_posts_gap_content or test_catch_up_passes_cursor_as_after_id or test_catch_up_called_on_reconnect_not_initial_connect or test_reconnect_catches_up_user_text_and_tool_items or test_catch_up_keeps_cursor_before_incomplete_message or test_reconnect_does_not_repost_already_seeded_content or test_fixture_messages_replay_on_catch_up"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `104 passed`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_forwarder_reconnect.py
git commit -m "feat(opencode-native): catch up v2 history after an SSE reconnect"
```

### Task 44: Full captured-turn replay

One integration test feeds every frame of `tests/fixtures/opencode_v2/events.ndjson` through the forwarder and checks cross-handler invariants: the turn opens with `running` and ends `idle`, every non-empty `session.text.ended` becomes exactly one assistant item in order, every `session.tool.called` gets a `function_call` in order plus an output, and every live text preview is retired by a final item with the same stream id. No production code changes; if it fails, fix the handler it points at.

**Files:**
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1760)

**Interfaces:**
- Consumes: Tasks 20-40; Stage 0 `load_events() -> list[dict]`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Also add `load_events` to the `from tests.opencode_v2_fixtures import ...` line.

Replace the import block `tests/test_opencode_native_forwarder.py:5-15` with:

```python
import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

import omnigent.harnesses.opencode_native.forwarder as fwd_mod
from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from tests.opencode_v2_fixtures import events_of_type, load_events
```

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- full fixture replay ----------------------------------------------------


async def test_full_fixture_turn_replays_consistently() -> None:
    """Every captured frame flows through the forwarder without contradictions."""
    server, opencode = _RecordingServerClient(), _FakeOpenCodeClient()
    fwd = _forwarder(server, opencode, opencode_session_id=_FIX_SESSION)
    for raw in load_events():
        await fwd.handle_event(_to_event(raw))
    await _drain(fwd)

    statuses = [e["status"] for e in _status_edges(server.posts)]
    assert statuses[0] == "running"
    assert statuses[-1] == "idle"

    ended_texts = [
        raw["data"]["text"]
        for raw in events_of_type("session.text.ended")
        if raw["data"]["sessionID"] == _FIX_SESSION and raw["data"]["text"]
    ]
    assistant_texts = [
        i["item_data"]["content"][0]["text"]
        for i in _items(server.posts)
        if i["item_type"] == "message" and i["item_data"]["role"] == "assistant"
    ]
    assert assistant_texts == ended_texts

    called_ids = [
        raw["data"]["id"]
        for raw in events_of_type("session.tool.called")
        if raw["data"]["sessionID"] == _FIX_SESSION
    ]
    call_items = [
        i["item_data"]["call_id"]
        for i in _items(server.posts)
        if i["item_type"] == "function_call"
    ]
    assert [call_id for call_id in call_items if call_id in called_ids] == called_ids
    outputs = {
        i["item_data"]["call_id"]
        for i in _items(server.posts)
        if i["item_type"] == "function_call_output"
    }
    assert set(called_ids) <= outputs

    stream_ids = {d["message_id"] for d in _datas(server.posts, "external_output_text_delta")}
    retired = {
        i["message_id"]
        for i in _items(server.posts)
        if i["item_type"] == "message" and i["item_data"]["role"] == "assistant"
    }
    assert stream_ids <= retired, "every live preview is retired by a final item"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_full_fixture_turn_replays_consistently"`

Expected: PASS already — this task adds regression coverage over Tasks 20-40 only. If it fails, the assertion names the handler to fix before committing.

- [ ] **Step 3: Write minimal implementation**

No production change.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k "test_full_fixture_turn_replays_consistently"`

Expected: PASS

Then run the whole forwarder suite: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py -q` — Expected: `105 passed`.

- [ ] **Step 5: Commit**

```bash
git add tests/test_opencode_native_forwarder.py
git commit -m "test(opencode-native): replay the captured v2 turn through the forwarder"
```

### Task 45: Port the web question e2e test to v2 forms

`tests/e2e_ui/approvals/test_opencode_question.py` drives the forwarder with a v1 `question.asked` event built as
`OpenCodeEvent(..., properties=..., raw={})` and a fake exposing `reply_question`
(lines 20-31, 52-76, 111). Both the event shape (Stage 1) and the handler (Task 23) are
gone, so the test no longer runs. Port it to `form.created` with a `multiselect` field —
the shape the v2 `question` tool emits (`packages/core/src/tool/plugin/question.ts:75-127`)
— and assert on `reply_form`.

**Files:**
- Modify: `tests/e2e_ui/approvals/test_opencode_question.py:1-111` (full rewrite)
- Test: `tests/e2e_ui/approvals/test_opencode_question.py`

**Interfaces:**
- Consumes: Tasks 35-37 (`_on_form_created`, `_form_tasks`, `form_answer`); the existing `seeded_session` / `page` fixtures from `tests/e2e_ui/conftest.py`.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

First run the unmodified file: `uv run pytest tests/e2e_ui/approvals/test_opencode_question.py -v` — it FAILS with
`TypeError: OpenCodeEvent.__init__() got an unexpected keyword argument 'properties'`
(Stage 1 event shape). Then replace the whole of `tests/e2e_ui/approvals/test_opencode_question.py` with:

```python
"""E2E: an OpenCode v2 question form round-trips through the web form."""

from __future__ import annotations

import asyncio
import contextlib
import threading
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.opencode_native.client import OpenCodeEvent
from omnigent.harnesses.opencode_native.forwarder import OpenCodeNativeForwarder

_FORM = '[data-testid="ask-user-question-form"]'
_SUBMIT = '[data-testid="ask-user-question-submit"]'


class _RecordingOpenCodeClient:
    """Record the form answer returned by the live web elicitation flow."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str, dict[str, Any]]] = []

    async def reply_form(self, session_id: str, form_id: str, answer: dict[str, Any]) -> bool:
        self.replies.append((session_id, form_id, answer))
        return True

    async def cancel_form(self, session_id: str, form_id: str) -> bool:
        return True


@pytest.mark.timeout(90)
def test_opencode_question_round_trips_through_web(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """form.created (multiselect) -> web checkboxes -> reply_form option values."""
    base_url, session_id = seeded_session
    opencode = _RecordingOpenCodeClient()
    result: dict[str, object] = {}

    async def _forward_question() -> None:
        async with httpx.AsyncClient(base_url=base_url, timeout=60.0) as server:
            forwarder = OpenCodeNativeForwarder(
                session_id=session_id,
                opencode_session_id="ses_e2e",
                opencode_client=opencode,  # type: ignore[arg-type]
                server_client=server,
            )
            await forwarder.handle_event(
                OpenCodeEvent(
                    id=None,
                    type="form.created",
                    data={
                        "form": {
                            "id": "frm_e2e",
                            "sessionID": "ses_e2e",
                            "title": "Choose tools",
                            "metadata": {"kind": "question"},
                            "fields": [
                                {
                                    "key": "q0",
                                    "type": "multiselect",
                                    "title": "Choose tools",
                                    "description": "Which tools should run?",
                                    "options": [
                                        {"value": "Tests", "label": "Tests"},
                                        {"value": "Lint", "label": "Lint"},
                                        {"value": "Build", "label": "Build"},
                                    ],
                                    "custom": True,
                                }
                            ],
                        }
                    },
                    location=None,
                )
            )
            task = forwarder._form_tasks["frm_e2e"]
            await task

    def _run_forwarder() -> None:
        try:
            asyncio.run(_forward_question())
        except Exception as exc:
            result["error"] = exc

    thread = threading.Thread(target=_run_forwarder, daemon=True)
    thread.start()
    try:
        page.goto(f"{base_url}/c/{session_id}")

        form = page.locator(_FORM)
        expect(form).to_be_visible(timeout=15_000)
        form.get_by_role("checkbox", name="Tests").check()
        form.get_by_role("checkbox", name="Lint").check()
        form.locator(_SUBMIT).click()
    finally:
        thread.join(timeout=5)
        if thread.is_alive():
            with contextlib.suppress(httpx.HTTPError):
                httpx.post(
                    f"{base_url}/v1/sessions/{session_id}/events",
                    json={
                        "type": "approval",
                        "data": {"elicitation_id": "frm_e2e", "action": "decline"},
                    },
                    timeout=10.0,
                ).raise_for_status()
            thread.join(timeout=30)

    assert not thread.is_alive(), "OpenCode form hook did not receive the web verdict"
    if "error" in result:
        raise AssertionError(f"forwarder failed: {result['error']}") from result["error"]
    assert opencode.replies == [("ses_e2e", "frm_e2e", {"q0": ["Tests", "Lint"]})]
```

- [ ] **Step 2: Run test to verify it fails**

The failing run is the Step 1 run of the old file. The replaced test targets behaviour
Tasks 35-37 already implemented, so there is no separate red phase for it; if Step 4
fails, the assertion names what the form path mis-mapped.

- [ ] **Step 3: Write minimal implementation**

No production change (Tasks 35-37 implement the path).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/e2e_ui/approvals/test_opencode_question.py -v`
Expected: PASS (`1 passed`). This builds the SPA on first run; add `--ui-skip-build` if
`web/dist` is current. The web card shows one checkbox question "Which tools should run?"
with Tests / Lint / Build; the test ticks Tests and Lint and the forwarder replies
`{"q0": ["Tests", "Lint"]}`.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e_ui/approvals/test_opencode_question.py
git commit -m "test(opencode-native): port the web question e2e to v2 forms"
```

### Task 46: Handler-table guard, v1 sweep, lint, and manual verification

Locks in the spec coverage (every section-3 event has a handler) and the v1 removal (no
v1 event name can come back), then runs the stage-wide checks and the manual checks a
human should do against a live 2.0.x server.

**Files:**
- Test: `tests/test_opencode_native_forwarder.py` (append after line 1814)

**Interfaces:**
- Consumes: Tasks 20-41.
- Produces: nothing new.

- [ ] **Step 1: Write the failing test**

Append to the end of `tests/test_opencode_native_forwarder.py`:

```python
# --- handler table ----------------------------------------------------------


def test_handler_table_covers_v2_events_and_drops_v1_names() -> None:
    """Every spec'd v2 event has a handler and no v1 event name survives."""
    required = {
        "session.status",
        "session.execution.started",
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
        "session.step.started",
        "session.step.ended",
        "session.text.delta",
        "session.text.ended",
        "session.reasoning.delta",
        "session.reasoning.ended",
        "session.tool.called",
        "session.tool.progress",
        "session.tool.success",
        "session.tool.failed",
        "session.usage.updated",
        "session.retry.scheduled",
        "session.compaction.started",
        "session.compaction.ended",
        "session.compaction.failed",
        "session.model.selected",
        "session.created",
        "permission.asked",
        "permission.replied",
        "form.created",
        "form.replied",
        "form.cancelled",
    }
    v1_names = {
        "message.updated",
        "message.part.updated",
        "message.part.delta",
        "session.idle",
        "session.error",
        "session.compacted",
        "session.next.compaction.started",
        "session.next.compaction.ended",
        "session.next.model.switched",
        "permission.v2.asked",
        "question.asked",
        "question.replied",
        "question.rejected",
    }
    assert required <= set(fwd_mod._HANDLERS)
    assert not v1_names & set(fwd_mod._HANDLERS)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_forwarder.py -v -k test_handler_table_covers_v2_events_and_drops_v1_names`
Expected: PASS already — this guard is written after the handlers exist. To see it bite,
temporarily delete the `"form.cancelled"` row from `_HANDLERS`: it FAILS with
`AssertionError: assert {...} <= {...}`; restore the row.

Then sweep for v1 leftovers:
Run: `rg -n "properties|message\.part|message\.updated|question\.|session\.idle|session\.error|permission\.v2|session\.next|reply_body|_question_tasks|\.events\(\)" omnigent/harnesses/opencode_native/forwarder.py`
Expected: no output.

- [ ] **Step 3: Write minimal implementation**

No production change. If the sweep printed anything, delete that v1 code path (it is
unreachable on v2) and re-run Step 2.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py tests/test_opencode_native_permissions.py -q`
Expected: forwarder files `106 passed`; the permissions file passes at its Stage 3 state.

Run: `uv run ruff check omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py tests/e2e_ui/approvals/test_opencode_question.py && uv run ruff format --check omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py tests/e2e_ui/approvals/test_opencode_question.py`
Expected: `All checks passed!` and `4 files already formatted`.

Run: `pre-commit run --files omnigent/harnesses/opencode_native/forwarder.py tests/test_opencode_native_forwarder.py tests/test_opencode_forwarder_reconnect.py tests/e2e_ui/approvals/test_opencode_question.py`
Expected: every hook `Passed`.

Manual check (after Stage 3 makes the harness launchable; record results in the PR Test
Plan): start `just dev`, create an `opencode-native` session in the web UI, then

1. Prompt "Think step by step, then list the files here with the shell tool." — the
   reasoning block and the answer text stream in word by word (no duplicate or frozen
   preview after the turn ends), a `shell` tool card appears with its output, and the cost
   badge updates.
2. Enable "Require Approval for File & Shell Operations"; prompt a shell command — an
   approval card appears; approve it on the web and the command runs. Repeat and answer the
   prompt in the tmux TUI instead — the web card disappears by itself.
3. Prompt "Ask me which indent style I prefer using the question tool." — a question card
   with Tabs/Spaces appears; pick one and the model continues with that answer. Repeat and
   answer in the TUI — the web card disappears.
4. Switch model with `/model` in the TUI — the web model pill follows.
5. Type `/compact` in the web UI — the "Compacting conversation…" marker shows and clears.
6. Prompt "Use a subagent to summarize README.md." — a child row appears in the Subagents
   panel and its transcript fills in.
7. Kill the runner's network briefly (or restart the runner) mid-turn — after reconnect the
   finished answer appears exactly once.

- [ ] **Step 5: Commit**

```bash
git add tests/test_opencode_native_forwarder.py
git commit -m "test(opencode-native): guard the v2 forwarder handler table"
```
## Stage 3: Config, plugin, policies, credentials (harness functional here)

Stage 3 of `designs/opencode-v2-native-harness.md` (section 4).
Branch: stacked on Stage 2. Tasks 47–63.

## Source-verified facts this stage relies on (OpenCode tag `v2.0.18`)

All paths are relative to the extracted copy `scratchpad/v2/packages/`.

| Fact | Evidence |
|---|---|
| Top-level v2 config keys: `model`, `permissions`, `mcp`, `instructions`, `plugins`, `providers` | `schema/src/config.ts:25-110` |
| `model` is `"provider/model[#variant]"` or `{providerID, model, variant?}` | `schema/src/config/model.ts:140-156` |
| `providers.<id>` = `{canonical?, name?, env?, package?, settings?, headers?, body?, models?}`; `settings` is open (`baseURL`, `apiKey`, …) | `schema/src/config/provider.ts:45-128` |
| The native OpenAI-compatible package is `@opencode/ai/providers/openai-compatible` and takes `settings.{baseURL, apiKey, provider}` | `core/src/provider.ts:93`, `ai/src/providers/openai-compatible.ts:17-22`, `core/src/plugin/provider/vllm.ts:41-48` |
| Config providers are activated unconditionally | `core/src/config/plugin/provider.ts:61-74` (`provider.activation = "enabled"`) |
| v1 `provider.<id>` maps to v2 as `npm → package "aisdk:<npm>"`, `options → settings` minus `headers`/`body`, `api → settings.baseURL` | `core/src/v1/config/migrate.ts:247-260`, `core/src/v1/config/provider-options.ts:11-27`, `core/src/provider.ts:24-26` |
| `mcp` = `{timeout?, servers?: {name: Local \| Remote}}`; Local = `{type:"local", command[], cwd?, environment?, disabled?, codemode?, timeout?:{startup?,catalog?,execution?}(ms)}`; Remote = `{type:"remote", url, headers?, oauth?: {...} \| false, disabled?, codemode?, timeout?}`. `codemode` defaults to true | `schema/src/config/mcp.ts:19-22`, `schema/src/mcp.ts:7-64` |
| v1 flat `mcp.<name>` entries are migrated: `enabled → disabled = !enabled`, `timeout n → {catalog:n, execution:n}` | `core/src/config/normalize.ts:244-294`, `core/src/v1/config/migrate.ts:202-227` |
| `permissions` is an ordered `[{action, resource, effect: allow\|deny\|ask}]`; the **last** matching rule wins; any resource hitting `deny` blocks first | `schema/src/permission.ts:55-66`, `core/src/permission.ts:89-99,160-177` |
| Config `permissions` are appended after built-in agent defaults (so ask-all overrides them); session `permissions` are merged after agent permissions | `core/src/config/plugin/agent.ts:84-92`, `core/src/permission.ts:156-161` |
| `instructions` is parsed as a string array of paths/URLs but **nothing in core consumes it** in 2.0.18; ambient instructions come from `AGENTS.md` in `Global.config` and the project walk | `core/src/config/normalize.ts:222-223`; `grep -rn "\.instructions\b" core/src` hits only session instruction *state*; `core/src/config/plugin/instruction.ts:36,64-68` (`join(global.config, "AGENTS.md")`) |
| `Global.config = OPENCODE_CONFIG_DIR ?? $XDG_CONFIG_HOME/opencode`; `Global.data = $XDG_DATA_HOME/opencode` | `util/src/global.ts:78-83`, `util/src/global-roots.ts:4-18` |
| Agent `system` **replaces** the default system prompt (so it is not used for Omnigent instructions) | `core/src/session/model-request.ts:84-88` |
| Plugin module contract: default export `{id: string, setup: function}` (Promise API) or `{id, effect}`; named/function exports are rejected with "Plugin must export a default definition…" | `core/src/plugin/module.ts:60-73,107-116` |
| `Plugin.define` is the identity function, so a plain object default export is equivalent and needs no `@opencode/plugin` import (a bare bridge-dir file cannot resolve that package: no `node_modules` above it) | `plugin/src/promise/plugin.ts:66-68` |
| A configured `plugins` entry that is an absolute **file** path is dropped with "configured plugin path must be a directory"; directories resolve `server(.js)` then `index(.js)` | `core/src/config/plugin/source.ts:148-157`, `plugin/src/host.ts:17-44` |
| Files under `$XDG_CONFIG_HOME/opencode/{plugin,plugins}/*.js` are auto-loaded (so a stale v1 `ucode-auth.js` there fails to load) | `core/src/plugin/source-directory.ts:7-33`, `core/src/config/plugin/source.ts:127-133` |
| The CLI is a Bun-compiled executable (`/home/jason/.opencode/bin/opencode`: ELF); plugin package gets `"type": "module"` so `.js` is ESM under Bun and Node | `file $(which opencode)`; `cli/package.json:5` |
| Hook names: `ctx.session.hook("prompt", cb)` with `{sessionID, messageID, prompt:{text, files?, agents?, skills?}, metadata?, delivery}`; `ctx.tool.hook("execute.after", cb)` with `{tool, sessionID, agent, messageID, id, input, status:"completed", result:{output?, content?, metadata?}}` or `{status:"error", error}` | `plugin/src/promise/session.ts:14-20,138-151`, `plugin/src/promise/tool.ts:217-233`, triggers `core/src/session/prompt.ts:40-51`, `core/src/tool.ts:128-156` |
| A throwing promise hook becomes a defect that aborts `SessionPrompt.prepare` (the prompt is rejected); `execute.after` result is read back after the hook (`afterEvent.result.content/output`) | `plugin/src/promise/adapter.ts:577-579,503-504`; `core/src/tool.ts:146-156` |
| Model HTTP hooks: `ctx.session.hook("http.request", cb, {providerID})` with mutable `request: Request`; `"http.response"` with `response: Response` | `plugin/src/promise/session.ts:72-87`, `plugin/src/promise/registration.ts:5-19`, `core/src/session/model-request.ts:295-316` |
| `permission.asked` payload = `{id, sessionID, action, resources: string[], save?, metadata?, source?: {type:"tool", messageID, id}, message?}` | `schema/src/permission.ts:16-44` |
| Action names (every `permission.assert` call site): `shell` (resources = parsed command strings, no metadata), `read`, `edit` (write/edit/patch tools), `external_directory`, `glob`/`grep` (resource = pattern, metadata.path), `webfetch` (resource = url), `websearch` (resource = query), `skill` (resource = skill id), `subagent` (resource = agent id), `question`, `opencode_list_mcp_resources`, `opencode_read_mcp_resource`, MCP tools `<server>_<tool>` (resources `["*"]`, metadata `{}`) | `core/src/tool/plugin/shell.ts:22,134-141`, `core/src/file-access.ts:141-164`, `core/src/tool/plugin/{edit.ts:180,write.ts:78,patch.ts:196,glob.ts:15,68,grep.ts:15,87,webfetch.ts:12,124,websearch.ts:12,49,skill.ts:10,51,subagent.ts:17,139,question.ts:64,mcp-resource.ts:36,65}`, `core/src/tool/mcp.ts:16-17,51-56`; v1→v2 rename table `core/src/v1/config/migrate.ts:117-122` |
| Legacy credential import reads `path.join(Global.data, "auth.json")` = `$XDG_DATA_HOME/opencode/auth.json`, once, inside a DB migration; api→key, oauth→oauth, wellknown→key | `core/src/database/migration/20260805200742_import_legacy_credentials.ts:34-103` |
| Credential store: SQLite `credential` table (`id, integration_id, label, value(json), connector_id, method_id, active, time_created, time_updated`); `value` is `{type:"key", key, metadata?}` or `{type:"oauth", methodID, refresh, access, expires, metadata?}` | `core/src/credential/sql.ts:5-14`, `schema/src/credential.ts:31-51`; verified read-only with `PRAGMA table_info(credential)` on `~/.local/share/opencode/opencode.db` |
| DB file = `path.resolve(Global.data, $OPENCODE_DB ?? "opencode.db")` (release channels) | `cli/src/database-path.ts:4-13` |
| Env-var provider credentials are resolved live from `process.env` for integrations with an `env` method | `core/src/integration.ts:367-371`, `core/src/config/plugin/provider.ts:35-40` |
| `POST /api/integration/{integrationID}/connect/key` body `{key, label?}` → 204 | `protocol/openapi.json` `integration.connect.key` |
| `opencode auth login` still exists in v2 | `opencode auth --help` (2.0.18): `login  log in to a provider` |

## Findings that change or sharpen spec section 4 (review before executing)

1. **`instructions` is not applied in 2.0.18.** The key is parsed but unused. This stage writes the
   raw author instructions to the per-session `$XDG_CONFIG_HOME/opencode/AGENTS.md` (the global
   instruction file core *does* read) and also lists that path under `instructions` so a future
   OpenCode that honours the key picks up the same file. `build_opencode_config(instructions=...)`
   therefore takes the **path** of that file (see Task 56/47). This resolves open item 1 in favour of
   neither `instructions` text nor a `synthetic` message.
2. **`plugins: ["<bridge>/omnigent-policy.js"]` would be silently dropped** (file paths must be
   directories). The plugin is written as a directory package `<bridge>/omnigent-policy/`
   (`package.json` + `server.js`) and the directory path is registered.
3. **ask-all in config can be overridden by a workspace `opencode.json`** that adds `allow` rules
   (project documents are appended after global ones). Stage 4 must also pass
   `permissions=ASK_ALL_PERMISSIONS` to `client.create_session(...)`: session rules merge last and
   win (`core/src/permission.ts:156-161`). This stage exports the constant; the call site
   (`orchestration.py:1692`, `client.create_session(...)`) belongs to Stage 4.
4. **`reply_permission(..., message="omnigent-policy")` turns every reject into a
   `CorrectedError` with feedback** (`core/src/permission.ts:258-262`), i.e. the model keeps going
   with the text "omnigent-policy". Stage 1's client default should send no message on reject, or a
   human-readable reason. Flagged for Stage 1/2 owners.
5. **v2-only logins live only in the SQLite DB.** Copying `auth.json` alone leaves a user who logged
   in with v2 unable to use those credentials in the per-session DB. Task 60 therefore merges the
   user's v2 `credential` rows (read-only) into the per-session `auth.json` in legacy shape so the
   one-time import migration loads them. The import runs only when the per-session DB is created, so
   re-logins after the first launch of a conversation do not propagate (documented limitation).
6. **The env-key fallback (`connect_provider_key`) is largely redundant**: v2 already resolves
   `env`-method credentials from `process.env` (fact table). It is implemented as specified (Task 61)
   but only for providers not already present in the seeded credentials. Recommend dropping it after
   the manual run confirms env keys work without it.
7. **Shell permission requests carry no command metadata** — only parsed command strings in
   `resources`. `command` is rebuilt as `"\n".join(resources)`. MCP tool permissions carry no
   arguments at all (`resources: ["*"]`, `metadata: {}`); argument-aware policies on MCP tools only
   see the tool name. The relay tools are still enforced server-side by the relay.
8. **ucode's generated `ucode-auth.js` is a v1 plugin** (named function export using the v1
   `config` hook to replace `options.fetch`) and is produced by the external, pinned ucode
   (`databricks/ucode@304e4a2`, `src/ucode/agents/opencode.py:57-126`). Omnigent cannot edit it, so
   Task 58 renders an Omnigent-owned v2 plugin that reuses ucode's `AUTH_COMMAND` (parsed out of the
   generated file) and stamps `Authorization` via `ctx.session.hook("http.request", …, {providerID})`.
   A 401 invalidates the cached token so the next retry re-mints (v2 hooks cannot re-issue a request
   in place).

## File map

| File | Change |
|---|---|
| `omnigent/harnesses/opencode_native/permissions.py` | v2-only request parsing, per-action policy arguments, drop v1 shapes and `reply_body` |
| `omnigent/runner/native/orchestration.py` | config assembly (`1491-1502`, `1504-1651`, `1657`) and evaluator argument forwarding (`1896-1904`) |
| `omnigent/policies/builtins/safety.py` | v2 action names; `skill` action in `block_skills` |
| `omnigent/harnesses/opencode_native/provider.py` | v2 provider/MCP/config builders, v1+v2 user-config merge, instructions file, managed-connect v2 port |
| `omnigent/harnesses/opencode_native/bridge.py` | plugin-package writer, v2 policy plugin, credential seeding, env-key fallback, `applied_model` |
| `omnigent/onboarding/opencode_auth.py` | v2 credential DB reader; readiness counts DB credentials |
| tests | `tests/test_opencode_native_permissions.py`, `tests/runner/test_opencode_policy_evaluator.py`, `tests/policies/builtins/test_safety.py`, `tests/test_opencode_native_provider.py`, `tests/test_opencode_native_bridge.py`, `tests/onboarding/test_opencode_auth.py` |

---

### Task 47: Parse v2 `permission.asked` only

**Files:**
- Modify: `omnigent/harnesses/opencode_native/permissions.py:1-103`
- Test: `tests/test_opencode_native_permissions.py:1-62`

**Interfaces:**
- Consumes: v2 event `data` from `OpenCodeEvent.data` (Stage 1), shape `{id, sessionID, action, resources: list[str], save?, metadata?, source?: {type, messageID, id}, message?}`.
- Produces:
  ```python
  @dataclass(frozen=True)
  class OpenCodePermissionRequest:
      request_id: str
      session_id: str | None
      action: str | None
      resources: list[str]
      metadata: JsonObject
      source: JsonObject | None        # {"type": "tool", "messageID": ..., "id": ...}
      message: str | None
      raw: JsonObject
      @property
      def tool_call_id(self) -> str | None      # source["id"]
      @property
      def message_id(self) -> str | None        # source["messageID"]
  PermissionRequest = OpenCodePermissionRequest   # alias for the cross-stage contract
  def parse_permission_request(data: Mapping[str, object]) -> OpenCodePermissionRequest | None
  ```

Delete (v1 only): the docstring paragraph at `permissions.py:1-14` describing `permission.v2.asked` /
`POST /permission/{requestID}/reply`; alias reads `requestID`/`request_id` (`:83`), `session_id`
(`:86`), `type`/`permission` action fallback (`:88`), `patterns` resources fallback (`:90`), and
string `source` (`:92,101`). Delete tests `test_parse_permission_request_v1_uses_permission_field`
(`tests/test_opencode_native_permissions.py:33-52`) and
`test_parse_permission_request_accepts_request_id_alias` (`:55-58`).

- [ ] **Step 1: Write the failing test**

Replace `tests/test_opencode_native_permissions.py:15-62` with:

```python
def _asked(**overrides: object) -> dict[str, object]:
    """A v2 ``permission.asked`` payload as captured from opencode 2.0.x."""
    data: dict[str, object] = {
        "id": "per_1",
        "sessionID": "ses_1",
        "action": "shell",
        "resources": ["rm -rf build"],
        "save": ["rm *"],
        "source": {"type": "tool", "messageID": "msg_1", "id": "call_1"},
    }
    data.update(overrides)
    return data


def test_parse_permission_request_reads_v2_fields() -> None:
    req = parse_permission_request(_asked(metadata={"filepath": "a.py"}, message="why"))
    assert req is not None
    assert req.request_id == "per_1"
    assert req.session_id == "ses_1"
    assert req.action == "shell"
    assert req.resources == ["rm -rf build"]
    assert req.metadata == {"filepath": "a.py"}
    assert req.source == {"type": "tool", "messageID": "msg_1", "id": "call_1"}
    assert req.tool_call_id == "call_1"
    assert req.message_id == "msg_1"
    assert req.message == "why"


def test_parse_permission_request_ignores_v1_fields() -> None:
    """v1 ``permission``/``patterns`` are no longer read: the action stays unset."""
    req = parse_permission_request(
        {"id": "per_v1", "sessionID": "ses_1", "permission": "bash", "patterns": ["ls"]}
    )
    assert req is not None
    assert req.action is None
    assert req.resources == []


def test_parse_permission_request_drops_non_string_resources() -> None:
    req = parse_permission_request(_asked(resources=["a.py", {"path": "b"}, 3]))
    assert req is not None
    assert req.resources == ["a.py"]


def test_parse_permission_request_without_source() -> None:
    req = parse_permission_request(_asked(source=None))
    assert req is not None
    assert req.source is None
    assert req.tool_call_id is None


def test_parse_permission_request_requires_id() -> None:
    assert parse_permission_request({"action": "shell"}) is None
    assert parse_permission_request({"requestID": "per_2", "action": "edit"}) is None
```

Also change the fixture in `test_normalize_for_policy_extracts_command_and_path` (`:65-82`) — it is
rewritten in Task 48, so for now delete that test body and leave the import.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_permissions.py -v`
Expected: FAIL — `AttributeError: 'OpenCodePermissionRequest' object has no attribute 'tool_call_id'`
and `assert 'bash' is None` in `test_parse_permission_request_ignores_v1_fields`.

- [ ] **Step 3: Write minimal implementation**

Replace `omnigent/harnesses/opencode_native/permissions.py:1-103` with:

```python
"""OpenCode permission normalization and policy/approval mapping.

OpenCode 2.x emits ``permission.asked`` for every tool call the ask-all
ruleset gates and accepts ``once`` / ``reject`` on
``POST /api/session/{id}/permission/{requestID}/reply``. This module turns a
request into a policy-evaluation input and maps the verdict back onto a
reply; an unmapped verdict yields no auto-reply (fail closed).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, TypeAlias

from omnigent.util.json_types import JsonObject as _JsonObject

OPENCODE_NATIVE_HARNESS = "opencode-native"

# Reply tokens the forwarder sends; ``always`` is never used (see decision_to_reply).
OpenCodeReply = Literal["once", "reject"]

# Omnigent-side normalized decisions used by the forwarder.
PolicyDecision = Literal["allow_once", "allow_always", "reject", "ask"]

_JsonMapping: TypeAlias = Mapping[str, object]


@dataclass(frozen=True)
class OpenCodePermissionRequest:
    """
    A normalized OpenCode ``Permission.Request``.

    :param request_id: Permission request id, e.g. ``"per_..."``.
    :param session_id: OpenCode session id, e.g. ``"ses_..."``.
    :param action: Permission action, e.g. ``"shell"``, ``"edit"``, or an MCP
        tool name such as ``"omnigent_sys_session_list"``.
    :param resources: Resource strings (command text, relative path, URL, pattern).
    :param metadata: Tool-supplied metadata (e.g. ``{"filepath": ..., "diff": ...}``).
    :param source: ``{"type": "tool", "messageID": ..., "id": ...}`` when raised by a tool.
    :param message: Optional message a permission hook attached.
    :param raw: The full payload.
    """

    request_id: str
    session_id: str | None
    action: str | None
    resources: list[str] = field(default_factory=list)
    metadata: _JsonObject = field(default_factory=dict)
    source: _JsonObject | None = None
    message: str | None = None
    raw: _JsonObject = field(default_factory=dict)

    @property
    def tool_call_id(self) -> str | None:
        """:returns: The originating tool call id (``source.id``), if any."""
        value = self.source.get("id") if self.source else None
        return value if isinstance(value, str) and value else None

    @property
    def message_id(self) -> str | None:
        """:returns: The originating assistant message id (``source.messageID``)."""
        value = self.source.get("messageID") if self.source else None
        return value if isinstance(value, str) and value else None


# Cross-stage contract name.
PermissionRequest = OpenCodePermissionRequest


def parse_permission_request(data: _JsonMapping) -> OpenCodePermissionRequest | None:
    """
    Parse a v2 ``permission.asked`` payload.

    :param data: The event ``data`` object (``Permission.Request``).
    :returns: Parsed request, or ``None`` when no ``id`` is present.
    """
    request_id = data.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    session_id = data.get("sessionID")
    action = data.get("action")
    resources = data.get("resources")
    metadata = data.get("metadata")
    source = data.get("source")
    message = data.get("message")
    return OpenCodePermissionRequest(
        request_id=request_id,
        session_id=session_id if isinstance(session_id, str) else None,
        action=action if isinstance(action, str) and action else None,
        resources=[item for item in resources if isinstance(item, str)]
        if isinstance(resources, list)
        else [],
        metadata={key: value for key, value in metadata.items() if isinstance(key, str)}
        if isinstance(metadata, Mapping)
        else {},
        source={key: value for key, value in source.items() if isinstance(key, str)}
        if isinstance(source, Mapping)
        else None,
        message=message if isinstance(message, str) and message else None,
        raw=dict(data),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_permissions.py -v`
Expected: PASS (the `test_decision_to_reply`/`test_reply_body` tests still pass; `reply_body` is
removed in Task 50).

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/permissions.py tests/test_opencode_native_permissions.py
git commit -m "refactor(opencode-native): parse v2 permission.asked only"
```

---

### Task 48: Per-action policy arguments from v2 resources

**Files:**
- Modify: `omnigent/harnesses/opencode_native/permissions.py:106-166` (`normalize_for_policy`, `_extract_resource_fields`)
- Test: `tests/test_opencode_native_permissions.py`

**Interfaces:**
- Consumes: `OpenCodePermissionRequest` (Task 47).
- Produces: `normalize_for_policy(request, *, omnigent_session_id: str, workspace: str | None) -> JsonObject`
  returning the existing keys (`harness, action, command, path, url, working_directory,
  opencode_session_id, omnigent_session_id, request_id, metadata`) **plus**
  `arguments: JsonObject` (the policy `data.arguments`), `resources: list[str]`,
  `tool_call_id: str | None`. Argument keys per action:

  | action | arguments |
  |---|---|
  | `shell` | `{"command": "\n".join(resources)}` |
  | `read`, `edit`, `external_directory` | `{"path": resources[0]}` (+ `"paths": resources` when >1) |
  | `glob`, `grep` | `{"pattern": resources[0]}` (+ `"path": metadata["path"]` when a string) |
  | `webfetch` | `{"url": resources[0]}` |
  | `websearch` | `{"query": resources[0]}` |
  | `skill` | `{"skill": resources[0]}` |
  | `subagent` | `{"agent": resources[0]}` |
  | anything else (MCP, `question`, …) | `{}` plus `"resources"` when not `["*"]` |

- [ ] **Step 1: Write the failing test**

Add `import pytest` to the top-of-file imports, then append to
`tests/test_opencode_native_permissions.py` (and delete the old
`test_normalize_for_policy_extracts_command_and_path` if Task 47 left a stub):

```python
@pytest.mark.parametrize(
    ("action", "resources", "metadata", "expected"),
    [
        ("shell", ["git status", "rm -rf x"], {}, {"command": "git status\nrm -rf x"}),
        ("read", ["src/a.py"], {}, {"path": "src/a.py"}),
        ("edit", ["a.py", "b.py"], {"filepath": "a.py, b.py"}, {"path": "a.py", "paths": ["a.py", "b.py"]}),
        ("external_directory", ["/etc/*"], {}, {"path": "/etc/*"}),
        ("grep", ["secret"], {"path": "src"}, {"pattern": "secret", "path": "src"}),
        ("glob", ["**/*.py"], {"path": None}, {"pattern": "**/*.py"}),
        ("webfetch", ["https://x.test"], {"url": "https://x.test"}, {"url": "https://x.test"}),
        ("websearch", ["opencode v2"], {}, {"query": "opencode v2"}),
        ("skill", ["deploy"], {}, {"skill": "deploy"}),
        ("subagent", ["explore"], {}, {"agent": "explore"}),
        ("omnigent_sys_session_list", ["*"], {}, {}),
        ("opencode_read_mcp_resource", ["srv:file://x"], {}, {"resources": ["srv:file://x"]}),
    ],
)
def test_normalize_for_policy_builds_action_arguments(
    action: str, resources: list[str], metadata: dict[str, object], expected: dict[str, object]
) -> None:
    req = parse_permission_request(
        {
            "id": "per_1",
            "sessionID": "ses_1",
            "action": action,
            "resources": resources,
            "metadata": metadata,
            "source": {"type": "tool", "messageID": "msg_1", "id": "call_9"},
        }
    )
    assert req is not None
    normalized = normalize_for_policy(req, omnigent_session_id="conv_1", workspace="/repo")
    assert normalized["arguments"] == expected
    assert normalized["action"] == action
    assert normalized["resources"] == resources
    assert normalized["tool_call_id"] == "call_9"
    assert normalized["harness"] == OPENCODE_NATIVE_HARNESS
    assert normalized["working_directory"] == "/repo"
    assert normalized["omnigent_session_id"] == "conv_1"
    assert normalized["opencode_session_id"] == "ses_1"


def test_normalize_for_policy_keeps_flat_command_path_url() -> None:
    """The flat keys stay for callers that predate ``arguments``."""
    shell = parse_permission_request({"id": "p", "action": "shell", "resources": ["ls"]})
    read = parse_permission_request({"id": "p", "action": "read", "resources": ["a.py"]})
    fetch = parse_permission_request({"id": "p", "action": "webfetch", "resources": ["https://u"]})
    assert shell and read and fetch
    assert normalize_for_policy(shell, omnigent_session_id="c", workspace=None)["command"] == "ls"
    assert normalize_for_policy(read, omnigent_session_id="c", workspace=None)["path"] == "a.py"
    assert normalize_for_policy(fetch, omnigent_session_id="c", workspace=None)["url"] == "https://u"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_permissions.py -k normalize -v`
Expected: FAIL with `KeyError: 'arguments'`.

- [ ] **Step 3: Write minimal implementation**

Replace `permissions.py:106-166` (`normalize_for_policy` and `_extract_resource_fields`) with:

```python
_PATH_ACTIONS = frozenset({"read", "edit", "external_directory"})
_PATTERN_ACTIONS = frozenset({"glob", "grep"})
# Actions whose single resource is the operand, keyed by the argument name policies read.
_SINGLE_RESOURCE_ARGUMENT = {
    "webfetch": "url",
    "websearch": "query",
    "skill": "skill",
    "subagent": "agent",
}


def policy_arguments(request: OpenCodePermissionRequest) -> _JsonObject:
    """
    Build policy ``data.arguments`` for a v2 permission request.

    :param request: The parsed permission request.
    :returns: Action-specific arguments, e.g. ``{"command": "ls"}`` for ``shell``.
    """
    action = request.action or ""
    resources = request.resources
    first = resources[0] if resources else None
    if action == "shell":
        return {"command": "\n".join(resources)} if resources else {}
    if action in _PATH_ACTIONS:
        if first is None:
            return {}
        arguments: _JsonObject = {"path": first}
        if len(resources) > 1:
            arguments["paths"] = list(resources)
        return arguments
    if action in _PATTERN_ACTIONS:
        if first is None:
            return {}
        arguments = {"pattern": first}
        search_path = request.metadata.get("path")
        if isinstance(search_path, str) and search_path:
            arguments["path"] = search_path
        return arguments
    key = _SINGLE_RESOURCE_ARGUMENT.get(action)
    if key is not None:
        return {key: first} if first is not None else {}
    if resources and resources != ["*"]:
        return {"resources": list(resources)}
    return {}


def normalize_for_policy(
    request: OpenCodePermissionRequest,
    *,
    omnigent_session_id: str,
    workspace: str | None,
) -> _JsonObject:
    """
    Build an Omnigent policy-evaluation input from a permission request.

    :param request: The normalized OpenCode permission request.
    :param omnigent_session_id: Owning Omnigent conversation id.
    :param workspace: Session working directory, when known.
    :returns: A flat dict; ``arguments`` is what the evaluator posts as ``data.arguments``.
    """
    arguments = policy_arguments(request)
    command = arguments.get("command")
    path = arguments.get("path")
    url = arguments.get("url")
    return {
        "harness": OPENCODE_NATIVE_HARNESS,
        "action": request.action,
        "arguments": arguments,
        "resources": list(request.resources),
        "command": command if isinstance(command, str) else None,
        "path": path if isinstance(path, str) else None,
        "url": url if isinstance(url, str) else None,
        "working_directory": workspace,
        "opencode_session_id": request.session_id,
        "omnigent_session_id": omnigent_session_id,
        "request_id": request.request_id,
        "tool_call_id": request.tool_call_id,
        "metadata": request.metadata,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_permissions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/permissions.py tests/test_opencode_native_permissions.py
git commit -m "feat(opencode-native): derive policy arguments from v2 permission resources"
```

---

### Task 49: Evaluator forwards `arguments`

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:1896-1904` (inside `_build_opencode_policy_evaluator._evaluate`)
- Test: `tests/runner/test_opencode_policy_evaluator.py`

Coordination: this touches `_build_opencode_policy_evaluator`, outside the config block. It is
isolated (one expression); Stage 4 should rebase onto it rather than re-edit it.

**Interfaces:**
- Consumes: `normalize_for_policy(...)["arguments"]` (Task 48).
- Produces: `PHASE_TOOL_CALL` body `data.arguments` = `normalized["arguments"]` (+ `metadata` when non-empty); falls back to the flat `command/path/url` keys when `arguments` is absent.

- [ ] **Step 1: Write the failing test**

Append to `tests/runner/test_opencode_policy_evaluator.py`:

```python
async def test_evaluator_posts_normalized_arguments() -> None:
    """v2 action arguments (e.g. a grep ``pattern``) reach the policy engine."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    await evaluate(
        {
            "action": "grep",
            "arguments": {"pattern": "secret", "path": "src"},
            "command": None,
            "path": "src",
            "url": None,
            "metadata": {},
        }
    )
    _url, body, _timeout = client.calls[0]
    assert body["event"]["data"] == {
        "name": "grep",
        "arguments": {"pattern": "secret", "path": "src"},
    }


async def test_evaluator_names_v2_shell_action() -> None:
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="c",
    )
    await evaluate({"action": "shell", "arguments": {"command": "ls"}, "metadata": {}})
    assert client.calls[0][1]["event"]["data"] == {"name": "shell", "arguments": {"command": "ls"}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_policy_evaluator.py -k "normalized_arguments" -v`
Expected: FAIL — `arguments` is `{"path": "src"}` (the `pattern` is dropped).

- [ ] **Step 3: Write minimal implementation**

In `orchestration.py`, replace the current lines `1896-1904`:

```python
    async def _evaluate(normalized: Mapping[str, object]) -> Mapping[str, object] | None:
        arguments: _JsonObject = {
            key: normalized[key]
            for key in ("command", "path", "url")
            if normalized.get(key) is not None
        }
        metadata = normalized.get("metadata")
        if isinstance(metadata, Mapping) and metadata:
            arguments.setdefault("metadata", dict(metadata))
```

with:

```python
    async def _evaluate(normalized: Mapping[str, object]) -> Mapping[str, object] | None:
        provided = normalized.get("arguments")
        if isinstance(provided, Mapping):
            arguments: _JsonObject = {str(key): value for key, value in provided.items()}
        else:
            arguments = {
                key: normalized[key]
                for key in ("command", "path", "url")
                if normalized.get(key) is not None
            }
        metadata = normalized.get("metadata")
        if isinstance(metadata, Mapping) and metadata:
            arguments.setdefault("metadata", dict(metadata))
```

Also update the docstring at `orchestration.py:1872-1874` ("every OpenCode ``permission.v2.asked``
request") to "every OpenCode ``permission.asked`` request", and the module docstring of
`tests/runner/test_opencode_policy_evaluator.py:4` the same way.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_policy_evaluator.py -v`
Expected: PASS (the existing `{"command": "ls"}` test still passes through the fallback branch).

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_policy_evaluator.py
git commit -m "feat(opencode-native): forward v2 permission arguments to policy evaluation"
```

---

### Task 50: Remove the v1 reply body

**Files:**
- Modify: `omnigent/harnesses/opencode_native/permissions.py:221-232` (delete `reply_body`)
- Test: `tests/test_opencode_native_permissions.py:114-119` (delete `test_reply_body`)

**Interfaces:**
- Consumes: Stage 1 `OpenCodeClient.reply_permission(session_id, request_id, decision)` builds the v2 body itself.
- Produces: nothing new; `reply_body` no longer exists.

- [ ] **Step 1: Write the failing test**

Replace `test_reply_body` (`tests/test_opencode_native_permissions.py:114-119`) with:

```python
def test_v1_reply_body_is_gone() -> None:
    """The v2 client owns the reply body (``{decision, message}``)."""
    import omnigent.harnesses.opencode_native.permissions as permissions

    assert not hasattr(permissions, "reply_body")
```

and drop `reply_body` from the import list at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_permissions.py::test_v1_reply_body_is_gone -v`
Expected: FAIL with `assert not True`.

- [ ] **Step 3: Write minimal implementation**

First confirm no caller remains (Stage 2 rewrote `forwarder.py:1004-1006` to call
`reply_permission(session_id, request.request_id, reply)`):

Run: `grep -rn "reply_body" omnigent tests`
Expected: only `permissions.py:221` (the definition). If `forwarder.py` still imports it, stop and
land Stage 2's forwarder permission handler first.

Delete `permissions.py:221-232` (the whole `reply_body` function) and update the
`decision_to_reply` docstring return line (`:211-212`) to:

```python
    :returns: ``"once"`` / ``"reject"``, or ``None`` for ``ask`` (no automatic
        reply — needs a human).
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_permissions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/permissions.py tests/test_opencode_native_permissions.py
git commit -m "refactor(opencode-native): drop v1 permission reply body"
```

---

### Task 51: v2 action names in built-in safety policies

**Files:**
- Modify: `omnigent/policies/builtins/safety.py:79-86`, `:262-265`, `:358-366`, `:432-441`
- Test: `tests/policies/builtins/test_safety.py:183-219`

**Interfaces:**
- Consumes: policy tool names = v2 permission actions (Task 48/42); arguments `command`, `path`, `pattern`, `skill`.
- Produces: `_OPENCODE_NATIVE_OS_TOOLS = frozenset({"shell", "edit", "read", "grep", "glob"})`; `block_skills` gates `tool == "skill"` with `arguments["skill"]`.

The complete v2 action inventory (fact table) is `shell, read, edit, external_directory, glob, grep,
webfetch, websearch, skill, subagent, question, opencode_list_mcp_resources,
opencode_read_mcp_resource, <server>_<tool>`. Only the file/shell ones belong in
`ask_on_os_tools`; `external_directory` is left out because it always precedes a `read`/`edit`/`shell`
request for the same operation and would double-prompt.

- [ ] **Step 1: Write the failing test**

Replace `tests/policies/builtins/test_safety.py:183-219` with:

```python
# ── ask_on_os_tools: opencode native permission actions ───────────────────────


@pytest.mark.parametrize(
    "tool,args,expected_preview",
    [
        ("shell", {"command": "rm -rf /"}, "rm -rf /"),
        ("read", {"path": "/etc/passwd"}, "/etc/passwd"),
        ("edit", {"path": "main.py"}, "main.py"),
        ("grep", {"pattern": "secret"}, "secret"),
        ("glob", {"pattern": "**/*.py"}, "**/*.py"),
    ],
    ids=["shell", "read", "edit", "grep", "glob"],
)
def test_ask_on_os_tools_asks_for_opencode_native_tools(
    tool: str,
    args: dict[str, str],
    expected_preview: str,
) -> None:
    """opencode 2.x permission actions trigger ASK via the forwarder's
    ``permission.asked`` → policy-evaluate path.

    :param tool: opencode permission action, e.g. ``"shell"``.
    :param args: Tool arguments dict.
    :param expected_preview: Substring that must appear in the reason.
    """
    result = ask_on_os_tools(tc(tool, args))
    assert result["result"] == "ASK"
    assert tool in result["reason"]
    assert expected_preview in result["reason"]


@pytest.mark.parametrize("tool", ["webfetch", "skill", "subagent", "question", "external_directory"])
def test_ask_on_os_tools_ignores_non_file_opencode_actions(tool: str) -> None:
    """Non file/shell opencode actions are not OS tools."""
    assert ask_on_os_tools(tc(tool, {}))["result"] == "ALLOW"


def test_opencode_os_tool_set_is_v2_action_names() -> None:
    from omnigent.policies.builtins.safety import _OPENCODE_NATIVE_OS_TOOLS

    assert frozenset({"shell", "edit", "read", "grep", "glob"}) == _OPENCODE_NATIVE_OS_TOOLS


def test_block_skills_blocks_opencode_skill_action() -> None:
    """opencode's ``skill`` permission action carries the skill id as ``skill``."""
    policy = block_skills(["deploy"])
    assert policy(tc("skill", {"skill": "deploy"}))["result"] == "DENY"
    assert policy(tc("skill", {"skill": "review"}))["result"] == "ALLOW"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/policies/builtins/test_safety.py -k "opencode" -v`
Expected: FAIL — `test_opencode_os_tool_set_is_v2_action_names` (set still contains `bash`) and
`test_block_skills_blocks_opencode_skill_action` (`ALLOW != DENY`). (`shell` already passes via the
codex set, which is why the set-equality test exists.)

- [ ] **Step 3: Write minimal implementation**

Replace `safety.py:79-86`:

```python
# opencode-native permission ACTIONS (``permission.asked`` ``action``, used as the
# policy tool name by the SSE forwarder). opencode 2.x names its shell tool
# ``shell`` and collapses write/edit/patch into ``edit``; listed explicitly so
# coverage does not depend on the overlapping pi / codex sets.
_OPENCODE_NATIVE_OS_TOOLS = frozenset({"shell", "edit", "read", "grep", "glob"})
```

Replace the docstring bullet at `safety.py:262-265`:

```python
    - **opencode native actions** (``shell``, ``edit``, ``read``,
      ``grep``, ``glob``) — opencode's permission actions, surfaced
      via the SSE forwarder's ``permission.asked`` → policy-evaluate
      path. opencode collapses write/edit/patch into ``edit``.
```

After `safety.py:366` (`_NATIVE_SKILL_TOOL = "Skill"`) add:

```python

# opencode's ``skill`` permission action; the forwarder passes the skill id as ``skill``.
_OPENCODE_SKILL_ACTION = "skill"
```

Replace `safety.py:432-441` (Path 2):

```python
            # Path 2: Claude Code / Codex native Skill tool, or opencode's
            # ``skill`` permission action; both carry the name as ``skill``.
            if tool in (_NATIVE_SKILL_TOOL, _OPENCODE_SKILL_ACTION):
                skill_name = args.get("skill")
                if skill_name and _is_blocked(skill_name):
                    return {
                        "result": "DENY",
                        "reason": f"Skill '{skill_name}' is blocked by policy",
                    }
                return _ALLOW
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/policies/builtins/test_safety.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/policies/builtins/safety.py tests/policies/builtins/test_safety.py
git commit -m "feat(opencode-native): gate v2 permission action names in safety policies"
```

---

### Task 52: v2 `providers.<id>` block for the gateway

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py:1-18` (module docstring), `:120-160` (delete `build_opencode_model_default_config`, replace `build_opencode_provider_config`)
- Test: `tests/test_opencode_native_provider.py:13-26`, `:79-131`

**Interfaces:**
- Consumes: `OpenCodeGatewayResolution` (unchanged, `provider.py:54-79`).
- Produces:
  ```python
  OPENCODE_CONFIG_SCHEMA: str = "https://opencode.ai/config.json"
  ASK_ALL_PERMISSIONS: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "ask"}]
  OPENAI_COMPATIBLE_PACKAGE: str = "@opencode/ai/providers/openai-compatible"
  def build_opencode_provider_block(resolution: OpenCodeGatewayResolution) -> dict[str, dict[str, object]]
  ```

Delete: `build_opencode_model_default_config` (`provider.py:120-135`) and
`build_opencode_provider_config` (`:138-160`) — replaced by `build_opencode_config` (Task 54).
Delete tests `test_build_model_default_config_pins_model_without_provider_block` (`:79-86`),
`test_model_default_config_round_trips_through_writer` (`:89-94`), `test_build_provider_config_shape`
(`:107-118`), and rewrite `test_write_provider_config_is_0600_and_valid_json` (`:121-130`).

- [ ] **Step 1: Write the failing test**

In `tests/test_opencode_native_provider.py`, replace the import block `:13-26` with:

```python
from omnigent.harnesses.opencode_native.provider import (
    ASK_ALL_PERMISSIONS,
    OPENAI_COMPATIBLE_PACKAGE,
    OpenCodeGatewayResolution,
    _gateway_endpoint_for_model,
    _strip_jsonc_comments,
    _strip_trailing_commas,
    build_opencode_omnigent_mcp_server,
    build_opencode_provider_block,
    managed_connect_opencode_config,
    maybe_merge_user_provider_config,
    resolve_databricks_gateway,
    write_opencode_provider_config,
)
```

Replace `:79-131` (from `test_build_model_default_config_pins_model_without_provider_block` through
`test_write_provider_config_is_0600_and_valid_json`) with:

```python
def test_qualified_model_joins_provider_and_endpoint() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="tok",
        model_id="databricks-claude-sonnet-4-6",
        provider_id="databricks-gateway",
    )
    assert res.qualified_model == "databricks-gateway/databricks-claude-sonnet-4-6"


def test_ask_all_permissions_is_single_wildcard_ask_rule() -> None:
    assert ASK_ALL_PERMISSIONS == [{"action": "*", "resource": "*", "effect": "ask"}]


def test_build_provider_block_is_v2_shape() -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints",
        api_key="sekret",
        model_id="databricks-claude-sonnet-4-6",
        model_ids=("databricks-claude-sonnet-4-6", "databricks-kimi-k3"),
    )
    block = build_opencode_provider_block(res)
    assert block == {
        "databricks-gateway": {
            "name": "Databricks AI Gateway",
            "package": OPENAI_COMPATIBLE_PACKAGE,
            "settings": {
                "baseURL": "https://ws/serving-endpoints",
                "apiKey": "sekret",
                "provider": "databricks-gateway",
            },
            "models": {
                "databricks-claude-sonnet-4-6": {"name": "databricks-claude-sonnet-4-6"},
                "databricks-kimi-k3": {"name": "databricks-kimi-k3"},
            },
        }
    }
    # No v1 keys.
    entry = block["databricks-gateway"]
    assert "npm" not in entry and "options" not in entry


def test_write_provider_config_is_0600_and_valid_json(tmp_path: Path) -> None:
    res = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints", api_key="tok", model_id="databricks-x"
    )
    path = write_opencode_provider_config(
        tmp_path, {"providers": build_opencode_provider_block(res)}
    )
    assert path == tmp_path / "opencode" / "opencode.json"
    # Token-bearing config must not be world/group readable.
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parsed = json.loads(path.read_text())
    assert parsed["providers"]["databricks-gateway"]["settings"]["apiKey"] == "tok"
```

(`test_qualified_model_joins_provider_and_endpoint` already exists at `:97-104`; keep a single copy.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -v`
Expected: FAIL at collection — `ImportError: cannot import name 'ASK_ALL_PERMISSIONS'`.

- [ ] **Step 3: Write minimal implementation**

Replace the module docstring `provider.py:1-18` with:

```python
"""Synthesize the OpenCode 2.x ``opencode.json`` for the native-server harness.

The runner-owned ``opencode serve`` reads its config from the per-session
``XDG_CONFIG_HOME``. This module emits the v2 keys: ``providers`` (an
OpenAI-compatible gateway declared with the native
``@opencode/ai/providers/openai-compatible`` package), ``model``
(``provider/model``), an ask-all ``permissions`` ruleset, ``mcp.servers``,
``plugins`` and ``instructions``.

Security: the file carries a bearer token, so it is written ``0600`` into the
per-session XDG dir (never the user's global ``~/.config/opencode``). The token
is resolved at spawn; a resumed session re-spawns the server and re-resolves, so
short-lived gateway tokens refresh on resume (a token that expires mid-session
is not refreshed in place).
"""
```

After `DATABRICKS_GATEWAY_DEFAULT_MODEL_ENV_VAR = ...` (`provider.py:51`) add:

```python
OPENCODE_CONFIG_SCHEMA = "https://opencode.ai/config.json"
# Native provider package bundled with opencode 2.x (no npm install at runtime).
OPENAI_COMPATIBLE_PACKAGE = "@opencode/ai/providers/openai-compatible"
# Every tool call raises ``permission.asked`` so the forwarder can apply Omnigent policy.
ASK_ALL_PERMISSIONS: list[dict[str, str]] = [{"action": "*", "resource": "*", "effect": "ask"}]
```

Replace `provider.py:120-160` with:

```python
def build_opencode_provider_block(
    resolution: OpenCodeGatewayResolution,
) -> dict[str, dict[str, object]]:
    """
    Build the ``providers`` entry for an OpenAI-compatible gateway.

    :param resolution: The resolved gateway (base URL + key + models).
    :returns: ``{provider_id: {name, package, settings, models}}``.
    """
    return {
        resolution.provider_id: {
            "name": resolution.provider_name,
            "package": OPENAI_COMPATIBLE_PACKAGE,
            "settings": {
                "baseURL": resolution.base_url,
                "apiKey": resolution.api_key,
                "provider": resolution.provider_id,
            },
            "models": {
                mid: {"name": mid} for mid in (resolution.model_ids or (resolution.model_id,))
            },
        }
    }
```

In `write_opencode_provider_config` (`provider.py:163-171`) change the docstring `:param config:`
line to `:param config: The v2 config dict (see :func:`build_opencode_config`).`

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "provider_block or ask_all or 0600 or qualified" -v`
Expected: PASS (other tests in the file still reference v1 shapes and are fixed in Tasks 46–51).

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): emit v2 providers block for the gateway"
```

---

### Task 53: v2 `mcp.servers` entries (relay with `codemode: false`)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py:188-313` (`build_opencode_mcp_block`, `build_opencode_omnigent_mcp_server`)
- Test: `tests/test_opencode_native_provider.py:38-56`, `:282-347`, `:662-692`

**Interfaces:**
- Consumes: `MCPServerConfig`-like objects (`name, transport, command, args, env, url, headers, databricks_profile, timeout`).
- Produces:
  ```python
  def build_opencode_mcp_block(servers: Sequence[MCPServerConfig]) -> dict[str, dict[str, object]]
      # values: {"type": "local", "command": [...], "environment"?: {...}, "codemode": False, "timeout"?: {"catalog": ms, "execution": ms}}
      #         {"type": "remote", "url": ..., "headers"?: {...}, "oauth"?: False, "codemode": False, "timeout"?: {...}}
  def build_opencode_omnigent_mcp_server(bridge_dir: Path, *, python_executable: str | None = None) -> dict[str, dict[str, object]]
      # {"omnigent": {"type": "local", "command": [...], "environment": {...}, "codemode": False,
      #               "timeout": {"execution": 360000}}}
  ```
  Both return the map that becomes `config["mcp"]["servers"]`. v1 `enabled: True` and integer
  `timeout` are removed. `oauth: False` is set on remote servers whose headers carry
  `Authorization`, so opencode does not start OAuth discovery for a bearer-authenticated server.
  All Omnigent-synthesized servers get `codemode: False` so each MCP tool keeps its name and raises
  its own `permission.asked` (open item 4); user-global MCP servers merged in Task 55 keep their own
  setting.

- [ ] **Step 1: Write the failing test**

Replace `tests/test_opencode_native_provider.py:38-56` with:

```python
def test_build_omnigent_mcp_server_points_serve_mcp_at_bridge_dir() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/bridge-xyz"))
    assert set(block) == {"omnigent"}
    entry = block["omnigent"]
    assert entry["type"] == "local"
    # Relay tools keep their names and are individually permission-gated.
    assert entry["codemode"] is False
    assert "enabled" not in entry
    # Milliseconds: must exceed the bridge's outer relay hop (330 s) so the
    # relay's clean timeout error beats opencode's client-side kill.
    assert entry["timeout"] == {"execution": 360_000}
    cmd = entry["command"]
    assert cmd[-3:] == ["serve-mcp", "--bridge-dir", "/tmp/bridge-xyz"]
    assert "omnigent.harnesses.claude_native.bridge" in cmd
    assert entry.get("environment", {}).get("PYTHONUNBUFFERED") == "1"


def test_build_omnigent_mcp_server_honors_python_executable() -> None:
    block = build_opencode_omnigent_mcp_server(Path("/tmp/b"), python_executable="/custom/python")
    assert block["omnigent"]["command"][0] == "/custom/python"
```

Replace the two assertions at the end of `test_build_mcp_block_stdio_and_http` (`:314-325`) with:

```python
    assert block["gh"] == {
        "type": "local",
        "command": ["npx", "-y", "server-github"],
        "codemode": False,
        "environment": {"GITHUB_TOKEN": "x"},
    }
    assert block["remote"] == {
        "type": "remote",
        "url": "https://mcp.example/sse",
        "codemode": False,
        "headers": {"X-Key": "k"},
    }
```

In `test_build_mcp_block_http_databricks_injects_bearer` (`:327-347`), after its last line
`assert block["dbx"]["headers"] == {"Authorization": "Bearer tok123"}` add:

```python
    assert block["dbx"]["oauth"] is False  # bearer header → no OAuth discovery
    assert block["dbx"]["codemode"] is False
```

Replace the final assertions of `test_build_mcp_block_preserves_custom_timeout` (`:690-692`) with:

```python
    # MCPServerConfig.timeout is seconds; v2 timeouts are {catalog, execution} in ms.
    assert block["local_custom"]["timeout"] == {"catalog": 120_000, "execution": 120_000}
    assert block["remote_custom"]["timeout"] == {"catalog": 45_500, "execution": 45_500}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "mcp" -v`
Expected: FAIL — `KeyError: 'codemode'` / `assert 360000 == {'execution': 360000}`.

- [ ] **Step 3: Write minimal implementation**

Replace `provider.py:188-241` (`build_opencode_mcp_block`) with:

```python
def _mcp_timeout(seconds: object) -> dict[str, int] | None:
    """Convert an ``MCPServerConfig.timeout`` in seconds to v2 ``{catalog, execution}`` ms."""
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    millis = int(seconds * 1000)
    return {"catalog": millis, "execution": millis}


def build_opencode_mcp_block(
    servers: Sequence[MCPServerConfig],
) -> dict[str, dict[str, object]]:
    """
    Translate Omnigent MCP server declarations into v2 ``mcp.servers`` entries.

    ``stdio`` → ``{type:"local", command:[cmd, *args], environment}``; ``http`` →
    ``{type:"remote", url, headers}``. A ``databricks_profile`` resolves a bearer
    token into ``Authorization`` at spawn. Every entry sets ``codemode: false`` so
    each tool keeps its name and is individually permission-gated. Entries
    without a command / url are skipped.

    :param servers: The agent spec's ``mcp_servers``.
    :returns: A ``mcp.servers`` map keyed by server name.
    """
    block: dict[str, dict[str, object]] = {}
    for server in servers:
        name = getattr(server, "name", None)
        if not name:
            continue
        if getattr(server, "transport", "http") == "stdio":
            command = getattr(server, "command", None)
            if not command:
                continue
            entry: dict[str, object] = {
                "type": "local",
                "command": [command, *getattr(server, "args", [])],
                "codemode": False,
            }
            env = dict(getattr(server, "env", {}) or {})
            if env:
                entry["environment"] = env
        else:
            url = getattr(server, "url", None)
            if not url:
                continue
            headers = dict(getattr(server, "headers", {}) or {})
            profile = getattr(server, "databricks_profile", None)
            if profile and "Authorization" not in headers:
                token = _databricks_bearer_token(profile)
                if token:
                    headers["Authorization"] = f"Bearer {token}"
            entry = {"type": "remote", "url": url, "codemode": False}
            if headers:
                entry["headers"] = headers
            if "Authorization" in headers:
                entry["oauth"] = False
        timeout = _mcp_timeout(getattr(server, "timeout", None))
        if timeout is not None:
            entry["timeout"] = timeout
        block[str(name)] = entry
    return block
```

In `build_opencode_omnigent_mcp_server` replace the docstring `:returns:` line (`:265`) with
`:returns: A one-entry ``mcp.servers`` map ``{"omnigent": {type:"local", codemode: False, …}}``.`
and replace the `entry` literal at `provider.py:289-301` with:

```python
    entry: dict[str, object] = {
        "type": "local",
        "command": [command, *args],
        # Relay tools keep their names so each call raises its own permission.asked.
        "codemode": False,
        # Execution deadline in ms: longer than the bridge's outer relay hop so the
        # relay's own timeout error arrives before opencode kills the call.
        "timeout": {"execution": int((_TOOL_RELAY_POST_TIMEOUT_S + 30.0) * 1000)},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "mcp" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): emit v2 mcp.servers entries with codemode disabled"
```

---

### Task 54: `build_opencode_config`

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py` (add after `build_opencode_provider_block`)
- Test: `tests/test_opencode_native_provider.py`

**Interfaces:**
- Consumes: `build_opencode_provider_block` (Task 52), `mcp.servers` map (Task 53), plugin dirs (Tasks 50/51), instructions file path (Task 56).
- Produces:
  ```python
  def build_opencode_config(
      *,
      model: str | None,
      gateway: OpenCodeGatewayResolution | None,
      mcp_servers: Mapping[str, Mapping[str, object]],
      plugin_paths: Sequence[str],
      instructions: str | None,          # path of the written AGENTS.md (see Finding 1)
      permissions: Sequence[Mapping[str, str]] | None = None,
      extra_providers: Mapping[str, Mapping[str, object]] | None = None,
  ) -> dict[str, object]
  ```
  Rules: `permissions` always starts with `ASK_ALL_PERMISSIONS`; caller rules are kept only when
  `effect == "deny"` and are appended after it (an `allow`/`ask` rule after ask-all would let a tool
  run without `permission.asked`). A gateway wins over `model` and `extra_providers` with the same
  id. `model` without a `/` is dropped (v2 only accepts `provider/model`).

- [ ] **Step 1: Write the failing test**

Add `build_opencode_config` to the `provider` import block at the top of
`tests/test_opencode_native_provider.py`, then append:

```python
def test_build_opencode_config_minimal_is_ask_all() -> None:
    cfg = build_opencode_config(
        model=None, gateway=None, mcp_servers={}, plugin_paths=[], instructions=None
    )
    assert cfg == {
        "$schema": "https://opencode.ai/config.json",
        "permissions": [{"action": "*", "resource": "*", "effect": "ask"}],
    }


def test_build_opencode_config_full_v2_shape() -> None:
    gateway = OpenCodeGatewayResolution(
        base_url="https://ws/serving-endpoints", api_key="tok", model_id="databricks-x"
    )
    cfg = build_opencode_config(
        model="anthropic/claude-sonnet-4-5",
        gateway=gateway,
        mcp_servers={"omnigent": {"type": "local", "command": ["py"], "codemode": False}},
        plugin_paths=["/b/ucode-auth", "/b/omnigent-policy", "/b/omnigent-policy"],
        instructions="/x/opencode/AGENTS.md",
    )
    # The gateway pins the model to its own provider.
    assert cfg["model"] == "databricks-gateway/databricks-x"
    assert set(cfg["providers"]) == {"databricks-gateway"}
    assert cfg["mcp"] == {
        "servers": {"omnigent": {"type": "local", "command": ["py"], "codemode": False}}
    }
    assert cfg["plugins"] == ["/b/ucode-auth", "/b/omnigent-policy"]
    assert cfg["instructions"] == ["/x/opencode/AGENTS.md"]
    for v1_key in ("provider", "permission", "plugin"):
        assert v1_key not in cfg


def test_build_opencode_config_keeps_only_deny_rules_after_ask_all() -> None:
    cfg = build_opencode_config(
        model="openai/gpt-5.5",
        gateway=None,
        mcp_servers={},
        plugin_paths=[],
        instructions=None,
        permissions=[
            {"action": "shell", "resource": "rm *", "effect": "deny"},
            {"action": "read", "resource": "*", "effect": "allow"},
        ],
    )
    assert cfg["permissions"] == [
        {"action": "*", "resource": "*", "effect": "ask"},
        {"action": "shell", "resource": "rm *", "effect": "deny"},
    ]
    assert cfg["model"] == "openai/gpt-5.5"


def test_build_opencode_config_extra_providers_and_bad_model() -> None:
    cfg = build_opencode_config(
        model="big-pickle",
        gateway=None,
        mcp_servers={},
        plugin_paths=[],
        instructions=None,
        extra_providers={"databricks-oss": {"package": "aisdk:@ai-sdk/openai"}},
    )
    assert cfg["providers"] == {"databricks-oss": {"package": "aisdk:@ai-sdk/openai"}}
    assert "model" not in cfg  # not provider/model
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k build_opencode_config -v`
Expected: FAIL — `ImportError: cannot import name 'build_opencode_config'`.

- [ ] **Step 3: Write minimal implementation**

Add after `build_opencode_provider_block` in `provider.py`:

```python
def _permission_rules(extra: Sequence[Mapping[str, str]] | None) -> list[dict[str, str]]:
    """Ask-all first, then caller ``deny`` rules; other effects would bypass the gate."""
    rules = [dict(rule) for rule in ASK_ALL_PERMISSIONS]
    for rule in extra or ():
        if rule.get("effect") == "deny":
            rules.append(dict(rule))
        else:
            _logger.info("opencode config: dropping non-deny permission rule %r", dict(rule))
    return rules


def build_opencode_config(
    *,
    model: str | None,
    gateway: OpenCodeGatewayResolution | None,
    mcp_servers: Mapping[str, Mapping[str, object]],
    plugin_paths: Sequence[str],
    instructions: str | None,
    permissions: Sequence[Mapping[str, str]] | None = None,
    extra_providers: Mapping[str, Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """
    Build the per-session v2 ``opencode.json``.

    :param model: Default ``provider/model``; replaced by the gateway's model when set.
    :param gateway: Resolved OpenAI-compatible gateway, or ``None``.
    :param mcp_servers: ``mcp.servers`` map (see :func:`build_opencode_mcp_block`).
    :param plugin_paths: Plugin package directories, in load order.
    :param instructions: Path of the instructions file (the per-session ``AGENTS.md``).
    :param permissions: Extra rules; only ``deny`` rules are kept, after ask-all.
    :param extra_providers: Already-v2 provider entries (e.g. the managed ucode config).
    :returns: The config dict.
    """
    config: dict[str, object] = {
        "$schema": OPENCODE_CONFIG_SCHEMA,
        "permissions": _permission_rules(permissions),
    }
    providers: dict[str, object] = {k: dict(v) for k, v in (extra_providers or {}).items()}
    if gateway is not None:
        providers.update(build_opencode_provider_block(gateway))
        model = gateway.qualified_model
    if providers:
        config["providers"] = providers
    if model and "/" in model:
        config["model"] = model
    elif model:
        _logger.info("opencode config: ignoring model %r without a provider prefix", model)
    if mcp_servers:
        config["mcp"] = {"servers": {name: dict(entry) for name, entry in mcp_servers.items()}}
    if plugin_paths:
        config["plugins"] = list(dict.fromkeys(plugin_paths))
    if instructions:
        config["instructions"] = [instructions]
    return config
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -k build_opencode_config -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): add v2 opencode.json builder with ask-all permissions"
```

---

### Task 55: Merge the user's global config (v1 and v2 keys → v2)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py:549-661` (`maybe_merge_user_provider_config`)
- Test: `tests/test_opencode_native_provider.py:380-605`, `:640-660`

**Interfaces:**
- Consumes: `user_opencode_config_path()` (`bridge.py:459-477`), `_strip_jsonc_comments`, `_strip_trailing_commas` (unchanged).
- Produces:
  ```python
  def maybe_merge_user_provider_config(config: dict[str, object]) -> dict[str, object]  # always v2 keys
  def v1_provider_to_v2(entry: Mapping[str, object]) -> dict[str, object]              # reused by Task 58
  ```
  Reads user `provider` (v1, converted per `core/src/v1/config/migrate.ts:247-260,304-354`) and
  `providers` (v2; wins over v1 for the same id, as `normalize.ts:168-177` does), `model` (string or
  `{providerID, model, variant?}`), `plugin` (v1: string or `[name, options]`) and `plugins` (v2:
  string or `{package, options?}`), `mcp` flat v1 entries and `mcp.servers` (v2). Synthesized values
  always win; user entries fill gaps; plugins keep synthesized order first.

- [ ] **Step 1: Write the failing test**

Replace the merge tests `tests/test_opencode_native_provider.py:380-605` and `:640-660` with:

```python
def _user_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str) -> None:
    cfg_dir = tmp_path / "cfg" / "opencode"
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "opencode.jsonc").write_text(text, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))


def test_merge_user_provider_config_noop_without_user_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nonexistent"))
    config = {"model": "anthropic/claude-sonnet-4-5"}
    assert maybe_merge_user_provider_config(config) == config


def test_merge_converts_v1_provider_to_v2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"provider": {"my-openai": {"npm": "@ai-sdk/openai-compatible", "name": "Mine", '
        '"options": {"baseURL": "https://gw/v1", "apiKey": "sk-", "headers": {"X-A": "1"}}, '
        '"models": {"gpt-4": {"name": "gpt-4", "id": "gpt-4-0613"}}}}}',
    )
    result = maybe_merge_user_provider_config({})
    assert "provider" not in result
    assert result["providers"]["my-openai"] == {
        "name": "Mine",
        "package": "aisdk:@ai-sdk/openai-compatible",
        "settings": {"baseURL": "https://gw/v1", "apiKey": "sk-"},
        "headers": {"X-A": "1"},
        "models": {"gpt-4": {"name": "gpt-4", "modelID": "gpt-4-0613"}},
    }
    assert result["$schema"] == "https://opencode.ai/config.json"


def test_merge_reads_v2_providers_and_v2_wins_over_v1(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"provider": {"p": {"npm": "@ai-sdk/openai"}}, '
        '"providers": {"p": {"package": "@opencode/ai/providers/openai"}, '
        '"q": {"settings": {"baseURL": "https://q"}}}}',
    )
    result = maybe_merge_user_provider_config({})
    assert result["providers"]["p"] == {"package": "@opencode/ai/providers/openai"}
    assert result["providers"]["q"] == {"settings": {"baseURL": "https://q"}}


def test_merge_does_not_clobber_synthesized_providers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch, tmp_path, '{"providers": {"databricks-gateway": {"name": "user"}}}'
    )
    config: dict[str, object] = {"providers": {"databricks-gateway": {"name": "synth"}}}
    result = maybe_merge_user_provider_config(config)
    assert result["providers"]["databricks-gateway"] == {"name": "synth"}


def test_merge_adopts_user_model_only_when_unset(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(monkeypatch, tmp_path, '{"model": {"providerID": "anthropic", "model": "c-4"}}')
    assert maybe_merge_user_provider_config({})["model"] == "anthropic/c-4"
    assert maybe_merge_user_provider_config({"model": "openai/g"})["model"] == "openai/g"


def test_merge_adopts_user_string_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(monkeypatch, tmp_path, '{"model": "databricks/databricks-claude-opus-4-8"}')
    assert (
        maybe_merge_user_provider_config({})["model"] == "databricks/databricks-claude-opus-4-8"
    )


def test_merge_plugins_v1_and_v2_after_synthesized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"plugin": ["/opt/a", ["pkg-b", {"k": 1}], "", 42], '
        '"plugins": ["/opt/a", {"package": "pkg-c"}, {"bad": 1}]}',
    )
    result = maybe_merge_user_provider_config({"plugins": ["/b/omnigent-policy", "/opt/a"]})
    assert "plugin" not in result
    assert result["plugins"] == [
        "/b/omnigent-policy",
        "/opt/a",
        {"package": "pkg-b", "options": {"k": 1}},
        {"package": "pkg-c"},
    ]


def test_merge_mcp_v1_flat_and_v2_servers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{"mcp": {"legacy": {"type": "local", "command": ["x"], "enabled": false, "timeout": 5000},'
        ' "omnigent": {"type": "local", "command": ["user"]},'
        ' "servers": {"modern": {"type": "remote", "url": "https://m"}}}}',
    )
    config: dict[str, object] = {"mcp": {"servers": {"omnigent": {"type": "local", "command": ["r"]}}}}
    result = maybe_merge_user_provider_config(config)
    servers = result["mcp"]["servers"]
    assert servers["omnigent"] == {"type": "local", "command": ["r"]}  # synthesized wins
    assert servers["legacy"] == {
        "type": "local",
        "command": ["x"],
        "disabled": True,
        "timeout": {"catalog": 5000, "execution": 5000},
    }
    assert servers["modern"] == {"type": "remote", "url": "https://m"}


def test_merge_user_provider_config_handles_jsonc_comments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _user_config(
        monkeypatch,
        tmp_path,
        '{\n  // comment\n  "providers": {"p": {"settings": {"baseURL": "https://x/v1"}}}, /* b */\n}',
    )
    assert maybe_merge_user_provider_config({})["providers"]["p"]["settings"]["baseURL"] == (
        "https://x/v1"
    )
```

Keep the `_strip_*` unit tests (`:349-377`, `:607-638`) unchanged.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k merge -v`
Expected: FAIL — e.g. `KeyError: 'providers'` in `test_merge_converts_v1_provider_to_v2`.

- [ ] **Step 3: Write minimal implementation**

Replace `provider.py:549-661` with:

```python
_AISDK_PREFIX = "aisdk:"


def _read_user_opencode_config() -> dict[str, object] | None:
    """Parse the user's global ``opencode.json(c)``; ``None`` when absent or invalid."""
    from omnigent.harnesses.opencode_native.bridge import user_opencode_config_path

    user_path = user_opencode_config_path()
    if user_path is None:
        return None
    try:
        raw = user_path.read_text(encoding="utf-8")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = json.loads(_strip_trailing_commas(_strip_jsonc_comments(raw)))
    except (OSError, UnicodeDecodeError):
        return None
    except json.JSONDecodeError:
        _logger.warning("Failed to parse user OpenCode config at %s — ignoring it", user_path)
        return None
    return parsed if isinstance(parsed, dict) else None


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {str(k): v for k, v in value.items() if isinstance(v, str)}


def _v1_model_to_v2(model: Mapping[str, object]) -> dict[str, object]:
    """Subset of opencode's v1 model migration (``migrate.ts:304-354``)."""
    out: dict[str, object] = {}
    if isinstance(model.get("name"), str):
        out["name"] = model["name"]
    if isinstance(model.get("id"), str):
        out["modelID"] = model["id"]
    headers = _string_map(model.get("headers"))
    if headers:
        out["headers"] = headers
    options = model.get("options")
    if isinstance(options, Mapping) and options:
        out["settings"] = dict(options)
    limit = model.get("limit")
    if isinstance(limit, Mapping):
        limits = {
            key: int(limit[key])
            for key in ("context", "input", "output")
            if isinstance(limit.get(key), (int, float)) and not isinstance(limit.get(key), bool)
        }
        if limits:
            out["limit"] = limits
    return out


def v1_provider_to_v2(entry: Mapping[str, object]) -> dict[str, object]:
    """
    Convert a v1 ``provider.<id>`` entry to a v2 ``providers.<id>`` entry.

    Mirrors opencode's own migration (``migrate.ts:247-260``): ``npm`` becomes an
    ``aisdk:``-prefixed ``package``, ``options`` becomes ``settings`` with
    ``headers``/``body`` lifted out, and ``api`` becomes ``settings.baseURL``.

    :param entry: The v1 provider object.
    :returns: The v2 provider object.
    """
    out: dict[str, object] = {}
    if isinstance(entry.get("name"), str):
        out["name"] = entry["name"]
    env = entry.get("env")
    if isinstance(env, list) and all(isinstance(item, str) for item in env):
        out["env"] = list(env)
    npm = entry.get("npm")
    if isinstance(npm, str) and npm:
        out["package"] = npm if npm.startswith(_AISDK_PREFIX) else _AISDK_PREFIX + npm
    options = entry.get("options")
    options = options if isinstance(options, Mapping) else {}
    settings = {str(k): v for k, v in options.items() if k not in ("headers", "body")}
    if isinstance(entry.get("api"), str):
        settings["baseURL"] = entry["api"]
    if settings:
        out["settings"] = settings
    headers = _string_map(options.get("headers"))
    if headers:
        out["headers"] = headers
    body = options.get("body")
    if isinstance(body, Mapping) and body:
        out["body"] = dict(body)
    models = entry.get("models")
    if isinstance(models, Mapping):
        out["models"] = {
            str(mid): _v1_model_to_v2(model)
            for mid, model in models.items()
            if isinstance(model, Mapping)
        }
    return out


def _user_providers(user: Mapping[str, object]) -> dict[str, object]:
    providers: dict[str, object] = {}
    legacy = user.get("provider")
    if isinstance(legacy, Mapping):
        for pid, entry in legacy.items():
            if isinstance(entry, Mapping):
                providers[str(pid)] = v1_provider_to_v2(entry)
    native = user.get("providers")
    if isinstance(native, Mapping):
        for pid, entry in native.items():
            if isinstance(entry, Mapping):
                providers[str(pid)] = dict(entry)
    return providers


def _user_model(user: Mapping[str, object]) -> str | None:
    model = user.get("model")
    if isinstance(model, str) and "/" in model:
        return model
    if isinstance(model, Mapping):
        provider_id, model_id = model.get("providerID"), model.get("model")
        if isinstance(provider_id, str) and isinstance(model_id, str):
            variant = model.get("variant")
            suffix = f"#{variant}" if isinstance(variant, str) and variant else ""
            return f"{provider_id}/{model_id}{suffix}"
    return None


def _user_plugins(user: Mapping[str, object]) -> list[object]:
    plugins: list[object] = []
    legacy = user.get("plugin")
    for item in legacy if isinstance(legacy, list) else []:
        if isinstance(item, str) and item:
            plugins.append(item)
        elif (
            isinstance(item, list)
            and len(item) == 2
            and isinstance(item[0], str)
            and isinstance(item[1], Mapping)
        ):
            plugins.append({"package": item[0], "options": dict(item[1])})
    native = user.get("plugins")
    for item in native if isinstance(native, list) else []:
        if isinstance(item, str) and item:
            plugins.append(item)
        elif isinstance(item, Mapping) and isinstance(item.get("package"), str):
            plugins.append(dict(item))
    return plugins


def _v1_mcp_to_v2(entry: Mapping[str, object]) -> dict[str, object]:
    """Subset of opencode's v1 MCP migration (``migrate.ts:202-227``)."""
    out = {str(k): v for k, v in entry.items() if k not in ("enabled", "timeout")}
    enabled = entry.get("enabled")
    if isinstance(enabled, bool):
        out["disabled"] = not enabled
    timeout = entry.get("timeout")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        out["timeout"] = {"catalog": timeout, "execution": timeout}
    return out


def _user_mcp_servers(user: Mapping[str, object]) -> dict[str, object]:
    mcp = user.get("mcp")
    if not isinstance(mcp, Mapping):
        return {}
    servers: dict[str, object] = {}
    for name, entry in mcp.items():
        if not isinstance(entry, Mapping):
            continue
        if entry.get("type") in ("local", "remote"):
            servers[str(name)] = _v1_mcp_to_v2(entry)
    native = mcp.get("servers")
    if isinstance(native, Mapping) and native.get("type") not in ("local", "remote"):
        for name, entry in native.items():
            if isinstance(entry, Mapping):
                servers[str(name)] = dict(entry)
    return servers


def maybe_merge_user_provider_config(config: dict[str, object]) -> dict[str, object]:
    """
    Merge the user's global OpenCode config into the synthesized v2 config.

    The per-session ``XDG_CONFIG_HOME`` hides ``~/.config/opencode``, so carry over
    the user's providers, default model, plugins and MCP servers. Both v1
    (``provider``, ``plugin``, flat ``mcp``) and v2 (``providers``, ``plugins``,
    ``mcp.servers``) spellings are read; the result uses v2 keys only.
    Synthesized entries always win; the user's model applies only when none is set.

    :param config: The synthesized config dict.
    :returns: A new dict with the user's entries merged in.
    """
    user = _read_user_opencode_config()
    if user is None:
        return config
    result = dict(config)

    user_providers = _user_providers(user)
    if user_providers:
        existing = result.get("providers")
        merged = dict(existing) if isinstance(existing, Mapping) else {}
        for pid, entry in user_providers.items():
            merged.setdefault(pid, entry)
        result["providers"] = merged

    user_model = _user_model(user)
    if user_model:
        result.setdefault("model", user_model)

    user_plugins = _user_plugins(user)
    if user_plugins:
        existing_plugins = result.get("plugins")
        plugins: list[object] = list(existing_plugins) if isinstance(existing_plugins, list) else []
        for plugin in user_plugins:
            if plugin not in plugins:
                plugins.append(plugin)
        result["plugins"] = plugins

    user_servers = _user_mcp_servers(user)
    if user_servers:
        mcp = result.get("mcp")
        mcp_block = dict(mcp) if isinstance(mcp, Mapping) else {}
        current = mcp_block.get("servers")
        servers = dict(current) if isinstance(current, Mapping) else {}
        for name, entry in user_servers.items():
            servers.setdefault(name, entry)
        mcp_block["servers"] = servers
        result["mcp"] = mcp_block

    result.setdefault("$schema", OPENCODE_CONFIG_SCHEMA)
    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "merge or strip" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): merge v1 and v2 user config into v2 opencode.json"
```

---

### Task 56: Per-session instructions file

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py` (add after `write_opencode_provider_config`, `:163-185`)
- Test: `tests/test_opencode_native_provider.py`

**Interfaces:**
- Consumes: raw author instructions text (`_native_startup_raw_instructions_from_spec(agent_spec) -> str | None`, `orchestration.py:6753`).
- Produces: `write_opencode_instructions(xdg_config_home: Path, instructions: str | None) -> Path | None` —
  writes `<xdg_config_home>/opencode/AGENTS.md` (read by opencode as the global instruction file,
  `core/src/config/plugin/instruction.ts:36,64-68`), prefixed by the user's own global
  `AGENTS.md` (which the per-session XDG dir otherwise hides). Removes a stale file and returns
  `None` when there is nothing to write.

- [ ] **Step 1: Write the failing test**

Add `write_opencode_instructions` to the `provider` import block at the top of
`tests/test_opencode_native_provider.py`, then append:

```python
def test_write_opencode_instructions_writes_global_agents_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-cfg"))
    session_xdg = tmp_path / "session-xdg"
    path = write_opencode_instructions(session_xdg, "  Be terse.\n")
    assert path == session_xdg / "opencode" / "AGENTS.md"
    assert path.read_text(encoding="utf-8") == "Be terse.\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_write_opencode_instructions_keeps_user_agents_md(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    user_dir = tmp_path / "user-cfg" / "opencode"
    user_dir.mkdir(parents=True)
    (user_dir / "AGENTS.md").write_text("User rules.\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "user-cfg"))
    path = write_opencode_instructions(tmp_path / "s", "Agent rules.")
    assert path is not None
    assert path.read_text(encoding="utf-8") == "User rules.\n\nAgent rules.\n"


def test_write_opencode_instructions_removes_stale_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing"))
    session_xdg = tmp_path / "s"
    assert write_opencode_instructions(session_xdg, "x") is not None
    assert write_opencode_instructions(session_xdg, "   ") is None
    assert not (session_xdg / "opencode" / "AGENTS.md").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k write_opencode_instructions -v`
Expected: FAIL — `ImportError: cannot import name 'write_opencode_instructions'`.

- [ ] **Step 3: Write minimal implementation**

Add after `write_opencode_provider_config` in `provider.py`:

```python
_INSTRUCTIONS_FILE = "AGENTS.md"


def _user_agents_md() -> str | None:
    """The user's global ``~/.config/opencode/AGENTS.md`` text, if any."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".config"
    try:
        text = (base / "opencode" / _INSTRUCTIONS_FILE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return text.strip() or None


def write_opencode_instructions(xdg_config_home: Path, instructions: str | None) -> Path | None:
    """
    Write the per-session global ``AGENTS.md`` opencode loads as ambient instructions.

    opencode 2.0 parses the config ``instructions`` key but does not apply it; the
    global ``AGENTS.md`` under its config dir is always read. The user's own
    global ``AGENTS.md`` comes first so the per-session config dir does not hide it.

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME``.
    :param instructions: Raw author instructions, or ``None``.
    :returns: The written path, or ``None`` (and any stale file removed) when empty.
    """
    cfg_dir = xdg_config_home / "opencode"
    path = cfg_dir / _INSTRUCTIONS_FILE
    parts = [part for part in (_user_agents_md(), (instructions or "").strip()) if part]
    if not parts:
        path.unlink(missing_ok=True)
        return None
    cfg_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f"{_INSTRUCTIONS_FILE}.", dir=str(cfg_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n\n".join(parts) + "\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -k write_opencode_instructions -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): deliver agent instructions via per-session AGENTS.md"
```

---

### Task 57: v2 policy plugin (`Plugin.define` shape, directory package)

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:66-216`
- Test: `tests/test_opencode_native_bridge.py:158-173`, `:301-317`

**Interfaces:**
- Consumes: env `OMNIGENT_POLICY_URL`, `OMNIGENT_SESSION_ID`, `OMNIGENT_POLICY_HEADERS`, `OMNIGENT_RELAY_FILE` (unchanged contract, stamped in `orchestration.py:1610-1641`).
- Produces:
  ```python
  OPENCODE_POLICY_PLUGIN_ID = "omnigent-policy"
  def write_plugin_package(root: Path, name: str, *, source: str) -> Path   # <root>/<name>/{package.json, server.js}; returns the dir
  def write_opencode_policy_plugin(bridge_dir: Path) -> Path                # returns <bridge_dir>/omnigent-policy (a directory)
  ```
  The module's default export is `{id: "omnigent-policy", setup(ctx)}` registering
  `ctx.session.hook("prompt")` (PHASE_REQUEST, throw on `POLICY_ACTION_DENY`) and
  `ctx.tool.hook("execute.after")` (PHASE_TOOL_RESULT, replace `event.result` with a withheld marker
  on DENY). Transport errors / non-2xx / unwired env → allow (fail open).

Delete: `_POLICY_PLUGIN_FILE = "omnigent-policy.js"` (`bridge.py:66-69`), the v1 source
`_OPENCODE_POLICY_PLUGIN_JS` (`:71-190`, `require("fs")`, `export const OmnigentPolicyPlugin`,
`"chat.message"`, `"tool.execute.after"` keys), and the file-writing body `:193-216`.

- [ ] **Step 1: Write the failing test**

Replace `tests/test_opencode_native_bridge.py:158-173` (`test_write_opencode_policy_plugin`) and
`:301-317` (`test_policy_plugin_merges_routing_headers`) with:

```python
def test_write_opencode_policy_plugin_is_v2_package(bridge_dir: Path) -> None:
    path = write_opencode_policy_plugin(bridge_dir)
    # opencode 2.x only loads configured local plugins that are directories.
    assert path == bridge_dir / "omnigent-policy"
    assert path.is_dir()
    package = json.loads((path / "package.json").read_text(encoding="utf-8"))
    assert package["type"] == "module"
    src = (path / "server.js").read_text(encoding="utf-8")
    assert "export default" in src and 'id: "omnigent-policy"' in src
    assert 'ctx.session.hook("prompt"' in src
    assert 'ctx.tool.hook("execute.after"' in src
    assert "PHASE_REQUEST" in src and "PHASE_TOOL_RESULT" in src
    assert "OMNIGENT_POLICY_URL" in src and "OMNIGENT_SESSION_ID" in src
    assert "OMNIGENT_POLICY_HEADERS" in src and "...POLICY_HEADERS" in src
    assert "/policies/evaluate" in src
    # No v1 shapes and no unresolved package import from a bare bridge dir.
    for v1 in ('"chat.message"', "export const OmnigentPolicyPlugin", "require(", "@opencode/plugin"):
        assert v1 not in src
    # Idempotent overwrite.
    assert write_opencode_policy_plugin(bridge_dir) == path


_PLUGIN_HARNESS = r"""
import path from "node:path"
import { pathToFileURL } from "node:url"
const [, , pluginDir, verdictJson, mode] = process.argv
const calls = []
globalThis.fetch = async (url, init) => {
  calls.push({ url, body: JSON.parse(init.body) })
  if (mode === "throw") throw new Error("connection refused")
  return { ok: true, json: async () => JSON.parse(verdictJson) }
}
const mod = await import(pathToFileURL(path.join(pluginDir, "server.js")).href)
const hooks = {}
const register = (domain) => async (name, cb) => { hooks[domain + "." + name] = cb; return { dispose: async () => {} } }
await mod.default.setup({ session: { hook: register("session") }, tool: { hook: register("tool") } })
const out = { id: mod.default.id, hooks: Object.keys(hooks).sort() }
try {
  await hooks["session.prompt"]({ sessionID: "s", messageID: "m", prompt: { text: "hi" }, delivery: "steer" })
  out.prompt = "allowed"
} catch (e) { out.prompt = "blocked: " + e.message }
const ev = { tool: "shell", sessionID: "s", status: "completed", result: { content: "secret", output: { x: 1 } } }
await hooks["tool.execute.after"](ev)
out.result = ev.result
out.calls = calls
console.log(JSON.stringify(out))
"""


def _run_plugin(tmp_path: Path, plugin_dir: Path, verdict: dict, mode: str) -> dict:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    harness = tmp_path / "harness.mjs"
    harness.write_text(_PLUGIN_HARNESS, encoding="utf-8")
    env = {
        **os.environ,
        "OMNIGENT_POLICY_URL": "http://srv/",
        "OMNIGENT_SESSION_ID": "conv_1",
        "OMNIGENT_RELAY_FILE": "",
    }
    proc = subprocess.run(
        [node, str(harness), str(plugin_dir), json.dumps(verdict), mode],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )
    return json.loads(proc.stdout)


def test_policy_plugin_denies_prompt_and_withholds_tool_result(
    bridge_dir: Path, tmp_path: Path
) -> None:
    out = _run_plugin(
        tmp_path,
        write_opencode_policy_plugin(bridge_dir),
        {"result": "POLICY_ACTION_DENY", "reason": "nope"},
        "ok",
    )
    assert out["id"] == "omnigent-policy"
    assert out["hooks"] == ["session.prompt", "tool.execute.after"]
    assert out["prompt"] == "blocked: Omnigent policy blocked this prompt: nope"
    assert out["result"] == {"content": "[Omnigent policy withheld this tool result: nope]"}
    assert out["calls"][0] == {
        "url": "http://srv/v1/sessions/conv_1/policies/evaluate",
        "body": {"event": {"type": "PHASE_REQUEST", "target": "", "data": {"text": "hi"}}},
    }
    assert out["calls"][1]["body"]["event"] == {
        "type": "PHASE_TOOL_RESULT",
        "target": "shell",
        "data": {"result": "secret"},
    }


def test_policy_plugin_fails_open_on_transport_error(bridge_dir: Path, tmp_path: Path) -> None:
    out = _run_plugin(tmp_path, write_opencode_policy_plugin(bridge_dir), {}, "throw")
    assert out["prompt"] == "allowed"
    assert out["result"] == {"content": "secret", "output": {"x": 1}}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k "policy_plugin" -v`
Expected: FAIL — `assert PosixPath('.../omnigent-policy.js') == PosixPath('.../omnigent-policy')`.

- [ ] **Step 3: Write minimal implementation**

Replace `bridge.py:66-216` with:

```python
# Directory name (and plugin id) of the generated opencode policy plugin package.
OPENCODE_POLICY_PLUGIN_ID = "omnigent-policy"
_PLUGIN_ENTRYPOINT = "server.js"

# Default-exported plain object: ``Plugin.define`` is an identity function and a
# bridge-dir plugin cannot resolve ``@opencode/plugin`` (no node_modules above it).
# Raw string so JS escapes survive verbatim.
_OPENCODE_POLICY_PLUGIN_JS = r"""// Omnigent policy bridge for opencode-native (generated; do not edit).
// Gates prompt submission (REQUEST) and tool output (TOOL_RESULT) through the
// Omnigent policy engine; tool calls are gated by permission.asked instead.
import fs from "node:fs"

const BASE = (process.env.OMNIGENT_POLICY_URL || "").replace(/\/+$/, "")
const SESSION = process.env.OMNIGENT_SESSION_ID || ""
const RELAY_FILE = process.env.OMNIGENT_RELAY_FILE || ""
let POLICY_HEADERS = {}
try {
  POLICY_HEADERS = JSON.parse(process.env.OMNIGENT_POLICY_HEADERS || "{}") || {}
} catch (_e) {
  POLICY_HEADERS = {}
}
const TIMEOUT_MS = 600000
const DENY = "POLICY_ACTION_DENY"

// tool_relay.json appears after the server starts, so re-read it per call.
function relayCredentials() {
  if (!RELAY_FILE) return null
  try {
    const d = JSON.parse(fs.readFileSync(RELAY_FILE, "utf8"))
    if (d && typeof d.url === "string" && typeof d.token === "string") {
      return { url: d.url, token: d.token }
    }
  } catch (_e) {}
  return null
}

async function evaluate(type, target, data) {
  // Unwired (no server/session) or any transport failure allows: fail open.
  if (!BASE || !SESSION) return { result: "ALLOW" }
  const relay = relayCredentials()
  const url = relay
    ? relay.url.replace(/\/+$/, "") + "/policies/evaluate"
    : BASE + "/v1/sessions/" + encodeURIComponent(SESSION) + "/policies/evaluate"
  const headers = relay
    ? { "content-type": "application/json", authorization: "Bearer " + relay.token }
    : { "content-type": "application/json", ...POLICY_HEADERS }
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  try {
    const resp = await fetch(url, {
      method: "POST",
      headers,
      body: JSON.stringify({ event: { type, target: target || "", data } }),
      signal: controller.signal,
    })
    if (!resp.ok) return { result: "ALLOW" }
    const body = await resp.json()
    return body && typeof body === "object" ? body : { result: "ALLOW" }
  } catch (_e) {
    return { result: "ALLOW" }
  } finally {
    clearTimeout(timer)
  }
}

function resultText(result) {
  if (!result) return ""
  const content = result.content
  if (typeof content === "string") return content
  if (Array.isArray(content)) {
    return content
      .filter((part) => part && part.type === "text" && typeof part.text === "string")
      .map((part) => part.text)
      .join("\n")
  }
  if (result.output === undefined) return ""
  try {
    return JSON.stringify(result.output)
  } catch (_e) {
    return String(result.output)
  }
}

export default {
  id: "omnigent-policy",
  setup: async (ctx) => {
    // REQUEST phase: a thrown error rejects the prompt before it is recorded.
    // Web-injected prompts were gated at injection, so the server allows them.
    await ctx.session.hook("prompt", async (event) => {
      const text = event && event.prompt && typeof event.prompt.text === "string" ? event.prompt.text : ""
      if (!text) return
      const verdict = await evaluate("PHASE_REQUEST", "", { text })
      if (verdict.result === DENY) {
        throw new Error("Omnigent policy blocked this prompt: " + (verdict.reason || "request denied"))
      }
    })
    // TOOL_RESULT phase: the tool already ran; a DENY withholds its output.
    await ctx.tool.hook("execute.after", async (event) => {
      if (!event || event.status !== "completed") return
      const verdict = await evaluate("PHASE_TOOL_RESULT", event.tool, { result: resultText(event.result) })
      if (verdict.result === DENY) {
        event.result = {
          content: "[Omnigent policy withheld this tool result: " + (verdict.reason || "denied") + "]",
        }
      }
    })
  },
}
"""


def _atomic_write_text(path: Path, text: str) -> None:
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def write_plugin_package(root: Path, name: str, *, source: str) -> Path:
    """
    Write an ESM opencode plugin package ``<root>/<name>/{package.json,server.js}``.

    opencode 2.x rejects configured plugin paths that are files; a directory
    resolves its ``server`` entrypoint. ``"type": "module"`` makes ``server.js`` ESM.

    :param root: Parent directory (the bridge dir).
    :param name: Package directory and npm name, e.g. ``"omnigent-policy"``.
    :param source: The ``server.js`` module source.
    :returns: The package directory (register this path in ``plugins``).
    """
    package_dir = root / name
    package_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _atomic_write_text(
        package_dir / "package.json",
        json.dumps({"name": name, "private": True, "type": "module"}, indent=2) + "\n",
    )
    _atomic_write_text(package_dir / _PLUGIN_ENTRYPOINT, source)
    return package_dir


def write_opencode_policy_plugin(bridge_dir: Path) -> Path:
    """
    Write the Omnigent policy-bridge plugin package and return its directory.

    The runner registers the directory in ``opencode.json`` ``plugins`` and stamps
    ``OMNIGENT_POLICY_URL`` / ``OMNIGENT_SESSION_ID`` / ``OMNIGENT_POLICY_HEADERS``
    / ``OMNIGENT_RELAY_FILE`` on ``opencode serve``. Overwritten each launch.

    :param bridge_dir: OpenCode-native bridge directory.
    :returns: ``<bridge_dir>/omnigent-policy``.
    """
    bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_plugin_package(
        bridge_dir, OPENCODE_POLICY_PLUGIN_ID, source=_OPENCODE_POLICY_PLUGIN_JS
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k "policy_plugin" -v`
Expected: PASS (the two node-driven tests skip only when `node` is absent; on this machine
`node v22` is present).

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py tests/test_opencode_native_bridge.py
git commit -m "feat(opencode-native): port policy plugin to the v2 Plugin.define API"
```

---

### Task 58: Port the managed-connect ucode auth plugin to v2

**Files:**
- Modify: `omnigent/harnesses/opencode_native/provider.py:702-809` (`_provider_base_urls_match_host`, `managed_connect_opencode_config`)
- Test: `tests/test_opencode_native_provider.py:748-830`

**Interfaces:**
- Consumes: ucode's generated `~/.ucode/opencode-xdg/opencode/opencode.json` (v1: `provider.<id>.{npm, options.baseURL}`, `model`) and `plugin/ucode-auth.js` whose line `const AUTH_COMMAND = [...]` holds the mint argv (ucode `src/ucode/agents/opencode.py:57-178`); `v1_provider_to_v2` (Task 55); `write_plugin_package` (Task 57).
- Produces:
  ```python
  UCODE_AUTH_PLUGIN_ID = "omnigent-ucode-auth"
  def render_ucode_auth_plugin(*, providers: Sequence[str], auth_command: Sequence[str]) -> str
  def managed_connect_opencode_config(xdg_config_home: Path, bridge_dir: Path) -> dict[str, object] | None
      # {"model"?: str, "providers": {id: v2 entry}, "plugins": [str(<bridge_dir>/omnigent-ucode-auth)]}
  ```
  The generated plugin registers, per provider, `ctx.session.hook("http.request", …, {providerID})`
  (sets `Authorization: Bearer <minted>`, re-minting within 120 s of JWT `exp`) and
  `ctx.session.hook("http.response", …, {providerID})` (401 → force re-mint on the next request).
  Any stale `<xdg_config_home>/opencode/plugin/ucode-auth.js` from a v1 launch is deleted so v2's
  plugin auto-discovery does not try to load it.

Delete: the copy of ucode's JS into the session plugin dir (`provider.py:803-808`) and the
`config["plugin"] = [...]` v1 key.

- [ ] **Step 1: Write the failing test**

Replace `tests/test_opencode_native_provider.py:748-830` with:

```python
_UCODE_PLUGIN_JS = (
    "// Generated by ucode. Keep Databricks auth fresh for model requests.\n"
    'const AUTH_COMMAND = ["ucode", "auth-token", "--force-refresh"]\n'
    "export const UcodeDatabricksAuth = async () => ({})\n"
)


def _ucode_home(tmp_path: Path, base_url: str) -> Path:
    ucode_dir = tmp_path / ".ucode" / "opencode-xdg" / "opencode"
    (ucode_dir / "plugin").mkdir(parents=True)
    (ucode_dir / "opencode.json").write_text(
        json.dumps(
            {
                "model": "databricks-anthropic/system.ai.claude-opus-4-8",
                "provider": {
                    "databricks-anthropic": {
                        "npm": "@ai-sdk/anthropic",
                        "options": {"baseURL": base_url, "apiKey": "stale"},
                        "models": {"system.ai.claude-opus-4-8": {"name": "Opus"}},
                    }
                },
            }
        )
    )
    (ucode_dir / "plugin" / "ucode-auth.js").write_text(_UCODE_PLUGIN_JS)
    return ucode_dir


def _sidecar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnigent.host.databricks_credential._read_sidecar",
        lambda path: {
            "server": "s",
            "host_id": "h",
            "host_token": "t",
            "workspace_host": "https://ws",
        },
    )


def test_managed_connect_opencode_config_is_v2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ucode_home(tmp_path, "https://ws/serving-endpoints/anthropic")
    _sidecar(monkeypatch)
    session_xdg = tmp_path / "session-xdg"
    stale = session_xdg / "opencode" / "plugin" / "ucode-auth.js"
    stale.parent.mkdir(parents=True)
    stale.write_text("// v1 copy from an older launch\n")
    bridge_dir = tmp_path / "bridge"

    config = managed_connect_opencode_config(session_xdg, bridge_dir)

    assert config is not None
    assert config["model"] == "databricks-anthropic/system.ai.claude-opus-4-8"
    provider = config["providers"]["databricks-anthropic"]
    assert provider["package"] == "aisdk:@ai-sdk/anthropic"
    assert provider["settings"]["baseURL"] == "https://ws/serving-endpoints/anthropic"
    assert "provider" not in config and "plugin" not in config
    plugin_dir = bridge_dir / "omnigent-ucode-auth"
    assert config["plugins"] == [str(plugin_dir)]
    src = (plugin_dir / "server.js").read_text(encoding="utf-8")
    assert 'const PROVIDERS = ["databricks-anthropic"]' in src
    assert 'const AUTH_COMMAND = ["ucode", "auth-token", "--force-refresh"]' in src
    assert 'ctx.session.hook(\n        "http.request"' in src
    assert not stale.exists()  # v2 would auto-load and reject the v1 plugin


def test_managed_connect_opencode_config_none_without_sidecar(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("omnigent.host.databricks_credential._read_sidecar", lambda path: None)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


def test_managed_connect_opencode_config_declines_without_auth_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    ucode_dir = _ucode_home(tmp_path, "https://ws/x")
    (ucode_dir / "plugin" / "ucode-auth.js").write_text("// no command here\n")
    _sidecar(monkeypatch)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


@pytest.mark.parametrize("bad_url", ["https://evil.example/x", "http://ws/x"])
def test_managed_connect_opencode_config_rejects_untrusted_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_url: str
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    _ucode_home(tmp_path, bad_url)
    _sidecar(monkeypatch)
    assert managed_connect_opencode_config(tmp_path / "xdg", tmp_path / "bridge") is None


def test_ucode_auth_plugin_stamps_bearer(tmp_path: Path) -> None:
    import os
    import shutil
    import subprocess

    from omnigent.harnesses.opencode_native.bridge import write_plugin_package
    from omnigent.harnesses.opencode_native.provider import render_ucode_auth_plugin

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "mint").write_text("#!/bin/sh\necho tok-123\n")
    (bin_dir / "mint").chmod(0o755)
    plugin_dir = write_plugin_package(
        tmp_path,
        "omnigent-ucode-auth",
        source=render_ucode_auth_plugin(providers=["databricks-oss"], auth_command=["mint"]),
    )
    harness = tmp_path / "h.mjs"
    harness.write_text(
        'import { pathToFileURL } from "node:url"\n'
        f'const mod = await import(pathToFileURL({json.dumps(str(plugin_dir / "server.js"))}).href)\n'
        "const hooks = []\n"
        "await mod.default.setup({ session: { hook: async (name, cb, opts) => { hooks.push({ name, cb, opts }) } } })\n"
        'const req = hooks.find((h) => h.name === "http.request")\n'
        'const ev = { request: new Request("https://ws/x", { method: "POST", body: "{}" }) }\n'
        "await req.cb(ev)\n"
        "console.log(JSON.stringify({ id: mod.default.id, hooks: hooks.map((h) => h.name + ':' + h.opts.providerID),"
        ' auth: ev.request.headers.get("authorization"), body: await ev.request.text() }))\n',
        encoding="utf-8",
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}
    out = json.loads(
        subprocess.run(
            [node, str(harness)], capture_output=True, text=True, env=env, timeout=30, check=True
        ).stdout
    )
    assert out == {
        "id": "omnigent-ucode-auth",
        "hooks": ["http.request:databricks-oss", "http.response:databricks-oss"],
        "auth": "Bearer tok-123",
        "body": "{}",
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "managed_connect or ucode_auth" -v`
Expected: FAIL — `TypeError: managed_connect_opencode_config() takes 1 positional argument but 2 were given`.

- [ ] **Step 3: Write minimal implementation**

Add `import re` to the `provider.py` imports (`:22-30`). Replace `provider.py:702-809` with:

```python
UCODE_AUTH_PLUGIN_ID = "omnigent-ucode-auth"
_UCODE_AUTH_COMMAND_RE = re.compile(r"^const AUTH_COMMAND = (\[.*\])\s*$", re.MULTILINE)

_UCODE_AUTH_PLUGIN_JS = """// Databricks token refresh for ucode-configured providers (generated by Omnigent; do not edit).
// Mints via ucode's auth-token command and stamps each model HTTP request.
import { execFile } from "node:child_process"
import { promisify } from "node:util"

const PROVIDERS = __PROVIDERS__
const AUTH_COMMAND = __AUTH_COMMAND__
const REFRESH_SKEW_MS = 120_000
const run = promisify(execFile)

let accessToken
let expiresAt = 0
let refreshPromise

function cacheToken(value) {
  accessToken = value
  expiresAt = Infinity
  try {
    const claims = JSON.parse(Buffer.from(value.split(".")[1], "base64url").toString())
    if (typeof claims.exp === "number") expiresAt = claims.exp * 1000
  } catch {}
}

async function mintToken() {
  try {
    const { stdout } = await run(AUTH_COMMAND[0], AUTH_COMMAND.slice(1), { encoding: "utf8" })
    const token = stdout.trim()
    if (!token) throw new Error("returned an empty token")
    cacheToken(token)
  } catch (error) {
    const detail = String(error.stderr || error.message || "").trim()
    throw new Error("ucode auth-token failed" + (detail ? ": " + detail : ""))
  }
}

function refreshToken() {
  if (!refreshPromise) refreshPromise = mintToken().finally(() => { refreshPromise = undefined })
  return refreshPromise
}

export default {
  id: "omnigent-ucode-auth",
  setup: async (ctx) => {
    for (const providerID of PROVIDERS) {
      await ctx.session.hook(
        "http.request",
        async (event) => {
          if (!accessToken || expiresAt <= Date.now() + REFRESH_SKEW_MS) await refreshToken()
          const headers = new Headers(event.request.headers)
          headers.set("Authorization", "Bearer " + accessToken)
          event.request = new Request(event.request, { headers })
        },
        { providerID },
      )
      // A 401 means the cached token is stale; the next retry mints a fresh one.
      await ctx.session.hook(
        "http.response",
        (event) => {
          if (event.response.status === 401) expiresAt = 0
        },
        { providerID },
      )
    }
  },
}
"""


def render_ucode_auth_plugin(*, providers: Sequence[str], auth_command: Sequence[str]) -> str:
    """
    Render the v2 plugin that stamps ucode-minted Databricks tokens on model requests.

    :param providers: opencode provider ids to authenticate, e.g. ``["databricks-oss"]``.
    :param auth_command: ucode's mint argv, e.g. ``["ucode", "auth-token", "--force-refresh"]``.
    :returns: The ``server.js`` source.
    """
    return _UCODE_AUTH_PLUGIN_JS.replace("__PROVIDERS__", json.dumps(list(providers))).replace(
        "__AUTH_COMMAND__", json.dumps(list(auth_command))
    )


def _ucode_auth_command(plugin_source: str) -> list[str] | None:
    """Extract ``AUTH_COMMAND`` from ucode's generated (v1) ``ucode-auth.js``."""
    match = _UCODE_AUTH_COMMAND_RE.search(plugin_source)
    if match is None:
        return None
    try:
        argv = json.loads(match.group(1))
    except ValueError:
        return None
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        return None
    return argv


def _provider_base_urls_match_host(config: Mapping[str, object], workspace_host: str) -> bool:
    """True when every v2 provider ``settings.baseURL`` is HTTPS on *workspace_host*.

    Guards the managed-connect path: a broker bearer is forwarded to whatever base
    URL the on-disk config names, so a stale/other origin must not be trusted.
    Requires at least one base URL.
    """
    from omnigent.host.databricks_credential import https_url_on_workspace_host

    providers = config.get("providers")
    if not isinstance(providers, Mapping):
        return False
    saw_url = False
    for provider in providers.values():
        settings = provider.get("settings") if isinstance(provider, Mapping) else None
        base_url = settings.get("baseURL") if isinstance(settings, Mapping) else None
        if not isinstance(base_url, str) or not base_url:
            continue
        saw_url = True
        if not https_url_on_workspace_host(base_url, workspace_host):
            return False
    return saw_url


def managed_connect_opencode_config(
    xdg_config_home: Path, bridge_dir: Path
) -> dict[str, object] | None:
    """Build v2 config fragments from ucode's opencode output on a managed connect host.

    ucode (run at host boot) writes a v1 ``opencode.json`` and a v1
    ``plugin/ucode-auth.js``. The provider blocks are converted to v2 and an
    Omnigent-owned v2 auth plugin reusing ucode's ``AUTH_COMMAND`` is written into
    *bridge_dir*. The caller must forward ``DATABRICKS_BEARER_COMMAND`` into the
    opencode env so ``ucode auth-token`` can mint.

    :param xdg_config_home: The per-session ``XDG_CONFIG_HOME`` (stale v1 plugin copies are removed).
    :param bridge_dir: Bridge dir that receives the ``omnigent-ucode-auth`` plugin package.
    :returns: ``{"model"?, "providers", "plugins"}``, or ``None`` off a managed host
        or when ucode's output is unusable.
    """
    from omnigent.harnesses.opencode_native.bridge import write_plugin_package
    from omnigent.host.databricks_credential import _read_sidecar, _sidecar_path

    sidecar = _read_sidecar(_sidecar_path())
    if sidecar is None:
        return None  # not a managed connect host
    workspace_host = sidecar["workspace_host"].rstrip("/")

    # ucode writes opencode's config into its own XDG root, not ~/.config/opencode.
    ucode_config_dir = Path.home() / ".ucode" / "opencode-xdg" / "opencode"
    ucode_config = ucode_config_dir / "opencode.json"
    if not ucode_config.exists():
        # The boot-time configure may not have run yet; configure on demand.
        _configure_opencode_on_demand()
    try:
        raw = json.loads(ucode_config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _logger.info("opencode managed config: unreadable ucode config %s: %r", ucode_config, exc)
        return None
    raw_providers = raw.get("provider") if isinstance(raw, dict) else None
    if not isinstance(raw_providers, Mapping) or not raw_providers:
        _logger.info(
            "opencode managed config: ucode did not configure opencode (no provider block in %s); "
            "opencode falls back to its own login.",
            ucode_config,
        )
        return None
    providers = {
        str(pid): v1_provider_to_v2(entry)
        for pid, entry in raw_providers.items()
        if isinstance(entry, Mapping)
    }
    config: dict[str, object] = {"providers": providers}
    # Security: only forward a broker bearer to HTTPS base URLs on the sidecar's host.
    if not _provider_base_urls_match_host(config, workspace_host):
        _logger.warning(
            "opencode managed config: provider base URL is not HTTPS on the connected "
            "workspace host %r — declining so the broker bearer is not forwarded to an "
            "unverified origin.",
            workspace_host,
        )
        return None

    ucode_plugin = ucode_config_dir / "plugin" / "ucode-auth.js"
    try:
        auth_command = _ucode_auth_command(ucode_plugin.read_text(encoding="utf-8"))
    except OSError as exc:
        _logger.info("opencode managed config: unreadable ucode plugin %s (%r)", ucode_plugin, exc)
        auth_command = None
    if auth_command is None:
        _logger.info(
            "opencode managed config: no AUTH_COMMAND in %s; declining so no static bearer is used.",
            ucode_plugin,
        )
        return None

    # A v1 copy from an older launch would be auto-discovered and fail to load.
    (xdg_config_home / "opencode" / "plugin" / "ucode-auth.js").unlink(missing_ok=True)
    plugin_dir = write_plugin_package(
        bridge_dir,
        UCODE_AUTH_PLUGIN_ID,
        source=render_ucode_auth_plugin(providers=list(providers), auth_command=auth_command),
    )
    config["plugins"] = [str(plugin_dir)]
    model = raw.get("model")
    if isinstance(model, str) and "/" in model:
        config["model"] = model
    return config
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py -v`
Expected: PASS (whole file green now that Tasks 45–51 replaced every v1 shape).

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/provider.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): port ucode Databricks auth plugin to the v2 plugin API"
```

---

### Task 59: Read v2 credentials; readiness counts them

**Files:**
- Modify: `omnigent/onboarding/opencode_auth.py:1-15` (docstring), `:44-69`
- Test: `tests/onboarding/test_opencode_auth.py`

The readiness gate at `omnigent/onboarding/harness_readiness.py:919-922`
(`return True if opencode_auth_summary().has_provider else "needs-auth"`) needs **no code change**:
`has_provider` reads `stored_providers`, which now includes v2 DB credentials. The hint text
`opencode auth login` remains valid (`opencode auth --help`, 2.0.18).

**Interfaces:**
- Consumes: the user's v2 DB (`$OPENCODE_DB` resolved against `$XDG_DATA_HOME/opencode`, default `opencode.db`; `cli/src/database-path.ts:4-13`), table `credential` (`core/src/credential/sql.ts:5-14`).
- Produces:
  ```python
  def opencode_data_dir() -> Path                                  # $XDG_DATA_HOME/opencode
  def opencode_db_path() -> Path | None                            # None for OPENCODE_DB=":memory:"
  def stored_v2_credentials(db_path: Path | None = None) -> dict[str, dict[str, object]]
      # integration id -> legacy auth.json entry ({"type":"api",...} | {"type":"oauth",...}); active/newest row wins
  _stored_providers() -> tuple[str, ...]                           # auth.json keys ∪ DB integration ids
  ```

- [ ] **Step 1: Write the failing test**

Add `import sqlite3` to the top-of-file imports of `tests/onboarding/test_opencode_auth.py`, then append:

```python
def _write_db(rows: list[tuple[str | None, dict[str, object], int | None, int]]) -> Path:
    """Create a v2 ``opencode.db`` with a ``credential`` table (live 2.0.18 columns)."""
    path = oc.opencode_data_dir() / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE credential (id TEXT PRIMARY KEY, integration_id TEXT, label TEXT NOT NULL,"
        " value TEXT NOT NULL, connector_id TEXT, method_id TEXT, active INTEGER,"
        " time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL)"
    )
    for index, (integration_id, value, active, updated) in enumerate(rows):
        conn.execute(
            "INSERT INTO credential VALUES (?, ?, 'x', ?, NULL, NULL, ?, 0, ?)",
            (f"cred_{index}", integration_id, json.dumps(value), active, updated),
        )
    conn.commit()
    conn.close()
    return path


def test_stored_v2_credentials_map_to_legacy_entries(tmp_path: Path) -> None:
    _write_db(
        [
            ("anthropic", {"type": "key", "key": "old"}, None, 1),
            ("anthropic", {"type": "key", "key": "new", "metadata": {"a": "b", "n": 1}}, 1, 0),
            (
                "openai",
                {
                    "type": "oauth",
                    "methodID": "chatgpt-browser",
                    "refresh": "r",
                    "access": "a",
                    "expires": 99,
                    "metadata": {"accountID": "acct"},
                },
                None,
                2,
            ),
            (None, {"type": "key", "key": "orphan"}, None, 3),
            ("broken", {"type": "mystery"}, None, 4),
        ]
    )
    assert oc.stored_v2_credentials() == {
        # The active row wins over a newer inactive one.
        "anthropic": {"type": "api", "key": "new", "metadata": {"a": "b"}},
        "openai": {"type": "oauth", "refresh": "r", "access": "a", "expires": 99, "accountId": "acct"},
    }


def test_stored_v2_credentials_empty_without_db_or_table(tmp_path: Path) -> None:
    assert oc.stored_v2_credentials() == {}
    path = oc.opencode_data_dir() / "opencode.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(path).close()  # a v1 DB without the credential table
    assert oc.stored_v2_credentials() == {}


def test_opencode_db_path_honors_opencode_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert oc.opencode_db_path() == tmp_path / "share" / "opencode" / "opencode.db"
    monkeypatch.setenv("OPENCODE_DB", "custom.db")
    assert oc.opencode_db_path() == tmp_path / "share" / "opencode" / "custom.db"
    monkeypatch.setenv("OPENCODE_DB", str(tmp_path / "abs.db"))
    assert oc.opencode_db_path() == tmp_path / "abs.db"
    monkeypatch.setenv("OPENCODE_DB", ":memory:")
    assert oc.opencode_db_path() is None


def test_summary_not_ready_with_empty_v2_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An installed CLI whose credential table is empty reads needs-auth, not ready."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_db([])
    summary = oc.opencode_auth_summary()
    assert summary.stored_providers == ()
    assert summary.has_provider is False
    assert summary.ready is False


def test_summary_ready_with_only_v2_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A v2-only login (SQLite, no auth.json) makes the harness ready."""
    monkeypatch.setattr(oc, "harness_cli_installed", lambda _key: True)
    _write_db([("anthropic", {"type": "key", "key": "k"}, 1, 0)])
    summary = oc.opencode_auth_summary()
    assert summary.stored_providers == ("anthropic",)
    assert summary.ready is True
```

Also add `monkeypatch.delenv("OPENCODE_DB", raising=False)` to the autouse `_isolate_env` fixture
(`tests/onboarding/test_opencode_auth.py:13-18`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/onboarding/test_opencode_auth.py -v`
Expected: FAIL — `AttributeError: module 'omnigent.onboarding.opencode_auth' has no attribute 'opencode_data_dir'`.

- [ ] **Step 3: Write minimal implementation**

Replace the module docstring `opencode_auth.py:1-15` with:

```python
"""OpenCode readiness + credential reporting for ``omnigent setup``.

Omnigent stores **no** OpenCode credentials: OpenCode owns provider auth via
``opencode auth login`` or ambient provider env vars. OpenCode 2.x keeps
credentials in the ``credential`` table of its SQLite DB
(``~/.local/share/opencode/opencode.db``) and imports a legacy ``auth.json``
once. This module reads both, read-only and best-effort, so setup can report
which providers are reachable and the runner can seed per-session data dirs.
"""
```

Add `import sqlite3` to the imports (`:17-22`) and replace `opencode_auth.py:44-69` with:

```python
def opencode_data_dir() -> Path:
    """Return OpenCode's data dir (``Global.Path.data``), honoring ``XDG_DATA_HOME``."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "opencode"


def opencode_auth_path() -> Path:
    """Return OpenCode's legacy ``auth.json`` path for this process's HOME."""
    return opencode_data_dir() / "auth.json"


def opencode_db_path() -> Path | None:
    """Return OpenCode 2.x's SQLite DB path (``OPENCODE_DB`` resolved against the data dir).

    :returns: The DB path, or ``None`` for an in-memory DB.
    """
    override = os.environ.get("OPENCODE_DB", "").strip()
    if override == ":memory:":
        return None
    if override:
        path = Path(override)
        return path if path.is_absolute() else opencode_data_dir() / path
    return opencode_data_dir() / "opencode.db"


def _legacy_auth_entry(raw: object) -> dict[str, object] | None:
    """Map a v2 ``Credential.Value`` JSON onto the legacy ``auth.json`` entry shape."""
    try:
        value = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
    if value.get("type") == "key" and isinstance(value.get("key"), str):
        entry: dict[str, object] = {"type": "api", "key": value["key"]}
        strings = {k: v for k, v in metadata.items() if isinstance(v, str)}
        if strings:
            entry["metadata"] = strings
        return entry
    if value.get("type") == "oauth":
        refresh, access, expires = value.get("refresh"), value.get("access"), value.get("expires")
        if not (isinstance(refresh, str) and isinstance(access, str) and isinstance(expires, int)):
            return None
        entry = {"type": "oauth", "refresh": refresh, "access": access, "expires": expires}
        if isinstance(metadata.get("accountID"), str):
            entry["accountId"] = metadata["accountID"]
        if isinstance(metadata.get("enterpriseUrl"), str):
            entry["enterpriseUrl"] = metadata["enterpriseUrl"]
        return entry
    return None


def stored_v2_credentials(db_path: Path | None = None) -> dict[str, dict[str, object]]:
    """Return OpenCode 2.x credentials as legacy ``auth.json`` entries.

    Opens the DB read-only; any missing file, missing table or SQLite error yields ``{}``.
    Rows are ordered so the active (then newest) credential per integration wins.

    :param db_path: DB to read; ``None`` uses :func:`opencode_db_path`.
    :returns: ``{integration_id: entry}``.
    """
    path = db_path or opencode_db_path()
    if path is None or not path.is_file():
        return {}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return {}
    try:
        rows = conn.execute(
            "SELECT integration_id, value FROM credential WHERE integration_id IS NOT NULL"
            " ORDER BY COALESCE(active, 0), time_updated"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        conn.close()
    credentials: dict[str, dict[str, object]] = {}
    for integration_id, raw in rows:
        entry = _legacy_auth_entry(raw)
        if entry is not None and isinstance(integration_id, str) and integration_id:
            credentials[integration_id] = entry
    return credentials


def _stored_providers() -> tuple[str, ...]:
    """Return provider ids with stored credentials (``auth.json`` ∪ the v2 DB).

    Best-effort: unreadable sources contribute nothing. An empty ``auth.json``
    value is config shape, not a credential, so it is ignored.
    """
    ids: list[str] = []
    try:
        data = json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if isinstance(data, dict):
        ids.extend(str(k) for k, v in data.items() if bool(v))
    for integration_id in stored_v2_credentials():
        if integration_id not in ids:
            ids.append(integration_id)
    return tuple(ids)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/onboarding/test_opencode_auth.py tests/onboarding/test_harness_readiness.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/opencode_auth.py tests/onboarding/test_opencode_auth.py
git commit -m "feat(opencode-native): detect OpenCode 2.x SQLite credentials for readiness"
```

---

### Task 60: Seed per-session credentials from `auth.json` and the v2 DB

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:446-508` (`user_opencode_auth_path`, `seed_opencode_auth`)
- Test: `tests/test_opencode_native_bridge.py:221-245`

**Interfaces:**
- Consumes: `opencode_auth_path()`, `stored_v2_credentials()` (Task 59); import migration reads `$XDG_DATA_HOME/opencode/auth.json` (`20260805200742_import_legacy_credentials.ts:39`).
- Produces:
  ```python
  def seed_opencode_auth(bridge_dir: Path) -> Path | None   # writes <xdg-data>/opencode/auth.json (0600); None when no credentials
  def seeded_provider_ids(bridge_dir: Path) -> frozenset[str]
  ```
  Merge rule: user `auth.json` entries, then v2 DB entries on top (the DB is current; it already
  imported `auth.json` once). Limitation: the import runs only when the per-session DB is created,
  so a re-login after a conversation's first launch reaches only new conversations.

Delete: `user_opencode_auth_path` (`bridge.py:446-456`) — replaced by
`omnigent.onboarding.opencode_auth.opencode_auth_path`; `shutil.copyfile` path (`:497-508`); the
`import shutil` at `bridge.py:31` if unused.

- [ ] **Step 1: Write the failing test**

Replace `tests/test_opencode_native_bridge.py:221-245` with:

```python
def _user_share(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    share = tmp_path / "user-share"
    (share / "opencode").mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(share))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    return share


def test_seed_opencode_auth_copies_user_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    share = _user_share(monkeypatch, tmp_path)
    (share / "opencode" / "auth.json").write_text('{"anthropic": {"type": "api", "key": "k"}}')
    bridge_dir = bridge.prepare_bridge_dir("conv_seed")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest == bridge.xdg_data_home_for_bridge_dir(bridge_dir) / "opencode" / "auth.json"
    assert json.loads(dest.read_text()) == {"anthropic": {"type": "api", "key": "k"}}
    assert (os.stat(dest).st_mode & 0o777) == 0o600
    assert bridge.seeded_provider_ids(bridge_dir) == frozenset({"anthropic"})


def test_seed_opencode_auth_merges_v2_db_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v2-only logins live in SQLite; they are written in legacy shape so the
    per-session DB's one-time import picks them up."""
    share = _user_share(monkeypatch, tmp_path)
    (share / "opencode" / "auth.json").write_text(
        '{"anthropic": {"type": "api", "key": "stale"}, "groq": {"type": "api", "key": "g"}}'
    )
    monkeypatch.setattr(
        "omnigent.onboarding.opencode_auth.stored_v2_credentials",
        lambda db_path=None: {"anthropic": {"type": "api", "key": "fresh"}},
    )
    bridge_dir = bridge.prepare_bridge_dir("conv_merge")
    dest = bridge.seed_opencode_auth(bridge_dir)
    assert dest is not None
    assert json.loads(dest.read_text()) == {
        "anthropic": {"type": "api", "key": "fresh"},
        "groq": {"type": "api", "key": "g"},
    }


def test_seed_opencode_auth_noop_without_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No auth.json and no v2 DB → None (e.g. on a remote runner)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "empty-share"))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    bridge_dir = bridge.prepare_bridge_dir("conv_noseed")
    assert bridge.seed_opencode_auth(bridge_dir) is None
    assert bridge.seeded_provider_ids(bridge_dir) == frozenset()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k seed -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'seeded_provider_ids'`, and the merge
test sees `"stale"`.

- [ ] **Step 3: Write minimal implementation**

Delete `bridge.py:446-456` (`user_opencode_auth_path`) and replace `bridge.py:480-508` with:

```python
def _per_session_auth_path(bridge_dir: Path) -> Path:
    return xdg_data_home_for_bridge_dir(bridge_dir) / "opencode" / "auth.json"


def seed_opencode_auth(bridge_dir: Path) -> Path | None:
    """
    Write the user's OpenCode credentials into the per-session ``auth.json``.

    ``opencode serve`` runs with a per-session ``XDG_DATA_HOME`` and DB. OpenCode 2.x
    imports ``$XDG_DATA_HOME/opencode/auth.json`` once, when that DB is created, so
    seed it with the user's legacy ``auth.json`` plus their v2 SQLite credentials
    (legacy shape; the DB wins on conflicts). Written ``0600``; refreshed each spawn.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: The written path, or ``None`` when there are no credentials or the write fails.
    """
    from omnigent.onboarding.opencode_auth import opencode_auth_path, stored_v2_credentials

    merged: dict[str, object] = {}
    try:
        legacy = json.loads(opencode_auth_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        legacy = None
    if isinstance(legacy, dict):
        merged.update({str(k): v for k, v in legacy.items() if v})
    merged.update(stored_v2_credentials())
    if not merged:
        return None
    dest = _per_session_auth_path(bridge_dir)
    try:
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix="auth.json.", dir=str(dest.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(merged, handle, sort_keys=True)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, dest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    except OSError:
        return None
    return dest


def seeded_provider_ids(bridge_dir: Path) -> frozenset[str]:
    """
    Return provider ids present in the per-session ``auth.json``.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Integration ids, empty when nothing was seeded.
    """
    try:
        data = json.loads(_per_session_auth_path(bridge_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    return frozenset(str(k) for k in data) if isinstance(data, dict) else frozenset()
```

Run `grep -n "shutil" omnigent/harnesses/opencode_native/bridge.py`; if the only hit is the import
at `:31`, delete it.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_bridge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py tests/test_opencode_native_bridge.py
git commit -m "feat(opencode-native): seed per-session credentials from auth.json and the v2 DB"
```

---

### Task 61: Env-key fallback via `connect_provider_key`

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py` (add after `seeded_provider_ids`; add `import logging` + `_logger`)
- Test: `tests/test_opencode_native_bridge.py`

**Interfaces:**
- Consumes: Stage 1 `OpenCodeClient.connect_provider_key(provider_id: str, api_key: str) -> bool` (`POST /api/integration/{id}/connect/key`); `_ENV_PROVIDER_VARS` (`opencode_auth.py:31-41`).
- Produces: `async def connect_env_provider_keys(client, *, stored: Iterable[str] = (), environ: Mapping[str, str] | None = None) -> list[str]` — connects each provider whose env var is set and which is not already in `stored`; failures are logged and skipped; returns connected ids. (See Finding 6: v2 already reads these env vars itself; this is the specified fallback.)

- [ ] **Step 1: Write the failing test**

```python
class _KeyClient:
    def __init__(self, *, fail: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self._fail = fail

    async def connect_provider_key(self, provider_id: str, api_key: str) -> bool:
        self.calls.append((provider_id, api_key))
        if provider_id == self._fail:
            raise RuntimeError("boom")
        return True


async def test_connect_env_provider_keys_skips_stored_and_failures() -> None:
    client = _KeyClient(fail="groq")
    connected = await bridge.connect_env_provider_keys(
        client,
        stored={"anthropic"},
        environ={
            "ANTHROPIC_API_KEY": "a",
            "OPENAI_API_KEY": " o ",
            "GEMINI_API_KEY": "g1",
            "GOOGLE_GENERATIVE_AI_API_KEY": "g2",
            "GROQ_API_KEY": "q",
        },
    )
    assert connected == ["openai", "google"]
    # google connects once (first matching var); groq failed and is skipped.
    assert client.calls == [("openai", "o"), ("google", "g1"), ("groq", "q")]


async def test_connect_env_provider_keys_noop_without_env() -> None:
    client = _KeyClient()
    assert await bridge.connect_env_provider_keys(client, environ={}) == []
    assert client.calls == []
```

(The repo runs async tests without a marker, as `tests/runner/test_opencode_policy_evaluator.py` does.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k connect_env -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'connect_env_provider_keys'`.

- [ ] **Step 3: Write minimal implementation**

Add `import logging` to `bridge.py` imports and, below the imports, `_logger = logging.getLogger(__name__)`.
Add `from collections.abc import Iterable, Mapping` and `from typing import Protocol`. Then add:

```python
class _ProviderKeyClient(Protocol):
    async def connect_provider_key(self, provider_id: str, api_key: str) -> bool: ...


async def connect_env_provider_keys(
    client: _ProviderKeyClient,
    *,
    stored: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """
    Store provider API keys from the environment in the per-session credential DB.

    Fallback for providers with no seeded credential: each provider whose API-key
    env var is set is connected with ``POST /api/integration/{id}/connect/key``.

    :param client: The per-session OpenCode client.
    :param stored: Provider ids that already have a credential.
    :param environ: Environment to read; ``None`` uses ``os.environ``.
    :returns: Provider ids connected.
    """
    from omnigent.onboarding.opencode_auth import _ENV_PROVIDER_VARS

    env = os.environ if environ is None else environ
    skip = set(stored)
    connected: list[str] = []
    for provider_id, _label, var in _ENV_PROVIDER_VARS:
        if provider_id in skip:
            continue
        key = env.get(var, "").strip()
        if not key:
            continue
        skip.add(provider_id)
        try:
            ok = await client.connect_provider_key(provider_id, key)
        except Exception:  # noqa: BLE001 - best effort; opencode can still read the env itself.
            _logger.info("opencode env key connect failed for %s", provider_id, exc_info=True)
            continue
        if ok:
            connected.append(provider_id)
    return connected
```

Call site (Stage 4 owns the post-start block): in `orchestration.py`, immediately after
`client = server.client()` / `try:` (current `:1679-1680`), add

```python
            await connect_env_provider_keys(client, stored=seeded_provider_ids(bridge_dir))
```

and import `connect_env_provider_keys, seeded_provider_ids` in the bridge import block
(`orchestration.py:1450-1458`). If Stage 4 has already restructured that block, add the call right
after the client is created and before `create_session`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k connect_env -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py tests/test_opencode_native_bridge.py omnigent/runner/native/orchestration.py
git commit -m "feat(opencode-native): connect provider env keys when no credential is seeded"
```

---

### Task 62: `applied_model` in `state.json`

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:236-268` (dataclass), `:568-603` (`write_bridge_state`), `:625-671` (`read_bridge_state`), `:720-744` (`update_model_override` docstring); add `update_applied_model`
- Test: `tests/test_opencode_native_bridge.py:63-127`

**Interfaces:**
- Consumes: nothing new.
- Produces: `OpenCodeNativeBridgeState.applied_model: str | None = None` (the `provider/model` last sent with `set_model`, spec section 2) and `update_applied_model(bridge_dir: Path, applied_model: str | None) -> bool`. The executor (Stage 1/4) calls `set_model` only when `model_override != applied_model`, then records it.

- [ ] **Step 1: Write the failing test**

```python
def test_applied_model_round_trips_and_updates(bridge_dir: Path) -> None:
    write_bridge_state(bridge_dir, _state(bridge_dir, model_override="openai/gpt-5.5"))
    state = read_bridge_state(bridge_dir)
    assert state is not None and state.applied_model is None
    assert bridge.update_applied_model(bridge_dir, " openai/gpt-5.5 ") is True
    state = read_bridge_state(bridge_dir)
    assert state is not None
    assert state.applied_model == "openai/gpt-5.5"
    assert state.model_override == "openai/gpt-5.5"
    assert bridge.update_applied_model(bridge_dir, None) is True
    assert read_bridge_state(bridge_dir).applied_model is None  # type: ignore[union-attr]


def test_update_applied_model_without_state(bridge_dir: Path) -> None:
    assert bridge.update_applied_model(bridge_dir, "a/b") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k applied_model -v`
Expected: FAIL — `AttributeError: 'OpenCodeNativeBridgeState' object has no attribute 'applied_model'`.

- [ ] **Step 3: Write minimal implementation**

In the dataclass (`bridge.py:256-268`) add after `last_event_id: str | None = None`:

```python
    applied_model: str | None = None
```

and in its docstring after `:param last_event_id:` add
`:param applied_model: The ``provider/model`` last applied with ``set_model``, or ``None``.`

In `write_bridge_state`'s payload (`bridge.py:583-595`) add after `"last_event_id": state.last_event_id,`:

```python
                    "applied_model": state.applied_model,
```

In `read_bridge_state`'s constructor call (`bridge.py:659-671`) add after
`last_event_id=_opt_str("last_event_id"),`:

```python
        applied_model=_opt_str("applied_model"),
```

Replace the `update_model_override` docstring paragraph at `bridge.py:724-729` with:

```python
    The executor compares ``model_override`` with ``applied_model`` before each
    web-injected prompt and calls ``set_model`` when they differ. A blank value
    clears the override (fall back to opencode's own default).
```

Add after `update_model_override`:

```python
def update_applied_model(bridge_dir: Path, applied_model: str | None) -> bool:
    """
    Record the model last applied to the OpenCode session with ``set_model``.

    :param bridge_dir: Native OpenCode bridge directory.
    :param applied_model: ``provider/model`` just applied, or ``None`` to clear.
    :returns: ``True`` when state existed and was updated.
    """
    state = read_bridge_state(bridge_dir)
    if state is None:
        return False
    import dataclasses

    normalized = applied_model.strip() if isinstance(applied_model, str) else None
    write_bridge_state(bridge_dir, dataclasses.replace(state, applied_model=normalized or None))
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_bridge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py tests/test_opencode_native_bridge.py
git commit -m "feat(opencode-native): record the applied model in bridge state"
```

---

### Task 63: Assemble the v2 `opencode.json` in the runner

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:1491-1502` (imports), `:1504-1651` (assembly), `:1653-1657` (credential seeding)
- Test: `tests/test_opencode_native_provider.py` (assembly contract via `build_opencode_config`), import smoke

**Interfaces:**
- Consumes: `resolve_bound_opencode_gateway`, `resolve_databricks_gateway` (unchanged), `managed_connect_opencode_config(xdg_config_home, bridge_dir)` (Task 58), `build_opencode_mcp_block`, `build_opencode_omnigent_mcp_server` (Task 53), `write_opencode_policy_plugin` (Task 57), `write_opencode_instructions` (Task 56), `build_opencode_config` (Task 54), `maybe_merge_user_provider_config` (Task 55), `write_opencode_provider_config`, `seed_opencode_auth` (Task 60), `_native_startup_raw_instructions_from_spec` (`orchestration.py:6753`).
- Produces: the per-session `<bridge>/xdg-config/opencode/opencode.json` is **always** written, with `permissions` ask-all (v1 only set `permission: "ask"` when an MCP block existed, `:1598-1601`). `model_override` keeps its meaning for the rest of the function (Stage 4).

Delete: `config["permission"] = "ask"` (`:1601`), the v1 `config["plugin"]` list assembly
(`:1617-1622`), `build_opencode_provider_config(gateway)` (`:1531`), and
`build_opencode_model_default_config(model_override)` (`:1577-1584`).

- [ ] **Step 1: Write the failing test**

The assembly is glue over unit-tested builders; pin the end-to-end shape with a test that mirrors the
runner's calls:

```python
def test_runner_assembly_shape_matches_v2_contract(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Mirror of the runner's opencode.json assembly (orchestration.py)."""
    from omnigent.harnesses.opencode_native.bridge import write_opencode_policy_plugin
    from omnigent.harnesses.opencode_native.provider import (
        build_opencode_config,
        build_opencode_mcp_block,
        write_opencode_instructions,
    )

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "no-user-config"))
    bridge_dir = tmp_path / "bridge"
    xdg = bridge_dir / "xdg-config"
    mcp_servers = build_opencode_mcp_block([])
    mcp_servers.update(build_opencode_omnigent_mcp_server(bridge_dir))
    plugin_paths = [str(write_opencode_policy_plugin(bridge_dir))]
    instructions = write_opencode_instructions(xdg, "Agent rules.")
    config = maybe_merge_user_provider_config(
        build_opencode_config(
            model="anthropic/claude-sonnet-4-5",
            gateway=None,
            mcp_servers=mcp_servers,
            plugin_paths=plugin_paths,
            instructions=str(instructions),
        )
    )
    path = write_opencode_provider_config(xdg, config)
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["permissions"] == [{"action": "*", "resource": "*", "effect": "ask"}]
    assert written["mcp"]["servers"]["omnigent"]["codemode"] is False
    assert written["plugins"] == [str(bridge_dir / "omnigent-policy")]
    assert written["instructions"] == [str(xdg / "opencode" / "AGENTS.md")]
    assert written["model"] == "anthropic/claude-sonnet-4-5"
    assert not {"provider", "permission", "plugin"} & set(written)
```

Then add the import-smoke check (the runner module imports the new names lazily inside the
function, so exercise them):

```python
def test_runner_imports_v2_builders() -> None:
    import omnigent.harnesses.opencode_native.provider as prov

    for name in (
        "build_opencode_config",
        "build_opencode_mcp_block",
        "build_opencode_omnigent_mcp_server",
        "managed_connect_opencode_config",
        "maybe_merge_user_provider_config",
        "resolve_bound_opencode_gateway",
        "resolve_databricks_gateway",
        "write_opencode_instructions",
        "write_opencode_provider_config",
    ):
        assert hasattr(prov, name), name
    for removed in ("build_opencode_provider_config", "build_opencode_model_default_config"):
        assert not hasattr(prov, removed), removed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_provider.py -k "runner_" -v`
Expected: PASS for the assembly-shape test (builders exist after Tasks 45–51) and PASS for the import
test; the failing signal for this task is the runner itself:

Run: `grep -n "build_opencode_provider_config\|build_opencode_model_default_config\|config\[\"permission\"\]\|config\[\"plugin\"\]" omnigent/runner/native/orchestration.py`
Expected: hits at `:1494`, `:1496`, `:1531`, `:1584`, `:1601`, `:1620` (the runner still uses v1 builders and would raise `ImportError` at launch).

- [ ] **Step 3: Write minimal implementation**

Replace `orchestration.py:1491-1502`:

```python
    from omnigent.harnesses.opencode_native.bridge import xdg_config_home_for_bridge_dir
    from omnigent.harnesses.opencode_native.provider import (
        build_opencode_mcp_block,
        build_opencode_model_default_config,
        build_opencode_omnigent_mcp_server,
        build_opencode_provider_config,
        managed_connect_opencode_config,
        maybe_merge_user_provider_config,
        resolve_bound_opencode_gateway,
        resolve_databricks_gateway,
        write_opencode_provider_config,
    )
```

with:

```python
    from omnigent.harnesses.opencode_native.bridge import xdg_config_home_for_bridge_dir
    from omnigent.harnesses.opencode_native.provider import (
        build_opencode_config,
        build_opencode_mcp_block,
        build_opencode_omnigent_mcp_server,
        managed_connect_opencode_config,
        maybe_merge_user_provider_config,
        resolve_bound_opencode_gateway,
        resolve_databricks_gateway,
        write_opencode_instructions,
        write_opencode_provider_config,
    )
```

Replace `orchestration.py:1504-1651` (from `# Accumulate the synthesized opencode.json` through
`write_opencode_provider_config(xdg_config_home_for_bridge_dir(bridge_dir), config)`) with:

```python
    # Synthesize the per-session v2 opencode.json: providers/model, MCP servers,
    # plugins, instructions, and an ask-all permission ruleset so every tool call
    # raises permission.asked for the forwarder's policy gate.
    xdg_config_home = xdg_config_home_for_bridge_dir(bridge_dir)
    managed_opencode_broker_cmd: str | None = None
    extra_providers: dict[str, dict[str, object]] = {}
    plugin_paths: list[str] = []
    # A spec/CLI-selected gateway wins first, exactly as claude/codex/pi resolve the
    # spec provider before their broker fallback; the ucode config is the last resort.
    opencode_spec = agent_spec.spec if isinstance(agent_spec, ResolvedSpec) else agent_spec
    gateway = await asyncio.to_thread(
        resolve_bound_opencode_gateway,
        model=model_override,
        auth=opencode_spec.executor.auth if opencode_spec is not None else None,
    )
    if gateway is None:
        gateway = resolve_databricks_gateway(
            _opencode_native_profile_from_spec(agent_spec), model_id=model_override
        )
    if gateway is not None:
        model_override = gateway.qualified_model
    else:
        # Managed connect host: reuse ucode's providers with an Omnigent-owned v2
        # auth plugin that mints per request via the broker command. Offloaded to a
        # thread because it may run ``ucode configure`` on first launch.
        managed_config = await asyncio.to_thread(
            managed_connect_opencode_config, xdg_config_home, bridge_dir
        )
        if managed_config:
            from omnigent.host.databricks_credential import (
                _read_sidecar,
                _sidecar_path,
                broker_token_command,
            )

            _oc_sidecar = _read_sidecar(_sidecar_path())
            managed_opencode_broker_cmd = (
                broker_token_command(_oc_sidecar["workspace_host"]) if _oc_sidecar else None
            )
            if managed_opencode_broker_cmd:
                providers = managed_config.get("providers")
                if isinstance(providers, dict):
                    extra_providers = providers
                managed_plugins = managed_config.get("plugins")
                if isinstance(managed_plugins, list):
                    plugin_paths.extend(p for p in managed_plugins if isinstance(p, str))
                pinned = managed_config.get("model")
                if isinstance(pinned, str):
                    if model_override and model_override != pinned:
                        _logger.info(
                            "opencode managed connect: replacing requested model %r with the "
                            "ucode-pinned served model %r (the workspace gateway is the only "
                            "provider on this host).",
                            model_override,
                            pinned,
                        )
                    model_override = pinned
            else:
                _logger.warning(
                    "opencode managed connect: ucode config resolved but no broker command "
                    "(sidecar missing/mismatched); leaving opencode on its own login."
                )

    # MCP: the Omnigent builtin-tool relay (only when it will be started below, so
    # serve-mcp finds tool_relay.json) plus the agent's own declared servers.
    mcp_servers = build_opencode_mcp_block(_opencode_native_mcp_servers_from_spec(agent_spec))
    if server_client is not None and ensure_comment_relay is not None:
        mcp_servers.update(build_opencode_omnigent_mcp_server(bridge_dir))

    # The policy plugin gates REQUEST (TUI-typed prompts) and TOOL_RESULT phases the
    # permission.asked path cannot reach; coordinates come from the OMNIGENT_* env.
    policy_env: dict[str, str] = {}
    if managed_opencode_broker_cmd:
        # The ucode auth plugin runs ``ucode auth-token``, which mints from this command.
        policy_env["DATABRICKS_BEARER_COMMAND"] = managed_opencode_broker_cmd
    runner_server_url = os.environ.get("RUNNER_SERVER_URL")
    if server_client is not None and runner_server_url:
        plugin_paths.append(str(write_opencode_policy_plugin(bridge_dir)))
        policy_env["OMNIGENT_POLICY_URL"] = runner_server_url
        policy_env["OMNIGENT_SESSION_ID"] = session_id
        # The plugin re-reads tool_relay.json per call, picking up the relay once it starts.
        from omnigent.harnesses.claude_native.bridge import _TOOL_RELAY_FILE

        policy_env["OMNIGENT_RELAY_FILE"] = str(bridge_dir / _TOOL_RELAY_FILE)
        # Fallback routing headers for calls made before the relay starts.
        from omnigent.runner._entry import _make_auth_token_factory

        _policy_factory = _make_auth_token_factory()
        _policy_token = _policy_factory() if _policy_factory is not None else None
        if _policy_token:
            from omnigent.cli_auth import databricks_request_headers

            policy_env["OMNIGENT_POLICY_HEADERS"] = json.dumps(
                databricks_request_headers(runner_server_url, bearer_token=_policy_token)
            )

    # opencode 2.0 ignores config ``instructions``; the per-session global AGENTS.md is read.
    instructions_path = write_opencode_instructions(
        xdg_config_home, _native_startup_raw_instructions_from_spec(agent_spec)
    )
    config = build_opencode_config(
        model=model_override,
        gateway=gateway,
        mcp_servers=mcp_servers,
        plugin_paths=plugin_paths,
        instructions=str(instructions_path) if instructions_path is not None else None,
        extra_providers=extra_providers,
    )
    # The per-session XDG_CONFIG_HOME hides the user's global config; carry over
    # their providers, default model, plugins and MCP servers (v1 or v2 spelling).
    config = maybe_merge_user_provider_config(config)
    write_opencode_provider_config(xdg_config_home, config)
```

Replace the credential block `orchestration.py:1653-1657`:

```python
    # The server runs with a per-session XDG_DATA_HOME, so copy the user's
    # `opencode auth login` credentials in — otherwise it can't authenticate
    # their providers and falls back to the no-auth default model. No-op on a
    # remote runner (no local auth.json) / Databricks-gateway path.
    seed_opencode_auth(bridge_dir)
```

with:

```python
    # The per-session DB imports $XDG_DATA_HOME/opencode/auth.json once when it is
    # created; seed it with the user's auth.json and v2 SQLite credentials.
    seed_opencode_auth(bridge_dir)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_provider.py tests/test_opencode_native_bridge.py tests/test_opencode_native_permissions.py tests/runner/test_opencode_policy_evaluator.py tests/runner/test_opencode_resume.py tests/policies/builtins/test_safety.py tests/onboarding/test_opencode_auth.py tests/onboarding/test_harness_readiness.py -v`
Expected: PASS

Run: `grep -n "build_opencode_provider_config\|build_opencode_model_default_config\|config\[\"permission\"\]\|config\[\"plugin\"\]\|permission.v2.asked" -r omnigent`
Expected: no output.

Run: `uv run ruff check omnigent/runner/native/orchestration.py omnigent/harnesses/opencode_native omnigent/onboarding/opencode_auth.py omnigent/policies/builtins/safety.py`
Expected: `All checks passed!`

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/test_opencode_native_provider.py
git commit -m "feat(opencode-native): assemble v2 opencode.json with ask-all permissions in the runner"
```

---

## Stage 3 manual verification (after Task 63, with Stages 1–2 landed)

1. `pre-commit run --all-files` — clean.
2. Launch: `omni opencode --model anthropic/claude-sonnet-4-5` from a repo with an agent spec that has
   instructions and one `mcp_servers` entry.
3. Inspect `~/.omnigent/opencode-native/<hash>/xdg-config/opencode/opencode.json`: `permissions`
   ask-all, `mcp.servers.omnigent.codemode == false`, `plugins == ["…/omnigent-policy"]`,
   `instructions == ["…/AGENTS.md"]`, no `provider`/`permission`/`plugin` keys. `AGENTS.md` holds the
   agent instructions.
4. In the web UI, ask the agent to run `ls`: an approval card appears (`shell` action); approve → the
   tool runs; deny → the model sees a rejection.
5. Enable "Require Approval for File & Shell Operations": a `read` of a file prompts with the path in
   the reason.
6. Add a `block_skills(["deploy"])` policy and ask the agent to load skill `deploy` → denied.
7. Add a REQUEST-phase deny policy (e.g. block text containing "forbidden"), type "forbidden" in the
   TUI → the prompt is rejected; the opencode server log contains "Omnigent policy blocked this prompt".
8. Add a TOOL_RESULT deny policy → the model sees "[Omnigent policy withheld this tool result: …]".
9. Credentials: with only a v2 login (`opencode auth login` after moving `auth.json` aside), start a
   new conversation → the per-session `xdg-data/opencode/auth.json` lists the provider and the
   pinned model runs. `omnigent setup` shows OpenCode ready.
10. Managed connect host (if available): the session config has `providers.databricks-*` and
    `plugins` contains `…/omnigent-ucode-auth`; a turn succeeds and a token refresh (wait past expiry)
    still works.
## Stage 4: Session commands (launch, resume, fork, clear, model switch, compact, interrupt) and session import

**Spec:** section 5 (session commands, import) plus the runner direct calls from section 2 ("the `/compact` dispatch drops model resolution; the model-options fallback reads `GET /api/model`").

**Depends on (must be merged first):**
- Stage 1: `OpenCodeClient` v2 surface (`get_session`, `create_session(title=, directory=, permissions=, model=, metadata=)`, `list_messages`, `seed_context(session_id, text) -> None`, `set_model`, `interrupt`, `compact`, `fork(session_id, *, before=None)`, `list_models`), module-level `_unwrap`, the existing `OpenCodeClientError` class and `OpenCodeSession` dataclass (fields `id/title/parent_id/directory/raw/model`, all but `id` defaulted), `client_for_state(*, base_url, auth_secret, directory=None)` in `app_server.py`, `OpenCodeNativeServer` launching `opencode serve ... --stdio` with `OPENCODE_DB=<bridge_dir>/opencode.db`, and `build_tui_command(...)` already wired into `_auto_create_opencode_terminal`.
- Stage 3: `omnigent/harnesses/opencode_native/provider.py::ASK_ALL_PERMISSIONS` and the rewritten `opencode.json` assembly block inside `_auto_create_opencode_terminal` (this stage does not touch that block).

**Convention for this stage's test snippets:** snippets that append tests show their new `import` lines next to the tests for readability; move those imports into the file's top-level import block when applying them (ruff `E402`/`I001` run in pre-commit).

**Decisions recorded in this stage (with evidence):**

1. **Native fork uses a SQLite snapshot of the source conversation's DB plus a claim release.** One `opencode serve` runs per conversation with `OPENCODE_DB=<bridge_dir>/opencode.db` (Stage 1), so `POST /api/session/{id}/fork` can only see the source session if the clone's server opens a DB that contains it. The runner takes a `sqlite3` online backup of `<source_bridge_dir>/opencode.db` into `<clone_bridge_dir>/opencode.db` before the clone's server starts, then calls `client.fork(source_session_id, before=None)`. The backup API is safe while the source server is writing (WAL). The copy must also clear execution claims: v2 records a durable "turn in flight" claim in `session_v2.time_suspended` and **resumes that turn on the next server start** (`packages/core/src/session/execution.ts:79-83`: "Terminals release it — except shutdown interruption, which preserves the claim so the next server start resumes the turn"; claim write in `packages/core/src/session/store.ts:205-216`; table `session_v2` in `packages/core/src/session/sql.ts:22-23`). Without `UPDATE session_v2 SET time_suspended = NULL` on the copy, forking a busy source would re-run the source's in-flight turn (and its tools) inside the clone's server. If the table or column is missing (schema drift) the copy is deleted and the fork falls back to the text preamble. `session.fork` copies the parent's `directory`, `metadata` and `permission` columns (`packages/core/src/session/projector.ts:147-165`), so the forked session keeps `ASK_ALL_PERMISSIONS`; its `metadata.omnigent_conversation` still names the source conversation (cosmetic, not read by Omnigent). Native fork is only attempted when the source bridge state's workspace equals the clone's workspace, because the forked session inherits the parent's directory.
   - Rejected alternative: `GET /api/experimental/session/{id}/export` + `POST /api/experimental/session/import` needs the source server to be live and both routes are `experimental`.
2. **The fork route must stamp the source OpenCode session id for opencode targets.** Today `resume_source_native_session` is `False` for every PREAMBLE harness (`omnigent/server/routes/sessions/routes_core.py:3300-3304`, `not target_is_cursor`), so `omnigent.fork.source_external_session_id` is never stamped for opencode and a truncated fork is indistinguishable from a full one at the runner. Task 66 lets opencode-native targets keep the directive; the store still skips it for truncated or cross-family forks (`omnigent/stores/conversation_store/sqlalchemy_store.py:4721`). `fork_history=PREAMBLE` stays in `omnigent/harness_plugins.py:509` and opencode stays out of `_FORK_HISTORY_NATIVE_HARNESSES` (the spec does not change it).
3. **`/compact` calls `POST /api/session/{id}/compact` with no model.** The v2 body is `{id?, delivery?}` only (`packages/protocol/openapi.json` operation `session.compact`: "Durably admit a session compaction request. Steers by default"). `_resolve_opencode_compact_model` and the last-assistant-message model lookup are deleted.
4. **Model options come only from `GET /api/model`.** v2 `opencode models` is a thin wrapper over the same endpoint and prints `${model.providerID}/${model.id}` (`packages/cli/src/commands/handlers/models.ts:20-23`); without `--server` it connects to the background service, which the harness never uses. `list_opencode_cli_model_options` is deleted. v2 `Model.Info` is `{id, modelID, providerID, name, variants[], status: "alpha"|"beta"|"deprecated"|"active", enabled, ...}` (`packages/schema/src/model.ts:118-142`); a `Model.Ref` is `{id, providerID, variant?}` parsed from `provider/id#variant` (`packages/schema/src/model.ts:18-42`).
5. **Session import lists and reads through a short-lived `opencode serve --stdio` against the user's own store.** `opencode session list --format json` still exists in 2.0.18:
   ```
   $ opencode session list --help
   DESCRIPTION
     List top-level sessions in the current project, newest first
   USAGE
     opencode session list [flags]
   FLAGS
     --standalone               Run with a private server instead of the background service
     --server string            Connect to a server URL instead of the background service
     --max-count, -n integer    Limit to N most recent sessions (default: 100)
     --format choice            Output format (choices: table, json)
   ```
   but it is scoped to the cwd's project (`packages/cli/src/commands/handlers/session/list.ts:18-33` passes `project: location.project.id`) and, without `--server`/`--standalone`, talks to the background service. v1's `--pure` flag is gone. The import therefore lists with `GET /api/session?parentID=null&order=desc&limit=N` on its own server, which applies no project filter when `project`/`directory` are omitted (`packages/core/src/session/store.ts:99-110`). Session fields: `Session.Info {id, parentID?, title?, location: {directory}, time: {created, updated (epoch ms)}, ...}` (`packages/schema/src/session.ts:30-59`). Messages come from `client.list_messages(session_id)`, whose items are `Session.Message.Info` (`packages/schema/src/session-message.ts:295-307`): `user {id, text, files?: [{data (base64), mime, source, name?}]}` (lines 73-81, `packages/schema/src/prompt.ts:26-34`), `assistant {id, content[]}` (lines 211-236) with `content[]` items `text {text}` (176-181), `reasoning {text}` (183-192), `tool {id, name, state}` (160-174); `state` is `streaming {input: string}` | `running {input, metadata}` | `completed {input, content: [TextContent|FileContent], metadata?}` | `error {input, error: {type, message}, content?}` (125-157). `Tool.TextContent {type:"text", text}` / `Tool.FileContent {type:"file", uri, mime, name?}` are in `packages/schema/src/tool.ts:67-83`. Other message types (`synthetic`, `system`, `shell`, `compaction`, `idle`, `*-switched`) are skipped, as v1 skipped non user/assistant messages.
6. **Interrupt gets a native handler.** The executor only injects and returns, so the in-process cancel the runner falls back to today never reaches a turn that OpenCode is running. `NativeInterruptRunner` now calls `client.interrupt(session_id)` for opencode-native (stop aliases to interrupt, like codex/pi). The forwarder's `session.execution.interrupted` handler (Stage 2) publishes the idle edge.
7. **Clear launches fresh.** The clear handler passes `fresh=True` so a cleared forked clone does not re-run the fork or preamble seeding (today it re-seeds the fork preamble because `fork_carry_history` is still set once `external_session_id` is cleared).
8. **The cost popup, blocked notice, cold boot (`omnigent/runner/app.py:7368-7475`, `9135-9165`) and `_build_spawn_env_from_spec` (`omnigent/runner/app.py:13570+`, no opencode branch) read no v1 fields and are unchanged.**

**v1-only code deleted in this stage:**

| Location (current tree) | What |
|---|---|
| `omnigent/runner/native/orchestration.py:1970-2024` | `_resolve_opencode_compact_model` |
| `omnigent/runner/native/orchestration.py:1678-1739` | inline resume/create/rehydrate block using `create_session({"title": ...})` |
| `omnigent/runner/native/orchestration.py:2139-2146` | `provider_id/model_id` split + `seed_context(..., provider_id=, model_id=)` |
| `omnigent/runner/native/__init__.py:119,254` | `_resolve_opencode_compact_model` re-export |
| `omnigent/runner/app.py:148` | `_resolve_opencode_compact_model` import |
| `omnigent/runner/app.py:6916-6932` | `get_session` + `list_messages` + model resolution + `client.summarize(...)` |
| `omnigent/runner/app.py:6946-6973` | `filtered_server_env` + `list_opencode_cli_model_options` CLI path |
| `omnigent/harnesses/opencode_native/app_server.py:97-98,186-253` | `_ANSI_RE` and `list_opencode_cli_model_options` (line numbers before Stage 1; locate by name) |
| `omnigent/session_import/local.py:9,30-34,152-187,281-305,1332-1522` | `subprocess` import, `_run_opencode_json`, `session list --pure`, `export --pure`, v1 `parts[]` parser, `opencode_tool_output_text` import |
| `tests/runner/test_app_sessions_native_events_options.py:1436-1851` | v1 `summarize` fakes and `_resolve_opencode_compact_model` tests |
| `tests/test_opencode_native_app_server.py:386-411` | `test_list_opencode_cli_model_options` |
| `tests/test_session_import.py:174-327` | v1 CLI list/export import tests |

---

### Task 64: Per-bridge OpenCode DB path and fork snapshot copy

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:26-36` (imports), append after `xdg_config_home_for_bridge_dir` (`bridge.py:436-443`)
- Modify: `omnigent/harnesses/opencode_native/app_server.py` (`filtered_server_env`, the `OPENCODE_DB` assignment Stage 1 added)
- Test: `tests/test_opencode_native_bridge.py`, `tests/test_opencode_native_app_server.py`

**Interfaces:**
- Consumes: Stage 1 `filtered_server_env(*, bridge_dir, auth_secret, extra_env=None)` which sets `env["OPENCODE_DB"]`.
- Produces:
  - `OPENCODE_DB_FILENAME: str = "opencode.db"`
  - `database_path_for_bridge_dir(bridge_dir: Path) -> Path`
  - `copy_opencode_database_for_fork(source_bridge_dir: Path, dest_bridge_dir: Path) -> bool`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_opencode_native_bridge.py`:

```python
import sqlite3

from omnigent.harnesses.opencode_native.bridge import (
    copy_opencode_database_for_fork,
    database_path_for_bridge_dir,
)


def _make_opencode_db(path: Path, *, claimed: bool) -> None:
    """Create a minimal v2-shaped OpenCode DB with one session row."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, time_suspended INTEGER)")
        conn.execute(
            "INSERT INTO session_v2 (id, time_suspended) VALUES (?, ?)",
            ("ses_src", 1_700_000_000_000 if claimed else None),
        )
    conn.close()


def test_database_path_for_bridge_dir(tmp_path: Path) -> None:
    assert database_path_for_bridge_dir(tmp_path) == tmp_path / "opencode.db"


def test_copy_opencode_database_for_fork_releases_claims(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    _make_opencode_db(database_path_for_bridge_dir(source_dir), claimed=True)

    assert copy_opencode_database_for_fork(source_dir, dest_dir) is True

    dest = database_path_for_bridge_dir(dest_dir)
    with sqlite3.connect(dest) as conn:
        rows = conn.execute("SELECT id, time_suspended FROM session_v2").fetchall()
    conn.close()
    assert rows == [("ses_src", None)], "a copied in-flight claim would re-run the source turn"
    assert (dest.stat().st_mode & 0o777) == 0o600
    # The source keeps its own claim; only the copy is released.
    with sqlite3.connect(database_path_for_bridge_dir(source_dir)) as conn:
        assert conn.execute("SELECT time_suspended FROM session_v2").fetchone()[0] is not None
    conn.close()


def test_copy_opencode_database_for_fork_missing_source(tmp_path: Path) -> None:
    (tmp_path / "dest").mkdir()
    assert copy_opencode_database_for_fork(tmp_path / "source", tmp_path / "dest") is False
    assert not database_path_for_bridge_dir(tmp_path / "dest").exists()


def test_copy_opencode_database_for_fork_unknown_schema_leaves_no_copy(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()
    with sqlite3.connect(database_path_for_bridge_dir(source_dir)) as conn:
        conn.execute("CREATE TABLE unrelated (id TEXT)")
    conn.close()

    assert copy_opencode_database_for_fork(source_dir, dest_dir) is False
    assert not database_path_for_bridge_dir(dest_dir).exists()
```

Append to `tests/test_opencode_native_app_server.py`:

```python
def test_filtered_server_env_points_opencode_db_at_bridge_dir(tmp_path: Path) -> None:
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["OPENCODE_DB"] == str(tmp_path / "opencode.db")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_opencode_native_bridge.py -k "database or copy_opencode" -v`
Expected: FAIL with "ImportError: cannot import name 'copy_opencode_database_for_fork'"

- [ ] **Step 3: Write minimal implementation**

In `omnigent/harnesses/opencode_native/bridge.py`, add `contextlib` and `sqlite3` to the stdlib imports (lines 26-32 become):

```python
import base64
import contextlib
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
```

Append after `xdg_config_home_for_bridge_dir` (current `bridge.py:436-443`):

```python
OPENCODE_DB_FILENAME = "opencode.db"


def database_path_for_bridge_dir(bridge_dir: Path) -> Path:
    """
    Return the per-conversation ``OPENCODE_DB`` path for *bridge_dir*.

    :param bridge_dir: Native OpenCode bridge directory.
    :returns: Absolute path of the conversation's OpenCode SQLite database.
    """
    return bridge_dir / OPENCODE_DB_FILENAME


def _remove_database_files(database: Path) -> None:
    """Delete a SQLite database and its WAL/SHM side files, if present."""
    for suffix in ("", "-wal", "-shm"):
        with contextlib.suppress(FileNotFoundError):
            database.with_name(database.name + suffix).unlink()


def copy_opencode_database_for_fork(source_bridge_dir: Path, dest_bridge_dir: Path) -> bool:
    """
    Snapshot a source conversation's OpenCode DB into a fork's bridge dir.

    The fork's own ``opencode serve`` must see the source session to run
    ``POST /api/session/{id}/fork``. The copy releases execution claims
    (``session_v2.time_suspended``) so the fork's server does not resume the
    source's unfinished turn on boot.

    :param source_bridge_dir: Bridge dir of the conversation being forked.
    :param dest_bridge_dir: Bridge dir of the new (forked) conversation.
    :returns: ``True`` when the copy is in place; ``False`` (and no copy left
        behind) when the source DB is missing or its schema is unrecognized.
    """
    source = database_path_for_bridge_dir(source_bridge_dir)
    if not source.is_file():
        return False
    dest = database_path_for_bridge_dir(dest_bridge_dir)
    dest_bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    _remove_database_files(dest)
    try:
        with (
            contextlib.closing(sqlite3.connect(source, timeout=10.0)) as src,
            contextlib.closing(sqlite3.connect(dest)) as dst,
        ):
            src.backup(dst)
            dst.execute("UPDATE session_v2 SET time_suspended = NULL")
            dst.commit()
    except sqlite3.Error:
        _remove_database_files(dest)
        return False
    os.chmod(dest, 0o600)
    return True
```

In `omnigent/harnesses/opencode_native/app_server.py`, add `database_path_for_bridge_dir` to the `from omnigent.harnesses.opencode_native.bridge import (...)` block and replace the `OPENCODE_DB` line Stage 1 added in `filtered_server_env` (it reads `env["OPENCODE_DB"] = str(bridge_dir / "opencode.db")`) with:

```python
    env["OPENCODE_DB"] = str(database_path_for_bridge_dir(bridge_dir))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_opencode_native_bridge.py tests/test_opencode_native_app_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py omnigent/harnesses/opencode_native/app_server.py tests/test_opencode_native_bridge.py tests/test_opencode_native_app_server.py
git commit -m "feat(opencode-native): snapshot a source conversation DB for native fork"
```

---

### Task 65: Parse fork source labels into the OpenCode launch config

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:1328-1412` (`_OpenCodeNativeLaunchConfig`, `_opencode_native_launch_config`)
- Test: `tests/runner/test_opencode_native_orchestration.py` (create)

**Interfaces:**
- Consumes: `omnigent.stores.conversation_store.FORK_SOURCE_LABEL_KEY`, `FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY`, `FORK_CARRY_HISTORY_LABEL_KEY`.
- Produces: `_OpenCodeNativeLaunchConfig` gains `fork_source_id: str | None = None` and `fork_source_external_id: str | None = None` (after `fork_carry_history`).

- [ ] **Step 1: Write the failing test**

Create `tests/runner/test_opencode_native_orchestration.py`:

```python
"""Tests for opencode-native runner orchestration helpers (launch, fork, TUI args)."""

from __future__ import annotations

from typing import Any

import pytest

import omnigent.runner.native.orchestration as orchestration


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _SnapshotClient:
    """Async client stub returning one session snapshot."""

    def __init__(self, snapshot: dict[str, Any]) -> None:
        self._snapshot = snapshot

    async def get(
        self, url: str, timeout: float | None = None, params: dict[str, str] | None = None
    ) -> _Resp:
        return _Resp(200, self._snapshot)


@pytest.mark.asyncio
async def test_launch_config_reads_fork_source_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "labels": {
            "omnigent.fork.source_id": "conv_source",
            "omnigent.fork.source_external_session_id": "ses_src",
            "omnigent.fork.carry_history": "1",
        },
    }
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_clone",
        server_client=_SnapshotClient(snapshot),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id == "conv_source"
    assert cfg.fork_source_external_id == "ses_src"
    assert cfg.fork_carry_history is True


@pytest.mark.asyncio
async def test_launch_config_without_fork_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    cfg = await orchestration._opencode_native_launch_config(
        session_id="conv_plain",
        server_client=_SnapshotClient({"workspace": "/tmp/repo"}),  # type: ignore[arg-type]
    )
    assert cfg.fork_source_id is None
    assert cfg.fork_source_external_id is None
    assert cfg.fork_carry_history is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -v`
Expected: FAIL with "AttributeError: '_OpenCodeNativeLaunchConfig' object has no attribute 'fork_source_id'"

- [ ] **Step 3: Write minimal implementation**

Replace the dataclass at `orchestration.py:1328-1349` with:

```python
@dataclasses.dataclass(frozen=True)
class _OpenCodeNativeLaunchConfig:
    """
    Persisted launch config for runner-owned OpenCode terminals.

    :param workspace: Workspace cwd for ``opencode serve`` and the TUI.
    :param policy_server_url: Omnigent server URL for the forwarder.
    :param terminal_launch_args: User pass-through OpenCode CLI args.
    :param model_override: Persisted model override, or ``None``.
    :param external_session_id: Existing OpenCode session id to resume.
    :param fork_carry_history: ``True`` on a forked clone whose prior history
        should carry over (``omnigent.fork.carry_history``).
    :param fork_source_id: Source Omnigent conversation id of a forked clone.
    :param fork_source_external_id: Source OpenCode session id, stamped only
        for untruncated same-harness forks.
    """

    workspace: Path
    policy_server_url: str
    terminal_launch_args: list[str] | None
    model_override: str | None
    external_session_id: str | None
    fork_carry_history: bool = False
    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
```

Replace `orchestration.py:1396-1412` (the carry-history comment, label read, and `return`) with:

```python
    # Fork directives are only consulted while the clone has no OpenCode
    # session of its own (see _prepare_opencode_native_fork).
    from omnigent.stores.conversation_store import (
        FORK_CARRY_HISTORY_LABEL_KEY,
        FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
        FORK_SOURCE_LABEL_KEY,
    )

    labels = snapshot.get("labels")
    fork_carry_history = False
    fork_source_id: str | None = None
    fork_source_external_id: str | None = None
    if isinstance(labels, dict):
        fork_carry_history = labels.get(FORK_CARRY_HISTORY_LABEL_KEY) == "1"
        source_id = labels.get(FORK_SOURCE_LABEL_KEY)
        if isinstance(source_id, str) and source_id:
            fork_source_id = source_id
        source_external = labels.get(FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY)
        if isinstance(source_external, str) and source_external:
            fork_source_external_id = source_external
    return _OpenCodeNativeLaunchConfig(
        workspace=_codex_session_workspace(session_workspace),
        policy_server_url=_required_runner_env("RUNNER_SERVER_URL"),
        terminal_launch_args=terminal_launch_args,
        model_override=model_override,
        external_session_id=external_session_id,
        fork_carry_history=fork_carry_history,
        fork_source_id=fork_source_id,
        fork_source_external_id=fork_source_external_id,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py tests/runner/test_codex_native_launch_config.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_native_orchestration.py
git commit -m "feat(opencode-native): read fork source labels at launch"
```

---

### Task 66: Stamp the source OpenCode session on same-harness forks

**Files:**
- Modify: `omnigent/server/routes/_sessions/helpers.py:8243-8266` (docstring of `_agent_carries_cursor_fork_history`; new helper after it), `helpers.py:11329` (`__all__`)
- Modify: `omnigent/server/routes/sessions/__init__.py:354`
- Modify: `omnigent/server/routes/sessions/routes_core.py:123` (import), `routes_core.py:3286-3304`
- Test: `tests/server/routes/test_sessions_fork.py`

**Interfaces:**
- Consumes: `get_agent_cache`, `canonicalize_harness` (same as `_agent_carries_cursor_fork_history`).
- Produces: `_agent_forks_opencode_native_session(agent: Agent) -> bool`; the fork route passes `resume_source_native_session=True` for opencode-native targets (subject to the existing switch/managed guards).

- [ ] **Step 1: Write the failing test**

Append after `test_fork_cursor_pi_native_carry_gating` in `tests/server/routes/test_sessions_fork.py`:

```python
@pytest.mark.parametrize(
    "harness,expect_resume_source",
    [
        # cursor's conversation is server-backed: it can never clone the source.
        ("cursor-native", False),
        # opencode clones the source session natively when the source DB is
        # reachable, so the route keeps the source-session directive for it.
        ("opencode-native", True),
    ],
)
@pytest.mark.asyncio
async def test_fork_preamble_harness_source_directive(
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
    expect_resume_source: bool,
) -> None:
    """A same-agent opencode fork keeps the source native session directive."""
    conv = _make_conversation()
    conv_store = _ConversationStore(
        conversations={"e9f8f58523cec9a57d3bdf93be543e8c": conv},
        items_by_conv={
            "e9f8f58523cec9a57d3bdf93be543e8c": [
                _make_item("9980c8a9248139f14f4165e5d53088aa", "Hi")
            ]
        },
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_agent_cache",
        lambda: _StubAgentCache({"087b7cb7ac30abf4debfaa578d052ec6": harness}),
    )
    client = TestClient(_build_app(conv_store))

    resp = client.post("/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/fork", json={})

    assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
    fork_call = conv_store.fork_calls[0]
    assert fork_call["carry_history_into_native"] is True
    assert fork_call["resume_source_native_session"] is expect_resume_source, (
        f"A {harness} fork should pass resume_source_native_session={expect_resume_source}."
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/routes/test_sessions_fork.py::test_fork_preamble_harness_source_directive -v`
Expected: FAIL on the `opencode-native` case with "resume_source_native_session should be True"

- [ ] **Step 3: Write minimal implementation**

In `helpers.py`, replace the docstring paragraph of `_agent_carries_cursor_fork_history` (lines 8244-8255) with:

```python
    """Return whether *agent*'s native harness carries FORK history via preamble.

    Cursor's conversation is server-backed and opencode keeps one store per
    conversation, so neither can seed a local store for a rebuilt resume; the
    runner replays prior turns as a text preamble on the fork (opencode first
    tries a native fork of the source session). Fork-only — switch-agent does
    not call this, so switching into one still launches fresh. Returns
    ``False`` when the bundle can't be loaded.

    :param agent: The agent whose harness to classify.
    :returns: ``True`` for the cursor-native / opencode-native harnesses.
    """
```

Insert after `_agent_carries_cursor_fork_history` (after `helpers.py:8266`):

```python
def _agent_forks_opencode_native_session(agent: Agent) -> bool:
    """Return whether *agent* runs opencode-native, which can fork the source natively.

    :param agent: The fork target agent.
    :returns: ``True`` when the target harness is ``opencode-native``.
    """
    from omnigent.harness_aliases import canonicalize_harness

    try:
        spec = (
            get_agent_cache()
            .load(agent.id, agent.bundle_location, expand_env=agent.session_id is None)
            .spec
        )
    except Exception:  # noqa: BLE001
        return False
    return canonicalize_harness(spec.executor.harness_kind) == "opencode-native"
```

Add `"_agent_forks_opencode_native_session",` after `"_agent_carries_native_fork_history_impl",` in `__all__` (`helpers.py:11331`). In `omnigent/server/routes/sessions/__init__.py` after line 354 add:

```python
    _agent_forks_opencode_native_session as _agent_forks_opencode_native_session,
```

In `routes_core.py` add `_agent_forks_opencode_native_session,` after `_agent_carries_native_fork_history,` in the import at line 124, then replace `routes_core.py:3286` and `3300-3304` so the block reads:

```python
        target_is_cursor = await asyncio.to_thread(_agent_carries_cursor_fork_history, base_agent)
        target_forks_opencode = await asyncio.to_thread(
            _agent_forks_opencode_native_session, base_agent
        )
        carry_history_into_native = target_is_cursor or await asyncio.to_thread(
            _agent_carries_native_fork_history, base_agent
        )
```

and

```python
        resume_source_native_session = (
            (not switching_agent or copy_model_settings)
            and (not target_is_cursor or target_forks_opencode)
            and body.host_type != "managed"
        )
```

Update the comment above it (`routes_core.py:3289-3299`) by replacing "cursor never clones a native session (server-backed; it carries history via the preamble), so it always skips the source directive too." with "cursor never clones a native session (server-backed; it carries history via the preamble), so it skips the source directive; opencode keeps it to fork the source session natively."

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/server/routes/test_sessions_fork.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/_sessions/helpers.py omnigent/server/routes/sessions/__init__.py omnigent/server/routes/sessions/routes_core.py tests/server/routes/test_sessions_fork.py
git commit -m "feat(opencode-native): keep the source session directive on opencode forks"
```

---

### Task 67: Stage a native fork before the clone's server boots

**Files:**
- Modify: `omnigent/runner/native/orchestration.py` (insert after `_opencode_native_launch_config`, before `_auto_create_opencode_terminal` at line 1415)
- Test: `tests/runner/test_opencode_native_orchestration.py`

**Interfaces:**
- Consumes: Task 64 `copy_opencode_database_for_fork`, `database_path_for_bridge_dir`; Task 65 launch-config fields; `bridge_dir_for_bridge_id`, `read_bridge_state`.
- Produces: `_prepare_opencode_native_fork(launch_config: _OpenCodeNativeLaunchConfig, *, bridge_dir: Path, workspace: str) -> str | None` — the source OpenCode session id to fork, or `None` for the preamble path.

- [ ] **Step 1: Write the failing test**

Append to `tests/runner/test_opencode_native_orchestration.py`:

```python
from pathlib import Path

from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from omnigent.harnesses.opencode_native.bridge import (
    OpenCodeNativeBridgeState,
    database_path_for_bridge_dir,
    write_bridge_state,
)
from omnigent.runner.native.orchestration import (
    _OpenCodeNativeLaunchConfig,
    _prepare_opencode_native_fork,
)


def _fork_config(**overrides: Any) -> _OpenCodeNativeLaunchConfig:
    values: dict[str, Any] = {
        "workspace": Path("/repo"),
        "policy_server_url": "http://127.0.0.1:8123",
        "terminal_launch_args": None,
        "model_override": None,
        "external_session_id": None,
        "fork_carry_history": True,
        "fork_source_id": "conv_source",
        "fork_source_external_id": "ses_src",
    }
    values.update(overrides)
    return _OpenCodeNativeLaunchConfig(**values)


@pytest.fixture
def bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(opencode_bridge, "_BRIDGE_ROOT", tmp_path / "opencode-native")
    return tmp_path / "opencode-native"


def _seed_source(workspace: str) -> None:
    import sqlite3

    source_dir = opencode_bridge.prepare_bridge_dir("conv_source")
    with sqlite3.connect(database_path_for_bridge_dir(source_dir)) as conn:
        conn.execute("CREATE TABLE session_v2 (id TEXT PRIMARY KEY, time_suspended INTEGER)")
        conn.execute("INSERT INTO session_v2 VALUES ('ses_src', NULL)")
    conn.close()
    write_bridge_state(
        source_dir,
        OpenCodeNativeBridgeState(
            session_id="conv_source",
            server_base_url="http://127.0.0.1:1",
            opencode_session_id="ses_src",
            workspace=workspace,
        ),
    )


def test_prepare_native_fork_copies_source_db(bridge_root: Path) -> None:
    _seed_source("/repo")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    source_session = _prepare_opencode_native_fork(
        _fork_config(), bridge_dir=clone_dir, workspace="/repo"
    )

    assert source_session == "ses_src"
    assert database_path_for_bridge_dir(clone_dir).is_file()


@pytest.mark.parametrize(
    "overrides",
    [
        {"external_session_id": "ses_own"},
        {"fork_carry_history": False},
        {"fork_source_id": None},
        {"fork_source_external_id": None},
        {"fork_source_external_id": "0b8f-claude-uuid"},
    ],
)
def test_prepare_native_fork_skips_without_directive(
    bridge_root: Path, overrides: dict[str, Any]
) -> None:
    _seed_source("/repo")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    assert (
        _prepare_opencode_native_fork(
            _fork_config(**overrides), bridge_dir=clone_dir, workspace="/repo"
        )
        is None
    )
    assert not database_path_for_bridge_dir(clone_dir).exists()


def test_prepare_native_fork_skips_other_workspace(bridge_root: Path) -> None:
    _seed_source("/elsewhere")
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")

    assert _prepare_opencode_native_fork(_fork_config(), bridge_dir=clone_dir, workspace="/repo") is None
    assert not database_path_for_bridge_dir(clone_dir).exists()


def test_prepare_native_fork_skips_unreachable_source(bridge_root: Path) -> None:
    clone_dir = opencode_bridge.prepare_bridge_dir("conv_clone")
    assert _prepare_opencode_native_fork(_fork_config(), bridge_dir=clone_dir, workspace="/repo") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -k prepare_native_fork -v`
Expected: FAIL with "ImportError: cannot import name '_prepare_opencode_native_fork'"

- [ ] **Step 3: Write minimal implementation**

Insert before `async def _auto_create_opencode_terminal(` (`orchestration.py:1415`):

```python
def _prepare_opencode_native_fork(
    launch_config: _OpenCodeNativeLaunchConfig,
    *,
    bridge_dir: Path,
    workspace: str,
) -> str | None:
    """
    Stage a same-harness native fork before the clone's server starts.

    Copies the source conversation's OpenCode DB into *bridge_dir* so the
    clone's ``opencode serve`` can fork the source session. Only an unbound
    clone whose source ran opencode in the same workspace qualifies; every
    other case returns ``None`` and the launch falls back to the preamble.

    :param launch_config: The clone's launch config.
    :param bridge_dir: The clone's bridge directory.
    :param workspace: The clone's workspace path.
    :returns: The source OpenCode session id to fork, or ``None``.
    """
    from omnigent.harnesses.opencode_native.bridge import (
        bridge_dir_for_bridge_id,
        copy_opencode_database_for_fork,
        read_bridge_state,
    )

    source_conversation = launch_config.fork_source_id
    source_session = launch_config.fork_source_external_id
    if (
        launch_config.external_session_id is not None
        or not launch_config.fork_carry_history
        or not source_conversation
        or not source_session
        or not source_session.startswith("ses_")
    ):
        return None
    source_bridge_dir = bridge_dir_for_bridge_id(source_conversation)
    source_state = read_bridge_state(source_bridge_dir)
    # A forked session inherits the parent's directory, so only fork in place.
    if source_state is not None and source_state.workspace not in (None, workspace):
        return None
    if not copy_opencode_database_for_fork(source_bridge_dir, bridge_dir):
        return None
    return source_session
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_native_orchestration.py
git commit -m "feat(opencode-native): stage native fork from the source conversation DB"
```

---

### Task 68: Rehydrate a lost session with v2 `seed_context`

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:2095-2153` (`_rehydrate_opencode_session_from_transcript`)
- Test: `tests/runner/test_opencode_resume.py`

**Interfaces:**
- Consumes: Stage 1 `OpenCodeClient.seed_context(session_id: str, text: str) -> None`.
- Produces: `_rehydrate_opencode_session_from_transcript(*, opencode_client, opencode_session_id: str, omnigent_session_id: str, server_client: httpx.AsyncClient | None) -> bool` (the `model_override` parameter is removed).

- [ ] **Step 1: Write the failing test**

Replace `tests/runner/test_opencode_resume.py:34-108` (the `_FakeOpenCodeClient` class and the three rehydrate tests) with:

```python
class _FakeOpenCodeClient:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.seeded: tuple[str, str] | None = None
        self._error = error

    async def seed_context(self, session_id: str, text: str) -> None:
        if self._error is not None:
            raise self._error
        self.seeded = (session_id, text)


# ── _render_opencode_transcript_text ────────────────────────────────────────


def test_render_transcript_extracts_user_assistant_text() -> None:
    assert app._render_opencode_transcript_text(_ITEMS) == "User: hi\n\nAssistant: yo"


def test_render_transcript_skips_non_message_and_other_roles() -> None:
    items = [
        {"type": "reasoning", "text": "ignored"},
        {"type": "message", "role": "tool", "content": [{"text": "ignored"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
    ]
    assert app._render_opencode_transcript_text(items) == "User: hi"


# ── _rehydrate_opencode_session_from_transcript ─────────────────────────────


async def test_rehydrate_seeds_transcript_without_model() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
    )
    assert ok is True
    assert oc.seeded is not None
    session_id, text = oc.seeded
    assert session_id == "ses_1"
    assert text.startswith("[Resumed session")
    assert "User: hi" in text and "Assistant: yo" in text


async def test_rehydrate_no_server_client_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=None,
    )
    assert ok is False
    assert oc.seeded is None


async def test_rehydrate_empty_transcript_returns_false() -> None:
    oc = _FakeOpenCodeClient()
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient([]),
    )
    assert ok is False
    assert oc.seeded is None


async def test_rehydrate_seed_failure_returns_false() -> None:
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError

    oc = _FakeOpenCodeClient(error=OpenCodeClientError("synthetic failed: 500"))
    ok = await app._rehydrate_opencode_session_from_transcript(
        opencode_client=oc,
        opencode_session_id="ses_1",
        omnigent_session_id="conv_1",
        server_client=_FakeServerClient(_ITEMS),
    )
    assert ok is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_resume.py -v`
Expected: FAIL with "TypeError: _rehydrate_opencode_session_from_transcript() missing 1 required keyword-only argument: 'model_override'"

- [ ] **Step 3: Write minimal implementation**

Replace `orchestration.py:2095-2153` with:

```python
async def _rehydrate_opencode_session_from_transcript(
    *,
    opencode_client: OpenCodeClient,
    opencode_session_id: str,
    omnigent_session_id: str,
    server_client: httpx.AsyncClient | None,
) -> bool:
    """
    Seed a fresh OpenCode session with the Omnigent transcript as context.

    Used when the persisted OpenCode session is gone (new host, wiped bridge
    dir) or a forked clone could not fork natively. The transcript is recorded
    through ``seed_context`` without starting a model turn. Best effort.

    :param opencode_client: Client bound to the conversation's server.
    :param opencode_session_id: The freshly created OpenCode session id.
    :param omnigent_session_id: Omnigent conversation id whose items to replay.
    :param server_client: Runner Omnigent server client, or ``None``.
    :returns: ``True`` when prior context was seeded.
    """
    if server_client is None:
        return False
    try:
        resp = await server_client.get(
            f"/v1/sessions/{urllib.parse.quote(omnigent_session_id, safe='')}/items",
            params={"limit": 1000, "order": "asc"},
            timeout=30.0,
        )
        resp.raise_for_status()
        payload = resp.json()
    except (httpx.HTTPError, ValueError):
        _logger.warning(
            "opencode resume: could not fetch transcript for %s",
            omnigent_session_id,
            exc_info=True,
        )
        return False
    items = payload.get("data", []) if isinstance(payload, dict) else []
    transcript = _render_opencode_transcript_text(items if isinstance(items, list) else [])
    if not transcript:
        return False
    text = (
        "[Resumed session — the prior opencode session was unavailable on this "
        "host, so the earlier conversation is included below for context. Treat "
        "it as history; do not re-run prior actions.]\n\n" + transcript
    )
    try:
        await opencode_client.seed_context(opencode_session_id, text)
    except Exception:  # noqa: BLE001 - rehydration is best effort.
        _logger.warning(
            "opencode resume: rehydration seed failed for %s", omnigent_session_id, exc_info=True
        )
        return False
    return True
```

The only caller (`orchestration.py:1699-1705`) is replaced in Task 69/67; until then change its call to drop `model_override=model_override,` so the module imports cleanly.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_resume.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_resume.py
git commit -m "refactor(opencode-native): seed lost-session context with v2 seed_context"
```

---

### Task 69: Resolve the OpenCode session (resume, native fork, create + seed)

**Files:**
- Modify: `omnigent/runner/native/orchestration.py` (insert after `_prepare_opencode_native_fork`)
- Modify: `omnigent/runner/native/__init__.py` (imports near line 99 and `__all__` near line 236)
- Test: `tests/runner/test_opencode_resume.py`

**Interfaces:**
- Consumes: Stage 1 client methods `get_session`, `fork(session_id, *, before=None)`, `create_session(*, title, directory, permissions, metadata)`, `OpenCodeClientError`; Stage 3 `ASK_ALL_PERMISSIONS`; Task 68 rehydrate.
- Produces: `async def _resolve_opencode_session(*, client: OpenCodeClient, launch_config: _OpenCodeNativeLaunchConfig, omnigent_session_id: str, workspace: str, server_client: httpx.AsyncClient | None, fork_source_session_id: str | None, fresh: bool = False) -> str`

- [ ] **Step 1: Write the failing test**

Append to `tests/runner/test_opencode_resume.py`:

```python
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.opencode_native.client import OpenCodeClientError, OpenCodeSession
from omnigent.harnesses.opencode_native.provider import ASK_ALL_PERMISSIONS
from omnigent.runner.native.orchestration import (
    _OpenCodeNativeLaunchConfig,
    _resolve_opencode_session,
)


class _FakeSessionClient:
    """OpenCode client stub for the session-resolution paths."""

    def __init__(
        self,
        *,
        existing: str | None = None,
        fork_error: Exception | None = None,
    ) -> None:
        self._existing = existing
        self._fork_error = fork_error
        self.created: list[dict[str, Any]] = []
        self.forked: list[tuple[str, str | None]] = []
        self.seeded: list[tuple[str, str]] = []

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        return OpenCodeSession(id=session_id) if session_id == self._existing else None

    async def create_session(self, **kwargs: Any) -> OpenCodeSession:
        self.created.append(kwargs)
        return OpenCodeSession(id="ses_new")

    async def fork(self, session_id: str, *, before: str | None = None) -> OpenCodeSession:
        if self._fork_error is not None:
            raise self._fork_error
        self.forked.append((session_id, before))
        return OpenCodeSession(id="ses_forked")

    async def seed_context(self, session_id: str, text: str) -> None:
        self.seeded.append((session_id, text))


def _config(**overrides: Any) -> _OpenCodeNativeLaunchConfig:
    values: dict[str, Any] = {
        "workspace": Path("/repo"),
        "policy_server_url": "http://127.0.0.1:8123",
        "terminal_launch_args": None,
        "model_override": None,
        "external_session_id": None,
    }
    values.update(overrides)
    return _OpenCodeNativeLaunchConfig(**values)


async def _resolve(
    client: _FakeSessionClient,
    config: _OpenCodeNativeLaunchConfig,
    *,
    fork_source_session_id: str | None = None,
    fresh: bool = False,
) -> str:
    return await _resolve_opencode_session(
        client=client,  # type: ignore[arg-type]
        launch_config=config,
        omnigent_session_id="conv_1",
        workspace="/repo",
        server_client=_FakeServerClient(_ITEMS),  # type: ignore[arg-type]
        fork_source_session_id=fork_source_session_id,
        fresh=fresh,
    )


async def test_resolve_resumes_existing_session() -> None:
    client = _FakeSessionClient(existing="ses_old")
    assert await _resolve(client, _config(external_session_id="ses_old")) == "ses_old"
    assert client.created == [] and client.seeded == []


async def test_resolve_lost_session_creates_and_seeds() -> None:
    client = _FakeSessionClient()
    assert await _resolve(client, _config(external_session_id="ses_gone")) == "ses_new"
    assert client.created == [
        {
            "title": "omnigent:conv_1",
            "directory": "/repo",
            "permissions": ASK_ALL_PERMISSIONS,
            "metadata": {"omnigent_conversation": "conv_1"},
        }
    ]
    assert [sid for sid, _ in client.seeded] == ["ses_new"]


async def test_resolve_new_session_is_not_seeded() -> None:
    client = _FakeSessionClient()
    assert await _resolve(client, _config()) == "ses_new"
    assert client.seeded == []


async def test_resolve_native_fork() -> None:
    client = _FakeSessionClient()
    session_id = await _resolve(
        client, _config(fork_carry_history=True), fork_source_session_id="ses_src"
    )
    assert session_id == "ses_forked"
    assert client.forked == [("ses_src", None)]
    assert client.created == [] and client.seeded == []


@pytest.mark.parametrize(
    "error",
    [OpenCodeClientError("fork failed: 404"), httpx.ConnectError("refused")],
)
async def test_resolve_fork_failure_falls_back_to_preamble(error: Exception) -> None:
    client = _FakeSessionClient(fork_error=error)
    session_id = await _resolve(
        client, _config(fork_carry_history=True), fork_source_session_id="ses_src"
    )
    assert session_id == "ses_new"
    assert [sid for sid, _ in client.seeded] == ["ses_new"]


async def test_resolve_fresh_ignores_resume_and_fork() -> None:
    client = _FakeSessionClient(existing="ses_old")
    session_id = await _resolve(
        client,
        _config(external_session_id="ses_old", fork_carry_history=True),
        fork_source_session_id="ses_src",
        fresh=True,
    )
    assert session_id == "ses_new"
    assert client.forked == [] and client.seeded == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_resume.py -k resolve -v`
Expected: FAIL with "ImportError: cannot import name '_resolve_opencode_session'"

- [ ] **Step 3: Write minimal implementation**

Insert after `_prepare_opencode_native_fork` in `orchestration.py`:

```python
async def _resolve_opencode_session(
    *,
    client: OpenCodeClient,
    launch_config: _OpenCodeNativeLaunchConfig,
    omnigent_session_id: str,
    workspace: str,
    server_client: httpx.AsyncClient | None,
    fork_source_session_id: str | None,
    fresh: bool = False,
) -> str:
    """
    Resume, fork, or create the conversation's OpenCode session.

    Order: resume the persisted session; else fork the staged source session;
    else create a session that asks for every permission and, for a lost
    session or a forked clone, seed the Omnigent transcript as context.

    :param client: Client bound to the conversation's ``opencode serve``.
    :param launch_config: The conversation's launch config.
    :param omnigent_session_id: Omnigent conversation id.
    :param workspace: Workspace directory for a new session.
    :param server_client: Runner Omnigent server client (transcript source).
    :param fork_source_session_id: Source session staged by
        :func:`_prepare_opencode_native_fork`, or ``None``.
    :param fresh: ``True`` for ``/clear``: always create an unseeded session.
    :returns: The OpenCode session id to attach.
    """
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError
    from omnigent.harnesses.opencode_native.provider import ASK_ALL_PERMISSIONS

    resume_lost_history = False
    if not fresh and launch_config.external_session_id is not None:
        existing = await client.get_session(launch_config.external_session_id)
        if existing is not None:
            return existing.id
        resume_lost_history = True
    if not fresh and fork_source_session_id is not None:
        try:
            forked = await client.fork(fork_source_session_id, before=None)
        except (OpenCodeClientError, httpx.HTTPError):
            _logger.warning(
                "opencode fork: native fork of %s failed for %s; using transcript preamble",
                fork_source_session_id,
                omnigent_session_id,
                exc_info=True,
                extra={"session_id": omnigent_session_id},
            )
        else:
            return forked.id
    created = await client.create_session(
        title=f"omnigent:{omnigent_session_id}",
        directory=workspace,
        permissions=ASK_ALL_PERMISSIONS,
        metadata={"omnigent_conversation": omnigent_session_id},
    )
    if not fresh and (resume_lost_history or launch_config.fork_carry_history):
        await _rehydrate_opencode_session_from_transcript(
            opencode_client=client,
            opencode_session_id=created.id,
            omnigent_session_id=omnigent_session_id,
            server_client=server_client,
        )
    return created.id
```

In `omnigent/runner/native/__init__.py` add `_prepare_opencode_native_fork,` and `_resolve_opencode_session,` to the `from omnigent.runner.native.orchestration import (...)` list (alphabetical, next to `_publish_native_terminal_start_error` / `_resolve_native_spawn_env`) and `"_prepare_opencode_native_fork",` / `"_resolve_opencode_session",` to `__all__` in the same positions.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_resume.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py omnigent/runner/native/__init__.py tests/runner/test_opencode_resume.py
git commit -m "feat(opencode-native): resolve resume, native fork, and create for v2 sessions"
```

---

### Task 70: Strip TUI flags that fight the runner-owned server

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:24` (import `Sequence`), insert helper after `_resolve_opencode_session`
- Test: `tests/runner/test_opencode_native_orchestration.py`

**Interfaces:**
- Consumes: none.
- Produces: `_sanitize_opencode_tui_args(args: Sequence[str]) -> list[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/runner/test_opencode_native_orchestration.py`:

```python
from omnigent.runner.native.orchestration import _sanitize_opencode_tui_args


@pytest.mark.parametrize(
    "args,expected",
    [
        (["--auto"], []),
        (["--standalone", "--continue", "-c"], []),
        (["--server", "http://x", "--session", "ses_1", "-s", "ses_2"], []),
        (["--server=http://x", "--session=ses_1"], []),
        (["--prompt", "hi", "--log-level", "debug"], ["--prompt", "hi", "--log-level", "debug"]),
        (["--auto", "--prompt", "hi"], ["--prompt", "hi"]),
    ],
)
def test_sanitize_opencode_tui_args(args: list[str], expected: list[str]) -> None:
    assert _sanitize_opencode_tui_args(args) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -k sanitize -v`
Expected: FAIL with "ImportError: cannot import name '_sanitize_opencode_tui_args'"

- [ ] **Step 3: Write minimal implementation**

Change `orchestration.py:24` to:

```python
from collections.abc import Awaitable, Callable, Mapping, MutableMapping, Sequence
```

Insert after `_resolve_opencode_session`:

```python
# Root ``opencode`` flags the runner owns: auto-approval would bypass policy,
# and server/session selection would detach the TUI from the per-session server.
_OPENCODE_TUI_DROPPED_FLAGS = frozenset({"--auto", "--standalone", "--continue", "-c"})
_OPENCODE_TUI_DROPPED_VALUE_FLAGS = frozenset({"--server", "--session", "-s"})


def _sanitize_opencode_tui_args(args: Sequence[str]) -> list[str]:
    """
    Drop user pass-through TUI flags that conflict with the runner-owned server.

    :param args: User ``terminal_launch_args``, e.g. ``["--auto", "--prompt", "hi"]``.
    :returns: The args with conflicting flags (and their values) removed.
    """
    kept: list[str] = []
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        name, has_value, _ = arg.partition("=")
        if name in _OPENCODE_TUI_DROPPED_FLAGS:
            continue
        if name in _OPENCODE_TUI_DROPPED_VALUE_FLAGS:
            skip_value = not has_value
            continue
        kept.append(arg)
    return kept
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_native_orchestration.py
git commit -m "feat(opencode-native): drop TUI flags that bypass policy or the session server"
```

---

### Task 71: Wire the v2 launch flow into `_auto_create_opencode_terminal`

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:1415-1817` (signature, docstring, fork staging, session block, TUI args)
- Test: `tests/runner/test_opencode_native_orchestration.py`

**Interfaces:**
- Consumes: Tasks 63, 65, 66; Stage 1 `build_tui_command(opencode_path, *, base_url, session_id, workspace, extra_args=())`.
- Produces: `_auto_create_opencode_terminal(session_id, resource_registry, publish_event, *, agent_spec=None, server_client=None, ensure_comment_relay=None, fresh: bool = False) -> SessionResourceView`

- [ ] **Step 1: Write the failing test**

Append to `tests/runner/test_opencode_native_orchestration.py`:

```python
import inspect


def test_auto_create_opencode_terminal_accepts_fresh() -> None:
    parameter = inspect.signature(orchestration._auto_create_opencode_terminal).parameters["fresh"]
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is False


def test_auto_create_opencode_terminal_has_no_v1_session_calls() -> None:
    source = inspect.getsource(orchestration._auto_create_opencode_terminal)
    assert "create_session({" not in source, "v1 dict-payload create_session must be gone"
    assert "_resolve_opencode_session(" in source
    assert "_prepare_opencode_native_fork(" in source
    assert "_sanitize_opencode_tui_args(" in source
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -k auto_create -v`
Expected: FAIL with "KeyError: 'fresh'"

- [ ] **Step 3: Write minimal implementation**

Change the signature and docstring at `orchestration.py:1415-1443` to:

```python
async def _auto_create_opencode_terminal(
    session_id: str,
    resource_registry: SessionResourceRegistry,
    publish_event: Callable[[str, _JsonObject], None],
    *,
    agent_spec: AgentSpec | ResolvedSpec | None = None,
    server_client: httpx.AsyncClient | None = None,
    ensure_comment_relay: _EnsureCommentRelay | None = None,
    fresh: bool = False,
) -> SessionResourceView:
    """
    Auto-create an OpenCode terminal for an opencode-native session.

    Boots a per-conversation ``opencode serve --stdio``, resumes, forks, or
    creates the OpenCode session, persists bridge state and
    ``external_session_id``, starts the SSE forwarder, then registers the
    ``opencode --server <url> --session <id>`` TUI as a streamable terminal.

    :param session_id: Session/conversation id, e.g. ``"conv_abc123"``.
    :param resource_registry: Registry used to launch the terminal.
    :param publish_event: Per-session SSE emitter for the new terminal.
    :param agent_spec: Optional resolved agent spec (os_env + model).
    :param server_client: Runner Omnigent server HTTP client.
    :param ensure_comment_relay: Callback that starts the Omnigent builtin-tool
        relay for this session's bridge dir. ``None`` skips the relay.
    :param fresh: ``True`` for ``/clear``: start an empty session and skip
        resume, fork, and transcript seeding.
    :returns: The created terminal resource view.
    """
```

Immediately before `server = OpenCodeNativeServer(` (current `orchestration.py:1670`) insert:

```python
    # A forked clone copies the source conversation's DB before its own server
    # opens it, so the source session can be forked natively.
    fork_source_session_id = (
        None
        if fresh
        else _prepare_opencode_native_fork(
            launch_config, bridge_dir=bridge_dir, workspace=workspace
        )
    )
```

Replace the session block at current `orchestration.py:1678-1719` (from `try:` / `client = server.client()` through the `finally: await client.aclose()` that follows the `external_session_id` PATCH) with:

```python
    try:
        client = server.client()
        try:
            opencode_session_id = await _resolve_opencode_session(
                client=client,
                launch_config=launch_config,
                omnigent_session_id=session_id,
                workspace=workspace,
                server_client=server_client,
                fork_source_session_id=fork_source_session_id,
                fresh=fresh,
            )
            # Persist a newly created or forked session id so a relaunch resumes it.
            if (
                server_client is not None
                and opencode_session_id != launch_config.external_session_id
            ):
                with contextlib.suppress(httpx.HTTPError):
                    await server_client.patch(
                        f"/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
                        json={"external_session_id": opencode_session_id},
                        params={"include_usage": "false"},
                        timeout=10.0,
                    )
        finally:
            await client.aclose()
```

(The `write_bridge_state(...)` call and the `except BaseException:` cleanup that follow stay unchanged.)

In the `TerminalEnvSpec(...)` built for the terminal (Stage 1 already replaced `build_opencode_attach_args` with `build_tui_command`), pass the sanitized args:

```python
                extra_args=_sanitize_opencode_tui_args(launch_config.terminal_launch_args or ()),
```

in place of `extra_args=tuple(launch_config.terminal_launch_args or ())` inside the `build_tui_command(...)` call.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py tests/runner/test_opencode_resume.py tests/runner/test_app_sessions_native_wake_forwarders.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_native_orchestration.py
git commit -m "feat(opencode-native): launch flow resumes, forks, or creates v2 sessions"
```

---

### Task 72: `/clear` launches an empty session

**Files:**
- Modify: `omnigent/runner/app.py:7004-7038` (`_handle_opencode_native_clear`)
- Test: `tests/runner/test_app_sessions_native_events_options.py`

**Interfaces:**
- Consumes: Task 71 `fresh` keyword.
- Produces: the clear handler calls `_auto_create_opencode_terminal(..., fresh=True)`.

- [ ] **Step 1: Write the failing test**

Add to `tests/runner/test_app_sessions_native_events_options.py`, immediately before `test_events_compact_on_non_native_session_is_204_noop` (current line 1853):

```python
@pytest.mark.asyncio
async def test_events_clear_on_opencode_native_relaunches_fresh(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """``/clear`` relaunches OpenCode without resuming, forking, or seeding."""
    import omnigent.runner.app as runner_app
    from tests.runner.helpers import make_test_terminal_instance

    conv_id = "4f1c2b7e9d0a4c55b1e7a3c9d2f60a18"
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    calls: list[dict[str, Any]] = []

    async def _fake_auto_create(*args: Any, **kwargs: Any) -> None:
        calls.append(kwargs)

    monkeypatch.setattr(runner_app, "_auto_create_opencode_terminal", _fake_auto_create)
    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("opencode", "main")] = instance
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    async with _runner_client(app) as http_client:
        create_resp = await http_client.post(
            "/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"}
        )
        assert create_resp.status_code == 201, create_resp.text
        resp = await http_client.post(f"/v1/sessions/{conv_id}/events", json={"type": "clear"})

    assert resp.status_code == 200, resp.text
    assert len(calls) == 1
    assert calls[0]["fresh"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_app_sessions_native_events_options.py::test_events_clear_on_opencode_native_relaunches_fresh -v`
Expected: FAIL with "KeyError: 'fresh'"

- [ ] **Step 3: Write minimal implementation**

In `_handle_opencode_native_clear` (`app.py:7019-7027`) replace the relaunch call with:

```python
        try:
            await _auto_create_opencode_terminal(
                conv_id,
                resource_registry,
                _publish_event,
                agent_spec=spec,
                server_client=server_client,
                ensure_comment_relay=_ensure_comment_relay_started,
                fresh=True,
            )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_app_sessions_native_events_options.py::test_events_clear_on_opencode_native_relaunches_fresh -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/app.py tests/runner/test_app_sessions_native_events_options.py
git commit -m "fix(opencode-native): clear starts an empty session instead of re-seeding a fork"
```

---

### Task 73: `/compact` calls `POST /compact` with no model

**Files:**
- Modify: `omnigent/runner/app.py:6898-6943` (`_handle_opencode_native_compact`), `app.py:148` (import)
- Modify: `omnigent/runner/native/orchestration.py:1970-2024` (delete `_resolve_opencode_compact_model`)
- Modify: `omnigent/runner/native/__init__.py:119,254` (delete re-export)
- Test: `tests/runner/test_app_sessions_native_events_options.py:1436-1851`

**Interfaces:**
- Consumes: Stage 1 `OpenCodeClient.compact(session_id) -> dict`, `OpenCodeClientError`.
- Produces: `/v1/sessions/{id}/events {"type":"compact"}` on opencode-native returns 200 after `compact`, 503 `opencode_native_compact_failed` on failure or when not active.

- [ ] **Step 1: Write the failing test**

Replace the block of `tests/runner/test_app_sessions_native_events_options.py` from `class _FakeOpenCodeCompactClient:` (current line 1436) through the end of `test_events_compact_on_opencode_native_503_when_summarize_raises` (current line 1851; Task 72's clear test sits after it and stays) with:

```python
class _FakeOpenCodeCompactClient:
    """OpenCode client stub recording ``compact`` calls."""

    def __init__(self, *, compact_error: BaseException | None = None) -> None:
        self._compact_error = compact_error
        self.compact_calls: list[str] = []
        self.closed = False

    async def compact(self, session_id: str) -> dict[str, Any]:
        if self._compact_error is not None:
            raise self._compact_error
        self.compact_calls.append(session_id)
        return {"id": "msg_compaction", "type": "compaction"}

    async def aclose(self) -> None:
        self.closed = True


class _FakeOpenCodeCompactServer:
    """``OpenCodeNativeServer`` stub whose ``client()`` returns a fixed stub."""

    def __init__(self, client: _FakeOpenCodeCompactClient) -> None:
        self._client = client

    def client(self, *, directory: str | None = None) -> _FakeOpenCodeCompactClient:
        del directory
        return self._client


async def _drive_opencode_native_compact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    conv_id: str,
    compact_error: BaseException | None = None,
    with_server: bool = True,
) -> tuple[httpx.Response, _FakeOpenCodeCompactClient]:
    """Create an opencode-native session and POST a ``compact`` control event."""
    from omnigent.harnesses.opencode_native import bridge as opencode_native_bridge
    from omnigent.harnesses.opencode_native.bridge import OpenCodeNativeBridgeState
    from omnigent.runner.app import _AUTO_OPENCODE_SERVERS, _session_event_queues_ref
    from tests.runner.helpers import make_test_terminal_instance

    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    terminal_registry = TerminalRegistry()
    instance = make_test_terminal_instance("opencode", "main", tmp_path)
    terminal_registry._by_conversation.setdefault(conv_id, {})[("opencode", "main")] = instance
    client = _FakeOpenCodeCompactClient(compact_error=compact_error)
    state = OpenCodeNativeBridgeState(
        session_id=conv_id,
        server_base_url="http://127.0.0.1:1",
        opencode_session_id="ses_x",
    )
    monkeypatch.setattr(opencode_native_bridge, "read_bridge_state", lambda _dir: state)
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=terminal_registry,
    )
    try:
        async with _runner_client(app) as http_client:
            create_resp = await http_client.post(
                "/v1/sessions",
                json={"session_id": conv_id, "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb"},
            )
            assert create_resp.status_code == 201, create_resp.text
            _drain_session_event_queue(_session_event_queues_ref.get(conv_id))
            if with_server:
                _AUTO_OPENCODE_SERVERS[conv_id] = _FakeOpenCodeCompactServer(client)
            resp = await http_client.post(
                f"/v1/sessions/{conv_id}/events", json={"type": "compact"}
            )
        return resp, client
    finally:
        _AUTO_OPENCODE_SERVERS.pop(conv_id, None)
        _session_event_queues_ref.pop(conv_id, None)


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_calls_compact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """opencode-native compact calls ``POST /compact`` with no model and returns 200.

    The 200 is load-bearing: the Omnigent server reads it to skip its own
    compaction for this session.
    """
    resp, client = await _drive_opencode_native_compact(
        monkeypatch, tmp_path, conv_id="f67241520c2101c4de5f81b976467bad"
    )
    assert resp.status_code == 200, resp.text
    assert client.compact_calls == ["ses_x"]
    assert client.closed


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_503_when_compact_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failing ``/compact`` surfaces 503 so the server does not double-compact."""
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError

    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="309c268a432cb4dbda4e8c15585578ee",
        compact_error=OpenCodeClientError("compact failed: 409"),
    )
    assert resp.status_code == 503, resp.text
    assert resp.json().get("error") == "opencode_native_compact_failed"
    assert client.closed


@pytest.mark.asyncio
async def test_events_compact_on_opencode_native_503_when_not_active(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """With no live server the compact reports 503 and never calls OpenCode."""
    resp, client = await _drive_opencode_native_compact(
        monkeypatch,
        tmp_path,
        conv_id="90c89e7e5e9131aa4bb062fd427927ae",
        with_server=False,
    )
    assert resp.status_code == 503, resp.text
    assert client.compact_calls == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_app_sessions_native_events_options.py -k "opencode_native" -v`
Expected: FAIL with "AttributeError: '_FakeOpenCodeCompactClient' object has no attribute 'get_session'"

- [ ] **Step 3: Write minimal implementation**

Replace `app.py:6898-6943` with:

```python
    async def _handle_opencode_native_compact(conv_id: str) -> Response:
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )
        from omnigent.harnesses.opencode_native.client import OpenCodeClientError

        server = _AUTO_OPENCODE_SERVERS.get(conv_id)
        state = read_bridge_state(bridge_dir_for_bridge_id(conv_id))
        if server is None or state is None or not state.opencode_session_id:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_compact_failed",
                    "detail": "OpenCode session is not active; reconnect first.",
                },
            )
        client = server.client()
        try:
            await client.compact(state.opencode_session_id)
        except (httpx.HTTPError, OpenCodeClientError, RuntimeError, ValueError) as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_compact_failed",
                    "detail": _client_safe_error_detail(exc, context="opencode-native compact"),
                },
            )
        finally:
            await client.aclose()
        return Response(status_code=200)
```

Delete `app.py:148` (`    _resolve_opencode_compact_model,`), `orchestration.py:1970-2024` (the whole `_resolve_opencode_compact_model` function), and `native/__init__.py:119` (`    _resolve_opencode_compact_model,`) and `:254` (`    "_resolve_opencode_compact_model",`). Remove `OpenCodeSession` from the `TYPE_CHECKING` import at `orchestration.py:39` if nothing else references it (`rg -n "OpenCodeSession" omnigent/runner/native/orchestration.py`).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_app_sessions_native_events_options.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/app.py omnigent/runner/native/orchestration.py omnigent/runner/native/__init__.py tests/runner/test_app_sessions_native_events_options.py
git commit -m "refactor(opencode-native): compact via POST /compact without model resolution"
```

---

### Task 74: Model switch applies to the live session

**Files:**
- Modify: `omnigent/runner/native/orchestration.py` (insert `_opencode_model_ref` after `_opencode_native_model_from_spec`, current line 1950-1967)
- Modify: `omnigent/runner/native/__init__.py`, `omnigent/runner/app.py:113-148` (import) and `app.py:6984-7002` (`_handle_opencode_native_model_change`)
- Test: `tests/runner/test_opencode_native_orchestration.py`, `tests/runner/test_app_sessions_native_events_lifecycle.py`

**Interfaces:**
- Consumes: Stage 1 `OpenCodeClient.set_model(session_id, *, provider_id, model_id, variant=None) -> None`; `update_model_override`, `read_bridge_state`.
- Produces: `_opencode_model_ref(model: str | None) -> dict[str, str] | None` returning `{"providerID", "id"}` plus `"variant"` when present.

- [ ] **Step 1: Write the failing tests**

Append to `tests/runner/test_opencode_native_orchestration.py`:

```python
from omnigent.runner.native.orchestration import _opencode_model_ref


@pytest.mark.parametrize(
    "model,expected",
    [
        ("anthropic/claude-sonnet-4-5", {"providerID": "anthropic", "id": "claude-sonnet-4-5"}),
        ("omnigent/omnigent/literal", {"providerID": "omnigent", "id": "omnigent/literal"}),
        ("openai/gpt-5#high", {"providerID": "openai", "id": "gpt-5", "variant": "high"}),
        (None, None),
        ("  ", None),
        ("no-provider", None),
        ("/missing-provider", None),
        ("openai/", None),
    ],
)
def test_opencode_model_ref(model: str | None, expected: dict[str, str] | None) -> None:
    assert _opencode_model_ref(model) == expected
```

Append to `tests/runner/test_app_sessions_native_events_lifecycle.py` after `test_bound_opencode_switch_qualifies_the_literal_gateway_id`:

```python
@pytest.mark.asyncio
async def test_opencode_model_switch_sets_model_on_live_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.harnesses.opencode_native.bridge import OpenCodeNativeBridgeState
    from omnigent.runner.app import _AUTO_OPENCODE_SERVERS

    conv_id = "c6d1e2f3a4b54c6d8e9f0a1b2c3d4e5f"
    update = Mock(return_value=True)
    monkeypatch.setattr("omnigent.harnesses.opencode_native.bridge.update_model_override", update)
    monkeypatch.setattr(
        "omnigent.harnesses.opencode_native.bridge.read_bridge_state",
        lambda _dir: OpenCodeNativeBridgeState(
            session_id=conv_id,
            server_base_url="http://127.0.0.1:1",
            opencode_session_id="ses_live",
        ),
    )

    class _Client:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str, str | None]] = []
            self.closed = False

        async def set_model(
            self, session_id: str, *, provider_id: str, model_id: str, variant: str | None = None
        ) -> None:
            self.calls.append((session_id, provider_id, model_id, variant))

        async def aclose(self) -> None:
            self.closed = True

    fake_client = _Client()

    class _Server:
        def client(self, *, directory: str | None = None) -> _Client:
            return fake_client

    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    try:
        async with _runner_client(app) as client:
            response = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "ag_1"}
            )
            assert response.status_code == 201, response.text
            _AUTO_OPENCODE_SERVERS[conv_id] = _Server()  # type: ignore[assignment]
            response = await client.post(
                f"/v1/sessions/{conv_id}/events",
                json={"type": "model_change", "model": "openai/gpt-5#high"},
            )
    finally:
        _AUTO_OPENCODE_SERVERS.pop(conv_id, None)

    assert response.status_code == 200, response.text
    assert update.call_args.args[1] == "openai/gpt-5#high"
    assert fake_client.calls == [("ses_live", "openai", "gpt-5", "high")]
    assert fake_client.closed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -k model_ref tests/runner/test_app_sessions_native_events_lifecycle.py::test_opencode_model_switch_sets_model_on_live_session -v`
Expected: FAIL with "ImportError: cannot import name '_opencode_model_ref'"

- [ ] **Step 3: Write minimal implementation**

Insert after `_opencode_native_model_from_spec` in `orchestration.py`:

```python
def _opencode_model_ref(model: str | None) -> dict[str, str] | None:
    """
    Parse ``provider/model[#variant]`` into an OpenCode ``Model.Ref``.

    Splits on the first ``/`` (gateway ids such as ``omnigent/omnigent/x``
    keep the rest as the model id) and on the first ``#`` after it.

    :param model: Qualified model string, e.g. ``"openai/gpt-5#high"``.
    :returns: ``{"providerID", "id"[, "variant"]}``, or ``None`` when unparsable.
    """
    if not isinstance(model, str) or not model.strip():
        return None
    provider_id, slash, rest = model.strip().partition("/")
    model_id, hash_sign, variant = rest.partition("#")
    if not slash or not provider_id or not model_id or "#" in provider_id:
        return None
    ref = {"providerID": provider_id, "id": model_id}
    if hash_sign and variant:
        ref["variant"] = variant
    return ref
```

Add `_opencode_model_ref,` / `"_opencode_model_ref",` to `omnigent/runner/native/__init__.py` (next to `_opencode_native_model_from_spec`) and `_opencode_model_ref,` to the `from omnigent.runner.native import (...)` block in `app.py:113-148`.

Replace `app.py:6984-7002` with:

```python
    async def _handle_opencode_native_model_change(conv_id: str, model: str | None) -> Response:
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            read_bridge_state,
            update_model_override,
        )
        from omnigent.harnesses.opencode_native.client import OpenCodeClientError
        from omnigent.inference_config import (
            binding_for_harness,
            load_runtime_inference_config,
            resolve_bound_model,
        )

        inference_config = load_runtime_inference_config()
        if binding_for_harness(inference_config, "opencode-native") is not None:
            selected = resolve_bound_model(inference_config, "opencode-native", model)
            model = f"omnigent/{selected}" if selected is not None else None
        bridge_dir = bridge_dir_for_bridge_id(conv_id)
        updated = await asyncio.to_thread(update_model_override, bridge_dir, model)
        model_ref = _opencode_model_ref(model)
        server = _AUTO_OPENCODE_SERVERS.get(conv_id)
        state = read_bridge_state(bridge_dir) if server is not None else None
        if model_ref is not None and server is not None and state is not None:
            # The TUI and the next turn pick the model up from the session.
            client = server.client()
            try:
                await client.set_model(
                    state.opencode_session_id,
                    provider_id=model_ref["providerID"],
                    model_id=model_ref["id"],
                    variant=model_ref.get("variant"),
                )
            except (httpx.HTTPError, OpenCodeClientError):
                _logger.warning(
                    "OpenCode set_model failed for %s; the next prompt retries it",
                    conv_id,
                    exc_info=True,
                    extra={"session_id": conv_id},
                )
            finally:
                await client.aclose()
        return Response(status_code=200 if updated else 204)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py tests/runner/test_app_sessions_native_events_lifecycle.py -k "opencode" -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py omnigent/runner/native/__init__.py omnigent/runner/app.py tests/runner/test_opencode_native_orchestration.py tests/runner/test_app_sessions_native_events_lifecycle.py
git commit -m "feat(opencode-native): apply model switches to the live session via set_model"
```

---

### Task 75: Model options from `GET /api/model`

**Files:**
- Modify: `omnigent/runner/native/orchestration.py` (insert `_opencode_model_options_from_catalog` after `_opencode_model_ref`), `omnigent/runner/native/__init__.py`, `omnigent/runner/app.py:113-148` (import), `app.py:6945-6982` (`_opencode_native_model_options`)
- Modify: `omnigent/harnesses/opencode_native/app_server.py` (delete `_ANSI_RE` and `list_opencode_cli_model_options`)
- Test: `tests/runner/test_opencode_native_orchestration.py`, `tests/runner/test_app_sessions_native_events_lifecycle.py:683-747`, `tests/test_opencode_native_app_server.py:386-411`

**Interfaces:**
- Consumes: Stage 1 `client_for_state(*, base_url, auth_secret, directory=None)`, `OpenCodeClient.list_models() -> list[dict]` (unwrapped `Model.Info[]`).
- Produces: `_opencode_model_options_from_catalog(models: list[_JsonObject]) -> list[_JsonObject]`, each `{"id": "provider/model", "model", "providerID", "displayName", "name", "isDefault": False}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/runner/test_opencode_native_orchestration.py`:

```python
from omnigent.runner.native.orchestration import _opencode_model_options_from_catalog


def test_model_options_from_v2_catalog() -> None:
    catalog = [
        {"id": "glm-5.2", "modelID": "glm-5.2", "providerID": "opencode-go", "name": "GLM 5.2",
         "status": "active", "enabled": True},
        {"id": "old", "modelID": "old", "providerID": "openai", "name": "Old",
         "status": "deprecated", "enabled": True},
        {"id": "off", "modelID": "off", "providerID": "openai", "name": "Off",
         "status": "active", "enabled": False},
        {"id": "glm-5.2", "modelID": "glm-5.2", "providerID": "opencode-go", "name": "dup",
         "status": "active", "enabled": True},
        {"providerID": "broken"},
    ]
    assert _opencode_model_options_from_catalog(catalog) == [
        {
            "id": "opencode-go/glm-5.2",
            "model": "glm-5.2",
            "providerID": "opencode-go",
            "displayName": "opencode-go/glm-5.2",
            "name": "GLM 5.2",
            "isDefault": False,
        }
    ]
```

Replace `tests/runner/test_app_sessions_native_events_lifecycle.py:683-747` (`test_opencode_native_model_options_uses_cli_catalog`) with:

```python
@pytest.mark.asyncio
async def test_opencode_native_model_options_reads_server_catalog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.harnesses.opencode_native import app_server as opencode_native_app_server
    from omnigent.harnesses.opencode_native import bridge as opencode_native_bridge
    from omnigent.harnesses.opencode_native.bridge import OpenCodeNativeBridgeState
    from omnigent.spec.types import ExecutorSpec

    conv_id = "conv_opencode_native_model_options"
    monkeypatch.setattr(opencode_native_bridge, "_BRIDGE_ROOT", tmp_path)
    monkeypatch.setattr(
        opencode_native_bridge,
        "read_bridge_state",
        lambda _dir: OpenCodeNativeBridgeState(
            session_id=conv_id,
            server_base_url="http://127.0.0.1:49231",
            opencode_session_id="ses_1",
            auth_secret="pw",
            workspace="/repo",
        ),
    )

    class _Client:
        closed = False

        async def list_models(self) -> list[dict[str, object]]:
            return [
                {"id": "glm-5.2", "modelID": "glm-5.2", "providerID": "opencode-go",
                 "name": "GLM 5.2", "status": "active", "enabled": True}
            ]

        async def aclose(self) -> None:
            _Client.closed = True

    built: list[dict[str, object]] = []

    def _fake_client_for_state(**kwargs: object) -> _Client:
        built.append(kwargs)
        return _Client()

    monkeypatch.setattr(opencode_native_app_server, "client_for_state", _fake_client_for_state)
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "opencode-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        create_resp = await client.post(
            "/v1/sessions",
            json={"session_id": conv_id, "agent_id": "ag_1"},
        )
        assert create_resp.status_code == 201, create_resp.text
        response = await client.get(f"/v1/sessions/{conv_id}/codex-model-options")

    assert response.status_code == 200
    assert response.json() == {
        "models": [
            {
                "id": "opencode-go/glm-5.2",
                "model": "glm-5.2",
                "providerID": "opencode-go",
                "displayName": "opencode-go/glm-5.2",
                "name": "GLM 5.2",
                "isDefault": False,
            }
        ]
    }
    assert built == [
        {"base_url": "http://127.0.0.1:49231", "auth_secret": "pw", "directory": "/repo"}
    ]
    assert _Client.closed
```

Delete `tests/test_opencode_native_app_server.py:386-411` (`test_list_opencode_cli_model_options`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py -k catalog tests/runner/test_app_sessions_native_events_lifecycle.py::test_opencode_native_model_options_reads_server_catalog -v`
Expected: FAIL with "ImportError: cannot import name '_opencode_model_options_from_catalog'"

- [ ] **Step 3: Write minimal implementation**

Insert after `_opencode_model_ref` in `orchestration.py`:

```python
def _opencode_model_options_from_catalog(models: list[_JsonObject]) -> list[_JsonObject]:
    """
    Map OpenCode v2 ``Model.Info`` entries onto web model-picker options.

    Disabled and deprecated models are hidden; duplicates keep the first
    (the catalog is ordered by release date).

    :param models: ``GET /api/model`` data, e.g.
        ``[{"id": "gpt-5", "providerID": "openai", "name": "GPT-5", ...}]``.
    :returns: Options keyed by the qualified ``provider/model`` id.
    """
    options: list[_JsonObject] = []
    seen: set[str] = set()
    for model in models:
        provider_id = model.get("providerID")
        model_id = model.get("id")
        if not isinstance(provider_id, str) or not provider_id:
            continue
        if not isinstance(model_id, str) or not model_id:
            continue
        if model.get("enabled") is False or model.get("status") == "deprecated":
            continue
        qualified = f"{provider_id}/{model_id}"
        if qualified in seen:
            continue
        seen.add(qualified)
        name = model.get("name")
        options.append(
            {
                "id": qualified,
                "model": model_id,
                "providerID": provider_id,
                "displayName": qualified,
                "name": name if isinstance(name, str) and name else model_id,
                "isDefault": False,
            }
        )
    return options
```

Export it from `omnigent/runner/native/__init__.py` (import list and `__all__`) and add `_opencode_model_options_from_catalog,` to the `from omnigent.runner.native import (...)` block in `app.py`.

Replace `app.py:6945-6982` with:

```python
    async def _opencode_native_model_options(conv_id: str) -> list[_JsonObject]:
        from omnigent.harnesses.opencode_native import app_server as opencode_app_server
        from omnigent.harnesses.opencode_native.bridge import (
            bridge_dir_for_bridge_id,
            read_bridge_state,
        )

        state = read_bridge_state(bridge_dir_for_bridge_id(conv_id))
        if state is None or not state.server_base_url:
            raise _CodexNativeModelOptionsNotReady("OpenCode-native app-server is not ready yet.")
        client = opencode_app_server.client_for_state(
            base_url=state.server_base_url,
            auth_secret=state.auth_secret,
            directory=state.workspace,
        )
        try:
            models = await client.list_models()
        finally:
            await client.aclose()
        return _opencode_model_options_from_catalog(models)
```

In `omnigent/harnesses/opencode_native/app_server.py` delete the `_ANSI_RE` constant and its comment (lines 97-98 before Stage 1) and the whole `list_opencode_cli_model_options` function (lines 186-253 before Stage 1). Confirm no references remain: `rg -n "list_opencode_cli_model_options|_ANSI_RE" omnigent tests` returns nothing.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/runner/test_opencode_native_orchestration.py tests/runner/test_app_sessions_native_events_lifecycle.py tests/test_opencode_native_app_server.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py omnigent/runner/native/__init__.py omnigent/runner/app.py omnigent/harnesses/opencode_native/app_server.py tests/runner/test_opencode_native_orchestration.py tests/runner/test_app_sessions_native_events_lifecycle.py tests/test_opencode_native_app_server.py
git commit -m "refactor(opencode-native): read model options from GET /api/model only"
```

---

### Task 76: Native interrupt and stop for opencode-native

**Files:**
- Modify: `omnigent/runner/native/interrupt.py:22-27` (module docstring), `:297` (`native_cancel_capability` docstring), `:335-373` (`interrupt`, `stop`), add `_opencode_interrupt` after `_codex_interrupt` (ends line 653)
- Test: `tests/runner/test_native_interrupt_runner.py:102-107` and new tests

**Interfaces:**
- Consumes: Stage 1 `client_for_state`, `OpenCodeClient.interrupt(session_id) -> bool`; `read_bridge_state`, `bridge_dir_for_bridge_id`.
- Produces: `NativeInterruptRunner.interrupt("opencode-native", conv)` / `.stop(...)` return 204 after `POST /api/session/{id}/interrupt`, 503 `opencode_native_interrupt_failed` on error, `None` when no bridge state.

- [ ] **Step 1: Write the failing test**

In `tests/runner/test_native_interrupt_runner.py` change the parametrize at line 102 to `["antigravity-native", "claude-sdk", None]` and append:

```python
def _patch_opencode(
    monkeypatch: pytest.MonkeyPatch,
    *,
    state: Any,
    error: Exception | None = None,
) -> list[str]:
    import omnigent.harnesses.opencode_native.app_server as oc_app_server
    import omnigent.harnesses.opencode_native.bridge as oc_bridge

    interrupted: list[str] = []

    class _Client:
        async def interrupt(self, session_id: str) -> bool:
            if error is not None:
                raise error
            interrupted.append(session_id)
            return True

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(oc_bridge, "read_bridge_state", lambda _dir: state)
    monkeypatch.setattr(oc_app_server, "client_for_state", lambda **_kw: _Client())
    return interrupted


def _opencode_state() -> Any:
    from omnigent.harnesses.opencode_native.bridge import OpenCodeNativeBridgeState

    return OpenCodeNativeBridgeState(
        session_id="conv_o",
        server_base_url="http://127.0.0.1:1",
        opencode_session_id="ses_o",
        auth_secret="pw",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["interrupt", "stop"])
async def test_opencode_interrupt_calls_server_and_wakes_parent(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    interrupted = _patch_opencode(monkeypatch, state=_opencode_state())
    runner, captured = _make_runner()

    resp = await getattr(runner, method)("opencode-native", "conv_o")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert interrupted == ["ses_o"]
    assert captured["wakes"] == [("conv_o", "cancelled", "[System: sub-agent interrupted]")]


@pytest.mark.asyncio
async def test_opencode_interrupt_without_state_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_opencode(monkeypatch, state=None)
    runner, _ = _make_runner()
    assert await runner.interrupt("opencode-native", "conv_o") is None


@pytest.mark.asyncio
async def test_opencode_interrupt_failure_is_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.harnesses.opencode_native.client import OpenCodeClientError

    _patch_opencode(
        monkeypatch, state=_opencode_state(), error=OpenCodeClientError("interrupt failed: 500")
    )
    runner, captured = _make_runner()

    resp = await runner.interrupt("opencode-native", "conv_o")

    assert resp is not None and resp.status_code == 503
    assert b"opencode_native_interrupt_failed" in resp.body
    assert captured["wakes"] == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_native_interrupt_runner.py -k opencode -v`
Expected: FAIL with "assert None is not None" / "isinstance(None, Response)" for the opencode cases

- [ ] **Step 3: Write minimal implementation**

Replace the coverage note in the module docstring (`interrupt.py:22-27`) with:

```python
Coverage note: antigravity-native has no handler here and :meth:`interrupt` /
:meth:`stop` return ``None`` for it, so the caller falls through to the
in-process turn cancel. opencode-native interrupts through its server's
``POST /api/session/{id}/interrupt`` and aliases stop to interrupt.
```

In `native_cancel_capability`'s docstring (`interrupt.py:296-297`) replace "(Codex/Pi alias stop to interrupt; Antigravity/OpenCode have no stop handler)" with "(Codex/Pi/OpenCode alias stop to interrupt; Antigravity has no stop handler)". In `interrupt()`'s docstring replace "(antigravity/opencode)" with "(antigravity)".

In `interrupt()` (`interrupt.py:345-349`) add the opencode branch after codex:

```python
        if key == "codex":
            return await self._codex_interrupt(conv_id)
        if key == "opencode":
            return await self._opencode_interrupt(conv_id)
```

In `stop()` (`interrupt.py:367-368`) change the alias line to:

```python
        if key in ("codex", "pi", "opencode"):
            return await self.interrupt(harness_name, conv_id)
```

Append after `_codex_interrupt` (after `interrupt.py:653`):

```python
    async def _opencode_interrupt(self, conv_id: str) -> Response | None:
        from omnigent.harnesses.opencode_native import app_server as opencode_app_server
        from omnigent.harnesses.opencode_native import bridge as opencode_bridge
        from omnigent.harnesses.opencode_native.client import OpenCodeClientError

        state = opencode_bridge.read_bridge_state(opencode_bridge.bridge_dir_for_bridge_id(conv_id))
        if state is None:
            return None
        client = opencode_app_server.client_for_state(
            base_url=state.server_base_url,
            auth_secret=state.auth_secret,
            directory=state.workspace,
        )
        try:
            await client.interrupt(state.opencode_session_id)
        except (OpenCodeClientError, httpx.HTTPError) as exc:
            self._logger.warning(
                "OpenCode-native interrupt failed for session=%s", conv_id, exc_info=True
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "opencode_native_interrupt_failed",
                    "detail": self._client_safe_error_detail(
                        exc, context="opencode-native interrupt"
                    ),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await client.aclose()
        # The forwarder publishes the idle edge from session.execution.interrupted.
        self._wake_parent_after_native_interrupt(conv_id)
        return Response(status_code=204)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_native_interrupt_runner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/interrupt.py tests/runner/test_native_interrupt_runner.py
git commit -m "feat(opencode-native): interrupt and stop running turns via POST /interrupt"
```

---

### Task 77: Policy evaluator wording and test for v2 actions

**Files:**
- Modify: `omnigent/runner/native/orchestration.py:1865-1888` (`_build_opencode_policy_evaluator` docstring)
- Test: `tests/runner/test_opencode_policy_evaluator.py:1-8, 43-62`

**Interfaces:**
- Consumes: Stage 3 normalized permission input `{action, command, path, url, metadata}`.
- Produces: unchanged evaluator behavior; the docstring and tests describe v2 `permission.asked` and `once`/`reject`.

- [ ] **Step 1: Write the failing test**

In `tests/runner/test_opencode_policy_evaluator.py` replace the module docstring's first paragraph with "every ``permission.asked`` request" (instead of ``permission.v2.asked``) and replace `test_evaluator_posts_tool_call_event_and_maps_allow` with:

```python
async def test_evaluator_posts_tool_call_event_and_maps_allow() -> None:
    """ALLOW maps to ``allow``; the POST carries the v2 action as the tool name."""
    client = _FakeServerClient(body={"result": "POLICY_ACTION_ALLOW"})
    evaluate = _build_opencode_policy_evaluator(
        server_client=client,  # type: ignore[arg-type]
        conversation_id="conv_1",
    )
    verdict = await evaluate(
        {"action": "shell", "command": "ls", "path": None, "url": None, "metadata": {}}
    )
    assert verdict == {"decision": "allow"}
    url, body, _timeout = client.calls[0]
    assert url == "/v1/sessions/conv_1/policies/evaluate"
    event = body["event"]
    assert event["type"] == "PHASE_TOOL_CALL"
    assert event["data"]["name"] == "shell"
    assert event["data"]["arguments"] == {"command": "ls"}
    assert event["context"] == {"harness": "opencode-native"}


def test_evaluator_docstring_describes_v2_permission_flow() -> None:
    doc = _build_opencode_policy_evaluator.__doc__ or ""
    assert "permission.v2.asked" not in doc
    assert "always" not in doc
    assert "permission.asked" in doc
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/runner/test_opencode_policy_evaluator.py -v`
Expected: FAIL with "assert 'permission.v2.asked' not in doc"

- [ ] **Step 3: Write minimal implementation**

Replace the first two paragraphs of the `_build_opencode_policy_evaluator` docstring (`orchestration.py:1870-1880`) with:

```python
    """
    Build the policy evaluator the OpenCode permission forwarder consults.

    Every OpenCode ``permission.asked`` request is POSTed to this session's
    ``/v1/sessions/{id}/policies/evaluate`` endpoint as a ``PHASE_TOOL_CALL``
    event named after the v2 action (``shell``, ``edit``, ``subagent``, ...).
    The server evaluates configured policies and, for an ``ASK`` verdict,
    parks a human approval card and blocks until it is resolved. The
    forwarder turns the verdict into an OpenCode ``once`` or ``reject`` reply.
```

(The "Fails CLOSED" paragraph and the `:param`/`:returns:` lines stay.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/runner/test_opencode_policy_evaluator.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/runner/native/orchestration.py tests/runner/test_opencode_policy_evaluator.py
git commit -m "docs(opencode-native): describe the v2 permission flow in the policy evaluator"
```

---

### Task 78: Client: list top-level sessions across projects

**Files:**
- Modify: `omnigent/harnesses/opencode_native/client.py` (add method to `OpenCodeClient`, after `get_session`)
- Test: `tests/test_opencode_native_client.py`

**Interfaces:**
- Consumes: Stage 1 `_unwrap`, `OpenCodeSession.from_payload`, `OpenCodeClientError`, `self._client` (`httpx.AsyncClient`).
- Produces: `async def list_root_sessions(self, *, limit: int = 100) -> list[OpenCodeSession]` — `GET /api/session?parentID=null&order=desc&limit=<n>`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_opencode_native_client.py`:

```python
async def test_list_root_sessions_queries_all_projects_newest_first() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "ses_new", "title": "new", "location": {"directory": "/a"},
                     "time": {"created": 1, "updated": 20}},
                    {"id": "ses_old", "location": {"directory": "/b"},
                     "time": {"created": 1, "updated": 10}},
                ],
                "cursor": {"previous": None, "next": None},
            },
        )

    client = _client(handler)
    sessions = await client.list_root_sessions(limit=5)
    await client.aclose()

    assert [s.id for s in sessions] == ["ses_new", "ses_old"]
    assert sessions[0].raw["time"]["updated"] == 20
    request = seen[0]
    assert request.method == "GET"
    assert request.url.path == "/api/session"
    assert dict(request.url.params) == {"parentID": "null", "order": "desc", "limit": "5"}


async def test_list_root_sessions_raises_on_error() -> None:
    client = _client(lambda request: httpx.Response(401, json={"error": "unauthorized"}))
    with pytest.raises(OpenCodeClientError):
        await client.list_root_sessions()
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_client.py -k list_root_sessions -v`
Expected: FAIL with "AttributeError: 'OpenCodeClient' object has no attribute 'list_root_sessions'"

- [ ] **Step 3: Write minimal implementation**

Add to `OpenCodeClient` after `get_session`:

```python
    async def list_root_sessions(self, *, limit: int = 100) -> list[OpenCodeSession]:
        """
        List top-level sessions in every project, newest first (``GET /api/session``).

        Omitting ``project``/``directory`` lists across projects;
        ``parentID=null`` excludes subagent child sessions.

        :param limit: Maximum sessions to return.
        :returns: Sessions ordered by ``time.updated`` descending.
        :raises OpenCodeClientError: On a non-2xx status or a non-array body.
        """
        response = await self._client.get(
            "/api/session",
            params={"parentID": "null", "order": "desc", "limit": str(limit)},
        )
        if response.status_code >= 400:
            raise OpenCodeClientError(
                f"OpenCode GET /api/session failed: {response.status_code} {response.text[:500]}"
            )
        data = _unwrap(response.json())
        if not isinstance(data, list):
            raise OpenCodeClientError("OpenCode session list returned a non-array body")
        return [
            OpenCodeSession.from_payload(item)
            for item in data
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
        ]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_client.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/client.py tests/test_opencode_native_client.py
git commit -m "feat(opencode-native): list top-level sessions across projects"
```

---

### Task 79: Import server mode that reads the user's own OpenCode store

**Files:**
- Modify: `omnigent/harnesses/opencode_native/bridge.py:446-457` (`user_opencode_auth_path`; add `user_xdg_data_home`)
- Modify: `omnigent/harnesses/opencode_native/app_server.py` (`filtered_server_env`, `OpenCodeNativeServer.__init__` and `env`)
- Test: `tests/test_opencode_native_app_server.py`

**Interfaces:**
- Consumes: Task 64 `OPENCODE_DB` assignment.
- Produces:
  - `user_xdg_data_home() -> Path` (bridge.py)
  - `filtered_server_env(..., user_data_store: bool = False)` — when true, `XDG_DATA_HOME` is the user's real data home and `OPENCODE_DB` is unset.
  - `OpenCodeNativeServer(..., user_data_store: bool = False)`; `server.xdg_data_home` reflects the choice.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_opencode_native_app_server.py`:

```python
def test_filtered_server_env_user_data_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_data = tmp_path / "real-data"
    monkeypatch.setenv("XDG_DATA_HOME", str(real_data))
    monkeypatch.setenv("OPENCODE_DB", "/elsewhere/other.db")

    env = filtered_server_env(bridge_dir=tmp_path / "bridge", auth_secret="pw", user_data_store=True)

    assert env["XDG_DATA_HOME"] == str(real_data)
    assert "OPENCODE_DB" not in env, "import must let OpenCode resolve the user's own DB"
    # Config stays isolated so user plugins and MCP servers never start.
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "bridge" / "xdg-config")


def test_server_user_data_store_sets_real_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "real-data"))
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    server = OpenCodeNativeServer(
        bridge_dir=tmp_path / "bridge",
        workspace=tmp_path,
        verify_version=False,
        user_data_store=True,
    )
    assert server.xdg_data_home == tmp_path / "real-data"
    assert "OPENCODE_DB" not in server.env
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native_app_server.py -k user_data_store -v`
Expected: FAIL with "TypeError: filtered_server_env() got an unexpected keyword argument 'user_data_store'"

- [ ] **Step 3: Write minimal implementation**

In `bridge.py`, replace `user_opencode_auth_path` (`bridge.py:446-457`) with:

```python
def user_xdg_data_home() -> Path:
    """
    Return the user's real ``XDG_DATA_HOME`` (not a per-session one).

    The runner's own env carries the user's data home; per-session overrides
    are set only on spawned servers.

    :returns: ``$XDG_DATA_HOME`` or ``~/.local/share``.
    """
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def user_opencode_auth_path() -> Path:
    """
    Return the user's real OpenCode ``auth.json`` path (not the per-session one).

    :returns: ``<user XDG_DATA_HOME>/opencode/auth.json``.
    """
    return user_xdg_data_home() / "opencode" / "auth.json"
```

In `app_server.py` add `user_xdg_data_home` to the bridge import block. Add the keyword `user_data_store: bool = False` to `filtered_server_env` (after `extra_env`) with this docstring line: `:param user_data_store: Read the user's own OpenCode data dir and DB (session import).`, and insert immediately after the existing `XDG_*` / `OPENCODE_DB` assignments:

```python
    if user_data_store:
        # Session import reads the user's own store in place.
        env["XDG_DATA_HOME"] = str(user_xdg_data_home())
        env.pop("OPENCODE_DB", None)
```

In `OpenCodeNativeServer.__init__` add the keyword `user_data_store: bool = False` (after `verify_version`), document it (`:param user_data_store: Serve the user's own OpenCode store instead of the per-session one.`), store `self._user_data_store = user_data_store`, and change the data-home line to:

```python
        self.xdg_data_home = (
            user_xdg_data_home() if user_data_store else xdg_data_home_for_bridge_dir(bridge_dir)
        )
```

In the `env` property pass it through:

```python
        return filtered_server_env(
            bridge_dir=self.bridge_dir,
            auth_secret=self.auth_secret,
            extra_env=self._extra_env,
            user_data_store=self._user_data_store,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native_app_server.py tests/test_opencode_native_bridge.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/bridge.py omnigent/harnesses/opencode_native/app_server.py tests/test_opencode_native_app_server.py
git commit -m "feat(opencode-native): serve the user's own store for session import"
```

---

### Task 80: Session import lists sessions through a short-lived server

**Files:**
- Modify: `omnigent/session_import/local.py:3-45` (imports, constants), `:152-187` (delete `_run_opencode_json`), `:281-305` (opencode branch)
- Test: `tests/test_session_import.py:174-220`

**Interfaces:**
- Consumes: Task 78 `list_root_sessions`, Task 79 `OpenCodeNativeServer(..., user_data_store=True)`, `user_xdg_data_home`, Stage 1 `client_for_state`.
- Produces:
  - `_opencode_import_client() -> AsyncContextManager[OpenCodeClient]` (module-level, monkeypatchable)
  - `_run_opencode_import(coro: Coroutine[Any, Any, T]) -> T`
  - `_opencode_user_store_exists() -> bool`

- [ ] **Step 1: Write the failing test**

Replace `tests/test_session_import.py:174-220` (the three `test_list_recent_opencode_sessions_*` tests) with:

```python
import contextlib
from collections.abc import AsyncIterator
from typing import Any

from omnigent.harnesses.opencode_native.client import OpenCodeSession


class _FakeImportClient:
    """Stand-in for the import server's OpenCode client."""

    def __init__(
        self,
        *,
        sessions: list[OpenCodeSession] | None = None,
        session: OpenCodeSession | None = None,
        messages: list[dict[str, Any]] | None = None,
    ) -> None:
        self._sessions = sessions or []
        self._session = session
        self._messages = messages or []
        self.list_limits: list[int] = []

    async def list_root_sessions(self, *, limit: int = 100) -> list[OpenCodeSession]:
        self.list_limits.append(limit)
        return self._sessions

    async def get_session(self, session_id: str) -> OpenCodeSession | None:
        return self._session if self._session and self._session.id == session_id else None

    async def list_messages(
        self, session_id: str, *, after_id: str | None = None
    ) -> list[dict[str, Any]]:
        return self._messages


def _use_import_client(monkeypatch: pytest.MonkeyPatch, fake: _FakeImportClient) -> None:
    @contextlib.asynccontextmanager
    async def _fake_cm() -> AsyncIterator[_FakeImportClient]:
        yield fake

    monkeypatch.setattr(local_import, "_opencode_import_client", _fake_cm)
    monkeypatch.setattr(local_import, "_opencode_user_store_exists", lambda: True)


def _session(session_id: str, *, updated: int, parent: str | None = None) -> OpenCodeSession:
    return OpenCodeSession(
        id=session_id,
        parent_id=parent,
        raw={"id": session_id, "time": {"created": 1, "updated": updated}},
    )


def test_list_recent_opencode_sessions_uses_server_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Batch discovery lists top-level sessions across projects, newest first."""
    fake = _FakeImportClient(
        sessions=[
            _session("ses_old", updated=10),
            _session("ses_child", updated=30, parent="ses_parent"),
            _session("ses_new", updated=20),
            _session("--help", updated=40),
        ]
    )
    _use_import_client(monkeypatch, fake)

    assert list_recent_local_session_ids("opencode", limit=2) == ("ses_new", "ses_old")
    assert fake.list_limits == [2]


def test_list_recent_opencode_sessions_without_store_skips_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No local OpenCode DB means no sessions, without starting a server."""

    @contextlib.asynccontextmanager
    async def _fail_cm() -> AsyncIterator[_FakeImportClient]:
        raise AssertionError("the import server must not start without a store")
        yield  # pragma: no cover

    monkeypatch.setattr(local_import, "_opencode_import_client", _fail_cm)
    monkeypatch.setattr(local_import, "_opencode_user_store_exists", lambda: False)

    assert list_recent_local_session_ids("opencode", limit=5) == ()


def test_opencode_user_store_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert local_import._opencode_user_store_exists() is False
    (tmp_path / "opencode").mkdir()
    (tmp_path / "opencode" / "opencode.db").write_bytes(b"")
    assert local_import._opencode_user_store_exists() is True


def test_opencode_import_client_reports_missing_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.harnesses.opencode_native.app_server import OpenCodeCliNotFoundError

    def _missing(_path: str | None = None) -> str:
        raise OpenCodeCliNotFoundError("opencode CLI not found on PATH")

    monkeypatch.setattr(local_import, "find_opencode_cli", _missing)

    async def _open() -> None:
        async with local_import._opencode_import_client():
            pass

    with pytest.raises(SessionImportNotFoundError, match="not found"):
        local_import._run_opencode_import(_open())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_import.py -k opencode -v`
Expected: FAIL with "AttributeError: <module 'omnigent.session_import.local'> does not have the attribute '_opencode_import_client'"

- [ ] **Step 3: Write minimal implementation**

In `local.py`, replace the stdlib/third-party import lines 5-13 and 30-34 so they read:

```python
import asyncio
import contextlib
import json
import os
import re
import sqlite3
import tempfile
from collections.abc import AsyncIterator, Coroutine, Sequence
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar, get_args
```

```python
from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeCliNotFoundError,
    OpenCodeNativeServer,
    client_for_state,
    find_opencode_cli,
)
from omnigent.harnesses.opencode_native.bridge import user_xdg_data_home
from omnigent.harnesses.opencode_native.client import OpenCodeClient, OpenCodeClientError
```

(`subprocess` and the `opencode_tool_output_text` import are removed; `rg -n "subprocess" omnigent/session_import/local.py` must return nothing.) Replace line 45 with:

```python
_OPENCODE_IMPORT_START_TIMEOUT_SECONDS = 120.0
_T = TypeVar("_T")
```

Replace `_run_opencode_json` (`local.py:152-187`) with:

```python
def _opencode_user_store_exists() -> bool:
    """Whether the user has a local OpenCode database to import from."""
    store = user_xdg_data_home() / "opencode"
    return any(store.glob("opencode*.db"))


@contextlib.asynccontextmanager
async def _opencode_import_client() -> AsyncIterator[OpenCodeClient]:
    """Start a short-lived ``opencode serve`` on the user's store and yield a client.

    The server gets a throwaway config home, so the user's plugins and MCP
    servers never start; it is stopped when the block exits.
    """
    try:
        opencode_path = find_opencode_cli(None)
    except OpenCodeCliNotFoundError as exc:
        raise SessionImportNotFoundError(str(exc)) from exc
    with tempfile.TemporaryDirectory(prefix="omnigent-opencode-import-") as scratch:
        server = OpenCodeNativeServer(
            bridge_dir=Path(scratch),
            workspace=Path.home(),
            opencode_path=opencode_path,
            user_data_store=True,
        )
        try:
            await asyncio.wait_for(server.start(), timeout=_OPENCODE_IMPORT_START_TIMEOUT_SECONDS)
        except (OSError, RuntimeError, TimeoutError) as exc:
            raise SessionImportNotFoundError(f"OpenCode server could not start: {exc}") from exc
        client = client_for_state(base_url=server.base_url, auth_secret=server.auth_secret)
        try:
            yield client
        finally:
            await client.aclose()
            await server.close()


def _run_opencode_import(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run one import coroutine from synchronous import code (CLI or worker thread)."""
    return asyncio.run(coro)


async def _list_opencode_root_sessions(limit: int) -> list[tuple[str, float]]:
    """Return ``(session_id, updated_ms)`` for the user's newest top-level sessions."""
    async with _opencode_import_client() as client:
        try:
            sessions = await client.list_root_sessions(limit=limit)
        except OpenCodeClientError as exc:
            raise SessionImportNotFoundError(f"OpenCode session list failed: {exc}") from exc
    recent: list[tuple[str, float]] = []
    for session in sessions:
        if session.parent_id or not _is_safe_opencode_import_session_id(session.id):
            continue
        time_info = session.raw.get("time")
        updated = time_info.get("updated") if isinstance(time_info, dict) else None
        recent.append((session.id, float(updated) if isinstance(updated, (int, float)) else 0.0))
    recent.sort(key=lambda entry: (entry[1], entry[0]), reverse=True)
    return recent[:limit]
```

Replace the opencode branch (`local.py:281-305`) with:

```python
    if source == "opencode":
        if not _opencode_user_store_exists():
            return []
        return _run_opencode_import(_list_opencode_root_sessions(limit))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_import.py -k "opencode or normalize_recency" -v`
Expected: PASS for the four new tests (the v1 `load_opencode_session` tests at the old lines 223-327 fail until Task 81)

- [ ] **Step 5: Commit**

```bash
git add omnigent/session_import/local.py tests/test_session_import.py
git commit -m "feat(opencode-native): list importable sessions through a short-lived v2 server"
```

---

### Task 81: Session import parses v2 messages

**Files:**
- Modify: `omnigent/session_import/local.py:1332-1522` (`_opencode_file_content`, `_opencode_message_items`, `load_opencode_session`)
- Test: `tests/test_session_import.py` (replace the old `test_load_opencode_session_*` tests at the former lines 223-327)

**Interfaces:**
- Consumes: Task 80 `_opencode_import_client`, `_run_opencode_import`; Stage 1 `get_session`, `list_messages`; Stage 0 `tests/opencode_v2_fixtures.load_messages()`.
- Produces: `load_opencode_session(session_id: str) -> LocalSessionImport` reading `Session.Message.Info[]`.

- [ ] **Step 1: Write the failing test**

Replace the two `test_load_opencode_session_*` tests with:

```python
_V2_MESSAGES: list[dict[str, Any]] = [
    {
        "id": "msg_user",
        "type": "user",
        "text": "inspect TODO.md",
        "files": [
            {"data": "AAAA", "mime": "image/png", "source": {"type": "inline"}, "name": "shot.png"},
            {"data": "Zm9v", "mime": "text/plain", "source": {"type": "inline"}, "name": "notes.txt"},
        ],
        "time": {"created": 1},
    },
    {"id": "msg_model", "type": "model-switched", "model": {"id": "m", "providerID": "p"},
     "time": {"created": 2}},
    {
        "id": "msg_assistant",
        "type": "assistant",
        "agent": "build",
        "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
        "time": {"created": 3, "completed": 4},
        "content": [
            {"type": "reasoning", "text": "private reasoning"},
            {"type": "text", "text": "Checking."},
            {
                "type": "tool",
                "id": "call_1",
                "name": "shell",
                "state": {
                    "status": "completed",
                    "input": {"command": "rg TODO"},
                    "content": [{"type": "text", "text": "TODO.md:1:item"}],
                    "metadata": {},
                },
                "time": {"created": 3},
            },
            {"type": "text", "text": "Done."},
        ],
    },
    {"id": "msg_seed", "type": "synthetic", "text": "seeded context", "time": {"created": 5}},
    {
        "id": "msg_failed",
        "type": "assistant",
        "agent": "build",
        "model": {"id": "claude-sonnet-4-5", "providerID": "anthropic"},
        "time": {"created": 6},
        "content": [
            {
                "type": "tool",
                "id": "call_2",
                "name": "edit",
                "state": {
                    "status": "error",
                    "input": {"path": "a.py"},
                    "error": {"type": "permission", "message": "denied by policy"},
                },
                "time": {"created": 6},
            },
            {
                "type": "tool",
                "id": "call_3",
                "name": "shell",
                "state": {"status": "streaming", "input": "{\"command\": \"l"},
                "time": {"created": 6},
            },
        ],
    },
]


def test_load_opencode_session_maps_v2_messages(monkeypatch: pytest.MonkeyPatch) -> None:
    """v2 typed messages map to ordered Omnigent items."""
    fake = _FakeImportClient(
        session=OpenCodeSession(
            id="ses_import",
            title="OpenCode session title",
            directory="/repo",
            raw={"id": "ses_import", "location": {"directory": "/repo"}},
        ),
        messages=_V2_MESSAGES,
    )
    _use_import_client(monkeypatch, fake)

    imported = load_opencode_session("ses_import")
    dumped = [item.data.model_dump(mode="json", exclude_none=True) for item in imported.items]

    assert imported.source == "opencode"
    assert imported.external_session_id == "ses_import"
    assert imported.workspace == "/repo"
    assert imported.native_title == "OpenCode session title"
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
        "function_call",
        "function_call_output",
        "function_call",
    ]
    assert dumped[0] == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "inspect TODO.md"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
            {"type": "input_text", "text": "[attachment: notes.txt]"},
        ],
    }
    assert dumped[1] == {
        "role": "assistant",
        "agent": "opencode-native-ui",
        "content": [{"type": "output_text", "text": "Checking."}],
    }
    assert dumped[2] == {
        "agent": "opencode-native-ui",
        "name": "shell",
        "arguments": '{"command":"rg TODO"}',
        "call_id": "call_1",
    }
    assert dumped[3] == {"call_id": "call_1", "output": "TODO.md:1:item"}
    assert dumped[4]["content"] == [{"type": "output_text", "text": "Done."}]
    assert dumped[6] == {"call_id": "call_2", "output": "[error] denied by policy"}
    assert dumped[7]["arguments"] == '{"command": "l'
    assert {item.response_id for item in imported.items[1:5]} == {"opencode:msg_assistant"}


def test_load_opencode_session_parses_captured_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    """The recon capture of a real 2.0.x turn imports with a shell call."""
    from tests.opencode_v2_fixtures import load_messages

    messages = load_messages()["data"]
    fake = _FakeImportClient(
        session=OpenCodeSession(id="ses_fixture", raw={"id": "ses_fixture"}),
        messages=messages,
    )
    _use_import_client(monkeypatch, fake)

    imported = load_opencode_session("ses_fixture")

    kinds = [item.type for item in imported.items]
    assert kinds[0] == "message"
    assert "function_call" in kinds
    calls = [
        item.data.model_dump(mode="json")
        for item in imported.items
        if item.type == "function_call"
    ]
    assert any(call["name"] == "shell" for call in calls)


def test_load_opencode_session_rejects_unsafe_or_missing_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe ids and unknown sessions are reported as not found."""
    with pytest.raises(SessionImportNotFoundError, match="was not found"):
        load_opencode_session("--help")

    _use_import_client(monkeypatch, _FakeImportClient(session=None))
    with pytest.raises(SessionImportNotFoundError, match="was not found"):
        load_opencode_session("ses_missing")


def test_load_opencode_session_without_history(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeImportClient(
        session=OpenCodeSession(id="ses_empty", raw={"id": "ses_empty"}),
        messages=[{"id": "msg_s", "type": "synthetic", "text": "x", "time": {"created": 1}}],
    )
    _use_import_client(monkeypatch, fake)
    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_opencode_session("ses_empty")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_session_import.py -k load_opencode -v`
Expected: FAIL with "TypeError: load_opencode_session() ..." / "NameError: name '_run_opencode_json' is not defined"

- [ ] **Step 3: Write minimal implementation**

Replace `local.py:1332-1522` (`_opencode_file_content` through the end of `load_opencode_session`) with:

```python
def _opencode_user_file_content(attachment: dict[str, object]) -> dict[str, object]:
    """Convert one v2 user ``FileAttachment`` to a durable content block."""
    mime = attachment.get("mime")
    data = attachment.get("data")
    if isinstance(mime, str) and mime.startswith("image/") and isinstance(data, str) and data:
        return {"type": "input_image", "image_url": f"data:{mime};base64,{data}"}
    name = attachment.get("name")
    label = name if isinstance(name, str) and name else mime
    if not isinstance(label, str) or not label:
        label = "attachment"
    return {"type": "input_text", "text": f"[attachment: {label}]"}


def _opencode_tool_content_text(content: object) -> str:
    """Flatten v2 tool ``content`` (``Tool.TextContent`` / ``Tool.FileContent``)."""
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block.get("type") == "file":
            label = block.get("name") or block.get("mime") or "file"
            parts.append(f"[file: {label}]")
    return "\n".join(part for part in parts if part)


def _opencode_tool_items(
    tool: dict[str, object], *, response_id: str
) -> tuple[NewConversationItem, ...]:
    """Normalize one v2 assistant ``tool`` content block into call (+ output) items."""
    call_id = tool.get("id")
    name = tool.get("name")
    state = tool.get("state")
    if (
        not isinstance(call_id, str)
        or not call_id
        or not isinstance(name, str)
        or not name
        or not isinstance(state, dict)
    ):
        return ()
    arguments = state.get("input")
    serialized_arguments = (
        arguments
        if isinstance(arguments, str)
        else json.dumps(
            arguments if arguments is not None else {},
            separators=(",", ":"),
            ensure_ascii=True,
        )
    )
    items = [
        NewConversationItem(
            type="function_call",
            response_id=response_id,
            data=parse_item_data(
                "function_call",
                {
                    "agent": "opencode-native-ui",
                    "name": name,
                    "arguments": serialized_arguments,
                    "call_id": call_id,
                },
            ),
        )
    ]
    status = state.get("status")
    output: str | None = None
    if status == "completed":
        output = _opencode_tool_content_text(state.get("content"))
    elif status == "error":
        error = state.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        output = f"[error] {message}" if message else "[error]"
    if output is not None:
        items.append(
            NewConversationItem(
                type="function_call_output",
                response_id=response_id,
                data=parse_item_data(
                    "function_call_output",
                    {"call_id": call_id, "output": output},
                ),
            )
        )
    return tuple(items)


def _opencode_message_items(
    message: dict[str, object],
    *,
    message_number: int,
) -> tuple[NewConversationItem, ...]:
    """Normalize one v2 ``Session.Message.Info`` while preserving content order."""
    message_type = message.get("type")
    if message_type not in {"user", "assistant"}:
        return ()
    message_id = message.get("id")
    native_id = message_id if isinstance(message_id, str) and message_id else str(message_number)
    response_id = _bounded_response_id(f"opencode:{native_id}")

    if message_type == "user":
        content: list[dict[str, object]] = []
        text = message.get("text")
        if isinstance(text, str) and text:
            content.append({"type": "input_text", "text": text})
        files = message.get("files")
        for attachment in files if isinstance(files, list) else []:
            if isinstance(attachment, dict):
                content.append(_opencode_user_file_content(attachment))
        if not content:
            return ()
        return (
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=parse_item_data("message", {"role": "user", "content": content}),
            ),
        )

    items: list[NewConversationItem] = []
    pending_text: list[dict[str, object]] = []

    def flush_text() -> None:
        if not pending_text:
            return
        items.append(
            NewConversationItem(
                type="message",
                response_id=response_id,
                data=parse_item_data(
                    "message",
                    {
                        "role": "assistant",
                        "agent": "opencode-native-ui",
                        "content": list(pending_text),
                    },
                ),
            )
        )
        pending_text.clear()

    blocks = message.get("content")
    for block in blocks if isinstance(blocks, list) else []:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                pending_text.append({"type": "output_text", "text": text})
        elif block_type == "tool":
            flush_text()
            items.extend(_opencode_tool_items(block, response_id=response_id))
    flush_text()
    return tuple(items)


async def _fetch_opencode_session(
    session_id: str,
) -> tuple[OpenCodeSession, list[dict[str, object]]]:
    """Read one session and its full message history from the import server."""
    async with _opencode_import_client() as client:
        try:
            session = await client.get_session(session_id)
            if session is None:
                raise SessionImportNotFoundError(f"OpenCode session {session_id!r} was not found")
            messages = await client.list_messages(session_id)
        except OpenCodeClientError as exc:
            raise SessionImportNotFoundError(
                f"OpenCode session {session_id!r} could not be read: {exc}"
            ) from exc
    return session, messages


def load_opencode_session(session_id: str) -> LocalSessionImport:
    """Load one session from the user's OpenCode store through ``GET /api/session/{id}/message``."""
    if not _is_safe_opencode_import_session_id(session_id):
        raise SessionImportNotFoundError(f"OpenCode session {session_id!r} was not found")
    session, messages = _run_opencode_import(_fetch_opencode_session(session_id))
    items = tuple(
        item
        for message_number, message in enumerate(messages, start=1)
        if isinstance(message, dict)
        for item in _opencode_message_items(message, message_number=message_number)
    )
    if not items:
        raise SessionImportNotFoundError(
            f"OpenCode session {session_id!r} has no importable history"
        )
    location = session.raw.get("location")
    workspace_value = session.directory or (
        location.get("directory") if isinstance(location, dict) else None
    )
    workspace = workspace_value.strip() if isinstance(workspace_value, str) else None
    native_title = session.title.strip() if session.title and session.title.strip() else None
    return LocalSessionImport(
        source="opencode",
        external_session_id=session_id,
        workspace=workspace or None,
        items=items,
        native_title=native_title,
    )
```

Add `OpenCodeSession` to the client import added in Task 80:

```python
from omnigent.harnesses.opencode_native.client import (
    OpenCodeClient,
    OpenCodeClientError,
    OpenCodeSession,
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_session_import.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/session_import/local.py tests/test_session_import.py
git commit -m "feat(opencode-native): import sessions from v2 typed messages"
```

---

### Task 82: Update `omnigent opencode` CLI and host e2e wording for v2

**Files:**
- Modify: `omnigent/harnesses/opencode_native/main.py:11-13, 171-174`
- Modify: `omnigent/cli_native.py:507`
- Modify: `tests/e2e/test_host_opencode_native_e2e.py:1-22, 44-49, 224-236, 259-261`
- Test: `tests/test_opencode_native.py` (grep guard)

**Interfaces:**
- Consumes: none (text only; the CLI never calls OpenCode's HTTP API or `attach`).
- Produces: no `opencode attach` / pinned-1.x wording in the CLI and host e2e.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_opencode_native.py`:

```python
from pathlib import Path


def test_opencode_cli_and_host_e2e_have_no_v1_attach_wording() -> None:
    repo = Path(__file__).resolve().parents[1]
    for relative in (
        "omnigent/harnesses/opencode_native/main.py",
        "omnigent/cli_native.py",
        "tests/e2e/test_host_opencode_native_e2e.py",
    ):
        text = (repo / relative).read_text(encoding="utf-8")
        assert "opencode attach" not in text, f"{relative} still describes the v1 attach TUI"
        assert "this PR" not in text, f"{relative} references a PR instead of the scenario"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_opencode_native.py::test_opencode_cli_and_host_e2e_have_no_v1_attach_wording -v`
Expected: FAIL with "omnigent/harnesses/opencode_native/main.py still describes the v1 attach TUI"

- [ ] **Step 3: Write minimal implementation**

`main.py:11-13` becomes:

```python
it ensures a local daemon + runner, creates-or-resumes the ``opencode-native-ui``
session (whose runner auto-creates the ``opencode serve`` process and the
``opencode --server <url> --session <id>`` TUI terminal), and attaches this TTY
directly to that runner-owned tmux pane — the
```

`main.py:171-174` (in `run_opencode_native`'s docstring) becomes:

```python
    Mirrors ``omnigent codex`` / ``omnigent pi``: ensure a local daemon + runner,
    create-or-resume the ``opencode-native-ui`` session (the runner auto-creates
    ``opencode serve`` and the ``opencode --server`` TUI terminal), then attach
    this TTY to that runner-owned tmux pane.
```

`cli_native.py:507` becomes:

```python
        # :param opencode_args: Pass-through args persisted for the ``opencode --server`` TUI.
```

`tests/e2e/test_host_opencode_native_e2e.py:4-8` becomes:

```python
(which drives ``OpenCodeNativeServer`` directly). This exercises the WHOLE
product path: list built-in agents -> find ``opencode-native-ui`` -> connect a
host daemon -> create a host-bound session -> the runner auto-creates the
``opencode serve --stdio`` + SSE forwarder + ``opencode --server`` TUI terminal
resource -> send a user message -> poll session items until the assistant echoes a marker.
```

and lines 10-18 become:

```python
Opt-in and run manually before merging opencode-native changes (needs
``@opencode/cli`` 2.0.x on PATH and LLM credentials)::

    npm install -g @opencode/cli@~2.0.18
    OMNIGENT_E2E_OPENCODE_NATIVE=1 \
    HOME=/tmp/omni-isolated DATABRICKS_CONFIG_FILE=$REAL_HOME/.databrickscfg \
    uv run pytest tests/e2e/test_host_opencode_native_e2e.py \
        --profile ai-devtools-prod \
        --llm-api-key "$(databricks auth token -p ai-devtools-prod \
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
        -v
```

In the skip reason (lines 44-49) replace "needs a pinned `opencode` binary" with "needs `@opencode/cli` 2.0.x". In the test docstring (line 229) replace "forwarder + ``opencode attach``)" with "forwarder + ``opencode --server`` TUI)". Replace the comment at lines 259-261 with:

```python
        # The runner registers the TUI on session creation; without it the
        # Web UI has no terminal to attach to.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_opencode_native.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add omnigent/harnesses/opencode_native/main.py omnigent/cli_native.py tests/e2e/test_host_opencode_native_e2e.py tests/test_opencode_native.py
git commit -m "docs(opencode-native): describe the v2 TUI in the CLI and host e2e"
```

---

### Stage 4 verification

- [ ] Run the full opencode and runner suites:

```bash
uv run pytest tests/test_opencode_native_bridge.py tests/test_opencode_native_app_server.py tests/test_opencode_native_client.py tests/test_session_import.py tests/test_opencode_native.py tests/runner/test_opencode_native_orchestration.py tests/runner/test_opencode_resume.py tests/runner/test_opencode_policy_evaluator.py tests/runner/test_native_interrupt_runner.py tests/runner/test_app_sessions_native_events_options.py tests/runner/test_app_sessions_native_events_lifecycle.py tests/runner/test_codex_native_launch_config.py tests/server/routes/test_sessions_fork.py -v
```

- [ ] Confirm the v1 helpers are gone:

```bash
rg -n "_resolve_opencode_compact_model|list_opencode_cli_model_options|summarize\(|_run_opencode_json|\"--pure\"|build_opencode_attach_args|opencode_tool_output_text" omnigent tests
```

Expected: no matches in `omnigent/runner`, `omnigent/session_import`, or the tests touched in this stage.

- [ ] `uvx pre-commit run --files $(git diff --name-only main...HEAD)`

- [ ] Manual full-stack check (with `@opencode/cli` 2.0.x and credentials), from the web UI on one opencode-native conversation:
  1. Send a prompt, then `/compact`: the transcript shows a compaction status and the next turn continues.
  2. Switch the model in the picker (options list real `provider/model` ids): the OpenCode TUI footer shows the new model before the next prompt.
  3. Start a long `shell` turn and press Stop: the TUI shows the turn interrupted and the web session returns to idle.
  4. Fork the conversation (same agent, from the latest message) into the same workspace: the clone's TUI opens on a session titled `... (fork #1)` with the prior messages, and `sqlite3 ~/.omnigent/opencode-native/<clone digest>/opencode.db "select id, time_suspended from session_v2"` shows no non-null claims.
  5. Fork from an earlier message: the clone opens on a fresh session whose first message is the "[Resumed session ..." preamble (truncated forks never clone natively).
  6. `/clear` the forked clone: the TUI opens an empty session with no preamble.
  7. Stop the host daemon, delete the conversation's bridge dir, restart and send a prompt: the conversation resumes with the preamble (lost-session rehydration).
  8. `omnigent import --harness opencode --last 3`: the three newest OpenCode sessions from any project import with user text, assistant text, and shell calls with outputs; no `opencode` process is left running afterwards (`pgrep -fa "opencode serve"`).
## Stage 5: Docs, capability registry, web copy, and final verification

Branch: `opencode-v2-stage-5-docs` stacked on Stage 4 (`gs branch create opencode-v2-stage-5-docs`).

What this stage does:
- Capability row for `opencode-native`: declares the session features v2 provides and records that text streaming is backed by v2 deltas.
- Web copy for hosts still on OpenCode 1.x: the "outdated" notice names the new npm package.
- Removes the remaining `opencode attach` / `opencode-ai` / 1.17.7 wording outside the harness package.
- Adds the in-repo `docs/opencode-native.md` page and supersedes the v1 gap doc.
- Adds the changelog line and a manual full-stack checklist.

Findings from reading the repo (these decide the task scope):

1. **Docs site.** omnigent.ai docs (including `/docs/build/harnesses/configuration`) are **not in this repo**. They live in the separate `omnigent-ai/omnigent-site` repository. `.github/workflows/publish-changelog.yml` and `sync-openapi-to-site.yml` open PRs there. No `docs-site/`, `website/` or `web/docs` directory exists, and nothing in the repo references `harnesses/configuration`. Plan: add `docs/opencode-native.md` in-repo (Task 87), modeled on `docs/devin-native.md`. The follow-up PR to `omnigent-site` is noted in Task 90.
2. **CHANGELOG.md is generated, and it has no `Unreleased` section.** Its header (lines 3-6) says it "is generated at release time from each PR's `## Changelog` section". `draft-release-notes.yml` writes it at release-cut. All recent commits touching it are `docs(changelog): record vX.Y.Z`. Adding a hand-written `## Unreleased` block would duplicate the line at release. Task 90 therefore puts the line in the PR body's `## Changelog` section, in the exact style of existing entries. It does **not** edit `CHANGELOG.md`.
3. **Capability field for deltas.** `HarnessCapabilities.streaming: bool` in `omnigent/harness_capabilities.py:120-122` means "forwards token-level deltas (vs a single complete blob)". There is no separate complete-only enum. `opencode-native` already declares `streaming=True` in `omnigent/harness_plugins.py:508`. Under v1 that claim was unverified, because v1 sends part snapshots. Task 83 keeps `streaming=True`, records the v2-delta evidence, and fills in the optional axes that stay `None` today: `steering`, `live_queue`, `images` and `compaction`. It also moves `instruction_delivery` from `COMPOSED_PER_TURN` to `COMPOSED_SESSION_SNAPSHOT`, because v2 has no per-prompt `system` field.
4. **`web/src/lib/harnessSetup.ts` contains no npm package name.** Install and login copy comes from the server's `ui_setup_steps()` in `omnigent/onboarding/harness_install.py:548-555`. That copy already says `opencode auth login`, and Stage 1 changes the package pin. The one place users see OpenCode-version copy is the generic "outdated CLI" notice in `web/src/shell/NewChatDialog.tsx:817-824`. That notice tells a v1 user to "upgrade the CLI directly", which does not work across the package rename. Task 84 adds an OpenCode harness predicate and Task 85 adds the package-specific copy.
5. **The harness-bench driver has no OpenCode code path to change.** `tests/harness_bench/native_tui_driver.py:166-176` returns `None` for `NATIVE_SERVER` harnesses ("``native-server`` harnesses (e.g. opencode-native) are a different transport"), and it never builds an `opencode attach` argv. The "attach → `--server`" wording lives in comments and docstrings instead: `web/src/lib/nativeCodingAgents.ts:141-150`, `omnigent/cli_native.py:510`, `omnigent/harnesses/opencode_native/main.py:12,173`, `omnigent/onboarding/harness_install.py:132-134` and `tests/e2e/test_host_opencode_native_e2e.py:7`. Task 86 sweeps these.
6. **No changes needed in these files** (verified by grep):
   - `.claude/skills/harness-integration-guide/` never mentions OpenCode.
   - `examples/polly/agents/opencode/config.yaml` has only `harness: opencode-native`, with no version, package or v1 key.
   - `deploy/docker/README.md:394-397` lists only harness names; the pin lives in `install-harness-cli.sh`, which Stage 1 owns.
   - `README.md:288` (`omnigent opencode  # OpenCode`) is version-neutral. Task 88 adds one pointer line next to it.
7. **The local server command is `omnigent server`, not `omnigent serve`.** See `omnigent/cli.py:4150` (`@cli.group("server", ...)`) and `CONTRIBUTING.md:245`. The manual checklist (Task 91) uses `omnigent server`.

---

### Task 83: Declare v2 session capabilities on the `opencode-native` row

**Files:**
- Modify: `omnigent/harness_plugins.py:499-512`
- Test: `tests/test_harness_capabilities.py` (append after `test_kiro_native_is_not_delivered`, currently the last test at ~line 370-372)

**Interfaces:**
- Consumes:
  - `tests/opencode_v2_fixtures.events_of_type(type_: str) -> list[dict]` (Stage 0).
  - The Stage 1 `http_transport.build_prompt_payload(..., delivery="steer")`. It sends `delivery` and no `system` field.
  - The Stage 3 `provider.build_opencode_config(..., instructions=...)`. It delivers the composed prompt once per server launch.
- Produces: `harness_capabilities()["opencode-native"]` with these values:
  - `streaming=True`, `steering=True`, `live_queue=True`, `images=True`, `compaction=True`
  - `fork_history=ForkHistory.PREAMBLE` (unchanged)
  - `instruction_delivery=InstructionDelivery.COMPOSED_SESSION_SNAPSHOT`
  - The `/v1/harnesses` catalog (`HarnessCapabilities.as_dict`) exposes the same values.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_harness_capabilities.py`:

```python
def test_opencode_native_declares_v2_session_capabilities() -> None:
    """OpenCode 2.x streams deltas and exposes steer/queue, images and compaction."""
    capability = harness_capabilities()["opencode-native"]
    assert capability.integration_mode is IntegrationMode.NATIVE_SERVER
    assert capability.streaming is True
    assert capability.steering is True
    assert capability.live_queue is True
    assert capability.images is True
    assert capability.compaction is True
    # Native fork stays unverified live, so history still rides as a preamble.
    assert capability.fork_history is ForkHistory.PREAMBLE
    # v2 has no per-prompt system field; instructions ride the launch config.
    assert capability.instruction_delivery is InstructionDelivery.COMPOSED_SESSION_SNAPSHOT
    catalog = capability.as_dict()
    assert catalog["streaming"] is True
    assert catalog["instruction_delivery"] == "composed-session-snapshot"


def test_opencode_native_streaming_claim_is_backed_by_v2_deltas() -> None:
    """The streaming=True claim rests on real v2 delta events, not snapshots."""
    from tests.opencode_v2_fixtures import events_of_type

    assert events_of_type("session.text.delta"), "fixture lost its text deltas"
    assert events_of_type("session.reasoning.delta"), "fixture lost its reasoning deltas"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_harness_capabilities.py::test_opencode_native_declares_v2_session_capabilities tests/test_harness_capabilities.py::test_opencode_native_streaming_claim_is_backed_by_v2_deltas -v`

Expected:
- The first test FAILS with `assert None is True` on `capability.steering`.
- The second test PASSES, because the Stage 0 fixture already exists. It is a guard, not a driver.

- [ ] **Step 3: Write minimal implementation**

In `omnigent/harness_plugins.py`, replace lines 499-512. The current code is:

```python
    "opencode-native": _C(
        _IM.NATIVE_SERVER,
        _EL.SSE_PERMISSION,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=True,
        interrupt=True,
        streaming=True,
        fork_history=_FH.PREAMBLE,
        # NATIVE_SERVER, not driven by the bench's native-tui tool probe, so
        # shell_tool_* stay None.
        instruction_delivery=_ID.COMPOSED_PER_TURN,
    ),
```

Replace it with:

```python
    # OpenCode 2.x: streaming is backed by session.text.delta /
    # session.reasoning.delta events (see tests/fixtures/opencode_v2/events.ndjson).
    # Prompts carry delivery "steer" or "queue"; files carry images; /compact is native.
    "opencode-native": _C(
        _IM.NATIVE_SERVER,
        _EL.SSE_PERMISSION,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=True,
        interrupt=True,
        streaming=True,
        steering=True,
        live_queue=True,
        images=True,
        compaction=True,
        fork_history=_FH.PREAMBLE,
        # NATIVE_SERVER, not driven by the bench's native-tui tool probe, so
        # shell_tool_* stay None.
        # The composed prompt rides the launch config's `instructions` key.
        instruction_delivery=_ID.COMPOSED_SESSION_SNAPSHOT,
    ),
```

(If a Stage 3 task already changed `instruction_delivery` on this row, keep its value and change only the other fields.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_harness_capabilities.py -v`

Expected: PASS for the whole file. That includes `test_fork_history_axis_matches_canonical_declarations`, because the PREAMBLE membership is unchanged, and `test_every_canonical_harness_declares_instruction_delivery`.

- [ ] **Step 5: Commit**

```bash
git add omnigent/harness_plugins.py tests/test_harness_capabilities.py
git commit -m "feat(opencode-native): declare v2 steering, queue, images and compaction capabilities"
```

---

### Task 84: Add an OpenCode harness predicate to the web setup helpers

**Files:**
- Modify: `web/src/lib/harnessSetup.ts:181-183` (add the new export after `isNativeCursorHarness`)
- Test: `web/src/lib/harnessSetup.test.ts:3-12` (import block) and the end of the file (new `describe`)

**Interfaces:**
- Consumes: nothing new.
- Produces: `export function isNativeOpenCodeHarness(harness: string): boolean`. It returns true for `"opencode-native"`, `"native-opencode"` and `"opencode"`. Task 85 uses it.

- [ ] **Step 1: Write the failing test**

In `web/src/lib/harnessSetup.test.ts`, replace the import block at lines 3-12:

```ts
import {
  harnessAuthableOnHost,
  harnessCredentialAdoptFamilies,
  harnessCredentialFamily,
  harnessInstallableOnHost,
  harnessReadinessOnHost,
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  resolveSetupSteps,
} from "./harnessSetup";
```

with:

```ts
import {
  harnessAuthableOnHost,
  harnessCredentialAdoptFamilies,
  harnessCredentialFamily,
  harnessInstallableOnHost,
  harnessReadinessOnHost,
  harnessUnavailableReasonOnHost,
  harnessUnconfiguredOnHost,
  harnessWarningBadgeText,
  isNativeOpenCodeHarness,
  resolveSetupSteps,
} from "./harnessSetup";
```

Fix the stale comment at line 338. Change:

```ts
    // OpenCode/Qwen (env-auth) and Cursor (own-login) are NOT UI-authable.
```

to:

```ts
    // OpenCode/Cursor (own CLI login) and Qwen (env-auth) are NOT UI-authable.
```

Append at the end of the file:

```ts
// The server's opencode descriptor: install, then a run-on-host CLI login that
// the host tracks through its "authed" readiness.
const OPENCODE_STEPS: SetupStepWire[] = [
  {
    kind: "install",
    title: "Install OpenCode",
    detail: "We'll install OpenCode on the host for you.",
    action: "install",
    command: null,
    status_key: "installed",
  },
  {
    kind: "auth",
    title: "Sign in to OpenCode",
    detail: "OpenCode manages its own credentials — sign in on the host.",
    action: "command",
    command: "opencode auth login",
    status_key: "authed",
  },
];

describe("OpenCode setup", () => {
  it("recognizes every OpenCode harness spelling", () => {
    expect(isNativeOpenCodeHarness("opencode-native")).toBe(true);
    expect(isNativeOpenCodeHarness("native-opencode")).toBe(true);
    expect(isNativeOpenCodeHarness("opencode")).toBe(true);
    expect(isNativeOpenCodeHarness("codex-native")).toBe(false);
    expect(isNativeOpenCodeHarness("cursor-native")).toBe(false);
  });

  it("flags a 1.x OpenCode host as outdated with install + login still to do", () => {
    const host = hostWith({ "opencode-native": "version-too-low" });
    expect(harnessUnavailableReasonOnHost("opencode-native", host)).toBe("version-too-low");
    expect(harnessWarningBadgeText("version-too-low")).toBe("outdated");
    const steps = resolveSetupSteps(OPENCODE_STEPS, "opencode-native", host);
    expect(steps.map((s) => [s.kind, s.status])).toEqual([
      ["install", "todo"],
      ["auth", "todo"],
    ]);
    expect(steps[1].command).toBe("opencode auth login");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/lib/harnessSetup.test.ts`

Expected: FAIL with `SyntaxError: The requested module './harnessSetup' does not provide an export named 'isNativeOpenCodeHarness'` (or the vitest message `isNativeOpenCodeHarness is not a function`).

- [ ] **Step 3: Write minimal implementation**

In `web/src/lib/harnessSetup.ts`, find the existing code at lines 181-183:

```ts
export function isNativeCursorHarness(harness: string): boolean {
  return harness === "cursor-native" || harness === "native-cursor";
}
```

Insert this directly after it:

```ts

/** Whether *harness* is an OpenCode spelling (bare, native, or reversed). */
export function isNativeOpenCodeHarness(harness: string): boolean {
  return harness === "opencode-native" || harness === "native-opencode" || harness === "opencode";
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/lib/harnessSetup.test.ts`

Expected: PASS (all tests in the file).

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/harnessSetup.ts web/src/lib/harnessSetup.test.ts
git commit -m "feat(opencode-native): add OpenCode harness predicate to web setup helpers"
```

---

### Task 85: Tell 1.x OpenCode hosts which package to install

**Files:**
- Modify: `web/src/shell/NewChatDialog.tsx:117-121` (import) and `:817-824` (`harnessWarningMessage` version-too-low branch)
- Test: `web/src/shell/NewChatDialog.test.tsx` (the `it.each` table at ~5905-5942 that holds the `a_codex` / `a_cursor` / `a_pi` / `a_polly` rows)

**Interfaces:**
- Consumes: `isNativeOpenCodeHarness(harness: string): boolean` (Task 84).
- Produces: user-visible copy when an OpenCode agent's host reports `version-too-low`: `"<Agent> needs OpenCode 2.0 on <host> — run npm i -g @opencode/cli@~2.0.18 (after npm rm -g opencode-ai) or omni setup on that machine."`. The pin must match `OPENCODE_KEY`'s package in `omnigent/onboarding/harness_install.py` (set to `@opencode/cli@~2.0.18` in Stage 1).

- [ ] **Step 1: Write the failing test**

In `web/src/shell/NewChatDialog.test.tsx`, add a row to the `it.each([...])` table directly after the `a_pi` row. The `a_pi` row currently ends:

```ts
      readiness: "version-too-low",
      badgeName: "outdated",
      warning: "Pi has an outdated CLI on machine-1 — run omni setup",
    },
```

The new row:

```ts
    {
      id: "a_opencode",
      name: "opencode-native-ui",
      displayName: "OpenCode",
      harness: "opencode-native",
      readiness: "version-too-low",
      badgeName: "outdated",
      warning: "OpenCode needs OpenCode 2.0 on machine-1 — run npm i -g @opencode/cli@~2.0.18",
    },
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd web && npx vitest run src/shell/NewChatDialog.test.tsx -t "availability warning for OpenCode"`

Expected: FAIL. The notice renders `OpenCode has an outdated CLI on machine-1 — run omni setup, or upgrade the CLI directly on that machine.` and `toHaveTextContent` cannot find `OpenCode needs OpenCode 2.0 ...`.

- [ ] **Step 3: Write minimal implementation**

In `web/src/shell/NewChatDialog.tsx`, change the import at lines 117-121 from:

```tsx
  harnessWarningBadgeText,
  isCodexHarness,
  isNativeCursorHarness,
} from "@/lib/harnessSetup";
```

to:

```tsx
  harnessWarningBadgeText,
  isCodexHarness,
  isNativeCursorHarness,
  isNativeOpenCodeHarness,
} from "@/lib/harnessSetup";
```

Then find the existing code at lines 817-824:

```tsx
  // ``version-too-low`` is a uniform state across all CLI harnesses now that
  // the server checks supported version ranges. Keep the message generic so
  // the user is nudged toward setup rather than being told the CLI is missing.
  if (reason === "version-too-low") {
```

Insert this directly **before** it:

```tsx
  // OpenCode 2.x ships as a new npm package; an in-place 1.x upgrade can't reach it.
  if (reason === "version-too-low" && !!harness && isNativeOpenCodeHarness(harness)) {
    return (
      <>
        {agentName} needs OpenCode 2.0 on {hostName} — run{" "}
        <code>npm i -g @opencode/cli@~2.0.18</code> (after <code>npm rm -g opencode-ai</code>) or{" "}
        <code>omni setup</code> on that machine.
      </>
    );
  }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd web && npx vitest run src/shell/NewChatDialog.test.tsx src/lib/harnessSetup.test.ts`

Expected: PASS. The Pi row still renders the generic "outdated CLI" copy.

- [ ] **Step 5: Commit**

```bash
git add web/src/shell/NewChatDialog.tsx web/src/shell/NewChatDialog.test.tsx
git commit -m "feat(opencode-native): point outdated OpenCode hosts at @opencode/cli"
```

---

### Task 86: Replace stale `opencode attach` / `opencode-ai` / 1.17.7 wording outside the harness package

No behavior changes, so this task has no unit test. It is verified by grep and by running the files' existing tests. The harness-bench driver `tests/harness_bench/native_tui_driver.py` has no OpenCode attach code (see finding 5), so this task does not touch it.

**Files:**
- Modify: `web/src/lib/nativeCodingAgents.ts:141-150`
- Modify: `omnigent/cli_native.py:510`
- Modify: `omnigent/harnesses/opencode_native/main.py:12` and `:173` (skip if Stage 1 already rewrote them)
- Modify: `omnigent/onboarding/harness_install.py:132-134` (skip if Stage 1 already rewrote it)
- Modify: `tests/e2e/test_host_opencode_native_e2e.py:7` and `:45-49` (skip reason)
- Modify: `tests/e2e/test_opencode_native_wire_contract_e2e.py:1-23` (docstring) and `:40-46` (skip reason). Skip these if Stage 1's wire-contract rewrite already dropped the `openapi-1.17.7.json` / `opencode-ai@1.17.7` references.

**Interfaces:**
- Consumes: `build_tui_command(opencode_path, *, base_url, session_id, workspace, extra_args=()) -> list[str]` (Stage 1). The comments now describe this command.
- Produces: nothing callable.

- [ ] **Step 1: Write the failing check**

Run:

```bash
git grep -n -e "opencode attach" -e "opencode-ai@1.17.7" -e "openapi-1.17.7.json" -e "(>=1.17.7,<1.18.0)" -- web/src omnigent/cli_native.py omnigent/harnesses/opencode_native/main.py omnigent/onboarding/harness_install.py tests/e2e/test_host_opencode_native_e2e.py tests/e2e/test_opencode_native_wire_contract_e2e.py
```

- [ ] **Step 2: Confirm the check fails**

Expected: matches are printed. At minimum you should see `web/src/lib/nativeCodingAgents.ts:144`, `:149`, `omnigent/cli_native.py:510` and `tests/e2e/test_host_opencode_native_e2e.py:7`.

- [ ] **Step 3: Rewrite the wording**

**3a.** `web/src/lib/nativeCodingAgents.ts:141-150`. Replace:

```ts
    // No capabilities → no permission picker. OpenCode has no claude-style
    // permission-mode surface to mirror: its native modes are the `build`
    // (allow-by-default) and `plan` primary agents, switched at runtime via Tab
    // inside the TUI — and `opencode attach` (how the runner launches it) has
    // no `--agent` flag to preset one anyway. The runner already forces
    // `permission: "ask"` so tools route through the Omnigent policy engine, so
    // a launch-time picker would mirror nothing. (Previously declared Codex's
    // `approvalMode`, whose `--sandbox`/`--ask-for-approval` presets aren't
    // understood by `opencode attach` and crashed the TUI on any non-default
    // pick.)
```

with:

```ts
    // No capabilities → no permission picker. OpenCode's native modes are the
    // `build` and `plan` primary agents, switched via Tab inside the TUI, and
    // `opencode --server <url>` (how the runner launches it) has no `--agent`
    // flag. The runner's config always asks on every tool (`permissions` rule
    // `{action:"*", effect:"ask"}`), so a launch-time picker would mirror nothing.
```

**3b.** `omnigent/cli_native.py:510`. Replace:

```python
        # :param opencode_args: Pass-through args persisted for the ``opencode attach`` TUI.
```

with:

```python
        # :param opencode_args: Pass-through args persisted for the ``opencode --server`` TUI.
```

**3c.** `omnigent/harnesses/opencode_native/main.py:12`. Replace `session (whose runner auto-creates the ``opencode serve`` + ``opencode attach``` with `session (whose runner auto-creates the ``opencode serve`` + ``opencode --server```.

At `:173`, replace `the ``opencode serve`` + ``opencode attach`` terminal), then attach this TTY` with `the ``opencode serve`` + ``opencode --server`` terminal), then attach this TTY`.

**3d.** `omnigent/onboarding/harness_install.py:132-134`. Replace:

```python
# OpenCode native harness CLI (``opencode serve`` / ``opencode attach``),
# installed via the ``opencode-ai`` npm package. No login/logout/status argv
# is wired yet — readiness is binary-only until an auth check exists.
```

with:

```python
# OpenCode native harness CLI (``opencode serve`` / ``opencode --server``),
# installed via the ``@opencode/cli`` npm package (2.x).
```

(If Stage 1 wired `opencode auth` status argv, keep any sentence it wrote about that.)

**3e.** `tests/e2e/test_host_opencode_native_e2e.py:7`. Replace ```` ``opencode serve`` + SSE forwarder + ``opencode attach`` terminal resource -> ```` with ```` ``opencode serve`` + SSE forwarder + ``opencode --server`` terminal resource -> ````.

In the `pytestmark` skip reason (lines ~45-49), replace `"opencode-native host e2e needs a pinned `opencode` binary + LLM creds; "` with `"opencode-native host e2e needs `opencode` 2.x (npm @opencode/cli@~2.0.18) + LLM creds; "`.

**3f.** `tests/e2e/test_opencode_native_wire_contract_e2e.py`, only if Stage 1 left these in place. Replace docstring lines 1-23 with:

```python
"""End-to-end test: the OpenCode-native client speaks to a REAL ``opencode serve``.

The opencode-native harness's HTTP+SSE client (``omnigent.harnesses.opencode_native.client``)
is shaped from the OpenCode 2.x OpenAPI (captured at
``tests/fixtures/opencode_v2/openapi.json``), so the rest of the suite exercises it
only against in-process fakes. This test boots a real ``opencode serve --stdio`` via
:class:`~omnigent.harnesses.opencode_native.app_server.OpenCodeNativeServer` and
drives the provider-independent ``/api/*`` endpoints the harness relies on, validating
the wire contract against the actual binary — the one thing the fakes cannot prove.

Environment requirements (why this is opt-in, not pure-CI)
----------------------------------------------------------
* **Opt-in only**: set ``OMNIGENT_E2E_OPENCODE_NATIVE=1`` and have ``opencode``
  2.x (>=2.0.0,<3.0.0; ``npm i -g @opencode/cli@~2.0.18``) on ``PATH``. No
  interactive login or model credential is needed — ``/api/info``, session
  create/get, message list, the ``/api/event`` stream, fork and interrupt are all
  provider-independent. The gate just keeps it off CI runners without the binary.
* Run it with::

    OMNIGENT_E2E_OPENCODE_NATIVE=1 \
    uv run pytest tests/e2e/test_opencode_native_wire_contract_e2e.py -v
"""
```

In the skip reason at lines ~43-46, replace `"set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and `npm i -g opencode-ai@1.17.7`) to run"` with `"set OMNIGENT_E2E_OPENCODE_NATIVE=1 (and `npm i -g @opencode/cli@~2.0.18`) to run"`.

- [ ] **Step 4: Re-run the check and the affected suites**

Run the same grep as Step 1. Expected: no output, and exit code 1.

Then run:
- `uv run pytest tests/e2e/test_host_opencode_native_e2e.py tests/e2e/test_opencode_native_wire_contract_e2e.py -v`. Expected: both SKIPPED with the new reasons, unless `OMNIGENT_E2E_OPENCODE_NATIVE=1` is set.
- `cd web && npx vitest run src/lib/nativeCodingAgents.test.ts`. Expected: PASS. If that file does not exist, run `npx tsc -p web --noEmit` instead.
- `pre-commit run --files web/src/lib/nativeCodingAgents.ts omnigent/cli_native.py omnigent/harnesses/opencode_native/main.py omnigent/onboarding/harness_install.py tests/e2e/test_host_opencode_native_e2e.py tests/e2e/test_opencode_native_wire_contract_e2e.py`. Expected: all hooks Passed.

- [ ] **Step 5: Commit**

```bash
git add web/src/lib/nativeCodingAgents.ts omnigent/cli_native.py omnigent/harnesses/opencode_native/main.py omnigent/onboarding/harness_install.py tests/e2e/test_host_opencode_native_e2e.py tests/e2e/test_opencode_native_wire_contract_e2e.py
git commit -m "docs(opencode-native): replace opencode attach and 1.x references with v2 wording"
```

---

### Task 87: Add the in-repo `docs/opencode-native.md` page

**Files:**
- Create: `docs/opencode-native.md`

**Interfaces:**
- Consumes: the behavior shipped in Stages 1-4. The page describes these and invents nothing else:
  - `opencode serve --stdio` per conversation
  - `opencode --server <url> --session <id> <workspace>`
  - `OPENCODE_PASSWORD`
  - config keys `providers` / `permissions` / `mcp.servers` / `plugins` / `instructions`
  - `set_model`, `/compact`, forms, and `permission.asked`
- Produces: a doc page that Task 88 links to from `README.md`.

- [ ] **Step 1: Write the failing check**

Run: `test -f docs/opencode-native.md`

- [ ] **Step 2: Confirm the check fails**

Expected: exit code 1 (file absent).

- [ ] **Step 3: Write the page**

Create `docs/opencode-native.md` with exactly this content:

````markdown
# The `opencode-native` harness

`omnigent opencode` runs the real **OpenCode** TUI in a runner-owned tmux pane
and mirrors it into an Omnigent conversation. The runner starts one private
`opencode serve` per conversation. An SSE forwarder mirrors OpenCode's events
into the session, and web turns are injected over loopback HTTP.

| Harness id | Aliases | Integration |
|---|---|---|
| `opencode-native` | `opencode`, `native-opencode` | Native server: `opencode serve` + `opencode --server` TUI |

## Supported version

OpenCode **2.0.x** (`>=2.0.0,<3.0.0`), published on npm as `@opencode/cli`.
OpenCode 1.x (`opencode-ai`) is no longer supported. A host running it reports
the harness as *outdated* until it is upgraded.
`OMNIGENT_OPENCODE_SKIP_VERSION_CHECK=1` bypasses the gate at your own risk.

## Install and sign in

    npm rm -g opencode-ai            # only if OpenCode 1.x is installed
    npm i -g @opencode/cli@~2.0.18
    opencode auth login
    omnigent opencode

`omni setup` → OpenCode does the same install and shows the login step. The web
UI's setup dialog shows the same checklist for a remote host.

Omnigent stores no OpenCode credential. At launch, the runner copies your
`auth.json` into the conversation's private data directory, and OpenCode 2.x
imports it once into that directory's database. If you only have OpenCode 2.x
credentials and no `auth.json`, the runner connects provider API keys from the
environment (for example `ANTHROPIC_API_KEY`) through OpenCode's integration API.
If neither is present, the host shows the `opencode auth login` hint.

To use a non-PATH binary, set `OMNIGENT_OPENCODE_PATH` or
`harness.opencode-native.command` in the Omnigent config.

## Agent YAML

```yaml
spec_version: 1
name: my-opencode
description: OpenCode with Omnigent policies and tools.
executor:
  type: omnigent
  config:
    harness: opencode-native
    model: anthropic/<model-id>   # optional; OpenCode's provider/model form
prompt: |
  You are a careful coding agent. Keep changes scoped to the task.
```

- **Model override:** a model id is OpenCode's `provider/model`, for example
  `anthropic/<model-id>` or `openai/<model-id>`. When the agent is bound to the
  Omnigent or Databricks gateway, give a bare endpoint name and Omnigent
  qualifies it as `<gateway-provider>/<endpoint>`. `omnigent opencode --model`
  and `omni setup` → OpenCode → "Set default model" (`opencode_model`) set the
  same value. `/model <id>` in the web composer switches the running session.
- **Instructions:** the agent's `prompt` / `instructions`, plus Omnigent's
  framework instructions, are passed to OpenCode once per launch through the
  config `instructions` key.

## What is mirrored

| OpenCode | Omnigent |
|---|---|
| Text and reasoning (`session.text.delta`, `session.reasoning.delta`) | Live streaming in the chat |
| Tool calls (`shell`, `edit`, `subagent`, MCP tools) | Tool cards with output |
| Permission requests (`permission.asked`) | Omnigent policies first; ASK becomes a web approval card. First answer wins between the web card and the TUI |
| Question forms (`form.created`) | Web question card (single/multi select, boolean, number, text) |
| Compaction (`session.compaction.*`, `/compact`) | Compaction status in the chat |
| Step cost and tokens (`session.step.ended`, `session.usage.updated`) | Session cost and usage |
| Model changes (TUI picker or `/model`) | Model badge |
| Child sessions (`subagent`) | Sub-agent sessions |

Every tool call asks: the runner writes a `permissions` rule
`{action: "*", resource: "*", effect: "ask"}` into the per-conversation config
and never passes `--auto`. This routes every call through the Omnigent policy
engine. Omnigent's own MCP tools are served through a relay entry with Code Mode
off, so each relay tool keeps its name and is gated on its own.

Images attached in the web UI are sent as `data:` URIs. Other attachments are
flattened to text. A message sent during a running turn is delivered as a
*steer*. Queued messages use OpenCode's own queue.

## One server per conversation

Each conversation gets its own `opencode serve --stdio` process, its own
password (`OPENCODE_PASSWORD`) and its own data directory under
`~/.omnigent/opencode-native/`. Model, gateway, MCP servers, the policy plugin
and the policy plugin's session binding are all per process, and credentials and
history stay isolated. Resume reattaches to the same data directory on the same
host. A resume on a different host, or of a lost session, starts a new OpenCode
session seeded with the Omnigent transcript. Fork replays history as a text
preamble.

## Known limits

- **Policy token snapshot.** The policy plugin gets its Omnigent credentials
  when the server launches. A session that outlives that token's expiry fails
  open on the request/tool-result phases until it is relaunched. Tool-call
  approvals are unaffected, because they go through `permission.asked`.
- **No shared server.** OpenCode's background `--service` server, pairing, and
  one-server-for-many-sessions are not used.
- **No Direct mode.** `opencode acp` is not wired as an ACP harness.
- **Session import** reads OpenCode 2.x sessions through a short-lived
  `opencode serve`. There is no `opencode export`.

## Troubleshooting

- *Harness is outdated*: install `@opencode/cli@~2.0.18` as shown above.
- *Needs auth / auth-shaped turn error*: run `opencode auth login` on the host.
- Diagnostics: see [harness-diagnostics.md](harness-diagnostics.md). The
  per-conversation bridge directory holds `opencode.json`, the policy plugin, and
  server logs.
````

- [ ] **Step 4: Run the checks**

Run: `pre-commit run --files docs/opencode-native.md`

Expected: `trailing-whitespace`, `end-of-file-fixer` and `mixed-line-ending` all Passed. Every other hook shows `(no files to check)Skipped`.

Then cross-check every claim against the code:
- `git grep -n "OPENCODE_PASSWORD\|codemode\|\"effect\": \"ask\"\|--stdio" omnigent/harnesses/opencode_native/` should hit each mechanism named on the page.
- If any claim doesn't match what Stages 1-4 shipped (for example, Stage 0 found `instructions` is not applied per session and a `synthetic` message is used instead), edit that sentence to match the code before committing.

- [ ] **Step 5: Commit**

```bash
git add docs/opencode-native.md
git commit -m "docs(opencode-native): add OpenCode 2.x harness page"
```

---

### Task 88: Link the page and update the queue/steer matrix

**Files:**
- Modify: `README.md:293-294` (after the `omnigent agy` note)
- Modify: `docs/QUEUE_STEER_DESIGN.md:86` and `:94-96`

**Interfaces:**
- Consumes: `docs/opencode-native.md` (Task 87); `delivery: "steer" | "queue"` (Stage 1).
- Produces: nothing callable.

- [ ] **Step 1: Write the failing check**

Run: `git grep -n "opencode-native.md" README.md ; git grep -n "no live-steer endpoint" docs/QUEUE_STEER_DESIGN.md`

- [ ] **Step 2: Confirm the check fails**

Expected: the first grep prints nothing. The second prints lines 86 and 95, the stale v1 claims.

- [ ] **Step 3: Edit**

**README.md.** After the existing lines 293-294:

```markdown
`omnigent agy` requires agy 1.1.13 or newer. When `GEMINI_API_KEY` is set,
direct Gemini API authentication takes precedence over agy's saved OAuth login.
```

insert a blank line and then:

```markdown
`omnigent opencode` requires OpenCode 2.0.x (`npm i -g @opencode/cli@~2.0.18`,
then `opencode auth login`). See the [OpenCode harness guide](docs/opencode-native.md).
```

**docs/QUEUE_STEER_DESIGN.md.** Replace line 86:

```markdown
| opencode-native | HTTP prompt (`supports_enqueue=True`); the native server has **no live-steer endpoint** → admitted as a new prompt, promoted by the server's own queue at turn end | ❌ next turn (code-confirmed) |
```

with:

```markdown
| opencode-native | HTTP prompt with `delivery:"steer"` for a live turn, `delivery:"queue"` for enqueued input (`supports_enqueue=True`) | ⚠️ app-defined — mechanism confirmed in code, not yet verified live |
```

Replace lines 94-96:

```markdown
> steer per harness to upgrade the ⚠️ rows. opencode-native is settled: its app
> server exposes no live-steer endpoint, so the steered message is always
> promoted at the next turn boundary.
```

with:

```markdown
> steer per harness to upgrade the ⚠️ rows.
```

(If the Task 91 manual run shows the steer folding into the running turn, change that row's last cell to `✅ verified (OpenCode 2.0.18)` in the same PR.)

- [ ] **Step 4: Re-run the check**

Run: `git grep -n "opencode-native.md" README.md`. Expected: one match.

Run: `git grep -n "no live-steer endpoint" docs/QUEUE_STEER_DESIGN.md`. Expected: no output.

Then run: `pre-commit run --files README.md docs/QUEUE_STEER_DESIGN.md`. Expected: Passed/Skipped.

- [ ] **Step 5: Commit**

```bash
git add README.md docs/QUEUE_STEER_DESIGN.md
git commit -m "docs(opencode-native): link OpenCode 2.x guide and update steer matrix"
```

---

### Task 89: Mark the v1 gap docs as superseded

**Files:**
- Modify: `designs/opencode-native-gaps.md:1-3`
- Modify: `designs/opencode-native-gaps-qa.md:1` (it is a 1.17.7 QA script; same header)

**Interfaces:**
- Consumes: spec path `designs/opencode-v2-native-harness.md`.
- Produces: nothing callable.

- [ ] **Step 1: Write the failing check**

Run: `git grep -n "Superseded" designs/opencode-native-gaps.md designs/opencode-native-gaps-qa.md`

- [ ] **Step 2: Confirm the check fails**

Expected: no output.

- [ ] **Step 3: Edit**

In `designs/opencode-native-gaps.md`, lines 1-3 are currently:

```markdown
# OpenCode-native: feature-gap closure plan

**Status:** implemented (single PR) · **Owner:** Dhruv Gupta · **Harness:** `opencode-native`
```

Replace them with:

```markdown
# OpenCode-native: feature-gap closure plan

> **Superseded.** This plan and its recon target OpenCode 1.17/1.18, which is no
> longer supported. The current design is the OpenCode v2 spec,
> [`designs/opencode-v2-native-harness.md`](../designs/opencode-v2-native-harness.md);
> the user guide is [`docs/opencode-native.md`](../docs/opencode-native.md).

**Status:** implemented (single PR) · **Owner:** Dhruv Gupta · **Harness:** `opencode-native`
```

In `designs/opencode-native-gaps-qa.md`, insert the following directly after line 1 (the `# ...` title), with a blank line before it:

```markdown
> **Superseded.** These checks target OpenCode 1.17.7. For OpenCode 2.x, use the
> manual checklist in the v2 spec's implementation plan and
> [`docs/opencode-native.md`](../docs/opencode-native.md).
```

- [ ] **Step 4: Re-run the check**

Run: `git grep -n "Superseded" designs/opencode-native-gaps.md designs/opencode-native-gaps-qa.md`. Expected: one match per file.

Then run: `pre-commit run --files designs/opencode-native-gaps.md designs/opencode-native-gaps-qa.md`. Expected: Passed/Skipped.

- [ ] **Step 5: Commit**

```bash
git add designs/opencode-native-gaps.md designs/opencode-native-gaps-qa.md
git commit -m "docs(opencode-native): mark v1 gap plan as superseded by the v2 spec"
```

---

### Task 90: Changelog line and docs-site follow-up

`CHANGELOG.md` is generated at release-cut from each PR's `## Changelog` section (file header, lines 3-6), and it has no `Unreleased` section. **Do not edit `CHANGELOG.md`.** The entry goes in the PR body, and `draft-release-notes.yml` renders it as `- [Feature / Breaking] <line> (#NNNN)`, matching existing entries such as the `devin` cutover at the v0.15.0 section.

**Files:**
- Modify: the Stage 5 PR description (`.github/pull_request_template.md` sections). There are no repo files.

**Interfaces:**
- Consumes: the PR opened for this stage (or the top of the stack).
- Produces: a PR body whose `## Changelog` section contains the line below, with the `Feature` and `Breaking change` boxes checked under *Type of change*.

- [ ] **Step 1: Write the failing check**

Run: `gh pr view --json body -q .body | grep -c "@opencode/cli"`

- [ ] **Step 2: Confirm the check fails**

Expected: `0`. If no PR is open yet, `gh` errors and this step still counts as failing.

- [ ] **Step 3: Fill in the PR body**

Open or edit the PR with the full template. Keep every section and every checkbox row, as `CLAUDE.md` requires. Check **Feature**, **Breaking change** and **Docs** under *Type of change*, and **UI / frontend change** (Task 85 changes user-visible copy). Put exactly this under `## Changelog`:

```markdown
`omnigent opencode` now runs on OpenCode 2.0.x (`npm i -g @opencode/cli@~2.0.18`), with live text/reasoning streaming, question forms, native `/compact` and mid-turn steering; OpenCode 1.x (`opencode-ai`) is no longer supported and hosts running it show the harness as outdated.
```

Use `gh pr edit --body-file <file>` with the complete filled template. Do not pass a `--body` that skips sections.

Under **Coverage notes**, paste the Task 91 checklist results.

Under **Summary**, add one sentence: "Follow-up: mirror `docs/opencode-native.md` into the omnigent.ai configuration page (`omnigent-ai/omnigent-site`, `/docs/build/harnesses/configuration`), because that site is not sourced from this repo."

- [ ] **Step 4: Re-run the check**

Run: `gh pr view --json body -q .body | grep -c "@opencode/cli"`

Expected: `1` or more.

- [ ] **Step 5: Commit**

Nothing to commit, because this task only edits the PR description. Confirm the working tree is clean: `git status --short` should print nothing.

---

### Task 91: Manual full-stack verification (human, no code)

Do this on the top of the stack after Tasks 80-87, on a machine with `opencode --version` → `opencode v2.0.18` and working model credentials (`opencode auth login` done, or `ANTHROPIC_API_KEY` exported). Record pass/fail for each box in the PR's **Coverage notes**. If any capability claimed in Task 83 fails here (steering, images or compaction), set that field back to `None` in `omnigent/harness_plugins.py` and in the Task 83 test before merging.

**Setup**
- [ ] `uv sync --extra all --group dev`
- [ ] `opencode --version` prints `opencode v2.0.18`. `npm ls -g opencode-ai` shows nothing.
- [ ] Start the stack. Either `just dev` (runs `omnidev`; open the `ui` URL it prints), or use three terminals: `uv run omnigent server`, then `uv run omnigent host --server http://localhost:6767`, then `cd web && pnpm run dev` (open `http://localhost:5173/`).

**Setup UX**
- [ ] New chat → agent picker → **OpenCode** shows no "needs setup" / "outdated" badge.
- [ ] Optional negative check: temporarily put an `opencode` 1.18 binary first on the host's `PATH` and restart the host. The OpenCode row shows **outdated**, and the notice reads "…needs OpenCode 2.0 on <host> — run `npm i -g @opencode/cli@~2.0.18` (after `npm rm -g opencode-ai`) or `omni setup`…". Restore `PATH` afterwards.

**Start an OpenCode session**
- [ ] Pick **OpenCode**, pick a workspace directory, send `Say hello in one sentence.`
- [ ] The assistant reply **streams**: text grows word by word rather than appearing all at once. The terminal panel shows the OpenCode TUI attached to the same session. `ps aux | grep "opencode --server"` shows `--server http://127.0.0.1:<port> --session ses_…`, and no `attach` argv.

**Tool approval**
- [ ] Send `Run the shell command: ls -la and summarize the output.`
- [ ] A web approval card appears for the `shell` tool. Click **Approve**. The tool card shows the `ls` output, and the TUI shows the permission as resolved (no second prompt).
- [ ] Repeat and click **Deny**. The turn continues with the tool rejected, and no output is produced.

**Question form**
- [ ] Send `Before you do anything, ask me whether I prefer tabs or spaces using your question tool, then tell me what I picked.`
- [ ] A web question card appears with the options. Pick one. The assistant's next message names that choice, and the TUI's form closes.

**Model switch**
- [ ] Type `/model` in the composer and pick a different model (or `/model <provider>/<model-id>`).
- [ ] Send `Which model are you?` The model badge shows the new model, and the TUI header matches it.

**Compaction**
- [ ] Type `/compact`. A compaction status appears and completes without error. The next prompt still answers with context (for example `What did I pick earlier, tabs or spaces?`).

**Streaming deltas + reasoning**
- [ ] With a reasoning-capable model, send `Think step by step: what is 17*23?` The reasoning block streams, and then the answer streams.

**Steer / queue**
- [ ] Send a long task (`Write a 40-line poem about rivers.`). While it is still streaming, send `Make the last line rhyme with "sea".` The follow-up is delivered, either folded into the running turn or run next, with no error card. Note which one happens (this feeds the Task 88 matrix row).

**Image input**
- [ ] Attach a small PNG screenshot and ask `What is in this image?` The reply describes the image.

**Cost**
- [ ] After the turns above, the session's cost/usage badge (the agent info panel) shows a non-zero cost and token count. The number increases after one more turn.

**Resume after reload**
- [ ] Reload the browser tab. The transcript, including tool cards and the question card result, is intact, and the terminal re-attaches to the same TUI.
- [ ] Send `What command did you run earlier?` The answer references `ls -la`.
- [ ] Stop the host process and start it again with the same command. Open the session and send a message. It resumes (same OpenCode session id in the TUI, or a fresh one seeded with the transcript) and answers with context.

**Fork**
- [ ] Hover an earlier assistant message → **Fork from here** → confirm. The fork opens as a new OpenCode session. Send `Summarize our conversation so far.` It knows about the turns up to the fork point and nothing after.

**Interrupt**
- [ ] Start a long turn and press the web **Stop** button. The turn stops and the session returns to idle. The next prompt works.

**Automated e2e (run and paste the output in the PR)**
- [ ] Wire contract, which needs no credentials:
  `OMNIGENT_E2E_OPENCODE_NATIVE=1 uv run pytest tests/e2e/test_opencode_native_wire_contract_e2e.py -v`. Expected: PASS, not SKIPPED.
- [ ] Full stack, which needs credentials:

      OMNIGENT_E2E_OPENCODE_NATIVE=1 \
      HOME=/tmp/omni-isolated DATABRICKS_CONFIG_FILE=$HOME/.databrickscfg \
      uv run pytest tests/e2e/test_host_opencode_native_e2e.py \
          --profile <your-profile> \
          --llm-api-key "$(databricks auth token -p <your-profile> \
              | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \
          -v

  (Set `DATABRICKS_CONFIG_FILE` to your real `~/.databrickscfg` path before overriding `HOME`.) Expected: PASS.
- [ ] Unit suites: `uv run pytest tests/test_opencode_native_*.py tests/test_opencode_http_transport.py tests/test_harness_capabilities.py -q` and `cd web && npx vitest run src/lib/harnessSetup.test.ts src/shell/NewChatDialog.test.tsx`. Expected: all PASS.
- [ ] `pre-commit run --all-files`. Expected: all Passed/Skipped.
