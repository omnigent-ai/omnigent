"""Shared subagent-routing decision logic for harness hooks.

Stdlib-only on purpose (bar the equally light
:mod:`omnigent.claude_model_vocabulary`): the Claude-native hook runs as
a per-spawn subprocess (``python -I -m
omnigent.inner.hook_scripts.claude_router_hook``) and blocks the spawn,
so importing anything heavier would show up as spawn latency. The
claude-agent-sdk executor imports the same functions for its in-process
``PreToolUse`` callback, so both paths map decisions identically.

The runner advertises its ``route-subagent`` endpoint by writing
``subagent_router.json`` (``{"url": ..., "token": ..., "pid": ...}``) into
the session bridge directory. A missing, malformed, non-loopback or
dead-pid advertisement means the router is unreachable: the hook allows
the spawn unchanged and emits nothing.

Every ``explicit-any`` type-ignore below marks the same thing — hook
payloads, request bodies and hook outputs are untrusted JSON with no
schema this process can import — so the per-site justifications are
omitted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.claude_model_vocabulary import (
    CLAUDE_MODEL_ALIASES,
    alias_pins,
    claude_model_alias,
    normalized_model_id,
)

# Advertisement file written by the runner's subagent-routing endpoint,
# mirroring the ``tool_relay.json`` discovery pattern.
ADVERTISEMENT_FILE = "subagent_router.json"
# Claude-native bridge config, read for the session id / launch model.
_BRIDGE_CONFIG_FILE = "bridge.json"
# Per-turn relay advertisement listing the Omnigent tools this session holds.
# Same filename as ``claude_native_bridge._TOOL_RELAY_FILE``; duplicated
# because this module is stdlib-only and must not import the bridge.
_TOOL_RELAY_FILE = "tool_relay.json"
# MCP server the bridge registers the Omnigent tools under. Must match
# ``claude_native_bridge._MCP_SERVER_NAME`` (and the codex-native
# ``[mcp_servers.omnigent]`` table), which the same no-import rule forbids
# reading directly.
_MCP_SERVER_NAME = "omnigent"

# Harnesses whose tool list spells an MCP tool ``mcp__<server>__<tool>``.
_MCP_PREFIXING_HARNESSES = frozenset({"claude-sdk", "claude_sdk", "claude-native"})
# Harnesses that address an MCP tool by its bare name plus a separate
# server/namespace field, so no prefixed spelling can be quoted at them.
# Codex flattens the pair for its own logs and hook payloads
# (``omnigentsys_session_create``), but that spelling is not callable.
_MCP_NAMESPACED_HARNESSES = frozenset({"codex", "codex-native"})

# Omnigent tools a routed spawn needs. Named in the deny reason only when the
# session's relay actually advertises the create tool.
_SPAWN_TOOL = "sys_session_create"
_AGENT_LIST_TOOL = "sys_agent_list"

# Explicit advertisement directory. Set for harnesses that have no
# claude-native bridge dir (e.g. the claude-agent-sdk executor).
ROUTER_DIR_ENV_VAR = "OMNIGENT_SUBAGENT_ROUTER_DIR"
# Session the spawn belongs to, when the harness knows it out of band.
SESSION_ID_ENV_VAR = "OMNIGENT_SUBAGENT_ROUTER_SESSION_ID"
# Claude-native bridge discovery, already exported to the harness.
BRIDGE_DIR_ENV_VAR = "HARNESS_CLAUDE_NATIVE_BRIDGE_DIR"
NATIVE_SESSION_ID_ENV_VAR = "HARNESS_CLAUDE_NATIVE_REQUEST_SESSION_ID"

# Claude Code's subagent-spawn tool. ``Agent`` is the current name;
# ``Task`` was renamed to it in CLI 2.1.63 and still works as an alias,
# so both are matched. Also used verbatim as the settings/SDK matcher —
# Claude Code reads a pipe-separated list as exact alternatives.
AGENT_TOOL_NAMES = ("Agent", "Task")
AGENT_TOOL_MATCHER = "|".join(AGENT_TOOL_NAMES)

# Hosts an advertised router URL may name. The advertisement lives in an
# agent-writable directory, so anything else is a self-approval or
# exfiltration target rather than our own runner.
#
# Advisory only: these checks stop an off-box exfiltration target and a
# stale port, not a same-uid agent, which can bind its own loopback port
# and advertise a live pid. Routing is an advisory gate, not a sandbox.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})

# Hop 2 of the routing timeout budget documented in
# ``omnigent.runner.subagent_routing``: larger than the runner's own wait so
# the runner answers first, smaller than the harness's hook timeout so this
# script's fail-open branch can run.
#
# Single digits on purpose. This gate holds the parent agent's spawn tool open
# until it answers, so a fail-open that takes half a minute stalls the agent as
# surely as an error would; the routing call inside it runs on a 5s budget
# (``omnigent.server.smart_routing.ROUTING_REQUEST_TIMEOUT_S``).
REQUEST_TIMEOUT_S = 8.0

# Headroom hop 1 adds over hop 2. Enough for interpreter start-up and the
# script's fail-open branch, and no more — every second here is a second the
# harness waits after the request has already given up.
HOOK_TIMEOUT_HEADROOM_S = 4.0

# Hop 1: the budget a harness registers on its own spawn hook. STRICTLY larger
# than ``REQUEST_TIMEOUT_S`` — a hook killed at the same instant its HTTP call
# gives up never reaches its fail-open branch, and the harness sees a dead hook
# instead of "no opinion". The claude-native and codex entries derive the same
# headroom; the in-process claude-sdk hook reads this constant.
HOOK_TIMEOUT_S = REQUEST_TIMEOUT_S + HOOK_TIMEOUT_HEADROOM_S

# ``tool_input`` keys naming the requested subagent, in preference order.
# Claude Code sends ``subagent_type``; codex sends ``task_name`` /
# ``agent_name``.
DEFAULT_TASK_KEYS: tuple[str, ...] = ("subagent_type",)

# Flags every harness hook entrypoint accepts. None is required: a hook
# misconfiguration must degrade to "no opinion", not an argparse exit.
STANDARD_HOOK_FLAGS: tuple[str, ...] = (
    "--bridge-dir",
    "--router-dir",
    "--session-id",
    "--harness",
)


@dataclass(frozen=True)
class RouterEndpoint:
    """Advertised ``route-subagent`` endpoint."""

    url: str
    token: str
    session_id: str | None = None


def discover_router_dir(bridge_dir: str | Path | None = None) -> Path | None:
    """
    Locate the directory holding the router advertisement.

    :param bridge_dir: Explicit directory, e.g. the ``--router-dir`` /
        ``--bridge-dir`` argv value. ``None`` falls back to
        :data:`ROUTER_DIR_ENV_VAR` then :data:`BRIDGE_DIR_ENV_VAR`.
    :returns: Directory path, or ``None`` when nothing advertises one.
    """
    if bridge_dir:
        return Path(bridge_dir)
    for env_var in (ROUTER_DIR_ENV_VAR, BRIDGE_DIR_ENV_VAR):
        raw = os.environ.get(env_var, "").strip()
        if raw:
            return Path(raw)
    return None


def read_router_endpoint(
    router_dir: str | Path | None,
    *,
    filename: str = ADVERTISEMENT_FILE,
) -> RouterEndpoint | None:
    """
    Read the advertised endpoint.

    The advertisement lives in an agent-writable directory, so it is
    validated before anything is sent to it: the URL must be plain ``http``
    on a loopback address, and the advertising process must still be alive
    (a stale entry's port can be re-bound by another local process). Either
    check failing means "router unreachable".

    :param router_dir: Directory containing *filename*.
    :param filename: Advertisement file name. Defaults to
        :data:`ADVERTISEMENT_FILE`; the codex ``route-turn`` hook passes
        its own so both endpoints share this validation.
    :returns: Endpoint, or ``None`` when the advertisement is absent,
        malformed, or fails validation.
    """
    if router_dir is None:
        return None
    try:
        raw = (Path(router_dir) / filename).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    token = payload.get("token")
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        return None
    # The rejected URL itself is never echoed: the advertisement it came from
    # also carries the bearer token, so anything derived from it stays out of
    # logs. The file name plus the reason is enough to find it on disk.
    if not _is_loopback_url(url):
        _diagnose(f"ignoring the router advertised in {filename}: not plain http on loopback")
        return None
    if not _advertiser_alive(payload.get("pid")):
        _diagnose(f"ignoring the router advertised in {filename}: advertiser pid not alive")
        return None
    session_id = payload.get("session_id")
    return RouterEndpoint(
        url=url.rstrip("/"),
        token=token,
        session_id=session_id if isinstance(session_id, str) and session_id else None,
    )


def _diagnose(message: str) -> None:
    """Print a routing diagnostic to stderr.

    The hook has no logger (stdlib-only, runs as a short-lived subprocess)
    and its stdout is the harness's hook protocol, so stderr is the only
    channel a user can see why routing silently fell open.
    """
    print(f"omnigent subagent router: {message}", file=sys.stderr)


def _is_loopback_url(url: str) -> bool:
    """Report whether *url* is plain HTTP on a loopback address."""
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
    except ValueError:
        return False
    return parsed.scheme == "http" and host in _LOOPBACK_HOSTS


def _advertiser_alive(pid: Any) -> bool:  # type: ignore[explicit-any]
    """Report whether the advertised runner process still exists.

    The runner always writes ``pid``, so a missing or malformed one means
    the advertisement was not written by us — rejected rather than
    trusted. On Windows ``os.kill(pid, 0)`` is unreliable, so the liveness
    probe itself is skipped there and the pid's presence is all that is
    checked. The probe is also blind inside a PID-namespaced sandbox
    (``--unshare-pid``), where the runner's pid is simply not visible; a
    hook running there sees "unreachable" and falls open on the inherited
    model.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not hasattr(os, "getuid"):
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        # EPERM: alive but owned by someone else. That is not our router
        # either, but the bearer token still gates the request.
        return True
    return True


def resolve_session_id(
    endpoint: RouterEndpoint,
    *,
    bridge_dir: str | Path | None = None,
) -> str | None:
    """
    Resolve the Omnigent session the spawn belongs to.

    :param endpoint: Advertised endpoint, which may carry the session id.
    :param bridge_dir: Claude-native bridge directory, read as a last
        resort (``bridge.json`` tracks the active session across
        ``/clear`` rotations).
    :returns: Session id, e.g. ``"conv_abc123"``, or ``None``.
    """
    if endpoint.session_id:
        return endpoint.session_id
    for env_var in (SESSION_ID_ENV_VAR, NATIVE_SESSION_ID_ENV_VAR):
        raw = os.environ.get(env_var, "").strip()
        if raw:
            return raw
    config = _read_bridge_config(bridge_dir)
    for key in ("active_session_id", "conversation_id"):
        value = config.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def resolve_parent_model(bridge_dir: str | Path | None) -> str | None:
    """
    Resolve the model the parent session runs on.

    :param bridge_dir: Claude-native bridge directory whose
        ``bridge.json`` records the launch model.
    :returns: Gateway model name, or ``None`` when unknown.
    """
    model = _read_bridge_config(bridge_dir).get("launch_model")
    return model if isinstance(model, str) and model else None


def resolve_model_vocabulary_env(bridge_dir: str | Path | None) -> Mapping[str, str] | None:
    """
    Resolve the session's alias pinning for model translation.

    :param bridge_dir: Claude-native bridge directory whose
        ``bridge.json`` records the launch env's model keys.
    :returns: The recorded ``{env var: model id}`` mapping, or ``None``
        to fall back to this process's environment (a hook subprocess
        inherits the CLI's).
    """
    model_env = _read_bridge_config(bridge_dir).get("model_env")
    if not isinstance(model_env, dict):
        return None
    resolved = {
        str(key): str(value)
        for key, value in model_env.items()
        if isinstance(key, str) and isinstance(value, str) and value
    }
    return resolved or None


def _read_bridge_config(
    bridge_dir: str | Path | None,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    if bridge_dir is None:
        return {}
    try:
        config = json.loads((Path(bridge_dir) / _BRIDGE_CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return config if isinstance(config, dict) else {}


def is_agent_tool(tool_name: Any) -> bool:  # type: ignore[explicit-any]
    """
    Report whether a hook payload names the subagent-spawn tool.

    :param tool_name: ``tool_name`` from the hook payload.
    :returns: ``True`` for Claude Code's ``Task`` / ``Agent`` tool.
    """
    return isinstance(tool_name, str) and tool_name in AGENT_TOOL_NAMES


def spawn_task_name(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    task_keys: Sequence[str] = DEFAULT_TASK_KEYS,
) -> str:
    """
    Read the requested subagent's name out of a spawn's ``tool_input``.

    :param tool_input: ``tool_input`` from the hook payload.
    :param task_keys: Keys to try, in preference order.
    :returns: The name, or ``""`` when the spawn names none (the server
        supplies the placeholder task; the hook does not invent one).
    """
    for key in task_keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def build_route_request(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    *,
    harness: str,
    parent_model: str | None = None,
    task_keys: Sequence[str] = DEFAULT_TASK_KEYS,
    include_prompt: bool = True,
    prompt_keys: Sequence[str] = ("prompt",),
    requested_model_resolver: Callable[[str], str | None] | None = None,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Build the ``route-subagent`` request body.

    :param tool_input: ``tool_input`` from the hook payload.
    :param harness: Requesting harness, e.g. ``"claude-native"``.
    :param parent_model: Model the parent session runs on, when known.
    :param task_keys: ``tool_input`` keys naming the subagent, in
        preference order.
    :param include_prompt: ``False`` sends ``prompt: null``, for harnesses
        whose spawn payload carries no usable task text.
    :param prompt_keys: ``tool_input`` keys carrying the task text, in
        preference order (codex spells it ``message``).
    :param requested_model_resolver: Turns the spawn tool's own ``model``
        spelling into a catalog id the server can compare. ``None``
        forwards it verbatim, which is what codex's slugs need.
    :returns: JSON-serializable request body.
    """
    prompt = None
    if include_prompt:
        for key in prompt_keys:
            value = tool_input.get(key)
            if isinstance(value, str) and value:
                prompt = value
                break
    requested = tool_input.get("model")
    requested = requested if isinstance(requested, str) and requested else None
    if requested is not None and requested_model_resolver is not None:
        requested = requested_model_resolver(requested)
    return {
        "harness": harness,
        "task_name": spawn_task_name(tool_input, task_keys),
        "prompt": prompt,
        "parent_model": parent_model,
        # The model the spawning agent asked for, if any. The router still
        # decides; the ask only shows up in the rationale and, on a mismatch,
        # as the recorded ``attempted_override``.
        "requested_model": requested,
    }


def request_decision(
    endpoint: RouterEndpoint,
    session_id: str,
    body: dict[str, Any],  # type: ignore[explicit-any]
    *,
    timeout: float = REQUEST_TIMEOUT_S,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """
    POST one routing request to the runner.

    :param endpoint: Advertised endpoint.
    :param session_id: Omnigent session id.
    :param body: Request body from :func:`build_route_request`.
    :param timeout: Socket timeout in seconds.
    :returns: Decoded decision, or ``None`` on any transport / decode
        failure (callers treat that as "allow unchanged").
    """
    url = f"{endpoint.url}/v1/sessions/{urllib.parse.quote(session_id, safe='')}/route-subagent"
    # Loopback runner URL read from the owner-only bridge dir.
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {endpoint.token}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None
    return payload if isinstance(payload, dict) else None


def _allow_with_model(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    model: str,
    reason: str,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    output: dict[str, Any] = {  # type: ignore[explicit-any]
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "updatedInput": {**tool_input, "model": model},
    }
    if reason:
        output["permissionDecisionReason"] = reason
    return {"hookSpecificOutput": output}


def _deny(reason: str) -> dict[str, Any]:  # type: ignore[explicit-any]
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def claude_model_translator(
    bridge_dir: str | Path | None,
) -> Callable[[str], str | None]:
    """
    Build the model translator for Claude's spawn tool.

    :param bridge_dir: Directory whose ``bridge.json`` records the
        session's alias pinning.
    :returns: Callable mapping a servable id to an accepted alias.
    """
    # Claude's Agent/Task ``model`` is a closed enum of family aliases, so a
    # catalog id ("databricks-claude-sonnet-5") fails its schema and the
    # spawn dies. Same vocabulary as ``/model``.
    vocabulary_env = resolve_model_vocabulary_env(bridge_dir)
    return lambda model: claude_model_alias(model, vocabulary_env)


def claude_requested_model_resolver(
    bridge_dir: str | Path | None,
) -> Callable[[str], str | None]:
    """
    Build the ask normalizer for Claude's spawn tool.

    Claude spells the Agent/Task ``model`` as a family alias, so forwarding
    it verbatim would compare ``"opus"`` against a catalog id and report
    every honored ask as overridden. Resolve the alias through the same
    pinning :func:`claude_model_translator` inverts, so the comparison is
    between two catalog ids.

    :param bridge_dir: Directory whose ``bridge.json`` records the
        session's alias pinning.
    :returns: Callable mapping a spawn's ``model`` to a catalog id, or
        ``None`` when the spelling names no concrete model (``"inherit"``,
        ``"default"``, an unpinned alias) — the body then claims no ask.
    """
    pins = alias_pins(resolve_model_vocabulary_env(bridge_dir))

    def resolve(model: str) -> str | None:
        candidate = model.strip().lower()
        if candidate in CLAUDE_MODEL_ALIASES:
            return pins.get(candidate)
        # A catalog id already compares; anything else (``inherit``,
        # ``default``, an unknown label) names no model, and claiming it as an
        # ask would report a phantom override.
        return model if normalized_model_id(model) != candidate else None

    return resolve


# Opening clause of every routed-spawn instruction. Naming the user's own
# choice, and framing the block as an approved re-route rather than a refusal,
# is what gets the model to follow through: a terse denial reads as a dead end
# and the model abandons the spawn instead of retrying (matrix row A-sub).
_SMART_ROUTING_PREAMBLE = (
    "Databricks Smart Routing is enabled for this session: the user chose to "
    "have each task run on the model best suited to it, and Smart Routing "
    "picked the model for this one."
)


def mcp_tool_name(bare: str, harness: str | None) -> str:
    """
    Spell an Omnigent tool the way *harness* advertises it.

    :param bare: Omnigent tool name, e.g. ``"sys_session_create"``.
    :param harness: Requesting harness, e.g. ``"claude-native"``. ``None``
        or an unrecognized value keeps the bare name — an invented prefix
        is worse than the name the agent spec already documents.
    :returns: The name to quote at the model.
    """
    if harness in _MCP_PREFIXING_HARNESSES:
        return f"mcp__{_MCP_SERVER_NAME}__{bare}"
    return bare


def advertised_relay_tools(bridge_dir: str | Path | None) -> frozenset[str]:
    """
    Read the Omnigent tool names this session's relay advertises.

    Defensive like :func:`read_router_endpoint`: any failure reads as
    "availability unknown" (an empty set), which callers treat as "assume
    the tool is there" rather than degrading a working instruction.

    :param bridge_dir: Directory containing :data:`_TOOL_RELAY_FILE`.
    :returns: Advertised tool names, or an empty set when unknown.
    """
    if bridge_dir is None:
        return frozenset()
    try:
        payload = json.loads((Path(bridge_dir) / _TOOL_RELAY_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    if not isinstance(payload, dict):
        return frozenset()
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return frozenset()
    return frozenset(
        name
        for spec in tools
        if isinstance(spec, dict) and isinstance((name := spec.get("name")), str) and name
    )


def _mcp_discovery_note(harness: str | None) -> str:
    """Explain where the named tools live and how to find them.

    Both native harnesses defer MCP schemas (Claude Code's tool search,
    codex's ``tool_search``), so the tool is absent from the up-front list
    and the model concludes it does not exist. Naming the server it comes
    from is what turns "no such tool" into a lookup.
    """
    if harness in _MCP_NAMESPACED_HARNESSES:
        return (
            f"Both come from the `{_MCP_SERVER_NAME}` MCP server already attached to "
            "this session, where they appear as "
            f"`{_MCP_SERVER_NAME}.{_SPAWN_TOOL}` / `{_MCP_SERVER_NAME}.{_AGENT_LIST_TOOL}` "
            "— call them by the bare names above with the server as the namespace, and "
            "do not run the two words together into one identifier. If they are not in "
            "your up-front tool list your MCP schemas are deferred: search your tools "
            "for the name instead of concluding it does not exist."
        )
    return (
        f"Both are MCP tools from the `{_MCP_SERVER_NAME}` server already attached to "
        "this session, not new tools you need to install. If they are not in your "
        "up-front tool list your MCP schemas are deferred: search your tools for the "
        "name instead of concluding it does not exist."
    )


def _no_spawn_tool_instruction(model: str, picked: str) -> str:
    """Deny reason for a session whose relay advertises no spawn tool.

    Names no tool at all: pointing at a tool the session does not hold is
    what made the model refuse in the first place.
    """
    del model
    return (
        f"{_SMART_ROUTING_PREAMBLE} It selected {picked} for this sub-task, which "
        "your built-in spawn tool cannot launch, and this session holds no Omnigent "
        "tool that can start one either. This is not an error and the sub-task is "
        "approved — do the work yourself on your current model instead of spawning, "
        "then continue."
    )


def routed_spawn_instruction(
    model: str,
    harness: str | None = None,
    *,
    requesting_harness: str | None = None,
    available_tools: frozenset[str] = frozenset(),
) -> str:
    """
    Build the instruction telling the model how to start a routed sub-task.

    Names ``sys_session_create`` — the tool a routed harness actually holds
    (``spawn: True`` grants it) — with parameters that exist on its schema,
    spelled the way *requesting_harness* advertises it. The previous wording
    named the bare tool at every harness, so a Claude session (which sees
    ``mcp__omnigent__sys_session_create``) reported the tool as nonexistent
    and dropped the sub-task.

    :param model: Model Smart Routing selected, e.g. ``"databricks-glm-5-2"``.
    :param harness: Harness it runs on, when the pick crosses harnesses.
    :param requesting_harness: Harness whose model reads this, which decides
        the tool spelling. ``None`` falls back to bare names.
    :param available_tools: Omnigent tools the session's relay advertises.
        Empty means "unknown", which assumes the spawn tool is there.
    :returns: Deny reason that reads as an approved, actionable re-route.
    """
    picked = f"{model} (harness {harness})" if harness else model
    if available_tools and _SPAWN_TOOL not in available_tools:
        return _no_spawn_tool_instruction(model, picked)
    create = mcp_tool_name(_SPAWN_TOOL, requesting_harness)
    agent_list = mcp_tool_name(_AGENT_LIST_TOOL, requesting_harness)
    return (
        f"{_SMART_ROUTING_PREAMBLE} It selected {picked} for this sub-task, "
        "which your built-in spawn tool cannot launch. This is not an error "
        f"and the sub-task is approved — start it with {create} "
        "instead, which does accept the routed model: "
        f'{create}(agent_id="<id from {agent_list}>", '
        f'model="{model}", message="<the task you were about to spawn>"). '
        f"{_mcp_discovery_note(requesting_harness)} Make that call now, then continue."
    )


def smart_routing_spawn_note(harness: str) -> str:
    """
    Build the launch-time system-prompt note about routed spawns.

    Lives next to :func:`routed_spawn_instruction` so the note and the deny
    reason name the same tool in the same spelling — a session told about
    one tool and denied with another is back to refusing the sub-task.

    :param harness: Harness being launched, e.g. ``"claude-native"``.
    :returns: Instructions to append to the session's system prompt.
    """
    create = mcp_tool_name(_SPAWN_TOOL, harness)
    agent_list = mcp_tool_name(_AGENT_LIST_TOOL, harness)
    return (
        "Databricks Smart Routing is enabled for this session: each sub-task may be "
        "placed on a different model than yours. When your built-in spawn tool "
        f"(Agent/Task/spawn_agent) is denied with an instruction naming {create}, "
        "that is an approved re-route, not an error or a permission problem — make "
        f"the {create} call as instructed, using {agent_list} for the agent id. "
        f"{_mcp_discovery_note(harness)}"
    )


def redirect_reason(
    harness: str,
    model: str,
    *,
    requesting_harness: str | None = None,
    available_tools: frozenset[str] = frozenset(),
) -> str:
    """
    Build the cross-harness redirect instruction shown to the model.

    :param harness: Harness the router picked, e.g. ``"codex"``.
    :param model: Model the router picked.
    :param requesting_harness: Harness whose model reads this.
    :param available_tools: Omnigent tools the session's relay advertises.
    :returns: Deny reason telling the model how to respawn correctly.
    """
    return routed_spawn_instruction(
        model,
        harness,
        requesting_harness=requesting_harness,
        available_tools=available_tools,
    )


def decision_to_hook_output(
    decision: dict[str, Any],  # type: ignore[explicit-any]
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    *,
    model_translator: Callable[[str], str | None] | None = None,
    requesting_harness: str | None = None,
    available_tools: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """
    Map a ``route-subagent`` decision to Claude ``PreToolUse`` output.

    :param decision: Decoded endpoint response.
    :param tool_input: Original ``tool_input``, preserved on rewrite.
    :param model_translator: Converts the decision's servable model id
        into the spawn tool's own ``model`` vocabulary, returning ``None``
        when it maps to nothing the tool accepts (the spawn is then
        allowed unchanged — a degraded model beats a dead spawn, and an
        unacceptable value beats neither). ``None`` injects the id as-is,
        which is what codex's ``spawn_agent`` expects.
    :param requesting_harness: Harness whose model reads a deny reason,
        which decides how the Omnigent tools are spelled.
    :param available_tools: Omnigent tools the session's relay advertises,
        so a deny never names one the session does not hold.
    :returns: Hook output, or ``None`` for "no opinion" (allow the spawn
        unchanged with no emitted decision).
    """
    action = decision.get("action")
    model = decision.get("model")
    rationale = decision.get("rationale")
    rationale = rationale if isinstance(rationale, str) else ""
    if action == "rewrite" and isinstance(model, str) and model:
        if model_translator is None:
            return _allow_with_model(tool_input, model, rationale)
        translated = model_translator(model)
        if translated is None:
            return None
        if translated != model:
            rationale = f"{rationale} (applied as {translated!r})".strip()
        return _allow_with_model(tool_input, translated, rationale)
    if action == "redirect":
        harness = decision.get("harness")
        if isinstance(harness, str) and harness and isinstance(model, str) and model:
            return _deny(
                redirect_reason(
                    harness,
                    model,
                    requesting_harness=requesting_harness,
                    available_tools=available_tools,
                )
            )
        # A redirect without a target can't be followed — fail open.
        return None
    if action == "deny":
        # A bare "denied" leaves the model nowhere to go, so it drops the
        # sub-task. When the verdict names a model, hand back the same
        # actionable re-route the redirect path uses.
        if isinstance(model, str) and model:
            return _deny(
                routed_spawn_instruction(
                    model,
                    requesting_harness=requesting_harness,
                    available_tools=available_tools,
                )
            )
        return _deny(
            rationale
            or (
                f"{_SMART_ROUTING_PREAMBLE} It could not place this sub-task on "
                "a model your built-in spawn tool can run. Start it with "
                f"{mcp_tool_name(_SPAWN_TOOL, requesting_harness)} instead, or "
                "continue the work yourself."
            )
        )
    return None


def route_pre_tool_use(
    payload: dict[str, Any],  # type: ignore[explicit-any]
    *,
    harness: str,
    router_dir: str | Path | None = None,
    bridge_dir: str | Path | None = None,
    parent_model: str | None = None,
    session_id: str | None = None,
    timeout: float = REQUEST_TIMEOUT_S,
    tool_matcher: Callable[[Any], bool] = is_agent_tool,  # type: ignore[explicit-any]
    task_keys: Sequence[str] = DEFAULT_TASK_KEYS,
    include_prompt: bool = True,
    prompt_keys: Sequence[str] = ("prompt",),
    parent_model_resolver: Callable[[dict[str, Any]], str | None] | None = None,  # type: ignore[explicit-any]
    model_translator_factory: Callable[[str | Path | None], Callable[[str], str | None]]
    | None = claude_model_translator,
    requested_model_resolver_factory: Callable[[str | Path | None], Callable[[str], str | None]]
    | None = claude_requested_model_resolver,
    post_process: Callable[[dict[str, Any] | None, dict[str, Any]], dict[str, Any] | None]  # type: ignore[explicit-any]
    | None = None,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """
    Route one ``PreToolUse`` payload end to end.

    :param payload: ``PreToolUse`` hook payload.
    :param harness: Requesting harness, e.g. ``"claude-sdk"``.
    :param router_dir: Advertisement directory; ``None`` discovers it.
    :param bridge_dir: Claude-native bridge directory for session-id
        fallback.
    :param parent_model: Model the parent session runs on, when known.
    :param session_id: Session baked into the hook command, used when the
        advertisement carries none.
    :param timeout: Socket timeout in seconds.
    :param tool_matcher: Recognizes the harness's spawn tool by name.
    :param task_keys: ``tool_input`` keys naming the subagent.
    :param include_prompt: ``False`` withholds the spawn prompt.
    :param prompt_keys: ``tool_input`` keys carrying the task text.
    :param parent_model_resolver: Derives the parent model from the
        payload; ``None`` reads the bridge config instead.
    :param model_translator_factory: Builds the decision-model translator
        for this harness's spawn tool. ``None`` injects the routed id
        verbatim, which is what codex's ``spawn_agent`` expects.
    :param requested_model_resolver_factory: Builds the normalizer for the
        spawn's own ``model`` ask. ``None`` forwards it verbatim, which is
        what codex's slugs need.
    :param post_process: Last pass over the hook output, given the
        incoming ``tool_input`` too, e.g. codex's routed-model notice.
    :returns: Hook output, or ``None`` for "no opinion" — every failure
        lands here so a spawn is never blocked by routing infrastructure.
    """
    if not tool_matcher(payload.get("tool_name")):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    discovered_dir = discover_router_dir(router_dir)
    endpoint = read_router_endpoint(discovered_dir)
    if endpoint is None:
        return None
    resolved_session = (
        endpoint.session_id
        or session_id
        or resolve_session_id(endpoint, bridge_dir=bridge_dir or router_dir)
    )
    if not resolved_session:
        return None
    if parent_model is None:
        parent_model = (
            parent_model_resolver(payload)
            if parent_model_resolver is not None
            else resolve_parent_model(bridge_dir)
        )
    body = build_route_request(
        tool_input,
        harness=harness,
        parent_model=parent_model,
        task_keys=task_keys,
        include_prompt=include_prompt,
        prompt_keys=prompt_keys,
        requested_model_resolver=(
            requested_model_resolver_factory(bridge_dir or router_dir)
            if requested_model_resolver_factory is not None
            else None
        ),
    )
    decision = request_decision(endpoint, resolved_session, body, timeout=timeout)
    if decision is None:
        return None
    translator = (
        model_translator_factory(bridge_dir or router_dir)
        if model_translator_factory is not None
        else None
    )
    output = decision_to_hook_output(
        decision,
        tool_input,
        model_translator=translator,
        requesting_harness=harness,
        available_tools=advertised_relay_tools(bridge_dir or discovered_dir),
    )
    return post_process(output, tool_input) if post_process is not None else output


def read_stdin_payload(
    label: str,
) -> dict[str, Any] | None:  # type: ignore[explicit-any]
    """
    Read one hook payload from stdin.

    :param label: Diagnostic prefix, e.g. ``"omnigent codex router hook"``.
    :returns: Decoded object, or ``None`` when stdin is empty, malformed,
        or not a JSON object (a diagnostic goes to stderr).
    """
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError as exc:
        print(f"{label}: malformed JSON: {exc}", file=sys.stderr)
        return None
    if not isinstance(payload, dict):
        print(f"{label}: expected JSON object", file=sys.stderr)
        return None
    return payload


def hook_arg_parser(
    prog: str,
    *,
    extra_flags: Sequence[str] = (),
) -> argparse.ArgumentParser:
    """
    Build the argument parser shared by the harness hook entrypoints.

    :param prog: Program label for usage text.
    :param extra_flags: Flags beyond :data:`STANDARD_HOOK_FLAGS`.
    :returns: Parser whose every flag is optional and defaults to ``None``.
    """
    parser = argparse.ArgumentParser(prog=prog)
    for flag in (*STANDARD_HOOK_FLAGS, *extra_flags):
        parser.add_argument(flag, default=None)
    return parser


def parse_hook_args(
    prog: str,
    argv: Sequence[str],
    *,
    extra_flags: Sequence[str] = (),
) -> argparse.Namespace:
    """
    Parse a hook entrypoint's arguments, tolerating anything unexpected.

    Unknown flags are dropped rather than raising ``SystemExit(2)``: a
    stale generated hook command must not turn into a failed spawn.

    :param prog: Program label for usage text.
    :param argv: Arguments after the subcommand, if any.
    :param extra_flags: Flags beyond :data:`STANDARD_HOOK_FLAGS`.
    :returns: Parsed namespace.
    """
    args, _unknown = hook_arg_parser(prog, extra_flags=extra_flags).parse_known_args(list(argv))
    return args


def run_route_subagent_main(
    argv: Sequence[str],
    *,
    prog: str,
    harness: str,
    label: str | None = None,
    **route_kwargs: Any,  # type: ignore[explicit-any]
) -> int:
    """
    Run a hook entrypoint's spawn-routing body.

    :param argv: Arguments after the subcommand, if any.
    :param prog: Program label for usage text.
    :param harness: Requesting harness, used when argv names none.
    :param label: Diagnostic prefix; ``None`` uses *prog*.
    :param route_kwargs: Per-harness seams for
        :func:`route_pre_tool_use`.
    :returns: Always ``0`` so a routing failure never blocks a spawn.
    """
    args = parse_hook_args(prog, argv)
    payload = read_stdin_payload(label or prog)
    if payload is None:
        return 0
    output = route_pre_tool_use(
        payload,
        harness=args.harness or harness,
        router_dir=args.router_dir or args.bridge_dir,
        bridge_dir=args.bridge_dir,
        session_id=args.session_id,
        **route_kwargs,
    )
    if output is not None:
        sys.stdout.write(json.dumps(output))
    return 0
