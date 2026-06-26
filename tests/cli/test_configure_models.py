"""Tests for ``omnigent setup --no-internal-beta`` (CLI model-provider config).

Drives the click command tree with :class:`click.testing.CliRunner` and
piped stdin, then asserts on the **exact config mutations** written to a
tmp ``~/.omnigent/config.yaml`` (isolated via ``OMNIGENT_CONFIG_HOME``)
and the secret store (forced to the file backend via
``OMNIGENT_DISABLE_KEYRING``). Each test asserts on the persisted YAML
shape, not just the command's exit code, so a regression in the
add/set-default/remove write paths surfaces here rather than silently.

``configure harnesses`` is a **three-level** picker. Level 1 shows every
harness on a single compact row — the name on the left, then an aligned
``✓``/``✗`` status column — in 0.3 priority order: ``1=Claude``,
``2=Codex``, ``3=Cursor``, ``4=OpenCode``, ``5=Hermes``, ``6=Pi``,
``7=Antigravity``, ``8=Qwen Code``, ``9=Goose``, ``10=Copilot``, ``11=Kiro``,
``12=Kimi Code``, ``13=Quit``. There is no "More" folding — every harness is
visible at once — and the actionable hint (install command / next step)
renders only for the highlighted row, as the selector's description line.
Selecting a harness drills into level 2 — its configured credentials, then ``+ Add a
credential`` and ``← Back``. So an empty harness's level 2 is ``1=+Add 2=Back``;
with one credential it's ``1=<credential> 2=+Add 3=Back``. Selecting a
credential opens level 3 — ``Make default`` (only when not already the
default), ``Remove``, and ``← Back``. Going back / exiting is also Esc on a TTY
or ``q`` on the numbered fallback. Under ``CliRunner`` stdin is not a TTY, so
the selector routes through the **numbered fallback** (1-based; ``q`` aborts).
The add menu is scoped to the
harness entered (Claude → Anthropic key / Claude sub / gateway / databricks;
Codex → OpenAI key / ChatGPT sub / OpenRouter key / gateway / other /
databricks). The add flow no longer asks "make default?": a credential
auto-becomes the default for any family with no existing default.

The per-family-default invariant (a Claude default and a Codex default
coexist; selecting a provider sets only its harness's default) is the most
load-bearing behavior — it has dedicated coverage below.
"""

from __future__ import annotations

import os

import pytest
import tomllib
import yaml
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.onboarding import secrets
from omnigent.onboarding.configure_models import (
    add_menu_options,
    add_menu_options_for_family,
    build_bedrock_provider_entry,
    credential_label,
    kind_glyph,
    provider_display_name,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    OPENAI_FAMILY,
    get_default_provider,
    load_config,
    load_providers,
)


@pytest.fixture()
def isolated_config(tmp_path, monkeypatch):
    """Isolate config + secrets to a tmp dir with the file secret backend.

    Sets ``OMNIGENT_CONFIG_HOME`` so config and secrets land under
    *tmp_path*, ``OMNIGENT_DISABLE_KEYRING`` so the secret store uses the
    ``0600`` JSON file (no OS keychain dependency in CI), and clears any
    ambient vendor keys so detection is deterministic.

    :param tmp_path: Pytest temp directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The tmp config-home directory path.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "ANTIGRAVITY_API_KEY",
        "OPENROUTER_API_KEY",
        "DATABRICKS_TOKEN",
        "CURSOR_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    # Redirect CLI-detected credential homes so a developer's real
    # ~/.claude / ~/.codex logins don't leak into ambient detection.
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _harnesses_installed(monkeypatch):
    """Default: pretend the harness CLIs are installed and login/logout succeed.

    The level-2 picker gates on an installed CLI (offering ``npm install`` when
    missing), and adding/removing a *subscription* now drives the harness's own
    login/logout. These tests exercise the *credential* flow, not the real
    interactive vendor commands, so stub ``harness_cli_installed`` → True and
    ``harness_login`` / ``harness_logout`` → True. The install-gate and the
    login/logout behaviors have dedicated tests that override these.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed",
        lambda family: True,
    )
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_login",
        lambda family: True,
    )
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_logout",
        lambda family: True,
    )


def _config_yaml(config_home) -> dict[str, object]:
    """Read and parse the isolated config.yaml.

    :param config_home: The tmp config-home directory.
    :returns: The parsed config mapping, or ``{}`` when no file exists.
    """
    path = os.path.join(config_home, "config.yaml")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def test_configure_models_list_groups_configured_providers(isolated_config) -> None:
    """``omnigent config list`` renders each configured provider grouped by harness.

    Seeds two providers directly, then asserts the listing shows both
    names, their kind words, the Claude/Codex harness groups, and the
    per-family default markers. A failure means the grouped-listing
    renderer dropped a provider or mislabeled a default — the exact thing
    the design's grouped view promises.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    },
                    "openai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                            "models": {"default": "gpt-5.5"},
                        },
                    },
                }
            },
            f,
        )

    result = CliRunner().invoke(cli, ["config", "list"])
    assert result.exit_code == 0, result.output
    # Grouped under each harness family (Claude / Codex); both providers
    # appear with their kind word — proves neither row was dropped.
    assert "Claude" in result.output
    assert "Codex" in result.output
    assert "anthropic" in result.output
    assert "openai" in result.output
    assert result.output.count("key") >= 2
    # anthropic is the Claude default and openai the Codex default, so the
    # default marker appears at least twice. A miss means the default
    # cross-check picked the wrong family or omitted the marker.
    assert result.output.count("✓ default") >= 2


def test_configure_models_add_key_provider_writes_entry_and_secret(isolated_config) -> None:
    """Adding a ``key`` provider writes the family entry + stores the secret.

    Pipes the interactive add flow (flat menu "Anthropic — API key" →
    paste key → pick default model → make default → quit). Asserts the
    exact ``providers:`` entry shape AND that the pasted key reached the
    secret store under ``keychain:anthropic``. A failure here means the add
    flow wrote a malformed entry or lost the secret.
    """
    # L1: 1=Claude → L2 (no credentials): 1=+Add → scoped anthropic add menu
    # 1="Anthropic — API key" → paste key → default model blank (= catalog
    # default) → L2: q=back → L1: q=exit.
    stdin = "\n".join(["1", "1", "1", "sk-ant-test-key", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # The entry is keyed by provider name with kind=key and an anthropic
    # family carrying a keychain ref (never an inline key).
    assert "anthropic" in providers
    entry = providers["anthropic"]
    assert entry["kind"] == "key"
    assert entry["anthropic"]["api_key_ref"] == "keychain:anthropic"
    assert entry["anthropic"]["base_url"] == "https://api.anthropic.com"
    # A default model was picked, so the family carries a models.default.
    assert entry["anthropic"]["models"]["default"]
    # The pasted key is in the secret store under the provider name — the
    # keychain ref above resolves to this value at runtime.
    assert secrets.load_secret("anthropic") == "sk-ant-test-key"

    # The entry round-trips through the parser and is the anthropic default.
    parsed = load_providers(cfg)
    assert parsed["anthropic"].kind == "key"
    assert get_default_provider(cfg, "anthropic").name == "anthropic"


def test_configure_models_add_key_persists_catalog_default_when_declined(
    isolated_config,
) -> None:
    """Declining the model pick still persists a catalog ``models.default``.

    Reproduces the reported persistence bug: a user adds (or re-adds) a
    ``key`` provider and declines "Pick a default model?". Previously the
    entry was written with NO ``models.default`` — so a later re-add silently
    dropped a previously-pinned default. The add flow must instead fall back
    to the bundled catalog's default model for that provider, so an anthropic
    ``key`` provider always carries a real ``models.default``.
    """
    from omnigent.onboarding.providers import default_chat_model

    # L1 1=Claude → L2 1=+Add → anthropic menu 1=Anthropic key → key →
    # default model blank (declined) → L2 q=back → L1 q=exit. Blank model
    # must still persist the catalog default rather than no pin.
    stdin = "\n".join(["1", "1", "1", "sk-ant-test-key", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["anthropic"]
    # Even though the user declined, the family carries the catalog default.
    expected = default_chat_model("anthropic")
    assert expected is not None
    assert entry["anthropic"]["models"]["default"] == expected


def test_configure_models_readd_key_does_not_drop_default(isolated_config) -> None:
    """Re-adding a ``key`` provider never leaves it without a ``models.default``.

    Seeds an anthropic ``key`` provider with a pinned default, then re-adds
    anthropic through the flow while declining the model pick. The re-add
    replaces the whole entry (deep-merge is one level deep), so without the
    catalog fallback the pin would vanish. Asserts the re-added entry still
    carries a (catalog) default rather than dropping ``models`` entirely.
    """
    from omnigent.onboarding.providers import default_chat_model

    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                            "models": {"default": "claude-opus-4-6-20260205"},
                        },
                    }
                }
            },
            f,
        )

    # L1 1=Claude → L2 (1=anthropic 2=+Add): re-add via 2=+Add → anthropic
    # menu 1=Anthropic key → key → blank model → L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "2", "1", "sk-ant-new-key", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["anthropic"]
    # The re-added entry carries a default (the catalog's), not a missing one.
    assert "models" in entry["anthropic"], "re-add dropped the models block"
    assert entry["anthropic"]["models"]["default"] == default_chat_model("anthropic")


def test_configure_models_add_gateway_openrouter_chat_wire(isolated_config) -> None:
    """Adding an OpenRouter gateway records the openai family with `wire_api: chat`.

    The gateway path is the long-tail (OpenRouter / LiteLLM). It asks which
    harness surfaces it serves (Codex/OpenAI, Claude/Anthropic) and — for the
    OpenAI surface — the wire protocol. OpenRouter is Chat-Completions-only,
    so the user picks Chat; persisting `wire_api: chat` is what makes
    OpenRouter actually work (the default Responses API 404s on it). Here the
    user serves only the Codex surface.
    """
    # L1 2=Codex → L2 1=+Add → scoped openai menu 3="Gateway — custom base
    # URL + key" (order: OpenAI key, ChatGPT sub, Gateway, OpenRouter,
    # Databricks, Other) → name; base_url; key; surfaces select 2="Codex /
    # OpenAI only"; wire 2=Chat; default model "qwen/q" → L2 q=back → L1 q=exit.
    stdin = (
        "\n".join(
            [
                "2",
                "1",
                "3",
                "openrouter",
                "https://openrouter.ai/api/v1",
                "sk-or-test",
                "2",
                "2",
                "qwen/q",
                "q",
                "q",
            ]
        )
        + "\n"
    )
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["openrouter"]
    assert entry["kind"] == "gateway"
    # Only the openai surface (Codex-only pick).
    assert "openai" in entry
    assert "anthropic" not in entry
    assert entry["openai"]["base_url"] == "https://openrouter.ai/api/v1"
    assert entry["openai"]["api_key_ref"] == "keychain:openrouter"
    # The wire-protocol pick is persisted — the fix that makes OpenRouter work.
    assert entry["openai"]["wire_api"] == "chat"
    # The gateway's default model is pinned (no catalog default for a gateway).
    assert entry["openai"]["models"]["default"] == "qwen/q"
    assert secrets.load_secret("openrouter") == "sk-or-test"


def test_configure_models_set_default_preserves_other_family(isolated_config) -> None:
    """Setting an openai default leaves the anthropic default intact.

    This is the per-family coexistence invariant. Seed an anthropic
    default (Claude) and a non-default openai key, then make the openai key
    the Codex default by selecting its row in the tree. The anthropic
    default must survive (it serves a different family); the openai key
    becomes the Codex default. A failure means set-default clobbered a
    sibling in another family — the exact regression the wholesale-write
    path guards against.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    },
                    "openai": {
                        "kind": "key",
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                            "models": {"default": "gpt-5.5"},
                        },
                    },
                }
            },
            f,
        )

    # L1 2=Codex → L2 (1=openai 2=+Add): select openai (1) → L3 1=Make default
    # → back to L2 q=back → L1 q=exit.
    stdin = "\n".join(["2", "1", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = load_config()
    # openai is now the Codex (openai-family) default.
    assert get_default_provider(cfg, "openai").name == "openai"
    # anthropic (Claude family) default is UNTOUCHED — different family, so
    # set-default must not have cleared it.
    assert get_default_provider(cfg, "anthropic").name == "anthropic"


def test_configure_models_set_default_replaces_same_family_default(isolated_config) -> None:
    """A new same-family default clears the previous one (≤1 per family).

    Two openai-family keys, one default. Setting the other as default must
    clear the first's flag so exactly one openai default remains —
    otherwise :func:`get_default_provider` would fail loud on the clash.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "openai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                            "models": {"default": "gpt-5.5"},
                        },
                    },
                    "openrouter": {
                        "kind": "gateway",
                        "openai": {
                            "base_url": "https://openrouter.ai/api/v1",
                            "api_key_ref": "keychain:openrouter",
                        },
                    },
                }
            },
            f,
        )

    # Both serve openai. L1 2=Codex → L2 (1=openai 2=openrouter 3=+Add):
    # select openrouter (2) → L3 1=Make default → it replaces openai as the
    # Codex default → back to L2 q=back → L1 q=exit.
    stdin = "\n".join(["2", "2", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = load_config()
    # openrouter is the sole openai default — proves the old openai flag
    # was cleared. If both kept default:true, get_default_provider raises.
    assert get_default_provider(cfg, "openai").name == "openrouter"


def test_configure_models_remove_drops_entry(isolated_config) -> None:
    """Removing a provider drops exactly that entry from the block.

    Seed two providers, remove the first, assert only the second remains.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                        },
                    },
                    "openai": {
                        "kind": "key",
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                        },
                    },
                }
            },
            f,
        )

    # L1 1=Claude → L2 (1=anthropic 2=+Add): select anthropic (1) → L3
    # (1=Make default 2=Remove): 2=Remove → back to L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert "anthropic" not in cfg["providers"]
    assert "openai" in cfg["providers"]


