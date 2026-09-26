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
provider; there is no mock path). The isolated recon server has no
credentials of its own, so either export the provider's env var
(``ANTHROPIC_API_KEY``, ...) or point ``--seed-credentials-from`` at your
real v2 ``opencode`` SQLite store to copy one stored key across. Run:

    uv run python dev/opencode_v2_recon.py --model anthropic/claude-sonnet-4-5 \\
        --seed-credentials-from ~/.local/share/opencode/opencode.db

The generated server password, every temp path, the user's home directory,
any provider credentials found in the environment, and any credential seeded
from ``--seed-credentials-from`` are redacted from the fixtures before
they're written. The server's stdout/stderr are logged to
``server.stdout.log``/``server.stderr.log`` in the temp workdir (kept only
with ``--keep-workdir``) for debugging; a failed run also dumps everything
captured so far to ``events-debug.ndjson`` there.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import itertools
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from collections.abc import Mapping
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

# A configured plugin path that resolves to a *file* is silently dropped
# (v2 logs "configured plugin path must be a directory" and skips it, per
# packages/core/src/config/plugin/source.ts's `scan`), so the recon plugin
# is written as a directory with its own package.json + entrypoint, per the
# resolution in packages/plugin/src/host.ts's `resolve`.
_PLUGIN_PACKAGE_JSON: dict[str, str] = {
    "name": "omnigent-recon-plugin",
    "type": "module",
    "main": "server.js",
}

