"""
Tests for the generic-provider routing branch of the per-harness spawn-env
builders in ``omnigent/runtime/workflow.py``.

Chunk 1b wires the kind-typed provider config
(``omnigent/onboarding/provider_config.py``) into the four
``_build_*_spawn_env`` builders so that a configured ``providers:`` entry —
either named explicitly via ``executor.auth: {type: provider, name: X}`` or
selected as the per-family global default — emits the per-harness
vendor-neutral gateway env vars (``HARNESS_*_GATEWAY_BASE_URL`` / ``_HOST`` /
``_AUTH_COMMAND`` / ``HARNESS_*_MODEL`` / the ``HARNESS_*_GATEWAY=true``
enable flag) the executors also consume from the Databricks producer.

Each test asserts the EXACT emitted values, so deleting the provider branch
(or mis-emitting a var) turns the test red. The "backwards-compat" tests
assert that with NO provider configured, the existing api_key / profile
paths are untouched and no provider vars leak in. These are unit tests — no
subprocess spawn, no real CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from omnigent.runtime.workflow import (
    _build_claude_sdk_spawn_env,
    _build_codex_spawn_env,
    _build_openai_agents_sdk_spawn_env,
    _build_pi_spawn_env,
    _build_qwen_spawn_env,
)
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
    ProviderAuth,
)


@pytest.fixture(autouse=True)
def _clear_ambient_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Clear ambient vendor keys so they cannot leak into the spawn env.

    The coding-agent process may have ``ANTHROPIC_API_KEY`` /
    ``OPENAI_API_KEY`` / ``DATABRICKS_TOKEN`` set; clearing them keeps the
    tests deterministic (the provider path resolves keys from the config
    file, not the ambient environment).

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "DATABRICKS_TOKEN"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def config_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Point ``$OMNIGENT_CONFIG_HOME`` at an isolated temp dir.

    Both the readout (provider_config) and the spawn-env builders read the
    global config through this env var, so writing a ``config.yaml`` under
    *tmp_path* exercises the real file-loading path the runtime uses.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: The temp directory used as the config home.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    return tmp_path


def _write_config(config_home: Path, config: dict[str, object]) -> None:
    """
    Write *config* as ``config.yaml`` under *config_home*.

    :param config_home: The ``$OMNIGENT_CONFIG_HOME`` directory.
    :param config: The config mapping to serialize, e.g.
        ``{"providers": {"openrouter": {...}}}``.
    """
    (config_home / "config.yaml").write_text(_yaml.safe_dump(config))


def _make_spec(
    *,
    harness: str,
    model: str | None = None,
    profile: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | ProviderAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal :class:`AgentSpec` for a given harness.

    :param harness: Harness name placed in ``executor.config["harness"]``,
        e.g. ``"claude-sdk"`` / ``"codex"`` / ``"openai-agents"`` / ``"pi"``.
    :param model: Spec-level model, e.g. ``"my-model"``. ``None`` omits it
        so the provider family's ``models.default`` supplies the model.
    :param profile: Legacy ``executor.config["profile"]``. ``None`` omits it.
    :param auth: Typed auth on ``spec.executor.auth``. ``None`` omits it, so
        the no-auth global-default provider path applies.
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": harness}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name=f"test-{harness}",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def _key_family(base_url: str, api_key: str, default_model: str) -> dict[str, object]:
    """
    Build a single provider-family config block (inline static key).

    :param base_url: Family endpoint base URL, e.g.
        ``"https://openrouter.ai/api/v1"``.
    :param api_key: Inline static key value, e.g. ``"sk-test-123"``.
    :param default_model: The family's ``models.default``, e.g. ``"gpt-4o"``.
    :returns: A family mapping ready to nest under a provider entry.
    """
    return {
        "base_url": base_url,
        "api_key": api_key,
        "models": {"default": default_model},
    }


def _anthropic_default_config() -> dict[str, object]:
    """
    Return a config with a single ``default: true`` anthropic ``key`` provider.

    :returns: A config mapping for an anthropic-family default provider.
    """
    return {
        "providers": {
            "vendor-anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1",
                    "sk-ant-secret",
                    "claude-default-model",
                ),
            }
        }
    }


def _openai_default_config() -> dict[str, object]:
    """
    Return a config with a single ``default: true`` openai ``key`` provider.

    :returns: A config mapping for an openai-family default provider.
    """
    return {
        "providers": {
            "vendor-openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family(
                    "https://openai.example.com/v1",
                    "sk-oai-secret",
                    "gpt-default-model",
                ),
            }
        }
    }


# ── Global-default selection, per harness ──────────────────────────────────


def test_claude_sdk_uses_anthropic_global_default(config_home: Path) -> None:
    """
    A ``default: true`` anthropic provider routes the claude-sdk harness.

    Asserts the exact gateway env vars: base_url, host (origin of base_url),
    the printf auth command carrying the resolved key, the family default
    model, and the ``DATABRICKS=true`` enable flag. Failure means the
    no-auth global-default branch is not selecting the provider, or the
    gateway vars are mis-emitted (the harness would then hit
    api.anthropic.com with no key).
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk")  # no auth, no model → use default

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    # Host is the origin (scheme://netloc) of the base URL, not the full URL.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_HOST"] == "https://anthropic.example.com"
    # The static key becomes a printf command carrying the resolved secret.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    # No spec model → the family's models.default supplies the model.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-default-model"


