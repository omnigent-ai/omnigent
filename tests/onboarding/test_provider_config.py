"""Tests for omnigent.onboarding.provider_config."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    OPENAI_FAMILY,
    PI_SURFACE,
    default_provider_for_harness,
    harness_family,
    load_providers,
    provider_families,
    provider_family_for_harness,
    resolve_secret,
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
        # Qwen Code is OpenAI-compatible; the native TUI harness keys both
        # spellings so a same-agent qwen→qwen fork/switch reads same-family.
        ("qwen", OPENAI_FAMILY),
        ("qwen-native", OPENAI_FAMILY),
        ("native-qwen", OPENAI_FAMILY),
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


def test_resolve_secret_env_ref_accepts_omnigent_prefixed_alias(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``env:ANTHROPIC_API_KEY`` falls back to ``OMNIGENT_ANTHROPIC_API_KEY``."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OMNIGENT_ANTHROPIC_API_KEY", "sk-ant-prefixed")

    assert resolve_secret("env:ANTHROPIC_API_KEY") == "sk-ant-prefixed"
    assert resolve_secret("$ANTHROPIC_API_KEY") == "sk-ant-prefixed"


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


_DATABRICKS_CODEX_CONFIG_TOML = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "jq"
args = ["-r", ".access_token", "/Users/me/.databricks/model-serving-token.json"]
timeout_ms = 5000
"""


def _write_codex_toml(home: Path, body: str) -> None:
    """Write a ``~/.codex/config.toml`` under *home* (resolver reads $HOME)."""
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(body, encoding="utf-8")


def test_default_provider_for_pi_selects_cli_config_databricks_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """For the unmapped ``pi`` harness, a cli-config Databricks gateway IS selected.

    A cli-config entry pins a provider table in ~/.codex/config.toml. PR #1251
    made a Databricks AI Gateway cli-config pi-consumable (Pi speaks its
    Anthropic surface natively), and pi resolution now routes it (pi-native
    translates it; the gateway-harness pi path translates it too). So when the
    pinned ``[model_providers.X]`` resolves to a real Databricks gateway, the
    shared selection returns it for pi — the previous "skip all cli-config for
    pi" behavior was the bug.
    """
    _write_codex_toml(tmp_path, _DATABRICKS_CODEX_CONFIG_TOML)
    monkeypatch.setenv("HOME", str(tmp_path))

    config = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "default": True,
                "cli": "codex",
                "model_provider": "Databricks",
            },
        }
    }
    pi_default = default_provider_for_harness(config, "pi")
    assert pi_default is not None
    assert pi_default.name == "codex-databricks"
    # The codex harness still takes the cli-config default — it is exactly the
    # CLI whose config.toml carries the provider table.
    assert default_provider_for_harness(config, "codex").name == "codex-databricks"


def test_default_provider_for_pi_skips_non_databricks_cli_config_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NON-Databricks (or unresolvable) cli-config default is still skipped for pi.

    Selecting a non-Databricks cli-config for pi would just drop to Pi's own
    login (``_cli_config_pi_provider`` returns None for it), so the pi fallback
    must skip it. Here the pinned table points at a generic proxy, so pi
    resolves no default (the REPL header / setup must show pi credential-less),
    while codex still takes it.
    """
    _write_codex_toml(
        tmp_path,
        """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Some Other Proxy"
base_url = "https://proxy.example.com/v1"

