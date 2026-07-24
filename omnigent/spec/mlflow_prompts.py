"""Resolve agent ``instructions:`` that reference an MLflow Prompt Registry entry.

An agent's ``instructions:`` (legacy alias ``prompt:``) normally resolves to
inline text or a bundle-relative file. This module adds a third form: a
reference into the `MLflow 3 Prompt Registry
<https://mlflow.org/docs/latest/genai/prompt-registry/>`_, resolved to plain
text at bundle-load time. Both parser copies
(:mod:`omnigent.spec.parser` and :mod:`omnigent.inner.loader`) call the single
:func:`resolve_mlflow_prompt` funnel here so the resolution rules can never
drift between them.

Two config forms are accepted:

- **structured**::

      instructions:
        source: mlflow
        reference: prompts:/greeting@production
        vars: {product: Acme}

- **shorthand string**: ``instructions: mlflow+prompts:/greeting@production``

The same code path serves both the OSS registry and the Databricks-managed
(Unity Catalog) registry: the backend is selected purely by configuration
(``registry_uri: databricks-uc`` and a 3-part UC name for managed; a tracking
URI plus token/basic auth env for OSS), never by branching business logic.

Auth is supplied via the environment / MLflow's unified auth only; secrets
never appear in agent YAML. Resolved prompt name and version are logged; prompt
contents are not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from omnigent.errors import ErrorCode, OmnigentError

_log = logging.getLogger(__name__)

# Shorthand marker: ``instructions: mlflow+prompts:/name@alias``.
_SHORTHAND_PREFIX = "mlflow+"
# All Prompt Registry references are ``prompts:/`` URIs (``prompts:/name@alias``
# or ``prompts:/name/version``). ``load_prompt`` resolves by version or alias
# only, never by tag, so no tag syntax is accepted here.
_PROMPT_URI_SCHEME = "prompts:/"


@dataclass(frozen=True)
class MlflowPromptReference:
    """A parsed MLflow Prompt Registry reference from an ``instructions:`` value."""

    reference: str
    vars: dict[str, object] | None = None
    tracking_uri: str | None = None
    registry_uri: str | None = None
    cache_ttl: float | None = None


def parse_mlflow_instructions(raw_value: object) -> MlflowPromptReference | None:
    """
    Detect and parse an MLflow Prompt Registry ``instructions:`` value.

    Returns a :class:`MlflowPromptReference` when *raw_value* is one of the
    supported MLflow forms (structured ``source: mlflow`` mapping or a
    ``mlflow+prompts:/...`` shorthand string), or ``None`` when it is an
    ordinary inline-text / file-path value that the caller should resolve the
    existing way. Detection is intentionally cheap and network-free so it is
    safe to run on every parse; the registry is only contacted later, in
    :func:`resolve_mlflow_prompt`.

    :param raw_value: The raw ``instructions:`` value from the agent config.
    :returns: The parsed reference, or ``None`` if not an MLflow form.
    :raises OmnigentError: If the value is an MLflow form but is malformed
        (e.g. missing ``reference``, non-``prompts:/`` URI, wrong types).
    """
    if isinstance(raw_value, str):
        if not raw_value.startswith(_SHORTHAND_PREFIX):
            return None
        reference = raw_value[len(_SHORTHAND_PREFIX) :]
        _require_prompt_uri(reference)
        return MlflowPromptReference(reference=reference)

    if isinstance(raw_value, dict) and raw_value.get("source") == "mlflow":
        ref_value = raw_value.get("reference")
        if not isinstance(ref_value, str) or not ref_value:
            raise OmnigentError(
                "instructions.source: mlflow requires a 'reference:' string, "
                "e.g. 'prompts:/greeting@production'.",
                code=ErrorCode.INVALID_INPUT,
            )
        _require_prompt_uri(ref_value)
        prompt_vars = raw_value.get("vars")
        if prompt_vars is not None and not isinstance(prompt_vars, dict):
            raise OmnigentError(
                "instructions.vars must be a mapping of template variable "
                f"names to values; got {type(prompt_vars).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        cache_ttl = raw_value.get("cache_ttl")
        if cache_ttl is not None and not isinstance(cache_ttl, (int, float)):
            raise OmnigentError(
                "instructions.cache_ttl must be a number of seconds; got "
                f"{type(cache_ttl).__name__}.",
                code=ErrorCode.INVALID_INPUT,
            )
        return MlflowPromptReference(
            reference=ref_value,
            vars=prompt_vars,
            tracking_uri=_optional_str(raw_value, "tracking_uri"),
            registry_uri=_optional_str(raw_value, "registry_uri"),
            cache_ttl=float(cache_ttl) if cache_ttl is not None else None,
        )

    return None


def resolve_mlflow_prompt(
    reference: str,
    *,
    tracking_uri: str | None = None,
    registry_uri: str | None = None,
    vars: dict[str, object] | None = None,
    cache_ttl: float | None = None,
) -> str:
    """
    Load a prompt from the MLflow Prompt Registry and return its text.

    The prompt is resolved by version (``prompts:/name/1``) or alias
    (``prompts:/name@production``). Pinned versions are immutable so MLflow
    caches them indefinitely; aliases are mutable so MLflow applies its own
    TTL (default 60s, or *cache_ttl* / the ``MLFLOW_*_PROMPT_CACHE_TTL_SECONDS``
    env vars) — the cache is owned entirely by MLflow, never wrapped here, so an
    alias re-point is picked up within the TTL rather than pinned forever.

    :param reference: A ``prompts:/`` URI.
    :param tracking_uri: Optional MLflow tracking URI (OSS backend). Applied
        via ``mlflow.set_tracking_uri`` before the load.
    :param registry_uri: Optional MLflow registry URI. Set to
        ``"databricks-uc"`` (with a 3-part UC name in *reference*) for the
        Databricks-managed registry; applied via ``mlflow.set_registry_uri``.
    :param vars: Template variables. When provided, the template is rendered
        with ``PromptVersion.format(**vars)`` (which honors ``{{var}}`` and
        ``{{{{escaped}}}}`` double-brace semantics); when omitted, the raw
        template text is returned.
    :param cache_ttl: Per-call cache TTL in seconds forwarded to MLflow's
        ``cache_ttl_seconds`` (0 disables the cache).
    :returns: The resolved prompt text.
    :raises OmnigentError: If ``mlflow`` is not installed, the reference points
        at a chat-style (message-list) prompt, or the registry load fails
        (unreachable registry, missing prompt/alias, auth error).
    """
    try:
        import mlflow
        from mlflow.exceptions import MlflowException
        from mlflow.genai import load_prompt
    except ImportError as exc:
        raise OmnigentError(
            "instructions reference an MLflow Prompt Registry entry "
            f"({reference!r}) but the 'mlflow' package is not installed. "
            "Install it with `pip install 'omnigent[mlflow]'` (or add mlflow "
            "to your environment) and retry.",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    if tracking_uri is not None:
        mlflow.set_tracking_uri(tracking_uri)
    if registry_uri is not None:
        mlflow.set_registry_uri(registry_uri)

    try:
        # link_to_model=False: bundle load has no active model/run to bind to;
        # binding would fail or create a spurious link.
        prompt = load_prompt(
            reference,
            cache_ttl_seconds=cache_ttl,
            link_to_model=False,
        )
    except MlflowException as exc:
        raise OmnigentError(
            f"failed to load MLflow prompt {reference!r}: {exc}. Verify the "
            "prompt name, version/alias, registry URI, and that auth is "
            "configured in the environment.",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    template = prompt.template
    if not isinstance(template, str):
        raise OmnigentError(
            f"MLflow prompt {reference!r} is a chat-style prompt (a list of "
            "messages); agent instructions must be a single text string. Use a "
            "text prompt whose template is a plain string.",
            code=ErrorCode.INVALID_INPUT,
        )

    _log.info("resolved MLflow prompt %r version %s", prompt.name, prompt.version)

    if vars:
        return str(prompt.format(**vars))
    return template


def _require_prompt_uri(reference: str) -> None:
    if not reference.startswith(_PROMPT_URI_SCHEME):
        raise OmnigentError(
            f"MLflow instructions reference must be a '{_PROMPT_URI_SCHEME}' "
            f"URI (e.g. 'prompts:/greeting@production'); got {reference!r}.",
            code=ErrorCode.INVALID_INPUT,
        )


def _optional_str(mapping: dict[str, object], key: str) -> str | None:
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OmnigentError(
            f"instructions.{key} must be a string; got {type(value).__name__}.",
            code=ErrorCode.INVALID_INPUT,
        )
    return value
