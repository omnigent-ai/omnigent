"""Strict admission checks for the supported subset of the existing AgentSpec.

The runtime parser owns conversion and semantic validation. These guards reject
fields/shapes it would silently discard or coerce, before calling its helpers.
"""

from __future__ import annotations

import math
import re
from dataclasses import fields
from typing import Any, TypeAlias

from omnigent.builtin_catalog import BUILTIN_FACTORIES, HINDSIGHT_FACTORIES
from omnigent.spec.parser import _TOOLS_CONFIG_KEYS
from omnigent.spec.types import RetryPolicy

YamlMapping: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]  # decoded YAML boundary

# web_fetch is configured by ToolManager rather than a catalog factory.
_ENABLABLE_BUILTINS = frozenset(BUILTIN_FACTORIES) | frozenset(HINDSIGHT_FACTORIES) | {"web_fetch"}


class ContentError(ValueError):
    """A safe diagnostic: messages and field paths never contain authored values."""

    def __init__(self, code: str, field: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.field = field


def require(condition: bool, field: str, message: str) -> None:
    if not condition:
        raise ContentError("INVALID_SHAPE", field, message)


def mapping(value: object, field: str, allowed: set[str] | None = None) -> YamlMapping:
    if not isinstance(value, dict):
        raise ContentError("INVALID_SHAPE", field, "Expected a mapping.")
    require(all(isinstance(key, str) for key in value), field, "Expected string mapping keys.")
    if allowed is not None and value.keys() - allowed:
        raise ContentError(
            "UNSUPPORTED_FIELD", field, "A field is unknown or outside the offline scope."
        )
    return value


def strings(value: object, field: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContentError("INVALID_SHAPE", field, "Expected a list of strings.")
    return value


def string_map(value: object, field: str) -> None:
    data = mapping(value, field)
    require(all(isinstance(item, str) for item in data.values()), field, "Expected string values.")


def scalar_fields(data: YamlMapping, field: str, names: set[str], kind: type) -> None:
    for name in sorted(names & data.keys()):
        value = data[name]
        require(type(value) is kind, f"{field}.{name}".lstrip("."), f"Expected {kind.__name__}.")


def retry(value: object, field: str) -> None:
    data = mapping(value, field, {item.name for item in fields(RetryPolicy)})
    for name, item in data.items():
        if name == "retryable_status_codes":
            require(
                isinstance(item, list) and all(type(code) is int for code in item),
                field,
                "Expected integer status codes.",
            )
        elif name == "jitter":
            require(type(item) is bool, field, "Expected a boolean jitter flag.")
        elif name == "timeout_per_request_s" and item is None:
            continue
        else:
            require(
                type(item) in (int, float) and (isinstance(item, int) or math.isfinite(item)),
                field,
                "Expected finite retry numbers.",
            )
            if name == "max_retries":
                require(type(item) is int, field, "Expected an integer retry count.")


def mcp(value: object, field: str, *, inline: bool) -> YamlMapping:
    common = {"description", "url", "headers", "command", "args", "env"}
    allowed = common | (
        {"type", "tools", "auth"} if inline else {"name", "transport", "timeout", "retry"}
    )
    data = mapping(value, field, allowed)
    scalar_fields(data, field, {"name", "description", "url", "command", "type", "transport"}, str)
    if inline:
        require(
            data.get("type") == "mcp", field, "Only inline MCP tool declarations are supported."
        )
        transport = "stdio" if "command" in data else "http"
    else:
        require(
            isinstance(data.get("name"), str) and bool(data["name"]),
            field,
            "MCP name is required.",
        )
        transport = data.get("transport")
    require(transport in ("http", "stdio"), field, "Expected an HTTP or stdio transport.")
    required = "url" if transport == "http" else "command"
    require(bool(data.get(required)), field, "The MCP transport endpoint is required.")
    disallowed = {"command", "args", "env"} if transport == "http" else {"url", "headers", "auth"}
    require(not (data.keys() & disallowed), field, "MCP fields conflict with the transport.")
    for name in ("headers", "env"):
        if name in data:
            string_map(data[name], f"{field}.{name}")
    for name in ("args", "tools"):
        if name in data:
            strings(data[name], f"{field}.{name}")
    if "auth" in data:
        auth = mapping(data["auth"], f"{field}.auth", {"type", "profile"})
        require(
            auth.get("type") == "databricks"
            and isinstance(auth.get("profile"), str)
            and bool(auth["profile"]),
            f"{field}.auth",
            "MCP auth requires a Databricks profile declaration.",
        )
    if "retry" in data:
        retry(data["retry"], f"{field}.retry")
    scalar_fields(data, field, {"timeout"}, int)
    return data


def guardrails(value: object) -> None:
    data = mapping(value, "guardrails", {"labels", "policies", "ask_timeout"})
    labels = mapping(data.get("labels", {}), "guardrails.labels")
    for index, label in enumerate(labels.values()):
        field = f"guardrails.labels[{index}]"
        if isinstance(label, dict):
            entry = mapping(label, field, {"initial", "values"})
            if "values" in entry:
                require(
                    isinstance(entry["values"], list)
                    and all(type(v) in (str, int, float, bool) for v in entry["values"]),
                    field,
                    "Expected scalar label values.",
                )
            require(
                type(entry.get("initial")) in (str, int, float, bool, type(None)),
                field,
                "Expected a scalar initial label.",
            )
        else:
            require(
                type(label) in (str, int, float, bool, type(None)),
                field,
                "Expected a scalar label or label definition.",
            )
    policies = mapping(data.get("policies", {}), "guardrails.policies")
    for index, value in enumerate(policies.values()):
        field = f"guardrails.policies[{index}]"
        policy = mapping(
            value,
            field,
            {"type", "function", "handler", "condition", "set_labels", "config", "ask_timeout"},
        )
        require(policy.get("type") == "function", field, "Expected a function policy.")
        require(
            ("function" in policy) != ("handler" in policy),
            field,
            "Declare exactly one function or handler reference.",
        )
        reference = policy.get("function", policy.get("handler"))
        if isinstance(reference, dict):
            function = mapping(reference, f"{field}.function", {"path", "arguments"})
            reference = function.get("path")
            if "arguments" in function:
                mapping(function["arguments"], f"{field}.function.arguments")
        require(
            isinstance(reference, str)
            and re.fullmatch(r"(?:[A-Za-z_]\w*\.)+[A-Za-z_]\w*", reference) is not None,
            f"{field}.function",
            "Expected a dotted Python reference; it will not be imported.",
        )
        if "config" in policy:
            mapping(policy["config"], f"{field}.config")
        if "set_labels" in policy:
            writable = strings(policy["set_labels"], f"{field}.set_labels")
            require(
                set(writable) <= labels.keys(),
                f"{field}.set_labels",
                "Writable labels must be declared in this agent.",
            )
        if "condition" in policy:
            condition = mapping(policy["condition"], f"{field}.condition")
            require(
                condition.keys() <= labels.keys(),
                f"{field}.condition",
                "Condition labels must be declared in this agent.",
            )
            for key, expected in condition.items():
                choices = expected if isinstance(expected, list) else [expected]
                require(
                    all(type(item) in (str, int, float, bool) for item in choices),
                    f"{field}.condition",
                    "Expected scalar condition values.",
                )
                label = labels[key]
                if isinstance(label, dict) and isinstance(label.get("values"), list):
                    require(
                        {str(item) for item in choices} <= {str(item) for item in label["values"]},
                        f"{field}.condition",
                        "Condition values must belong to the declared label values.",
                    )
        scalar_fields(policy, field, {"ask_timeout"}, int)
    scalar_fields(data, "guardrails", {"ask_timeout"}, int)


def config(value: object) -> YamlMapping:
    data = mapping(value, "")
    if "spec_version" not in data:
        raise ContentError(
            "UNSUPPORTED_FORMAT", "", "Offline validation requires a spec_version: 1 agent image."
        )
    mapping(
        data,
        "",
        {
            "spec_version",
            "name",
            "description",
            "instructions",
            "prompt",
            "llm",
            "interaction",
            "tools",
            "executor",
            "compaction",
            "guardrails",
            "params",
            "async",
            "timers",
            "spawn",
            "agent_session_sharing",
            "skills",
        },
    )
    scalar_fields(data, "", {"spec_version"}, int)
    scalar_fields(data, "", {"async", "timers", "spawn"}, bool)
    scalar_fields(
        data, "", {"name", "description", "instructions", "prompt", "agent_session_sharing"}, str
    )
    if "params" in data:
        mapping(data["params"], "params")
    if "skills" in data and data["skills"] != "*":
        strings(data["skills"], "skills")
    if "llm" in data:
        llm = mapping(
            data["llm"], "llm", {"model", "profile", "connection", "request_timeout", "retry"}
        )
        scalar_fields(llm, "llm", {"model", "profile"}, str)
        scalar_fields(llm, "llm", {"request_timeout"}, int)
        if "connection" in llm:
            string_map(llm["connection"], "llm.connection")
        if "retry" in llm:
            retry(llm["retry"], "llm.retry")
    if "executor" in data:
        executor = mapping(
            data["executor"],
            "executor",
            {
                "type",
                "model",
                "reasoning_effort",
                "profile",
                "config",
                "connection",
                "auth",
                "timeout",
                "max_iterations",
                "context_window",
            },
        )
        scalar_fields(executor, "executor", {"type", "model", "reasoning_effort", "profile"}, str)
        scalar_fields(executor, "executor", {"timeout", "max_iterations", "context_window"}, int)
        if "config" in executor:
            mapping(executor["config"], "executor.config", {"harness", "profile"})
            string_map(executor["config"], "executor.config")
        if "connection" in executor:
            string_map(executor["connection"], "executor.connection")
        if "auth" in executor:
            auth = mapping(executor["auth"], "executor.auth")
            variants = {
                "api_key": {"type", "api_key", "base_url"},
                "databricks": {"type", "profile"},
                "provider": {"type", "name"},
            }
            require(isinstance(auth.get("type"), str), "executor.auth", "Auth type is required.")
            allowed = variants.get(auth["type"])
            require(allowed is not None, "executor.auth", "Unsupported auth type.")
            mapping(auth, "executor.auth", allowed)
            string_map(auth, "executor.auth")
    if "interaction" in data:
        interaction = mapping(data["interaction"], "interaction", {"conversational", "modalities"})
        scalar_fields(interaction, "interaction", {"conversational"}, bool)
        if "modalities" in interaction:
            modalities = mapping(
                interaction["modalities"], "interaction.modalities", {"input", "output"}
            )
            for value in modalities.values():
                strings(value, "interaction.modalities")
    if "compaction" in data:
        compaction = mapping(
            data["compaction"], "compaction", {"trigger_threshold", "recent_window"}
        )
        scalar_fields(compaction, "compaction", {"recent_window"}, int)
        if "trigger_threshold" in compaction:
            threshold = compaction["trigger_threshold"]
            require(
                type(threshold) in (int, float)
                and (isinstance(threshold, int) or math.isfinite(threshold)),
                "compaction.trigger_threshold",
                "Expected a finite threshold.",
            )
    if "tools" in data:
        tools = mapping(data["tools"], "tools")
        for index, (name, value) in enumerate(tools.items()):
            if name == "agents":
                strings(value, "tools.agents")
            elif name == "timeout":
                require(type(value) is int, "tools.timeout", "Expected an integer timeout.")
            elif name == "retry":
                retry(value, "tools.retry")
            elif name == "builtins":
                names = strings(value, "tools.builtins")
                require(
                    set(names) <= _ENABLABLE_BUILTINS,
                    "tools.builtins",
                    "Unknown or non-configurable builtin tool.",
                )
            elif name in _TOOLS_CONFIG_KEYS:
                raise ContentError(
                    "UNSUPPORTED_FIELD",
                    "tools",
                    "This reserved tools field is outside the offline scope.",
                )
            else:
                mcp(value, f"tools[{index}]", inline=True)
    if "guardrails" in data:
        guardrails(data["guardrails"])
    return data
