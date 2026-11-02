"""Bounded model-selection evidence shared by Claude native and SDK paths."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Mapping, Sequence

from omnigent.debug_logging import debug_event
from omnigent.models.claude_model_vocabulary import ALIAS_MODEL_ENV_VARS

_logger = logging.getLogger(__name__)
_SOURCES = frozenset(
    {
        "executor_config",
        "executor_override",
        "gateway_default",
        "cli_default",
        "session_override",
        "agent_spec",
        "provider_config",
        "inference_binding",
        "catalog_default",
        "provider_fallback",
        "launch_arguments",
    }
)


def _fingerprint(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if len(value) > 4096:
        return "oversized"
    return hashlib.sha256(value.encode()).hexdigest()


def log_claude_model_resolution(
    *,
    phase: str,
    selection_source: str,
    selected_model: str | None,
    resolved_model: str | None,
    session_id: str | None = None,
    reported_model: str | None = None,
    model_env: Mapping[str, str] | None = None,
    model_overrides: Mapping[str, str] | None = None,
    picker_values: Sequence[str] | None = None,
    catalog_available: bool | None = None,
    catalog_stale: bool | None = None,
    explicit_launch_args: Sequence[str] | None = None,
) -> None:
    """Log fingerprints and resolution facts without models, settings, or arguments."""
    with contextlib.suppress(Exception):
        from omnigent.version import VERSION

        if phase not in {"sdk_query", "sdk_failure", "native_launch", "native_model_change"}:
            return
        argument_models: list[str | None] = []
        for index, argument in enumerate(explicit_launch_args or ()):
            if argument == "--model":
                value = (
                    explicit_launch_args[index + 1]
                    if explicit_launch_args is not None and index + 1 < len(explicit_launch_args)
                    else None
                )
                argument_models.append(value if value and not value.startswith("--") else None)
            elif argument.startswith("--model="):
                argument_models.append(argument.removeprefix("--model="))
        if argument_models:
            selection_source = "launch_arguments"
            resolved_model = argument_models[0] if len(argument_models) == 1 else None
        env = model_env or {}
        overrides = model_overrides or {}
        alias_pin = env.get(ALIAS_MODEL_ENV_VARS.get(resolved_model or "", ""))
        canonical_override = overrides.get(resolved_model or "")
        _logger.info(
            "Claude model resolution",
            extra=debug_event(
                "claude_model_resolution",
                session_id=session_id,
                phase=phase,
                selection_source=selection_source if selection_source in _SOURCES else "unknown",
                selected_model_sha256=_fingerprint(selected_model),
                resolved_model_sha256=_fingerprint(resolved_model),
                reported_model_sha256=_fingerprint(reported_model),
                selected_alias_pin_sha256=_fingerprint(alias_pin),
                selected_canonical_override_sha256=_fingerprint(canonical_override),
                alias_pin_count=sum(bool(env.get(key)) for key in ALIAS_MODEL_ENV_VARS.values()),
                canonical_override_count=len(overrides),
                picker_vocabulary_present=picker_values is not None,
                picker_vocabulary_count=len(picker_values) if picker_values is not None else None,
                picker_exact_selected=selected_model in picker_values
                if picker_values is not None and selected_model is not None
                else None,
                picker_exact_resolved=resolved_model in picker_values
                if picker_values is not None and resolved_model is not None
                else None,
                catalog_available=catalog_available,
                catalog_stale=catalog_stale,
                explicit_model_argument_count=len(argument_models),
                runtime_version=VERSION,
                diagnostic_version=1,
            ),
        )
