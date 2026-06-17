"""PiExecutor: run agents through the Pi coding agent's RPC mode.

Spawns Pi (``pi --mode rpc``) as a subprocess and communicates via a JSONL
protocol over stdin/stdout.  Pi manages its own agent loop, tool execution,
context window, and compaction internally.  This executor translates the Pi
event stream into Omnigent ExecutorEvents.

Omnigent tools are bridged into Pi via a generated JavaScript extension
that registers each tool with ``pi.registerTool()``.  Tool execution is
proxied over a local TCP socket to the Omnigent Python process, so
policies, history recording, sub-agents, runtime, and all other Omnigent
features work exactly as they do with the Claude SDK and Codex harnesses.

When ``os_env`` is set, the Pi subprocess is wrapped in the same
sandbox used for other harnesses.

Databricks support: a temporary ``models.json`` is generated with three
providers (OpenAI Responses for GPT, Anthropic Messages for Claude, and
OpenAI Completions for others) and ``PI_CODING_AGENT_DIR`` is set so Pi
picks it up.

Requirements:
    The ``pi`` CLI must be installed and on PATH.

Environment (Databricks):
    DATABRICKS_CONFIG_PROFILE — optional Databricks profile selector
    ~/.databrickscfg          — host + token profile for workspace access
    (or ~/.databrickscfg with a profile containing host + token)
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import pathlib
import secrets
import shutil
import subprocess
import tempfile
from asyncio import Queue, Task
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeAlias
from urllib.parse import urlparse as _urlparse

from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict
from omnigent.onboarding.databricks_config import DATABRICKS_CLAUDE_DEFAULT_MODEL
from omnigent.spec.types import RetryPolicy

from ._subprocess_lifecycle import close_subprocess_transport
from .databricks_executor import _read_databrickscfg
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    ToolSpec,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# Each line of Pi's JSONL output; the event schema is owned by the Pi CLI
# not us, and varies across subcommands (response ack, message_update,
# tool_execution_start/end, agent_end, message_end, etc.).
CodexEvent: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]


def _fetch_shell_command_token(command: str) -> str | None:
    """Run an auth helper command and return its stdout token.

    :param command: Shell command that prints a bearer token.
    :returns: The stripped token, or ``None`` when the command fails or
        prints no token.
    """
    result = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )
    token = result.stdout.strip()
    if result.returncode != 0 or not token:
        logger.debug("Pi Databricks auth command failed: %s", result.stderr.strip())
        return None
    return token


# Tool-server callback provided by ``Session._wire_sdk_executor``. Invoked
# with a tool name and argument dict; may return the result dict directly
# or a coroutine/future yielding one.
ToolExecutor: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, dict[str, Any]],
    Awaitable[dict[str, Any]] | dict[str, Any],
]

# Native-tool policy gate wired by :class:`PiExecutor`. Invoked with a native
# (non-bridged) tool name + argument dict; returns ``{"block": bool, "reason":
# str}``. Pi's native tools (e.g. ``read``, enabled for skill loading) execute
# inside the Pi process and never traverse the bridged ``/mcp`` path, so the
# ``tool_call`` extension hook routes them here for a TOOL_CALL policy verdict.
NativePolicyGate: TypeAlias = Callable[  # type: ignore[explicit-any]
    [str, dict[str, Any]],
    Awaitable[dict[str, Any]] | dict[str, Any],
]

# Plain JSON value — recursive union used by ``_check_blocked`` to walk
# parsed Pi tool-result payloads.
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

# ---------------------------------------------------------------------------
# TCP tool server — serves Omnigent tools to the Pi extension
# ---------------------------------------------------------------------------


class _ToolServer:
    """Async TCP server that handles tool-call requests from the Pi extension.

    Protocol (JSONL over TCP):
        Request:  {"id":"...","token":"...","tool":"tool_name","args":{...}}
        Response: {"id":"...","result":{...}} or {"id":"...","error":"..."}

    The loopback socket is reachable by any co-located process, so every
    request must carry :attr:`token` (a per-server secret embedded only in
    the generated Pi extension); frames with a missing or wrong token are
    rejected before dispatch.
    """

    def __init__(self) -> None:
        self._server: asyncio.Server | None = None
        self.port: int = 0
        self._tool_executor: ToolExecutor | None = None
        # Policy gate for native (non-bridged) Pi tool calls, set by
        # :meth:`PiExecutor._ensure_tool_server`. ``None`` (single-process
        # / test paths) means native tool calls are not gated.
        self._policy_gate: NativePolicyGate | None = None
        # Per-server bearer token required on every request. Minted at
        # construction (never None) so auth is always enforced.
        self.token: str = secrets.token_urlsafe(32)

    async def start(self) -> int:
        """Start listening on a random port. Returns the port number."""
        self._server = await asyncio.start_server(
            self._handle_client,
            "127.0.0.1",
            0,
        )
        addr = self._server.sockets[0].getsockname()
        self.port = addr[1]
        logger.debug("PiExecutor tool server listening on port %d", self.port)
        return self.port

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Authenticate before any dispatch; close the connection on
                # failure (unlike the malformed-frame ``continue`` below).
                if not self._token_ok(request.get("token")):
                    writer.write(
                        json.dumps(
                            {"id": request.get("id"), "error": "unauthorized"},
                            separators=(",", ":"),
                        ).encode("utf-8")
                        + b"\n"
                    )
                    await writer.drain()
                    break
                raw_req_id = request.get("id")
                raw_tool_name = request.get("tool")
                # The Pi extension always supplies ``id`` and ``tool``
                # on a tool request. Drop malformed frames rather than
                # executing under an empty-string tool name.
                if not isinstance(raw_req_id, str) or not isinstance(raw_tool_name, str):
                    continue
                tool_args = request.get("args", {})
                if request.get("kind") == "policy_eval":
                    # The ``tool_call`` extension hook asks for a TOOL_CALL
                    # verdict on a native (non-bridged) tool — evaluate only,
                    # never execute (Pi runs the tool itself on ALLOW).
                    verdict = await self._evaluate_policy(raw_tool_name, tool_args)
                    response = {"id": raw_req_id, "verdict": verdict}
                else:
                    response = await self._execute(raw_tool_name, tool_args)
                    response["id"] = raw_req_id
                out = json.dumps(response, separators=(",", ":")) + "\n"
                writer.write(out.encode("utf-8"))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionError):
            pass
        finally:
            writer.close()

    def _token_ok(self, presented: JsonValue) -> bool:
        """
        Validate a request's ``token`` against this server's secret.

        :param presented: The ``token`` field from the request JSON — a
            ``str`` when authenticated, any JSON type (or ``None``) on a
            malformed/forged frame, hence the ``isinstance`` guard.
        :returns: ``True`` only when *presented* is a string equal to
            :attr:`token`, compared in constant time.
        """
        return isinstance(presented, str) and hmac.compare_digest(presented, self.token)

    async def _execute(  # type: ignore[explicit-any]
        self,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        if self._tool_executor is None:
            return {"error": f"No tool executor for '{name}'"}
        try:
            raw = self._tool_executor(name, args)
            resolved = await raw if asyncio.iscoroutine(raw) or asyncio.isfuture(raw) else raw
            if not isinstance(resolved, dict):
                resolved = {"result": resolved}
            return {"result": resolved}
        except Exception as exc:  # noqa: BLE001 — tool errors are surfaced via the JSON response envelope
            return {"error": str(exc)}

    async def _evaluate_policy(  # type: ignore[explicit-any]
        self,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate a native (non-bridged) tool call against TOOL_CALL policy.

        Routes through the :attr:`_policy_gate` wired by
        :meth:`PiExecutor._ensure_tool_server`. Returns ``{"block": bool,
        "reason": str}``.

        Fail-open (allow) when no gate is wired or the gate raises: this
        mirrors the runner/scaffold policy-evaluation contract, which also
        defaults to ALLOW on a stalled or failed verdict so a transient
        Omnigent outage can't wedge the agent mid-turn.
        """
        if self._policy_gate is None:
            return {"block": False, "reason": ""}
        try:
            raw = self._policy_gate(name, args)
            resolved = await raw if asyncio.iscoroutine(raw) or asyncio.isfuture(raw) else raw
            if isinstance(resolved, dict):
                return {
                    "block": bool(resolved.get("block")),
                    "reason": str(resolved.get("reason") or ""),
                }
            return {"block": False, "reason": ""}
        except Exception as exc:  # noqa: BLE001 — fail-open; the verdict path must never wedge Pi
            logger.warning("Pi native-tool policy eval failed for %r: %s", name, exc)
            return {"block": False, "reason": ""}


# ---------------------------------------------------------------------------
# Pi extension generator — bridges Omnigent tools into Pi
# ---------------------------------------------------------------------------


def _sanitize_schema(schema: ToolSpec) -> ToolSpec:
    """Strip JSON Schema features unsupported by the OpenAI Responses/Completions APIs.

    Removes ``anyOf``, ``oneOf``, ``allOf``, ``examples``, ``default``,
    ``additionalProperties``, and ``$ref``.  Union keywords are collapsed
    to the first ``object`` branch when one exists (it carries the
    structured properties the model needs), else the first typed branch.
    This ensures the schema is accepted by Databricks serving endpoints.
    """
    if not isinstance(schema, dict):
        return schema
    result: ToolSpec = {}
    for key, value in schema.items():
        if key in ("examples", "default", "$ref", "additionalProperties"):
            continue
        if key in ("anyOf", "oneOf", "allOf"):
            # Prefer the object branch: collapsing to a primitive drops the properties.
            if isinstance(value, list) and value:
                typed = [c for c in value if isinstance(c, dict) and "type" in c]
                for candidate in typed:
                    if candidate["type"] == "object":
                        return _sanitize_schema(candidate)
                if typed:
                    return _sanitize_schema(typed[0])
            continue
        if key == "properties" and isinstance(value, dict):
            result[key] = {k: _sanitize_schema(v) for k, v in value.items()}
        elif key == "items" and isinstance(value, dict):
            result[key] = _sanitize_schema(value)
        else:
            result[key] = value
    return result


