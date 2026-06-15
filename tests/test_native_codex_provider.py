"""Tests for native-Codex provider routing (configure harnesses parity).

Covers :func:`omnigent.inner.codex_executor._provider_codex_config_overrides`
and :func:`omnigent.codex_native_app_server.resolve_native_codex_launch` —
the path that makes a native Codex terminal route through a ``configure
harness`` provider just like the in-process codex harness, instead of only
the Databricks ucode profile. Providers are constructed via the real config
parser; config + ambient are isolated so resolution is deterministic.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnigent.codex_native_app_server import resolve_native_codex_launch
from omnigent.inner.codex_executor import _provider_codex_config_overrides


@pytest.fixture()
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate config + ambient so codex routing resolution is deterministic."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    return tmp_path


def _seed(config_home: Path, providers: dict[str, object]) -> None:
    """Write a ``providers:`` block into the isolated config home."""
    (config_home / "config.yaml").write_text(yaml.safe_dump({"providers": providers}))


def _write_codex_login(home: Path, *, logged_in: bool) -> None:
    """Write (or stub-empty) ``~/.codex/auth.json`` under the isolated HOME.

    The native subscription routing resolves ``CODEX_HOME or ~/.codex`` — with
    HOME redirected to *home* by the ``_isolated`` fixture, this controls
    whether Codex is considered logged in.

    :param home: The isolated HOME directory (the ``_isolated`` fixture value).
    :param logged_in: When ``True``, write an apikey-mode credential so
        ``codex_auth_has_credential`` returns ``True``; when ``False``, write an
        empty ``{}`` (present-but-logged-out) so it returns ``False``.
    """
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    content = '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-login"}' if logged_in else "{}"
    (codex_dir / "auth.json").write_text(content, encoding="utf-8")


def test_provider_codex_overrides_coerce_chat_wire_to_responses() -> None:
    """A ``chat`` provider wire is coerced to ``responses`` in the override.

    codex >= 0.137 hard-fails config load on ``wire_api="chat"``
    (``wire_api = "chat" is no longer supported``), so emitting it would break
    OSS / OpenRouter routing outright. ``responses`` is the only wire codex
    still accepts, so the override must carry it even for a chat-configured
    provider. Failure (a literal ``wire_api="chat"`` line) means a native Codex
    launch would refuse to start.
    """
    overrides = _provider_codex_config_overrides(
        model="qwen/qwen3.7-plus",
        base_url="https://openrouter.ai/api/v1",
        auth_command="printf %s sk-or-test",
        wire_api="chat",
    )
    joined = "\n".join(overrides)
    assert 'model="qwen/qwen3.7-plus"' in joined
    assert 'model_provider="omnigent_provider"' in joined
    assert 'base_url="https://openrouter.ai/api/v1"' in joined
    # chat is coerced to responses; codex >= 0.137 rejects a chat config.
    assert 'wire_api="responses"' in joined
    assert 'wire_api="chat"' not in joined
    # The token command is embedded as the sh auth command.
    assert "printf %s sk-or-test" in joined


def test_provider_codex_overrides_preserve_responses_wire() -> None:
    """An explicit ``responses`` wire passes through unchanged."""
    overrides = _provider_codex_config_overrides(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        auth_command="printf %s sk",
        wire_api="responses",
    )
    assert 'wire_api="responses"' in "\n".join(overrides)


def test_provider_codex_overrides_omit_model_line_when_none() -> None:
    """``model=None`` omits the ``model="..."`` line but still routes."""
    overrides = _provider_codex_config_overrides(
        model=None,
        base_url="https://api.openai.com/v1",
        auth_command="printf %s sk",
        wire_api="responses",
    )
    joined = "\n".join(overrides)
    assert "model=" not in joined.replace("model_provider=", "")  # no bare model= line
    assert 'model_provider="omnigent_provider"' in joined


def test_resolve_native_codex_launch_key_default_routes_via_overrides(
    _isolated: Path,
) -> None:
    """An openai key default → provider overrides, profile None.

    The P0 parity: native Codex honors `configure harnesses`. Failure means
    the native launch ignored the configured provider.
    """
    _seed(
        _isolated,
        {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-default",
                    "models": {"default": "gpt-5.5"},
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)
    assert launch.profile is None  # a provider routes via overrides, not a profile
    assert launch.model == "gpt-5.5"
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert "printf %s sk-oai-default" in joined


def test_resolve_native_codex_launch_openrouter_coerces_chat_wire(_isolated: Path) -> None:
    """A chat-configured gateway (OpenRouter) routes with the coerced responses wire.

    The provider is persisted with ``wire_api: chat``, but codex >= 0.137 can
    no longer load a chat config, so the resolved launch overrides must carry
    ``wire_api="responses"`` (the coercion in ``_provider_codex_config_overrides``).
    """
    _seed(
        _isolated,
        {
            "openrouter": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "sk-or",
                    "wire_api": "chat",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model="qwen/q")
    joined = "\n".join(launch.config_overrides)
    assert 'wire_api="responses"' in joined
    assert 'wire_api="chat"' not in joined
    assert 'base_url="https://openrouter.ai/api/v1"' in joined
    # Explicit model override wins over the (absent) provider default.
    assert launch.model == "qwen/q"


def test_resolve_native_codex_launch_subscription_logged_in_uses_cli_login(
    _isolated: Path,
) -> None:
    """A subscription default + a logged-in Codex → CLI login, openai pinned.

    When Codex actually has a stored login, deferring to its own auth is
    correct — the bridged ``auth.json`` authenticates it. The launch still
    pins the built-in ``openai`` provider: the bridged config.toml may set a
    custom default ``model_provider`` (e.g. isaac's Databricks AI Gateway),
    which would otherwise silently hijack the Subscription selection. Failure
    with extra overrides means we synthesized a provider route over a working
    subscription; failure with NO overrides means the pin regressed and a
    custom config.toml default can shadow the subscription again.
    """
    _seed(
        _isolated,
        {"codex-subscription": {"kind": "subscription", "cli": "codex", "default": True}},
    )
    _write_codex_login(_isolated, logged_in=True)

    launch = resolve_native_codex_launch(model=None)
    # Exactly the openai pin — no base_url/auth overrides (the login carries auth).
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"


def test_resolve_native_codex_launch_subscription_ignores_private_inherited_home(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A private inherited ``CODEX_HOME`` does not hide the real Codex login.

    Nested Omnigent runs can inherit a per-session private Codex home from
    the parent native terminal. Subscription routing must check the same real
    ``~/.codex`` source that the app-server launch will bridge from; otherwise
    it falls through to a key provider even though the Codex CLI is logged in.

    :param _isolated: Isolated HOME/config directory.
    :param monkeypatch: Pytest fixture used to set inherited ``CODEX_HOME``.
    :returns: None.
    """
    _seed(
        _isolated,
        {
            "codex-subscription": {"kind": "subscription", "cli": "codex", "default": True},
            "openai": {
                "kind": "key",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-real",
                    "models": {"default": "gpt-5.5"},
                },
            },
        },
    )
    _write_codex_login(_isolated, logged_in=True)
    inherited = _isolated / ".omnigent" / "codex-native" / "abc123" / "codex-home"
    inherited.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(inherited))

    launch = resolve_native_codex_launch(model=None)

    # The openai pin (see the logged-in test); the point here is that no
    # key-provider overrides were synthesized despite the private CODEX_HOME.
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"


