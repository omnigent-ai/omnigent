"""Model-override validation helpers shared across runner/server paths.

A per-session model override crosses a spawn boundary: the persisted
value reaches the native CLIs as a ``--model`` argv element at terminal
launch and the SDK harnesses as a ``HARNESS_<H>_MODEL`` env var. The
helpers here keep that string data-only — a conservative model-id
charset rejects anything shell- or flag-shaped before it is persisted.
"""

from __future__ import annotations

import re

from omnigent.harness_aliases import canonicalize_harness, is_native_harness

# Generous-but-safe upper bound; real ids ("databricks-claude-opus-4-8",
# "us.anthropic.claude-sonnet-4-6") stay well under it.
MODEL_OVERRIDE_MAX_LEN = 256

# First char alphanumeric so the value can never read as a CLI flag
# (``--model --evil``); the tail covers real id shapes: dots
# ("gpt-5.4-mini"), slashes ("openai/gpt-4o"), colons ("vendor:tag"),
# and bracket suffixes ("claude-opus-4-8[1m]").
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*$")

# SDK harnesses whose model override lands in the spawn env — must stay
# in sync with ``_HARNESS_MODEL_ENV_KEY`` in ``omnigent/runner/app.py``.
_SDK_MODEL_OVERRIDE_HARNESSES: frozenset[str] = frozenset(
    {"claude-sdk", "codex", "pi", "openai-agents", "cursor", "antigravity", "qwen", "goose"}
)


def validate_model_override(value: str) -> str:
    """
    Validate a caller-supplied model override and return it stripped.

    :param value: Raw model id, e.g. ``"databricks-claude-sonnet-4-6"``.
    :returns: The stripped model id.
    :raises ValueError: If the value is empty, too long, or contains
        characters outside the conservative model-id charset.
    """
    stripped = value.strip()
    if not stripped:
        raise ValueError("model must be a non-empty string")
    if len(stripped) > MODEL_OVERRIDE_MAX_LEN:
        raise ValueError(f"model exceeds {MODEL_OVERRIDE_MAX_LEN} characters")
    if not _MODEL_ID_RE.fullmatch(stripped):
        raise ValueError(
            "model must start with a letter or digit and contain only "
            "letters, digits, and the characters . _ : / [ ] -"
        )
    return stripped


# Single-vendor harnesses only run their own vendor's models; multi-model
# harnesses (pi, openai-agents) accept any validated id.
# Reversed native spellings are valid harness ids (NATIVE_HARNESSES)
# that canonicalize_harness passes through, so list them explicitly —
# likewise the executor-type spelling ("claude_sdk") that spec_harness()
# yields when a claude spec declares no config harness.
_CLAUDE_FAMILY_HARNESSES: frozenset[str] = frozenset(
    {"claude-native", "native-claude", "claude-sdk", "claude_sdk"}
)
# codex stays single-vendor (GPT-only): the Databricks gateway only serves
# codex over the Anthropic-incompatible Responses wire, and codex >= 0.137
# dropped the chat/completions wire that was the only path to Claude — so a
# codex x Claude dispatch is genuinely broken and must fail loud here.
# openai-agents (and its "openai-agents-sdk" / "agents_sdk" spellings) is
# intentionally NOT in this set: a live SDK probe completed a Claude
# tool-calling turn on the gateway over the chat wire, so the harness is
# multi-model like pi and accepts any validated id (no family rejection).
_CODEX_FAMILY_HARNESSES: frozenset[str] = frozenset({"codex", "codex-native", "native-codex"})
# antigravity is Gemini-native: it authenticates a direct Gemini API key /
# Vertex AI and has no Databricks/gateway path (see _build_antigravity_spawn_env
# in omnigent/runtime/workflow.py). So unlike the single-vendor harnesses above,
# the rule here is framed as a *reject-list* of the families it definitively
# cannot serve (Claude / GPT, and any ``databricks-``-prefixed gateway id),
# rather than a strict Gemini allow-list — bare/ambiguous ids (e.g. a future
# ``gemini-pro`` alias the SDK accepts) still pass through to the Gemini-native
# SDK path. Mirrors how the cross-family rejection above fails loud at the
# dispatch gate instead of leaking a ``HARNESS_ANTIGRAVITY_MODEL`` the SDK can
# never route.
_ANTIGRAVITY_FAMILY_HARNESSES: frozenset[str] = frozenset(
    {"antigravity", "agy", "google-antigravity"}
)
# A ``databricks-`` gateway prefix marks an id bound to the Databricks gateway,
# which antigravity never reaches — a definitive mismatch on its own.
_DATABRICKS_GATEWAY_PREFIX = "databricks-"