def _generate_extension_js(port: int, tool_schemas: list[ToolSpec], token: str) -> str:
    """Generate a JavaScript Pi extension that registers Omnigent tools.

    Each tool is forwarded to the TCP tool server at ``127.0.0.1:<port>``.

    :param port: TCP port the Omnigent tool server is listening on, e.g.
        ``54321``.
    :param tool_schemas: Omnigent tool schemas to register with Pi.
    :param token: The tool server's bearer token
        (:attr:`_ToolServer.token`), embedded in the extension and sent
        on every request so the server can authenticate this Pi process.
    """
    # Build tool descriptors for the JS code. ``ToolSpec`` is the
    # same JSON-shaped ``dict[str, Any]`` we consume.
    descriptors: list[ToolSpec] = []
    for s in tool_schemas:
        raw_name = s.get("name")
        # Pi requires a concrete name on each registered tool; skip
        # malformed entries rather than registering an unnamed tool.
        if not isinstance(raw_name, str) or not raw_name:
            continue
        raw_desc = s.get("description")
        # Description is optional; Pi's JS bridge expects ``str`` on
        # both ``description`` and ``promptSnippet``.
        desc: str = raw_desc if isinstance(raw_desc, str) else ""
        descriptors.append(
            {
                "name": raw_name,
                "description": desc,
                "promptSnippet": desc[:120],
                "parameters": _sanitize_schema(
                    s.get("parameters", {"type": "object", "properties": {}})
                ),
            }
        )
    tools_json = json.dumps(descriptors, indent=2)
    # json.dumps so the secret is correctly JS-string-escaped.
    token_json = json.dumps(token)

    return f"""\
// Auto-generated Omnigent tool bridge extension for Pi.
// Connects to the Omnigent TCP tool server on port {port}.
const net = require("net");

const TOOLS = {tools_json};
const BRIDGED = new Set(TOOLS.map((t) => t.name));
const PORT = {port};
const TOKEN = {token_json};

/** Send a tool call request over TCP and return the result. */
function callTool(toolName, args) {{
  return new Promise((resolve, reject) => {{
    const client = net.createConnection({{ port: PORT, host: "127.0.0.1" }}, () => {{
      const id = Math.random().toString(36).slice(2);
      const req = JSON.stringify({{ id, token: TOKEN, tool: toolName, args }}) + "\\n";
      let buf = "";
      client.on("data", (chunk) => {{
        buf += chunk.toString();
        const nl = buf.indexOf("\\n");
        if (nl !== -1) {{
          try {{
            const resp = JSON.parse(buf.slice(0, nl));
            client.end();
            if (resp.error) {{
              resolve({{
                content: [{{ type: "text", text: JSON.stringify({{ error: resp.error }}) }}],
                isError: true
              }});
            }} else {{
              const text = typeof resp.result === "string"
                ? resp.result
                : JSON.stringify(resp.result);
              const isError = resp.result && (resp.result.error || resp.result.blocked);
              resolve({{ content: [{{ type: "text", text }}], isError: !!isError }});
            }}
          }} catch (e) {{
            client.end();
            resolve({{
              content: [{{ type: "text", text: JSON.stringify({{ error: e.message }}) }}],
              isError: true
            }});
          }}
        }}
      }});
      client.on("error", (err) => {{
        resolve({{
          content: [{{ type: "text", text: JSON.stringify({{ error: err.message }}) }}],
          isError: true
        }});
      }});
      client.write(req);
    }});
    client.on("error", (err) => {{
      resolve({{
        content: [{{ type: "text", text: JSON.stringify({{ error: err.message }}) }}],
        isError: true
      }});
    }});
  }});
}}

/**
 * Ask the Omnigent tool server for a TOOL_CALL policy verdict on a native
 * (non-bridged) Pi tool. Resolves to the verdict object {{ block, reason }}
 * or null. Fail-open (null) on any transport error: a wedged native tool
 * would break Pi worse than a missed gate, and bridged tools stay gated
 * server-side regardless.
 */
function evalNativePolicy(toolName, args) {{
  return new Promise((resolve) => {{
    const client = net.createConnection({{ port: PORT, host: "127.0.0.1" }}, () => {{
      const id = Math.random().toString(36).slice(2);
      const frame = {{ id, token: TOKEN, kind: "policy_eval", tool: toolName, args }};
      const req = JSON.stringify(frame) + "\\n";
      let buf = "";
      client.on("data", (chunk) => {{
        buf += chunk.toString();
        const nl = buf.indexOf("\\n");
        if (nl !== -1) {{
          client.end();
          try {{
            const resp = JSON.parse(buf.slice(0, nl));
            resolve(resp && resp.verdict ? resp.verdict : null);
          }} catch (e) {{
            resolve(null);
          }}
        }}
      }});
      client.on("error", () => resolve(null));
      client.write(req);
    }});
    client.on("error", () => resolve(null));
  }});
}}

module.exports = function(pi) {{
  // Gate native (non-bridged) tool calls through Omnigent policy. Pi's
  // native tools (e.g. ``read``, enabled for skill loading) run in-process
  // and never traverse the bridged /mcp path, so without this hook they
  // escape all guardrails. Bridged tools ARE evaluated server-side at /mcp,
  // so skip them here to avoid double-evaluation (and double ASK prompts).
  pi.on("tool_call", async (event) => {{
    if (!event || typeof event.toolName !== "string") return;
    if (BRIDGED.has(event.toolName)) return;
    const verdict = await evalNativePolicy(event.toolName, event.input || {{}});
    if (verdict && verdict.block) {{
      return {{ block: true, reason: verdict.reason || "blocked by Omnigent policy" }};
    }}
  }});

  for (const tool of TOOLS) {{
    // Pi passes tool.parameters directly to the LLM as JSON Schema, so we
    // can use the Omnigent schema as-is without TypeBox conversion.
    pi.registerTool({{
      name: tool.name,
      label: tool.name,
      description: tool.description,
      promptSnippet: tool.promptSnippet || tool.description,
      parameters: tool.parameters || {{ type: "object", properties: {{}} }},
      async execute(_toolCallId, _params, _signal, _onUpdate, _ctx) {{
        return callTool(tool.name, _params);
      }},
    }});
  }}
}};
"""


# ---------------------------------------------------------------------------
# Credential helpers (shared pattern with codex_executor / claude_sdk_executor)
# ---------------------------------------------------------------------------


def _find_pi_cli() -> str | None:
    """Find the ``pi`` CLI on PATH."""
    return shutil.which("pi")


# ---------------------------------------------------------------------------
# Databricks models.json generation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Databricks model definitions for Pi's models.json
# ---------------------------------------------------------------------------
# Databricks exposes API styles at different URL paths. ucode state can
# override these provider URLs; the host-derived defaults remain for legacy
# profile-only usage.

_DATABRICKS_RESPONSES_MODELS = [
    {
        "id": "databricks-gpt-5-4-mini",
        "name": "GPT-5.4 Mini",
        "contextWindow": 1047576,
        "maxTokens": 32768,
    },
    {"id": "databricks-gpt-5-4", "name": "GPT-5.4", "contextWindow": 1047576, "maxTokens": 32768},
]

_DATABRICKS_ANTHROPIC_MODELS = [
    {
        "id": "databricks-claude-opus-4-8",
        "name": "Claude Opus 4.8",
        # Gateway-verified caps: >1000000 input rejects, 128001+ output rejects.
        "contextWindow": 1000000,
        "maxTokens": 128000,
    },
    {
        "id": "databricks-claude-sonnet-4-6",
        "name": "Claude Sonnet 4.6",
        "contextWindow": 1000000,
        "maxTokens": 128000,
    },
    {
        "id": "databricks-claude-sonnet-4-5",
        "name": "Claude Sonnet 4.5",
        # Gateway rejects this model past ~200k input.
        "contextWindow": 200000,
        "maxTokens": 16384,
    },
]

# Empty: the only listed endpoint (meta-llama-3.3-70b) no longer exists on
# the gateway. The provider stays so non-Claude/GPT ids keep a routing home.
_DATABRICKS_COMPLETIONS_MODELS: list[dict[str, str | int]] = []

# Prefix-matched env var names allowed into the Pi subprocess. Only
# known-safe categories pass: Pi's own config knobs, proxy settings,
# TLS trust overrides, Node.js runtime knobs (Pi is a Node CLI), and
# locale. Credential families (``DATABRICKS_*``, ``AWS_*``, LLM
# provider API keys, ...) deliberately do NOT match.
_PI_ENV_ALLOW_PREFIXES: tuple[str, ...] = (
    "PI_",
    "HTTP_",
    "HTTPS_",
    "ALL_PROXY",
    "NO_PROXY",
    "SSL_",
    "NODE_",
    "XDG_",
    "LANG",
    "LC_",
)

# Exact-matched env var names allowed into the Pi subprocess: the
# minimal set a POSIX CLI reasonably expects.
_PI_ENV_ALLOW_EXACT: frozenset[str] = frozenset(
    {
        "HOME",
        "PATH",
        "TERM",
        "TMPDIR",
        "TMP",
        "TEMP",
        "USER",
        "LOGNAME",
        "SHELL",
        "TZ",
    }
)
_STREAM_READ_CHUNK_SIZE = 65536


