"""Tests for omnigent.onboarding.provider_config."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    OPENAI_FAMILY,
    PI_SURFACE,
    default_provider_for_harness,
    harness_family,
    load_providers,
    provider_families,
    provider_family_for_harness,
    set_default_provider,
    surface_default_model,
    surface_default_provider,
)


@pytest.mark.parametrize(
    "harness,expected",
    [
        ("claude-sdk", ANTHROPIC_FAMILY),
        # Native CLI harnesses — the canonical spec spellings. These were
        # missing from the family map, so a claude-native / codex-native
        # agent's credential failed to resolve for the /model readout and the
        # startup-header creds line (nessie's sub-agents use exactly these).
        ("claude-native", ANTHROPIC_FAMILY),
        ("codex-native", OPENAI_FAMILY),
        # The reversed spellings are also accepted.
        ("native-claude", ANTHROPIC_FAMILY),
        ("native-codex", OPENAI_FAMILY),
        ("codex", OPENAI_FAMILY),
        ("openai-agents", OPENAI_FAMILY),
        # An unknown harness has no family (caller falls back / shows nothing).
        ("some-unknown-harness", None),
    ],
)
def test_harness_family_maps_native_harness_spellings(harness: str, expected: str | None) -> None:
    """``harness_family`` resolves both native-harness spellings to a family.

    Proves the fix for the multi-vendor startup header / ``/model`` readout:
    nessie's sub-agents declare ``claude-native`` / ``codex-native``, which
    the family map previously didn't carry (it only had the reversed
    ``native-claude`` / ``native-codex``), so the openai family went
    undetected. A regression that drops the canonical spellings returns
    ``None`` here and the creds line would silently omit Codex.
    """
    assert harness_family(harness) == expected


@pytest.mark.parametrize(
    "harness,expected",
    [
        # Canonical ids keyed in the family map.
        ("claude-native", ANTHROPIC_FAMILY),
        ("codex-native", OPENAI_FAMILY),
        ("claude-sdk", ANTHROPIC_FAMILY),
        ("openai-agents", OPENAI_FAMILY),
        # Executor-type spellings AgentSpec.harness_kind returns for SDK
        # harnesses — these are NOT keys in _HARNESS_FAMILY, so they only
        # resolve via the executor-type alias map. A regression dropping the
        # alias returns None and a same-family SDK fork would be misjudged
        # cross-family (model settings + native carry wrongly reset).
        ("claude_sdk", ANTHROPIC_FAMILY),
        ("agents_sdk", OPENAI_FAMILY),
        # The "claude" shorthand canonicalizes to claude-sdk.
        ("claude", ANTHROPIC_FAMILY),
        ("some-unknown-harness", None),
        (None, None),
    ],
)
def test_provider_family_for_harness_accepts_executor_type_spellings(
    harness: str | None, expected: str | None
) -> None:
    """``provider_family_for_harness`` resolves SDK executor-type spellings.

    The fork agent-switch reads ``AgentSpec.harness_kind``, which returns
    executor types (``claude_sdk`` / ``agents_sdk``) for SDK agents — not
    the canonical ``claude-sdk`` / ``openai-agents`` keys. This helper must
    bridge both so a claude_sdk → claude-native switch is recognised as
    same-family (anthropic) and carries history.
    """
    assert provider_family_for_harness(harness) == expected


def test_default_provider_for_pi_skips_subscription_defaults() -> None:
    """For the unmapped ``pi`` harness, a subscription default is skipped.

    A subscription entry's credential is the claude/codex CLI's own login,
    which pi does not wrap and cannot read —
    ``configure_agent_harness_with_provider`` no-ops on subscription kind, so
    routing pi to one spawns the harness with no auth at all ("No API key
    found", observed live on the nessie ``pi`` sub-agent). The resolver must
    fall through to the next family's default instead. A regression here
    re-selects the claude subscription and the pi worker spawns authless.
    """
    config = {
        "providers": {
            "claude": {"kind": "subscription", "default": True, "cli": "claude"},
            "databricks": {"kind": "databricks", "default": "openai", "profile": "p1"},
        }
    }
    # pi skips the anthropic-family subscription and lands on the openai-
    # family databricks default, which it CAN consume (ucode/gateway path).
    assert default_provider_for_harness(config, "pi").name == "databricks"
    # The mapped claude-sdk harness still takes the subscription — it wraps
    # the claude CLI, so the CLI login is exactly its credential.
    assert default_provider_for_harness(config, "claude-sdk").name == "claude"


def test_default_provider_for_pi_none_when_only_subscriptions() -> None:
    """Subscription-only configs resolve no default for ``pi``.

    With nothing but CLI-login providers configured, pi has no consumable
    provider: the resolver must return ``None`` (the spawn builder then
    leaves auth to pi's own login state) rather than a claude/codex login
    pi cannot read. Returning the subscription here would also make
    credential readouts claim pi runs on "claude CLI login".
    """
    config = {
        "providers": {
            "claude": {"kind": "subscription", "default": True, "cli": "claude"},
            "codex": {"kind": "subscription", "default": True, "cli": "codex"},
        }
    }
    assert default_provider_for_harness(config, "pi") is None


def test_default_provider_for_pi_skips_cli_config_defaults() -> None:
    """For the unmapped ``pi`` harness, a cli-config default is skipped.

    A cli-config entry pins a provider table in ~/.codex/config.toml (e.g.
    isaac's Databricks AI Gateway); only the codex harness bridges that file,
    and ``configure_agent_harness_with_provider`` raises for any other
    harness. A regression here makes the resolver hand pi the codex-only
    gateway: the REPL startup header then shows "Pi → ⚙️ Databricks AI
    Gateway" while ``setup`` (which filters via ``provider_families``)
    correctly shows pi as credential-less, and an actual pi spawn fails.
    """
    config = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "default": True,
                "cli": "codex",
                "model_provider": "databricks",
            },
        }
    }
    # With only the codex-pinned gateway configured, pi must resolve no
    # default — the gateway's credential lives in codex's config.toml,
    # which pi cannot read. A non-None result means the fallback regressed
    # to accepting cli-config and the header/setup readouts diverge again.
    assert default_provider_for_harness(config, "pi") is None
    # The codex harness itself still takes the cli-config default — it is
    # exactly the CLI whose config.toml carries the provider table.
    assert default_provider_for_harness(config, "codex").name == "codex-databricks"


# ── the pi default scope ──────────────────────────────────────────────


def _key_entry(
    family: str, *, default: object = None, model: str | None = None
) -> dict[str, object]:
    """Build a raw ``kind: key`` provider entry for one family.

    :param family: The family the key serves, ``"anthropic"`` or ``"openai"``.
    :param default: The raw ``default:`` value to carry, e.g. ``True`` or
        ``["openai", "pi"]``; ``None`` omits the key.
    :param model: The family's ``models.default`` pin, e.g. ``"gpt-5.5"``;
        ``None`` omits the ``models`` block.
    :returns: The raw entry mapping, ready for a ``providers:`` block.
    """
    block: dict[str, object] = {
        "base_url": "https://api.example.com/v1",
        "api_key_ref": f"env:{family.upper()}_KEY",
    }
    if model is not None:
        block["models"] = {"default": model}
    entry: dict[str, object] = {"kind": "key", family: block}
    if default is not None:
        entry["default"] = default
    return entry


def test_pi_scope_parses_and_outranks_fallback() -> None:
    """An explicit ``"pi"`` in ``default:`` parses and wins pi resolution.

    The authoritative-setup invariant: a key marked ``default: ["openai",
    "pi"]`` must beat the anthropic-preferred fallback (which would
    otherwise pick the anthropic-family default). A regression that drops
    the pi scope from parsing (or from resolution precedence) returns the
    anthropic key here.
    """
    config = {
        "providers": {
            "anthropic": _key_entry(ANTHROPIC_FAMILY, default=True),
            "openai": _key_entry(OPENAI_FAMILY, default=["openai", "pi"]),
        }
    }
    # The openai entry carries the pi scope after parsing.
    assert PI_SURFACE in load_providers(config)["openai"].default_families
    # Explicit pi scope outranks the anthropic-first fallback.
    assert default_provider_for_harness(config, "pi").name == "openai"
    # The single-family surfaces are untouched by the pi scope.
    assert surface_default_provider(config, ANTHROPIC_FAMILY).name == "anthropic"
    assert surface_default_provider(config, OPENAI_FAMILY).name == "openai"


def test_default_true_never_claims_pi_scope() -> None:
    """``default: true`` expands to the served model families only — never pi.

    Two coexisting ``default: true`` keys (one per family) are a valid,
    common config. If ``true`` expanded to the pi scope, both would claim
    it and pi resolution would fail loud on the clash; instead pi must
    resolve via the anthropic-preferred fallback.
    """
    config = {
        "providers": {
            "anthropic": _key_entry(ANTHROPIC_FAMILY, default=True),
            "openai": _key_entry(OPENAI_FAMILY, default=True),
        }
    }
    providers = load_providers(config)
    # `true` claims only the model family each key serves.
    assert providers["anthropic"].default_families == frozenset({ANTHROPIC_FAMILY})
    assert providers["openai"].default_families == frozenset({OPENAI_FAMILY})
    # No clash: pi falls back to the anthropic-family default.
    assert default_provider_for_harness(config, "pi").name == "anthropic"


def test_subscription_cannot_claim_pi_scope() -> None:
    """Naming ``"pi"`` in a subscription's default scope fails loud.

    Both at parse time (a hand-edited config) and via set_default_provider
    (the menu path) — a subscription can never drive pi, so persisting the
    scope would wedge pi on an unusable credential.
    """
    raw = {"kind": "subscription", "cli": "claude", "default": ["pi"]}
    with pytest.raises(OmnigentError):
        load_providers({"providers": {"claude-subscription": raw}})
    block = {"claude-subscription": {"kind": "subscription", "cli": "claude"}}
    with pytest.raises(OmnigentError):
        set_default_provider(block, "claude-subscription", PI_SURFACE)


def test_set_default_provider_pi_scope_round_trips_and_moves() -> None:
    """Setting the pi scope persists in a re-parseable form and moves cleanly.

    The ``default: true`` compact form must NOT absorb the pi scope on
    rewrite (re-parsing ``true`` would drop it — the round-trip bug), and
    moving the pi default to another provider must clear it from the first
    while leaving both providers' family defaults untouched.
    """
    providers: dict[str, object] = {
        "anthropic": _key_entry(ANTHROPIC_FAMILY, default=True),
        "openai": _key_entry(OPENAI_FAMILY, default=True),
    }
    after_first = set_default_provider(providers, "anthropic", PI_SURFACE)
    parsed = load_providers({"providers": after_first})
    # The pi scope survived a write→parse round-trip (not collapsed to true).
    assert parsed["anthropic"].default_families == frozenset({ANTHROPIC_FAMILY, PI_SURFACE})

    after_move = set_default_provider(after_first, "openai", PI_SURFACE)
    moved = load_providers({"providers": after_move})
    # pi moved to openai; each key kept its own family default.
    assert moved["anthropic"].default_families == frozenset({ANTHROPIC_FAMILY})
    assert moved["openai"].default_families == frozenset({OPENAI_FAMILY, PI_SURFACE})


@pytest.mark.parametrize(
    "raw,expect_pi",
    [
        # Every kind that can hand pi a usable credential serves the scope.
        ({"kind": "key", "openai": {"base_url": "https://x/v1", "api_key_ref": "env:K"}}, True),
        (
            {"kind": "gateway", "anthropic": {"base_url": "https://x", "api_key_ref": "env:K"}},
            True,
        ),
        ({"kind": "databricks", "profile": "my-ws"}, True),
        # A CLI login is unusable outside its own CLI — never pi-capable.
        ({"kind": "subscription", "cli": "claude"}, False),
    ],
)
def test_provider_families_pi_capability(raw: dict[str, object], expect_pi: bool) -> None:
    """``provider_families`` reports the pi scope for every kind but subscription.

    This drives both the Pi page's credential list (which rows appear) and
    set-default validation — a regression in either direction lets the menu
    offer a credential pi can't use, or hides one it can.
    """
    entry = load_providers({"providers": {"p": raw}})["p"]
    assert (PI_SURFACE in provider_families(entry)) is expect_pi


def test_surface_default_model_prefers_anthropic_for_pi() -> None:
    """``surface_default_model`` mirrors pi's anthropic-preferred auth pick.

    A two-family gateway shows its anthropic default model under the Pi
    page (matching `_apply_provider_to_pi`'s auth-source order); a
    codex-only key shows its openai model; the family surfaces are
    unchanged direct lookups.
    """
    gateway = load_providers(
        {
            "providers": {
                "gw": {
                    "kind": "gateway",
                    "anthropic": {
                        "base_url": "https://gw",
                        "api_key_ref": "env:K",
                        "models": {"default": "claude-sonnet-4-6"},
                    },
                    "openai": {
                        "base_url": "https://gw/v1",
                        "api_key_ref": "env:K",
                        "models": {"default": "gpt-5.5"},
                    },
                }
            }
        }
    )["gw"]
    assert surface_default_model(gateway, PI_SURFACE) == "claude-sonnet-4-6"
    assert surface_default_model(gateway, OPENAI_FAMILY) == "gpt-5.5"

    openai_only = load_providers(
        {"providers": {"openai": _key_entry(OPENAI_FAMILY, model="gpt-5.5")}}
    )["openai"]
    assert surface_default_model(openai_only, PI_SURFACE) == "gpt-5.5"


# ── cli-config kind: parsing, families, readout ─────────────────────────────


def test_parse_cli_config_entry() -> None:
    """A cli-config entry parses with its pin fields and openai family.

    Failure means adoption-written entries stop loading (every configure
    open would crash) or the entry loses its harness surface.
    """
    from omnigent.onboarding.provider_config import load_providers, provider_families

    entry = load_providers(
        {
            "providers": {
                "codex-databricks": {
                    "kind": "cli-config",
                    "cli": "codex",
                    "model_provider": "Databricks",
                    "display_name": "Databricks AI Gateway",
                    "default": True,
                }
            }
        }
    )["codex-databricks"]
    assert entry.kind == "cli-config"
    assert entry.cli == "codex"
    assert entry.model_provider == "Databricks"
    assert entry.display_name == "Databricks AI Gateway"
    # Serves (and can default) exactly the codex/openai surface.
    assert provider_families(entry) == frozenset({OPENAI_FAMILY})
    assert entry.default_families == frozenset({OPENAI_FAMILY})


@pytest.mark.parametrize(
    "body,message_fragment",
    [
        # Only codex has config-file model providers; a claude analog would
        # be a deliberate extension, not a silently-accepted value.
        (
            {"kind": "cli-config", "cli": "claude", "model_provider": "X"},
            "requires cli: 'codex'",
        ),
        # The pin target is the entry's whole point — fail loud without it.
        ({"kind": "cli-config", "cli": "codex"}, "'model_provider'"),
    ],
)
def test_parse_cli_config_entry_invalid(body: dict[str, object], message_fragment: str) -> None:
    """Malformed cli-config entries fail loud with a pointed message.

    Failure means a broken entry would parse into a launch that pins
    nothing (or the wrong CLI) at run time.
    """
    from omnigent.errors import OmnigentError
    from omnigent.onboarding.provider_config import load_providers

    with pytest.raises(OmnigentError, match=r"cli-config|model_provider|cli"):
        load_providers({"providers": {"bad": body}})
    try:
        load_providers({"providers": {"bad": body}})
    except OmnigentError as exc:
        # The message names the missing/wrong field so the user can fix
        # config.yaml without reading source.
        assert message_fragment in str(exc)


def test_describe_active_credential_cli_config() -> None:
    """The /model readout describes a cli-config default truthfully.

    Failure means the readout would crash on (or misname) an adopted
    isaac-style provider.
    """
    from omnigent.onboarding.provider_config import describe_active_credential

    config = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
                "default": True,
            }
        }
    }
    cred = describe_active_credential(config, "codex")
    assert cred is not None
    assert cred.kind == "cli-config"
    assert cred.provider_name == "codex-databricks"
    # The source names the file and the pinned provider — the two facts a
    # user needs to find/edit the underlying credential.
    assert cred.source == "~/.codex/config.toml provider: Databricks"
    # No inline endpoint/model: both live in the CLI's own config.
    assert cred.base_url is None
    assert cred.model is None


def test_describe_active_credential_antigravity_reports_gemini_not_openai(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The /model readout names the Gemini key for an antigravity harness.

    The regression this guards: ``harness_family("antigravity")`` is
    ``"openai"`` (a family-keyed lookup convenience), so the readout used to
    resolve the OpenAI default provider and show an OpenAI credential for a
    Gemini-native agent. It must instead describe the ``antigravity:`` block's
    Gemini key — with no ``openai`` family / base_url — even when an OpenAI
    provider is the configured default.
    """
    from omnigent.onboarding.provider_config import describe_active_credential

    monkeypatch.setenv("MY_GEMINI", "AIza-test")
    config = {
        "antigravity": {"api_key_ref": "env:MY_GEMINI"},
        "providers": {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": {"base_url": "https://api.openai.com/v1", "api_key": "$OPENAI_API_KEY"},
            }
        },
    }
    cred = describe_active_credential(config, "antigravity")
    assert cred is not None
    assert cred.provider_name == "antigravity"
    assert cred.kind == "key"
    # Crucially NOT the openai family / its base_url.
    assert cred.family is None
    assert cred.base_url is None
    assert cred.source == "env:MY_GEMINI"


def test_describe_active_credential_antigravity_alias_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readout special-case also fires for the ``agy`` alias spelling."""
    from omnigent.onboarding.provider_config import describe_active_credential

    monkeypatch.setenv("MY_GEMINI", "AIza-test")
    config = {"antigravity": {"api_key_ref": "env:MY_GEMINI"}}
    cred = describe_active_credential(config, "agy")
    assert cred is not None
    assert cred.provider_name == "antigravity"


def test_describe_active_credential_antigravity_none_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no Gemini key (block or ambient), the antigravity readout is None."""
    from omnigent.onboarding.provider_config import describe_active_credential

    for var in ("GEMINI_API_KEY", "ANTIGRAVITY_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    assert describe_active_credential({}, "antigravity") is None


def test_describe_active_credential_antigravity_dangling_ref_reads_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling ``env:`` antigravity ref reads as NOT configured in the readout.

    The regression this guards: the readout used to report an active credential
    whenever the ``antigravity:`` block carried a reference, WITHOUT resolving
    it. A dangling ``env:MISSING`` / keychain ref (whose secret no longer exists)
    would then show an active Gemini credential in ``/model`` / the startup
    header — yet ``_build_antigravity_spawn_env`` uses
    ``resolve_antigravity_api_key`` and silently omits it, so the worker runs
    with no key. The readout must resolve the ref first and mark it unconfigured
    when resolution fails. With no ambient Gemini var either, the readout is
    ``None``.
    """
    from omnigent.onboarding.provider_config import describe_active_credential

    for var in ("GEMINI_API_KEY", "ANTIGRAVITY_API_KEY", "MISSING_GEMINI"):
        monkeypatch.delenv(var, raising=False)
    # The block references an env var that is not set — a dangling ref.
    config = {"antigravity": {"api_key_ref": "env:MISSING_GEMINI"}}
    assert describe_active_credential(config, "antigravity") is None


def test_describe_active_credential_antigravity_dangling_ref_falls_back_to_ambient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dangling config ref does not suppress a genuinely-present ambient key.

    Complement to the dangling-ref test: when the configured ref does not
    resolve but an ambient ``GEMINI_API_KEY`` IS set, the readout reports the
    ambient credential (mirroring the spawn-env fallback), not ``None``.
    """
    from omnigent.onboarding.provider_config import describe_active_credential

    monkeypatch.delenv("ANTIGRAVITY_API_KEY", raising=False)
    monkeypatch.delenv("MISSING_GEMINI", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-ambient")
    config = {"antigravity": {"api_key_ref": "env:MISSING_GEMINI"}}
    cred = describe_active_credential(config, "antigravity")
    assert cred is not None
    assert cred.source == "env:GEMINI_API_KEY"


def test_provider_family_for_harness_antigravity_is_google() -> None:
    """Antigravity reports its own ``google`` family, not ``openai``.

    The fork/switch model-carry decision (server ``_same_provider_family`` /
    the ap-web ``harnessFamily`` reset hint) keys off this. Reporting
    ``openai`` would (1) wrongly copy an OpenAI model id into a Gemini-native
    agent on a codex→antigravity switch, and (2) disagree with the UI, which
    reports ``google``. Accepts the alias spellings too.
    """
    from omnigent.onboarding.provider_config import GOOGLE_FAMILY

    assert provider_family_for_harness("antigravity") == GOOGLE_FAMILY
    assert provider_family_for_harness("agy") == GOOGLE_FAMILY
    assert provider_family_for_harness("google-antigravity") == GOOGLE_FAMILY
    # It is distinct from openai, so a codex↔antigravity switch is cross-family.
    assert provider_family_for_harness("antigravity") != provider_family_for_harness("codex")
