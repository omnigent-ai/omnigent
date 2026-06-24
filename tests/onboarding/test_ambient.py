"""Tests for omnigent.onboarding.ambient — machine credential detection.

Detection reads the environment, two CLI-login files under ``$HOME``, a single
localhost TCP probe for Ollama, and — on macOS only — a ``claude auth status``
fallback for the Keychain-stored Claude credential. These tests redirect
``$HOME`` to a tmp dir, control the environment explicitly, and monkeypatch
both :func:`omnigent.onboarding.ambient._ollama_reachable` and
:func:`omnigent.onboarding.harness_install.harness_cli_logged_in` so no real
network or subprocess I/O occurs. Each test asserts the exact
:class:`DetectedProvider` fields (name / kind / family / source), not just the
count, so a wrong field turns the test red.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding import ambient
from omnigent.onboarding.ambient import DetectedProvider, detect_providers

# Every provider env var ambient may read — cleared in the base fixture so
# the host's own keys don't leak into the deterministic detection tests.
_PROVIDER_ENV_VARS = [
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "CLAUDE_CODE_USE_VERTEX",
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "CLOUD_ML_REGION",
]


@pytest.fixture
def clean_env(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Isolate detection: empty HOME, no provider keys, Ollama unreachable.

    Redirects ``$HOME`` to a tmp dir (so the CLI-login path checks see no
    credential files), clears every provider env var, stubs
    ``_ollama_reachable`` to ``False``, and stubs the macOS Keychain fallback
    (``harness_cli_logged_in``) to ``False`` so the suite never shells out to a
    real ``claude auth status`` — keeping detection deterministic and free of
    real I/O even when the suite runs on a macOS host with a logged-in CLI.
    Individual tests then opt back in to exactly the signal they exercise.

    :param tmp_path: pytest's per-test temporary directory fixture.
    :param monkeypatch: pytest's env/attr patching fixture.
    :returns: The tmp HOME path, e.g. ``"/tmp/pytest-.../test_x0"``.
    """
    from omnigent.onboarding import harness_install

    monkeypatch.setenv("HOME", str(tmp_path))
    for var in _PROVIDER_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: False)
    # Neutralize the macOS Keychain fallback by default (the file check already
    # sees no creds under the tmp HOME). The detected/absent tests override this.
    monkeypatch.setattr(harness_install, "harness_cli_logged_in", lambda key: False)
    return tmp_path


def test_no_credentials_detected(clean_env) -> None:
    """With nothing present, detection returns an empty list.

    Failure means a stray host credential leaked into the test, or
    detection fabricated a provider with no backing signal.
    """
    assert detect_providers() == []


@pytest.mark.parametrize(
    "env_var,expected",
    [
        (
            "ANTHROPIC_API_KEY",
            DetectedProvider(
                name="anthropic", kind="key", family="anthropic", source="$ANTHROPIC_API_KEY"
            ),
        ),
        (
            "OPENAI_API_KEY",
            DetectedProvider(name="openai", kind="key", family="openai", source="$OPENAI_API_KEY"),
        ),
        (
            "OPENROUTER_API_KEY",
            DetectedProvider(
                name="openrouter", kind="key", family="openai", source="$OPENROUTER_API_KEY"
            ),
        ),
        (
            "GEMINI_API_KEY",
            # Gemini serves the ``gemini`` surface (the antigravity-sdk harness
            # drives the Gemini SDK with a GEMINI_API_KEY), so a detected key
            # is mapped to the gemini family — no longer dropped as family None.
            DetectedProvider(name="gemini", kind="key", family="gemini", source="$GEMINI_API_KEY"),
        ),
    ],
)
def test_env_key_detection(
    clean_env, monkeypatch: pytest.MonkeyPatch, env_var: str, expected: DetectedProvider
) -> None:
    """Each provider env key is detected with the right family mapping.

    Failure means a key would be missed, or mapped to the wrong family —
    routing a Claude key through the OpenAI surface, say.
    """
    monkeypatch.setenv(env_var, "some-secret-value")
    detected = detect_providers()
    # Exactly the one key we set is detected, with all fields exact.
    assert detected == [expected]


