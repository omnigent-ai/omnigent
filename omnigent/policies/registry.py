"""Policy registry — discovers and serves built-in policy metadata.

Scans modules listed in :data:`BUILTIN_POLICY_MODULES` for
``POLICY_REGISTRY`` lists at import time. The collected entries
are served via ``GET /v1/policy-registry`` so users can browse
available policies and attach them to sessions with validated
``factory_params``.

Usage::

    from omnigent.policies.registry import load_registry, get_registry

    # At server startup:
    load_registry()

    # At request time:
    entries = get_registry()
    schema = get_params_schema("omnigent.policies.builtins.safety.max_tool_calls_per_turn")
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from typing import Any

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolicyRegistryEntry:
    """One entry in the policy registry.

    :param handler: Full dotted import path to the callable,
        e.g. ``"omnigent.policies.builtins.safety.ask_on_os_tools"``.
    :param kind: Whether the handler is a direct ``"callable"``
        (receives event, returns decision) or a ``"factory"``
        (called with ``factory_params`` kwargs at build time,
        returns the actual callable). e.g. ``"callable"`` or
        ``"factory"``.
    :param name: Short display name for the UI, e.g.
        ``"Block Destructive Commands"``. Auto-derived from
        the handler's function name if not provided in the
        registry entry.
    :param description: Human-readable description of what
        the policy does, e.g. ``"Blocks destructive shell commands"``.
    :param params_schema: JSON Schema dict describing the
        factory parameters. Only meaningful when ``kind`` is
        ``"factory"``. ``None`` when the handler takes no
        factory params.
    """

    handler: str
    kind: str
    name: str
    description: str
    params_schema: dict[str, Any] | None = None
    internal_only: bool = False


# Module-level singleton. Populated by load_registry().
_registry: list[PolicyRegistryEntry] = []
# Lookup by handler path for O(1) schema retrieval.
_registry_by_handler: dict[str, PolicyRegistryEntry] = {}


def load_registry(
    extra_modules: list[str] | None = None,
) -> None:
    """Scan built-in and user-configured modules and populate the registry.

    Called once at server startup. Safe to call multiple times
    (clears and re-scans). Modules that fail to import are
    logged and skipped — a broken module should not prevent
    the server from starting.

    :param extra_modules: Additional dotted module paths to
        scan for ``POLICY_REGISTRY`` lists. Sourced from the
        server config's ``policy_modules`` key. ``None`` and
        ``[]`` both mean no extra modules.
    """
    from omnigent.policies.builtins import BUILTIN_POLICY_MODULES

    _registry.clear()
    _registry_by_handler.clear()

    all_modules = list(BUILTIN_POLICY_MODULES) + list(extra_modules or [])
    for module_path in all_modules:
        try:
            mod = importlib.import_module(module_path)
        except (ImportError, ModuleNotFoundError):
            _logger.warning(
                "Failed to import policy module %s; skipping",
                module_path,
                exc_info=True,
            )
            continue
        raw_entries = getattr(mod, "POLICY_REGISTRY", None)
        if not isinstance(raw_entries, list):
            _logger.warning(
                "Module %s has no POLICY_REGISTRY list; skipping",
                module_path,
            )
            continue
        for raw in raw_entries:
            if not isinstance(raw, dict) or "handler" not in raw:
                _logger.warning(
                    "Skipping malformed registry entry in %s: %r",
                    module_path,
                    raw,
                )
                continue
            handler_path = raw["handler"]
            # Auto-derive display name from the function name
            # if not explicitly provided.
            name = raw.get("name") or handler_path.rsplit(".", 1)[-1].replace("_", " ").title()
            entry = PolicyRegistryEntry(
                handler=handler_path,
                kind=raw.get("kind", "callable"),
                name=name,
                description=raw.get("description", ""),
                params_schema=raw.get("params_schema"),
                internal_only=raw.get("internal_only", False),
            )
            _registry.append(entry)
            _registry_by_handler[entry.handler] = entry

    _logger.info(
        "Policy registry loaded: %d entries from %d modules",
        len(_registry),
        len(all_modules),
    )


def get_registry() -> list[PolicyRegistryEntry]:
    """Return all registered policy entries.

    :returns: List of :class:`PolicyRegistryEntry` in
        discovery order.
    """
    return list(_registry)


def get_entry(handler: str) -> PolicyRegistryEntry | None:
    """Look up a registry entry by handler path. O(1).

    :param handler: Full dotted import path, e.g.
        ``"omnigent.policies.builtins.safety.ask_on_os_tools"``.
    :returns: The :class:`PolicyRegistryEntry`, or ``None`` if
        the handler is not registered.
    """
    return _registry_by_handler.get(handler)


def is_registered_handler(handler: str) -> bool:
    """Return whether *handler* is an allowed (registered) policy handler.

    The registry — built-in policy modules plus any admin-configured
    ``policy_modules`` — is the single allowlist of policy handlers a
    user may attach. This is the guard against arbitrary Python callable
    injection: an attacker cannot register
    ``subprocess.Popen`` or ``builtins.exec`` as a policy handler because
    those are not exported by any module's ``POLICY_REGISTRY``. Custom
    handlers become allowed only when a server admin adds the module
    that declares them to ``policy_modules``.

    This is the shared allowlist enforced at every untrusted entry
    point where a user supplies a handler path: the policy write APIs
    (``POST/PATCH`` on ``/v1/sessions/{id}/policies`` and
    ``/v1/policies``) and the agent-bundle upload path
    (:func:`omnigent.server.bundles.validate_agent_bundle`, which scans
    parsed specs / raw bundle YAML before any handler is resolved or
    called). Trusted spec loading (local ``omnigent run``, operator
    configs) deliberately does not run this check, so it keeps
    supporting custom handlers.

    If the registry has never been populated (e.g. a unit-test or CLI
    context that never called :func:`load_registry`), the built-in
    modules are scanned on first use. Built-ins always produce entries,
    so an empty registry means "not yet scanned", never "no policies
    exist" — this keeps the check correct without requiring every caller
    to remember a startup hook.

    :param handler: Full dotted import path, e.g.
        ``"omnigent.policies.builtins.safety.ask_on_os_tools"``.
    :returns: ``True`` if the handler is in the loaded registry.
    """
    if not _registry_by_handler:
        load_registry()
    return handler in _registry_by_handler


def get_params_schema(handler: str) -> dict[str, Any] | None:
    """Look up the params schema for a handler path.

    :param handler: Full dotted import path, e.g.
        ``"omnigent.policies.builtins.safety.max_tool_calls_per_turn"``.
    :returns: The JSON Schema dict, or ``None`` if the handler
        is not in the registry or has no schema.
    """
    entry = _registry_by_handler.get(handler)
    if entry is None:
        return None
    return entry.params_schema


def validate_factory_params(
    handler: str,
    factory_params: dict[str, Any] | None,
) -> str | None:
    """Validate factory_params against the registry schema.

    Returns an error message string if validation fails, or
    ``None`` if valid. Does not validate if the handler is not
    in the registry (custom policies are allowed without schema).

    :param handler: Full dotted import path.
    :param factory_params: The params to validate.
    :returns: Error message, or ``None`` if valid.
    """
    entry = _registry_by_handler.get(handler)
    if entry is None:
        # Not in registry — custom policy, skip validation.
        return None

    if entry.kind == "callable":
        # Direct callable — must not receive factory_params.
        if factory_params:
            return (
                f"Policy '{handler}' is a direct callable and does not "
                f"accept factory_params, but got: {list(factory_params.keys())}"
            )
        return None

    # kind == "factory" — validate against params_schema.
    schema = entry.params_schema
    if schema is None:
        # Factory with no declared schema — accept anything.
        return None

    if factory_params is None:
        # Check if all required params have defaults.
        required = schema.get("required", [])
        properties = schema.get("properties", {})
        missing = [k for k in required if "default" not in properties.get(k, {})]
        if missing:
            return f"Policy '{handler}' requires params: {missing}"
        return None

    # Validate against schema properties.
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    # Check for unknown keys.
    unknown = set(factory_params.keys()) - set(properties.keys())
    if unknown:
        return f"Unknown params for '{handler}': {sorted(unknown)}"

    # Check required keys.
    missing = required - set(factory_params.keys())
    # Exclude required keys that have defaults.
    missing = {k for k in missing if "default" not in properties.get(k, {})}
    if missing:
        return f"Missing required params for '{handler}': {sorted(missing)}"

    # Type check each provided param.
    for key, value in factory_params.items():
        prop = properties.get(key)
        if prop is None:
            continue
        expected_type = prop.get("type")
        if expected_type and not _type_matches(value, expected_type):
            return (
                f"Param '{key}' for '{handler}' must be {expected_type}, "
                f"got {type(value).__name__}"
            )

    return None


def _type_matches(value: Any, json_type: str) -> bool:
    """Check if a Python value matches a JSON Schema type.

    :param value: The value to check.
    :param json_type: JSON Schema type string.
    :returns: ``True`` if the value matches.
    """
    if json_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if json_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if json_type == "string":
        return isinstance(value, str)
    if json_type == "boolean":
        return isinstance(value, bool)
    if json_type == "array":
        return isinstance(value, list)
    if json_type == "object":
        return isinstance(value, dict)
    # Unknown type — don't reject.
    return True