def test_detected_ambient_key_routes_with_no_config(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh machine with only an ambient key routes via the detected provider.

    No ``config.yaml`` is written — the only credential is an ambient
    ``ANTHROPIC_API_KEY``. The spawn-env builder must merge that detection
    (``effective_config_with_detected``) and route the claude-sdk harness
    through it, so "first run without configure" works. Failure means a
    fresh machine would emit no gateway vars and the harness would hit
    api.anthropic.com with no key. HOME is isolated so a real CLI login on
    the test box can't shadow the env-key detection.
    """
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-detected")
    spec = _make_spec(harness="claude-sdk")  # no auth, no config file

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The detected anthropic key became the routed provider.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    # The ambient key is carried as the printf auth command (resolved, not leaked as a ref).
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-detected"
    # No pinned model on the detected entry → the catalog default fills in
    # (non-empty), rather than leaving the model unset.
    assert env["HARNESS_CLAUDE_SDK_MODEL"]


def test_global_databricks_auth_beats_ambient_key(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit global ``auth:`` block wins over an ambient-detected key.

    Regression guard for the databricks/ucode user: ``omnigent setup``
    writes a global ``auth: {type: databricks, profile: oss}`` block (not a
    providers: entry). A spec with NO executor.auth must route through that
    explicit databricks auth, NOT through a stray ``ANTHROPIC_API_KEY`` that
    ambient detection would otherwise auto-default. Explicit config beats
    ambient. Failure means a databricks user's turns silently went to their
    env key instead of Databricks.
    """
    _write_config(config_home, {"auth": {"type": "databricks", "profile": "oss"}})
    monkeypatch.setenv("HOME", str(config_home))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-shadow")
    spec = _make_spec(harness="claude-sdk")  # no executor.auth, no providers:

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Routed via the global databricks profile, not the ambient key.
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "oss"
    # The ambient key never leaked into the spawn env (no provider shadowing).
    assert "sk-ant-shadow" not in repr(env)


def test_codex_uses_openai_global_default(config_home: Path) -> None:
    """
    A ``default: true`` openai provider routes the codex harness.

    Asserts the codex gateway vars plus ``HARNESS_CODEX_WIRE_API`` defaulting
    to ``responses``. Failure means codex is not picking up the openai-family
    default, or the wire-API default regressed.
    """
    _write_config(config_home, _openai_default_config())
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_GATEWAY"] == "true"
    assert env["HARNESS_CODEX_GATEWAY_BASE_URL"] == "https://openai.example.com/v1"
    assert env["HARNESS_CODEX_GATEWAY_HOST"] == "https://openai.example.com"
    assert env["HARNESS_CODEX_GATEWAY_AUTH_COMMAND"] == "printf %s sk-oai-secret"
    assert env["HARNESS_CODEX_MODEL"] == "gpt-default-model"
    # Codex defaults to the Responses wire API when the family omits wire_api.
    assert env["HARNESS_CODEX_WIRE_API"] == "responses"


def test_openai_agents_uses_openai_global_default(config_home: Path) -> None:
    """
    A ``default: true`` openai provider routes the openai-agents-sdk harness.

    Unlike the gateway harnesses, openai-agents takes the API key directly
    (``HARNESS_OPENAI_AGENTS_API_KEY``) with no ``DATABRICKS`` enable flag.
    Failure means the openai-agents builder's early-return provider branch is
    not firing, or the key/base_url/model are mis-emitted.
    """
    _write_config(config_home, _openai_default_config())
    spec = _make_spec(harness="openai-agents")

    env = _build_openai_agents_sdk_spawn_env(spec)

    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] == "https://openai.example.com/v1"
    # Static key → passed directly (not as an auth command) for this harness.
    assert env["HARNESS_OPENAI_AGENTS_API_KEY"] == "sk-oai-secret"
    assert env["HARNESS_OPENAI_AGENTS_MODEL"] == "gpt-default-model"
    # No DATABRICKS enable flag for this harness (executor takes key directly).
    assert "HARNESS_OPENAI_AGENTS_DATABRICKS" not in env