def test_resolve_native_codex_launch_subscription_no_login_falls_through_to_key(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription default but Codex NOT logged in → falls through to a real key.

    This is the core fix for the reported bug: a stale/dead subscription default
    must not strand the user at Codex's login screen when they have a real
    credential configured. The key is NOT the persisted default (the
    subscription is), so this proves the runtime fall-through, not a default
    change. Failure means the dead subscription shadows the key → empty
    overrides → Codex login prompt.
    """
    # No ambient providers, so the fall-through target is unambiguously the
    # explicitly-configured key (not a detected env key / Ollama).
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    _seed(
        _isolated,
        {
            "codex-subscription": {"kind": "subscription", "cli": "codex", "default": True},
            "openai": {
                "kind": "key",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-real",
                    "models": {"default": "gpt-5.5"},
                },
            },
        },
    )
    _write_codex_login(_isolated, logged_in=False)

    launch = resolve_native_codex_launch(model=None)
    # Routed through the real key, not Codex's login.
    assert launch.profile is None
    assert launch.model == "gpt-5.5"
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert "printf %s sk-oai-real" in joined


def test_resolve_native_codex_launch_subscription_no_login_no_alternative_uses_login(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription default, NOT logged in, no other provider → Codex login.

    With no usable Codex login and nothing to fall through to, dropping to
    Codex's own login is the correct outcome (the user must re-authenticate) —
    still pinned to the built-in ``openai`` provider so the login screen the
    user lands on is ChatGPT's, not a custom config.toml provider's. Failure
    with base_url/auth overrides would mean we fabricated a route from nothing.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    _seed(
        _isolated,
        {"codex-subscription": {"kind": "subscription", "cli": "codex", "default": True}},
    )
    _write_codex_login(_isolated, logged_in=False)

    launch = resolve_native_codex_launch(model=None)
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"


def test_resolve_native_codex_launch_databricks_provider_uses_profile(_isolated: Path) -> None:
    """A databricks provider default → the ucode profile path (its profile)."""
    _seed(
        _isolated,
        {"databricks": {"kind": "databricks", "default": True, "profile": "oss"}},
    )

    launch = resolve_native_codex_launch(model=None)
    assert launch.config_overrides == []
    # Routes via the Databricks profile path, not provider overrides.
    assert launch.profile == "oss"
    assert launch.model == "databricks-gpt-5-5"


def test_resolve_native_codex_launch_global_auth_when_no_provider(_isolated: Path) -> None:
    """No provider configured + a global Databricks ``auth:`` block → ucode.

    With the ``--profile`` flag removed, the global ``auth:`` block in
    ``config.yaml`` is the only spec-less way to route native Codex through a
    Databricks profile. Failure means the global auth fallback was skipped and
    the launch dropped to ambient detection / Codex's own login.
    """
    (_isolated / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "databricks", "profile": "oss"}})
    )

    launch = resolve_native_codex_launch(model=None)
    # Routes via the Databricks ucode profile path, not provider overrides.
    assert launch.config_overrides == []
    assert launch.profile == "oss"


def test_resolve_native_codex_launch_ambient_key_routes(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-less with only an ambient OPENAI_API_KEY → provider overrides.

    First run without configure: native Codex still routes through the
    detected env key (api.openai.com), not the CLI login.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-ambient")

    launch = resolve_native_codex_launch(model=None)
    assert launch.profile is None
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert "printf %s sk-oai-ambient" in joined


def test_resolve_native_codex_launch_cli_config_default_pins_provider(
    _isolated: Path,
) -> None:
    """A cli-config default routes native codex via a model_provider pin only.

    The provider table + credential live in ~/.codex/config.toml (bridged
    into the session CODEX_HOME), so the launch must carry exactly the pin —
    no synthesized base_url/auth overrides, no profile, with the Codex default model pinned.
    Failure on the pin means an adopted isaac-style provider launches the
    native terminal on codex's built-in (unauthenticated) path; extra
    overrides mean we fabricated a transport over the config.toml one.
    """
    _seed(
        _isolated,
        {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
                "default": True,
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"


_DISMISSIBLE_CODEX_CONFIG = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"

[model_providers.Databricks.auth]
command = "jq"
"""


def test_resolve_native_codex_launch_dismissed_config_provider_pins_openai(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Removed (dismissed) config.toml provider is neutralized at launch.

    With the detection dismissed and nothing else configured, the launch
    resolves NO provider — but the bridged ~/.codex/config.toml still sets
    ``model_provider = "Databricks"``, so an unpinned launch would silently
    route through the very credential the user removed (the reported bug:
    codex kept answering through the gateway after Remove). The launch must
    pin codex's built-in ``openai`` provider instead.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG)
    (_isolated / "config.yaml").write_text(
        yaml.safe_dump({"dismissed_detections": ["codex-databricks"]})
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"


def test_resolve_native_codex_launch_undismissed_config_provider_routes_via_pin(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same config WITHOUT a dismissal routes through the detected provider.

    Counterpart to the dismissal test above: an isaac-configured machine
    that never Removed anything must keep routing through the gateway (via
    the detected cli-config provider's pin), not get force-pinned to
    ``openai``. Failure here means the no-provider neutralization fires too
    broadly and breaks the feature's golden path.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG)

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.profile is None
    assert launch.model == "gpt-5.5"
