"""Rendering + flow helpers for ``omnigent setup --no-internal-beta``.

The user-facing model-selection surface (chunk 2b of
``designs/oss-cuj/04-model-selection-implementation.md``) has two pieces:

- the ``omnigent setup --no-internal-beta`` CLI command (interactive
  add/set-default/remove + a scriptable ``list`` subcommand), and
- the ``/model`` REPL readout/switch (in :mod:`omnigent.repl._repl`).

Both render the **same** grouped, kind-annotated view of the configured
providers and the same per-family default markers, so the heavy lifting
lives here as plain functions that take an already-parsed config and
return display lines. The click command wiring stays in
:mod:`omnigent.cli`; this module owns the look/feel and the
config-shape construction so the two surfaces never drift apart.

Persistence nuance (kept here so callers can't get it wrong): an **add**
deep-merges a single provider entry under ``providers:`` (see
:func:`build_key_provider_entry` etc.), while **set-default** and
**remove** must rewrite the *whole* ``providers:`` block — set-default
clears sibling ``default`` flags per family (a deep-merge cannot), and
remove drops a key. The click handlers call
:func:`~omnigent.onboarding.provider_config.set_default_provider` /
delete a key and write the result wholesale.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.databricks_config import databricks_sdk_installed
from omnigent.onboarding.interactive import ACCENT, console
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    CHAT_WIRE_API,
    CLI_CONFIG_KIND,
    DATABRICKS_KIND,
    GATEWAY_KIND,
    KEY_KIND,
    LOCAL_KIND,
    OPENAI_FAMILY,
    PI_SURFACE,
    SUBSCRIPTION_KIND,
    ProviderEntry,
    surface_default_provider,
)

# A short glyph per kind for the grouped listing. ASCII-safe fallbacks are
# not needed — the rest of the REPL/CLI already emits emoji freely — but
# the glyph is purely decorative: the kind word follows it.
# The ADMISSION TICKETS glyph (subscription) carries a VARIATION SELECTOR-16 so
# terminals render it as a 2-cell emoji (matching 🔑 / 🌐 / 🧱 and the 🎟️ in
# README.oss.md) instead of a cramped 1-cell text glyph — that VS16, not extra
# padding, is what aligns the subscription label with the wider glyphs.
# (rich >= 14's cell_len counts such a VS16-forced wide emoji as 2 cells —
# see omnigent.inner.banner._display_width.)
_KIND_GLYPH: dict[str, str] = {
    KEY_KIND: "\N{KEY}",
    SUBSCRIPTION_KIND: "\N{ADMISSION TICKETS}\N{VARIATION SELECTOR-16}",
    GATEWAY_KIND: "\N{GLOBE WITH MERIDIANS}",
    LOCAL_KIND: "\N{DESKTOP COMPUTER}\N{VARIATION SELECTOR-16}",
    DATABRICKS_KIND: "\N{BRICK}",
    # GEAR carries a VS16 for the same 2-cell-emoji rendering reason as the
    # ADMISSION TICKETS glyph above.
    CLI_CONFIG_KIND: "\N{GEAR}\N{VARIATION SELECTOR-16}",
    # CLOUD for Bedrock-style gateways (AWS Bedrock, corporate AI gateways)
    BEDROCK_KIND: "\N{CLOUD}",
}


# Rich style (colour) per kind word in the grouped listing, so the kind
# is scannable at a glance alongside its glyph. Purely cosmetic — the
# kind word itself is the source of truth.
_KIND_STYLE: dict[str, str] = {
    KEY_KIND: "yellow",
    SUBSCRIPTION_KIND: "magenta",
    GATEWAY_KIND: "cyan",
    LOCAL_KIND: "green",
    DATABRICKS_KIND: "red",
    CLI_CONFIG_KIND: "blue",
}

# Human label per harness surface, for the "(default · Claude)" /
# "(default · Codex)" / "(default · Pi)" markers. Anthropic powers the
# Claude surface; openai powers the Codex surface (and the OpenAI-Agents
# SDK harness); the pi surface is the pi harness itself (it consumes both
# model families).
_FAMILY_LABEL: dict[str, str] = {
    ANTHROPIC_FAMILY: "Claude",
    OPENAI_FAMILY: "Codex",
    PI_SURFACE: "Pi",
}

# The concrete harness ids each surface powers, shown as a dim annotation
# beside the harness header in ``configure harness`` so the brand label
# ("Claude") is honest about which harnesses it covers.
_FAMILY_HARNESS_IDS: dict[str, str] = {
    ANTHROPIC_FAMILY: "claude-sdk, native-claude",
    OPENAI_FAMILY: "codex, native-codex, openai-agents",
    PI_SURFACE: "pi",
}


def family_label(family: str) -> str:
    """Return the harness brand label for a surface.

    Public accessor for :data:`_FAMILY_LABEL`, so the CLI tree renders the
    same brand names as the listing without importing the private map.

    :param family: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: ``"Claude"`` for anthropic, ``"Codex"`` for openai, ``"Pi"``
        for pi; the family name itself for any other value.
    """
    return _FAMILY_LABEL.get(family, family)


def family_harness_ids(family: str) -> str:
    """Return the dim harness-id annotation for a surface.

    :param family: ``"anthropic"``, ``"openai"``, or ``"pi"``.
    :returns: A comma-separated harness-id string, e.g.
        ``"claude-sdk, native-claude"``; empty for an unknown surface.
    """
    return _FAMILY_HARNESS_IDS.get(family, "")


# Default endpoint base URL per family for a ``key`` provider (a direct
# vendor key, not a gateway). A gateway/local provider supplies its own
# base_url; a ``key`` provider talks to the canonical vendor endpoint.
_FAMILY_DEFAULT_BASE_URL: dict[str, str] = {
    ANTHROPIC_FAMILY: "https://api.anthropic.com",
    OPENAI_FAMILY: "https://api.openai.com/v1",
}

# Maps a catalog provider name (from
# :func:`omnigent.onboarding.providers.get_all_providers`) to the
# omnigent family it serves for a ``key`` provider. Anthropic serves the
# Claude (anthropic) surface; OpenAI and OpenAI-compatible vendors serve
# the Codex (openai) surface. Providers absent here have no omnigent
# harness family yet and are not offered for a ``key`` add.
_CATALOG_PROVIDER_FAMILY: dict[str, str] = {
    "anthropic": ANTHROPIC_FAMILY,
    "openai": OPENAI_FAMILY,
    "openrouter": OPENAI_FAMILY,
    "groq": OPENAI_FAMILY,
    "deepseek": OPENAI_FAMILY,
    "xai": OPENAI_FAMILY,
    "mistral": OPENAI_FAMILY,
    "together_ai": OPENAI_FAMILY,
    "fireworks_ai": OPENAI_FAMILY,
}


@dataclass(frozen=True)
class _VendorEndpoint:
    """A direct-``key`` vendor's OpenAI-compatible endpoint + wire protocol.

    :param base_url: The vendor's own API base URL, e.g.
        ``"https://openrouter.ai/api/v1"`` — NOT ``api.openai.com``.
    :param wire_api: The OpenAI wire protocol the vendor speaks —
        ``"chat"`` for the OpenAI-compatible Chat Completions surface every
        third-party vendor below exposes (none implement the OpenAI
        Responses API).
    """

    base_url: str
    wire_api: str


# Per-vendor endpoint + wire for direct ``key`` providers that are NOT the
# canonical OpenAI / Anthropic endpoints. These OpenAI-compatible third
# parties are reached at their OWN base_url (the catalog has no base_url, and
# the openai-family default ``api.openai.com`` is wrong for them — the reason
# an OpenRouter key "didn't work"). All speak Chat Completions, not the
# Responses API. ``openai`` / ``anthropic`` are intentionally absent: they use
# :func:`default_base_url_for_family` (and openai keeps the Responses default).
_KEY_PROVIDER_ENDPOINT: dict[str, _VendorEndpoint] = {
    "openrouter": _VendorEndpoint("https://openrouter.ai/api/v1", CHAT_WIRE_API),
    "groq": _VendorEndpoint("https://api.groq.com/openai/v1", CHAT_WIRE_API),
    "deepseek": _VendorEndpoint("https://api.deepseek.com", CHAT_WIRE_API),
    "xai": _VendorEndpoint("https://api.x.ai/v1", CHAT_WIRE_API),
    "mistral": _VendorEndpoint("https://api.mistral.ai/v1", CHAT_WIRE_API),
    "together_ai": _VendorEndpoint("https://api.together.xyz/v1", CHAT_WIRE_API),
    "fireworks_ai": _VendorEndpoint("https://api.fireworks.ai/inference/v1", CHAT_WIRE_API),
}


def key_provider_endpoint(provider: str) -> _VendorEndpoint | None:
    """Return the vendor endpoint + wire for a ``key`` *provider*, if non-default.

    :param provider: A catalog provider id, e.g. ``"openrouter"``.
    :returns: The :class:`_VendorEndpoint` (own base_url + ``wire_api``) for a
        third-party OpenAI-compatible vendor, or ``None`` for ``openai`` /
        ``anthropic`` (which use the canonical family base_url and, for
        openai, the Responses wire default).
    """
    return _KEY_PROVIDER_ENDPOINT.get(provider)


def key_providers() -> list[str]:
    """Return catalog provider names eligible for a ``key`` add.

    A ``key`` provider is a direct vendor API key reached via an
    omnigent family. Only providers in :data:`_CATALOG_PROVIDER_FAMILY`
    (those that map to the ``anthropic`` or ``openai`` surface) qualify;
    the order follows the catalog's popular-first ordering.

    :returns: Catalog provider names, e.g. ``["openai", "anthropic",
        "openrouter", ...]``.
    """
    from omnigent.onboarding.providers import get_all_providers

    return [p for p in get_all_providers() if p in _CATALOG_PROVIDER_FAMILY]


def family_for_key_provider(provider: str) -> str:
    """Return the omnigent family a ``key`` *provider* serves.

    :param provider: Catalog provider name, e.g. ``"anthropic"`` or
        ``"openrouter"``.
    :returns: The family, ``"anthropic"`` or ``"openai"``.
    :raises KeyError: If *provider* is not a key-eligible catalog provider
        (caller should restrict choices to :func:`key_providers`).
    """
    return _CATALOG_PROVIDER_FAMILY[provider]


# Human display names for catalog provider ids, so menus and prompts show
# "OpenAI" / "OpenRouter" rather than the raw lowercase id. Unlisted ids
# fall back to a title-cased form (see :func:`provider_display_name`).
_PROVIDER_DISPLAY_NAME: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "openrouter": "OpenRouter",
    "groq": "Groq",
    "deepseek": "DeepSeek",
    "xai": "xAI",
    "mistral": "Mistral",
    "together_ai": "Together AI",
    "fireworks_ai": "Fireworks AI",
    "gemini": "Google Gemini",
    "google": "Google Gemini",
    "databricks": "Databricks",
    "ollama": "Ollama",
}


def provider_display_name(provider: str) -> str:
    """Return a human display name for a provider id.

    :param provider: A provider id, e.g. ``"openai"`` or ``"together_ai"``.
    :returns: A friendly name, e.g. ``"OpenAI"`` or ``"Together AI"``.
        Falls back to a title-cased form for ids not in the map (e.g. a
        user-named gateway ``"my-proxy"`` → ``"My-Proxy"``).
    """
    return _PROVIDER_DISPLAY_NAME.get(provider, provider.replace("_", " ").title())


def cli_display_name(cli: str) -> str:
    """Return a human display name for a subscription CLI login.

    :param cli: The CLI name, ``"claude"`` or ``"codex"``.
    :returns: A friendly name, e.g. ``"Claude (Pro/Max)"`` for ``claude``
        or ``"ChatGPT"`` for ``codex`` (the ChatGPT plan drives codex).
        Falls back to a title-cased form for any other value.
    """
    return {"claude": "Claude (Pro/Max)", "codex": "ChatGPT"}.get(cli, cli.title())


def kind_glyph(kind: str) -> str:
    """Return the emoji glyph for a provider *kind*.

    The single source of truth is :data:`_KIND_GLYPH`; this public
    accessor lets other surfaces (e.g. the ``/model`` readout in the
    REPL and the startup-header creds line) render the same glyph as the
    ``configure harness`` menu and listing without importing the private
    map. Every glyph is a 2-cell emoji on modern terminals (the
    subscription ticket via its VARIATION SELECTOR-16), so a single space
    separates it from the following label.

    :param kind: A provider kind, e.g. ``"key"``, ``"subscription"``,
        ``"gateway"``, ``"local"``, or ``"databricks"``.
    :returns: The kind's glyph (e.g. ``"🔑"`` for ``"key"``), or an
        empty string for an unknown kind.
    """
    return _KIND_GLYPH.get(kind, "")


def credential_label(
    kind: str,
    provider_name: str,
    *,
    profile: str | None = None,
    display_name: str | None = None,
) -> str:
    """A friendly, jargon-free label for a configured credential.

    The single source of truth for how a credential is named across every
    surface — the ``configure harness`` menus/listing and the ``/model``
    REPL readout — so a subscription always reads as ``"Subscription"``
    (never the raw ``"claude"`` / brand name), a vendor key names the
    vendor + ``"API Key"``, and Databricks names its profile. Pair with
    :func:`kind_glyph` for the glyph prefix.

    :param kind: The provider kind, e.g. ``"key"``, ``"subscription"``,
        ``"gateway"``, ``"local"``, ``"databricks"``, or ``"cli-config"``.
    :param provider_name: The provider id keyed under ``providers:``,
        e.g. ``"openai"`` or ``"my-proxy"``.
    :param profile: The Databricks profile name for a ``databricks``
        credential, e.g. ``"oss"``; ``None`` for other kinds (and for a
        databricks credential whose profile is unknown to the caller).
    :param display_name: The provider's own display name for a
        ``cli-config`` credential — the ``name`` field of its
        ``[model_providers.X]`` table, e.g. ``"Databricks AI Gateway"``;
        ``None`` for other kinds (and when the table named none, falling
        back to *provider_name*).
    :returns: A human label, e.g. ``"Subscription"``, ``"Anthropic API
        Key"``, ``"Databricks (oss)"``, ``"Databricks AI Gateway"``, or a
        gateway's display name.
    """
    if kind == SUBSCRIPTION_KIND:
        # Within a harness there is only one subscription, so the plan
        # name adds no information — just "Subscription".
        return "Subscription"
    if kind == DATABRICKS_KIND:
        return f"Databricks ({profile})" if profile else "Databricks"
    if kind == CLI_CONFIG_KIND:
        # The provider's own name field is the friendliest label there is
        # ("Databricks AI Gateway"); the entry name ("codex-databricks") is
        # the readable fallback.
        return display_name or provider_name
    if kind == KEY_KIND:
        return f"{provider_display_name(provider_name)} API Key"
    if kind == BEDROCK_KIND:
        return f"{provider_display_name(provider_name)} Bedrock"
    return provider_display_name(provider_name)


@dataclass(frozen=True)
class AddOption:
    """One intuitive entry in the ``configure harness`` add menu.

    Flattens the kind + provider/cli choice into a single credential-aware
    option so the user picks ``"OpenAI — API key"`` or
    ``"Claude — subscription"`` directly, rather than a bare ``key`` then
    ``openai`` two-step.

    :param label: The menu label, e.g. ``"OpenAI — API key"``.
    :param description: A one-line hint shown under the selected option,
        e.g. ``"Use an OpenAI API key (platform.openai.com)."``.
    :param kind: The provider kind this resolves to, e.g. ``"key"``.
    :param provider: For a fixed ``key`` option, the catalog provider id
        (e.g. ``"openai"``); ``None`` when the user picks it next.
    :param cli: For a ``subscription`` option, the CLI login
        (``"claude"`` / ``"codex"``); ``None`` otherwise.
    :param other: ``True`` for the catch-all ``key`` option that prompts
        for a provider from the remaining catalog (Groq, DeepSeek, …).
    """

    label: str
    description: str
    kind: str
    provider: str | None = None
    cli: str | None = None
    other: bool = False


# Catalog providers surfaced as their own top-level add option; the rest
# are reachable via the "Other provider" catch-all so the menu stays short.
_PRESET_KEY_PROVIDERS: tuple[str, ...] = ("openai", "anthropic", "openrouter")


def add_menu_options() -> list[AddOption]:
    """Return the flat, credential-aware add-menu options in display order.

    Common provider+credential combinations are surfaced directly
    (``"OpenAI — API key"``, ``"Claude — subscription"``); the long tail of
    API-key providers folds into a single ``"Other provider — API key"``
    entry, and any OpenAI/Anthropic-compatible proxy uses the gateway entry.

    :returns: The ordered :class:`AddOption` list backing the add menu.
        Each label is prefixed with its kind glyph (🔑 / 🎟️ / 🌐 / 🧱).

        Order is chosen so it reads well both in the full menu and in
        each family-scoped subset (:func:`add_menu_options_for_family`,
        which filters while preserving this order): the first-party API
        key(s) and subscription(s) lead, the cross-vendor extras
        (Gateway, OpenRouter) follow alphabetically, and Databricks sits
        just above the catch-all "Other provider" at the bottom. Scoped
        to one family this collapses to: API key → subscription →
        Gateway [→ OpenRouter] → Databricks [→ Other].
    """

    def _opt(text: str, description: str, kind: str, **kw: object) -> AddOption:
        """Build an :class:`AddOption` whose label is glyph-prefixed for *kind*."""
        return AddOption(
            label=f"{_KIND_GLYPH[kind]} {text}",
            description=description,
            kind=kind,
            **kw,  # type: ignore[arg-type]  # provider/cli/other forwarded to AddOption
        )

    return [
        # API keys, then subscriptions, for the two first-party vendors —
        # so each family-scoped menu leads with "<vendor> API key" then
        # "<vendor> subscription".
        _opt(
            "OpenAI — API key",
            "Use an OpenAI API key (platform.openai.com).",
            KEY_KIND,
            provider="openai",
        ),
        _opt(
            "Anthropic — API key",
            "Use an Anthropic API key (console.anthropic.com).",
            KEY_KIND,
            provider="anthropic",
        ),
        _opt(
            "ChatGPT — subscription",
            "Use your ChatGPT plan via the codex CLI login.",
            SUBSCRIPTION_KIND,
            cli="codex",
        ),
        _opt(
            "Claude — subscription (Pro/Max)",
            "Use your Claude Pro/Max plan via the claude CLI login.",
            SUBSCRIPTION_KIND,
            cli="claude",
        ),
        # Cross-vendor extras, alphabetical (Gateway before OpenRouter).
        _opt(
            "Gateway — custom base URL + key (e.g. OpenRouter)",
            "An OpenAI/Anthropic-compatible proxy: LiteLLM, Ollama, OpenRouter, vLLM, …",
            GATEWAY_KIND,
        ),
        _opt(
            "OpenRouter — API key",
            "One key, many models (openrouter.ai).",
            KEY_KIND,
            provider="openrouter",
        ),
        # Databricks sits just above the catch-all "Other provider". The
        # option stays visible without the `databricks` extra (so it remains
        # discoverable), but its description carries the install hint and
        # selecting it aborts with the same hint (_configure_harness_add).
        _opt(
            "Databricks — workspace",
            "Route harnesses through a Databricks workspace's Unity AI Gateway (via ucode)."
            if databricks_sdk_installed()
            # Markup-safe (rendered via Text.from_markup): no literal
            # brackets, so the extra is named in prose here and the exact
            # `omnigent[databricks]` command appears on selection.
            else "Requires the Databricks extra — select for the install command.",
            DATABRICKS_KIND,
        ),
        _opt(
            "Other provider — API key",
            "Groq, DeepSeek, xAI, Mistral, Together AI, Fireworks, …",
            KEY_KIND,
            other=True,
        ),
    ]


def _add_option_families(opt: AddOption) -> frozenset[str]:
    """Return the surfaces an add-menu *opt* can serve.

    Used to scope the add menu to the harness the user drilled into
    (``configure harness`` → Claude / Codex / Pi → "Add a provider"): a
    Claude add should not offer an OpenAI-only key, and vice versa.
    Gateways and Databricks serve every surface; any API key can drive pi
    (it consumes both model families); subscriptions never drive pi (a
    CLI login is unusable outside its own CLI).

    :param opt: One add-menu option.
    :returns: The surfaces this option can configure — a subset of
        ``{"anthropic", "openai", "pi"}``.
    """
    if opt.kind == GATEWAY_KIND or opt.kind == DATABRICKS_KIND:
        return frozenset({ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE})
    if opt.kind == SUBSCRIPTION_KIND:
        if opt.cli == "claude":
            return frozenset({ANTHROPIC_FAMILY})
        if opt.cli == "codex":
            return frozenset({OPENAI_FAMILY})
        return frozenset()
    if opt.kind == KEY_KIND:
        if opt.other:
            # The catch-all tail (Groq, DeepSeek, …) are all openai-family.
            return frozenset({OPENAI_FAMILY, PI_SURFACE})
        if opt.provider is not None:
            return frozenset({family_for_key_provider(opt.provider), PI_SURFACE})
    return frozenset()


def add_menu_options_for_family(family: str) -> list[AddOption]:
    """Return the add-menu options relevant to a single harness surface.

    Scopes :func:`add_menu_options` to *family* so the ``configure
    harness`` per-harness "Add a provider" flow only offers credentials
    that can drive that harness (plus gateways / Databricks, which serve
    every surface). Order is preserved from :func:`add_menu_options`.

    :param family: ``"anthropic"`` (Claude), ``"openai"`` (Codex), or
        ``"pi"`` (Pi).
    :returns: The subset of :func:`add_menu_options` serving *family*.
    """
    return [opt for opt in add_menu_options() if family in _add_option_families(opt)]


def other_key_providers() -> list[str]:
    """Return key-eligible catalog providers not surfaced as preset options.

    Backs the ``"Other provider — API key"`` secondary pick — the long tail
    of API-key providers (Groq, DeepSeek, xAI, …) not already a top-level
    :func:`add_menu_options` entry.

    :returns: Catalog provider ids excluding :data:`_PRESET_KEY_PROVIDERS`,
        in catalog (popular-first) order.
    """
    return [p for p in key_providers() if p not in _PRESET_KEY_PROVIDERS]


def render_provider_listing_by_harness(
    config: dict[str, object],
    providers: dict[str, ProviderEntry],
) -> None:
    """Render configured providers grouped under each harness family.

    Like :func:`render_provider_listing`, but organized by harness — an
    accent ``Credentials (by harness)`` header, then one bold surface header
    per harness (Claude, Codex, Pi) followed by the providers that can drive
    it, each with the per-surface default marked. A provider that serves
    several surfaces is listed under each. Used by ``omnigent config list``.

    :param config: The parsed config mapping (``providers:`` block), used to
        resolve per-surface defaults.
    :param providers: Parsed provider entries keyed by name (from
        :func:`~omnigent.onboarding.provider_config.load_providers`).
    :returns: None. Side effect: writes the listing to the shared console.
    """
    from omnigent.onboarding.provider_config import provider_families

    console.print(f"[{ACCENT}]Credentials (by harness)[/]")
    if not providers:
        console.print("  [dim]none configured yet[/dim]")
        return
    for family in (ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE):
        console.print(f"  [bold]{_FAMILY_LABEL[family]}[/]")
        serving = [
            (name, entry)
            for name, entry in providers.items()
            if family in provider_families(entry)
        ]
        if not serving:
            console.print("    [dim](none configured)[/dim]")
            continue
        for name, entry in serving:
            glyph = _KIND_GLYPH.get(entry.kind, "")
            kind_style = _KIND_STYLE.get(entry.kind, "white")
            summary = _entry_models_summary(entry)
            line = (
                f"    {glyph} [{kind_style}]{entry.kind}[/] [bold]{name}[/] [dim]{summary}[/dim]"
            )
            if family in _provider_default_families(entry, config):
                line += " [green]✓ default[/green]"
            console.print(line)


def _provider_default_families(entry: ProviderEntry, config: dict[str, object]) -> list[str]:
    """Return the surfaces *entry* is the effective default for.

    Cross-checks the per-surface default resolved from *config* against
    *entry* so the listing marks ``(default · Claude)`` / ``(default ·
    Codex)`` / ``(default · Pi)`` only on the provider that actually wins
    that surface. Pi resolves its *effective* default (explicit pi scope,
    else the cross-family fallback), so the marker names the provider the
    pi harness would really route through.

    :param entry: The provider entry being rendered.
    :param config: The parsed config mapping (``providers:`` block), used
        to resolve each surface's default.
    :returns: Surface names this entry is the default for, in
        ``[anthropic, openai, pi]`` order, e.g. ``["anthropic", "pi"]``.
    """
    result: list[str] = []
    for family in (ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE):
        default = surface_default_provider(config, family)
        if default is not None and default.name == entry.name:
            result.append(family)
    return result


def _entry_models_summary(entry: ProviderEntry) -> str:
    """Return a short model summary for a provider listing row.

    For inline-family kinds (``key`` / ``gateway`` / ``local``) this is
    the family default model(s); subscription/databricks describe their
    auth source instead (the model is picked by the CLI / profile).

    :param entry: The provider entry to summarize.
    :returns: A display string, e.g. ``"claude-sonnet-4-6"`` (a key with a
        default), ``"base_url set"`` (a gateway with no default model),
        ``"via claude CLI"`` (a subscription), or ``"profile: oss"`` (a
        databricks profile).
    """
    if entry.kind == SUBSCRIPTION_KIND:
        return f"via {entry.cli} CLI"
    if entry.kind == DATABRICKS_KIND:
        return f"profile: {entry.profile}"
    if entry.kind == CLI_CONFIG_KIND:
        return f"~/.{entry.cli}/config.toml: {entry.model_provider}"
    # Inline-family kinds: list each family's default model, else note the
    # base_url is set (a gateway without a pinned default model still works
    # — the spec/override picks the model).
    models: list[str] = []
    for family in (ANTHROPIC_FAMILY, OPENAI_FAMILY):
        default_model = entry.family_default_model(family)
        if default_model:
            models.append(default_model)
    if models:
        return ", ".join(models)
    return "base_url set"


def render_provider_listing(
    config: dict[str, object],
    providers: dict[str, ProviderEntry],
    detected: list[DetectedProvider],
) -> None:
    """Render the grouped, kind-annotated provider listing.

    Prints (via the shared :data:`~omnigent.onboarding.interactive.console`)
    a theme-picker-styled view: an accent ``Configured providers`` header,
    one styled line per provider (kind glyph + colour-styled kind word +
    bold name + dim model/auth summary + a green per-family ``✓ default ·
    Claude`` / ``· Codex`` marker), and a dim ``Detected (not
    configured):`` section for ambient credentials the user has but has
    not added. The provider order follows the ``providers:`` block's
    declared order.

    :param config: The parsed config mapping (``providers:`` block), used
        to resolve per-family defaults.
    :param providers: Parsed provider entries keyed by name (from
        :func:`~omnigent.onboarding.provider_config.load_providers`).
    :param detected: Ambient detections from
        :func:`~omnigent.onboarding.ambient.detect_providers`; entries
        whose name is already configured — or whose CLI is already wrapped
        by a configured ``subscription`` provider — are omitted from the
        hint section.
    :returns: None. Side effect: writes the listing to the shared console.
    """
    if providers:
        console.print(f"[{ACCENT}]Configured providers[/]")
        for name, entry in providers.items():
            glyph = _KIND_GLYPH.get(entry.kind, "")
            kind_style = _KIND_STYLE.get(entry.kind, "white")
            summary = _entry_models_summary(entry)
            default_families = _provider_default_families(entry, config)
            line = f"  {glyph} [{kind_style}]{entry.kind}[/] [bold]{name}[/] [dim]{summary}[/dim]"
            if default_families:
                labels = " · ".join(_FAMILY_LABEL[f] for f in default_families)
                line += f" [green]✓ default · {labels}[/green]"
            console.print(line)
    else:
        console.print("[dim]No providers configured yet.[/dim]")

    configured_names = set(providers)
    # A detected CLI login (``det.name`` is the CLI, e.g. ``"claude"``) is
    # already configured when a ``subscription`` provider wraps that CLI —
    # but that provider is named e.g. ``"claude-subscription"``, so the
    # plain name check above misses it and the login wrongly shows as "not
    # configured". Also exclude detections whose name matches a configured
    # subscription's CLI.
    configured_subscription_clis = {
        entry.cli for entry in providers.values() if entry.kind == SUBSCRIPTION_KIND and entry.cli
    }
    hints = [
        det
        for det in detected
        if det.name not in configured_names and det.name not in configured_subscription_clis
    ]
    if hints:
        console.print()
        console.print("[dim]Detected (not configured):[/dim]")
        for det in hints:
            console.print(f"  [cyan]{det.name}[/cyan] [dim]({det.source})[/dim]")


def build_key_provider_entry(
    family: str,
    base_url: str,
    api_key_ref: str,
    default_model: str | None,
    *,
    wire_api: str | None = None,
) -> dict[str, object]:
    """Build a ``kind: key`` provider entry body (config shape).

    :param family: The family this key serves, ``"anthropic"`` or
        ``"openai"``.
    :param base_url: The endpoint base URL, e.g.
        ``"https://api.anthropic.com"``.
    :param api_key_ref: The secret reference, e.g. ``"keychain:anthropic"``
        or ``"env:ANTHROPIC_API_KEY"``.
    :param default_model: The family default model id, e.g.
        ``"claude-sonnet-4-6"``, or ``None`` to omit the ``models`` block.
    :param wire_api: OpenAI wire protocol for an ``openai``-family key —
        ``"chat"`` for third-party vendors (OpenRouter, Groq, …) that only
        speak Chat Completions. Written onto the family block; ``None`` omits
        it (the executor defaults to the Responses API, correct for OpenAI).
    :returns: A provider entry body, e.g.
        ``{"kind": "key", "anthropic": {"base_url": "...",
        "api_key_ref": "keychain:anthropic",
        "models": {"default": "claude-sonnet-4-6"}}}``.
    """
    family_block: dict[str, object] = {"base_url": base_url, "api_key_ref": api_key_ref}
    if default_model:
        family_block["models"] = {"default": default_model}
    if wire_api is not None and family == OPENAI_FAMILY:
        family_block["wire_api"] = wire_api
    return {"kind": KEY_KIND, family: family_block}


def default_base_url_for_family(family: str) -> str:
    """Return the canonical vendor base URL for a ``key`` provider family.

    :param family: ``"anthropic"`` or ``"openai"``.
    :returns: The default endpoint base URL, e.g.
        ``"https://api.anthropic.com"``.
    :raises KeyError: If *family* is not a known family.
    """
    return _FAMILY_DEFAULT_BASE_URL[family]


def build_subscription_provider_entry(cli: str) -> dict[str, object]:
    """Build a ``kind: subscription`` provider entry body.

    :param cli: The CLI whose login carries auth, ``"claude"`` or
        ``"codex"``.
    :returns: A provider entry body, e.g.
        ``{"kind": "subscription", "cli": "claude"}``.
    """
    return {"kind": SUBSCRIPTION_KIND, "cli": cli}


def build_cli_config_provider_entry(
    cli: str,
    model_provider: str,
    display_name: str | None,
) -> dict[str, object]:
    """Build a ``kind: cli-config`` provider entry body (config shape).

    A cli-config provider pins a custom model provider defined in the
    harness CLI's own config file (today: a ``[model_providers.X]`` table
    in ``~/.codex/config.toml`` with self-contained auth, e.g. the
    Databricks AI Gateway written by ``isaac configure codex``). The
    provider definition and credential stay in that file; this entry only
    records which provider the launch selects.

    :param cli: The CLI whose config file defines the provider —
        ``"codex"`` (the only CLI with config-file model providers today).
    :param model_provider: The ``[model_providers.X]`` id to pin, e.g.
        ``"Databricks"``.
    :param display_name: The provider table's ``name`` field, snapshotted
        for display, e.g. ``"Databricks AI Gateway"``; ``None`` omits it
        (labels fall back to the entry name).
    :returns: A provider entry body, e.g. ``{"kind": "cli-config", "cli":
        "codex", "model_provider": "Databricks", "display_name":
        "Databricks AI Gateway"}``.
    """
    body: dict[str, object] = {
        "kind": CLI_CONFIG_KIND,
        "cli": cli,
        "model_provider": model_provider,
    }
    if display_name:
        body["display_name"] = display_name
    return body


def build_gateway_provider_entry(
    base_url: str,
    api_key_ref: str,
    *,
    families: list[str],
    wire_api: str | None = None,
    models: dict[str, str] | None = None,
) -> dict[str, object]:
    """Build a ``kind: gateway`` provider entry body (config shape).

    A gateway is an OpenAI/Anthropic-compatible proxy reached at a custom
    ``base_url`` (OpenRouter, LiteLLM, a local Ollama). It may serve the
    ``openai`` family, the ``anthropic`` family, or both — each family
    gets its own block pointing at the same base_url + key.

    :param base_url: The gateway base URL, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key_ref: The secret reference, e.g.
        ``"keychain:openrouter"`` or ``"env:OPENROUTER_API_KEY"``.
    :param families: The families the gateway serves, a non-empty subset
        of ``["openai", "anthropic"]``.
    :param wire_api: Wire protocol for the **openai** family —
        ``"responses"`` (OpenAI / LiteLLM) or ``"chat"`` (OpenRouter and
        most OSS-model gateways, which don't implement the Responses API).
        Written only onto the ``openai`` block; the ``anthropic`` block
        (Messages API) has no wire choice. ``None`` omits it (the codex
        executor then defaults to the Responses API). Getting this wrong is
        the usual reason an OpenRouter gateway fails while LiteLLM works.
    :param models: Optional per-family default model id, e.g.
        ``{"openai": "qwen/qwen3.7-plus"}``. Written as ``models.default``
        onto each named family's block — important for a gateway, which has
        no catalog default, so without it routing falls back to a vendor
        model the gateway may not host. ``None`` / a family absent from the
        map omits the pin for that family.
    :returns: A provider entry body, e.g.
        ``{"kind": "gateway", "openai": {"base_url": "...",
        "api_key_ref": "keychain:openrouter", "wire_api": "chat",
        "models": {"default": "qwen/qwen3.7-plus"}}}``.
    :raises ValueError: If *families* is empty.
    """
    if not families:
        raise ValueError("a gateway must serve at least one family")
    models = models or {}
    body: dict[str, object] = {"kind": GATEWAY_KIND}
    for family in families:
        block: dict[str, object] = {"base_url": base_url, "api_key_ref": api_key_ref}
        if family == OPENAI_FAMILY and wire_api is not None:
            block["wire_api"] = wire_api
        if models.get(family):
            block["models"] = {"default": models[family]}
        body[family] = block
    return body


def build_databricks_provider_entry(profile: str) -> dict[str, object]:
    """Build a ``kind: databricks`` provider entry body.

    :param profile: The Databricks profile name from
        ``~/.databrickscfg``, e.g. ``"oss"``.
    :returns: A provider entry body, e.g.
        ``{"kind": "databricks", "profile": "oss"}``.
    """
    return {"kind": DATABRICKS_KIND, "profile": profile}
