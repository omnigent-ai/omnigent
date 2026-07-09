"""Turn ambient-detected credentials into first-class provider entries.

:mod:`omnigent.onboarding.ambient` discovers credentials already on the
machine (env API keys, logged-in ``claude`` / ``codex`` CLIs, a local
Ollama). This module bridges those raw detections into the kind-typed
``providers:`` shape consumed by :mod:`omnigent.onboarding.provider_config`,
so they are treated as real providers everywhere — the ``/model`` readout
names them truthfully, routing uses them, and ``configure harness`` shows
them with no "detected vs configured" split.

Two surfaces:

- :func:`effective_config_with_detected` — a **read-time** merge used by
  the readout and routing: explicit config wins on name conflict, and a
  detected provider auto-becomes the default for a family that has no
  explicit default. Never writes to disk.
- :func:`providers_to_adopt` — the raw entries ``configure harness`` should
  **persist** into ``config.yaml`` (the actual write lives in the CLI,
  which owns the config file), so opening the manager adopts detections as
  ordinary, editable provider entries.
"""

from __future__ import annotations

from omnigent.env_credentials import getenv_nonempty_with_omnigent_prefix
from omnigent.onboarding.ambient import DetectedProvider, detect_providers
from omnigent.onboarding.configure_models import (
    build_cli_config_provider_entry,
    build_key_provider_entry,
    build_subscription_provider_entry,
    default_base_url_for_family,
    key_provider_endpoint,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    LOCAL_KIND,
    OPENAI_FAMILY,
    SUBSCRIPTION_KIND,
    get_default_provider,
    load_providers,
    provider_families,
    set_default_provider,
)

# The families auto-default resolution walks, in a stable order. ``gemini``
# is included so a detected-only GEMINI_API_KEY (the antigravity-sdk harness's
# credential) auto-becomes the gemini-family default in the read-time merge,
# matching how a detected anthropic / openai key auto-defaults.
_FAMILIES = (ANTHROPIC_FAMILY, OPENAI_FAMILY, GEMINI_FAMILY)

# Top-level config key listing detection names the user has dismissed by
# removing the adopted entry. A detection whose backing credential cannot be
# "signed out" (an env API key, a codex config.toml provider — anything
# non-subscription) would otherwise bounce straight back on the next
# configure open, making Remove a no-op. Both merge surfaces below skip
# dismissed names; re-adding the credential (the add menu's detected option)
# clears its dismissal.
DISMISSED_DETECTIONS_KEY = "dismissed_detections"


def dismissed_detection_names(config: dict[str, object]) -> frozenset[str]:
    """Return the detection names the user has dismissed in *config*.

    :param config: The parsed config mapping, e.g.
        ``{"dismissed_detections": ["codex-databricks"], "providers": {...}}``.
    :returns: The dismissed names, e.g. ``frozenset({"codex-databricks"})``;
        empty when the key is absent or not a list (a malformed value is
        treated as "nothing dismissed" — the next dismissal write self-heals
        it into a proper list).
    """
    raw = config.get(DISMISSED_DETECTIONS_KEY)
    if not isinstance(raw, list):
        return frozenset()
    return frozenset(name for name in raw if isinstance(name, str))


def codex_config_provider_dismissed(config: dict[str, object]) -> bool:
    """Whether the host codex config's custom default provider was dismissed.

    The codex executor bridges the user's ``~/.codex/config.toml`` into the
    per-session ``CODEX_HOME``, so when a launch resolves NO provider entry
    at all, the file's custom default ``model_provider`` still routes the
    session. After the user Removed (dismissed) that detected provider, the
    launch must neutralize it (pin codex's built-in ``openai`` provider)
    instead of silently routing through the very credential the user
    removed. This is the launch-side predicate for that case; it is
    deliberately scoped to a *dismissed* provider — an undetectable custom
    provider (e.g. ``env_key`` auth) keeps its config.toml routing.

    :param config: The parsed config mapping (``dismissed_detections`` key).
    :returns: ``True`` when ``~/.codex/config.toml`` selects a custom,
        self-contained-auth default provider AND its detection name is
        dismissed in *config*.
    """
    from omnigent.onboarding.ambient import codex_config_detection

    det = codex_config_detection()
    return det is not None and det.name in dismissed_detection_names(config)


def _drop_dismissed(
    synthesized: dict[str, dict[str, object]],
    dismissed: frozenset[str],
) -> dict[str, dict[str, object]]:
    """Drop synthesized entries whose detection name the user dismissed.

    :param synthesized: Config-shape entries keyed by detection name, from
        :func:`synthesize_detected_entries`.
    :param dismissed: Dismissed names from :func:`dismissed_detection_names`.
    :returns: A new mapping without the dismissed entries.
    """
    if not dismissed:
        return synthesized
    return {name: entry for name, entry in synthesized.items() if name not in dismissed}