[model_providers.Databricks.auth]
command = "printf"
args = ["%s", "sk-static"]
""",
    )
    monkeypatch.setenv("HOME", str(tmp_path))

    config = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "default": True,
                "cli": "codex",
                "model_provider": "Databricks",
            },
        }
    }
    # A non-Databricks cli-config is not pi-consumable → pi falls back to None.
    assert default_provider_for_harness(config, "pi") is None
    # The codex harness still takes the cli-config default.
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


def test_gemini_key_never_becomes_pi_default() -> None:
    """A gemini key serves ONLY the Gemini surface — never pi.

    pi consumes the anthropic / openai families only (a gemini key's
    add-surface scoping is ``frozenset({GEMINI_FAMILY})`` — no pi scope). So
    a machine whose only configured provider is a gemini key must leave pi
    UNRESOLVED: the cross-family pi fallback must skip gemini. A regression
    that walks gemini in the pi fallback silently routes pi through a
    credential it cannot use (incorrect default, broken pi launches).
    """
    config = {"providers": {"gemini": _key_entry(GEMINI_FAMILY, default=True)}}
    # The gemini (antigravity-native) surface still resolves to its key…
    assert default_provider_for_harness(config, "antigravity-native").name == "gemini"
    # …but pi does NOT — gemini is not a pi-capable family.
    assert default_provider_for_harness(config, "pi") is None
    assert surface_default_provider(config, PI_SURFACE) is None


def test_gemini_key_not_pi_capable_surface() -> None:
    """A gemini key's served-surface set excludes pi (no auto/explicit pi default).

    ``provider_families`` drives the add flow's "default every served surface"
    step AND set_default_provider's scope validation. If a gemini key reported
    pi, adding it would auto-write a pi default (and the Pi credential menu
    would list it), wedging pi on a credential it cannot consume. So a gemini
    key must serve only the Gemini surface, and scoping it to pi must fail loud
    (parity with subscriptions, which also cannot drive pi).
    """
    config = {"providers": {"gemini": _key_entry(GEMINI_FAMILY, default=True)}}
    entry = load_providers(config)["gemini"]
    assert provider_families(entry) == frozenset({GEMINI_FAMILY})
    # set_default_provider refuses to scope a gemini key to pi.
    block = {"gemini": _key_entry(GEMINI_FAMILY)}
    with pytest.raises(OmnigentError):
        set_default_provider(block, "gemini", PI_SURFACE)


def test_gemini_key_cannot_claim_pi_scope_at_parse() -> None:
    """A hand-edited ``default: ["gemini","pi"]`` / ``"pi"`` on a gemini key
    fails LOUD at parse, not silently at pi launch.

    ``default_provider_for_harness(config, "pi")`` matches on
    ``entry.default_families`` directly, bypassing ``provider_families``. So a
    gemini key's pi scope must be rejected at PARSE time (parity with how a
    subscription claiming pi is rejected) — otherwise a hand-edited config is
    accepted, resolves pi to the gemini key, and only fails when pi launches.
    """
    for bad in (["gemini", "pi"], "pi"):
        raw = {
            "kind": "key",
            "gemini": {"base_url": "https://x/v1", "api_key_ref": "env:K"},
            "default": bad,
        }
        with pytest.raises(OmnigentError):
            load_providers({"providers": {"gemini": raw}})


def test_databricks_does_not_serve_gemini_surface() -> None:
    """Databricks routes anthropic/openai + pi, NOT the Gemini surface.

    The antigravity-native harness drives Gemini via the Google SDK + a
    GEMINI_API_KEY / OAuth, not an OpenAI-compatible gateway, so a databricks
    profile cannot supply the Gemini surface. Were it gemini-capable, a
    ``default: true`` databricks profile would auto-become the gemini-surface
    default and wedge its launch on a credential it cannot use.
    """
    config = {"providers": {"dbx": {"kind": "databricks", "profile": "ws", "default": True}}}
    entry = load_providers(config)["dbx"]
    assert provider_families(entry) == frozenset({ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_SURFACE})
    assert GEMINI_FAMILY not in provider_families(entry)
    # A default databricks profile does NOT become the gemini-surface default.
    assert default_provider_for_harness(config, "antigravity-native") is None
    # And a databricks profile cannot name the gemini scope at parse.
    bad = {"providers": {"dbx": {"kind": "databricks", "profile": "ws", "default": ["gemini"]}}}
    with pytest.raises(OmnigentError):
        load_providers(bad)


@pytest.mark.parametrize("kind", ["gateway", "local"])
def test_gateway_local_does_not_serve_gemini_surface(kind: str) -> None:
    """A gateway/local declaring a ``gemini:`` block does NOT claim the Gemini surface.

    Invariant A: the Gemini surface is consumed by the antigravity flavors
    (the antigravity SDK harness via a raw GEMINI_API_KEY, antigravity-native
    via OAuth), neither of which can be driven by an OpenAI/Anthropic-compatible
    proxy. So a ``gateway`` / ``local`` may carry a gemini block alongside a real
    family but must NOT report ``gemini`` in ``provider_families`` — otherwise it
    could silently become the gemini-surface default and wedge a launch the proxy
    can't honor. Its legitimate anthropic surface is unaffected.
    """
    raw = {
        "kind": kind,
        "anthropic": {"base_url": "https://gw", "api_key_ref": "env:K"},
        "gemini": {"base_url": "https://gw/v1beta", "api_key_ref": "env:G"},
    }
    entry = load_providers({"providers": {"gw": raw}})["gw"]
    served = provider_families(entry)
    assert GEMINI_FAMILY not in served
    # The real (anthropic) surface — and its pi capability — are untouched.
    assert served == frozenset({ANTHROPIC_FAMILY, PI_SURFACE})
    # And it can never become the gemini-surface default…
    cfg = {"providers": {"gw": {**raw, "default": True}}}
    assert default_provider_for_harness(cfg, "antigravity-native") is None
    # …nor name the gemini scope explicitly at parse.
    with pytest.raises(OmnigentError):
        load_providers({"providers": {"gw": {**raw, "default": ["gemini"]}}})


@pytest.mark.parametrize("kind", ["gateway", "local"])
def test_gemini_only_gateway_local_rejected_at_parse(kind: str) -> None:
    """A gateway/local whose ONLY family is gemini fails loud at parse.

    Such an entry configures nothing it can serve (the Gemini surface is
    key-only, and it declares no anthropic/openai family), so parsing it into a
    silently-surfaceless provider would be a footgun. Reject it, steering the
    author to ``kind: 'key'`` for a real GEMINI_API_KEY.
    """
    raw = {"kind": kind, "gemini": {"base_url": "https://x/v1beta", "api_key_ref": "env:G"}}
    with pytest.raises(OmnigentError, match="Gemini surface"):
        load_providers({"providers": {"gw": raw}})


def test_gemini_auth_command_rejected_at_parse() -> None:
    """A ``gemini:`` family with ``auth_command`` (no static key) fails loud at parse.

    The Gemini surface is consumed only by the antigravity harness, which
    drives the google SDK with a STATIC GEMINI_API_KEY. ``auth_command`` mints a
    bearer token the SDK cannot use as a key, so the block is nonsensical.
    Rejecting it at PARSE (rather than only at the runtime spawn / ``/models``
    guard) keeps every layer — provider_families, default-resolution, the
    display+readiness check, spawn, ``/models`` — consistent by construction: an
    auth_command gemini key can no longer leak a false "Gemini ready" into the
    configure-harness readiness path while spawn rejects it.
    """
    raw = {"kind": "key", "gemini": {"base_url": "https://x/v1beta", "auth_command": "echo tok"}}
    with pytest.raises(OmnigentError, match="auth_command is not allowed on a 'gemini' family"):
        load_providers({"providers": {"google": raw}})


def test_auth_command_still_valid_for_non_gemini_families() -> None:
    """``auth_command`` remains valid for anthropic/openai families — no over-restriction.

    The gemini parse-rejection is gemini-SPECIFIC: gateways and dynamic-token
    setups still mint bearers via ``auth_command`` for the anthropic/openai
    surfaces. A regression that rejected ``auth_command`` family-wide would break
    every gateway, so assert these parse cleanly and keep the auth_command source.
    """
    gateway = {
        "kind": "gateway",
        "anthropic": {"base_url": "https://gw", "auth_command": "mint-anthropic-tok"},
        "openai": {"base_url": "https://gw/v1", "auth_command": "mint-openai-tok"},
    }
    entry = load_providers({"providers": {"gw": gateway}})["gw"]
    assert entry.families[ANTHROPIC_FAMILY].auth_command == "mint-anthropic-tok"
    assert entry.families[OPENAI_FAMILY].auth_command == "mint-openai-tok"
    # And a ``key`` provider's openai family may also use auth_command.
    key = {"kind": "key", "openai": {"base_url": "https://x/v1", "auth_command": "mint-tok"}}
    key_entry = load_providers({"providers": {"k": key}})["k"]
    assert key_entry.families[OPENAI_FAMILY].auth_command == "mint-tok"


def test_key_with_gemini_block_still_serves_gemini() -> None:
    """A ``key`` provider with a ``gemini:`` block DOES report the Gemini surface.

    The contrast case to the gateway/local rejection: a real GEMINI_API_KEY is
    exactly what the antigravity harness consumes, so a ``key`` keeps the
    Gemini surface (and only that — gemini is not pi-capable).
    """
    raw = {"kind": "key", "gemini": {"base_url": "https://x/v1beta", "api_key_ref": "env:G"}}
    entry = load_providers({"providers": {"google": raw}})["google"]
    assert provider_families(entry) == frozenset({GEMINI_FAMILY})
    # A multi-family key keeps every served surface, gemini included.
    multi = {
        "kind": "key",
        "openai": {"base_url": "https://x/v1", "api_key_ref": "env:K"},
        "gemini": {"base_url": "https://y/v1beta", "api_key_ref": "env:G"},
    }
    multi_entry = load_providers({"providers": {"multi": multi}})["multi"]
    assert provider_families(multi_entry) == frozenset({OPENAI_FAMILY, GEMINI_FAMILY, PI_SURFACE})


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
        # An inline key/gateway/local serves pi when it declares a pi-capable
        # family (anthropic / openai); databricks routes pi too.
        ({"kind": "key", "openai": {"base_url": "https://x/v1", "api_key_ref": "env:K"}}, True),
        (
            {"kind": "gateway", "anthropic": {"base_url": "https://x", "api_key_ref": "env:K"}},
            True,
        ),
        ({"kind": "databricks", "profile": "my-ws"}, True),
        # Bedrock mode is native-`omnigent claude` only — pi cannot use it.
        (
            {"kind": "bedrock", "anthropic": {"base_url": "https://x", "api_key_ref": "env:K"}},
            False,
        ),
        # A gemini-only inline key serves ONLY the Gemini surface, never pi.
        ({"kind": "key", "gemini": {"base_url": "https://x/v1", "api_key_ref": "env:K"}}, False),
        # A multi-family inline entry keeps pi via its anthropic / openai family.
        (
            {
                "kind": "gateway",
                "anthropic": {"base_url": "https://x", "api_key_ref": "env:K"},
                "gemini": {"base_url": "https://y/v1", "api_key_ref": "env:G"},
            },
            True,
        ),
        (
            {
                "kind": "key",
                "openai": {"base_url": "https://x/v1", "api_key_ref": "env:K"},
                "gemini": {"base_url": "https://y/v1", "api_key_ref": "env:G"},
            },
            True,
        ),
        # A CLI login is unusable outside its own CLI — never pi-capable.
        ({"kind": "subscription", "cli": "claude"}, False),
    ],
)
def test_provider_families_pi_capability(raw: dict[str, object], expect_pi: bool) -> None:
    """``provider_families`` reports the pi scope only for pi-capable providers.

    pi-capable = an inline key/gateway/local declaring an anthropic or openai
    family, or a databricks profile. A gemini-only key (Gemini surface only)
    and a subscription (CLI-bound) are NOT pi-capable. This drives both the Pi
    page's credential list (which rows appear) and set-default validation — a
    regression in either direction lets the menu offer a credential pi can't
    use, or hides one it can.
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
    # A codex cli-config serves the openai surface AND is structurally
    # pi-capable: a Databricks AI Gateway is reusable by Pi (its Anthropic
    # surface), so it can claim the pi scope. (``default: true`` deliberately
    # never expands to pi — only an explicit ``pi`` does — so default_families
    # stays openai-only here.)
    assert provider_families(entry) == frozenset({OPENAI_FAMILY, PI_SURFACE})
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


