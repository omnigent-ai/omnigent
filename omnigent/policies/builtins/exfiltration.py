"""Data-flow builtin policies: credential exfiltration + egress allowlist.

Two deterministic factories:

- :func:`credential_flow_guard` — Bell-LaPadula "no write-out" for local
  files: once the session reads a file under ``credential_paths``, any
  later invocation of an egress binary (``curl``, ``wget``, ``nc``, …)
  is escalated. A single command that both reads a credential path and
  invokes an egress binary is escalated immediately.
- :func:`egress_allowlist` — fetch binaries may only reach
  ``approved_hosts``; any other (or undeterminable) destination is
  escalated.

Both follow the factory + POLICY_REGISTRY conventions of the neighbouring
builtins (see github.py / google.py).
"""

from __future__ import annotations

import fnmatch
import posixpath
import shlex
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import urlsplit

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    UnsupportedShellSyntax,
    is_shell_interpreter,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# Session-state latch, set on the tool_result of a credential read.
# The latch mechanism mirrors google.py's READ_CONFIDENTIAL_STATE_KEY
# (tool_result -> ALLOW + state_updates), but the key itself follows
# google.py's plain descriptive naming: ``_policy_*`` keys are reserved
# on current main (see is_reserved_state_key in omnigent.policies.schema
# and the engine's apply path) and a non-ASK write of one raises, so a
# ``_policy_``-prefixed latch could never be set.
READ_CREDENTIAL_STATE_KEY = "exfiltration_credential_read"

_DEFAULT_EGRESS_BINS = ("curl", "wget", "nc", "ncat", "scp", "sftp", "ftp")
_DEFAULT_READ_BINS = (
    "cat",
    "head",
    "tail",
    "less",
    "more",
    "cp",
    "dd",
    "base64",
    "xxd",
    "od",
    "tar",
    "zip",
)
_DEFAULT_READ_TOOLS = ("sys_os_read",)


def _normalize(path: str) -> str:
    """Lexically collapse ``.``/``..`` so traversal spellings can't slip by."""
    return posixpath.normpath(path)


def _path_matches(path: str, patterns: tuple[str, ...]) -> bool:
    p = _normalize(path)
    return any(fnmatch.fnmatch(p, _normalize(pat)) for pat in patterns)


def _segments_from_command(
    command: str, _depth: int = 0
) -> tuple[list[tuple[str, list[str]]], bool]:
    """(binary, tokens) per shell segment, plus an opaque-tail flag.

    Follows github.py's parsing pattern: split on operators, shlex-split
    each segment, strip wrappers via :func:`real_invocation_tokens`, and
    recursively unwrap shell-interpreter / ``eval`` / ``env -S`` wrappers
    so a ``bash -c "…pipeline…"`` form is parsed as the pipeline it runs.
    A segment that cannot be tokenized, an interpreter form with no ``-c``
    string (program from stdin / script file), or nesting past
    ``MAX_SHELL_NESTING`` sets the opaque flag instead of being silently
    dropped.
    """
    if _depth > MAX_SHELL_NESTING:
        return [], True
    try:
        raw_segments = split_command_segments(command)
        unparseable = False
    except UnsupportedShellSyntax as exc:
        raw_segments = exc.completed_segments
        unparseable = True
    out: list[tuple[str, list[str]]] = []
    for seg in raw_segments:
        try:
            toks = shlex.split(seg)
        except ValueError:
            unparseable = True
            continue
        toks = real_invocation_tokens(toks)
        if not toks:
            continue
        inner = unwrap_shell_command(toks)
        if inner is not None:
            sub, sub_unparseable = _segments_from_command(inner, _depth + 1)
            out.extend(sub)
            unparseable = unparseable or sub_unparseable
            continue
        if is_shell_interpreter(toks):
            # Interpreter with no -c string: program is opaque.
            unparseable = True
            continue
        out.append((toks[0].rsplit("/", 1)[-1], toks))
    return out, unparseable


def _segments(event: PolicyEvent) -> tuple[list[tuple[str, list[str]]], bool]:
    """(binary, tokens) per shell segment of the event's command, plus flag."""
    data = event.get("data")
    args = data.get("arguments") if isinstance(data, dict) else {}
    args = args if isinstance(args, dict) else {}
    command = args.get("command") or ""
    command = command if isinstance(command, str) else ""
    return _segments_from_command(command)