def _synthesize_entry(det: DetectedProvider) -> dict[str, object] | None:
    """Build a config-shape provider entry for one detection.

    :param det: A credential found by
        :func:`omnigent.onboarding.ambient.detect_providers`.
    :returns: A raw provider entry body (config shape), or ``None`` when the
        detection maps to no omnigent harness surface (a ``family``-less
        ``key`` detection).
    """
    if det.kind == "subscription":
        # ``det.name`` is the CLI login (``"claude"`` / ``"codex"``).
        return build_subscription_provider_entry(det.name)

    if det.kind == "cli-config":
        # A custom model provider defined (and authenticated) by the codex
        # CLI's own config.toml — pin it by name; the credential stays in
        # that file. ``model_provider`` is always set on this kind (the
        # detector constructs it from the provider id it just matched).
        if det.model_provider is None:
            return None
        return build_cli_config_provider_entry("codex", det.model_provider, det.display_name)

    if det.name == "vertex-claude":
        # Claude Code on Vertex AI — the CLI authenticates via its own env
        # vars and GCP ADC.  A subscription entry makes the native-claude
        # resolver skip gateway routing, letting the CLI use Vertex natively.
        return build_subscription_provider_entry("claude")

    if det.family is None:
        # An env key we detect but can't route to a harness (a detection
        # whose provider maps to no omnigent family).
        return None

    if det.kind == "key":
        # ``det.source`` is the ``$VAR`` reference; persist it as an
        # ``env:`` ref so the entry tracks the live environment variable.
        env_var = det.source[1:] if det.source.startswith("$") else det.source
        api_key_ref = f"env:{env_var}"
        vendor = key_provider_endpoint(det.name)
        if vendor is not None:
            # A third-party OpenAI-compatible vendor (OpenRouter, …): its
            # OWN base_url + Chat wire, not api.openai.com.
            base_url, wire_api = vendor.base_url, vendor.wire_api
        else:
            base_url, wire_api = default_base_url_for_family(det.family), None
            # An ``OPENAI_API_KEY`` detection honors a companion
            # ``OPENAI_BASE_URL`` (the same convention the OpenAI SDK reads,
            # matching the interactive wizard / non-interactive onboarding /
            # ``provider_selection._read_credentials_from_env``). Without
            # this, an env key pointed at an OpenAI-compatible gateway (e.g.
            # the Databricks AI gateway) is synthesized against
            # ``api.openai.com`` and every request 401s — the credential is a
            # gateway token, not an OpenAI key. Scoped to the openai family's
            # canonical vendor (not a third-party endpoint, handled above).
            if det.family == OPENAI_FAMILY and env_var in (
                "OPENAI_API_KEY",
                "OMNIGENT_OPENAI_API_KEY",
            ):
                env_base_url = getenv_nonempty_with_omnigent_prefix("OPENAI_BASE_URL")
                if env_base_url is not None:
                    base_url = env_base_url[1]
        # No pinned model — the spec / catalog default picks it; /model then
        # shows "(no model pinned)" rather than a fabricated one.
        return build_key_provider_entry(det.family, base_url, api_key_ref, None, wire_api=wire_api)

    if det.kind == "local":
        # A self-hosted OpenAI-compatible server (Ollama). ``det.source`` is
        # the base host (e.g. ``http://localhost:11434``); append the
        # OpenAI-compatible ``/v1`` path. The key is a placeholder the
        # server ignores but the family block requires a credential source.
        base_url = det.source.rstrip("/") + "/v1"
        return {
            "kind": LOCAL_KIND,
            det.family: {"base_url": base_url, "api_key": "ollama"},
        }

    return None


def synthesize_detected_entries(
    detected: list[DetectedProvider],
) -> dict[str, dict[str, object]]:
    """Build config-shape provider entries from ambient detections.

    :param detected: Detections from
        :func:`omnigent.onboarding.ambient.detect_providers`, in priority
        order.
    :returns: Raw provider entries keyed by the detection name, e.g.
        ``{"anthropic": {"kind": "key", ...}, "codex": {"kind":
        "subscription", "cli": "codex"}}``. A detected GEMINI_API_KEY is
        adopted as a ``gemini``-family ``key`` provider (the antigravity-sdk
        surface) — see :data:`omnigent.onboarding.ambient._ENV_KEY_FAMILY`.
        Only detections that map to no omnigent family at all (a ``family``-less
        :class:`DetectedProvider`) are skipped. The mapping preserves detection
        order.
    """
    entries: dict[str, dict[str, object]] = {}
    for det in detected:
        entry = _synthesize_entry(det)
        if entry is not None:
            entries[det.name] = entry
    return entries


def _explicit_providers(config: dict[str, object]) -> dict[str, object]:
    """Return the raw ``providers:`` mapping from *config* (or empty).

    :param config: The parsed config mapping.
    :returns: The ``providers`` sub-mapping, or ``{}`` when absent/invalid.
    """
    raw = config.get("providers")
    return dict(raw) if isinstance(raw, dict) else {}