def test_empty_env_key_not_detected(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty (set-but-blank) env var is not treated as a credential.

    Failure means a ``ANTHROPIC_API_KEY=""`` would surface a bogus
    detection that resolves to an empty bearer token downstream.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    assert detect_providers() == []


@pytest.mark.parametrize(
    "creds_json",
    [
        # Renewable login: refresh token present (access token may be stale).
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real", "expiresAt": 1}}',
        # No refresh token, but the access token is far from expiry.
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "", '
        '"expiresAt": 99999999999999}}',
    ],
)
def test_claude_cli_login_detected(clean_env, creds_json: str) -> None:
    """A ``~/.claude/.credentials.json`` carrying a usable login is detected.

    Failure means a genuinely logged-in Claude CLI would not be offered during
    setup. The two shapes cover the "renewable (refresh token)" and "unexpired
    access token" branches of usability.
    """
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(creds_json, encoding="utf-8")
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


@pytest.mark.parametrize(
    "creds_json",
    [
        "{}",  # empty — logged out / never logged in
        '{"claudeAiOauth": {}}',  # present object, no tokens
        '{"claudeAiOauth": {"accessToken": "", "refreshToken": "rt"}}',  # blank access token
        # Expired access token with NO refresh token → dead (re-login needed).
        '{"claudeAiOauth": {"accessToken": "at", "refreshToken": "", "expiresAt": 1}}',
        '{"claudeAiOauth": null}',
        "not json",  # malformed
    ],
)
def test_claude_auth_without_credential_not_detected(clean_env, creds_json: str) -> None:
    """A ``.credentials.json`` with no usable login is NOT detected (claude Fix 1).

    Mirrors the codex check: existence alone used to count as a subscription, so
    an empty / logged-out / expired-without-refresh / malformed file planted a
    phantom claude subscription. The expired-without-refresh case is the
    important one — an expired access token with a refresh token is still usable
    (covered above), but with neither it's a dead login. Failure means the
    over-detection regressed.
    """
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(creds_json, encoding="utf-8")
    assert detect_providers() == []


@pytest.mark.parametrize(
    "auth_json",
    [
        # apikey mode: a baked-in OpenAI API key.
        '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-real"}',
        # chatgpt mode: an OAuth access token.
        '{"auth_mode": "chatgpt", "tokens": {"access_token": "at-real", '
        '"refresh_token": "", "id_token": ""}}',
        # chatgpt mode: only a refresh token (CLI mints a fresh access token).
        '{"tokens": {"access_token": "", "refresh_token": "rt-real", "id_token": ""}}',
        # enterprise / external-token integration.
        '{"personal_access_token": "pat-real"}',
    ],
)
def test_codex_cli_login_detected(clean_env, auth_json: str) -> None:
    """A ``~/.codex/auth.json`` carrying a credential is detected as a subscription.

    Failure means a genuinely logged-in Codex CLI would not be offered during
    setup, forcing the user to paste a key they don't need. Each parametrization
    is a distinct usable ``AuthDotJson`` shape (apikey, chatgpt access token,
    refresh-token-only, enterprise PAT).
    """
    cred_dir = clean_env / ".codex"
    cred_dir.mkdir()
    (cred_dir / "auth.json").write_text(auth_json, encoding="utf-8")
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="codex",
            kind="subscription",
            family="openai",
            source="codex CLI login",
        )
    ]


