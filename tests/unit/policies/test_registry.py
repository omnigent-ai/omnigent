"""Tests for the policy registry and built-in policy discovery.

Verifies that :func:`load_registry` discovers built-in modules,
:func:`get_registry` returns the entries, and
:func:`validate_factory_params` correctly validates against
declared schemas.
"""

from __future__ import annotations

from omnigent.policies.registry import (
    get_params_schema,
    get_registry,
    is_registered_handler,
    load_registry,
    validate_factory_params,
)

# ── load_registry + get_registry ────────────────────────────────────────────


def test_load_registry_discovers_builtins() -> None:
    """``load_registry`` finds entries from the built-in safety module.

    The safety module declares entries including max_tool_calls_per_session
    and ask_on_os_tools.
    """
    load_registry()
    entries = get_registry()

    assert len(entries) >= 2
    handlers = {e.handler for e in entries}
    assert "omnigent.policies.builtins.safety.max_tool_calls_per_session" in handlers
    assert "omnigent.policies.builtins.safety.ask_on_os_tools" in handlers


def test_registry_entries_have_correct_kind() -> None:
    """Each entry declares ``kind`` as ``"callable"`` or ``"factory"``."""
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert (
        by_handler["omnigent.policies.builtins.safety.max_tool_calls_per_session"].kind
        == "factory"
    )
    assert by_handler["omnigent.policies.builtins.safety.ask_on_os_tools"].kind == "callable"


def test_registry_entries_have_descriptions() -> None:
    """Every entry has a non-empty description."""
    load_registry()
    for entry in get_registry():
        assert entry.description, f"Entry {entry.handler} has no description"


# ── get_params_schema ───────────────────────────────────────────────────────


def test_get_params_schema_returns_schema() -> None:
    """``get_params_schema`` returns the schema for a factory callable."""
    load_registry()
    schema = get_params_schema("omnigent.policies.builtins.safety.max_tool_calls_per_session")

    assert schema is not None
    assert schema["type"] == "object"
    assert "limit" in schema["properties"]
    assert schema["properties"]["limit"]["type"] == "integer"


def test_get_params_schema_none_for_direct_callable() -> None:
    """``get_params_schema`` returns ``None`` for a direct (non-factory) callable."""
    load_registry()
    schema = get_params_schema("omnigent.policies.builtins.safety.ask_on_os_tools")

    assert schema is None


def test_get_params_schema_none_for_unknown() -> None:
    """``get_params_schema`` returns ``None`` for an unregistered handler."""
    load_registry()
    assert get_params_schema("nonexistent.module.func") is None


# ── validate_factory_params ─────────────────────────────────────────────────


def test_validate_valid_params() -> None:
    """Valid params pass validation."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        {"limit": 5},
    )
    assert error is None


def test_validate_missing_required_param_with_default_is_ok() -> None:
    """A required param with a default passes when omitted."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        {},
    )
    # "limit" is required but has default=100, so {} is valid.
    assert error is None


def test_validate_wrong_type() -> None:
    """A param with the wrong type returns an error."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        {"limit": "not_a_number"},
    )
    assert error is not None
    assert "integer" in error


def test_validate_unknown_param() -> None:
    """An unknown param key returns an error."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.max_tool_calls_per_session",
        {"limit": 5, "bogus": True},
    )
    assert error is not None
    assert "bogus" in error


def test_validate_params_on_no_schema_callable() -> None:
    """Passing params to a callable with no schema returns an error."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.ask_on_os_tools",
        {"unexpected": 1},
    )
    assert error is not None
    assert "does not accept" in error


def test_validate_none_params_on_no_schema() -> None:
    """``None`` params on a callable with no schema passes."""
    load_registry()
    error = validate_factory_params(
        "omnigent.policies.builtins.safety.ask_on_os_tools",
        None,
    )
    assert error is None


def test_validate_skips_unknown_handler() -> None:
    """Unknown handlers pass validation (custom policies without registry entry)."""
    load_registry()
    error = validate_factory_params(
        "custom.package.my_policy",
        {"anything": "goes"},
    )
    assert error is None


# ── is_registered_handler (allowlist) ──────────────────────────────


def test_is_registered_handler_true_for_builtin() -> None:
    """A built-in handler reports as registered."""
    load_registry()
    assert is_registered_handler("omnigent.policies.builtins.safety.ask_on_os_tools") is True


def test_is_registered_handler_false_for_injection_gadget() -> None:
    """An arbitrary importable callable is NOT registered.

    This is the core guard: ``subprocess.Popen`` is a real,
    importable callable, so only registry membership keeps it out.
    """
    load_registry()
    assert is_registered_handler("subprocess.Popen") is False
    assert is_registered_handler("builtins.exec") is False


def test_is_registered_handler_false_for_unregistered_first_party() -> None:
    """A first-party path that is not a registry entry is NOT registered.

    The registry check is exact: ``make_fixed_action_callable`` lives in
    the ``omnigent`` package but is not a browsable registry entry, so the
    write APIs (which enforce this check) reject it. Such handlers are
    reachable only via operator-run / declarative specs, not the
    user-facing write APIs.
    """
    load_registry()
    assert is_registered_handler("omnigent.policies.function.make_fixed_action_callable") is False


def test_is_registered_handler_includes_extra_modules() -> None:
    """Admin-configured ``policy_modules`` extend the allowlist.

    A handler declared by an extra module's ``POLICY_REGISTRY`` becomes
    registered — this is the supported path for custom handlers: an admin
    adds the module to ``policy_modules`` rather than naming an arbitrary
    callable at the API.
    """
    # The built-in google module is normally scanned; load with it as an
    # explicit extra to prove extra_modules entries are registered too.
    load_registry(extra_modules=["omnigent.policies.builtins.google"])
    assert is_registered_handler("omnigent.policies.builtins.google.gdrive_policy") is True
    # Re-scan without extras leaves built-ins intact (idempotent reload).
    load_registry()
    assert is_registered_handler("omnigent.policies.builtins.safety.ask_on_os_tools") is True