# The module loader (packages/core/src/plugin/module.ts's `Module` schema)
# only requires a default export shaped `{id, setup}` (or `{id, effect}`);
# `Plugin.define` is the identity on that shape, and a bare directory path
# can't resolve the `@opencode/plugin` package, so this plugs a plain object
# in directly. Logs the three hooks the omnigent-policy plugin (Stage 3)
# will use: ctx.session.hook("prompt"), ctx.tool.hook("execute.after"), and
# ctx.permission.hook("evaluate") (packages/plugin/src/promise/{session,tool,permission}.ts).
_PLUGIN_SERVER_TEMPLATE = """\
import { appendFileSync } from "node:fs"

const LOG_FILE = process.env.OMNIGENT_RECON_PLUGIN_LOG

function log(event, data) {
  if (!LOG_FILE) return
  appendFileSync(LOG_FILE, JSON.stringify({ event, data }) + "\\n")
}

export default {
  id: "omnigent-recon",
  setup: async (ctx) => {
    await ctx.session.hook("prompt", async (event) => {
      log("session.prompt", { sessionID: event.sessionID, messageID: event.messageID })
    })
    await ctx.tool.hook("execute.after", async (event) => {
      log("tool.execute.after", {
        tool: event.tool, sessionID: event.sessionID, status: event.status
      })
    })
    await ctx.permission.hook("evaluate", async (event) => {
      log("permission.evaluate", {
        sessionID: event.sessionID, action: event.action, resources: event.resources
      })
    })
  },
}
"""


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
        help="provider/model to prompt, e.g. <provider>/<model-id> (see module docstring)",
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
    parser.add_argument(
        "--seed-credentials-from",
        type=Path,
        default=None,
        help=(
            "Path to a v2 opencode SQLite store (its `credential` table) to read a "
            "stored provider API key from and seed into the isolated recon server."
        ),
    )
    parser.add_argument(
        "--seed-provider",
        default=None,
        help="Integration id to seed a credential for (default: the provider half of --model).",
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


_CREDENTIAL_ENV_NAME_SUFFIXES = ("_API_KEY", "_TOKEN", "_SECRET")


def credential_values_from_env(env: Mapping[str, str]) -> list[str]:
    """
    Collect env var values that look like provider credentials, for redaction.

    Matches names ending in ``_API_KEY``, ``_TOKEN``, or ``_SECRET``, or
    containing ``PASSWORD`` — the conventions real provider env vars follow
    (``ANTHROPIC_API_KEY``, ``GITHUB_TOKEN``, ``OPENCODE_PASSWORD``, ...).

    :param env: Environment mapping to scan, e.g. ``os.environ``.
    :returns: Every non-empty matching value, so callers can add them to a
        secret-redaction list.
    """
    return [
        value
        for name, value in env.items()
        if value and (name.endswith(_CREDENTIAL_ENV_NAME_SUFFIXES) or "PASSWORD" in name)
    ]


def read_stored_key(db_path: Path, provider: str) -> str | None:
    """
    Read the newest active key-type credential for *provider* from a v2 SQLite store.

    v2 stores provider credentials in the SQLite ``credential`` table, not
    env vars (``packages/core/src/credential/sql.ts``): columns ``id``,
    ``integration_id``, ``label``, ``value`` (JSON), ``connector_id``,
    ``method_id``, ``active``, ``time_created``, ``time_updated``. ``value``
    is a tagged union (``packages/schema/src/credential.ts``): either
    ``{type: "key", key, ...}`` or ``{type: "oauth", ...}`` — an oauth
    credential can't be seeded as a static key, so it's skipped.

    :param db_path: Path to the v2 ``opencode`` SQLite database, opened read-only.
    :param provider: Integration id to look up (e.g. ``"anthropic"``).
    :returns: The stored API key, or ``None`` if there's no usable key credential.
    """
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT value FROM credential WHERE integration_id = ? "
            "ORDER BY active DESC, time_updated DESC LIMIT 1",
            (provider,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    value = json.loads(row[0])
    if value.get("type") == "oauth":
        print(
            f"stored credential for {provider!r} is oauth, not a static key; skipping seed",
            file=sys.stderr,
        )
        return None
    return value.get("key") if value.get("type") == "key" else None


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
    :param plugin_path: Path to the recon test plugin's directory (a plain
        ``{id, setup}`` default export, not a single-file path).
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


def form_answer_for(fields: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Build a ``{field_key: value}`` answer picking the first option per field.

    Each value must satisfy v2's ``Form.Value`` union — ``string | number |
    boolean | string[]`` (``packages/schema/src/form.ts``) — not the raw
    option object. A ``string``/``multiselect`` field's ``options`` are
    ``{label, value, description?}`` objects (confirmed against a captured
    ``form.created`` event), so the *value* to send is
    ``option["value"]``, not the option itself; a bare string option (or no
    options at all) is handled defensively too.

    :param fields: The ``form.created`` event's ``data.form.fields`` array.
    :returns: One valid ``Form.Value`` per field key, ready for
        ``POST /api/session/{id}/form/{formId}/reply``'s ``answer``.
    """

    def option_value(field: dict[str, Any]) -> Any:
        options = field.get("options") or []
        if not options:
            return "A"
        first = options[0]
        return first["value"] if isinstance(first, dict) else first

    answer: dict[str, Any] = {}
    for field in fields:
        field_type = field.get("type")
        if field_type == "boolean":
            answer[field["key"]] = True
        elif field_type in ("number", "integer"):
            answer[field["key"]] = 1
        elif field_type == "multiselect":
            answer[field["key"]] = [option_value(field)]
        elif field_type == "external":
            answer[field["key"]] = True
        else:
            answer[field["key"]] = option_value(field)
    return answer


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
                with contextlib.suppress(json.JSONDecodeError):
                    out.append(json.loads("".join(buffer)))
                buffer = []


_TERMINAL_EXECUTION_EVENT_TYPES = frozenset(
    {
        "session.execution.succeeded",
        "session.execution.failed",
        "session.execution.interrupted",
    }
)


def is_terminal_session_event(event: dict[str, Any], session_id: str) -> bool:
    """
    True if *event* signals that session *session_id* has left the running state.

    Per v2's schema, a completed turn is signalled by one of: the live-only
    ``session.status`` event whose ``status.type`` is ``"idle"`` (as opposed
    to ``"busy"``/``"retry"``, ``session-event.ts`` ``Status``); one of the
    durable ``session.execution.{succeeded,failed,interrupted}`` events
    (``session-event.ts`` ``Execution``); or the deprecated ``session.idle``
    event, kept for older clients, which carries only ``sessionID``
    (``session-status-event.ts`` ``Idle``). Any of these ends the "still
    running" window this recon script waits out.

    :param event: One decoded ``/api/event`` frame.
    :param session_id: The session this recon run is driving.
    :returns: Whether *event* is a terminal signal for *session_id*.
    """
    data = event.get("data", {})
    if data.get("sessionID") != session_id:
        return False
    event_type = event.get("type")
    if event_type == "session.idle" or event_type in _TERMINAL_EXECUTION_EVENT_TYPES:
        return True
    if event_type == "session.status":
        status = data.get("status")
        if status == "idle":
            return True
        if isinstance(status, dict) and status.get("type") == "idle":
            return True
    return False


_DRIVE_PROMPTS_TIMEOUT_SECONDS = 180.0  # 120s auto-answer + 60s terminal-wait budgets, combined


async def _drive_prompts_until_terminal(
    client: httpx.AsyncClient,
    session_id: str,
    events: list[dict[str, Any]],
    findings: dict[str, str],
    timeout: float = _DRIVE_PROMPTS_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """
    Answer every permission/form prompt as it appears, until *session_id* goes terminal.

    A single combined loop (rather than answer-the-first-of-each, then
    separately wait for idle) is required because a model can raise more
    than one permission ask or form in one turn: the earlier two-phase
    version stopped answering after the first of each, so a second
    ``permission.asked`` for the same turn was never replied to and the
    session never left "running".

    :param timeout: Overall budget for both answering prompts and reaching
        a terminal event.
    :returns: The terminal event that ended the session, or ``None`` if
        *timeout* elapses first.
    """
    answered_permission_ids: set[str] = set()
    answered_form_ids: set[str] = set()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for event in list(events):
            if event.get("type") == "permission.asked":
                request_id = event["data"]["id"]
                if request_id in answered_permission_ids:
                    continue
                reply = await client.post(
                    f"/api/session/{session_id}/permission/{request_id}/reply",
                    json={"decision": "once"},
                )
                findings["permission_reply_status"] = str(reply.status_code)
                if reply.status_code < 300:
                    answered_permission_ids.add(request_id)
                else:
                    print(
                        f"permission reply failed: {reply.status_code} {reply.text}",
                        file=sys.stderr,
                    )
            if event.get("type") == "form.created":
                form = event["data"]["form"]
                form_id = form["id"]
                if form_id in answered_form_ids:
                    continue
                answer = form_answer_for(form.get("fields", []))
                reply = await client.post(
                    f"/api/session/{session_id}/form/{form_id}/reply",
                    json={"answer": answer},
                )
                findings["form_reply_status"] = str(reply.status_code)
                if reply.status_code < 300:
                    answered_form_ids.add(form_id)
                else:
                    print(
                        f"form reply failed: {reply.status_code} {reply.text}",
                        file=sys.stderr,
                    )

        for event in events:
            if is_terminal_session_event(event, session_id):
                findings["permissions_answered"] = str(len(answered_permission_ids))
                findings["forms_answered"] = str(len(answered_form_ids))
                return event

        await asyncio.sleep(0.5)

    findings["permissions_answered"] = str(len(answered_permission_ids))
    findings["forms_answered"] = str(len(answered_form_ids))
    return None


async def _wait_for_integration_loaded(
    client: httpx.AsyncClient, provider: str, timeout: float = 30.0, interval: float = 0.5
) -> bool:
    """
    Poll ``GET /api/integration/{provider}`` until it returns 200.

    The server loads its integration catalog asynchronously after startup;
    posting a credential (``POST /api/integration/{id}/connect/key``) before
    that catalog is populated 404s with integration-not-found even though
    the provider is valid, because ``service.get(id)`` is still empty. A 404
    here just means "not loaded yet" and is retried; any other non-200
    status is treated as a real error and stops the wait immediately.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.get(f"/api/integration/{provider}")
        if resp.status_code == 200:
            return True
        if resp.status_code != 404:
            return False
        await asyncio.sleep(interval)
    return False


def progress_has_incremental_output(events: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """
    Detect whether ``session.tool.progress`` metadata grows incrementally per tool call.

    Per v2's schema (``session-event.ts``'s ``Tool.Progress``), each
    progress event carries ``{..., id (the tool call id), metadata:
    Record<string, Json>}`` and is a live replacement of the previous
    metadata for that same tool call as it runs. "Incremental" means some
    string-valued metadata field is a growing prefix across consecutive
    progress events for the same tool call id — the streaming-output
    pattern a UI would render live, as opposed to metadata that's static or
    replaced wholesale each time.

    :param events: Captured ``/api/event`` frames (any type; non-progress
        events are ignored).
    :returns: ``(has_incremental_output, metadata_keys_seen)`` — whether any
        field grew this way, and the sorted distinct metadata key names
        observed across every progress event.
    """
    by_tool_call: dict[str, list[dict[str, Any]]] = {}
    keys_seen: set[str] = set()
    for event in events:
        if event.get("type") != "session.tool.progress":
            continue
        data = event.get("data", {})
        metadata = data.get("metadata") or {}
        keys_seen.update(metadata.keys())
        tool_call_id = data.get("id")
        if tool_call_id is None:
            continue
        by_tool_call.setdefault(tool_call_id, []).append(metadata)

    has_incremental = any(
        isinstance(previous_value, str)
        and isinstance(current.get(key), str)
        and current[key] != previous_value
        and current[key].startswith(previous_value)
        for metadata_sequence in by_tool_call.values()
        for previous, current in itertools.pairwise(metadata_sequence)
        for key, previous_value in previous.items()
    )
    return has_incremental, sorted(keys_seen)


def _fill_recon_findings(
    events: list[dict[str, Any]], messages: dict[str, Any], findings: dict[str, str]
) -> None:
    """Answer the six open design-spec questions (spec section 'Open items') from captured data."""
    instructions_seen = any(
        _INSTRUCTIONS_MARKER in json.dumps(event.get("data", {})) for event in events
    ) or _INSTRUCTIONS_MARKER in json.dumps(messages)
    findings["1_instructions_applies_system_prompt"] = "yes" if instructions_seen else "no"

    progress_events = [e for e in events if e.get("type") == "session.tool.progress"]
    if not progress_events:
        findings["3_tool_progress_has_incremental_output"] = (
            "no data captured (no session.tool.progress events seen)"
        )
    else:
        has_incremental, metadata_keys = progress_has_incremental_output(events)
        keys_text = ", ".join(metadata_keys) if metadata_keys else "(none)"
        findings["3_tool_progress_has_incremental_output"] = (
            f"{'yes' if has_incremental else 'no'} (metadata keys seen: {keys_text})"
        )

    # An MCP tool's permission action is namespaced `{mcp_server_name}_{tool}`
    # (e.g. `recon-echo_echo`), so its `permission.asked` event is itself
    # proof that a codemode:false MCP call raised one.
    mcp_permission_asks = [
        e
        for e in events
        if e.get("type") == "permission.asked"
        and str(e.get("data", {}).get("action", "")).startswith("recon-echo_")
    ]
    findings["4_codemode_false_mcp_raises_permission_asked"] = (
        f"yes (action: {mcp_permission_asks[0]['data']['action']})"
        if mcp_permission_asks
        else "no"
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
    (out_dir / "events.ndjson").write_text(
        events_text + ("\n" if events_text else ""), encoding="utf-8"
    )

    messages_text = redact_secrets(
        json.dumps(messages, indent=2, sort_keys=True), secrets_to_redact
    )
    (out_dir / "messages.json").write_text(messages_text + "\n", encoding="utf-8")

    lines = ["# OpenCode v2 recon findings", ""]
    for key, value in findings.items():
        lines.append(f"- **{key}**: {redact_secrets(value, secrets_to_redact)}")
    (out_dir / "recon-findings.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


_TURN_TIMEOUT_SECONDS = 600.0


def _drain_pipe_to_file(pipe: Any, log_path: Path) -> None:
    """
    Continuously copy *pipe* to *log_path* until EOF.

    Runs in a daemon thread once the ``{url}`` line has been read from
    ``proc.stdout``, so a chatty server can't fill the OS pipe buffer and
    deadlock the run once nothing else is reading from it.
    """
    try:
        with log_path.open("wb") as fh:
            for chunk in iter(lambda: pipe.read(4096), b""):
                fh.write(chunk)
    except (ValueError, OSError):
        pass


def _write_debug_events(workdir: Path, events: list[dict[str, Any]]) -> None:
    """Dump captured events to ``<workdir>/events-debug.ndjson`` so a failed run is diagnosable."""
    debug_path = workdir / "events-debug.ndjson"
    text = "\n".join(json.dumps(event, sort_keys=True) for event in events)
    debug_path.write_text(text + ("\n" if text else ""), encoding="utf-8")

    type_counts: dict[str, int] = {}
    for event in events:
        event_type = str(event.get("type", "<unknown>"))
        type_counts[event_type] = type_counts.get(event_type, 0) + 1
    summary = ", ".join(f"{name}={count}" for name, count in sorted(type_counts.items()))

    print(f"wrote {len(events)} captured events to {debug_path}", file=sys.stderr)
    print(f"event types seen: {summary or '(none)'}", file=sys.stderr)


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
    plugin_path = workdir / "omnigent-recon-plugin"
    plugin_path.mkdir(parents=True, exist_ok=True)
    (plugin_path / "package.json").write_text(
        json.dumps(_PLUGIN_PACKAGE_JSON, indent=2), encoding="utf-8"
    )
    (plugin_path / "server.js").write_text(_PLUGIN_SERVER_TEMPLATE, encoding="utf-8")
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
    secrets_to_redact = [password, str(workdir), str(Path.home())]
    secrets_to_redact.extend(credential_values_from_env(env))
    findings: dict[str, str] = {}
    events: list[dict[str, Any]] = []

    provider_id, model_id = args.model.split("/", 1)
    seed_provider = args.seed_provider or provider_id

    stderr_log_path = workdir / "server.stderr.log"
    stdout_log_path = workdir / "server.stdout.log"
    stderr_fh = stderr_log_path.open("wb")

    async def _drive_turn() -> tuple[dict[str, Any], dict[str, Any]] | None:
        """Run the whole post-URL turn; returns ``None`` if the session never idles."""
        auth = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        async with httpx.AsyncClient(base_url=base_url, headers=headers, timeout=60.0) as client:
            info_resp = await client.get("/api/info")
            info_resp.raise_for_status()
            print("server info:", info_resp.json())

            if args.seed_credentials_from:
                key = read_stored_key(args.seed_credentials_from, seed_provider)
                if key is None:
                    findings["credential_seed_status"] = "no key credential found"
                else:
                    secrets_to_redact.append(key)
                    catalog_loaded = await _wait_for_integration_loaded(client, seed_provider)
                    if not catalog_loaded:
                        findings["credential_seed_status"] = (
                            f"integration {seed_provider} never became available"
                        )
                        print(
                            f"integration {seed_provider} never became available; "
                            "aborting without writing fixtures",
                            file=sys.stderr,
                        )
                        _write_debug_events(workdir, events)
                        return None
                    connect_resp = await client.post(
                        f"/api/integration/{seed_provider}/connect/key",
                        json={"key": key},
                    )
                    findings["credential_seed_status"] = str(connect_resp.status_code)
                    if not (200 <= connect_resp.status_code < 300):
                        redacted_body = redact_secrets(connect_resp.text, secrets_to_redact)
                        print(
                            f"credential seed failed: {connect_resp.status_code} "
                            f"{redacted_body}; aborting without writing fixtures",
                            file=sys.stderr,
                        )
                        _write_debug_events(workdir, events)
                        return None

            openapi = (await client.get("/openapi.json")).json()

            stop = asyncio.Event()
            stream_task = asyncio.create_task(_stream_events(client, events, stop))

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
                "yes"
                if resume_false_resp.status_code < 300
                else f"no ({resume_false_resp.status_code})"
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

            terminal_event = await _drive_prompts_until_terminal(
                client, session_id, events, findings
            )
            if terminal_event is None:
                print(
                    f"session {session_id} did not reach a terminal state within "
                    f"{_DRIVE_PROMPTS_TIMEOUT_SECONDS}s of answering prompts; aborting without "
                    "writing fixtures",
                    file=sys.stderr,
                )
                stop.set()
                stream_task.cancel()
                _write_debug_events(workdir, events)
                return None
            findings["terminal_event"] = str(terminal_event.get("type", "unknown"))

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
            "yes"
            if session_list.returncode == 0
            else f"no ({session_list.returncode}: {session_list.stderr})"
        )
        return openapi, messages

    proc = subprocess.Popen(
        [opencode_path, "serve", "--hostname", "127.0.0.1", "--port", "0", "--stdio"],
        cwd=workspace,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr_fh,
    )
    try:
        base_url = await _wait_for_url(proc)

        assert proc.stdout is not None
        drain_thread = threading.Thread(
            target=_drain_pipe_to_file, args=(proc.stdout, stdout_log_path), daemon=True
        )
        drain_thread.start()

        try:
            result = await asyncio.wait_for(_drive_turn(), timeout=_TURN_TIMEOUT_SECONDS)
        except TimeoutError:
            print(
                f"recon turn did not complete within {_TURN_TIMEOUT_SECONDS}s; aborting without "
                "writing fixtures",
                file=sys.stderr,
            )
            _write_debug_events(workdir, events)
            return 1
        except httpx.HTTPError as exc:
            print(f"recon turn failed with an HTTP error: {exc!r}", file=sys.stderr)
            _write_debug_events(workdir, events)
            return 1

        if result is None:
            return 1

        openapi, messages = result
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
        stderr_fh.close()
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return asyncio.run(run_recon(args))


if __name__ == "__main__":
    raise SystemExit(main())