def _build_models_json(
    host: str,
    token: str,
    base_urls: dict[str, str] | None = None,
    model: str | None = None,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    # Pi's models.json mixes str/int/bool/list/dict across provider configs;
    # see _DATABRICKS_*_MODELS and the compat/authHeader shapes below. The
    # schema is owned by the Pi CLI and not worth a TypedDict tree here.
    """Build a Pi ``models.json`` with three gateway providers.

    Each provider targets a different API gateway path and wire format so
    the correct protocol is used for each model family. The static model
    lists cover the known Databricks-gateway ids; *model* additionally
    registers the resolved run model so a gateway model outside those
    lists (an OpenRouter/LiteLLM id like ``moonshotai/kimi-k2.6``, or a
    Databricks id newer than the static list) resolves instead of Pi
    failing with "Model not found" — Pi only accepts ``provider/<model>``
    selectors whose id is registered under that provider.

    :param host: Databricks workspace URL used for legacy profile-only
        defaults.
    :param token: Bearer token to write into Pi's provider entries.
    :param base_urls: Optional provider base URLs keyed by model family,
        e.g. ``{"claude": "...", "openai": "..."}`` — from ucode state or
        a generic (OpenRouter/LiteLLM) provider entry.
    :param model: The resolved model id this run will select, e.g.
        ``"moonshotai/kimi-k2.6"``; registered (bare ``{"id": ...}``, the
        same shape ucode writes) under the provider
        :func:`_pi_provider_for_model` routes it to when absent from the
        static list. ``None`` skips registration (Pi picks its default).
    :returns: Pi ``models.json`` contents.
    """
    h = host.rstrip("/")
    serving_endpoints_url = f"{h}/serving-endpoints"
    raw_openai_base_url = (base_urls or {}).get("openai")
    # ucode's ``openai`` gateway is the Codex Responses gateway
    # (``.../ai-gateway/codex/v1``), which 404s pi's openai-completions
    # ``/chat/completions`` POST. Re-route only that case to serving-endpoints;
    # generic providers (no ``/ai-gateway/codex``) pass through. Gemini rides
    # this path via databricks-completions since pi can't speak generateContent.
    if raw_openai_base_url and "/ai-gateway/codex" in raw_openai_base_url:
        openai_base_url = serving_endpoints_url
    else:
        openai_base_url = raw_openai_base_url or serving_endpoints_url
    claude_base_url = (base_urls or {}).get("claude") or f"{h}/serving-endpoints/anthropic"
    config: dict[str, Any] = {  # type: ignore[explicit-any]  # Pi-owned schema, see note above
        "providers": {
            # GPT models → OpenAI Chat Completions API.
            # We use completions (not responses) because the Databricks
            # Responses API rejects tool-result chaining on subsequent turns.
            # The ``compat`` settings ensure Pi uses ``system`` role (not
            # ``developer``) and avoids other OpenAI-specific features that
            # Databricks doesn't support.
            "databricks": {
                "baseUrl": openai_base_url,
                "apiKey": token,
                "api": "openai-completions",
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsStore": False,
                    "supportsStrictMode": False,
                    "supportsReasoningEffort": False,
                },
                "models": _DATABRICKS_RESPONSES_MODELS,
            },
            # Claude models → Anthropic Messages API.
            # ``authHeader`` sends ``Authorization: Bearer <token>`` instead
            # of the default Anthropic ``x-api-key`` header.
            "databricks-anthropic": {
                "baseUrl": claude_base_url,
                "apiKey": token,
                "api": "anthropic-messages",
                "authHeader": True,
                "models": _DATABRICKS_ANTHROPIC_MODELS,
            },
            # Everything else (Llama, etc.) → same endpoint, same API
            "databricks-completions": {
                "baseUrl": openai_base_url,
                "apiKey": token,
                "api": "openai-completions",
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsStore": False,
                    "supportsStrictMode": False,
                    "supportsReasoningEffort": False,
                },
                "models": _DATABRICKS_COMPLETIONS_MODELS,
            },
        },
    }
    if model is not None:
        provider = config["providers"][_pi_provider_for_model(model)]
        if not any(entry.get("id") == model for entry in provider["models"]):
            # Rebind (don't append): the static lists are module-level
            # constants shared across builds, so in-place mutation would
            # leak this run's model id into every later models.json.
            # Declare image input: Pi's transformMessages drops every image
            # block ("model does not support images") unless the model entry
            # advertises it, so a dynamically-registered vision model would
            # silently lose its images (#515).
            provider["models"] = [
                *provider["models"],
                {"id": model, "input": ["text", "image"]},
            ]
    return config


def _pi_provider_for_model(model: str) -> str:
    """Return the Pi provider name to use for a given Databricks model."""
    lower = model.lower()
    if "claude" in lower:
        return "databricks-anthropic"
    if "gpt" in lower:
        return "databricks"
    return "databricks-completions"


async def _create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:  # type: ignore[explicit-any]
    """
    Indirection point for ``asyncio.create_subprocess_exec``.

    Exists so tests can stub subprocess creation without patching
    ``asyncio.create_subprocess_exec`` globally (patching
    ``omnigent.inner.pi_executor.asyncio.create_subprocess_exec``
    walks the dotted path into the real ``asyncio`` module singleton
    and leaks the mock into every other test in the process).

    :param args: Positional argv components forwarded to
        ``asyncio.create_subprocess_exec``.
    :param kwargs: Keyword args (``stdin``, ``stdout``, ``stderr``,
        ``env``, ``cwd``, ...) forwarded as-is.
    :returns: The spawned subprocess handle.
    """
    return await asyncio.create_subprocess_exec(*args, **kwargs)


def _clean_pi_env(extra_allowed: Sequence[str] | None = None) -> dict[str, str]:
    """
    Build a filtered copy of ``os.environ`` for the Pi subprocess.

    Deny-by-default allowlist mirroring the codex path's
    :func:`omnigent.inner.codex_executor._clean_codex_env`. The previous
    additive ``{**os.environ, **env}`` merge leaked every host secret
    — cloud tokens, API keys — into the Pi process, sandboxed or not.
    Credential families are deliberately not allowlisted:
    gateway runs authenticate through the generated ``models.json``,
    and a spec that legitimately needs a variable inside the Pi
    process opts in via ``os_env.sandbox.env_passthrough``.

    :param extra_allowed: Extra exact names to pass through, e.g. the
        spec's ``os_env.sandbox.env_passthrough`` entries
        (``["ANTHROPIC_API_KEY"]`` for an env-key-authenticated
        non-gateway run). ``None`` means no extras.
    :returns: Filtered environment dict, ready to extend with the
        executor's own variables (``PI_CODING_AGENT_DIR``, ...).
    """
    allow_exact = set(_PI_ENV_ALLOW_EXACT)
    if extra_allowed is not None:
        allow_exact.update(extra_allowed)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allow_exact or key.startswith(_PI_ENV_ALLOW_PREFIXES)
    }


# ---------------------------------------------------------------------------
# Pi RPC subprocess wrapper
# ---------------------------------------------------------------------------