@pytest.mark.parametrize(
    "auth_json",
    [
        "{}",  # empty object — logged out / never logged in
        '{"auth_mode": "apikey", "OPENAI_API_KEY": ""}',  # blank key
        '{"auth_mode": "apikey", "OPENAI_API_KEY": null}',  # null key
        '{"tokens": {"access_token": "", "refresh_token": "", "id_token": ""}}',  # empty tokens
        '{"tokens": null}',  # null tokens
        "not json at all",  # malformed
        "[1, 2, 3]",  # valid JSON but not an object
    ],
)
def test_codex_auth_without_credential_not_detected(clean_env, auth_json: str) -> None:
    """A ``~/.codex/auth.json`` that carries no usable credential is NOT detected.

    This is the core fix: existence alone used to count as a subscription, so an
    empty / logged-out / malformed file planted a phantom ``codex`` subscription
    provider. That phantom could claim the openai-family default and shadow a
    real configured credential, dropping Codex to its own login screen. Failure
    here means the over-detection regressed and the phantom subscription is back.
    """
    cred_dir = clean_env / ".codex"
    cred_dir.mkdir()
    (cred_dir / "auth.json").write_text(auth_json, encoding="utf-8")
    # No codex detection at all — empty list (nothing else is configured).
    assert detect_providers() == []


def test_claude_macos_keychain_login_detected(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On macOS with no creds file, a Keychain login (CLI status) is detected.

    The bug this fixes: Claude Code stores its OAuth in the macOS Keychain, not
    ``~/.claude/.credentials.json``, so the file check misses a real, working
    subscription — and ``configure harnesses`` failed to auto-detect it even
    though the same machine could sign in without a web login. With the file
    absent, detection must fall back to the CLI status check on macOS. Failure
    means the Keychain subscription is silently dropped again.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    # No ~/.claude/.credentials.json under the tmp HOME → file check is False,
    # forcing the macOS Keychain fallback. The fallback asks the CLI (which
    # reads the Keychain); stub it so no real ``claude auth status`` runs.
    from omnigent.onboarding import harness_install

    seen_keys: list[str] = []

    def _fake_logged_in(key: str) -> bool:
        seen_keys.append(key)
        return True

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _fake_logged_in)
    detected = detect_providers()
    # The fallback queried the CLI for the anthropic family specifically.
    assert seen_keys == ["anthropic"]
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


def test_claude_macos_keychain_absent_not_detected(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS with no creds file and a logged-out CLI, claude is NOT detected.

    The Keychain fallback runs (file is absent) but the CLI reports no login, so
    detection must stay empty — no phantom subscription. Failure means the macOS
    fallback fabricates a subscription whenever the file happens to be missing.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    from omnigent.onboarding import harness_install

    seen_keys: list[str] = []

    def _fake_logged_in(key: str) -> bool:
        seen_keys.append(key)
        return False

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _fake_logged_in)
    # The fallback was consulted (proving it ran) but reported no login.
    assert detect_providers() == []
    assert seen_keys == ["anthropic"]


def test_claude_linux_no_keychain_cli_fallback(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """On Linux the CLI-status fallback never runs — detection stays file-only.

    The macOS Keychain fallback is gated on ``sys.platform == "darwin"``: Linux
    stores the credential in the file, so a subprocess fallback would be both
    wrong (Linux is file-accurate) and a needless cost. The stub raises if
    called, so any Linux invocation of the CLI fallback fails the test.
    """
    monkeypatch.setattr(ambient.sys, "platform", "linux")
    from omnigent.onboarding import harness_install

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"CLI fallback must not run on Linux (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    # No creds file under tmp HOME, and the fallback is gated off → empty.
    assert detect_providers() == []


def test_claude_macos_file_present_skips_cli_fallback(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On macOS a usable creds file is detected without invoking the CLI.

    Detection is file-first: when ``~/.claude/.credentials.json`` already
    carries a usable login, the macOS path must short-circuit and never pay the
    ``claude auth status`` subprocess. The stub raises if called, so a redundant
    fallback fails the test.
    """
    monkeypatch.setattr(ambient.sys, "platform", "darwin")
    from omnigent.onboarding import harness_install

    def _must_not_call(key: str) -> bool:
        raise AssertionError(f"file-present path must not invoke the CLI (key={key!r})")

    monkeypatch.setattr(harness_install, "harness_cli_logged_in", _must_not_call)
    cred_dir = clean_env / ".claude"
    cred_dir.mkdir()
    (cred_dir / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real"}}',
        encoding="utf-8",
    )
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="claude",
            kind="subscription",
            family="anthropic",
            source="claude CLI login",
        )
    ]


def test_ollama_detected_when_reachable(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reachable local Ollama is detected as a local openai provider.

    Uses the monkeypatched ``_ollama_reachable`` so no real socket is
    opened. Failure means a running local model server would not be offered.
    """
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: True)
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="ollama",
            kind="local",
            family="openai",
            source="http://localhost:11434",
        )
    ]