def test_add_menu_options_are_friendly_and_credential_aware() -> None:
    """The add menu shows intuitive provider+credential labels, not raw ids.

    Proves the user-facing fix: choices read like "OpenAI — API key" and
    "Claude — subscription", and each label resolves to the right
    (kind, provider/cli). A failure means a menu entry would show a raw id
    (e.g. "openai") or map to the wrong credential path.
    """
    options = add_menu_options()

    # Every label is emoji-prefixed (first char is the kind glyph, not
    # ASCII) and uses the friendly " — <credential>" form — never a raw id.
    assert all(not o.label[0].isascii() for o in options)
    assert all(" — " in o.label for o in options)

    # The friendly, credential-aware labels are present (the user's
    # examples) — matched as suffixes so the test isn't coupled to the
    # exact emoji glyph.
    assert any(o.label.endswith("OpenAI — API key") for o in options)
    assert any(o.label.endswith("ChatGPT — subscription") for o in options)
    assert any(o.label.endswith("Claude — subscription (Pro/Max)") for o in options)

    # Each resolves to the correct kind + preset provider/cli.
    by_provider = {o.provider: o for o in options if o.provider}
    by_cli = {o.cli: o for o in options if o.cli}
    assert by_provider["openai"].kind == "key"
    assert by_provider["openai"].label.endswith("OpenAI — API key")
    assert by_cli["codex"].kind == "subscription"
    assert by_cli["claude"].kind == "subscription"
    # The catch-all has no preset provider (the user picks one next).
    other = next(o for o in options if o.other)
    assert other.kind == "key" and other.provider is None


def test_add_menu_options_ordering() -> None:
    """The add menu orders: API key → subscription → extras → Databricks → Other.

    Proves the user-requested ordering, in both the full menu and each
    family-scoped subset (the menu actually shown after drilling into a
    harness): the first-party API key(s) and subscription(s) lead, the
    cross-vendor extras follow alphabetically (Gateway before OpenRouter),
    and Databricks sits just above the catch-all "Other". A regression to
    the old interleaved order (or Other above Databricks) fails here.
    """
    # Full menu: first-party keys (OpenAI, Anthropic, Gemini), then
    # subscriptions, then Gateway, OpenRouter, Databricks, Other.
    full = [o.label.split(None, 1)[1] for o in add_menu_options()]
    assert full == [
        "OpenAI — API key",
        "Anthropic — API key",
        "Gemini — API key",
        "ChatGPT — subscription",
        "Claude — subscription (Pro/Max)",
        "Gateway — custom base URL + key (e.g. OpenRouter)",
        "OpenRouter — API key",
        "Databricks — workspace",
        "Other provider — API key",
        # Bedrock is appended last so it never shifts the established order.
        "AWS Bedrock — API key",
    ]

    # Codex (openai) scoped: API key, subscription, Gateway, OpenRouter,
    # Databricks, Other — Databricks immediately above Other.
    codex = [o.label.split(None, 1)[1] for o in add_menu_options_for_family(OPENAI_FAMILY)]
    assert codex == [
        "OpenAI — API key",
        "ChatGPT — subscription",
        "Gateway — custom base URL + key (e.g. OpenRouter)",
        "OpenRouter — API key",
        "Databricks — workspace",
        "Other provider — API key",
    ]
    assert codex.index("Databricks — workspace") < codex.index("Other provider — API key")

    # Claude (anthropic) scoped: API key, subscription, Gateway, Databricks
    # (no OpenRouter / Other — those are openai-family).
    claude = [o.label.split(None, 1)[1] for o in add_menu_options_for_family(ANTHROPIC_FAMILY)]
    assert claude == [
        "Anthropic — API key",
        "Claude — subscription (Pro/Max)",
        "Gateway — custom base URL + key (e.g. OpenRouter)",
        "Databricks — workspace",
        "AWS Bedrock — API key",
    ]

    # Gemini (antigravity) scoped: API key only — Gemini is key-only (no
    # subscription/gateway/Databricks), and it must NOT appear in the
    # openai-family "Other provider" catch-all (asserted via `codex` above).
    gemini = [o.label.split(None, 1)[1] for o in add_menu_options_for_family(GEMINI_FAMILY)]
    assert gemini == ["Gemini — API key"]


def test_add_menu_databricks_option_gated_on_extra(monkeypatch) -> None:
    """The Databricks option stays visible without the SDK, with the hint.

    The `databricks` extra (databricks-sdk) is no longer a default
    dependency, so the add menu gates the Databricks flow on it: the option
    is never hidden (discoverability), but its description switches from
    the routing explanation to the install hint when the SDK is absent.
    A failure means a bare-OSS user either loses the option entirely or
    sees the routing description for a flow that would abort on selection.
    """
    # Patch the symbol configure_models bound at import — the menu builder
    # calls this exact name, so the patch deterministically simulates a
    # bare install without touching the process-wide importlib machinery.
    monkeypatch.setattr(
        "omnigent.onboarding.configure_models.databricks_sdk_installed",
        lambda: False,
    )
    options = add_menu_options()
    databricks = next(o for o in options if o.label.endswith("Databricks — workspace"))
    # The label (and thus menu presence/ordering) is unchanged; only the
    # description carries the gate.
    assert databricks.kind == "databricks"
    assert databricks.description == (
        "Requires the Databricks extra — select for the install command."
    )

    # With the SDK present (the dev/CI env — no patch), the description
    # explains the routing instead of demanding an install.
    monkeypatch.undo()
    options = add_menu_options()
    databricks = next(o for o in options if o.label.endswith("Databricks — workspace"))
    assert "Unity AI Gateway" in databricks.description