def model_family_mismatch(harness: str, model: str) -> str | None:
    """
    Return a rejection reason when *model*'s family cannot run on *harness*.

    Family is detected by vendor token: Claude ids contain ``"claude"``
    (``databricks-claude-opus-4-8``), GPT ids contain ``"gpt"`` or
    ``"codex"`` (``databricks-gpt-5-4``). Single-vendor harnesses reject
    the other family and ids whose family cannot be determined — failing
    loud at dispatch beats an opaque harness/gateway error after spawn.
    The Gemini-native ``antigravity`` harness rejects the Claude/GPT
    families and any ``databricks-`` gateway id (it has no gateway path),
    but accepts Gemini shapes and bare/ambiguous ids the SDK may honor.
    Multi-model harnesses (pi, openai-agents) accept any validated id.

    :param harness: Harness id from the sub-agent spec, alias or
        canonical, e.g. ``"claude-native"``.
    :param model: Model id that already passed
        :func:`validate_model_override`.
    :returns: Human-readable reason, or ``None`` when compatible.
    """
    canon = canonicalize_harness(harness)
    lower = model.lower()
    is_claude = "claude" in lower
    is_gpt = "gpt" in lower or "codex" in lower
    if canon in _CLAUDE_FAMILY_HARNESSES and not is_claude:
        return (
            f"harness {canon!r} only runs Claude models (id containing "
            f"'claude'); got {model!r}. Use the codex worker for GPT models "
            "or the pi / openai-agents worker for any other gateway model."
        )
    if canon in _CODEX_FAMILY_HARNESSES and not is_gpt:
        return (
            f"harness {canon!r} only runs GPT models (id containing 'gpt' "
            f"or 'codex'); got {model!r}. Use the claude_code worker for "
            "Claude models or the pi / openai-agents worker for any other "
            "gateway model."
        )
    if canon in _ANTIGRAVITY_FAMILY_HARNESSES and (
        is_claude or is_gpt or lower.startswith(_DATABRICKS_GATEWAY_PREFIX)
    ):
        return (
            f"harness {canon!r} is Gemini-native and cannot run Claude/GPT or "
            f"Databricks-gateway models; got {model!r}. Use a Gemini id "
            "(e.g. 'gemini-3.5-flash'), or the claude_code / codex / pi worker "
            "for those families."
        )
    return None


# Bare canonical vendor ids ("claude-opus-4-8", "gpt-5-4"); slash/colon/
# bracket/vendor-prefixed shapes have no mechanical gateway counterpart.
_MECHANICAL_VENDOR_ID_RE = re.compile(r"^(?:claude|gpt)-[a-z0-9][a-z0-9.-]*$")

_DATABRICKS_MODEL_PREFIX = "databricks-"

# Provider kinds whose endpoints take bare canonical vendor ids.
_VENDOR_DIRECT_PROVIDER_KINDS = frozenset({"key", "subscription"})


def canonical_model_spelling(model: str) -> str:
    """
    Return the canonical (gateway-prefix-free) spelling of *model*.

    A bare canonical vendor id and its mechanical ``databricks-``
    counterpart name the same model — :func:`normalize_model_for_provider`
    converts between them per provider — so comparisons that must treat
    the two spellings as equivalent (e.g. cost-tier ranking in
    :mod:`omnigent.cost_plan`) compare in this form.

    :param model: A model id, e.g. ``"databricks-claude-haiku-4-5"``.
    :returns: The bare canonical id (``"claude-haiku-4-5"``) when the
        prefix is mechanical; otherwise *model* unchanged (slash/colon/
        bracket shapes and non-claude/gpt families have no mechanical
        gateway counterpart).
    """
    if model.startswith(_DATABRICKS_MODEL_PREFIX):
        bare = model[len(_DATABRICKS_MODEL_PREFIX) :]
        if _MECHANICAL_VENDOR_ID_RE.fullmatch(bare):
            return bare
    return model


def normalize_model_for_provider(model: str, provider_kind: str | None) -> str:
    """
    Mechanically localize *model* for the child's resolved provider.

    Runs at the ``sys_session_send`` dispatch gate AFTER the family
    guard, which validates the caller's requested id verbatim (family
    tokens survive this transform in both directions, so the verdict is
    order-independent — checking first keeps error text quoting exactly
    what the caller sent). Two transforms, both prefix-mechanical:

    - Databricks-gateway child + bare canonical claude/gpt id →
      prepend ``databricks-`` (``claude-opus-4-8`` →
      ``databricks-claude-opus-4-8``).
    - Vendor-direct child (API key / CLI subscription) +
      ``databricks-``-prefixed claude/gpt id → strip the prefix
      (``databricks-gpt-5-4`` → ``gpt-5-4``).

    Anything non-mechanical (slash/colon/bracket shapes, non-claude/gpt
    families, gateway/local/unknown provider kinds) passes through
    unchanged — the existing fail-loud harness/gateway error remains
    the safety net for genuinely unroutable ids.

    :param model: A model id that already passed
        :func:`validate_model_override`, e.g. ``"claude-sonnet-4-6"``.
    :param provider_kind: The child's resolved provider kind from
        :func:`omnigent.model_catalog.resolve_model_provider`, e.g.
        ``"databricks"`` or ``"key"``; ``None`` when undeterminable.
    :returns: The localized model id, or *model* unchanged.
    """
    if provider_kind == "databricks":
        if _MECHANICAL_VENDOR_ID_RE.fullmatch(model):
            return _DATABRICKS_MODEL_PREFIX + model
        return model
    if provider_kind in _VENDOR_DIRECT_PROVIDER_KINDS:
        return canonical_model_spelling(model)
    return model


def harness_supports_model_override(harness: str | None) -> bool:
    """
    Return whether *harness* has per-session model-override plumbing.

    Native CLIs (claude-native / codex-native) receive the override as
    ``--model`` at terminal launch; the SDK harnesses receive it via
    ``HARNESS_<H>_MODEL`` in the spawn env. Anything else (e.g.
    unknown harnesses) silently ignores the
    persisted value, so callers must reject the override up front.

    :param harness: Harness id from a spec, e.g. ``"codex-native"`` or
        ``"claude"``; ``None`` when the harness could not be resolved.
    :returns: ``True`` when the override reaches the harness process.
    """
    if harness is None:
        return False
    return (
        is_native_harness(harness)
        or canonicalize_harness(harness) in _SDK_MODEL_OVERRIDE_HARNESSES
    )