def test_detection_priority_order(clean_env, monkeypatch: pytest.MonkeyPatch) -> None:
    """All signals together are returned in env → claude → codex → ollama order.

    Failure means the stable ordering broke, so the setup UI would present
    detected providers in a non-deterministic / surprising order.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-ant")
    monkeypatch.setenv("OPENAI_API_KEY", "k-oai")
    claude_dir = clean_env / ".claude"
    claude_dir.mkdir()
    # A real credential — existence alone is no longer enough to be detected.
    (claude_dir / ".credentials.json").write_text(
        '{"claudeAiOauth": {"accessToken": "at-real", "refreshToken": "rt-real"}}',
        encoding="utf-8",
    )
    codex_dir = clean_env / ".codex"
    codex_dir.mkdir()
    # A real credential — existence alone is no longer enough to be detected.
    (codex_dir / "auth.json").write_text(
        '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-real"}', encoding="utf-8"
    )
    monkeypatch.setattr(ambient, "_ollama_reachable", lambda: True)

    detected = detect_providers()
    # Env keys first, in PROVIDER_ENV_VARS iteration order (openai precedes
    # anthropic in that dict), then claude login, codex login, ollama.
    assert [d.name for d in detected] == ["openai", "anthropic", "claude", "codex", "ollama"]


# ── Codex config.toml custom provider (cli-config) detection ───────────────

# The exact shape `isaac configure codex` writes (AI Gateway mode): a custom
# [model_providers.Databricks] table authenticated by a token-printing
# command, selected via a top-level model_provider — and NO auth.json.
_ISAAC_STYLE_CODEX_CONFIG = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "jq"
args = ["-r", ".access_token", "/home/user/.databricks/model-serving-token.json"]
timeout_ms = 5000
refresh_interval_ms = 1500000

[profiles.default]
model_provider = "Databricks"
"""

# The DetectedProvider the isaac-style config must produce, asserted by
# full equality so any drifted field (name slug, kind, family, source
# wording, provider id, display name) turns the test red.
_ISAAC_STYLE_DETECTION = DetectedProvider(
    name="codex-databricks",
    kind="cli-config",
    family="openai",
    source="~/.codex/config.toml provider 'Databricks'",
    model_provider="Databricks",
    display_name="Databricks AI Gateway",
)


def _write_codex_config(home, body: str) -> None:
    """Write a ``~/.codex/config.toml`` under the test HOME.

    :param home: The tmp HOME directory (from the ``clean_env`` fixture).
    :param body: The TOML text to write, e.g.
        :data:`_ISAAC_STYLE_CODEX_CONFIG`.
    """
    codex_dir = home / ".codex"
    codex_dir.mkdir(exist_ok=True)
    (codex_dir / "config.toml").write_text(body)


def test_codex_config_custom_provider_detected(clean_env) -> None:
    """An isaac-style config.toml (custom provider + auth command) is detected.

    This is the exact state ``isaac configure codex`` leaves behind — no
    auth.json at all — which the subscription detection cannot see. Failure
    means internal users' gateway-configured codex shows as "not configured"
    in setup (the original bug).
    """
    _write_codex_config(clean_env, _ISAAC_STYLE_CODEX_CONFIG)
    assert detect_providers() == [_ISAAC_STYLE_DETECTION]