def _configured_subscription_clis(explicit: dict[str, object]) -> set[str]:
    """The CLI logins already configured as subscriptions in *explicit*.

    A subscription provider is the CLI's own login, and a harness has at
    most one. The ambient detector names a Claude login ``"claude"``, but
    the user may have already added that same login explicitly under a
    different name (``"claude-subscription"``). Adopting the detection by
    name would then write a *second* subscription for the same CLI — the
    ``claude`` + ``claude-subscription`` duplicate. This reports the CLIs
    already covered so :func:`synthesize_detected_entries`'s output can be
    skipped for them regardless of the explicit entry's name.

    :param explicit: The raw explicit ``providers:`` mapping (entry bodies
        keyed by name).
    :returns: The set of CLI names with a configured subscription, e.g.
        ``{"claude"}``; empty when none are configured.
    """
    clis: set[str] = set()
    for entry in load_providers({"providers": explicit}).values():
        if entry.kind == SUBSCRIPTION_KIND and entry.cli:
            clis.add(entry.cli)
    return clis


def _drop_covered_subscriptions(
    synthesized: dict[str, dict[str, object]],
    covered_clis: set[str],
) -> dict[str, dict[str, object]]:
    """Drop synthesized subscriptions whose CLI is already configured.

    Keeps every non-subscription detection and any subscription whose CLI
    has no explicit entry yet; removes a synthesized subscription for a CLI
    already covered explicitly (under any name), so a detected ``claude``
    login is not adopted on top of an existing ``claude-subscription``.

    :param synthesized: Config-shape entries keyed by detection name, from
        :func:`synthesize_detected_entries`.
    :param covered_clis: CLIs already configured as subscriptions, from
        :func:`_configured_subscription_clis`, e.g. ``{"claude"}``.
    :returns: A new mapping with the duplicate subscriptions removed.
    """
    return {
        name: entry
        for name, entry in synthesized.items()
        if not (entry.get("kind") == SUBSCRIPTION_KIND and entry.get("cli") in covered_clis)
    }


def effective_config_with_detected(
    config: dict[str, object],
    detected: list[DetectedProvider] | None = None,
) -> dict[str, object]:
    """Return *config* with ambient detections merged in (read-only).

    Explicit providers win on name conflict (the user's config is
    authoritative). For each family with **no explicit default**, the first
    detected provider serving that family is marked its default — so a fresh
    machine with only ambient credentials still resolves a default (and the
    ``/model`` readout names it) without anything written to disk.

    :param config: The parsed config mapping (``providers:`` block).
    :param detected: Detections to merge; defaults to a live
        :func:`omnigent.onboarding.ambient.detect_providers` call.
    :returns: A new config mapping whose ``providers`` block is the merged
        view. The input is not mutated.
    """
    if detected is None:
        detected = detect_providers()
    explicit = _explicit_providers(config)
    # Drop a detected subscription whose CLI is already configured under a
    # different name (``claude`` detection vs an explicit ``claude-subscription``)
    # so the merged view never shows the same login twice.
    synthesized = _drop_covered_subscriptions(
        synthesize_detected_entries(detected), _configured_subscription_clis(explicit)
    )
    # A dismissed detection must not re-enter the merged view either —
    # otherwise a Removed credential keeps routing/showing as the default
    # even though the manager no longer lists it.
    synthesized = _drop_dismissed(synthesized, dismissed_detection_names(config))
    # Explicit entries override synthesized ones of the same name.
    merged: dict[str, object] = {**synthesized, **explicit}

    explicit_config = {"providers": explicit}
    merged_parsed = load_providers({"providers": merged})
    for family in _FAMILIES:
        # An explicit default for this family always wins — never overridden.
        if get_default_provider(explicit_config, family) is not None:
            continue
        for det in detected:
            name = det.name
            if name not in synthesized or name in explicit:
                continue  # not synthesizable, or overridden by explicit
            entry = merged_parsed.get(name)
            if entry is not None and family in provider_families(entry):
                merged = set_default_provider(merged, name, family)
                break

    return {**config, "providers": merged}


def providers_to_adopt(
    config: dict[str, object],
    detected: list[DetectedProvider] | None = None,
) -> dict[str, dict[str, object]]:
    """Return the detected entries ``configure harness`` should persist.

    These are detections not already present (by name) in the explicit
    config — the new provider entries to write so they become ordinary,
    editable providers (no "detected vs configured" split in the manager).
    Already-configured names are skipped (the user's entry is authoritative).

    :param config: The parsed config mapping (``providers:`` block).
    :param detected: Detections to consider; defaults to a live
        :func:`omnigent.onboarding.ambient.detect_providers` call.
    :returns: New raw provider entries keyed by name, ready to merge under
        ``providers:``. Empty when every detection is already configured.
    """
    if detected is None:
        detected = detect_providers()
    explicit = _explicit_providers(config)
    # Skip a subscription detection whose CLI is already configured (under any
    # name) — adopting ``claude`` on top of an explicit ``claude-subscription``
    # would persist a duplicate subscription for the one CLI login.
    synthesized = _drop_covered_subscriptions(
        synthesize_detected_entries(detected), _configured_subscription_clis(explicit)
    )
    # A dismissed detection stays un-adopted until the user re-adds it
    # explicitly (which clears the dismissal) — otherwise Remove would be
    # undone on the very next configure open.
    synthesized = _drop_dismissed(synthesized, dismissed_detection_names(config))
    return {name: entry for name, entry in synthesized.items() if name not in explicit}