def credential_flow_guard(
    *,
    credential_paths: list[str],
    egress_bins: list[str] | None = None,
    read_bins: list[str] | None = None,
    action: Literal["DENY", "ASK"] = "DENY",
    shell_tools: list[str] | None = None,
    read_tools: list[str] | None = None,
    deny_reason: str = "Blocked by the credential flow guard policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """Factory: build the credential-read → egress flow guard.

    :param credential_paths: Paths/globs whose contents must never flow
        into an egress invocation (e.g. ``["~/.aws/*", "*/secrets/*"]``
        after expansion by the caller).
    :param egress_bins: Binaries treated as transmission channels.
    :param action: ``"DENY"`` (default) or ``"ASK"`` on a flow violation.
    :returns: A PolicyCallable registered in POLICY_REGISTRY below.
    """
    cred = tuple(credential_paths)
    egress = tuple(egress_bins or _DEFAULT_EGRESS_BINS)
    readers = tuple(read_bins or _DEFAULT_READ_BINS)
    shell_names = tuple(shell_tools or SHELL_TOOLS)
    read_names = tuple(read_tools or _DEFAULT_READ_TOOLS)

    def _escalate(why: str) -> PolicyResponse:
        return {"result": action, "reason": f"{deny_reason} {why}"}

    def _shell_reads_credential(segs) -> bool:
        return any(
            binary in readers and any(_path_matches(t, cred) for t in toks[1:])
            for binary, toks in segs
        )

    def _shell_egresses(segs) -> bool:
        return any(binary in egress for binary, _ in segs)

    def policy(event: PolicyEvent) -> PolicyResponse | None:
        etype = event.get("type")
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        name = data.get("name")

        # Latch: a completed read of a credential path raises clearance.
        # Same hook gdrive_policy uses — the originating call rides on
        # the tool_result event as request_data.
        if etype == "tool_result":
            req = event.get("request_data")
            req = req if isinstance(req, dict) else {}
            rname = req.get("name")
            rargs = req.get("arguments") or {}
            rargs = rargs if isinstance(rargs, dict) else {}
            hit = (rname in read_names and _path_matches(str(rargs.get("path", "")), cred)) or (
                rname in shell_names
                and _shell_reads_credential(_segments({"data": {"arguments": rargs}})[0])
            )
            if hit and not (event.get("session_state") or {}).get(READ_CREDENTIAL_STATE_KEY):
                return {
                    "result": "ALLOW",
                    "state_updates": [
                        {"key": READ_CREDENTIAL_STATE_KEY, "action": "set", "value": True}
                    ],
                }
            return None

        if etype != "tool_call" or name not in shell_names:
            return None
        segs, unparseable = _segments(event)
        if _shell_egresses(segs):
            # Single-command flow: read + egress in one pipeline.
            if _shell_reads_credential(segs):
                return _escalate(
                    "This command both reads a credential path and invokes an egress binary."
                )
            # Cross-command flow: latch was raised by an earlier read.
            if (event.get("session_state") or {}).get(READ_CREDENTIAL_STATE_KEY):
                return _escalate(
                    "This session has read a credential file; egress invocations are blocked."
                )
        elif unparseable and (event.get("session_state") or {}).get(READ_CREDENTIAL_STATE_KEY):
            # Fail closed, not silently ALLOW: the session has read a
            # credential and the tail of this command is opaque, so it
            # may be an egress invocation we cannot see.
            return _escalate(
                "This session has read a credential file and "
                "this command cannot be fully parsed; egress "
                "invocations are blocked."
            )
        return None

    return policy


def egress_allowlist(
    *,
    approved_hosts: list[str],
    fetch_bins: list[str] | None = None,
    action: Literal["DENY", "ASK"] = "ASK",
    shell_tools: list[str] | None = None,
    deny_reason: str = "Blocked by the egress allowlist policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """Factory: confine fetch binaries to ``approved_hosts``.

    Hosts match exactly, or as ``*.example.com`` suffix entries. A fetch
    whose destination cannot be determined escalates (never abstains),
    mirroring github_policy's undeterminable-repository handling.
    """
    approved = tuple(h.lower() for h in approved_hosts)
    bins = tuple(fetch_bins or ("curl", "wget"))
    shell_names = tuple(shell_tools or SHELL_TOOLS)

    def _host_ok(host: str) -> bool:
        host = host.lower()
        return any(
            host == pat or (pat.startswith("*.") and host.endswith(pat[1:])) for pat in approved
        )

    def _dest_host(toks: list[str]) -> str | None:
        for tok in toks[1:]:
            if "://" in tok:
                return urlsplit(tok).hostname
            if tok.startswith(("http:", "https:")):
                return urlsplit(tok).hostname
        return None

    def policy(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        data = data if isinstance(data, dict) else {}
        if data.get("name") not in shell_names:
            return None
        for binary, toks in _segments(event)[0]:
            if binary not in bins:
                continue
            host = _dest_host(toks)
            if host is None:
                return {
                    "result": action,
                    "reason": f"{deny_reason} Destination of {binary} "
                    "invocation cannot be determined.",
                }
            if not _host_ok(host):
                return {
                    "result": action,
                    "reason": f"{deny_reason} Host {host} is not in the approved list.",
                }
        return None

    return policy


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.exfiltration.credential_flow_guard",
        "kind": "factory",
        "name": "Credential Read → Egress Flow Guard",
        "description": "Blocks (or asks on) egress-binary invocations after "
        "the session has read a credential-path file, and "
        "single commands that do both.",
        "params_schema": {
            "type": "object",
            "properties": {
                "credential_paths": {"type": "array", "items": {"type": "string"}},
                "egress_bins": {"type": "array", "items": {"type": "string"}},
                "read_bins": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "string", "enum": ["DENY", "ASK"], "default": "DENY"},
                "shell_tools": {"type": "array", "items": {"type": "string"}},
                "read_tools": {"type": "array", "items": {"type": "string"}},
                "deny_reason": {"type": "string"},
            },
            "required": ["credential_paths"],
        },
    },
    {
        "handler": "omnigent.policies.builtins.exfiltration.egress_allowlist",
        "kind": "factory",
        "name": "Egress Host Allowlist",
        "description": "Confines fetch binaries (curl/wget) to approved "
        "hosts; other or undeterminable destinations are "
        "denied or asked on.",
        "params_schema": {
            "type": "object",
            "properties": {
                "approved_hosts": {"type": "array", "items": {"type": "string"}},
                "fetch_bins": {"type": "array", "items": {"type": "string"}},
                "action": {"type": "string", "enum": ["DENY", "ASK"], "default": "ASK"},
                "shell_tools": {"type": "array", "items": {"type": "string"}},
                "deny_reason": {"type": "string"},
            },
            "required": ["approved_hosts"],
        },
    },
]