def test_codex_config_detected_before_codex_login(clean_env) -> None:
    """With BOTH a custom config provider and a codex login, config wins priority.

    Detection order drives the auto-default in the read-time merge, and
    plain ``codex`` on such a machine uses config.toml's default provider
    (it beats auth.json) — omnigents must match. Failure means the
    subscription would auto-default and route differently from the user's
    own ``codex`` terminal.
    """
    _write_codex_config(clean_env, _ISAAC_STYLE_CODEX_CONFIG)
    codex_dir = clean_env / ".codex"
    (codex_dir / "auth.json").write_text('{"OPENAI_API_KEY": "sk-test"}')

    detected = detect_providers()
    # Exactly the two codex signals, config provider first (priority order).
    assert [(d.name, d.kind) for d in detected] == [
        ("codex-databricks", "cli-config"),
        ("codex", "subscription"),
    ]


def test_codex_config_active_profile_overrides_top_level(clean_env) -> None:
    """A top-level ``profile`` selection beats the top-level ``model_provider``.

    Mirrors codex's own resolution (the active profile's model_provider wins
    after profile merge). Failure means detection adopts the wrong provider
    on a profile-switched config.
    """
    _write_codex_config(
        clean_env,
        """
profile = "work"
model_provider = "TopLevel"

[model_providers.TopLevel]
name = "Top Level"
base_url = "https://top.example/v1"
[model_providers.TopLevel.auth]
command = "top-token"

[profiles.work]
model_provider = "WorkProvider"

[model_providers.WorkProvider]
name = "Work Provider"
base_url = "https://work.example/v1"
[model_providers.WorkProvider.auth]
command = "work-token"
""",
    )
    detected = detect_providers()
    # The active profile's provider, not the top-level one — wrong id here
    # means the profile merge is ignored.
    assert [d.model_provider for d in detected] == ["WorkProvider"]
    assert detected[0].display_name == "Work Provider"


def test_codex_config_display_name_falls_back_to_provider_id(clean_env) -> None:
    """A provider table without a ``name`` field labels as its id.

    Failure (None / empty display name) would render a blank label in the
    configure-harnesses listing.
    """
    _write_codex_config(
        clean_env,
        """
model_provider = "MyProxy"
[model_providers.MyProxy]
base_url = "https://proxy.example/v1"
[model_providers.MyProxy.auth]
command = "print-token"
""",
    )
    detected = detect_providers()
    assert [d.display_name for d in detected] == ["MyProxy"]


@pytest.mark.parametrize(
    "label,body",
    [
        # The default provider is codex's built-in "openai" — that's the
        # subscription / API-key path, not a config-defined credential.
        (
            "builtin-provider",
            'model_provider = "openai"\n'
            '[model_providers.Custom]\n[model_providers.Custom.auth]\ncommand = "x"\n',
        ),
        # No model_provider at all → codex defaults to builtin "openai".
        (
            "no-model-provider",
            '[model_providers.Custom]\n[model_providers.Custom.auth]\ncommand = "x"\n',
        ),
        # Selected provider has no [model_providers.X] table → codex itself
        # would fail; nothing usable to adopt.
        ("missing-table", 'model_provider = "Ghost"\n'),
        # requires_openai_auth rides the ChatGPT login — auth.json territory.
        (
            "requires-openai-auth",
            'model_provider = "Custom"\n'
            "[model_providers.Custom]\n"
            "requires_openai_auth = true\n"
            '[model_providers.Custom.auth]\ncommand = "x"\n',
        ),
        # env_key-based auth is deliberately excluded: the codex executor's
        # scrubbed subprocess env would not carry the var, so adopting it
        # would 401 at run time despite detecting as configured.
        (
            "env-key-only",
            'model_provider = "Custom"\n[model_providers.Custom]\nenv_key = "CUSTOM_API_KEY"\n',
        ),
        # An auth table without a command carries no usable credential.
        (
            "auth-without-command",
            'model_provider = "Custom"\n'
            "[model_providers.Custom]\n"
            "[model_providers.Custom.auth]\n"
            "timeout_ms = 5000\n",
        ),
        # Malformed TOML must degrade to "not configured", not crash setup.
        ("malformed-toml", "model_provider = [unclosed\n"),
    ],
)
def test_codex_config_not_detected(clean_env, label: str, body: str) -> None:
    """Configs without a usable custom default provider detect nothing.

    Failure means detection adopts a provider codex itself could not
    authenticate with (or crashes on a malformed file).
    """
    _write_codex_config(clean_env, body)
    assert detect_providers() == []


