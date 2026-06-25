"""Read/write the kind-typed model-provider config in ``~/.omnigent/config.yaml``.

For open-source users who route coding agents through a non-Databricks
endpoint (a vendor API key, a subscription CLI login, a gateway like
OpenRouter, a local Ollama, or a Databricks profile), the ``providers:``
block in ``~/.omnigent/config.yaml`` is the source of truth for the
active model selection. Defaults are **per family**: a provider marked
**``default: true``** is the default for the family/families it serves,
so a Claude (``anthropic``) default and a Codex (``openai``) default
coexist (at most one default per family). This mirrors how multi-model
tools key defaults by purpose — Zed's per-feature model pointers,
Continue's per-role models, Goose's lead/worker. (A per-agent spec can
still select a provider explicitly via ``executor.auth: {type: provider,
name: <name>}`` — independent of these defaults.)

This module is the data layer of the model-selection feature (chunk 1a of
``designs/oss-cuj/04-model-selection-implementation.md``). It extends the
family-based provider shape with:

- an explicit **kind** per provider entry — one of ``key`` /
  ``subscription`` / ``gateway`` / ``local`` / ``databricks`` — so the
  readout and routing can describe *how* a provider authenticates;
- a **secret reference** (``api_key_ref: env:<VAR>`` or
  ``keychain:<name>``) as an alternative to an inline ``api_key: $VAR``;
- an **introspection** helper (:func:`describe_active_credential`) that
  resolves the active provider + harness into a :class:`ResolvedCredential`
  for the ``/model`` readout and the ``models`` command — never exposing
  the secret value itself.

Each inline-credential provider (``key`` / ``gateway`` / ``local``) groups
one or more *families*:

- ``anthropic`` — the Anthropic Messages API surface, consumed by the
  Claude SDK harness and native Claude Code.
- ``openai`` — the OpenAI Responses/Chat surface, consumed by the Codex
  harness, native Codex, and the OpenAI-Agents SDK harness.
- ``gemini`` — the Google Gemini surface, the credential the native
  Antigravity (``agy``) onboarding adopts as a ``key``-kind ``gemini:``
  provider from a detected ``GEMINI_API_KEY``.

The ``pi`` harness consumes the ``anthropic`` / ``openai`` families only —
never ``gemini``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Literal

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.spec.parser import check_unresolved_env_vars

# Family keys. ``anthropic`` is the Messages-API surface (Claude SDK,
# native Claude); ``openai`` is the Responses/Chat surface (Codex,
# native Codex, OpenAI-Agents SDK). ``pi`` consumes both. ``gemini`` is the
# Google Gemini surface — the credential the native Antigravity (``agy``)
# onboarding adopts as a ``key``-kind provider with a ``gemini:`` block from a
# detected ``GEMINI_API_KEY``. It is key-only: a gateway/local proxy cannot
# drive it, and the ``antigravity-native`` (``agy``) OAuth flavor consumes no
# provider credential at all (its readiness is the file-based OAuth check in
# :mod:`omnigent.onboarding.gemini_auth`, not a ``gemini:`` family here).
ANTHROPIC_FAMILY = "anthropic"
OPENAI_FAMILY = "openai"
GEMINI_FAMILY = "gemini"
# ``gemini`` joins the inline-credential families so a ``providers: gemini:``
# block (the detected GEMINI_API_KEY path) parses and validates like the
# anthropic / openai families.
_VALID_FAMILIES = (ANTHROPIC_FAMILY, OPENAI_FAMILY, GEMINI_FAMILY)

# Families the unmapped ``pi`` surface can actually consume, in fallback
# preference order. Gemini is EXCLUDED: a gemini key serves ONLY the Gemini
# surface, never pi. The cross-family pi fallbacks below must walk THIS list,
# not ``_VALID_FAMILIES`` — else a machine whose only credential is a gemini
# key resolves pi to a credential pi cannot use.
_PI_FALLBACK_FAMILIES = (ANTHROPIC_FAMILY, OPENAI_FAMILY)

# The pi harness's *default scope*. pi is not a model family — a provider
# entry never carries a ``pi:`` block — but defaults are scoped per harness
# surface, and pi consumes both families, so it gets its own scope name a
# ``default:`` value may reference (``default: ["anthropic", "pi"]``).
# Every provider kind except ``subscription`` can drive pi (a claude/codex
# CLI login is unusable outside its own CLI), so only those kinds may claim
# this scope. Resolution: an explicit pi default wins; otherwise pi falls
# back to the anthropic then openai family default, skipping subscriptions
# (see :func:`default_provider_for_harness`).
PI_SURFACE = "pi"

# Accepted ``wire_api`` values. ``responses`` is the OpenAI Responses API;
# ``chat`` is Chat Completions. Only meaningful for the ``openai`` family
# (the ``anthropic`` family always speaks the Messages API).
RESPONSES_WIRE_API = "responses"
CHAT_WIRE_API = "chat"
_VALID_WIRE_API = (RESPONSES_WIRE_API, CHAT_WIRE_API)

# Provider kinds (the top-level ``kind:`` discriminator on a provider entry).
# - ``key``: a vendor API key reached via families (Anthropic / OpenAI).
# - ``subscription``: a logged-in CLI (``claude`` / ``codex``) — no families,
#   no base_url; the CLI carries its own auth.
# - ``gateway``: an OpenAI/Anthropic-compatible proxy (OpenRouter, LiteLLM).
# - ``local``: a self-hosted endpoint (Ollama, vLLM) reached via families.
# - ``databricks``: a Databricks profile from ``~/.databrickscfg``.
# - ``cli-config``: a custom model provider the harness CLI's own config
#   file defines and authenticates (today: a ``[model_providers.X]`` table
#   in ``~/.codex/config.toml`` with self-contained auth, e.g. written by
#   ``isaac configure codex``). The entry pins the provider *by name*; the
#   provider definition and credential stay in the CLI's config file.
KEY_KIND = "key"
SUBSCRIPTION_KIND = "subscription"
GATEWAY_KIND = "gateway"
LOCAL_KIND = "local"
DATABRICKS_KIND = "databricks"
CLI_CONFIG_KIND = "cli-config"
BEDROCK_KIND = "bedrock"
_VALID_KINDS = (
    KEY_KIND,
    SUBSCRIPTION_KIND,
    GATEWAY_KIND,
    LOCAL_KIND,
    DATABRICKS_KIND,
    CLI_CONFIG_KIND,
    BEDROCK_KIND,
)

# Provider kinds that resolve their model/credentials from inline families
# (``key`` / ``gateway`` / ``local``) — as opposed to ``subscription`` (a
# CLI login) and ``databricks`` (a profile), neither of which carries
# families. _parse_provider dispatches subscription/databricks first, then
# treats every remaining kind as a family kind.

ProviderKind = Literal[
    "key", "subscription", "gateway", "local", "databricks", "cli-config", "bedrock"
]

# Maps a canonical harness name to the provider family it consumes. The
# ``pi`` harness consumes both families and so is absent here — callers
# needing a single family for ``pi`` fall back to whichever family the
# active provider configures (see :func:`_family_for_harness`).
_HARNESS_FAMILY: dict[str, str] = {
    "claude-sdk": ANTHROPIC_FAMILY,
    # Native CLI harnesses. Specs (and the omnigent-compat translator)
    # spell these "claude-native" / "codex-native"; the reversed
    # "native-claude" / "native-codex" spellings are also accepted so a
    # caller passing either form resolves. Without the canonical spelling
    # here, a claude-native / codex-native agent's credential failed to
    # resolve for the `/model` readout and the startup-header creds line.
    "claude-native": ANTHROPIC_FAMILY,
    "native-claude": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
    "codex-native": OPENAI_FAMILY,
    "native-codex": OPENAI_FAMILY,
    "openai-agents": OPENAI_FAMILY,
    # The workflow's AgentHarnessType spells this "openai-agents-sdk" and
    # normally maps it down via _provider_harness_name; accept both spellings
    # here so callers that pass the spec/CLI spelling directly resolve too.
    "openai-agents-sdk": OPENAI_FAMILY,
    # Antigravity is Gemini-native but routes generic-provider traffic over
    # the OpenAI-compatible wire, so it consumes the ``openai`` family.
    "antigravity": OPENAI_FAMILY,
    # NB: ``kimi`` is intentionally absent. Upstream Kimi Code CLI has no
    # per-spawn provider override flag, so Omnigent cannot thread a generic
    # provider through. Provider routing for kimi lives in ``~/.kimi/config.toml``
    # and is managed out-of-band via ``kimi provider add``.
    # Qwen Code uses an OpenAI-compatible provider.
    "qwen": OPENAI_FAMILY,
    # The native agy TUI bridge authenticates via the Gemini OAuth credential
    # (file-based, checked in :mod:`omnigent.onboarding.gemini_auth`) and the
    # detected GEMINI_API_KEY is adopted as a ``gemini``-family key, so the
    # native harness consumes the ``gemini`` family for onboarding / readiness.
    # Both spellings are accepted, mirroring claude-native / native-claude.
    "antigravity-native": GEMINI_FAMILY,
    "native-antigravity": GEMINI_FAMILY,
}

# Executor-type spellings that ``AgentSpec.harness_kind`` returns for SDK
# harnesses (the executor *type*, not a ``config["harness"]`` value) mapped
# to the canonical harness ids keyed in :data:`_HARNESS_FAMILY`.
_EXECUTOR_TYPE_HARNESS_ALIASES: dict[str, str] = {
    "claude_sdk": "claude-sdk",
    "agents_sdk": "openai-agents",
}


def provider_family_for_harness(harness: str | None) -> str | None:
    """Return the provider family a harness consumes, or ``None``.

    Maps a harness identifier to ``"anthropic"`` / ``"openai"``. Accepts both
    the canonical ids keyed in :data:`_HARNESS_FAMILY` and the executor-type
    spellings :attr:`AgentSpec.harness_kind` returns for SDK harnesses
    (``"claude_sdk"`` → ``"claude-sdk"``, ``"agents_sdk"`` →
    ``"openai-agents"``).

    Used to decide whether model settings (``model_override`` /
    ``reasoning_effort``) survive a fork that switches the agent, and whether
    a fork into a native target can carry history (same-family only): a model
    id is provider-bound, so it only transfers within the same family.

    :param harness: A harness id, e.g. ``"claude-native"`` or
        ``"claude_sdk"``; ``None`` returns ``None``.
    :returns: ``"anthropic"`` / ``"openai"``, else ``None`` when the harness
        is unknown.
    """
    if harness is None:
        return None
    canonical = canonicalize_harness(harness) or harness
    canonical = _EXECUTOR_TYPE_HARNESS_ALIASES.get(canonical, canonical)
    return harness_family(canonical)


@dataclass(frozen=True)
class FamilyConfig:
    """One provider family (``anthropic`` or ``openai``) for a harness surface.

    Carries the gateway/endpoint base URL plus exactly one secret source:
    an inline ``api_key`` (possibly a ``$VAR`` reference), an
    ``api_key_ref`` (``env:<VAR>`` / ``keychain:<name>``), or a dynamic
    ``auth_command``. The secret is resolved lazily — see
    :meth:`Provider.family` and :func:`resolve_secret` — so a family the
    active harness does not consume never forces its secret to exist.

    :param base_url: Endpoint base URL the harness talks to, e.g.
        ``"https://openrouter.ai/api/v1"`` (a gateway) or
        ``"http://localhost:11434/v1"`` (a local Ollama). Required. As
        stored on :attr:`Provider.families` this may still contain a raw
        ``$VAR`` reference; it is expanded by :meth:`Provider.family`.
    :param api_key: Inline static API key, possibly a ``$VAR`` reference
        (expanded by :meth:`Provider.family`), e.g. ``"$OPENROUTER_API_KEY"``
        or a literal ``"sk-or-..."``. Mutually exclusive with
        :attr:`api_key_ref` and :attr:`auth_command`.
    :param api_key_ref: A reference to a secret stored outside the config
        file: ``"env:<VAR>"`` (read from the environment) or
        ``"keychain:<name>"`` (read from the omnigent secret store — see
        :func:`resolve_secret` and :mod:`omnigent.onboarding.secrets`),
        e.g. ``"keychain:anthropic"``. Mutually exclusive with
        :attr:`api_key` and :attr:`auth_command`.
    :param auth_command: Shell command that prints a bearer token, for
        short-lived / dynamic tokens, e.g. ``"my-cli print-token"``.
        Mutually exclusive with :attr:`api_key` and :attr:`api_key_ref`.
    :param wire_api: Wire protocol for the ``openai`` family —
        ``"responses"`` or ``"chat"``. ``None`` lets the consuming harness
        pick its own default. Ignored for the ``anthropic`` family.
    :param models: Map of role/tier to model id, with an optional
        ``default`` key consulted when the spec declares no model, e.g.
        ``{"default": "gpt-4o", "opus": "claude-opus-4"}``.
    """

    base_url: str
    api_key: str | None = None
    api_key_ref: str | None = None
    auth_command: str | None = None
    wire_api: str | None = None
    models: dict[str, str] = field(default_factory=dict)

    @property
    def default_model(self) -> str | None:
        """Return the family's default model id, or ``None``.

        :returns: The ``models["default"]`` entry, e.g. ``"gpt-4o"``, or
            ``None`` when the family declares no default.
        """
        return self.models.get("default")


@dataclass(frozen=True)
class ProviderEntry:
    """A named, kind-typed provider from the ``providers:`` config block.

    The :attr:`kind` discriminates how the provider authenticates:

    - ``key`` / ``gateway`` / ``local`` carry :attr:`families` (and no
      :attr:`cli` / :attr:`profile`).
    - ``subscription`` carries :attr:`cli` (``"claude"`` / ``"codex"``)
      and no families/base_url — the CLI's own login supplies auth.
    - ``databricks`` carries :attr:`profile` and no families — auth is
      resolved from ``~/.databrickscfg`` + ucode state by the runtime.
    - ``cli-config`` carries :attr:`cli` (``"codex"`` only today) and
      :attr:`model_provider` — the provider definition and credential live
      in the CLI's own config file (``~/.codex/config.toml``); the entry
      just pins which ``[model_providers.X]`` the launch selects.

    :param name: The provider name as keyed under ``providers:``, e.g.
        ``"anthropic"`` or ``"openrouter"``.
    :param kind: The provider kind, one of ``"key"`` / ``"subscription"``
        / ``"gateway"`` / ``"local"`` / ``"databricks"``.
    :param families: Parsed families keyed by family name
        (``"anthropic"`` / ``"openai"``), e.g.
        ``{"openai": FamilyConfig(...)}``. Empty for ``subscription`` /
        ``databricks`` kinds. The ``base_url`` / ``api_key`` values stored
        here may still hold raw ``$VAR`` references — they are expanded
        lazily by :meth:`family`, so consumers should go through
        :meth:`family` rather than indexing :attr:`families` directly.
    :param cli: For ``kind="subscription"`` and ``kind="cli-config"``: the
        CLI whose login / config file carries auth, ``"claude"`` or
        ``"codex"`` (``cli-config`` supports only ``"codex"`` today).
        ``None`` otherwise.
    :param profile: For ``kind="databricks"`` only: the Databricks profile
        name from ``~/.databrickscfg``, e.g. ``"oss"``. ``None`` otherwise.
    :param model_provider: For ``kind="cli-config"`` only: the custom
        provider id in the CLI's config file that the launch pins, i.e. the
        ``X`` in ``[model_providers.X]``, e.g. ``"Databricks"``. ``None``
        otherwise.
    :param display_name: For ``kind="cli-config"`` only: the provider's
        human display name (the table's ``name`` field, snapshotted at
        adoption), e.g. ``"Databricks AI Gateway"``. ``None`` otherwise and
        when the table named none.
    :param default_families: The set of model families this provider is
        the **default** for. Sourced from the entry's ``default:`` flag:
        ``true`` → every family it serves (:func:`provider_families`); a
        family name (``default: openai``) or list (``default: [anthropic]``)
        → just those. Empty when not a default. Per-family scoping lets a
        shared provider (gateway / OpenRouter / Databricks) be the default
        for one harness's surface without claiming the other. At most one
        default may serve a given family (enforced in
        :func:`get_default_provider`). See
        :func:`get_default_provider` / :func:`default_provider_for_harness`.
    """

    name: str
    kind: ProviderKind
    families: dict[str, FamilyConfig] = field(default_factory=dict)
    cli: str | None = None
    profile: str | None = None
    model_provider: str | None = None
    display_name: str | None = None
    default_families: frozenset[str] = frozenset()

    @property
    def default(self) -> bool:
        """Whether this provider is the default for any family it serves.

        Backward-compatible accessor: ``True`` iff :attr:`default_families`
        is non-empty. Use :attr:`default_families` when the *which family*
        matters (per-harness defaults).

        :returns: ``True`` when the provider is a default for ≥1 family.
        """
        return bool(self.default_families)

    def family(self, name: str) -> FamilyConfig | None:
        """Return the parsed family for *name* with ``$VAR`` refs expanded.

        Expansion (and the fail-loud check for unresolved variables) is
        deferred to here rather than done at read time, so a provider that
        declares a family the current harness does not consume never forces
        the unused family's env var to exist. Only the family actually
        retrieved is expanded. ``api_key_ref`` is resolved here too via
        :func:`resolve_secret`.

        :param name: Family key, e.g. ``"anthropic"`` or ``"openai"``.
        :returns: The :class:`FamilyConfig` with ``base_url`` resolved and,
            when present, the inline ``api_key`` / ``api_key_ref`` resolved
            into :attr:`FamilyConfig.api_key`. ``None`` when this provider
            does not configure that family.
        :raises OmnigentError: If the family references an unset
            environment variable, or a ``keychain:`` reference names a
            secret that is not stored (see :func:`resolve_secret`).
        """
        raw = self.families.get(name)
        if raw is None:
            return None
        return _expand_family(self.name, name, raw)

    def family_default_model(self, name: str) -> str | None:
        """Return a family's default model id **without** resolving credentials.

        Unlike :meth:`family`, this does not touch ``api_key`` /
        ``api_key_ref`` / ``base_url``, so it never forces a secret to
        exist. Used to pick a default model before the consuming harness
        (and thus the required family) is known.

        :param name: Family key, e.g. ``"openai"`` or ``"anthropic"``.
        :returns: The family's ``models["default"]``, e.g. ``"gpt-4o"``,
            or ``None`` when the family is absent or declares no default.
        """
        raw = self.families.get(name)
        return raw.default_model if raw is not None else None


@dataclass(frozen=True)
class ResolvedCredential:
    """A human-readable description of the active credential for a harness.

    Built by :func:`describe_active_credential` for the ``/model`` readout
    and the ``models`` command. It never carries the secret value — only a
    descriptor of where the secret comes from (:attr:`source`).

    :param provider_name: The active provider's name, e.g. ``"anthropic"``.
    :param kind: The provider's kind, e.g. ``"key"`` or ``"subscription"``.
    :param family: The family selected for the harness (``"anthropic"`` /
        ``"openai"``), or ``None`` for kinds without families
        (``subscription`` / ``databricks``).
    :param model: The resolved model id (override > family default), e.g.
        ``"claude-sonnet-4-6"``, or ``None`` when no model is determinable
        (e.g. a subscription whose model the CLI picks).
    :param source: A non-secret descriptor of where the credential comes
        from, suitable for display, e.g. ``"$ANTHROPIC_API_KEY"``,
        ``"env:ANTHROPIC_API_KEY"``, ``"keychain:anthropic"``,
        ``"claude CLI login"``, or ``"profile: oss"``.
    :param base_url: The endpoint base URL for inline-family kinds, e.g.
        ``"https://openrouter.ai/api/v1"``, or ``None`` for
        ``subscription`` / ``databricks`` kinds (no inline base URL).
    """

    provider_name: str
    kind: ProviderKind
    family: str | None
    model: str | None
    source: str
    base_url: str | None


def resolve_secret(ref: str) -> str:
    """Resolve a secret *ref* into its plaintext value, failing loud.

    Accepts three shapes:

    - ``"env:<VAR>"`` — read ``<VAR>`` from the environment.
    - a bare inline ``$VAR`` / ``${VAR}`` reference — expanded via
      :func:`os.path.expandvars` with an unresolved-variable check.
    - ``"keychain:<name>"`` — read ``<name>`` from the omnigent secret
      store (OS keychain, else a ``0600`` JSON file). The store is
      populated by ``omnigent setup --no-internal-beta`` — see
      :mod:`omnigent.onboarding.secrets`.

    :param ref: The secret reference, e.g. ``"env:OPENROUTER_API_KEY"``,
        ``"$ANTHROPIC_API_KEY"``, or ``"keychain:anthropic"``.
    :returns: The resolved secret value, e.g. ``"sk-or-..."``.
    :raises OmnigentError: If an ``env:`` / ``$VAR`` reference names an
        unset environment variable, or a ``keychain:`` reference names a
        secret that is not stored.
    """
    if ref.startswith("keychain:"):
        name = ref[len("keychain:") :]
        # Imported lazily to avoid a circular import: secrets.py is a leaf
        # module that must not import provider_config.
        from omnigent.onboarding import secrets

        value = secrets.load_secret(name)
        if value is None:
            raise OmnigentError(
                f"no stored secret named {name!r}; run "
                "`omnigent setup --no-internal-beta` to set it.",
                code=ErrorCode.INVALID_INPUT,
            )
        return value
    if ref.startswith("env:"):
        var = ref[len("env:") :]
        value = os.environ.get(var)
        if value is None:
            raise OmnigentError(
                f"Unresolved environment variable '${var}' referenced by "
                f"'env:{var}'. Set the variable in the environment.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Strip surrounding whitespace: a key exported with a stray trailing
        # newline (e.g. ``export KEY=$(cat file)``) must not be forwarded
        # verbatim to a harness/SDK, where the padding fails auth.
        return value.strip()
    # Bare inline reference, e.g. "$ANTHROPIC_API_KEY" or a literal value.
    expanded = os.path.expandvars(ref)
    check_unresolved_env_vars(ref, expanded)
    return expanded


def _config_path() -> str:
    """Return the path to the global omnigent config file.

    Respects ``$OMNIGENT_CONFIG_HOME`` for test isolation (matching the
    rest of the onboarding layer).

    :returns: Path to ``config.yaml``, e.g.
        ``"/home/u/.omnigent/config.yaml"``.
    """
    config_home = os.environ.get("OMNIGENT_CONFIG_HOME")
    if config_home:
        return os.path.join(config_home, "config.yaml")
    return os.path.join(os.path.expanduser("~"), ".omnigent", "config.yaml")


def _load_config() -> dict[str, object]:
    """Load and parse the global config file.

    :returns: The parsed YAML mapping, or ``{}`` when the file is missing,
        empty, or unreadable.
    """
    import yaml

    path = _config_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _expand(key: str, value: str) -> str:
    """Expand ``$VAR`` references in *value* and fail loud if unresolved.

    :param key: Config key path for error messages, e.g.
        ``"providers.openrouter.openai.api_key"``.
    :param value: The raw value possibly containing ``$VAR`` / ``${VAR}``
        references, e.g. ``"$OPENROUTER_API_KEY"``.
    :returns: The expanded value, e.g. ``"sk-or-..."``.
    :raises OmnigentError: If a referenced variable is unset.
    """
    expanded = os.path.expandvars(value)
    check_unresolved_env_vars(key, expanded)
    return expanded


def _expand_family(provider_name: str, family_name: str, family: FamilyConfig) -> FamilyConfig:
    """Return a copy of *family* with ``base_url`` + the secret resolved.

    Called by :meth:`ProviderEntry.family` so resolution (and the
    fail-loud checks) happen only for the family a harness actually
    consumes. The inline ``api_key`` (``$VAR``) and ``api_key_ref``
    (``env:`` / ``keychain:``) are both collapsed into the returned
    family's :attr:`FamilyConfig.api_key`; an ``auth_command`` is left
    untouched (resolved at the harness boundary, not here).

    :param provider_name: Owning provider name, e.g. ``"openrouter"``
        (for error messages).
    :param family_name: Family key being expanded, e.g. ``"openai"``.
    :param family: The raw-valued :class:`FamilyConfig` from
        :attr:`ProviderEntry.families`.
    :returns: A new :class:`FamilyConfig` with ``base_url`` expanded and,
        when a static secret source is present, ``api_key`` resolved (with
        ``api_key_ref`` cleared).
    :raises OmnigentError: If a referenced environment variable is unset,
        or a ``keychain:`` reference names a secret that is not stored.
    """
    prefix = f"providers.{provider_name}.{family_name}"
    base_url = _expand(f"{prefix}.base_url", family.base_url)
    if family.api_key is not None:
        api_key = _expand(f"{prefix}.api_key", family.api_key)
        return replace(family, base_url=base_url, api_key=api_key)
    if family.api_key_ref is not None:
        resolved = resolve_secret(family.api_key_ref)
        return replace(family, base_url=base_url, api_key=resolved, api_key_ref=None)
    # auth_command (or, defensively, nothing) — leave the secret slot empty.
    return replace(family, base_url=base_url)


def _parse_family(provider_name: str, family_name: str, raw: dict[str, object]) -> FamilyConfig:
    """Parse one family entry under a provider into a :class:`FamilyConfig`.

    Performs **structural** validation only (required ``base_url``, exactly
    one credential source, valid ``wire_api``). ``$VAR`` references and
    ``api_key_ref`` are left **unresolved** here — resolved lazily by
    :meth:`ProviderEntry.family` via :func:`_expand_family`.

    :param provider_name: Owning provider name, e.g. ``"openrouter"``
        (for error messages).
    :param family_name: Family key being parsed, e.g. ``"openai"``.
    :param raw: The raw family mapping, e.g.
        ``{"base_url": "...", "api_key_ref": "keychain:openrouter",
        "models": {...}}``.
    :returns: A populated :class:`FamilyConfig` whose ``base_url`` /
        ``api_key`` may still contain raw ``$VAR`` references and whose
        ``api_key_ref`` is unresolved.
    :raises OmnigentError: If ``base_url`` is missing, the credential
        sources are not exactly one, or ``wire_api`` is invalid.
    """
    prefix = f"providers.{provider_name}.{family_name}"
    base_url_raw = raw.get("base_url")
    if not isinstance(base_url_raw, str) or not base_url_raw:
        raise OmnigentError(
            f"{prefix}.base_url is required and must be a string.",
            code=ErrorCode.INVALID_INPUT,
        )

    # Exactly one of {api_key, api_key_ref, auth_command} must be set.
    api_key_raw = raw.get("api_key")
    api_key_ref_raw = raw.get("api_key_ref")
    auth_command_raw = raw.get("auth_command")
    present = [
        ("api_key", api_key_raw),
        ("api_key_ref", api_key_ref_raw),
        ("auth_command", auth_command_raw),
    ]
    set_names = [n for n, v in present if isinstance(v, str) and v]
    if len(set_names) > 1:
        raise OmnigentError(
            f"{prefix} must set exactly one of 'api_key', 'api_key_ref', or "
            f"'auth_command', not multiple ({', '.join(set_names)}).",
            code=ErrorCode.INVALID_INPUT,
        )
    if not set_names:
        raise OmnigentError(
            f"{prefix} requires one of 'api_key', 'api_key_ref', or 'auth_command'.",
            code=ErrorCode.INVALID_INPUT,
        )

    # The ``gemini`` family is consumed by the antigravity harness, which drives
    # the google SDK with a STATIC GEMINI_API_KEY. ``auth_command`` mints a
    # bearer token — useless as a GEMINI_API_KEY — so an ``auth_command`` gemini
    # block is nonsensical. Reject it HERE (parse) so it never reaches
    # provider_families / default-resolution / the display+readiness layer /
    # spawn / ``/models``: every layer then agrees by construction. ``auth_command``
    # stays valid for anthropic/openai families (gateways / dynamic tokens).
    if family_name == GEMINI_FAMILY and isinstance(auth_command_raw, str) and auth_command_raw:
        raise OmnigentError(
            f"{prefix}.auth_command is not allowed on a 'gemini' family: the "
            "antigravity harness drives Gemini with a static GEMINI_API_KEY, and "
            "an auth_command mints a bearer token the google SDK cannot use as one. "
            "Use a static key source ('api_key' or 'api_key_ref') instead.",
            code=ErrorCode.INVALID_INPUT,
        )

    api_key = api_key_raw if isinstance(api_key_raw, str) and api_key_raw else None
    api_key_ref = api_key_ref_raw if isinstance(api_key_ref_raw, str) and api_key_ref_raw else None
    # auth_command is a user-controlled shell command, not a secret value,
    # so it is never env-expanded here (it may reference env vars that exist
    # only in the harness subprocess).
    auth_command = (
        auth_command_raw if isinstance(auth_command_raw, str) and auth_command_raw else None
    )

    wire_api_raw = raw.get("wire_api")
    wire_api: str | None = None
    if wire_api_raw is not None:
        wire_api = str(wire_api_raw)
        if wire_api not in _VALID_WIRE_API:
            raise OmnigentError(
                f"{prefix}.wire_api must be one of {_VALID_WIRE_API}, got {wire_api!r}.",
                code=ErrorCode.INVALID_INPUT,
            )

    models_raw = raw.get("models")
    models = (
        {str(k): str(v) for k, v in models_raw.items()} if isinstance(models_raw, dict) else {}
    )
    return FamilyConfig(
        base_url=base_url_raw,
        api_key=api_key,
        api_key_ref=api_key_ref,
        auth_command=auth_command,
        wire_api=wire_api,
        models=models,
    )


def _parse_default_families(
    name: str, default_raw: object, served: set[str], *, pi_capable: bool = False
) -> frozenset[str]:
    """Resolve a raw ``default:`` value into the scopes it applies to.

    Accepts every form the config can carry:

    - ``True`` / ``"true"`` → every family the provider serves.
    - ``False`` / ``"false"`` / absent / ``""`` → not a default.
    - a family name (``"openai"``) → just that family.
    - a list of family names (``["anthropic", "openai"]``) → those.

    A pi-capable provider may additionally name the :data:`PI_SURFACE`
    scope explicitly (``default: ["anthropic", "pi"]``). ``default: true``
    deliberately does **not** expand to the pi scope: ``true`` means "all
    families served", and two coexisting ``default: true`` providers (one
    per family) are valid — if ``true`` claimed pi on both, they would
    collide on the pi slot. The pi scope is only ever claimed explicitly.

    :param name: Provider name, for error messages.
    :param default_raw: The raw ``default`` value from the entry.
    :param served: The model families this provider actually serves; a
        default may only name these (plus ``"pi"`` when *pi_capable*).
    :param pi_capable: Whether this provider's kind can drive the pi
        harness (every kind except ``subscription``), allowing an explicit
        ``"pi"`` in the default scope.
    :returns: The (validated) scopes the provider is the default for.
    :raises OmnigentError: If ``default`` is an unsupported type, or names
        a scope the provider does not serve.
    """
    if default_raw is None or default_raw is False:
        return frozenset()
    if default_raw is True:
        return frozenset(served)
    if isinstance(default_raw, str):
        token = default_raw.strip().lower()
        if token in ("", "false"):
            return frozenset()
        if token == "true":
            return frozenset(served)
        requested = {default_raw.strip()}
    elif isinstance(default_raw, (list, tuple)):
        requested = {str(f).strip() for f in default_raw}
    else:
        raise OmnigentError(
            f"provider {name!r}: 'default' must be true/false, a family name, "
            f"or a list of family names, got {default_raw!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    # pi is a valid default scope only when the provider is a pi-capable KIND
    # (``pi_capable``) AND serves a pi-capable FAMILY (anthropic/openai). A
    # gemini-only key is an inline kind (``pi_capable=True``) but serves only
    # the Gemini surface, so it must NOT accept a pi scope — this mirrors
    # ``provider_families`` and rejects a hand-edited ``default: ["gemini",
    # "pi"]`` at parse time (parity with how a subscription's pi scope is
    # rejected), rather than failing loudly only at pi launch.
    pi_ok = pi_capable and bool(served & frozenset(_PI_FALLBACK_FAMILIES))
    allowed = served | {PI_SURFACE} if pi_ok else served
    invalid = requested - allowed
    if invalid:
        raise OmnigentError(
            f"provider {name!r}: 'default' names {sorted(invalid)}, which it does "
            f"not serve (serves {sorted(allowed)}).",
            code=ErrorCode.INVALID_INPUT,
        )
    return frozenset(requested)


def _default_raw_value(default_families: frozenset[str], served: set[str]) -> object:
    """Render *default_families* back to the most compact config form.

    Inverse of :func:`_parse_default_families`: the whole served set →
    ``True`` (stays correct if the provider later serves more families is
    NOT a concern — we re-render on each write); a single family → its
    name; several → a sorted list; none → ``None`` (caller omits the key).

    :param default_families: The scopes the provider is the default for.
    :param served: The scopes the provider serves (may include ``"pi"``).
    :returns: ``True``, a family-name ``str``, a sorted ``list[str]``, or
        ``None`` when not a default.
    """
    if not default_families:
        return None
    # ``True`` round-trips as "all model families served" and never claims
    # the pi scope (see _parse_default_families), so a default set that
    # includes pi must stay explicit — rendering it as ``True`` would drop
    # the pi scope on the next parse.
    if PI_SURFACE not in default_families and default_families == frozenset(served) - {PI_SURFACE}:
        return True
    if len(default_families) == 1:
        return next(iter(default_families))
    return sorted(default_families)


def _parse_provider(name: str, raw: dict[str, object]) -> ProviderEntry:
    """Parse one entry under ``providers:`` into a :class:`ProviderEntry`.

    Dispatches on the entry's ``kind:``. ``key`` / ``gateway`` / ``local``
    require at least one family; ``subscription`` requires a ``cli``;
    ``databricks`` requires a ``profile``.

    :param name: The provider name keyed under ``providers:``, e.g.
        ``"anthropic"``.
    :param raw: The raw provider mapping, e.g.
        ``{"kind": "subscription", "cli": "claude"}``.
    :returns: A populated :class:`ProviderEntry`.
    :raises OmnigentError: If ``kind`` is missing/invalid or the
        kind-specific required fields are absent.
    """
    kind_raw = raw.get("kind")
    if not isinstance(kind_raw, str) or kind_raw not in _VALID_KINDS:
        raise OmnigentError(
            f"provider {name!r}: 'kind' is required and must be one of "
            f"{_VALID_KINDS}, got {kind_raw!r}.",
            code=ErrorCode.INVALID_INPUT,
        )
    kind: ProviderKind = kind_raw  # type: ignore[assignment]  # validated against _VALID_KINDS above

    # The (possibly family-scoped) default flag — resolved per-kind below
    # once the served families are known. The repo's YAML loader strips
    # implicit scalar resolvers (the Norway-problem guard in
    # omnigent/inner/loader.py), so a YAML ``default: true`` arrives as
    # the string ``"true"``; the programmatic config-writing path uses a
    # real bool / family-name / list — :func:`_parse_default_families`
    # accepts all forms. The per-family "at most one default per family"
    # invariant is enforced in :func:`get_default_provider`.
    default_raw = raw.get("default")

    if kind == SUBSCRIPTION_KIND:
        cli_raw = raw.get("cli")
        if not isinstance(cli_raw, str) or not cli_raw:
            raise OmnigentError(
                f"provider {name!r}: a 'cli' (e.g. 'claude' or 'codex') is "
                "required when kind is 'subscription'.",
                code=ErrorCode.INVALID_INPUT,
            )
        # A subscription serves the family its CLI implies (claude→anthropic,
        # codex→openai); an unknown CLI serves nothing.
        served = (
            {ANTHROPIC_FAMILY}
            if cli_raw == "claude"
            else ({OPENAI_FAMILY} if cli_raw == "codex" else set())
        )
        return ProviderEntry(
            name=name,
            kind=kind,
            cli=cli_raw,
            # A subscription is locked to its own CLI, so it can never drive
            # pi — naming "pi" in its default scope is a config error.
            default_families=_parse_default_families(name, default_raw, served, pi_capable=False),
        )

    if kind == CLI_CONFIG_KIND:
        cli_raw = raw.get("cli")
        # Only codex has a model_provider concept; a claude analog (e.g. an
        # isaac-written settings.json) would be a different mechanism and a
        # deliberate extension, not a value to silently accept here.
        if cli_raw != "codex":
            raise OmnigentError(
                f"provider {name!r}: kind 'cli-config' requires cli: 'codex' "
                f"(the only CLI with config-file model providers), got {cli_raw!r}.",
                code=ErrorCode.INVALID_INPUT,
            )
        model_provider_raw = raw.get("model_provider")
        if not isinstance(model_provider_raw, str) or not model_provider_raw:
            raise OmnigentError(
                f"provider {name!r}: a 'model_provider' (the [model_providers.X] "
                "id in ~/.codex/config.toml, e.g. 'Databricks') is required when "
                "kind is 'cli-config'.",
                code=ErrorCode.INVALID_INPUT,
            )
        display_name_raw = raw.get("display_name")
        return ProviderEntry(
            name=name,
            kind=kind,
            cli=cli_raw,
            model_provider=model_provider_raw,
            display_name=display_name_raw if isinstance(display_name_raw, str) else None,
            # A codex cli-config provider serves the openai surface, like a
            # codex subscription.
            default_families=_parse_default_families(name, default_raw, {OPENAI_FAMILY}),
        )

    if kind == DATABRICKS_KIND:
        profile_raw = raw.get("profile")
        if not isinstance(profile_raw, str) or not profile_raw:
            raise OmnigentError(
                f"provider {name!r}: a 'profile' is required when kind is 'databricks'.",
                code=ErrorCode.INVALID_INPUT,
            )
        # Databricks (ucode) routes the anthropic/openai surfaces + pi, but NOT
        # gemini: the antigravity harness drives Gemini via the dedicated google
        # SDK + GEMINI_API_KEY, not an OpenAI-compatible gateway, so a databricks
        # profile cannot serve (or default) the Gemini surface.
        return ProviderEntry(
            name=name,
            kind=kind,
            profile=profile_raw,
            default_families=_parse_default_families(
                name, default_raw, set(_VALID_FAMILIES) - {GEMINI_FAMILY}, pi_capable=True
            ),
        )

    # Inline-family kinds: key / gateway / local.
    families: dict[str, FamilyConfig] = {}
    for family_name in _VALID_FAMILIES:
        family_raw = raw.get(family_name)
        if isinstance(family_raw, dict):
            families[family_name] = _parse_family(name, family_name, family_raw)
    if not families:
        raise OmnigentError(
            f"provider {name!r} (kind {kind!r}) configures no "
            "'anthropic', 'openai', or 'gemini' family.",
            code=ErrorCode.INVALID_INPUT,
        )
    # The Gemini surface is key-ONLY (the antigravity flavors need either a raw
    # GEMINI_API_KEY or OAuth — never a proxy). A ``gateway`` / ``local`` may
    # *carry* a gemini block alongside a real family (we ignore it for the Gemini
    # surface — see :func:`provider_families`), but one whose ONLY family is
    # gemini configures nothing it can actually serve: reject it loudly here.
    if kind != KEY_KIND and set(families) == {GEMINI_FAMILY}:
        raise OmnigentError(
            f"provider {name!r} (kind {kind!r}) declares only a 'gemini' family, "
            "but the Gemini surface is served only by a 'key' provider with a real "
            "GEMINI_API_KEY (a gateway/local proxy cannot drive the antigravity "
            "harness). Use kind: 'key', or add an 'anthropic'/'openai' family.",
            code=ErrorCode.INVALID_INPUT,
        )
    # Scope the parseable default to the families this kind can actually serve:
    # a gateway/local's gemini block never grants the Gemini surface, so a
    # hand-edited ``default: gemini`` on a gateway must fail at parse (parity
    # with how a databricks profile cannot name the gemini scope).
    served_for_default = set(families)
    if kind != KEY_KIND:
        served_for_default -= {GEMINI_FAMILY}
    return ProviderEntry(
        name=name,
        kind=kind,
        families=families,
        default_families=_parse_default_families(
            name, default_raw, served_for_default, pi_capable=True
        ),
    )


def load_config() -> dict[str, object]:
    """Load the global ``~/.omnigent/config.yaml`` mapping.

    Public entry point for callers (e.g. the runtime spawn-env builders)
    that need to pass the parsed config into :func:`load_providers` /
    :func:`default_provider_for_harness`. Respects
    ``$OMNIGENT_CONFIG_HOME`` for test isolation, exactly like the rest
    of the onboarding layer, so a single set of providers drives both the
    readout and the routing path.

    :returns: The parsed config mapping, e.g.
        ``{"providers": {"openrouter": {"kind": "gateway", ...}}}``, or
        ``{}`` when the config file is missing, empty, or unreadable.
    """
    return _load_config()


def load_providers(config: dict[str, object]) -> dict[str, ProviderEntry]:
    """Parse the ``providers:`` block of *config* into named entries.

    :param config: The parsed ``~/.omnigent/config.yaml`` mapping, e.g.
        ``{"providers": {"anthropic": {"kind": "key", ...}}, "auth": {...}}``.
    :returns: Providers keyed by name, e.g.
        ``{"anthropic": ProviderEntry(...)}``. Empty when no ``providers:``
        block is present.
    :raises OmnigentError: If any provider entry is malformed.
    """
    providers_raw = config.get("providers")
    if not isinstance(providers_raw, dict):
        return {}
    result: dict[str, ProviderEntry] = {}
    for name, raw in providers_raw.items():
        if not isinstance(raw, dict):
            raise OmnigentError(
                f"provider {str(name)!r} must be a mapping.",
                code=ErrorCode.INVALID_INPUT,
            )
        result[str(name)] = _parse_provider(str(name), raw)
    return result


def harness_family(harness: str) -> str | None:
    """Return the single model family a harness consumes, or ``None``.

    Public accessor for the canonical harness→family map. ``None`` means
    the harness is unmapped or spans both families (e.g. ``pi``), so callers
    filtering by surface should treat ``None`` as "both / no single family".

    :param harness: The canonical harness name, e.g. ``"codex"`` or ``"pi"``.
    :returns: ``"anthropic"`` / ``"openai"`` for a single-family harness, or
        ``None`` for an unmapped / both-family harness.
    """
    return _HARNESS_FAMILY.get(harness)


def provider_families(entry: ProviderEntry) -> frozenset[str]:
    """Return the model families *entry* can serve.

    Defaults are scoped **per family** (the Claude/``anthropic`` surface
    vs the Codex/``openai`` surface), mirroring how multi-model tools key
    defaults by purpose (Zed's per-feature model pointers, Continue's
    per-role models, Goose's lead/worker). This reports which families a
    provider is a default *candidate* for:

    - ``key`` / ``gateway`` / ``local``: the families it declares inline,
      plus the :data:`PI_SURFACE` scope (pi consumes either family).
    - ``subscription`` / ``cli-config``: derived from the CLI — ``claude``
      serves the ``anthropic`` surface, ``codex`` serves the ``openai``
      surface. Never pi: a CLI login (or a provider pinned in the CLI's
      own config file) is unusable outside its own CLI.
    - ``databricks``: both families plus pi — ucode routes the Claude,
      Codex, and pi surfaces.

    :param entry: The provider entry to classify.
    :returns: The scope names this provider can be the default for, e.g.
        ``frozenset({"anthropic"})`` for a Claude subscription, or
        ``frozenset({"anthropic", "openai", "pi"})`` for a Databricks
        profile.
    """
    if entry.kind == BEDROCK_KIND:
        # Bedrock mode is native-``omnigent claude`` only — the in-process /
        # gateway harnesses (incl. pi) reject it (see
        # configure_agent_harness_with_provider). Surface only its real
        # family (anthropic), never the pi scope.
        return frozenset(entry.families)
    if entry.kind in (KEY_KIND, GATEWAY_KIND, LOCAL_KIND):
        served = frozenset(entry.families)
        # The Gemini surface is key-ONLY: it is consumed by the antigravity
        # flavors (the SDK harness via a raw GEMINI_API_KEY, antigravity-native
        # via OAuth), neither driveable by a gateway / local proxy. So a
        # ``gateway`` / ``local`` declaring a ``gemini:`` block must NOT claim
        # the Gemini surface — only a ``key`` may. Stripping it here (rather than
        # at parse) keeps a multi-family gateway usable for its anthropic /
        # openai surfaces; a gateway whose ONLY family is gemini is rejected at
        # parse (see :func:`_parse_provider`).
        if entry.kind != KEY_KIND:
            served = served - {GEMINI_FAMILY}
        # pi consumes the anthropic / openai families only — a gemini-only key
        # serves just the Gemini surface, never pi (see ``_PI_FALLBACK_FAMILIES``).
        # Granting pi here would let the add flow auto-default pi to a gemini key
        # and let set_default_provider accept a pi scope for it, both of which
        # break pi launch. A multi-family key keeps pi via its anthropic/openai
        # family.
        if served & frozenset(_PI_FALLBACK_FAMILIES):
            return served | {PI_SURFACE}
        return served
    if entry.kind in (SUBSCRIPTION_KIND, CLI_CONFIG_KIND):
        if entry.cli == "claude":
            return frozenset({ANTHROPIC_FAMILY})
        if entry.cli == "codex":
            return frozenset({OPENAI_FAMILY})
        return frozenset()
    if entry.kind == DATABRICKS_KIND:
        # ucode routes anthropic/openai + pi, never the Gemini surface (which
        # needs the antigravity SDK + GEMINI_API_KEY, not a gateway).
        return (frozenset(_VALID_FAMILIES) - {GEMINI_FAMILY}) | {PI_SURFACE}
    return frozenset()


def get_default_provider(config: dict[str, object], family: str) -> ProviderEntry | None:
    """Return the ``default: true`` provider serving *family*, if any.

    Defaults are per-family: a Claude (``anthropic``) default and a Codex
    (``openai``) default coexist independently. At most one default may
    serve a given family.

    :param config: The parsed config mapping (``providers:`` block).
    :param family: The scope to resolve a default for, ``"anthropic"``,
        ``"openai"``, or the explicit :data:`PI_SURFACE` scope (``"pi"``).
    :returns: The default :class:`ProviderEntry` serving *family*, or
        ``None`` when none is marked default for it.
    :raises OmnigentError: If any provider is malformed, or more than one
        ``default: true`` provider serves *family*.
    """
    candidates = [
        entry for entry in load_providers(config).values() if family in entry.default_families
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        names = ", ".join(sorted(e.name for e in candidates))
        raise OmnigentError(
            f"multiple providers set 'default: true' for the {family!r} family "
            f"({names}); at most one may be the default per family.",
            code=ErrorCode.INVALID_INPUT,
        )
    return candidates[0]


def default_provider_for_harness(config: dict[str, object], harness: str) -> ProviderEntry | None:
    """Return the default provider for *harness* (resolving its family).

    Maps the harness to its family (claude-sdk/native-claude→anthropic;
    codex/native-codex/openai-agents→openai) and returns that family's
    default. The ``pi`` harness (and any unmapped harness) consumes both
    families: an explicit :data:`PI_SURFACE` default wins; otherwise it
    falls back to the ``anthropic`` then ``openai`` family default,
    skipping ``subscription``, ``cli-config``, and ``bedrock`` defaults —
    the first two live in the claude/codex CLI's own files an unmapped
    harness can't read, and ``bedrock`` is native-``omnigent claude`` only.
    Routing pi to any of them fails: ``configure_agent_harness_with_provider``
    no-ops on subscription (spawning pi authless) and raises on cli-config
    (non-codex) and bedrock. This mirrors :func:`provider_families`, which
    never reports the :data:`PI_SURFACE` scope for these kinds.

    :param config: The parsed config mapping (``providers:`` block).
    :param harness: The canonical harness name, e.g. ``"claude-sdk"`` or
        ``"pi"``.
    :returns: The default :class:`ProviderEntry` for that harness's family,
        or ``None`` when none is configured.
    :raises OmnigentError: If a provider is malformed, or more than one
        default serves the resolved family.
    """
    family = _HARNESS_FAMILY.get(harness)
    if family is not None:
        return get_default_provider(config, family)
    # Unmapped (e.g. pi): an explicit pi-scope default is authoritative.
    explicit = get_default_provider(config, PI_SURFACE)
    if explicit is not None:
        return explicit
    # Fall back across the pi-capable surfaces — prefer anthropic's default,
    # and skip the CLI-bound kinds (unusable outside their own CLI). Gemini is
    # excluded (a gemini key serves only the Gemini surface, never pi).
    for fam in _PI_FALLBACK_FAMILIES:
        provider = get_default_provider(config, fam)
        # Subscription logins and cli-config provider pins live in the
        # claude/codex CLI's own files, which an unmapped harness doesn't
        # wrap; a bedrock provider is native-``omnigent claude`` only
        # (configure_agent_harness_with_provider raises for it). None can
        # serve pi, so skip them and fall through — otherwise a bedrock Claude
        # default would turn a working pi run (own login) into a hard error.
        if provider is not None and provider.kind not in (
            SUBSCRIPTION_KIND,
            CLI_CONFIG_KIND,
            BEDROCK_KIND,
        ):
            return provider
    return None


def surface_default_provider(config: dict[str, object], surface: str) -> ProviderEntry | None:
    """Return the *effective* default provider for a harness surface.

    The display-side companion to :func:`default_provider_for_harness`,
    keyed by surface name rather than harness id: the ``anthropic`` /
    ``openai`` surfaces resolve their explicit per-family default, and the
    :data:`PI_SURFACE` surface resolves the pi harness's effective default
    (explicit pi scope, else the cross-family fallback). Used by the
    ``setup`` harness menus and the REPL startup header so every surface
    shows the provider its harness would actually route through.

    :param config: The parsed config mapping (``providers:`` block).
    :param surface: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: The effective default :class:`ProviderEntry` for *surface*,
        or ``None`` when none resolves.
    :raises OmnigentError: If a provider is malformed, or more than one
        default serves a scope.
    """
    if surface == PI_SURFACE:
        return default_provider_for_harness(config, PI_SURFACE)
    return get_default_provider(config, surface)


def surface_default_model(entry: ProviderEntry, surface: str) -> str | None:
    """Return the default model *entry* yields for a harness surface.

    For a model family this is that family's ``models.default``. For the
    :data:`PI_SURFACE` surface — pi consumes whichever family supplies its
    credential, anthropic preferred (see :func:`_family_for_harness`) —
    it is the first configured family's default in that same order.

    :param entry: The provider entry whose model is displayed.
    :param surface: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: The default model id, e.g. ``"claude-sonnet-4-6"``, or
        ``None`` when the relevant family declares no default (or, for
        ``subscription`` / ``databricks`` kinds, always — the CLI /
        profile picks the model).
    """
    if surface != PI_SURFACE:
        return entry.family_default_model(surface)
    for family_name in _PI_FALLBACK_FAMILIES:
        if family_name in entry.families:
            return entry.family_default_model(family_name)
    return None


def _family_for_harness(provider: ProviderEntry, harness: str) -> str | None:
    """Pick the family name *provider* exposes for *harness*.

    Uses the canonical harness→family map; for the ``pi`` harness (which
    consumes both families) or an unmapped harness, falls back to whichever
    single family the provider configures (anthropic preferred).

    :param provider: The active provider entry.
    :param harness: The canonical harness name, e.g. ``"claude-sdk"`` or
        ``"pi"``.
    :returns: ``"anthropic"`` / ``"openai"`` when the provider configures a
        matching family, else ``None``.
    """
    preferred = _HARNESS_FAMILY.get(harness)
    if preferred is not None and preferred in provider.families:
        return preferred
    if preferred is not None:
        # Harness wants a specific family the provider does not configure.
        return None
    # Unmapped harness (e.g. "pi"): use anthropic if present, else openai.
    # Gemini is excluded (it never drives pi).
    for family_name in _PI_FALLBACK_FAMILIES:
        if family_name in provider.families:
            return family_name
    return None


def _source_descriptor(family: FamilyConfig) -> str:
    """Return a non-secret descriptor of a family's credential source.

    :param family: The (raw, unexpanded) family config.
    :returns: A display string, e.g. ``"$ANTHROPIC_API_KEY"``,
        ``"env:OPENROUTER_API_KEY"``, ``"keychain:anthropic"``, or an
        auth-command descriptor like ``"auth_command: my-cli token"``.
    :raises OmnigentError: If the family has no credential source (should
        not happen post-parse).
    """
    if family.api_key is not None:
        # Show the $VAR reference verbatim, not the resolved secret.
        return family.api_key
    if family.api_key_ref is not None:
        return family.api_key_ref
    if family.auth_command is not None:
        return f"auth_command: {family.auth_command}"
    raise OmnigentError(
        "provider family has no credential source.",
        code=ErrorCode.INVALID_INPUT,
    )


def describe_active_credential(
    config: dict[str, object],
    harness: str,
    model_override: str | None = None,
) -> ResolvedCredential | None:
    """Describe the active credential for *harness*, for the readout.

    Resolves the default provider for *harness*'s family (per-family
    defaults — see :func:`default_provider_for_harness`), resolves the
    model (``model_override`` beats the family default), and fills a
    :class:`ResolvedCredential` whose :attr:`ResolvedCredential.source` is
    a non-secret descriptor. Never expands or returns the secret value
    itself, so it is safe to call for a family whose secret is unset.

    :param config: The parsed config mapping (``providers:`` block).
    :param harness: The canonical harness name the credential is for, e.g.
        ``"claude-sdk"`` or ``"codex"``.
    :param model_override: An in-session ``/model`` override that beats the
        family default, e.g. ``"openai/gpt-4o"``. ``None`` to use the
        provider's configured default.
    :returns: A :class:`ResolvedCredential`, or ``None`` when no default
        provider serves *harness*'s family.
    :raises OmnigentError: If the resolved provider is malformed, or more
        than one default serves the family.
    """
    provider = default_provider_for_harness(config, harness)
    if provider is None:
        return None

    if provider.kind == SUBSCRIPTION_KIND:
        # The CLI login carries auth; the CLI picks the model unless
        # overridden in-session. No family / base_url.
        return ResolvedCredential(
            provider_name=provider.name,
            kind=provider.kind,
            family=None,
            model=model_override,
            source=f"{provider.cli} CLI login",
            base_url=None,
        )

    if provider.kind == DATABRICKS_KIND:
        # Auth + model resolve from the Databricks profile / ucode state at
        # the harness boundary; the readout reports the profile pointer.
        return ResolvedCredential(
            provider_name=provider.name,
            kind=provider.kind,
            family=None,
            model=model_override,
            source=f"profile: {provider.profile}",
            base_url=None,
        )

    if provider.kind == CLI_CONFIG_KIND:
        # The provider definition + credential live in the CLI's own config
        # file; the launch pins it by name. The CLI picks the model unless
        # overridden in-session. No family / base_url known here.
        return ResolvedCredential(
            provider_name=provider.name,
            kind=provider.kind,
            family=None,
            model=model_override,
            source=f"~/.codex/config.toml provider: {provider.model_provider}",
            base_url=None,
        )

    # Inline-family kinds: key / gateway / local.
    family_name = _family_for_harness(provider, harness)
    if family_name is None:
        return None
    raw_family = provider.families[family_name]
    model = model_override or raw_family.default_model
    return ResolvedCredential(
        provider_name=provider.name,
        kind=provider.kind,
        family=family_name,
        model=model,
        source=_source_descriptor(raw_family),
        # base_url may contain a raw $VAR; expand it for display only.
        base_url=_expand(f"providers.{provider.name}.{family_name}.base_url", raw_family.base_url),
    )


def set_default_provider(
    providers: dict[str, object], name: str, family: str | None = None
) -> dict[str, object]:
    """Return a copy of ``providers:`` with *name* the default for *family*.

    Marks *name* the default for the chosen family/families and removes
    only that family/those families from every other provider's default —
    so a Claude (``anthropic``) default and a Codex (``openai``) default
    coexist untouched (Zed's "don't disturb the other slot"). When a shared
    provider (gateway / Databricks) loses one family it keeps the other:
    its ``default: true`` is rewritten to the remaining family. Defaults are
    marked **inline**, so this is a read-modify-write of the whole
    ``providers:`` block — the caller writes the return value back wholesale.

    :param providers: The current raw ``providers:`` mapping (config shape),
        e.g. ``{"anthropic": {"kind": "key", "default": true, ...},
        "openrouter": {"kind": "gateway", ...}}``.
    :param name: The provider to make the default, e.g. ``"openrouter"``.
    :param family: The single scope to make the default for — ``"anthropic"``,
        ``"openai"``, or ``"pi"`` (the per-harness path). ``None`` makes
        *name* the default for **all** scopes it serves (the legacy
        whole-provider behavior), clearing those scopes from siblings.
    :returns: A new ``providers:`` mapping with *name* default for the chosen
        family/families and that scope cleared on the others.
    :raises OmnigentError: If *name* is absent, an entry is malformed, or
        *family* is given but *name* does not serve it.
    """
    if name not in providers:
        raise OmnigentError(
            f"cannot set default: provider {name!r} is not declared under 'providers:'.",
            code=ErrorCode.INVALID_INPUT,
        )
    target_raw = providers[name]
    if not isinstance(target_raw, dict):
        raise OmnigentError(f"provider {name!r} must be a mapping.", code=ErrorCode.INVALID_INPUT)
    target_served = provider_families(_parse_provider(name, target_raw))
    # The families to (re)assign to *name*: a single scoped family, or all
    # it serves when unscoped.
    if family is None:
        scope = set(target_served)
    else:
        if family not in target_served:
            raise OmnigentError(
                f"cannot set default: provider {name!r} does not serve the "
                f"{family!r} family (serves {sorted(target_served)}).",
                code=ErrorCode.INVALID_INPUT,
            )
        scope = {family}

    result: dict[str, object] = {}
    for provider_name, entry in providers.items():
        if not isinstance(entry, dict):
            result[provider_name] = entry
            continue
        parsed = _parse_provider(provider_name, entry)
        served = provider_families(parsed)
        if provider_name == name:
            new_defaults = parsed.default_families | scope
        else:
            # Other providers lose exactly the scoped family/families.
            new_defaults = parsed.default_families - scope
        body = {k: v for k, v in entry.items() if k != "default"}
        raw_value = _default_raw_value(new_defaults, set(served))
        if raw_value is not None:
            body["default"] = raw_value
        result[provider_name] = body
    return result


def provider_entry_settings(
    name: str, entry: dict[str, object], *, make_default: bool
) -> dict[str, object]:
    """Build a ``{"providers": {name: entry}}`` dict to merge into config.

    Packages a single provider entry ready to deep-merge into
    ``~/.omnigent/config.yaml`` under ``providers:``. When *make_default*
    is set, the entry carries ``default: true`` — but the caller must still
    clear the flag on any other provider (use :func:`set_default_provider`
    over the merged result), since a deep-merge does not touch siblings.

    :param name: Provider name to key under ``providers:``, e.g.
        ``"openrouter"``.
    :param entry: The provider entry body (already in config shape, without
        the ``default`` key), e.g. ``{"kind": "gateway", "openai":
        {"base_url": "...", "api_key_ref": "keychain:openrouter"}}``.
    :param make_default: Whether to mark this entry ``default: true``.
    :returns: A settings dict with a single ``providers`` key, e.g.
        ``{"providers": {"openrouter": {"kind": "gateway", "default": true,
        ...}}}``.
    """
    body = {k: v for k, v in entry.items() if k != "default"}
    if make_default:
        body["default"] = True
    return {"providers": {name: body}}