def test_pi_uses_anthropic_global_default(config_home: Path) -> None:
    """
    A ``default: true`` anthropic provider routes the pi harness.

    pi consumes both families: it emits a JSON ``BASE_URLS`` object keyed by
    pi's own family names. With only the anthropic family configured, the
    JSON carries just the ``claude`` key. Failure means pi's both-families
    handling regressed or the JSON keying is wrong.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="pi")

    env = _build_pi_spawn_env(spec, workdir=None)

    assert env["HARNESS_PI_GATEWAY"] == "true"
    # pi keys the base-URL JSON by its own family names ("claude" / "openai").
    assert env["HARNESS_PI_GATEWAY_BASE_URLS"] == (
        '{"claude": "https://anthropic.example.com/v1"}'
    )
    assert env["HARNESS_PI_GATEWAY_HOST"] == "https://anthropic.example.com"
    assert env["HARNESS_PI_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    assert env["HARNESS_PI_MODEL"] == "claude-default-model"


# ── Named ProviderAuth selection ───────────────────────────────────────────


def test_named_provider_auth_selects_provider_over_global_default(config_home: Path) -> None:
    """
    ``executor.auth: {type: provider, name: X}`` selects X over the default.

    The config has TWO anthropic ``key`` providers: a ``default: true`` one
    and a non-default ``named`` one. A spec naming the non-default provider
    must route through it, proving the named branch beats the global-default
    branch. Failure means the named ProviderAuth lookup is ignored and the
    global default wins (wrong endpoint + key).
    """
    config: dict[str, object] = {
        "providers": {
            "vendor-default": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://default.example.com/v1", "sk-default", "default-model"
                ),
            },
            "vendor-named": {
                "kind": "key",
                "anthropic": _key_family(
                    "https://named.example.com/v1", "sk-named", "named-model"
                ),
            },
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk", auth=ProviderAuth(name="vendor-named"))

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The NAMED provider, not the default, supplies the endpoint + key.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://named.example.com/v1"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-named"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "named-model"


def test_named_provider_auth_missing_provider_fails_loud(config_home: Path) -> None:
    """
    A ProviderAuth naming an undeclared provider raises a clear error.

    Failure (no raise) means a typo'd / unconfigured provider name would
    silently fall through to ambient credentials instead of failing loud.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk", auth=ProviderAuth(name="does-not-exist"))

    with pytest.raises(Exception, match="does-not-exist"):
        _build_claude_sdk_spawn_env(spec, workdir=None)


# ── Per-family selection through the spawn path ─────────────────────────────


def test_per_family_defaults_route_independently(config_home: Path) -> None:
    """
    An anthropic default and an openai default coexist and route per-family.

    With BOTH a ``default: true`` anthropic provider and a ``default: true``
    openai provider configured, claude-sdk must get the anthropic base_url
    and codex must get the openai base_url — proving per-family selection
    flows all the way through the spawn path. Failure means the harness→family
    resolution is wrong (e.g. claude-sdk picking the openai default).
    """
    config: dict[str, object] = {
        "providers": {
            "vendor-anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family(
                    "https://anthropic.example.com/v1", "sk-ant", "claude-model"
                ),
            },
            "vendor-openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family("https://openai.example.com/v1", "sk-oai", "gpt-model"),
            },
        }
    }
    _write_config(config_home, config)

    claude_env = _build_claude_sdk_spawn_env(_make_spec(harness="claude-sdk"), workdir=None)
    codex_env = _build_codex_spawn_env(_make_spec(harness="codex"), workdir=None)

    # claude-sdk resolves the anthropic family's default, not the openai one.
    assert claude_env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    assert claude_env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-model"
    # codex resolves the openai family's default, not the anthropic one.
    assert codex_env["HARNESS_CODEX_GATEWAY_BASE_URL"] == "https://openai.example.com/v1"
    assert codex_env["HARNESS_CODEX_MODEL"] == "gpt-model"