@dataclass
class _PiRpcSession:
    """Manages a single Pi subprocess in RPC mode."""

    process: asyncio.subprocess.Process | None = None
    _read_task: Task[None] | None = None
    _stderr_task: Task[None] | None = None
    # Non-optional: initialized eagerly so the reader/writer paths don't
    # need to None-narrow on every access. ``None`` sentinel values are
    # pushed onto the queue to signal EOF to ``read_line``.
    _line_queue: Queue[str | None] = field(default_factory=Queue)
    _stderr_lines: list[str] = field(default_factory=list)
    _tmp_dir: str | None = None

    async def start(
        self,
        pi_path: str,
        *,
        env: dict[str, str],
        cwd: str | None = None,
        model: str | None = None,
        system_prompt: str | None = None,
        extra_args: list[str] | None = None,
    ) -> None:
        """
        Spawn the Pi subprocess in RPC mode and start the I/O readers.

        :param pi_path: Path to spawn — the ``pi`` binary or the
            sandbox launcher script wrapping it.
        :param env: The COMPLETE subprocess environment. Deliberately
            not merged with ``os.environ`` — the caller builds it from
            the :func:`_clean_pi_env` allowlist so host/server secrets
            (e.g. ``DATABRICKS_TOKEN``) never leak into the (possibly
            sandboxed) Pi process.
        :param cwd: Working directory for the subprocess, or ``None``
            to inherit the parent's.
        :param model: Pi model selector, e.g.
            ``"databricks-anthropic/databricks-claude-sonnet-4-6"``.
            ``None`` lets Pi pick its default.
        :param system_prompt: Text appended to Pi's default system
            prompt via ``--append-system-prompt``. ``None`` skips it.
        :param extra_args: Extra CLI tokens (``--extension``,
            ``--tools``, ...). ``None`` appends nothing.
        """
        args = [pi_path, "--mode", "rpc", "--no-session"]
        if model:
            pi_coding_agent_dir = env.get("PI_CODING_AGENT_DIR")
            args.extend(
                [
                    "--model",
                    f"databricks/{model}"
                    if pi_coding_agent_dir is not None and "databricks" in pi_coding_agent_dir
                    else model,
                ]
            )
        if system_prompt:
            # Use --append-system-prompt instead of --system-prompt so Pi
            # keeps its default prompt (which includes tool descriptions from
            # promptSnippet and guidelines).  Using --system-prompt would
            # replace the default prompt entirely, stripping tool awareness.
            args.extend(["--append-system-prompt", system_prompt])
        if extra_args:
            args.extend(extra_args)

        logger.debug("PiExecutor: spawning %s", " ".join(args))
        self.process = await _create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        self._read_task = asyncio.create_task(self._reader())
        self._stderr_task = asyncio.create_task(self._stderr_reader())

    async def _reader(self) -> None:
        """Background task: read lines from Pi stdout and enqueue them."""
        assert self.process is not None and self.process.stdout is not None
        try:
            async for raw_line in self._iter_stream_lines(self.process.stdout):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                if line:
                    self._line_queue.put_nowait(line)
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — reader loop logs and exits on any unexpected error
            logger.debug("PiExecutor reader error: %s", exc)
        finally:
            self._line_queue.put_nowait(None)

    async def _stderr_reader(self) -> None:
        """Drain stderr in the background."""
        if self.process is None or self.process.stderr is None:
            return
        try:
            async for raw_line in self._iter_stream_lines(self.process.stderr):
                text = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                if text:
                    logger.debug("pi stderr: %s", text)
                    if len(self._stderr_lines) < 50:
                        self._stderr_lines.append(text)
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 — stderr drainer is best-effort; never crashes
            pass

    @staticmethod
    async def _iter_stream_chunks(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        while True:
            chunk = await stream.read(_STREAM_READ_CHUNK_SIZE)
            if not chunk:
                break
            yield chunk

    @staticmethod
    async def _iter_stream_lines(stream: asyncio.StreamReader) -> AsyncIterator[bytes]:
        buffer = bytearray()
        async for chunk in _PiRpcSession._iter_stream_chunks(stream):
            buffer.extend(chunk)
            while True:
                newline_index = buffer.find(b"\n")
                if newline_index < 0:
                    break
                line = bytes(buffer[: newline_index + 1])
                del buffer[: newline_index + 1]
                yield line
        if buffer:
            yield bytes(buffer)

    async def send_command(self, command: CodexEvent) -> None:
        """Send a JSONL command to Pi's stdin."""
        if self.process is None or self.process.stdin is None:
            raise RuntimeError("Pi process not running")
        line = json.dumps(command, separators=(",", ":")) + "\n"
        self.process.stdin.write(line.encode("utf-8"))
        await self.process.stdin.drain()

    async def read_line(self, timeout: float = 120.0) -> str | None:
        """Read the next JSONL line from Pi's stdout. Returns None on EOF."""
        try:
            return await asyncio.wait_for(self._line_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    async def close(self) -> None:
        for task in (self._read_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._read_task = None
        self._stderr_task = None
        if self.process is not None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError, RuntimeError):
                # RuntimeError can happen when the subprocess was created on a
                # different event loop (e.g. test fixtures that call close() in
                # a fresh loop).  Fall back to synchronous kill.
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()
            close_subprocess_transport(self.process)
            self.process = None
        if self._tmp_dir is not None:
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None


# ---------------------------------------------------------------------------
# PiExecutor
# ---------------------------------------------------------------------------


@dataclass
class _PiSessionState:
    rpc: _PiRpcSession | None = None
    system_prompt: str | None = None
    model: str | None = None
    _has_sent_prompt: bool = False


@dataclass(frozen=True)
class BlockedCheck:
    """Result of inspecting a Pi tool result for a policy-blocked payload.

    :param blocked: ``True`` when the result is (or wraps) a
        ``{"blocked": true, "reason": "..."}`` dict.
    :param reason: The human-readable block reason when ``blocked`` is
        ``True``; an empty string otherwise.
    """

    blocked: bool
    reason: str


@dataclass(frozen=True)
class PiSubprocessConfig:
    """Materialized environment + CLI args for a Pi subprocess.

    :param env: The complete environment for the Pi process — the
        :func:`_clean_pi_env` allowlist base, plus
        ``PI_CODING_AGENT_DIR`` when Databricks model routing is in use.
    :param tmp_dir: Temp directory that owns any ``models.json`` /
        ``omnigent_tools.js`` files generated for this subprocess.  The
        caller is responsible for cleaning it up on shutdown.
    :param extra_args: Extra CLI arguments to append to the Pi invocation,
        e.g. ``["--extension", ".../omnigent_tools.js"]``.
    """

    env: dict[str, str]
    tmp_dir: str
    extra_args: list[str]


def _extract_text(msg: Message) -> str:
    """
    Extract text content from a message dict.

    :param msg: A conversation message dict.
    :returns: Plain text content of the message.
    """
    content = msg.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        return " ".join(parts)
    return str(content)


def _extract_latest_user_content(
    messages: list[Message],
) -> str | list[dict[str, Any]]:
    """
    Extract the latest user message content.

    Returns a plain string for text-only messages. When the
    message carries multimodal content blocks, returns the
    block list so the caller can pass structured input to Pi.

    :param messages: Conversation history.
    :returns: A string prompt or a list of content block dicts.
    """
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content")
            if content is None:
                return ""
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return content
            return str(content)
    return ""


def _parse_data_uri(uri: str) -> tuple[str, str]:
    """Parse a ``data:`` URI into ``(media_type, base64_payload)``.

    :param uri: A data URI, e.g. ``"data:image/png;base64,iVBOR..."``.
    :returns: Tuple of ``(media_type, base64_payload)``.
    :raises ValueError: If ``uri`` is not a ``data:`` URI.
    """
    if not uri.startswith("data:"):
        raise ValueError(f"Not a data URI: {uri[:40]!r}")
    header, _, payload = uri[5:].partition(",")
    media_type = header.replace(";base64", "")
    return media_type, payload


def _split_pi_prompt(blocks: list[dict[str, Any]]) -> tuple[str, list[dict[str, str]]]:
    """Split content blocks into Pi's prompt ``message`` text and ``images``.

    Pi's RPC ``prompt`` command carries text in ``message`` and images in a
    native ``images`` field (``ImageContent = {type, data, mimeType}``), which
    the ``openai-completions`` provider converts to ``image_url`` blocks.
    JSON-encoding the blocks into ``message`` instead (the previous behavior)
    makes Pi forward the image data URI as literal text, so the model never
    sees the image (#515).

    :param blocks: Responses-API content block dicts (``input_text`` /
        ``input_image``). Non-text, non-image blocks have no Pi channel and are
        skipped.
    :returns: ``(message, images)`` — joined text and a list of Pi
        ``ImageContent`` dicts.
    """
    text_parts: list[str] = []
    images: list[dict[str, str]] = []
    for block in blocks:
        block_type = block.get("type")
        if block_type in ("input_text", "output_text", "text"):
            text_parts.append(str(block.get("text") or ""))
        elif block_type == "input_image":
            image_url = block.get("image_url")
            if not isinstance(image_url, str) or not image_url.startswith("data:"):
                raise ValueError(
                    "input_image block must carry an inline data URI in "
                    "'image_url'. Upload the image via the session files API "
                    "and reference it by file_id."
                )
            media_type, data = _parse_data_uri(image_url)
            images.append({"type": "image", "data": data, "mimeType": media_type})
    return "\n".join(text_parts), images


def _build_pi_prompt(
    messages: list[Message], *, is_first_turn: bool
) -> str | list[dict[str, Any]]:
    """
    Build the prompt to send to Pi.

    On the first turn with prior history (e.g. sub-agent with
    ``pass_history=True``), serializes the full conversation so
    the Pi process has context. Otherwise returns just the
    latest user message content (may be multimodal).

    :param messages: Omnigent conversation history for the
        turn.
    :param is_first_turn: ``True`` when this is the first turn
        against a freshly-started Pi subprocess, so the full
        history needs to be serialized into the prompt.
    :returns: A string prompt or a list of content block dicts.
    """
    user_messages = [m for m in messages if m.get("role") == "user"]

    if is_first_turn and len(messages) > 1 and len(user_messages) > 1:
        lines = ["Conversation so far:"]
        for msg in messages:
            role = str(msg.get("role") or "user").replace("_", " ")
            text = _extract_text(msg)
            lines.append(f"{role}: {text}")
        lines.append("")
        lines.append(
            "Respond to the latest user message, using the conversation above as context."
        )
        return "\n".join(lines)

    return _extract_latest_user_content(messages)


@dataclass(frozen=True)
class SandboxedPiCli:
    """Result of wrapping the Pi CLI in a sandbox.

    :param launch_path: The path the executor should actually spawn.  Either
        the original ``pi_path`` (sandbox skipped) or a generated wrapper
        script that applies the sandbox before exec-ing Pi.
    :param sandboxed: ``True`` when the wrapper script is active.
    """

    launch_path: str
    sandboxed: bool


def _try_sandbox_pi(
    pi_path: str,
    os_env: OSEnvSpec | None,
    cwd: str | None,
    spawn_env_names: Sequence[str] | None = None,
) -> SandboxedPiCli:
    """Wrap the Pi CLI in a sandbox if ``os_env`` requests it.

    :param pi_path: Path to the resolved ``pi`` binary.
    :param os_env: The agent's ``os_env`` spec.  ``None`` or a ``"none"``
        sandbox skips wrapping.
    :param cwd: Working directory the sandbox should consider the root.
    :param spawn_env_names: Env-var names the executor deliberately
        passes in the Pi subprocess env (the :func:`_clean_pi_env`
        keys plus per-spawn extras like ``PI_CODING_AGENT_DIR``).
        Baked into the launcher policy so :func:`run_launcher` prunes
        anything else its environment picks up — defense in depth
        against host-env leakage into the sandbox. ``None`` skips the
        prune.
    """
    if os_env is None:
        return SandboxedPiCli(launch_path=pi_path, sandboxed=False)
    sandbox_spec = os_env.sandbox or OSEnvSandboxSpec()
    if sandbox_spec.type == "none":
        return SandboxedPiCli(launch_path=pi_path, sandboxed=False)
    try:
        import pathlib

        from .sandbox import (
            create_exec_launcher,
            resolve_sandbox,
            with_additional_read_roots,
            with_additional_write_roots,
            with_spawn_env_allowlist,
        )

        resolved_cwd = pathlib.Path(cwd or os.getcwd()).resolve(strict=False)
        sandbox = resolve_sandbox(os_env, resolved_cwd)
        if not sandbox.active:
            return SandboxedPiCli(launch_path=pi_path, sandboxed=False)
        # Pi needs to read its own installation + node_modules.
        pi_dir = pathlib.Path(pi_path).resolve().parent.parent
        sandbox = with_additional_read_roots(sandbox, [pi_dir])
        # Pi writes to ~/.pi and to /tmp.
        home_pi = pathlib.Path(os.path.expanduser("~/.pi"))
        sandbox = with_additional_write_roots(sandbox, [home_pi, pathlib.Path("/tmp")])
        sandbox = with_spawn_env_allowlist(sandbox, spawn_env_names)
        launcher = create_exec_launcher(pi_path, sandbox)
        return SandboxedPiCli(launch_path=launcher, sandboxed=True)
    except (OSError, ImportError, NotImplementedError) as exc:
        logger.warning("Could not apply sandbox for Pi: %s", exc)
        return SandboxedPiCli(launch_path=pi_path, sandboxed=False)


def _resolve_pi_skill_args(
    skills_filter: str | list[str],
    bundle_dir: pathlib.Path | None,
) -> list[str]:
    """
    Translate ``skills_filter`` into Pi CLI args.

    Pi exposes two skill knobs at the CLI: ``--no-skills`` (suppress
    auto-discovery + loading) and ``--skill <path>`` (explicitly load
    a skill directory; repeatable). This helper composes them based
    on the spec's ``skills_filter``:

    - ``"all"`` → emit ``--skill <path>`` for every bundled skill so
      the agent definitely sees them, AND let Pi's auto-discovery
      run (no ``--no-skills``) so any host-installed skills also
      surface.
    - ``"none"`` → ``["--no-skills"]``. Pi's auto-discovery is
      suppressed and no explicit skills are loaded.
    - ``list[str]`` → ``["--no-skills"]`` (suppress auto-discovery)
      plus one ``--skill <path>`` per named bundle skill that
      exists. Names not present in the bundle are silently skipped
      — matches the SDK convention where a missing skill is no-op,
      not an error.

    Pi's ``--skill`` flag accepts a directory path, not a name, so
    the resolver looks up named skills under ``<bundle>/skills/``.
    Host-installed Pi skills aren't accessible by name without
    knowing Pi's internal extension layout, so the named-list mode
    only resolves bundle skills.

    :param skills_filter: ``"all"`` / ``"none"`` / list of skill
        names from the spec.
    :param bundle_dir: The agent bundle's extracted on-disk path
        (e.g. ``loaded.workdir``). ``None`` when no bundle is
        available — the resolver returns no ``--skill`` flags
        regardless of filter, and only the ``--no-skills`` flag for
        ``"none"``/list cases.
    :returns: A list of CLI tokens to extend ``self._extra_args``
        with. Empty for ``"all"`` when there are no bundle skills.
    """
    bundle_skills: dict[str, pathlib.Path] = {}
    if bundle_dir is not None:
        skills_root = bundle_dir / "skills"
        if skills_root.is_dir():
            for child in sorted(skills_root.iterdir()):
                if child.is_dir() and (child / "SKILL.md").is_file():
                    bundle_skills[child.name] = child

    if skills_filter == "all":
        # Pi auto-discovers host skills; we explicitly add bundled
        # ones so the agent definitely sees them regardless of cwd.
        args: list[str] = []
        for path in bundle_skills.values():
            args.extend(["--skill", str(path)])
        return args
    if skills_filter == "none":
        # ``--no-skills`` suppresses Pi's discovery walk AND loading.
        # No ``--skill`` flags either — explicit paths would override
        # ``--no-skills`` (Pi loads what's explicitly named).
        return ["--no-skills"]
    if isinstance(skills_filter, list):
        args = ["--no-skills"]
        for name in skills_filter:
            if name in bundle_skills:
                args.extend(["--skill", str(bundle_skills[name])])
        return args
    # Unknown shape — fall back to Pi's defaults (auto-discovery on,
    # no explicit skills). The harness wrap's resolver should
    # have validated upstream, so this branch is belt-and-suspenders.
    return []


def _extract_pi_turn_usage(
    message: object,
    fallback_model: str | None,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Map a Pi assistant message's ``usage`` object onto the wire shape
    that :class:`TurnComplete` consumes, so pi sub-agent cost is priced
    the same way as ``claude-sdk`` and ``codex`` turns.

    Pi (``@earendil-works/pi-coding-agent``) forwards assistant messages
    whose ``usage`` dict carries ``input`` / ``output`` / ``cacheRead`` /
    ``cacheWrite`` / ``totalTokens`` token counts, and the message itself
    carries the resolved ``model``. This translates those into omnigent's
    usage schema (see :class:`omnigent.inner.executor.TurnComplete`).

    :param message: A pi message dict (e.g. ``event["message"]`` from a
        ``message_end`` event, or an entry from ``event["messages"]`` on
        ``agent_end``). Defensive against non-dict shapes.
    :param fallback_model: The executor's configured model, used for
        cost pricing when the assistant message omits ``model``.
    :returns: The mapped usage dict, or ``None`` when ``message`` is not
        an assistant message carrying a ``usage`` dict — callers leave
        ``TurnComplete.usage`` as ``None`` in that case.
    """
    if not isinstance(message, dict):
        return None
    if message.get("role") != "assistant":
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    raw_model = message.get("model")
    model = raw_model if isinstance(raw_model, str) and raw_model else fallback_model
    return {
        "input_tokens": int(usage.get("input") or 0),
        "output_tokens": int(usage.get("output") or 0),
        "total_tokens": int(usage.get("totalTokens") or 0),
        "cache_read_input_tokens": int(usage.get("cacheRead") or 0),
        "cache_creation_input_tokens": int(usage.get("cacheWrite") or 0),
        "model": model,
    }


def _aggregate_pi_turn_usage(
    message_usages: list[dict[str, Any]],  # type: ignore[explicit-any]
    fallback_model: str | None,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """Aggregate per-message Pi usage into one turn-level usage dict.

    A single Omnigent turn drives Pi's full agent loop, which may make
    several LLM calls (one assistant message per iteration of a tool-use
    loop), each forwarded as its own ``message_end``. Mirrors the
    openai-agents executor's billing/context split (see
    ``openai_agents_sdk_executor``): token counts are SUMMED across those
    calls because each call is billed for the full input it sends, while
    ``context_tokens`` reflects only the LAST call's total — the proxy for
    how full the context window is going into the next request (summing
    inputs would double-count the re-sent history).

    :param message_usages: Per-message usage dicts from
        :func:`_extract_pi_turn_usage`, in arrival order. Empty when pi
        reported no usage for the turn.
    :param fallback_model: The executor's configured model id, used only
        when no captured message carried a ``model``,
        e.g. ``"databricks-claude-sonnet-4-6"``.
    :returns: A turn-level usage dict for ``TurnComplete.usage`` carrying
        summed ``input_tokens`` / ``output_tokens`` / ``total_tokens`` /
        ``cache_read_input_tokens`` / ``cache_creation_input_tokens``, a
        last-call ``context_tokens``, and the pricing ``model``; or
        ``None`` when no usable usage was captured.
    """
    if not message_usages:
        return None
    input_tokens = sum(u["input_tokens"] for u in message_usages)
    output_tokens = sum(u["output_tokens"] for u in message_usages)
    total_tokens = sum(u["total_tokens"] for u in message_usages)
    cache_read = sum(u["cache_read_input_tokens"] for u in message_usages)
    cache_write = sum(u["cache_creation_input_tokens"] for u in message_usages)
    # No countable tokens means pi reported empty usage objects — treat as
    # "no usage" so the server leaves the session unpriced rather than
    # recording a $0.00 turn.
    if not (input_tokens or output_tokens):
        return None
    last = message_usages[-1]
    # context_tokens = the LAST call's total context (the proxy for
    # next-request context fill); recompute from components when the
    # provider omitted ``totalTokens``.
    context_tokens = last["total_tokens"] or (
        last["input_tokens"]
        + last["output_tokens"]
        + last["cache_read_input_tokens"]
        + last["cache_creation_input_tokens"]
    )
    # _extract_pi_turn_usage already applied the per-message
    # message-model-else-fallback precedence, so the last entry's model is
    # the authoritative one to price the turn with.
    model = last["model"] if last["model"] else fallback_model
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens or (input_tokens + output_tokens),
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "context_tokens": context_tokens,
        "model": model,
    }


class PiExecutor(Executor):
    """Execute agent turns via the Pi coding agent (``pi --mode rpc``)."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        pi_path: str | None = None,
        gateway: bool = False,
        databricks_profile: str | None = None,
        gateway_host: str | None = None,
        base_url_override: str | None = None,
        base_urls_override: dict[str, str] | None = None,
        gateway_auth_command: str | None = None,
        retry_policy: RetryPolicy | None = None,
        bundle_dir: pathlib.Path | None = None,
        agent_name: str | None = None,
        skills_filter: str | list[str] = "all",
    ) -> None:
        """Create a PiExecutor.

        :param cwd: Working directory for the Pi subprocess.
        :param os_env: Optional OS environment / sandbox spec.  When set, the
            Pi subprocess is wrapped in the same sandbox other
            harnesses use.
        :param model: Override the model name, e.g. ``"databricks-claude-sonnet-4-6"``.
        :param pi_path: Absolute path to a ``pi`` CLI binary.  When ``None``
            the executor searches ``PATH``.
        :param gateway: When ``True``, write a ``models.json`` pointing Pi
            at a vendor-neutral gateway. The Databricks AI gateway is one
            producer of this transport; generic providers are another.
        :param databricks_profile: Databricks-specific config profile from
            ``~/.databrickscfg``, e.g. ``"<your-profile>"``.  Only used on the
            Databricks producer path. ``None`` falls back to
            ``DATABRICKS_CONFIG_PROFILE`` then the first valid profile.
        :param gateway_host: Gateway workspace host origin, e.g.
            ``"https://example.databricks.com"``.  Set from
            ``HARNESS_PI_GATEWAY_HOST`` (written by the Omnigent workflow layer).
            When set, skips profile host lookup.
        :param base_url_override: Override the workspace host used when
            building Pi's ``models.json``.  Expected to be the Anthropic
            gateway URL from ucode state.json (``base_urls.claude``), e.g.
            ``"https://example.databricks.com/ai-gateway/anthropic"``.
            The host portion is extracted and used as the base for all
            Pi provider URLs.  ``None`` falls back to deriving the host from
            the Databricks profile credentials.
        :param base_urls_override: ucode-provided Pi gateway URLs keyed by
            model family, e.g. ``{"claude": "...", "openai": "..."}``.
        :param gateway_auth_command: Shell command that prints a bearer token,
            e.g.
            ``"databricks auth token --host https://example.databricks.com ..."``
            or ``"printf %s sk-..."``. Set from
            ``HARNESS_PI_GATEWAY_AUTH_COMMAND``.
        :param retry_policy: The spec's ``llm.retry`` budget. Threads
            ``policy.pi.settings()`` into Pi's ``.pi/settings.json``
            before subprocess spawn — Pi natively does exponential
            backoff and Retry-After honoring; this just sets the
            budget. ``None`` resolves to ``RetryPolicy()`` defaults.
            See Phase 1f of ``designs/RETRY_ACROSS_HARNESSES.md``.
        :param bundle_dir: The agent bundle's extracted on-disk path.
            When set, ``<bundle_dir>/skills/<name>/SKILL.md`` files
            are exposed to Pi via ``--skill <path>`` based on
            *skills_filter*. ``None`` skips bundle-skill wiring.
        :param agent_name: Optional agent display name. Reserved for
            future use; currently unused by Pi.
        :param skills_filter: Host-skill filter (``"all"`` / ``"none"``
            / ``list[str]``). ``"all"`` (default) lets Pi's
            auto-discovery run AND adds explicit ``--skill`` flags
            for each bundle skill; ``"none"`` adds ``--no-skills``
            so Pi sees zero skills; a list adds ``--no-skills`` plus
            ``--skill`` for each named bundle skill — names not
            present in the bundle are silently skipped.
        """
        resolved_pi = pi_path or _find_pi_cli()
        if not resolved_pi:
            raise ImportError(
                "PiExecutor requires the 'pi' CLI on PATH. "
                "Install it with: npm install -g @earendil-works/pi-coding-agent"
            )
        self._pi_path = resolved_pi
        self._cwd = cwd
        self._os_env_spec = os_env
        self._model_override = model
        self._gateway = gateway
        self._databricks_profile = databricks_profile
        self._gateway_host_override = gateway_host.rstrip("/") if gateway_host else None
        self._base_url_override = base_url_override
        self._base_urls_override = base_urls_override
        self._gateway_auth_command = gateway_auth_command
        # Retry policy → Pi's .pi/settings.json before subprocess spawn.
        # See ``RetryPolicy.pi.settings()`` for the schema. Pi natively
        # does exponential backoff + Retry-After honoring; we just set
        # the budget.
        self._retry_policy = retry_policy if retry_policy is not None else RetryPolicy()
        # Allowlisted base env for the Pi subprocess — never the full
        # host environ. The spec's os_env.sandbox.env_passthrough is the
        # opt-in for anything beyond the base set (e.g. a provider API
        # key on a direct, non-gateway run).
        self._env: dict[str, str] = _clean_pi_env(
            os_env.sandbox.env_passthrough if os_env is not None and os_env.sandbox else None
        )
        # ``--no-tools`` in pi 0.68.x disables BOTH built-ins AND extension
        # tools by default; we re-enable just the bridged tool names per
        # turn via ``--tools <comma-list>`` in :meth:`_build_env_and_dir`.
        # The combined effect is: pi's native read/bash/edit/write stay
        # off (they don't route through Omnigent policies / history and
        # can 400 against the Databricks Responses API), and the bridge
        # extension's tools are explicitly allowlisted.
        self._extra_args: list[str] = ["--no-tools"]
        self._bundle_dir = bundle_dir
        self._agent_name = agent_name
        self._skills_filter = skills_filter
        # Resolve once at construction (the bundle layout doesn't
        # change across turns within a session). Each turn's
        # ``_build_env_and_dir`` copies ``self._extra_args`` so this
        # extension is read-only after init.
        self._extra_args.extend(_resolve_pi_skill_args(skills_filter, bundle_dir))
        # Set by Session._wire_sdk_executor().
        self._tool_executor: ToolExecutor | None = None

        if gateway:
            creds = None
            if self._gateway_host_override is None:
                creds = _read_databrickscfg(databricks_profile)
                if creds is None:
                    raise OSError(
                        "PiExecutor(gateway=True) requires gateway credentials via "
                        "the gateway base URL / auth command or a valid "
                        "~/.databrickscfg profile."
                    )
            if self._gateway_host_override is not None:
                self._databricks_host = self._gateway_host_override
                if gateway_auth_command is None:
                    raise OSError(
                        "PiExecutor(gateway=True) with a gateway workspace host "
                        "requires a gateway auth command."
                    )
                token = _fetch_shell_command_token(gateway_auth_command)
                if token is None:
                    raise OSError(
                        "PiExecutor(gateway=True) could not fetch a gateway token "
                        "for the workspace host."
                    )
                self._databricks_token = token
            elif base_url_override:
                # Derive the workspace host from the Anthropic gateway URL
                # ucode wrote to state.json.
                _parsed = _urlparse(base_url_override)
                self._databricks_host = f"{_parsed.scheme}://{_parsed.netloc}"
                assert creds is not None
                self._databricks_token = creds.token
            else:
                assert creds is not None
                self._databricks_host = creds.host
                self._databricks_token = creds.token

        # True when the gateway transport was derived from a ~/.databrickscfg
        # profile (no gateway host or base URL supplied directly). Gates the
        # Databricks default model in :meth:`_resolve_model`; on the ucode /
        # generic-provider paths the producer resolves a concrete model.
        self._gateway_uses_databricks_profile = bool(
            gateway and self._gateway_host_override is None and base_url_override is None
        )

        # Apply sandbox.
        # ``PI_CODING_AGENT_DIR`` is added per-spawn by
        # ``_build_env_and_dir`` on the gateway path, so it must be in
        # the launcher's prune allowlist even though it's not in
        # ``self._env`` yet.
        sandboxed = _try_sandbox_pi(
            self._pi_path,
            os_env,
            cwd,
            spawn_env_names=[*self._env, "PI_CODING_AGENT_DIR"],
        )
        self._pi_launch_path = sandboxed.launch_path
        self._sandboxed = sandboxed.sandboxed

        self._session_states: dict[str, _PiSessionState] = {}
        self._tool_server: _ToolServer | None = None

    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        return True

    def supports_live_message_queue(self) -> bool:
        return True

    async def enqueue_session_message(  # type: ignore[explicit-any]
        self,
        session_key: str,
        content: str | dict[str, Any],
    ) -> bool:
        """Send a steering message to Pi mid-turn.

        Pi's RPC ``steer`` command injects a user message between tool calls
        within the current agent turn, so the model sees it before its next
        response.
        """
        state = self._session_states.get(session_key)
        if state is None or state.rpc is None:
            return False
        text = content if isinstance(content, str) else str(content)
        try:
            await state.rpc.send_command(
                {
                    "type": "steer",
                    "message": text,
                }
            )
            return True
        except Exception as exc:  # noqa: BLE001 — steer is best-effort; any failure surfaces as False
            logger.debug("PiExecutor: failed to enqueue steer message: %s", exc)
            return False

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None and state.rpc is not None:
            await state.rpc.close()

    async def interrupt_session(self, session_key: str) -> bool:
        state = self._session_states.get(session_key)
        if state is None or state.rpc is None:
            return False
        # Best-effort abort to halt the in-flight turn.
        try:
            await state.rpc.send_command({"type": "abort", "id": "abort"})
        except Exception as exc:  # noqa: BLE001 — abort is best-effort
            logger.debug("PiExecutor: interrupt abort failed: %s", exc)
        # Always drop the session so the next turn starts fresh and replays
        # full history. A resumed subprocess sends only the latest user
        # message, which would bypass the runner's "[System: interrupted]"
        # marker and silently continue the abandoned request. See
        # claude_sdk_executor.interrupt_session for the rationale.
        try:
            await self.close_session(session_key)
            return True
        except Exception as exc:  # noqa: BLE001 — close failures surface as False
            logger.debug("PiExecutor: session close after interrupt failed: %s", exc)
            return False

    async def close(self) -> None:
        keys = list(self._session_states.keys())
        for key in keys:
            await self.close_session(key)
        if self._tool_server is not None:
            await self._tool_server.stop()
            self._tool_server = None

    def _session_key(self, messages: list[Message]) -> str:
        if messages:
            last = messages[-1]
            if last.get("session_id"):
                return str(last["session_id"])
            meta = last.get("metadata", {})
            if meta.get("session_id"):
                return str(meta["session_id"])
        return "__default__"

    def _resolve_model(self, config: ExecutorConfig | None) -> str | None:
        """
        Determine the model name to pass to Pi.

        ``cfg.model`` (per-request /model override) wins over the spec
        default (``HARNESS_PI_MODEL`` → ``self._model_override``). On the
        Databricks-profile gateway path a missing model falls back to
        :data:`DATABRICKS_CLAUDE_DEFAULT_MODEL` — Pi's own default is an
        Anthropic-direct id the gateway rejects. Elsewhere ``None`` falls
        through to let Pi pick its own default.

        :param config: Optional :class:`ExecutorConfig` whose ``model``
            takes precedence when set.
        :returns: The resolved model string, or ``None`` when both
            sources are unset off the Databricks-profile gateway path.
        """
        cfg = config or ExecutorConfig()
        model = cfg.model or self._model_override
        if model is None and self._gateway_uses_databricks_profile:
            return DATABRICKS_CLAUDE_DEFAULT_MODEL
        return model

    async def _ensure_tool_server(self, tools: list[ToolSpec]) -> int | None:
        """Start the TCP tool server if there are Omnigent tools to bridge."""
        if not tools:
            return None
        if self._tool_server is None:
            self._tool_server = _ToolServer()
            await self._tool_server.start()
        self._tool_server._tool_executor = self._tool_executor
        self._tool_server._policy_gate = self._gate_native_tool
        return self._tool_server.port

    async def _gate_native_tool(  # type: ignore[explicit-any]
        self,
        name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Evaluate a native Pi tool call against Omnigent TOOL_CALL policy.

        Bridges the tool server's :attr:`_ToolServer._policy_gate` to the
        ``_policy_evaluator`` the harness scaffold installs on this executor
        (the same round-trip the Claude SDK executor uses for LLM_REQUEST /
        LLM_RESPONSE policies). Mirrors the claude-native / codex-native
        PreToolUse hooks: the verdict is computed by the Omnigent server
        against the session's full policy set (inherited parent session
        policies + the agent spec's guardrails).

        :param name: Native tool name, e.g. ``"read"`` or ``"bash"``.
        :param args: The tool's argument dict.
        :returns: ``{"block": bool, "reason": str}`` — block on DENY.
            Allow when no evaluator is wired (single-process / test paths).
            TOOL_CALL ASK is collapsed to ALLOW/DENY server-side before the
            verdict returns, so only DENY blocks here.
        """
        # ``_policy_evaluator`` is installed best-effort by the harness
        # scaffold's executor adapter; absent on single-process / pre-turn
        # paths. Same fetch pattern as the Claude SDK executor.
        evaluator = getattr(self, "_policy_evaluator", None)
        if evaluator is None:
            return {"block": False, "reason": ""}
        verdict = await evaluator("PHASE_TOOL_CALL", {"name": name, "arguments": args})
        if verdict.action == "POLICY_ACTION_DENY":
            return {"block": True, "reason": verdict.reason or "blocked by policy"}
        return {"block": False, "reason": ""}

    def _build_env_and_dir(
        self,
        tools: list[ToolSpec],
        tool_server_port: int | None,
        tool_server_token: str | None,
        model: str | None,
    ) -> PiSubprocessConfig:
        """Build env dict, temp dir, and extra CLI args for a Pi subprocess.

        :param tools: Omnigent tool schemas to bridge into Pi.
        :param tool_server_port: TCP port the Omnigent tool server is
            listening on, or ``None`` if no tools need bridging.
        :param tool_server_token: The tool server's bearer token
            (:attr:`_ToolServer.token`), embedded in the generated
            extension so requests authenticate. Non-``None`` whenever
            *tool_server_port* is.
        :param model: The resolved model id this subprocess will run, e.g.
            ``"moonshotai/kimi-k2.6"``; registered in the generated
            ``models.json`` on the gateway path so the
            ``provider/<model>`` selector resolves. ``None`` when no model
            is pinned (Pi picks its own default).
        """
        env = dict(self._env)
        tmp_dir = tempfile.mkdtemp(prefix="omnigent_pi_")
        extra_args: list[str] = list(self._extra_args)

        if self._gateway:
            models_json = _build_models_json(
                self._databricks_host,
                self._databricks_token,
                self._base_urls_override,
                model=model,
            )
            models_path = os.path.join(tmp_dir, "models.json")
            with open(models_path, "w") as f:
                json.dump(models_json, f)
            env["PI_CODING_AGENT_DIR"] = tmp_dir

        # Pi natively supports retry config via ``.pi/settings.json``
        # (see ``RetryPolicy.pi.settings()`` for schema). Write the
        # retry block to ``<tmp_dir>/.pi/settings.json`` so Pi picks
        # it up when spawned with ``cwd=tmp_dir`` (when the user
        # didn't override cwd) or merge into ``<cwd>/.pi/settings.json``
        # when they did. This sets max_retries, backoff base/cap from
        # the spec policy.
        retry_settings = self._retry_policy.pi.settings()
        # Decide where to write. Project-level (cwd/.pi/settings.json)
        # is Pi's documented project override; merge into existing
        # file if present, else create.
        settings_dir_root = self._cwd or tmp_dir
        settings_path = os.path.join(settings_dir_root, ".pi", "settings.json")
        try:
            if os.path.exists(settings_path):
                with open(settings_path) as f:
                    existing_settings = json.load(f)
                if not isinstance(existing_settings, dict):
                    existing_settings = {}
            else:
                existing_settings = {}
            existing_settings.update(retry_settings)
            os.makedirs(os.path.dirname(settings_path), exist_ok=True)
            with open(settings_path, "w") as f:
                json.dump(existing_settings, f, indent=2)
        except OSError:
            # If we can't write to the user's cwd (read-only fs,
            # permissions), fall back to tmp_dir; Pi will use its
            # own defaults if neither location works.
            fallback_path = os.path.join(tmp_dir, ".pi", "settings.json")
            os.makedirs(os.path.dirname(fallback_path), exist_ok=True)
            with open(fallback_path, "w") as f:
                json.dump(retry_settings, f, indent=2)

        # Generate the Omnigent tool bridge extension if tools are available.
        if tools and tool_server_port is not None:
            if tool_server_token is None:
                # A port without a token would spawn the bridge
                # unauthenticated; fail loud instead.
                raise ValueError("tool_server_token is required when tool_server_port is set")
            ext_path = os.path.join(tmp_dir, "omnigent_tools.js")
            with open(ext_path, "w") as f:
                f.write(_generate_extension_js(tool_server_port, tools, tool_server_token))
            extra_args.extend(["--extension", ext_path])
            # Allowlist the bridged tool names. ``--no-tools`` (set in
            # __init__) disables every tool by default in pi 0.68+;
            # ``--tools`` adds specific names back. Without this pass
            # the bridge extension's tools register but pi never
            # exposes them to the LLM — symptom: model replies "I
            # don't have a calculate tool available."
            tool_names = [
                name for name in (s.get("name") for s in tools) if isinstance(name, str) and name
            ]
            # Pi's ``formatSkillsForPrompt`` (system-prompt.js:33,112)
            # gates skill-index injection on ``selectedTools`` including
            # ``"read"``. Pi's native ``read`` is a local filesystem read
            # that runs in-process and never traverses the bridged /mcp
            # path — so enabling it lets the model see (and load) the skills
            # we wired via ``--skill <path>``. As a native tool it would
            # otherwise escape all guardrails, so the generated extension's
            # ``tool_call`` hook routes it (and any other native tool) through
            # an Omnigent TOOL_CALL policy verdict; see
            # :func:`_generate_extension_js` and
            # :meth:`PiExecutor._gate_native_tool`.
            if self._skills_filter != "none":
                tool_names.append("read")
            if tool_names:
                extra_args.extend(["--tools", ",".join(tool_names)])

        return PiSubprocessConfig(env=env, tmp_dir=tmp_dir, extra_args=extra_args)

    async def _ensure_rpc(
        self,
        session_key: str,
        system_prompt: str,
        model: str | None,
        tools: list[ToolSpec],
    ) -> _PiRpcSession:
        """Get or create a Pi RPC subprocess for the given session."""
        state = self._session_states.setdefault(session_key, _PiSessionState())

        if (
            state.rpc is not None
            and state.rpc.process is not None
            and state.rpc.process.returncode is None
            and state.system_prompt == system_prompt
            and state.model == model
        ):
            return state.rpc

        if state.rpc is not None:
            await state.rpc.close()

        tool_server_port = await self._ensure_tool_server(tools)
        tool_server_token = self._tool_server.token if self._tool_server is not None else None
        subprocess_config = self._build_env_and_dir(
            tools, tool_server_port, tool_server_token, model
        )
        env = subprocess_config.env
        tmp_dir = subprocess_config.tmp_dir
        extra_args = subprocess_config.extra_args

        rpc = _PiRpcSession()
        rpc._tmp_dir = tmp_dir

        # For Databricks models, prefix with the provider name so Pi resolves
        # the model from our custom provider in models.json.
        pi_model: str | None
        if self._gateway and model:
            provider = _pi_provider_for_model(model)
            pi_model = f"{provider}/{model}"
        else:
            pi_model = model

        await rpc.start(
            self._pi_launch_path,
            env=env,
            cwd=self._cwd,
            model=pi_model or None,
            system_prompt=system_prompt or None,
            extra_args=extra_args or None,
        )
        state.rpc = rpc
        state.system_prompt = system_prompt
        state.model = model
        state._has_sent_prompt = False
        return rpc

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        if self._gateway:
            if self._gateway_host_override is None:
                creds = _read_databrickscfg(self._databricks_profile)
                if creds is not None:
                    self._databricks_token = creds.token
            else:
                assert self._gateway_auth_command is not None
                token = _fetch_shell_command_token(self._gateway_auth_command)
                if token:
                    self._databricks_token = token
        session_key = self._session_key(messages)
        model = self._resolve_model(config)

        try:
            rpc = await self._ensure_rpc(session_key, system_prompt, model, tools)
        except Exception as exc:  # noqa: BLE001 — executor boundary surfaces startup errors as ExecutorError
            yield ExecutorError(message=f"Failed to start Pi: {exc}")
            return

        # Build the prompt to send to Pi.  On the first turn of a new Pi
        # process, if there are prior messages (e.g. parent history passed
        # to a sub-agent), we serialize the full conversation so Pi has
        # context.  On subsequent turns the Pi process already has context,
        # so we only send the latest user message.
        state = self._session_states.get(session_key)
        is_first_turn = state is not None and not state._has_sent_prompt

        prompt = _build_pi_prompt(messages, is_first_turn=is_first_turn)

        if not prompt:
            # No prompt built (e.g. empty message list on a resumed session) —
            # end the turn with no assistant text rather than fabricating one.
            yield TurnComplete(response=None)
            return

        if state is not None:
            state._has_sent_prompt = True

        # Send prompt command. Pi's JSONL protocol carries text in ``message``
        # and images in a native ``images`` field. Split multimodal blocks into
        # both (rather than JSON-encoding them into the text) so the model
        # actually receives attached images instead of a base64 text blob (#515).
        message: str
        images: list[dict[str, str]] = []
        if isinstance(prompt, list):
            message, images = _split_pi_prompt(prompt)
        else:
            message = prompt
        cmd_id = f"turn_{id(messages)}"
        command: dict[str, Any] = {"type": "prompt", "message": message, "id": cmd_id}
        if images:
            command["images"] = images
        try:
            await rpc.send_command(command)
        except Exception as exc:  # noqa: BLE001 — executor boundary surfaces prompt-send errors as ExecutorError
            yield ExecutorError(message=f"Failed to send prompt to Pi: {exc}")
            return

        # Read events until agent_end.
        response_text = ""
        streamed_any = False
        # Per-LLM-call token usage captured from each assistant message pi
        # forwards (``message_end`` is the capture site; ``agent_end`` is a
        # fallback). Summed into a turn-level usage dict at completion so a
        # multi-step (tool-loop) turn bills for every call, not just the
        # last. Empty when pi reports no usage — cost tracking is skipped.
        message_usages: list[dict[str, Any]] = []  # type: ignore[explicit-any]

        while True:
            line = await rpc.read_line(timeout=120.0)
            if line is None:
                if not streamed_any and not response_text:
                    stderr = "\n".join(rpc._stderr_lines) if rpc._stderr_lines else ""
                    stderr_suffix = f" Stderr: {stderr}" if stderr else ""
                    yield ExecutorError(
                        message=f"Pi process ended without response.{stderr_suffix}"
                    )
                else:
                    turn_usage = _aggregate_pi_turn_usage(message_usages, model)
                    _notify_usage_from_dict(model=model, usage=turn_usage)
                    yield TurnComplete(response=response_text, usage=turn_usage)
                return

            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                logger.debug("PiExecutor: non-JSON line: %s", line[:200])
                continue

            raw_event_type = event.get("type")
            event_type: str | None = raw_event_type if isinstance(raw_event_type, str) else None

            # Skip the command-ack response.
            if event_type == "response":
                if not event.get("success", True):
                    yield ExecutorError(message=event.get("error", "Pi command failed"))
                    return
                continue

            # Streaming text and thinking deltas.
            if event_type == "message_update":
                ame = event.get("assistantMessageEvent", {})
                ame_type = ame.get("type")
                if ame_type == "text_delta":
                    raw_delta = ame.get("delta")
                    if isinstance(raw_delta, str) and raw_delta:
                        yield TextChunk(text=raw_delta)
                        response_text += raw_delta
                        streamed_any = True
                elif ame_type == "thinking_start":
                    # Anchors the "Thinking…" indicator before the first delta.
                    yield ReasoningChunk(delta="", event_type="reasoning_started")
                elif ame_type == "thinking_delta":
                    raw_delta = ame.get("delta")
                    if isinstance(raw_delta, str) and raw_delta:
                        # Reasoning stays out of response_text — it is not assistant text.
                        yield ReasoningChunk(delta=raw_delta, event_type="reasoning_text")
                continue

            # Tool execution events.
            if event_type == "tool_execution_start":
                tool_name = event.get("toolName", "unknown")
                args = event.get("args", {})
                yield ToolCallRequest(
                    name=tool_name,
                    args=args if isinstance(args, dict) else {},
                )
                continue

            if event_type == "tool_execution_end":
                tool_name = event.get("toolName", "unknown")
                is_error = event.get("isError", False)
                result = event.get("result")
                # Pi may report isError at the top level OR only inside
                # the result dict (result.isError).  Check both.
                if isinstance(result, dict) and result.get("isError"):
                    is_error = True
                # Detect policy-blocked results coming back from the tool
                # server.  The _execute_tool callback returns
                # {"blocked": True, "reason": "..."} when a policy blocks
                # the call.  By the time it reaches us, the result may be:
                #   - a dict with "blocked" key (direct)
                #   - a dict with "content" list containing JSON text (from Pi extension)
                #   - a string containing JSON with "blocked" key
                result_str = str(result) if result is not None else ""
                is_blocked = False

                def _check_blocked(obj: JsonValue) -> BlockedCheck:
                    """Check if obj represents a blocked tool result."""
                    if isinstance(obj, dict):
                        if obj.get("blocked"):
                            return BlockedCheck(blocked=True, reason=str(obj.get("reason", obj)))
                        # Pi extension wraps in {content: [{type:"text", text:"..."}]}
                        content = obj.get("content")
                        if isinstance(content, list):
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text = item.get("text")
                                    if not isinstance(text, str):
                                        continue
                                    try:
                                        parsed = json.loads(text)
                                        if isinstance(parsed, dict) and parsed.get("blocked"):
                                            return BlockedCheck(
                                                blocked=True,
                                                reason=str(parsed.get("reason", text)),
                                            )
                                    except (json.JSONDecodeError, TypeError):
                                        pass
                    elif isinstance(obj, str):
                        try:
                            parsed = json.loads(obj)
                            if isinstance(parsed, dict) and parsed.get("blocked"):
                                return BlockedCheck(
                                    blocked=True,
                                    reason=str(parsed.get("reason", obj)),
                                )
                        except (json.JSONDecodeError, TypeError):
                            pass
                    return BlockedCheck(blocked=False, reason="")

                if is_error:
                    check = _check_blocked(result)
                    is_blocked = check.blocked
                    if is_blocked:
                        result_str = check.reason

                if is_blocked:
                    status = ToolCallStatus.BLOCKED
                elif is_error:
                    status = ToolCallStatus.ERROR
                else:
                    status = ToolCallStatus.SUCCESS

                yield ToolCallComplete(
                    name=tool_name,
                    status=status,
                    result=result,
                    error=result_str if (is_error or is_blocked) else "",
                )
                continue

            # Agent ended — the turn is complete.
            if event_type == "agent_end":
                end_messages = event.get("messages", [])
                if not response_text:
                    for m in reversed(end_messages):
                        if m.get("role") == "assistant":
                            content = m.get("content", [])
                            if isinstance(content, str):
                                response_text = content
                            elif isinstance(content, list):
                                text_parts: list[str] = []
                                for part in content:
                                    if not (isinstance(part, dict) and part.get("type") == "text"):
                                        continue
                                    part_text = part.get("text")
                                    if isinstance(part_text, str):
                                        text_parts.append(part_text)
                                response_text = "".join(text_parts)
                            break
                # Fallback usage capture: if no ``message_end`` carried
                # usage, pull it from the last assistant message in
                # ``messages`` (only the last — ``messages`` may hold the
                # whole conversation, so summing it would overcount).
                if not message_usages and isinstance(end_messages, list):
                    for m in reversed(end_messages):
                        captured = _extract_pi_turn_usage(m, model)
                        if captured is not None:
                            message_usages.append(captured)
                            break
                turn_usage = _aggregate_pi_turn_usage(message_usages, model)
                _notify_usage_from_dict(model=model, usage=turn_usage)
                yield TurnComplete(response=response_text, usage=turn_usage)
                return

            # message_end carries one completed assistant message, whose
            # ``usage`` object holds that LLM call's token counts — collect
            # each for the turn-level sum before handling error stop reasons.
            if event_type == "message_end":
                msg = event.get("message", {})
                if isinstance(msg, dict):
                    captured = _extract_pi_turn_usage(msg, model)
                    if captured is not None:
                        message_usages.append(captured)
                    raw_stop = msg.get("stopReason")
                    stop: str | None = raw_stop if isinstance(raw_stop, str) else None
                    if stop in ("error", "aborted"):
                        err = msg.get("errorMessage", stop)
                        yield ExecutorError(message=str(err))
                        return
                continue

            logger.debug("PiExecutor: ignoring event type=%s", event_type)
