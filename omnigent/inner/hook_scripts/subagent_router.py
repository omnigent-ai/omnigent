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

from omnigent.claude_model_vocabulary import claude_model_alias

# Advertisement file written by the runner's subagent-routing endpoint,
# mirroring the ``tool_relay.json`` discovery pattern.
ADVERTISEMENT_FILE = "subagent_router.json"
# Claude-native bridge config, read for the session id / launch model.
_BRIDGE_CONFIG_FILE = "bridge.json"

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

# Subagent types that inherit the caller's context instead of starting
# fresh. Routing a fork would price a task whose real cost is dominated
# by inherited context, so v1 reports them and lets the server exempt
# them.
FORK_SUBAGENT_TYPES = frozenset({"fork"})
_FORK_SUFFIXES = ("-fork", "_fork", ":fork")

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
REQUEST_TIMEOUT_S = 30.0

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


def read_router_endpoint(router_dir: str | Path | None) -> RouterEndpoint | None:
    """
    Read the advertised endpoint.

    The advertisement lives in an agent-writable directory, so it is
    validated before anything is sent to it: the URL must be plain ``http``
    on a loopback address, and the advertising process must still be alive
    (a stale entry's port can be re-bound by another local process). Either
    check failing means "router unreachable".

    :param router_dir: Directory containing
        :data:`ADVERTISEMENT_FILE`.
    :returns: Endpoint, or ``None`` when the advertisement is absent,
        malformed, or fails validation.
    """
    if router_dir is None:
        return None
    try:
        raw = (Path(router_dir) / ADVERTISEMENT_FILE).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    url = payload.get("url")
    token = payload.get("token")
    if not isinstance(url, str) or not url or not isinstance(token, str) or not token:
        return None
    if not _is_loopback_url(url):
        _diagnose(f"ignoring advertised router at {url!r}: not plain http on loopback")
        return None
    if not _advertiser_alive(payload.get("pid")):
        _diagnose(f"ignoring advertised router at {url!r}: advertiser pid not alive")
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


def is_fork_spawn(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    task_keys: Sequence[str] = DEFAULT_TASK_KEYS,
) -> bool:
    """
    Detect a context-inheriting (fork-typed) spawn.

    :param tool_input: ``tool_input`` from the hook payload.
    :param task_keys: Extra name keys to try when ``subagent_type`` is
        absent, e.g. codex's ``task_name``.
    :returns: ``True`` when the requested subagent type inherits the
        caller's context.
    """
    keys = dict.fromkeys(("subagent_type", *task_keys))
    normalized = spawn_task_name(tool_input, tuple(keys)).strip().lower()
    return normalized in FORK_SUBAGENT_TYPES or normalized.endswith(_FORK_SUFFIXES)


def build_route_request(
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    *,
    harness: str,
    parent_model: str | None = None,
    task_keys: Sequence[str] = DEFAULT_TASK_KEYS,
    include_prompt: bool = True,
) -> dict[str, Any]:  # type: ignore[explicit-any]
    """
    Build the ``route-subagent`` request body.

    :param tool_input: ``tool_input`` from the hook payload.
    :param harness: Requesting harness, e.g. ``"claude-native"``.
    :param parent_model: Model the parent session runs on, when known.
    :param task_keys: ``tool_input`` keys naming the subagent, in
        preference order.
    :param include_prompt: ``False`` sends ``prompt: null``, for harnesses
        whose spawn message is encrypted in hook payloads (codex).
    :returns: JSON-serializable request body.
    """
    prompt = tool_input.get("prompt") if include_prompt else None
    return {
        "harness": harness,
        "task_name": spawn_task_name(tool_input, task_keys),
        "prompt": prompt if isinstance(prompt, str) and prompt else None,
        "fork": is_fork_spawn(tool_input, task_keys),
        "parent_model": parent_model,
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


def redirect_reason(harness: str, model: str) -> str:
    """
    Build the cross-harness redirect instruction shown to the model.

    :param harness: Harness the router picked, e.g. ``"codex"``.
    :param model: Model the router picked.
    :returns: Deny reason telling the model how to respawn correctly.
    """
    return (
        f"Router selected {harness}/{model}. Use sys_session_send with "
        f"args.harness={harness}, args.model={model} instead."
    )


def decision_to_hook_output(
    decision: dict[str, Any],  # type: ignore[explicit-any]
    tool_input: dict[str, Any],  # type: ignore[explicit-any]
    *,
    model_translator: Callable[[str], str | None] | None = None,
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
            return _deny(redirect_reason(harness, model))
        # A redirect without a target can't be followed — fail open.
        return None
    if action == "deny":
        return _deny(rationale or "Spawn denied by Omnigent smart routing.")
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
    parent_model_resolver: Callable[[dict[str, Any]], str | None] | None = None,  # type: ignore[explicit-any]
    model_translator_factory: Callable[[str | Path | None], Callable[[str], str | None]]
    | None = claude_model_translator,
    post_process: Callable[[dict[str, Any] | None], dict[str, Any] | None] | None = None,  # type: ignore[explicit-any]
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
    :param parent_model_resolver: Derives the parent model from the
        payload; ``None`` reads the bridge config instead.
    :param model_translator_factory: Builds the decision-model translator
        for this harness's spawn tool. ``None`` injects the routed id
        verbatim, which is what codex's ``spawn_agent`` expects.
    :param post_process: Last pass over the hook output, e.g. codex's
        routed-model notice.
    :returns: Hook output, or ``None`` for "no opinion" — every failure
        lands here so a spawn is never blocked by routing infrastructure.
    """
    if not tool_matcher(payload.get("tool_name")):
        return None
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return None
    endpoint = read_router_endpoint(discover_router_dir(router_dir))
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
    )
    decision = request_decision(endpoint, resolved_session, body, timeout=timeout)
    if decision is None:
        return None
    translator = (
        model_translator_factory(bridge_dir or router_dir)
        if model_translator_factory is not None
        else None
    )
    output = decision_to_hook_output(decision, tool_input, model_translator=translator)
    return post_process(output) if post_process is not None else output


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