# ── Spec model overrides the family default ────────────────────────────────


def test_spec_model_beats_family_default(config_home: Path) -> None:
    """
    A spec-level model wins over the provider family's ``models.default``.

    Failure means the provider branch clobbers an explicit ``executor.model``
    with the family default — the spec must always win for the model.
    """
    _write_config(config_home, _anthropic_default_config())
    spec = _make_spec(harness="claude-sdk", model="spec-chosen-model")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The provider branch fired (gateway base_url is set) — so this is not a
    # vacuous pass where the model is simply the spec's because no provider
    # ran. The base_url proves the provider was selected despite a spec model.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://anthropic.example.com/v1"
    # Spec model wins; the family default ("claude-default-model") is ignored.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "spec-chosen-model"


# ── Catalog default model fallback (no spec model, no provider default) ─────


def _key_family_no_model(base_url: str, api_key: str) -> dict[str, object]:
    """
    Build a provider-family block with NO ``models.default``.

    Mirrors the reported bug: a ``key`` provider with only ``base_url`` +
    credential and no ``models`` block.

    :param base_url: Family endpoint base URL, e.g.
        ``"https://api.anthropic.com"``.
    :param api_key: Inline static key value, e.g. ``"sk-ant-secret"``.
    :returns: A family mapping with no ``models`` key.
    """
    return {"base_url": base_url, "api_key": api_key}


