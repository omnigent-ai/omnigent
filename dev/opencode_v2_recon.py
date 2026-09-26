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

The generated server password, every temp path, the user's home directory,
and any provider credentials found in the environment are redacted from the
fixtures before they're written. The server's stdout/stderr are logged to
``server.stdout.log``/``server.stderr.log`` in the temp workdir (kept only
with ``--keep-workdir``) for debugging.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import json
import os
import secrets
import shutil
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

# A minimal Plugin.define plugin (packages/plugin/src/promise/plugin.ts) that
# logs the three hooks the omnigent-policy plugin (Stage 3) will use:
# ctx.session.hook("prompt"), ctx.tool.hook("execute.after"), and
# ctx.permission.hook("evaluate") (packages/plugin/src/promise/{session,tool,permission}.ts).
_PLUGIN_TEMPLATE = """\
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
})
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
                    json={"decision": "once"},
                )
                findings["permission_reply_status"] = str(reply.status_code)
                if reply.status_code < 300:
                    answered_permission = True
                else:
                    print(
                        f"permission reply failed: {reply.status_code} {reply.text}",
                        file=sys.stderr,
                    )
            if event.get("type") == "form.created" and not answered_form:
                form = event["data"]["form"]
                answer = {
                    field["key"]: (
                        field.get("options", ["A"])[0] if field.get("type") == "string" else "A"
                    )
                    for field in form.get("fields", [])
                }
                reply = await client.post(
                    f"/api/session/{session_id}/form/{form['id']}/reply",
                    json={"answer": answer},
                )
                findings["form_reply_status"] = str(reply.status_code)
                if reply.status_code < 300:
                    answered_form = True
                else:
                    print(
                        f"form reply failed: {reply.status_code} {reply.text}",
                        file=sys.stderr,
                    )


async def _wait_for_session_idle(
    events: list[dict[str, Any]], session_id: str, timeout: float
) -> bool:
    """Poll captured events for a ``session.idle`` frame for *session_id*."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        for event in events:
            if (
                event.get("type") == "session.idle"
                and event.get("data", {}).get("sessionID") == session_id
            ):
                return True
        await asyncio.sleep(0.5)
    return False


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
        if e.get("type") == "session.tool.called"
        and "recon-echo" in str(e.get("data", {}).get("name", ""))
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


_IDLE_TIMEOUT_SECONDS = 60.0
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
    secrets_to_redact = [password, str(workdir), str(Path.home())]
    secrets_to_redact.extend(credential_values_from_env(env))
    findings: dict[str, str] = {}
    events: list[dict[str, Any]] = []

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

            await _auto_answer_prompts(client, session_id, events, findings)

            idle_reached = await _wait_for_session_idle(events, session_id, _IDLE_TIMEOUT_SECONDS)
            if not idle_reached:
                print(
                    f"session {session_id} did not reach idle within "
                    f"{_IDLE_TIMEOUT_SECONDS}s of the auto-answer window; aborting without "
                    "writing fixtures",
                    file=sys.stderr,
                )
                stop.set()
                stream_task.cancel()
                return None

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