def test_configure_models_add_databricks_aborts_without_extra(
    isolated_config, monkeypatch
) -> None:
    """Selecting Databricks without the SDK aborts before any side effect.

    Drives the real add flow (Claude → +Add → Databricks) with the SDK
    absent. The gate must return to the menu without prompting for a
    workspace URL, running `databricks auth login`, `ucode configure`, or
    writing a provider entry. A failure here (non-zero exit via the raising
    login stub, or a written provider) means the gate ran after a side
    effect — exactly the bug it exists to prevent: signing the user into a
    workspace that routing then can't use.
    """
    # cli.py's databricks branch resolves databricks_sdk_installed from the
    # source module at call time, so patching the module attribute is seen.
    monkeypatch.setattr(
        "omnigent.onboarding.databricks_config.databricks_sdk_installed",
        lambda: False,
    )

    def _login_must_not_run(*args: object, **kwargs: object) -> str:
        """Stub that fails the test if the Databricks login is reached."""
        raise AssertionError(
            "login_databricks_workspace ran despite the missing databricks "
            "extra — the gate must abort before the browser login."
        )

    monkeypatch.setattr(
        "omnigent.onboarding.setup.login_databricks_workspace",
        _login_must_not_run,
    )

    # L1: 1=Claude → L2 (empty): 1=+Add → Claude-scoped menu: 4=Databricks
    # (key, subscription, gateway, then Databricks) → gate aborts back to
    # L2: q=back → L1: q=exit. If the gate were broken, the next stdin line
    # ("q") would be consumed as the workspace URL and the login stub would
    # raise, failing the invoke with a non-zero exit code.
    stdin = "\n".join(["1", "1", "4", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    # No provider entry was persisted — the add aborted cleanly.
    cfg = _config_yaml(isolated_config)
    assert cfg.get("providers", {}) == {}


@pytest.mark.parametrize(
    "kind,name,profile,expected",
    [
        ("subscription", "claude-subscription", None, "Subscription"),
        ("subscription", "codex-subscription", None, "Subscription"),
        ("key", "anthropic", None, "Anthropic API Key"),
        ("key", "openai", None, "OpenAI API Key"),
        ("databricks", "databricks", "oss", "Databricks (oss)"),
        ("databricks", "databricks", None, "Databricks"),
        ("gateway", "my-proxy", None, "My-Proxy"),  # display-name fallback
    ],
)
def test_credential_label_by_kind(
    kind: str, name: str, profile: str | None, expected: str
) -> None:
    """The shared credential label names each kind consistently.

    Proves the single source of truth used by BOTH ``configure harnesses``
    and the ``/model`` readout: a subscription is always "Subscription"
    (never the brand/cli name), a vendor key is "<Vendor> API Key", and
    Databricks names its profile. A drift here would make the two surfaces
    disagree — the exact inconsistency the shared helper was added to fix.
    """
    assert credential_label(kind, name, profile=profile) == expected


@pytest.mark.parametrize("kind", ["key", "subscription", "gateway", "databricks", "local"])
def test_kind_glyph_uniform_display_width(kind: str) -> None:
    """Every kind glyph renders at a uniform 2-cell width on modern terminals.

    Proves the "ticket looks cramped" fix comes from the subscription
    glyph's VARIATION SELECTOR-16 (which makes ADMISSION TICKETS a 2-cell
    emoji like 🔑 / 🌐 / 🧱), not ad-hoc padding. Width is measured via the
    banner box's own ``_display_width`` (rich >= 14 ``cell_len``, which counts
    a VS16-forced wide emoji as the two cells terminals render). A regression
    that dropped the VS16 (or a glyph) yields width != 2.
    """
    from omnigent.inner.banner import _display_width

    g = kind_glyph(kind)
    width = _display_width(g)
    assert width == 2, f"glyph for {kind!r} should be 2 display cells; got {width} ({g!r})."


@pytest.mark.parametrize(
    "provider,expected",
    [
        ("openai", "OpenAI"),
        ("openrouter", "OpenRouter"),
        ("xai", "xAI"),
        ("together_ai", "Together AI"),
        ("some-custom-proxy", "Some-Custom-Proxy"),  # fallback: title-cased
    ],
)
def test_provider_display_name_friendly(provider: str, expected: str) -> None:
    """Provider ids render as human names; unknown ids fall back title-cased.

    A failure means a menu/prompt would show the raw id (e.g. "xai" instead
    of "xAI").
    """
    assert provider_display_name(provider) == expected


def test_configure_models_add_subscription_via_flat_menu(isolated_config) -> None:
    """Picking "Claude — subscription" adds a subscription provider (cli=claude).

    Exercises the flat menu's subscription path end-to-end (the user's
    "OpenAI Subscription"-style option): no kind/cli sub-pick, just the
    one intuitive choice. A failure means the subscription option didn't
    preset the CLI or wrote the wrong entry.
    """
    # L1 1=Claude → L2 1=+Add → scoped anthropic menu 2="Claude —
    # subscription (Pro/Max)" → harness login (stubbed True by the autouse
    # fixture) → name derived "<cli>-subscription", auto-default → L2 q → L1 q.
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["claude-subscription"]
    # A subscription entry routes through the claude CLI login (no families).
    assert entry["kind"] == "subscription"
    assert entry["cli"] == "claude"
    # claude serves the anthropic surface, so it became the Claude default.
    assert get_default_provider(cfg, "anthropic").name == "claude-subscription"


def test_add_subscription_invokes_harness_login(isolated_config, monkeypatch) -> None:
    """Adding a subscription drives the harness's own login before recording.

    Proves "configure is the single place to sign in": picking "Claude —
    subscription" calls ``harness_login("anthropic")``. A failure means the menu
    recorded a subscription without ever logging the user in (the original bug).
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_login",
        lambda family: calls.append(family) or True,
    )
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"  # Claude → +Add → subscription
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    # Logged into Claude (anthropic) exactly once, before recording the entry.
    assert calls == ["anthropic"]
    cfg = _config_yaml(isolated_config)
    assert cfg["providers"]["claude-subscription"]["kind"] == "subscription"


def test_add_subscription_aborts_when_login_fails(isolated_config, monkeypatch) -> None:
    """A login that doesn't complete records NO subscription (no phantom).

    If ``harness_login`` returns False (user cancelled / OAuth failed), the add
    must not persist a subscription entry — otherwise routing would later strand
    the user at the harness's own login screen, exactly what we're fixing.
    """
    monkeypatch.setattr("omnigent.onboarding.harness_install.harness_login", lambda family: False)
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"  # Claude → +Add → subscription
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    # Nothing persisted — the failed login aborted the add.
    assert "claude-subscription" not in cfg.get("providers", {})


def test_remove_subscription_signs_out_and_removes(isolated_config, monkeypatch) -> None:
    """Removing a subscription runs the harness logout AND drops the entry.

    A subscription's credential lives in the harness CLI's own auth file, so a
    bare entry-delete wouldn't sign the user out (and would be re-detected). A
    failure means logout wasn't invoked or the entry wasn't removed.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {"providers": {"claude-subscription": {"kind": "subscription", "cli": "claude"}}}, f
        )
    calls: list[str] = []
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_logout",
        lambda family: calls.append(family) or True,
    )
    # L1 1=Claude → L2 1=select the subscription → L3 2=Remove → confirm 1=Yes
    # (sign out + remove) → L2 q → L1 q.
    stdin = "\n".join(["1", "1", "2", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    # Signed out of Claude (anthropic) …
    assert calls == ["anthropic"]
    cfg = _config_yaml(isolated_config)
    # … and the entry is gone.
    assert "claude-subscription" not in cfg.get("providers", {})


def test_remove_subscription_declined_keeps_it_and_login(isolated_config, monkeypatch) -> None:
    """Declining the remove-confirm leaves the entry AND never logs out.

    The confirm defaults to "No"; choosing it must be a true no-op — no logout
    (which would sign the user out of the standalone CLI) and no entry change.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {"providers": {"claude-subscription": {"kind": "subscription", "cli": "claude"}}}, f
        )

    def _no_logout(family: str) -> bool:
        raise AssertionError("harness_logout called despite the user declining removal")

    monkeypatch.setattr("omnigent.onboarding.harness_install.harness_logout", _no_logout)
    # L1 1=Claude → L2 1=select → L3 2=Remove → confirm 2=No → L2 q → L1 q.
    stdin = "\n".join(["1", "1", "2", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    # The subscription survived the declined removal.
    assert "claude-subscription" in cfg["providers"]


def _write_databricks_provider(config_home) -> None:
    """Persist a ``kind: databricks`` provider entry to the isolated config.

    :param config_home: The tmp config-home directory (``isolated_config``).
    """
    config_path = os.path.join(config_home, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"providers": {"databricks": {"kind": "databricks", "profile": "myws"}}}, f)


def test_remove_databricks_cleans_ucode_wiring_without_asking(isolated_config) -> None:
    """Remove on a databricks provider strips ucode's wiring as part of removal.

    A databricks provider was wired by `ucode configure`, which (for codex
    < 0.134.0) edits the user's real ~/.codex/config.toml — so a bare
    entry-delete would leave codex routing through the workspace gateway.
    Cleanup is the removal's expected behavior, so there is NO extra confirm:
    the stdin below carries no confirm digit, and an unexpected prompt would
    consume the trailing ``q``s and leave the entry in place (failing the
    config assertion). Exercises the real cleanup against files under the
    isolated tmp HOME — no stubs — so it also proves the default-path
    resolution (``~/.codex/...``) and the user-key preservation.
    """
    _write_databricks_provider(isolated_config)
    codex_dir = isolated_config / ".codex"
    codex_dir.mkdir()
    # The exact shape ucode's legacy layout leaves behind, merged into a
    # user-owned key that must survive.
    (codex_dir / "config.toml").write_text(
        'model = "gpt-5.4"\n'
        'profile = "ucode"\n'
        "\n"
        "[profiles.ucode]\n"
        'model_provider = "ucode-databricks"\n'
        "\n"
        "[model_providers.ucode-databricks]\n"
        'base_url = "https://example.databricks.com/ai-gateway/codex/v1"\n',
        encoding="utf-8",
    )
    (codex_dir / "ucode.config.toml").write_text(
        'model_provider = "ucode-databricks"\n', encoding="utf-8"
    )
    # L1 1=Claude → L2 1=select databricks → L3 2=Remove (acts immediately)
    # → L2 q → L1 q.
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    # The provider entry is gone from config.yaml.
    assert "databricks" not in cfg.get("providers", {})
    doc = tomllib.loads((codex_dir / "config.toml").read_text(encoding="utf-8"))
    # The invasive selector was stripped — bare codex no longer routes
    # through the workspace. If present, Remove never invoked the cleanup.
    assert "profile" not in doc
    assert "profiles" not in doc
    assert "model_providers" not in doc
    # The user's own key survived the strip with its exact value.
    assert doc["model"] == "gpt-5.4"
    # ucode's sidecar is deleted too.
    assert not (codex_dir / "ucode.config.toml").exists()


def test_remove_databricks_without_ucode_wiring_still_removes(isolated_config) -> None:
    """Remove works when no ucode wiring exists on the machine.

    The cleanup steps must all no-op gracefully (missing ~/.codex,
    ~/.claude.json, sidecars) rather than erroring and blocking the
    entry removal.
    """
    _write_databricks_provider(isolated_config)
    # L1 1=Claude → L2 1=select databricks → L3 2=Remove → L2 q → L1 q.
    stdin = "\n".join(["1", "1", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    # The provider entry is gone despite there being nothing to clean.
    assert "databricks" not in cfg.get("providers", {})


def test_render_listing_excludes_configured_subscription_clis(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A detected CLI login isn't shown as "not configured" once its subscription is added.

    Regression: a ``subscription`` provider is named e.g. ``claude-subscription``,
    so the listing's "Detected (not configured)" filter — which compared the
    detected CLI name (``"claude"``) against provider *names* — missed it,
    and the login kept showing as not-configured even after the user added
    it. The CLI must be excluded once a subscription wraps it, while an
    unrelated detection still shows.
    """
    from omnigent.onboarding.ambient import DetectedProvider
    from omnigent.onboarding.configure_models import render_provider_listing
    from omnigent.onboarding.provider_config import load_providers

    config: dict[str, object] = {
        "providers": {"claude-subscription": {"kind": "subscription", "cli": "claude"}}
    }
    providers = load_providers(config)
    detected = [
        DetectedProvider(
            name="claude", kind="subscription", family="anthropic", source="claude CLI login"
        ),
        DetectedProvider(name="gemini", kind="key", family="openai", source="$GEMINI_API_KEY"),
    ]

    render_provider_listing(config, providers, detected)
    out = capsys.readouterr().out

    # The configured subscription is listed…
    assert "claude-subscription" in out
    # …and its wrapped CLI is NOT offered under "Detected (not configured)"…
    assert "claude CLI login" not in out
    # …while an unrelated ambient detection still surfaces as a hint.
    assert "Detected (not configured)" in out
    assert "gemini" in out


def _seed_config(config_home, providers: dict[str, object]) -> None:
    """Write a ``providers:`` block to the isolated config.yaml.

    :param config_home: The tmp config-home directory (from ``isolated_config``).
    :param providers: The raw ``providers`` mapping to persist, e.g.
        ``{"claude": {"kind": "subscription", "cli": "claude"}}``.
    """
    with open(os.path.join(config_home, "config.yaml"), "w") as f:
        yaml.safe_dump({"providers": providers}, f)


def test_add_subscription_replaces_existing_for_same_cli(isolated_config) -> None:
    """Adding a subscription when one already exists replaces it (one per harness).

    Seeds a Claude subscription under the ambient-adopted name ``"claude"``
    (the shape that produced the ``claude`` + ``claude-subscription``
    duplicate), then adds "Claude — subscription" and chooses "Replace it".
    The old entry is dropped and only the canonical ``claude-subscription``
    remains, still the anthropic default — so a harness never accumulates two
    subscriptions for one CLI login.
    """
    _seed_config(
        isolated_config,
        {"claude": {"kind": "subscription", "default": True, "cli": "claude"}},
    )
    # L1 1=Claude → L2 2=+Add → anthropic menu 2=Claude subscription → replace
    # prompt 1=Replace it → L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "2", "2", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # The duplicate ``claude`` entry is gone; only the canonical name remains.
    assert "claude" not in providers
    assert providers["claude-subscription"]["kind"] == "subscription"
    assert providers["claude-subscription"]["cli"] == "claude"
    # The replacement re-claimed the Claude default the old entry held.
    assert get_default_provider(cfg, "anthropic").name == "claude-subscription"


def test_add_subscription_keep_current_aborts(isolated_config) -> None:
    """Declining the replace prompt leaves the existing subscription untouched.

    Choosing "Keep the current one" aborts the add — no second subscription is
    written and the original entry (name and default) is preserved.
    """
    _seed_config(
        isolated_config,
        {"claude": {"kind": "subscription", "default": True, "cli": "claude"}},
    )
    # L1 1=Claude → L2 2=+Add → anthropic menu 2=Claude subscription → replace
    # prompt 2=Keep the current one (abort) → L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "2", "2", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # No canonical entry was added; the original is still the only subscription.
    assert "claude-subscription" not in providers
    assert providers["claude"]["kind"] == "subscription"
    assert get_default_provider(cfg, "anthropic").name == "claude"


def test_add_second_key_different_source_keeps_both(isolated_config) -> None:
    """A second API key from a new source coexists with the first.

    Seeds an anthropic ``key`` backed by ``env:ANTHROPIC_API_KEY``, then adds
    another Anthropic key by pasting one (a different source: a ``keychain:``
    ref). The paste must NOT overwrite the env-backed entry — it gets a fresh
    name (``anthropic-2``) so both keys are kept ("allow multiple API keys").
    """
    _seed_config(
        isolated_config,
        {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key_ref": "env:ANTHROPIC_API_KEY",
                },
            }
        },
    )
    # ANTHROPIC_API_KEY is cleared by isolated_config, so detection offers no
    # env reuse — the add takes the paste path. L1 1=Claude → L2 2=+Add →
    # anthropic menu 1=Anthropic key → paste key → blank model → q, q.
    stdin = "\n".join(["1", "2", "1", "sk-ant-second", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # Both keys survive — the env-backed original and the pasted second.
    assert providers["anthropic"]["anthropic"]["api_key_ref"] == "env:ANTHROPIC_API_KEY"
    assert providers["anthropic-2"]["kind"] == "key"
    assert providers["anthropic-2"]["anthropic"]["api_key_ref"] == "keychain:anthropic-2"
    assert secrets.load_secret("anthropic-2") == "sk-ant-second"


def test_readd_same_source_key_updates_in_place(isolated_config, monkeypatch) -> None:
    """Re-adding a key from the same source updates it, never duplicates it.

    Seeds an anthropic ``key`` backed by ``env:ANTHROPIC_API_KEY`` and makes
    the variable present, so the add flow offers to reuse it. Accepting the
    detected env var yields the same source as the existing entry, so it is
    updated in place — no ``anthropic-2`` is created (re-adding the key you
    already have is idempotent).
    """
    _seed_config(
        isolated_config,
        {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key_ref": "env:ANTHROPIC_API_KEY",
                },
            }
        },
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-env")  # so detection offers reuse
    # L1 1=Claude → L2 2=+Add → anthropic menu 1=Anthropic key → "y" reuse the
    # detected env var → blank model → q, q.
    stdin = "\n".join(["1", "2", "1", "y", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # Same source → in-place update, so there is still exactly one anthropic key.
    assert "anthropic-2" not in providers
    assert providers["anthropic"]["anthropic"]["api_key_ref"] == "env:ANTHROPIC_API_KEY"


def test_multiple_keys_show_source_bracket(isolated_config) -> None:
    """When >1 API key serves a harness, each row is qualified with its source.

    Two anthropic keys would otherwise both read as "Anthropic API Key". The
    manager appends the source hint — ``$ENV_VAR`` for an env ref, the stored
    name for a keychain ref — so the rows are distinguishable. A lone key has
    no qualifier (covered implicitly by the other add tests).
    """
    _seed_config(
        isolated_config,
        {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key_ref": "env:ANTHROPIC_API_KEY",
                },
            },
            "anthropic-prod": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://api.anthropic.com",
                    "api_key_ref": "keychain:anthropic-prod",
                },
            },
        },
    )
    # L1 1=Claude renders the level-2 credential rows; q=back, q=exit.
    stdin = "\n".join(["1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    # Both source hints render, so the two keys are told apart.
    assert "$ANTHROPIC_API_KEY" in result.output
    assert "anthropic-prod" in result.output


def test_configure_models_add_other_provider_prompts_for_name(
    isolated_config, monkeypatch
) -> None:
    """The "Other provider" path is the one key case that still prompts for a name.

    Per the UX rule (only gateway / "other" prompt for a name; presets,
    subscriptions, and databricks derive theirs), adding via "Other provider
    — API key" lets the user name the entry — useful for a custom name or
    two configs for the same vendor. The pasted key is stored under that
    chosen name (``keychain:<name>``), not the catalog id, so custom names
    don't collide. A failure means the "other" path stopped prompting or
    keyed the secret by the wrong identifier.
    """
    # The first "other" provider is xai (an openai-family vendor); clear its
    # env var so detection doesn't add a "use the detected key?" prompt.
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # "Other" is openai-family, so it lives in the Codex add menu. L1 2=Codex
    # → L2 1=+Add → openai menu 6="Other provider — API key" (order: OpenAI
    # key, ChatGPT sub, Gateway, OpenRouter, Databricks, Other) → which
    # provider → xAI(1) → NAME "my-xai" → key → default model blank → L2
    # q=back → L1 q=exit.
    stdin = "\n".join(["2", "1", "6", "1", "my-xai", "sk-xai-test", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    providers = cfg["providers"]
    # Entry is keyed by the user-chosen NAME (not the catalog id "xai")…
    assert "my-xai" in providers
    assert "xai" not in providers
    entry = providers["my-xai"]
    assert entry["kind"] == "key"
    # …and the secret ref + store are keyed by that same custom name.
    assert entry["openai"]["api_key_ref"] == "keychain:my-xai"
    assert secrets.load_secret("my-xai") == "sk-xai-test"


def test_configure_models_add_key_free_form_model(isolated_config) -> None:
    """The default-model prompt is free-form: a model NOT in the catalog persists.

    Regression for the "default models are outdated" feedback — the bundled
    catalog lags new releases, so the prompt must accept any typed model id
    (not just a fixed picker). Typing a brand-new id that the catalog does
    not know must be written verbatim as `models.default`.
    """
    novel = "claude-sonnet-9-9-21001231"  # deliberately not in the catalog
    # L1 1=Claude → L2 1=+Add → anthropic menu 1=Anthropic key → key →
    # default model = <novel> (typed, not from catalog) → L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "1", "1", "sk-ant-test-key", novel, "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["providers"]["anthropic"]["anthropic"]["models"]["default"] == novel


def test_configure_models_add_gateway_serves_both_harnesses(isolated_config) -> None:
    """A gateway can serve BOTH harness surfaces (Codex + Claude) in one add.

    Per the feedback that a gateway / custom endpoint (LiteLLM-style) should
    be usable from both the claude-sdk and codex/openai-agents harnesses, the
    add flow asks for each surface (defaulting to both). Accepting both writes
    an `openai` AND an `anthropic` family block pointing at the same base_url.
    """
    # Enter via Claude. L1 1=Claude → L2 1=+Add → anthropic menu 3=Gateway →
    # name; base_url; key; surfaces 1="Both Claude and Codex"; wire
    # 1=Responses; default model for the OpenAI surface ("gpt-ll") then the
    # Claude surface ("claude-ll") → L2 q=back → L1 q=exit.
    stdin = (
        "\n".join(
            [
                "1",
                "1",
                "3",
                "litellm",
                "https://litellm.example/v1",
                "sk-ll",
                "1",
                "1",
                "gpt-ll",
                "claude-ll",
                "q",
                "q",
            ]
        )
        + "\n"
    )
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    entry = _config_yaml(isolated_config)["providers"]["litellm"]
    assert entry["kind"] == "gateway"
    # Both surfaces present, same base_url — usable from claude-sdk AND codex.
    assert entry["openai"]["base_url"] == "https://litellm.example/v1"
    assert entry["anthropic"]["base_url"] == "https://litellm.example/v1"
    # Responses wire was chosen for the openai surface.
    assert entry["openai"].get("wire_api") == "responses"
    # Per-surface default models are pinned (a gateway has no catalog default).
    assert entry["openai"]["models"]["default"] == "gpt-ll"
    assert entry["anthropic"]["models"]["default"] == "claude-ll"


def test_configure_models_add_openrouter_key_uses_vendor_endpoint_and_chat_wire(
    isolated_config, monkeypatch
) -> None:
    """Adding the OpenRouter *key* preset points at openrouter.ai + Chat wire.

    Root cause of "OpenRouter doesn't work (LiteLLM does)": a third-party
    OpenAI-compatible key got the openai-*family* default base_url
    (api.openai.com) and no wire override, so requests went to OpenAI with an
    OpenRouter key on the Responses API. The key add now uses the vendor's own
    endpoint (openrouter.ai) and `wire_api: chat`.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # OpenRouter key is openai-family → Codex add menu. L1 2=Codex → L2
    # 1=+Add → openai menu 4="OpenRouter — API key" (order: OpenAI key,
    # ChatGPT sub, Gateway, OpenRouter, Databricks, Other) → key → default
    # model blank → L2 q=back → L1 q=exit.
    stdin = "\n".join(["2", "1", "4", "sk-or-test", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    entry = _config_yaml(isolated_config)["providers"]["openrouter"]["openai"]
    assert entry["base_url"] == "https://openrouter.ai/api/v1"  # NOT api.openai.com
    assert entry["wire_api"] == "chat"  # OpenRouter is Chat-Completions-only


def test_promote_global_auth_backfills_databricks_for_existing_configs(isolated_config) -> None:
    """An old ``auth:``-only config self-heals into a databricks provider.

    Before this PR, ``setup`` wrote only the global ``auth: {type: databricks}``
    block, invisible to ``configure harnesses``. Existing users would still see no
    databricks even after upgrading — unless they re-ran ``setup``.
    ``_promote_global_auth_to_provider`` backfills that block into a first-class
    ``kind: databricks`` providers entry on the next ``configure harnesses`` open,
    defaulting both families (the config only ever had the auth: block, so
    routing already used databricks for both).
    """
    from omnigent.cli import _promote_global_auth_to_provider, _save_global_config

    _save_global_config({"auth": {"type": "databricks", "profile": "oss"}})

    assert _promote_global_auth_to_provider() == "databricks"

    cfg = load_config()
    providers = load_providers(cfg)
    assert "databricks" in providers
    assert providers["databricks"].profile == "oss"
    assert get_default_provider(cfg, "anthropic").name == "databricks"
    assert get_default_provider(cfg, "openai").name == "databricks"

    # Idempotent: a databricks provider already exists, so re-opening configure
    # does not double-promote (would otherwise churn the config every open).
    assert _promote_global_auth_to_provider() is None


def test_promote_global_auth_respects_explicit_default(isolated_config) -> None:
    """Promotion mirrors routing precedence: an explicit provider default wins.

    Routing puts an explicit ``providers:`` default ahead of the ``auth:`` block,
    so when a family already has a provider default, the backfilled databricks
    must NOT steal it — it only claims families with no existing default. Here
    an explicit anthropic key default is kept while databricks takes openai.
    """
    from omnigent.cli import _promote_global_auth_to_provider, _save_global_config

    _save_global_config(
        {
            "auth": {"type": "databricks", "profile": "oss"},
            "providers": {
                "anthropic": {
                    "kind": "key",
                    "default": True,
                    "anthropic": {
                        "base_url": "https://api.anthropic.com",
                        "api_key_ref": "keychain:anthropic",
                        "models": {"default": "claude-opus-4-8"},
                    },
                },
            },
        }
    )

    assert _promote_global_auth_to_provider() == "databricks"

    cfg = load_config()
    # Explicit anthropic default untouched; databricks only took the open
    # (openai) family — exactly what routing would resolve.
    assert get_default_provider(cfg, "anthropic").name == "anthropic"
    assert get_default_provider(cfg, "openai").name == "databricks"


def test_promote_global_auth_noop_without_databricks_auth(isolated_config) -> None:
    """No databricks ``auth:`` block → nothing to backfill (returns None)."""
    from omnigent.cli import _promote_global_auth_to_provider, _save_global_config

    # An api_key auth block (not databricks) must not synthesize a databricks
    # provider, and a config with no auth: block at all is a clean no-op.
    _save_global_config({"auth": {"type": "api_key", "api_key": "sk-x"}})
    assert _promote_global_auth_to_provider() is None
    assert "databricks" not in load_providers(load_config())


def _databricks_add_menu_index() -> int:
    """Return the 1-based numbered-fallback position of the Databricks option.

    Computed from the live Claude-scoped add menu rather than hardcoded, so a
    future reordering of :func:`add_menu_options` doesn't silently point this
    test's piped stdin at the wrong row.

    :returns: The 1-based index of the ``databricks``-kind option within the
        Claude (anthropic) add menu, e.g. ``4``.
    """
    from omnigent.onboarding.configure_models import add_menu_options_for_family
    from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, DATABRICKS_KIND

    opts = add_menu_options_for_family(ANTHROPIC_FAMILY)
    return next(i for i, o in enumerate(opts) if o.kind == DATABRICKS_KIND) + 1


def test_configure_harnesses_add_databricks_normalizes_url_and_persists(
    isolated_config, monkeypatch
) -> None:
    """The Databricks add branch normalizes the workspace URL, logs in + runs
    ucode against that same URL, and persists a default ``databricks`` provider.

    Drives the real ``_configure_harness_add`` databricks branch through the CLI
    (numbered fallback), stubbing only the two boundary helpers that shell out
    (``login_databricks_workspace`` → returns a profile; ``configure_ucode_for_workspace``)
    and ``ucode_workspace_exists`` (→ True). Asserts the user-entered
    ``"example.cloud.databricks.com/"`` (no scheme, trailing slash) is normalized to
    ``"https://example.cloud.databricks.com"`` for BOTH the login and the ucode call,
    and that ``providers.databricks`` is written as the default. Added under the
    Claude harness, so ucode is scoped to ``--agents claude`` (NOT codex/pi) and
    the provider defaults only the Claude (anthropic) family. A regression in URL
    handling, the per-harness scoping, or persistence surfaces here.
    """
    login_calls: list[str] = []
    ucode_calls: list[tuple[str, list[str] | None]] = []
    exists_calls: list[str] = []

    def _fake_login(url: str, *, console: object | None = None) -> str:
        login_calls.append(url)
        return "my-ws"

    def _fake_configure_ucode(url: str, *, agents: list[str] | None = None) -> None:
        ucode_calls.append((url, agents))

    def _fake_exists(url: str) -> bool:
        exists_calls.append(url)
        return True

    # Patch at the source modules — the databricks branch imports these at call
    # time, so the attribute lookup resolves to these stubs.
    monkeypatch.setattr("omnigent.onboarding.setup.login_databricks_workspace", _fake_login)
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup.configure_ucode_for_workspace", _fake_configure_ucode
    )
    monkeypatch.setattr("omnigent.onboarding.ucode_setup.ucode_workspace_exists", _fake_exists)

    db = _databricks_add_menu_index()
    # L1 1=Claude → L2 1=+Add → add menu <db>=Databricks → workspace URL (no
    # scheme + trailing slash, to exercise normalization) → L2 q=back → L1 q=exit.
    stdin = "\n".join(["1", "1", str(db), "example.cloud.databricks.com/", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    normalized = "https://example.cloud.databricks.com"
    # Login + ucode each ran exactly once, against the normalized URL — if the
    # branch dropped the scheme-prefixing or trailing-slash strip, these fail.
    assert login_calls == [normalized]
    assert exists_calls == [normalized]
    # ucode is scoped to the drilled-in harness only: added under Claude →
    # `--agents claude`, NOT the legacy claude,codex,pi. A regression to the
    # hardcoded agent set (configuring/installing harnesses the user didn't
    # pick) changes this and fails here.
    assert ucode_calls == [(normalized, ["claude"])]

    cfg = _config_yaml(isolated_config)
    # Persisted as a kind=databricks provider keyed on the returned profile, and
    # made the default for ONLY the Claude (anthropic) family it was added under
    # — `default: "anthropic"`, not `True` (which would claim both families).
    assert cfg["providers"]["databricks"] == {
        "kind": "databricks",
        "profile": "my-ws",
        "default": "anthropic",
    }
    assert get_default_provider(cfg, "anthropic").name == "databricks"
    # Codex was NOT configured in ucode, so it must NOT be defaulted to
    # Databricks (that would route Codex through a workspace ucode never set up
    # for it). It stays unset here — add Databricks under Codex to wire it.
    assert get_default_provider(cfg, "openai") is None


def test_configure_harnesses_add_databricks_fails_loud_when_ucode_records_no_state(
    isolated_config, monkeypatch
) -> None:
    """If ``ucode configure`` records no state for the workspace, the add aborts
    loudly and persists NO databricks provider.

    Guards against a half-configured provider: routing would otherwise silently
    fall back. With ``ucode_workspace_exists`` → False the branch must raise, so
    the command exits non-zero and ``providers`` stays empty.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.setup.login_databricks_workspace",
        lambda url, *, console=None: "my-ws",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup.configure_ucode_for_workspace",
        lambda url, *, agents=None: None,
    )
    # ucode "succeeded" but left no state for this workspace.
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup.ucode_workspace_exists", lambda url: False
    )

    db = _databricks_add_menu_index()
    stdin = "\n".join(["1", "1", str(db), "https://example.cloud.databricks.com", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)

    # The branch raised ClickException → non-zero exit with an explanatory message.
    assert result.exit_code != 0
    assert "recorded no state" in result.output
    # Nothing was persisted — no half-configured databricks provider.
    cfg = _config_yaml(isolated_config)
    assert "databricks" not in cfg.get("providers", {})


def test_configure_harnesses_add_databricks_under_codex_scopes_to_codex(
    isolated_config, monkeypatch
) -> None:
    """Adding Databricks under the Codex harness scopes ucode to ``--agents codex``
    and defaults only the Codex (openai) family.

    The mirror of the Claude-path test: the per-harness scoping must follow
    whichever harness the user drilled into, so the Claude family is left
    untouched here.
    """
    from omnigent.onboarding.configure_models import add_menu_options_for_family
    from omnigent.onboarding.provider_config import DATABRICKS_KIND, OPENAI_FAMILY

    ucode_calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(
        "omnigent.onboarding.setup.login_databricks_workspace",
        lambda url, *, console=None: "my-ws",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup.configure_ucode_for_workspace",
        lambda url, *, agents=None: ucode_calls.append((url, agents)),
    )
    monkeypatch.setattr("omnigent.onboarding.ucode_setup.ucode_workspace_exists", lambda url: True)

    # Databricks position within the Codex (openai) add menu, computed live.
    codex_opts = add_menu_options_for_family(OPENAI_FAMILY)
    db = next(i for i, o in enumerate(codex_opts) if o.kind == DATABRICKS_KIND) + 1
    # L1 2=Codex → L2 1=+Add → add menu <db>=Databricks → URL → q → q.
    stdin = "\n".join(["2", "1", str(db), "https://example.cloud.databricks.com", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    assert ucode_calls == [("https://example.cloud.databricks.com", ["codex"])]
    cfg = _config_yaml(isolated_config)
    assert get_default_provider(cfg, "openai").name == "databricks"
    assert get_default_provider(cfg, "anthropic") is None


def test_uninstalled_harness_shows_x_and_not_installed(isolated_config, monkeypatch) -> None:
    """A harness whose CLI isn't installed renders a red ✗ "Not installed" status.

    Overrides the installed-by-default fixture. The level-1 overview folds the
    readiness into the row's right-aligned status: an absent CLI reads
    ``✗ Not installed`` inline (the exact install command is the selection-only
    description, surfaced when the row is highlighted, not in the always-visible
    row).
    """
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: False
    )
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input="q\n")
    assert result.exit_code == 0, result.output
    out = result.output
    assert "✗" in out
    assert "Not installed" in out


def test_overview_marks_unconfigured_with_x_and_configured_without_checkmark(
    isolated_config,
) -> None:
    """Level 1: a configured harness carries no name-level ✓; an unconfigured one gets ✗.

    Seeds only an Anthropic (Claude) default and leaves Codex with no
    credential. The overview must (1) drop the old green ✓ next to the
    configured Claude name — the green ✓ now lives only on the status column —
    and (2) mark the installed-but-unconfigured Codex with a ✗ "Not configured"
    status. A regression that restores the name-level ✓ or fails to flag the
    empty harness surfaces here.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    }
                }
            },
            f,
        )

    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input="q\n")
    assert result.exit_code == 0, result.output
    out = result.output
    # Configured Claude: the green ✓ rides the right-aligned status, not the
    # name — so "✓ Claude" never appears, but the credential's "✓ …" does.
    assert "✓ Claude" not in out
    assert "✓ Anthropic API Key" in out
    # Installed-but-unconfigured Codex: the row's status is a ✗ "Not configured".
    assert "Codex" in out
    assert "✗ Not configured" in out


def _capture_setup_overview(
    monkeypatch,
) -> tuple[list[str], list[bool], list[str], bool]:
    """Render the level-1 setup overview once and capture the menu it builds.

    Monkeypatches the shared ``select`` so the picker records the rows it would
    draw, then returns ``-1`` (Esc) so setup exits after one frame. Returns the
    captured ``(options, selectable, descriptions, compact)`` — enough to assert
    the row set, ordering, single-line compactness, and the selection-only
    install hints without driving a real TTY.
    """
    captured: dict[str, object] = {}

    def _capture_select(
        title: str,
        options: list[str],
        *,
        selectable: list[bool] | None = None,
        descriptions: list[str] | None = None,
        compact: bool = False,
        clear_on_exit: bool = False,
        **_kwargs: object,
    ) -> int:
        assert title == "Configure harnesses"
        assert selectable is not None
        assert descriptions is not None
        captured.update(
            options=options,
            selectable=selectable,
            descriptions=descriptions,
            compact=compact,
        )
        return -1

    monkeypatch.setattr("omnigent.onboarding.interactive.select", _capture_select)
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"])
    assert result.exit_code == 0, result.output
    return (
        captured["options"],  # type: ignore[return-value]
        captured["selectable"],  # type: ignore[return-value]
        captured["descriptions"],  # type: ignore[return-value]
        captured["compact"],  # type: ignore[return-value]
    )


def _overview_row_names(options: list[str], selectable: list[bool]) -> list[str]:
    """Extract the harness / Quit names from a captured overview frame.

    Each row label is ``"<name><padding>[<color>]<glyph> <status>[/]"``; the
    name is the text before the 2-space status gutter, recovered after Rich
    markup is stripped.
    """
    import re

    from rich.text import Text

    names: list[str] = []
    for option, is_selectable in zip(options, selectable, strict=True):
        if not is_selectable:
            continue
        plain = Text.from_markup(option).plain
        names.append(re.split(r"\s{2,}", plain, maxsplit=1)[0].strip())
    return names


def test_overview_lists_all_harnesses_in_priority_order(isolated_config, monkeypatch) -> None:
    """The overview shows every harness on one compact row, in 0.3 priority order.

    No "More" folding: all twelve harnesses are visible at once, followed by
    Quit. A regression that hides a harness, reorders the core six, or
    reintroduces a collapse row fails here. The menu also opts into the compact
    (underline-highlight) rendering.
    """
    options, selectable, _descriptions, compact = _capture_setup_overview(monkeypatch)
    assert _overview_row_names(options, selectable) == [
        "Claude",
        "Codex",
        "Cursor",
        "OpenCode",
        "Hermes",
        "Pi",
        "Antigravity",
        "Qwen Code",
        "Goose",
        "Copilot",
        "Kiro",
        "Kimi Code",
        "Quit",
    ]
    assert compact is True


def test_overview_rows_are_single_line(isolated_config, monkeypatch) -> None:
    """Every overview row is a single selectable line — no skipped sub-lines.

    The compact layout folds each harness's status into its row (right-aligned)
    instead of a dim sub-line beneath it, so the cursor lands on every rendered
    row and each row carries a (possibly empty) description. A regression that
    brings back non-selectable sub-lines fails here.
    """
    options, selectable, descriptions, _compact = _capture_setup_overview(monkeypatch)
    assert all(selectable)
    assert len(descriptions) == len(options)


def test_overview_lists_kiro_row(isolated_config, monkeypatch) -> None:
    """Kiro is a first-class harness row with its own status + selection hint.

    Kiro (``kiro-native``) is a native CLI harness with its own auth
    (``kiro-cli login``). Absent the CLI the row reads ``✗ Not installed`` and
    its selection-only description names the curl installer; installed, it reads
    ``✓ Installed`` with the sign-in reminder. A regression that drops the Kiro
    row fails here.
    """
    from rich.text import Text

    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: False
    )
    options, selectable, descriptions, _ = _capture_setup_overview(monkeypatch)
    names = _overview_row_names(options, selectable)
    kiro = names.index("Kiro")
    assert "Not installed" in Text.from_markup(options[kiro]).plain
    assert "cli.kiro.dev/install" in Text.from_markup(descriptions[kiro]).plain

    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: True
    )
    options, selectable, descriptions, _ = _capture_setup_overview(monkeypatch)
    names = _overview_row_names(options, selectable)
    kiro = names.index("Kiro")
    assert "Installed" in Text.from_markup(options[kiro]).plain
    assert "kiro-cli login" in Text.from_markup(descriptions[kiro]).plain


@pytest.mark.parametrize(
    "choice,manager_attr",
    [
        ("4", "_manage_opencode_harness"),
        ("5", "_manage_hermes_harness"),
        ("8", "_manage_qwen_harness"),
        ("9", "_manage_goose_harness"),
        ("10", "_manage_copilot_harness"),
        ("11", "_manage_kiro_harness"),
        ("12", "_manage_kimi_harness"),
    ],
)
def test_overview_dispatches_to_correct_manager(
    isolated_config, monkeypatch, choice: str, manager_attr: str
) -> None:
    """Selecting a harness routes to its drill-in, pinning position→sentinel→manager.

    The ordering test asserts row *names* only, so a copy-paste slip that paired
    the wrong sentinel with a name (e.g. ``(_QWEN, "Goose", …)``) would route
    "Goose" to ``_manage_qwen_harness`` yet still pass the name check. This drives
    the real numbered-fallback dispatch end-to-end for the seven harnesses whose
    positions no other scripted-stdin test exercises (Claude/Codex/Cursor/Pi/
    Antigravity are covered by the add/remove/key tests), so a misrouted row is
    caught here.
    """
    called: list[str] = []
    monkeypatch.setattr(
        f"omnigent.cli.{manager_attr}", lambda *a, **k: called.append(manager_attr)
    )
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=f"{choice}\nq\n")
    assert result.exit_code == 0, result.output
    assert called == [manager_attr]


def test_overview_status_color_distinguishes_missing_from_unconfigured(
    isolated_config, monkeypatch
) -> None:
    """The ✗ status color encodes *absent* (red) vs *installed-but-unconfigured* (yellow).

    The kind taxonomy (``missing`` → red, ``warn`` → yellow, ``ready`` → green)
    is the whole point of the status column, but the other overview tests assert
    only the glyph + text. Here we capture the raw row markup (pre-render) and
    pin the color so a regression that, say, paints an absent CLI yellow (telling
    a user a missing tool is merely "unconfigured") fails.
    """
    # Installed but unconfigured → yellow ✗ (a usable harness awaiting setup).
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: True
    )
    options, selectable, _descriptions, _compact = _capture_setup_overview(monkeypatch)
    codex = options[_overview_row_names(options, selectable).index("Codex")]
    assert "[yellow]✗ Not configured[/]" in codex

    # CLI absent → red ✗ (nothing to use yet).
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: False
    )
    options, selectable, _descriptions, _compact = _capture_setup_overview(monkeypatch)
    codex = options[_overview_row_names(options, selectable).index("Codex")]
    assert "[red]✗ Not installed[/]" in codex


def test_drill_into_uninstalled_installs_then_proceeds(isolated_config, monkeypatch) -> None:
    """Selecting an uninstalled harness → 'Yes, install' runs the install and
    proceeds to credential setup.

    The install boundary (``install_harness_cli``) is stubbed to succeed; the
    test asserts it was invoked for the right family. A regression that skipped
    the install or called it for the wrong harness fails here.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: False
    )
    installed: list[str] = []
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.install_harness_cli",
        lambda family: installed.append(family) or True,
    )
    # L1 1=Claude → install prompt 1=Yes (install) → L2 credential menu q=back
    # → L1 q=exit.
    stdin = "\n".join(["1", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    assert installed == ["anthropic"]  # installed exactly the drilled-in harness


def test_decline_install_returns_without_installing(isolated_config, monkeypatch) -> None:
    """Choosing 'No' at the install prompt returns to the picker, no install."""
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: False
    )

    def _must_not_install(family: str) -> bool:
        raise AssertionError("install_harness_cli called despite declining")

    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.install_harness_cli", _must_not_install
    )
    # L1 1=Claude → install prompt 2=No → L1 q=exit.
    stdin = "\n".join(["1", "2", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output


# ── the Pi harness page ───────────────────────────────────────────────


def test_pi_add_menu_offers_keys_gateway_databricks_but_no_subscription() -> None:
    """The Pi-scoped add menu offers every credential pi can use — and only those.

    pi consumes both model families, so both vendors' API keys, gateways,
    OpenRouter, "Other provider", and Databricks all appear; the claude /
    codex subscriptions must NOT (a CLI login is unusable outside its own
    CLI — offering it would configure a credential pi silently can't use).
    """
    from omnigent.onboarding.provider_config import PI_SURFACE

    options = add_menu_options_for_family(PI_SURFACE)
    kinds = {o.kind for o in options}
    # No subscription row — the one credential kind pi can't consume.
    assert "subscription" not in kinds
    # Both vendors' keys are offered (pi spans both families), plus the
    # cross-vendor extras and Databricks.
    assert any(o.label.endswith("Anthropic — API key") for o in options)
    assert any(o.label.endswith("OpenAI — API key") for o in options)
    assert "gateway" in kinds
    assert "databricks" in kinds


def test_configure_harnesses_pi_page_sets_explicit_pi_default(isolated_config) -> None:
    """Making a credential the Pi default writes the explicit pi scope.

    Seed two keys (anthropic the default for its family — so pi initially
    rides the anthropic-preferred fallback) and use the Pi page to make the
    openai key pi's default. The persisted openai entry must carry the pi
    scope, pi resolution must follow it, and BOTH family defaults must be
    untouched — the per-surface coexistence invariant extended to pi.
    """
    from omnigent.onboarding.provider_config import default_provider_for_harness

    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                            "models": {"default": "claude-sonnet-4-6"},
                        },
                    },
                    "openai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                            "models": {"default": "gpt-5.5"},
                        },
                    },
                }
            },
            f,
        )

    # L1 6=Pi → L2 (1=anthropic 2=openai 3=+Add): select openai (2) → L3
    # 1=Make default for Pi → back to L2 q=back → L1 q=exit.
    stdin = "\n".join(["6", "2", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = load_config()
    # pi now explicitly routes through the openai key — beating the
    # anthropic-preferred fallback it rode before the action.
    assert default_provider_for_harness(cfg, "pi").name == "openai"
    # The family defaults are untouched: setting the pi scope must not
    # disturb either harness's own slot.
    assert get_default_provider(cfg, "anthropic").name == "anthropic"
    assert get_default_provider(cfg, "openai").name == "openai"
    # The persisted form carries the explicit scope (not collapsed to true,
    # which re-parses without pi — the round-trip bug).
    raw_default = _config_yaml(isolated_config)["providers"]["openai"]["default"]
    assert sorted(raw_default) == ["openai", "pi"]


def test_configure_harnesses_pi_page_excludes_subscription_rows(isolated_config) -> None:
    """The Pi page lists only credentials pi can use; subscriptions are absent.

    Seed a claude subscription (the anthropic default) plus an openai key.
    Pi's level 2 must show the key — marked ✓ default via the fallback,
    which skips the subscription — and no ``Subscription`` row at all. A
    regression that lists subscriptions under Pi lets the user "select" a
    credential the pi harness silently can't consume.
    """
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "claude-subscription": {
                        "kind": "subscription",
                        "cli": "claude",
                        "default": True,
                    },
                    "openai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": "https://api.openai.com/v1",
                            "api_key_ref": "keychain:openai",
                        },
                    },
                }
            },
            f,
        )

    # L1 6=Pi → L2 renders its rows → q=back → L1 q=exit. The L2 frame is
    # cleared on exit under a TTY but the numbered fallback echoes options.
    stdin = "\n".join(["6", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    # Isolate the Pi level-2 frame (after the "Pi — select or add" title).
    pi_frame = result.output.split("Pi — select or add a credential", 1)[1]
    pi_frame = pi_frame.split("Configure harnesses", 1)[0]
    # The key row appears and carries the effective-default marker (the
    # fallback skipped the subscription); no Subscription row is offered.
    assert "OpenAI API Key" in pi_frame
    assert "✓ default" in pi_frame
    assert "Subscription" not in pi_frame


def test_configure_harnesses_add_databricks_under_pi_scopes_to_pi(
    isolated_config, monkeypatch
) -> None:
    """Adding Databricks under the Pi harness scopes ucode to ``--agents pi``
    and defaults only the pi surface.

    The pi mirror of the Claude/Codex-path tests: ucode must configure only
    the pi tool, and the provider must claim only the explicit pi scope —
    routing Claude/Codex through a workspace ucode never configured for
    them would be the regression.
    """
    from omnigent.onboarding.configure_models import add_menu_options_for_family
    from omnigent.onboarding.provider_config import (
        DATABRICKS_KIND,
        PI_SURFACE,
        default_provider_for_harness,
    )

    ucode_calls: list[tuple[str, list[str] | None]] = []
    monkeypatch.setattr(
        "omnigent.onboarding.setup.login_databricks_workspace",
        lambda url, *, console=None: "my-ws",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_setup.configure_ucode_for_workspace",
        lambda url, *, agents=None: ucode_calls.append((url, agents)),
    )
    monkeypatch.setattr("omnigent.onboarding.ucode_setup.ucode_workspace_exists", lambda url: True)

    # Databricks position within the Pi add menu, computed live.
    pi_opts = add_menu_options_for_family(PI_SURFACE)
    db = next(i for i, o in enumerate(pi_opts) if o.kind == DATABRICKS_KIND) + 1
    # L1 6=Pi → L2 1=+Add → add menu <db>=Databricks → URL → q → q.
    stdin = "\n".join(["6", "1", str(db), "https://example.cloud.databricks.com", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    # ucode ran once, for the pi agent only — not the legacy claude,codex,pi.
    assert ucode_calls == [("https://example.cloud.databricks.com", ["pi"])]
    cfg = _config_yaml(isolated_config)
    # The provider claims only the explicit pi scope it was added under.
    assert cfg["providers"]["databricks"] == {
        "kind": "databricks",
        "profile": "my-ws",
        "default": "pi",
    }
    assert default_provider_for_harness(load_config(), "pi").name == "databricks"
    # Claude/Codex stay unset — ucode never configured them for this workspace.
    assert get_default_provider(cfg, "anthropic") is None
    assert get_default_provider(cfg, "openai") is None


def test_add_key_does_not_steal_pi_from_fallback_default(isolated_config) -> None:
    """A newly added key never claims the pi scope while a fallback serves pi.

    Seed an anthropic key as the anthropic default — pi rides the
    anthropic-preferred fallback onto it. Adding an openai key (under the
    Codex page) auto-claims the free openai family but must NOT claim the
    pi scope: pi's effective default already resolves, and stealing it
    would silently re-route pi to the brand-new key.
    """
    from omnigent.onboarding.provider_config import default_provider_for_harness

    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.anthropic.com",
                            "api_key_ref": "keychain:anthropic",
                        },
                    },
                }
            },
            f,
        )

    # L1 2=Codex → L2 1=+Add → 1=OpenAI key → decline detected env (none set)
    # → paste key → accept catalog default model (blank) → L2 q → L1 q.
    stdin = "\n".join(["2", "1", "1", "sk-test-openai", "", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = load_config()
    # The new key took the free openai family default…
    assert get_default_provider(cfg, "openai").name == "openai"
    # …but pi still rides the fallback onto the anthropic key: the openai
    # entry must not have claimed the explicit pi scope.
    assert default_provider_for_harness(cfg, "pi").name == "anthropic"
    raw_default = _config_yaml(isolated_config)["providers"]["openai"]["default"]
    assert raw_default in (True, "openai", ["openai"])


# ── cli-config labels + entry builder ───────────────────────────────────────


def test_credential_label_cli_config_uses_display_name() -> None:
    """A cli-config credential labels as the provider's own display name.

    Failure means configure-harnesses shows the raw entry id instead of
    the friendly name isaac wrote into the provider table.
    """
    from omnigent.onboarding.configure_models import credential_label

    label = credential_label(
        "cli-config", "codex-databricks", display_name="Databricks AI Gateway"
    )
    assert label == "Databricks AI Gateway"


def test_credential_label_cli_config_falls_back_to_entry_name() -> None:
    """Without a display name, the entry name is the label.

    Failure (empty/None label) would render a blank credential row.
    """
    from omnigent.onboarding.configure_models import credential_label

    assert credential_label("cli-config", "codex-myproxy") == "codex-myproxy"


def test_build_cli_config_provider_entry_shapes() -> None:
    """The builder emits the exact config shape the parser requires.

    Full-equality assertions: a drifted key would make adoption write
    entries that fail to load on the next configure open.
    """
    from omnigent.onboarding.configure_models import build_cli_config_provider_entry

    assert build_cli_config_provider_entry("codex", "Databricks", "Databricks AI Gateway") == {
        "kind": "cli-config",
        "cli": "codex",
        "model_provider": "Databricks",
        "display_name": "Databricks AI Gateway",
    }
    # No display name → key omitted entirely (labels fall back to the
    # entry name), not written as None/empty.
    assert build_cli_config_provider_entry("codex", "MyProxy", None) == {
        "kind": "cli-config",
        "cli": "codex",
        "model_provider": "MyProxy",
    }


# ── cli-config removal dismissal + re-add ────────────────────────────────────

# The exact state `isaac configure codex` leaves behind: a custom provider
# with self-contained auth in config.toml, and NO auth.json.
_CODEX_CONFIG_TOML = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"

[model_providers.Databricks.auth]
command = "jq"
"""


def _write_codex_config_toml(home) -> None:
    """Write an isaac-style ``~/.codex/config.toml`` under the test HOME.

    :param home: The tmp HOME directory (the ``isolated_config`` fixture
        redirects ``$HOME`` there).
    """
    codex_dir = home / ".codex"
    codex_dir.mkdir(exist_ok=True)
    (codex_dir / "config.toml").write_text(_CODEX_CONFIG_TOML)


def test_remove_cli_config_credential_dismisses_detection(isolated_config) -> None:
    """Removing an adopted codex config.toml credential sticks across reopens.

    The reported bug: Remove dropped the entry, but the next ``setup``
    re-detected the unchanged ~/.codex/config.toml and silently re-adopted
    it — making Remove a no-op. Removal must record a dismissal that the
    next open honors.
    """
    _write_codex_config_toml(isolated_config)

    # Open 1: plain open auto-adopts the detected config provider.
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input="q\n")
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    # Adopted as a real entry — the feature's golden path. Absence means
    # detection itself broke, not the removal under test.
    assert "codex-databricks" in cfg["providers"]

    # Open 2: L1 2=Codex → L2 1=the credential → L3 1=Remove (it is the
    # codex default, so no "Make default" row precedes Remove) → q → q.
    stdin = "\n".join(["2", "1", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    assert "codex-databricks" not in cfg["providers"]
    # The dismissal is what makes Remove stick — without it open 3 re-adopts.
    assert cfg["dismissed_detections"] == ["codex-databricks"]

    # Open 3: a plain reopen must NOT re-adopt the dismissed detection.
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input="q\n")
    assert result.exit_code == 0, result.output
    cfg = _config_yaml(isolated_config)
    assert "codex-databricks" not in cfg.get("providers", {})


def test_add_menu_readds_dismissed_cli_config_credential(isolated_config) -> None:
    """The add menu offers a dismissed config.toml provider; picking it re-adds.

    Removal dismisses the detection (test above), so the add menu's
    detected-config row is the only way back. Re-adding must persist the
    entry, restore it as the codex default, and clear the dismissal.
    """
    from omnigent.onboarding.configure_models import add_menu_options_for_family
    from omnigent.onboarding.provider_config import OPENAI_FAMILY

    _write_codex_config_toml(isolated_config)
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"dismissed_detections": ["codex-databricks"]}, f)

    # The detected-config row is appended after the base codex-scoped
    # options; select() input is its 1-based index.
    detected_row = str(len(add_menu_options_for_family(OPENAI_FAMILY)) + 1)
    # L1 2=Codex → L2 1=+Add (no credentials yet) → add menu: the appended
    # detected-config row → back to L2 q → L1 q.
    stdin = "\n".join(["2", "1", detected_row, "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["codex-databricks"]
    # The persisted entry pins the config.toml provider by name and keeps
    # the friendly display name for labels.
    assert entry["kind"] == "cli-config"
    assert entry["model_provider"] == "Databricks"
    assert entry["display_name"] == "Databricks AI Gateway"
    # Re-claims the codex (openai) default — there is no other credential.
    assert entry["default"] is True or entry.get("default") == "true"
    # The dismissal is cleared, so the credential behaves like an ordinary
    # detection again instead of staying half-dismissed.
    assert cfg["dismissed_detections"] == []


# ── Cursor API-key flow ─────────────────────────────────────────────────────
# Cursor runs via the ``cursor-sdk`` package and authenticates with a
# ``CURSOR_API_KEY``; it has no provider/gateway family. Its drill-in (L1 row 4)
# stores the key in the secret store + a dedicated ``cursor:`` config block,
# mirroring the other harnesses' api-key persistence. The menu is API-key-only
# (Set/Replace/Remove), so it touches neither the ``cursor-agent`` binary nor a
# login probe. ``isolated_config`` clears any ambient ``CURSOR_API_KEY``.


@pytest.fixture()
def _cursor_sdk_present(monkeypatch):
    """Force ``cursor-sdk`` detection to report installed.

    The key-management tests below script the Cursor drill-in assuming no
    install-offer. ``cursor-sdk`` is an opt-in extra (absent in CI), so without
    this the drill-in's install-offer fires — consuming a scripted menu token
    (desyncing the input) and even running a real ``uv pip install``. Patching the
    source-module attribute is seen at every call site (it's resolved at call
    time). Mirror of :func:`_cursor_sdk_absent`.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.cursor_auth.cursor_sdk_installed",
        lambda: True,
    )


def test_cursor_set_api_key_paste_writes_block_and_secret(
    isolated_config, _cursor_sdk_present
) -> None:
    """Pasting a ``crsr_`` key stores the secret + writes the ``cursor:`` block.

    Proves the api-key path: the secret lands in the store (never plaintext in
    config) and the config references it via ``keychain:cursor``.
    """
    # L1 3=Cursor → cursor menu 1=Set API key → paste key (crsr_ → no warn) →
    # cursor menu q=back → L1 q=quit.
    stdin = "\n".join(["3", "1", "crsr_test_key_123", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["cursor"] == {"api_key_ref": "keychain:cursor"}
    # The pasted secret reached the store under the ``cursor`` name; config
    # holds only the reference.
    assert secrets.load_secret("cursor") == "crsr_test_key_123"


def test_cursor_adopt_env_api_key_writes_env_ref(
    isolated_config, monkeypatch, _cursor_sdk_present
) -> None:
    """Adopting an existing ``$CURSOR_API_KEY`` records an ``env:`` ref only.

    The env path must NOT copy the secret into the store — it points the config
    at the live environment variable so the key never leaves the user's shell.
    """
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_env_key_456")
    # L1 3=Cursor → 1=Set API key → "y" adopt detected $CURSOR_API_KEY →
    # q back → q quit.
    stdin = "\n".join(["3", "1", "y", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["cursor"] == {"api_key_ref": "env:CURSOR_API_KEY"}
    assert secrets.load_secret("cursor") is None


def test_cursor_remove_api_key_drops_block_and_secret(
    isolated_config, _cursor_sdk_present
) -> None:
    """Removing a Cursor key deletes the stored secret AND drops the config block."""
    # Seed a stored key: the keychain secret + the ``cursor:`` block referencing it.
    secrets.store_secret("cursor", "crsr_seeded")
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"cursor": {"api_key_ref": "keychain:cursor"}}, f)

    # L1 3=Cursor → cursor menu (key set: 1=Replace 2=Remove 3=Back) → 2=Remove
    # → q back → q quit.
    stdin = "\n".join(["3", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert "cursor" not in cfg
    assert secrets.load_secret("cursor") is None


def test_cursor_set_api_key_non_crsr_declined_is_not_stored(
    isolated_config, _cursor_sdk_present
) -> None:
    """A non-``crsr_`` paste that the user declines to force is NOT persisted.

    The soft prefix check warns and asks to store anyway; declining must leave
    both the secret store and the config untouched.
    """
    # L1 3=Cursor → 1=Set API key → paste non-crsr_ key → "n" decline warning →
    # q back → q quit.
    stdin = "\n".join(["3", "1", "sk-not-a-cursor-key", "n", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert "cursor" not in cfg
    assert secrets.load_secret("cursor") is None


# ── Cursor SDK-extra install offer (the optional ``cursor`` extra) ───────────
# ``cursor-sdk`` is now an OPTIONAL extra, so a key can be set with no SDK and
# setup must offer to install it (like antigravity post-#322). These tests force
# detection absent (the SDK is actually present in the test venv).


@pytest.fixture()
def _cursor_sdk_absent(monkeypatch):
    """Force ``cursor-sdk`` detection to report missing.

    Both the overview row and the drill-in resolve ``cursor_sdk_installed`` from
    the source module at call time, so patching the module attribute covers both.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.cursor_auth.cursor_sdk_installed",
        lambda: False,
    )


def test_cursor_overview_install_command_is_selection_only(
    isolated_config, _cursor_sdk_absent, monkeypatch
) -> None:
    """With the cursor extra absent, the Cursor row's install command is its description.

    The exact ``pip install "omnigent[cursor]"`` (brackets included) is the
    selection-only hint — the selector's per-row description, shown when the row
    is highlighted — and is NOT baked into the always-visible row label. This is
    the "tooltip only on selection" behavior.
    """
    from rich.text import Text

    options, selectable, descriptions, _ = _capture_setup_overview(monkeypatch)
    names = _overview_row_names(options, selectable)
    cursor = names.index("Cursor")
    assert 'pip install "omnigent[cursor]"' in Text.from_markup(descriptions[cursor]).plain
    # The command lives in the description only — never the always-visible row.
    assert "pip install" not in Text.from_markup(options[cursor]).plain


def test_cursor_drillin_offers_install_when_sdk_missing(
    isolated_config, _cursor_sdk_absent
) -> None:
    """Drilling into Cursor with the SDK absent presents the install offer.

    Here the user picks "show the command" (choice 3), which prints it and falls
    through to the key menu, then backs out.
    """
    # L1 3=Cursor → install offer 3=show command → key menu q=back → L1 q.
    stdin = "\n".join(["3", "3", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "isn't installed" in out
    assert 'pip install "omnigent[cursor]"' in out


def test_cursor_key_settable_when_sdk_missing(isolated_config, _cursor_sdk_absent) -> None:
    """The Cursor key is still storable when the SDK is absent (no hard block).

    The deliberate divergence from pi: the drill-in offers the install but does
    NOT gate key management on it. Here the user declines ("set the key anyway" =
    choice 2), then sets the key — which must persist as it does with the SDK.
    """
    # L1 3=Cursor → install offer 2=set key anyway → key menu 1=Set →
    # paste crsr_ key → key menu q=back → L1 q=quit.
    stdin = "\n".join(["3", "2", "1", "crsr_key_no_sdk", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["cursor"] == {"api_key_ref": "keychain:cursor"}
    assert secrets.load_secret("cursor") == "crsr_key_no_sdk"


def test_cursor_install_now_invokes_runner_without_index(
    isolated_config, _cursor_sdk_absent, monkeypatch
) -> None:
    """Choosing "install it now" shells the install with ``omnigent[cursor]``.

    Mocks the subprocess and asserts the argv targets the extra and carries NO
    hardcoded index URL / proxy. Forces the ``uv``-absent path for determinism.
    """
    import subprocess

    calls: list[list[str]] = []

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr("omnigent.onboarding.cursor_auth.shutil.which", lambda name: None)
    monkeypatch.setattr("omnigent.onboarding.cursor_auth.subprocess.run", _run)

    # L1 3=Cursor → install offer 1=install now → key menu q=back → L1 q.
    stdin = "\n".join(["3", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    assert len(calls) == 1, f"expected exactly one install invocation, got {calls}"
    argv = calls[0]
    assert "omnigent[cursor]" in argv
    assert "install" in argv
    # No index URL / proxy is baked into committed code.
    assert not any("index" in part or "://" in part for part in argv)


# ── Antigravity Gemini API-key flow ─────────────────────────────────────────
# Antigravity (Gemini-native, no provider family) is row 7 on the overview (it
# follows Pi) and stores its key in the secret store + the ``antigravity:``
# config block. API-key-only menu (Set/Replace/Remove); ``isolated_config``
# clears ambient GEMINI_API_KEY / ANTIGRAVITY_API_KEY.


@pytest.fixture()
def _antigravity_sdk_present(monkeypatch):
    """Force ``google-antigravity`` detection to report installed.

    The key-management tests below script the Antigravity drill-in assuming no
    install-offer. The optional ``antigravity`` extra is absent in CI, so without
    this the drill-in's install-offer fires — consuming a scripted menu token
    (desyncing the input) and even running a real ``uv pip install``. Patching the
    source-module attribute is seen at every call site (it's resolved at call
    time). Mirror of :func:`_antigravity_sdk_absent`.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.antigravity_auth.antigravity_sdk_installed",
        lambda: True,
    )


def test_antigravity_set_api_key_paste_writes_block_and_secret(
    isolated_config, _antigravity_sdk_present
) -> None:
    """Pasting an ``AIza`` key stores the secret + writes the ``antigravity:`` block.

    Proves the api-key path: the secret lands in the store (never plaintext in
    config) and the config references it via ``keychain:antigravity``.
    """
    # L1 7=Antigravity → antigravity menu 1=Set API key →
    # paste key (AIza → no warn) → antigravity menu q=back → L1 q=quit.
    stdin = "\n".join(["7", "1", "AIza_test_key_123", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["antigravity"] == {"api_key_ref": "keychain:antigravity"}
    # The pasted secret reached the store under the ``antigravity`` name; config
    # holds only the reference.
    assert secrets.load_secret("antigravity") == "AIza_test_key_123"


def test_antigravity_adopt_env_api_key_writes_env_ref(
    isolated_config, monkeypatch, _antigravity_sdk_present
) -> None:
    """Adopting an existing ``$GEMINI_API_KEY`` records an ``env:`` ref only.

    The env path must NOT copy the secret into the store — it points the config
    at the live environment variable so the key never leaves the user's shell.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "AIza_env_key_456")
    # L1 7=Antigravity → 1=Set API key →
    # "y" adopt detected $GEMINI_API_KEY → q back → q quit.
    stdin = "\n".join(["7", "1", "y", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["antigravity"] == {"api_key_ref": "env:GEMINI_API_KEY"}
    assert secrets.load_secret("antigravity") is None


def test_antigravity_remove_api_key_drops_block_and_secret(
    isolated_config, _antigravity_sdk_present
) -> None:
    """Removing a Gemini key deletes the stored secret AND drops the config block."""
    # Seed a stored key: the keychain secret + the ``antigravity:`` block.
    secrets.store_secret("antigravity", "AIza_seeded")
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"antigravity": {"api_key_ref": "keychain:antigravity"}}, f)

    # L1 7=Antigravity → antigravity menu (key set:
    # 1=Replace 2=Remove 3=Back) → 2=Remove → q back → q quit.
    stdin = "\n".join(["7", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert "antigravity" not in cfg
    assert secrets.load_secret("antigravity") is None


def test_antigravity_remove_does_not_delete_foreign_keychain_secret(
    isolated_config, _antigravity_sdk_present
) -> None:
    """Removing antigravity drops the block but spares a shared ``keychain:<other>``.

    A hand-edited ``antigravity:`` block may point at a secret we don't own
    (here ``keychain:shared-gemini``). Remove must NOT clobber that secret —
    only the config block is dropped. Against the old over-broad delete (any
    ``keychain:`` ref) the shared secret would have been destroyed.
    """
    # Seed a foreign shared secret referenced by a hand-edited block.
    secrets.store_secret("shared-gemini", "AIza_shared_seeded")
    config_path = os.path.join(isolated_config, "config.yaml")
    with open(config_path, "w") as f:
        yaml.safe_dump({"antigravity": {"api_key_ref": "keychain:shared-gemini"}}, f)

    # L1 7=Antigravity → antigravity menu 2=Remove →
    # q back → q quit.
    stdin = "\n".join(["7", "2", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    # Block dropped, but the secret we don't own is left intact.
    assert "antigravity" not in cfg
    assert secrets.load_secret("shared-gemini") == "AIza_shared_seeded"


def test_antigravity_set_api_key_non_aiza_declined_is_not_stored(
    isolated_config, _antigravity_sdk_present
) -> None:
    """A non-``AIza`` paste that the user declines to force is NOT persisted.

    The soft prefix check warns and asks to store anyway; declining must leave
    both the secret store and the config untouched.
    """
    # L1 7=Antigravity → 1=Set API key →
    # paste non-AIza key → "n" decline warning → q back → q quit.
    stdin = "\n".join(["7", "1", "sk-not-a-gemini-key", "n", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert "antigravity" not in cfg
    assert secrets.load_secret("antigravity") is None


# ── Antigravity SDK-extra install offer (the optional ``antigravity`` extra) ──
# The antigravity SDK ships in an OPTIONAL extra, so a user can paste a key and still
# have no SDK; setup must detect that and offer to install. These tests force detection
# absent (the SDK is actually present in the test venv).


@pytest.fixture()
def _antigravity_sdk_absent(monkeypatch):
    """Force ``google-antigravity`` detection to report missing.

    Both call sites (overview row + drill-in) resolve ``antigravity_sdk_installed``
    from the source module at call time, so patching the module attribute is seen by
    both.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.antigravity_auth.antigravity_sdk_installed",
        lambda: False,
    )


def test_antigravity_overview_install_command_is_selection_only(
    isolated_config, _antigravity_sdk_absent, monkeypatch
) -> None:
    """With the antigravity extra absent, the Antigravity row's install command is its description.

    The exact ``pip install "omnigent[antigravity]"`` (brackets included) is the
    selection-only hint — the selector's per-row description — not baked into the
    always-visible row. Without the SDK-detection branch the hint never appears.
    """
    from rich.text import Text

    options, selectable, descriptions, _ = _capture_setup_overview(monkeypatch)
    names = _overview_row_names(options, selectable)
    antigravity = names.index("Antigravity")
    assert (
        'pip install "omnigent[antigravity]"' in Text.from_markup(descriptions[antigravity]).plain
    )
    assert "pip install" not in Text.from_markup(options[antigravity]).plain


@pytest.fixture()
def _copilot_sdk_absent(monkeypatch):
    """Force the ``github-copilot-sdk`` extra absent and clear ambient Copilot tokens.

    Copilot is the third soft-SDK-extra harness (like Cursor / Antigravity): its
    readiness is a GitHub token, and a missing SDK is surfaced as an install
    hint rather than a hard block. This drives the unconfigured + SDK-absent
    state so the overview row reads "Not installed" with the extra's install
    command as its selection-only description.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("omnigent.onboarding.copilot_auth.copilot_sdk_installed", lambda: False)


def test_copilot_overview_install_command_is_selection_only(
    isolated_config, _copilot_sdk_absent, monkeypatch
) -> None:
    """With the copilot extra absent, the Copilot row's install command is its description.

    Mirrors the Cursor / Antigravity selection-only-hint contract for the third
    soft-SDK-extra harness: the ``omnigent[copilot]`` install command is the
    per-row description (shown only when highlighted), never baked into the
    always-visible row label.
    """
    from rich.text import Text

    options, selectable, descriptions, _ = _capture_setup_overview(monkeypatch)
    names = _overview_row_names(options, selectable)
    copilot = names.index("Copilot")
    assert "omnigent[copilot]" in Text.from_markup(descriptions[copilot]).plain
    assert "pip install" not in Text.from_markup(options[copilot]).plain


def test_antigravity_drillin_offers_install_when_sdk_missing(
    isolated_config, _antigravity_sdk_absent
) -> None:
    """Drilling into Antigravity with the SDK absent presents the install offer.

    The user picks "show the command" (choice 3), which prints the command and falls
    through to the key menu, then backs out.
    """
    # L1 7=Antigravity → install offer 3=show command →
    # key menu q=back → L1 q.
    stdin = "\n".join(["7", "3", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output
    out = result.output
    assert "isn't installed" in out
    assert 'pip install "omnigent[antigravity]"' in out


def test_antigravity_key_settable_when_sdk_missing(
    isolated_config, _antigravity_sdk_absent
) -> None:
    """The Gemini key is still storable when the SDK is absent (no hard block).

    The deliberate divergence from pi: the drill-in offers the install but does NOT
    gate key management on it. The user declines ("set the key anyway" = choice 2),
    then sets the key, which must persist as it does with the SDK present.
    """
    # L1 7=Antigravity → install offer 2=set key anyway →
    # key menu 1=Set → paste AIza key → key menu q=back → L1 q=quit.
    stdin = "\n".join(["7", "2", "1", "AIza_key_no_sdk", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    assert cfg["antigravity"] == {"api_key_ref": "keychain:antigravity"}
    assert secrets.load_secret("antigravity") == "AIza_key_no_sdk"


def test_antigravity_install_now_invokes_runner_without_index(
    isolated_config, _antigravity_sdk_absent, monkeypatch
) -> None:
    """Choosing "install it now" shells the install with ``omnigent[antigravity]``.

    Mocks the subprocess and asserts the argv targets the extra and carries NO
    hardcoded index URL / proxy. Forces the ``uv``-absent path for a deterministic argv.
    """
    import subprocess

    calls: list[list[str]] = []

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr("omnigent.onboarding.antigravity_auth.shutil.which", lambda name: None)
    monkeypatch.setattr("omnigent.onboarding.antigravity_auth.subprocess.run", _run)

    # L1 7=Antigravity → install offer 1=install now →
    # key menu q=back → L1 q.
    stdin = "\n".join(["7", "1", "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    assert len(calls) == 1, f"expected exactly one install invocation, got {calls}"
    argv = calls[0]
    assert "omnigent[antigravity]" in argv
    assert "install" in argv
    # No index URL / proxy is baked into committed code.
    assert not any("index" in part or "://" in part for part in argv)


def _other_key_add_menu_index(family: str) -> int:
    """Return the 1-based numbered-fallback position of "Other provider — API key".

    Computed from the live per-family add menu rather than hardcoded, so a
    reordering of :func:`add_menu_options` doesn't aim this test's piped stdin
    at the wrong row.

    :param family: The harness surface whose add menu is inspected.
    :returns: The 1-based index of the catch-all ``other``-key option.
    """
    from omnigent.onboarding.configure_models import add_menu_options_for_family
    from omnigent.onboarding.provider_config import KEY_KIND

    opts = add_menu_options_for_family(family)
    return next(i for i, o in enumerate(opts) if o.kind == KEY_KIND and o.other) + 1


def test_configure_harnesses_add_other_key_no_remaining_providers_aborts_cleanly(
    isolated_config, monkeypatch
) -> None:
    """Picking "Other provider — API key" with no catalog providers left aborts
    cleanly instead of crashing.

    Regression for #820: when every catch-all key provider is already configured,
    ``other_key_providers()`` returns ``[]`` and the secondary ``select`` was
    handed an empty option list, raising ``ValueError: select() requires at least
    one option`` out of ``omnigent setup``. The add branch must detect the empty
    list, tell the user, and return — exit code 0, no traceback. Driven under Pi
    (the surface from the report), with the harness CLI forced installed so the
    drill-in reaches the add menu.
    """
    from omnigent.onboarding.provider_config import PI_SURFACE

    # Force the harness CLI "installed" so the Pi drill-in shows the add menu
    # rather than the install prompt, and pretend the catch-all catalog is
    # exhausted (the real-world trigger: all of Groq/DeepSeek/… already added).
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_installed", lambda family: True
    )
    monkeypatch.setattr("omnigent.onboarding.configure_models.other_key_providers", list)

    other = _other_key_add_menu_index(PI_SURFACE)
    # L1 6=Pi → L2 1=+Add → add menu <other>=Other provider — API key → L2 q=back → L1 q=exit.
    stdin = "\n".join(["6", "1", str(other), "q", "q"]) + "\n"
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)

    # Pre-fix this exited non-zero with a ValueError; the guard makes it graceful.
    assert result.exit_code == 0, result.output
    assert result.exception is None, result.exception
    assert "No other API-key providers" in result.output


def test_build_bedrock_provider_entry_shape() -> None:
    """`build_bedrock_provider_entry` produces a kind: bedrock / anthropic body."""
    entry = build_bedrock_provider_entry(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key_ref="env:AWS_BEARER_TOKEN_BEDROCK",
        default_model="us.anthropic.claude-opus-4-5-20251101-v1:0",
    )
    assert entry == {
        "kind": "bedrock",
        "anthropic": {
            "base_url": "https://bedrock-runtime.us-east-1.amazonaws.com",
            "api_key_ref": "env:AWS_BEARER_TOKEN_BEDROCK",
            "models": {"default": "us.anthropic.claude-opus-4-5-20251101-v1:0"},
        },
    }


def test_configure_models_add_bedrock_writes_entry_and_secret(
    isolated_config, monkeypatch
) -> None:
    """Adding 'AWS Bedrock — API key' from the Claude menu writes a kind: bedrock entry.

    Drives the new interactive path: Claude harness → +Add → 'AWS Bedrock —
    API key' (last in the Claude-scoped menu) → name, base_url, pasted bearer
    token, Bedrock model id. Asserts the persisted ``kind: bedrock`` body, the
    keychain secret, and that it auto-becomes the anthropic default. A
    regression means the setup menu can't create a bedrock provider — the gap
    this closes.
    """
    # No exported token → the paste→keychain path (deterministic prompts).
    monkeypatch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
    # L1 1=Claude → L2 1=+Add → Claude menu 5='AWS Bedrock — API key'
    # (1=Anthropic key, 2=Claude sub, 3=Gateway, 4=Databricks, 5=Bedrock) →
    # name; base_url; pasted key; default model → L2 q=back → L1 q=exit.
    stdin = (
        "\n".join(
            [
                "1",
                "1",
                "5",
                "mybr",
                "https://bedrock-runtime.us-east-1.amazonaws.com",
                "absk-test",
                "us.anthropic.claude-opus-4-5-20251101-v1:0",
                "q",
                "q",
            ]
        )
        + "\n"
    )
    result = CliRunner().invoke(cli, ["setup", "--no-internal-beta"], input=stdin)
    assert result.exit_code == 0, result.output

    cfg = _config_yaml(isolated_config)
    entry = cfg["providers"]["mybr"]
    assert entry["kind"] == "bedrock"
    assert entry["anthropic"]["base_url"] == "https://bedrock-runtime.us-east-1.amazonaws.com"
    assert entry["anthropic"]["api_key_ref"] == "keychain:mybr"
    assert entry["anthropic"]["models"]["default"] == "us.anthropic.claude-opus-4-5-20251101-v1:0"
    assert secrets.load_secret("mybr") == "absk-test"
    # A bedrock provider serves the anthropic surface, so it auto-claims the
    # (previously empty) Claude default.
    assert get_default_provider(cfg, "anthropic").name == "mybr"


def test_credential_label_bedrock_not_duplicated() -> None:
    """A bedrock credential reads 'AWS Bedrock', never 'Bedrock Bedrock'.

    The entry name is user-chosen (the default is 'bedrock'); naming the
    credential after the provider id used to render 'Bedrock Bedrock'. The
    generic default collapses to 'AWS Bedrock'; a custom name is qualified.
    """
    from omnigent.onboarding.configure_models import credential_label
    from omnigent.onboarding.provider_config import BEDROCK_KIND

    assert credential_label(BEDROCK_KIND, "bedrock") == "AWS Bedrock"
    assert credential_label(BEDROCK_KIND, "nexus") == "AWS Bedrock (nexus)"