def test_claude_sdk_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An anthropic ``key`` provider with no ``models.default`` resolves a
    catalog default model instead of failing loud.

    This is the reported bug: the spec sets no model and the provider's
    anthropic family declares no ``models.default``. Rather than raising,
    the builder must emit ``HARNESS_CLAUDE_SDK_MODEL`` equal to the bundled
    catalog's default anthropic model — proving the gateway path still gets
    a real model. The base_url assertion proves the provider branch fired
    (not a vacuous pass).
    """
    from omnigent.onboarding.providers import default_chat_model

    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk")  # no auth, no model, no provider default

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    catalog_default = default_chat_model("anthropic")
    assert catalog_default is not None  # the catalog knows anthropic
    assert catalog_default.startswith("claude-")  # a real anthropic model
    # The provider branch fired (gateway base_url set) AND the model came
    # from the catalog, not from a provider/spec default and not a failure.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == catalog_default
    # The generic provider routes through the vendor-neutral GATEWAY
    # transport: enable flag + a real bearer-token command are emitted,
    # and crucially NO Databricks-branded transport var leaks (the
    # "inherited wart" this rename removed). The only Databricks-named
    # var that may ever appear is the profile, which is absent here
    # because this is a key provider, not a databricks-kind one.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"] == "printf %s sk-ant-secret"
    assert not any(k.startswith("HARNESS_CLAUDE_SDK_DATABRICKS") for k in env)


def test_codex_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An openai ``key`` provider with no ``models.default`` resolves the
    catalog default for the codex harness.

    Proves the openai-family fallback path: codex must emit
    ``HARNESS_CODEX_MODEL`` equal to the catalog's default openai model
    (a ``gpt-*`` flagship, not an audio/realtime specialty variant).
    """
    from omnigent.onboarding.providers import default_chat_model

    config: dict[str, object] = {
        "providers": {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family_no_model("https://api.openai.com/v1", "sk-oai-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    catalog_default = default_chat_model("openai")
    assert catalog_default is not None
    assert catalog_default.startswith("gpt-")  # a real general-purpose openai model
    assert env["HARNESS_CODEX_GATEWAY_BASE_URL"] == "https://api.openai.com/v1"
    assert env["HARNESS_CODEX_MODEL"] == catalog_default


def test_openai_agents_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An openai ``key`` provider with no ``models.default`` resolves the
    catalog default for the openai-agents-sdk harness.

    Proves the analogous fallback in :func:`_apply_provider_to_openai_agents`.
    """
    from omnigent.onboarding.providers import default_chat_model

    config: dict[str, object] = {
        "providers": {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family_no_model("https://api.openai.com/v1", "sk-oai-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="openai-agents")

    env = _build_openai_agents_sdk_spawn_env(spec)

    catalog_default = default_chat_model("openai")
    assert catalog_default is not None
    assert env["HARNESS_OPENAI_AGENTS_GATEWAY_BASE_URL"] == "https://api.openai.com/v1"
    assert env["HARNESS_OPENAI_AGENTS_MODEL"] == catalog_default


def test_qwen_uses_openai_global_default(config_home: Path) -> None:
    """
    A ``default: true`` openai provider routes the qwen harness.

    Qwen consumes the openai family (OpenAI-compatible wire), so it should
    emit env vars keyed by "openai" in the gateway base URL JSON.
    """
    _write_config(config_home, _openai_default_config())
    spec = _make_spec(harness="qwen")

    env = _build_qwen_spawn_env(spec, workdir=None)

    assert env["HARNESS_QWEN_GATEWAY"] == "true"
    # qwen keys the base-URL JSON by its family name ("openai").
    assert env["HARNESS_QWEN_GATEWAY_BASE_URLS"] == (
        '{"openai": "https://api.openai.com/v1"}'
    )
    assert env["HARNESS_QWEN_GATEWAY_HOST"] == "https://api.openai.com"
    assert env["HARNESS_QWEN_GATEWAY_AUTH_COMMAND"] == "printf %s sk-oai-secret"
    assert env["HARNESS_QWEN_MODEL"] == "gpt-default-model"


def test_qwen_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An openai ``key`` provider with no ``models.default`` resolves the
    catalog default for the qwen harness.

    Proves the analogous fallback in :func:`_build_qwen_spawn_env`.
    """
    from omnigent.onboarding.providers import default_chat_model

    config: dict[str, object] = {
        "providers": {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": _key_family_no_model("https://api.openai.com/v1", "sk-oai-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="qwen")

    env = _build_qwen_spawn_env(spec, workdir=None)

    catalog_default = default_chat_model("openai")
    assert catalog_default is not None
    assert env["HARNESS_QWEN_GATEWAY_BASE_URL"] == "https://api.openai.com/v1"
    assert env["HARNESS_QWEN_MODEL"] == catalog_default


def test_pi_falls_back_to_catalog_default_model(config_home: Path) -> None:
    """
    An anthropic ``key`` provider with no ``models.default`` resolves the
    catalog default for the pi harness (anthropic auth-source family).

    pi prefers the anthropic family for auth, so the model fallback must
    come from the anthropic catalog default.
    """
    from omnigent.onboarding.providers import default_chat_model

    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="pi")

    env = _build_pi_spawn_env(spec, workdir=None)

    catalog_default = default_chat_model("anthropic")
    assert catalog_default is not None
    assert env["HARNESS_PI_MODEL"] == catalog_default


def test_provider_default_beats_catalog_default(config_home: Path) -> None:
    """
    A provider's ``models.default`` still wins over the catalog default.

    The catalog fallback is the LAST resort: when the provider declares a
    ``models.default`` it must be used unchanged, never overridden by the
    catalog. Failure means the precedence (provider default > catalog
    default) regressed.
    """
    from omnigent.onboarding.providers import default_chat_model

    _write_config(config_home, _anthropic_default_config())  # declares "claude-default-model"
    spec = _make_spec(harness="claude-sdk")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # The provider's explicit default is used, not the catalog's.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "claude-default-model"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] != default_chat_model("anthropic")


def test_spec_model_beats_catalog_default(config_home: Path) -> None:
    """
    A spec-level model still wins when the provider has no ``models.default``.

    The spec model is the highest-precedence source; the catalog fallback
    must not fire when the spec already named a model.
    """
    config: dict[str, object] = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "default": True,
                "anthropic": _key_family_no_model("https://api.anthropic.com", "sk-ant-secret"),
            }
        }
    }
    _write_config(config_home, config)
    spec = _make_spec(harness="claude-sdk", model="spec-chosen-model")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Provider branch fired (base_url set) but the spec model wins.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "https://api.anthropic.com"
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "spec-chosen-model"


# ── databricks-kind provider routes through the profile/ucode path ──────────


def test_databricks_kind_default_routes_through_profile(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A ``databricks``-kind default routes via the profile/ucode path.

    A databricks-kind provider carries a ``profile`` (no inline families), so
    the provider branch must set ``HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE`` to
    that profile and the enable flag — NOT a raw gateway base_url. ucode
    enrichment is stubbed to a no-op so the test asserts only the profile
    wiring this branch owns. Failure means a databricks-kind provider stopped
    delegating to the existing ucode path (breaking nessie / the Databricks
    coding agent).
    """
    config: dict[str, object] = {
        "providers": {
            "dbx": {
                "kind": "databricks",
                "default": True,
                "profile": "my-dbx-profile",
            }
        }
    }
    _write_config(config_home, config)
    # Stub ucode enrichment: it would otherwise read ~/.databrickscfg + ucode
    # state for the profile. We assert the profile wiring this branch owns,
    # independent of whether ucode state exists on the test machine.
    import omnigent.runtime.workflow as workflow_mod

    def _noop_ucode(env: dict[str, str], profile: str | None, *, harness_type: str) -> None:
        # Record the profile passed through so the test can confirm delegation.
        if profile is not None:
            env["_TEST_UCODE_PROFILE"] = profile

    monkeypatch.setattr(workflow_mod, "configure_agent_harness_with_ucode", _noop_ucode)
    spec = _make_spec(harness="claude-sdk")

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    # The profile is set (not a raw gateway base_url) and delegated to ucode.
    assert env["HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"] == "my-dbx-profile"
    assert env["_TEST_UCODE_PROFILE"] == "my-dbx-profile"
    # No raw gateway base_url for a databricks-kind provider.
    assert "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL" not in env


# ── Backwards-compat: no provider configured ───────────────────────────────


def test_no_provider_api_key_path_unchanged(config_home: Path) -> None:
    """
    With NO provider configured, the existing api_key path is untouched.

    A spec with ``ApiKeyAuth`` and no ``providers:`` block must still emit
    ``HARNESS_CLAUDE_SDK_API_KEY_HELPER`` and NO provider gateway vars.
    Failure means the provider branch is firing when it shouldn't (and
    swallowing the api_key path), or provider vars leak in.
    """
    _write_config(config_home, {})  # empty config — no providers
    spec = _make_spec(harness="claude-sdk", model=None, auth=ApiKeyAuth(api_key="sk-direct"))

    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Existing api_key path emits exactly what it did before.
    assert env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] == "printf %s sk-direct"
    # No provider gateway vars leak in (the provider branch did not fire).
    assert "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL" not in env
    assert "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND" not in env
    # api_key auth does not trigger Databricks routing.
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env


def test_no_provider_legacy_profile_path_unchanged(config_home: Path) -> None:
    """
    With NO provider configured, the legacy profile path is untouched.

    A codex spec with a legacy ``executor.config["profile"]`` must still emit
    the ``DATABRICKS=true`` + ``DATABRICKS_PROFILE`` pair and NO provider
    gateway base_url. Failure means the provider branch hijacked the
    legacy-profile path (it must only fire for ProviderAuth / no-auth).
    """
    _write_config(config_home, {})
    spec = _make_spec(harness="codex", model="some-model", profile="legacy-profile")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_GATEWAY"] == "true"
    assert env["HARNESS_CODEX_DATABRICKS_PROFILE"] == "legacy-profile"
    # The legacy path never emits a gateway base_url or auth command.
    assert "HARNESS_CODEX_GATEWAY_BASE_URL" not in env
    assert "HARNESS_CODEX_GATEWAY_AUTH_COMMAND" not in env


def test_legacy_profile_suppresses_global_default_provider(config_home: Path) -> None:
    """
    A legacy ``profile`` on the spec suppresses the global-default provider.

    A spec declaring the deprecated ``executor.config["profile"]`` is an
    explicit spec-level auth declaration: the no-auth global-default provider
    branch must NOT override it. Failure means a user's global default
    silently hijacks a spec that pinned a legacy profile.
    """
    _write_config(config_home, _openai_default_config())  # global default exists
    spec = _make_spec(harness="codex", model="some-model", profile="legacy-profile")

    env = _build_codex_spawn_env(spec, workdir=None)

    # The legacy profile wins; the global-default provider is not consulted.
    assert env["HARNESS_CODEX_DATABRICKS_PROFILE"] == "legacy-profile"
    assert "HARNESS_CODEX_GATEWAY_BASE_URL" not in env


# ── cli-config kind: model_provider pinning ─────────────────────────────────


def _cli_config_default_config() -> dict[str, object]:
    """A config whose codex default is a config.toml-pinned provider.

    :returns: A config mapping with one ``default: true`` cli-config entry.
    """
    return {
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


def test_codex_cli_config_default_pins_model_provider(config_home: Path) -> None:
    """A ``default: true`` cli-config provider pins codex's model_provider.

    The entry routes by name only — the provider table + credential live in
    the user's ~/.codex/config.toml, which the executor bridges into the
    session CODEX_HOME — so the spawn env must carry exactly the pin and
    none of the gateway transport vars. Failure on the pin means an adopted
    isaac-style provider launches codex on its built-in (unauthenticated)
    path; a leaked gateway var means the executor would expect a base
    URL/auth command that was never resolved.
    """
    _write_config(config_home, _cli_config_default_config())
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_MODEL_PROVIDER"] == "Databricks"
    # No gateway transport: the provider's endpoint/auth come from the
    # bridged config.toml, not from spawn-env vars.
    assert "HARNESS_CODEX_GATEWAY" not in env
    assert "HARNESS_CODEX_GATEWAY_BASE_URL" not in env
    assert "HARNESS_CODEX_GATEWAY_AUTH_COMMAND" not in env
    # No model pinned either — codex keeps its own default model against
    # the pinned provider (matching how isaac configures it).
    assert "HARNESS_CODEX_MODEL" not in env


def test_codex_subscription_default_pins_builtin_openai(config_home: Path) -> None:
    """A codex ``subscription`` default pins the built-in ``openai`` provider.

    The executor bridges the user's ~/.codex/config.toml, whose custom
    default model_provider (e.g. isaac's Databricks AI Gateway) would
    otherwise silently hijack a Subscription selection. Failure means
    "Subscription" stops meaning "ChatGPT login" on machines with a custom
    config.toml default.
    """
    _write_config(
        config_home,
        {"providers": {"codex-sub": {"kind": "subscription", "cli": "codex", "default": True}}},
    )
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_MODEL_PROVIDER"] == "openai"
    # Subscription still emits no gateway transport (the CLI login is auth).
    assert "HARNESS_CODEX_GATEWAY" not in env


def test_openai_agents_cli_config_default_fails_loud(config_home: Path) -> None:
    """A cli-config default cannot drive the openai-agents-sdk harness.

    The pinned provider exists only inside ~/.codex/config.toml, which only
    the codex CLI reads. Failure (no exception) means openai-agents would
    launch with no credential at all and die opaquely at the first request.
    """
    from omnigent.errors import OmnigentError

    _write_config(config_home, _cli_config_default_config())
    spec = _make_spec(harness="openai-agents")

    with pytest.raises(OmnigentError, match=r"cli-config.*codex"):
        _build_openai_agents_sdk_spawn_env(spec)


_DISMISSIBLE_CODEX_CONFIG_TOML = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"

[model_providers.Databricks.auth]
command = "jq"
"""


def _isolate_home_with_codex_config(config_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point ``$HOME`` at the config home and write a custom codex config there.

    :param config_home: The isolated ``OMNIGENT_CONFIG_HOME`` directory,
        reused as ``$HOME`` so ambient detection reads a controlled
        ``~/.codex/config.toml`` instead of the developer's real one.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setenv("HOME", str(config_home))
    codex_dir = config_home / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG_TOML)


def test_codex_dismissed_config_provider_pins_openai(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no provider resolved and the config provider dismissed, pin openai.

    The executor bridges ~/.codex/config.toml into the session CODEX_HOME,
    so an unpinned no-provider launch would still route through the file's
    custom default — the very credential the user Removed (the reported
    "harness codex still says hi" bug). The dismissal must hold at run time
    via an explicit openai pin.
    """
    _isolate_home_with_codex_config(config_home, monkeypatch)
    _write_config(config_home, {"dismissed_detections": ["codex-databricks"]})
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_MODEL_PROVIDER"] == "openai"
    # Still no gateway transport — this is a neutralizing pin, not a route.
    assert "HARNESS_CODEX_GATEWAY" not in env


def test_codex_undismissed_config_provider_routes_via_detection(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same config WITHOUT a dismissal routes via the detected provider.

    Counterpart to the test above: an isaac-configured machine that never
    Removed anything keeps its gateway routing (the ambient cli-config
    detection auto-defaults and pins its own provider). Failure means the
    no-provider neutralization fires too broadly and breaks the golden path.
    """
    _isolate_home_with_codex_config(config_home, monkeypatch)
    spec = _make_spec(harness="codex")

    env = _build_codex_spawn_env(spec, workdir=None)

    assert env["HARNESS_CODEX_MODEL_PROVIDER"] == "Databricks"
