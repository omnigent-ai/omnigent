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
import base64  # noqa: F401  # used by Task 2's run_recon
import json  # noqa: F401  # used by Task 2's run_recon
import os  # noqa: F401  # used by Task 2's run_recon
import secrets  # noqa: F401  # used by Task 2's run_recon
import shutil  # noqa: F401  # used by Task 2's run_recon
import subprocess  # noqa: F401  # used by Task 2's run_recon
import sys  # noqa: F401  # used by Task 2's run_recon
import tempfile  # noqa: F401  # used by Task 2's run_recon
from pathlib import Path
from typing import Any

import httpx  # noqa: F401  # used by Task 2's run_recon

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
    return asyncio.run(run_recon(args))  # noqa: F821  # defined in Task 2


if __name__ == "__main__":
    raise SystemExit(main())