@pytest.mark.parametrize(
    "label,auth_lines",
    [
        # Inline static bearer token.
        ("bearer-token", 'experimental_bearer_token = "tok-123"'),
        # AWS SigV4 request signing (Bedrock-style).
        ("aws-sigv4", '[model_providers.Custom.aws]\nregion = "us-east-1"'),
        # A static Authorization header.
        (
            "authorization-header",
            '[model_providers.Custom.http_headers]\nAuthorization = "Bearer tok"',
        ),
    ],
)
def test_codex_config_other_self_contained_auth_detected(
    clean_env, label: str, auth_lines: str
) -> None:
    """Every self-contained auth shape codex supports counts as configured.

    Failure means a provider codex can authenticate with (bearer / SigV4 /
    static auth header) is invisible to setup.
    """
    _write_codex_config(
        clean_env,
        f'model_provider = "Custom"\n[model_providers.Custom]\n{auth_lines}\n',
    )
    detected = detect_providers()
    assert [d.model_provider for d in detected] == ["Custom"]


# ── Claude on Vertex AI (GCP ADC) detection ──────────────────────────────────


def test_vertex_claude_detected_with_all_env_vars(
    clean_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All three Vertex env vars present → vertex-claude is detected.

    Claude Code natively supports Vertex AI via CLAUDE_CODE_USE_VERTEX,
    ANTHROPIC_VERTEX_PROJECT_ID, and CLOUD_ML_REGION. When all three are
    set, ambient detection must surface a vertex-claude provider so
    Omnigent recognises the credential and routes through the CLI's own
    Vertex auth (GCP ADC).
    """
    monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
    monkeypatch.setenv("ANTHROPIC_VERTEX_PROJECT_ID", "my-gcp-project")
    monkeypatch.setenv("CLOUD_ML_REGION", "us-east5")
    detected = detect_providers()
    assert detected == [
        DetectedProvider(
            name="vertex-claude",
            kind="key",
            family="anthropic",
            source="$CLAUDE_CODE_USE_VERTEX",
        )
    ]


@pytest.mark.parametrize(
    "label,env",
    [
        ("missing-use-vertex", {"ANTHROPIC_VERTEX_PROJECT_ID": "p", "CLOUD_ML_REGION": "r"}),
        ("missing-project-id", {"CLAUDE_CODE_USE_VERTEX": "1", "CLOUD_ML_REGION": "r"}),
        ("missing-region", {"CLAUDE_CODE_USE_VERTEX": "1", "ANTHROPIC_VERTEX_PROJECT_ID": "p"}),
        (
            "use-vertex-false",
            {
                "CLAUDE_CODE_USE_VERTEX": "0",
                "ANTHROPIC_VERTEX_PROJECT_ID": "p",
                "CLOUD_ML_REGION": "r",
            },
        ),
        (
            "use-vertex-blank",
            {
                "CLAUDE_CODE_USE_VERTEX": "",
                "ANTHROPIC_VERTEX_PROJECT_ID": "p",
                "CLOUD_ML_REGION": "r",
            },
        ),
    ],
)
def test_vertex_claude_not_detected_when_incomplete(
    clean_env, monkeypatch: pytest.MonkeyPatch, label: str, env: dict[str, str]
) -> None:
    """Missing or disabled Vertex env vars → no vertex-claude detection.

    All three vars must be present and CLAUDE_CODE_USE_VERTEX must be
    truthy. Failure means a partial configuration would surface a bogus
    provider that fails at run time.
    """
    for var, val in env.items():
        monkeypatch.setenv(var, val)
    assert detect_providers() == []