def test_bedrock_kind_rejected_for_non_native_harnesses() -> None:
    """`kind: bedrock` is native-`omnigent claude` only; in-process harnesses fail loud.

    ``configure_agent_harness_with_provider`` has no Bedrock path — emitting the
    generic ``HARNESS_*_GATEWAY_*`` vars would silently point claude-sdk / pi at
    the Bedrock endpoint as if it were the Anthropic Messages API. Each non-native
    harness must raise rather than mis-configure.
    """
    from omnigent.errors import ErrorCode
    from omnigent.runtime.workflow import configure_agent_harness_with_provider

    entry = load_providers(
        {
            "providers": {
                "b": {
                    "kind": "bedrock",
                    "anthropic": {
                        "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                        "api_key": "k",
                        "models": {"default": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
                    },
                }
            }
        }
    )["b"]
    for harness in ("claude-sdk", "pi"):
        env: dict[str, str] = {}
        with pytest.raises(OmnigentError) as exc:
            configure_agent_harness_with_provider(env, entry, harness_type=harness)
        assert exc.value.code == ErrorCode.INVALID_INPUT
        assert env == {}  # nothing written before the raise


def test_default_provider_for_pi_skips_bedrock_default() -> None:
    """A bedrock Claude default is not handed to pi (native-claude only).

    pi can't drive Bedrock mode (configure_agent_harness_with_provider raises),
    so the unmapped-harness fallback must skip a kind: bedrock default and fall
    through to the next family — otherwise adding a Bedrock Claude default would
    turn a previously-working pi run (its own login) into a hard INVALID_INPUT
    error. The mapped claude-sdk harness still takes the bedrock default (its
    family); the fail-loud there is by design.
    """
    config = {
        "providers": {
            "bedrock": {
                "kind": "bedrock",
                "default": True,
                "anthropic": {
                    "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                    "api_key": "k",
                    "models": {"default": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
                },
            },
            "oai": {
                "kind": "key",
                "default": True,
                "openai": {"base_url": "https://api.openai.com/v1", "api_key": "k"},
            },
        }
    }
    assert default_provider_for_harness(config, "pi").name == "oai"
    assert default_provider_for_harness(config, "claude-sdk").name == "bedrock"


def test_default_provider_for_pi_none_when_only_bedrock_default() -> None:
    """A bedrock-only Claude default leaves pi with no provider (own login).

    With nothing pi can consume, the fallback returns None so pi uses its own
    auth, rather than handing it a bedrock provider that would fail loud.
    """
    config = {
        "providers": {
            "bedrock": {
                "kind": "bedrock",
                "default": True,
                "anthropic": {
                    "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
                    "api_key": "k",
                    "models": {"default": "us.anthropic.claude-haiku-4-5-20251001-v1:0"},
                },
            }
        }
    }
    assert default_provider_for_harness(config, "pi") is None
