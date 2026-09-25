"""Tests for harness readiness checks (``harness_readiness.py``)."""

from __future__ import annotations

import subprocess
import threading
from collections import Counter
from pathlib import Path

import pytest
import yaml

import omnigent.onboarding.harness_install as hi
from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.harness_availability import HARNESS_VERSION_TOO_LOW
from omnigent.onboarding.harness_readiness import (
    _READINESS_PROBE_MAX_WORKERS,
    configured_harness_map,
    harness_is_configured,
)


@pytest.fixture(autouse=True)
def _isolate_cli_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate CLI credential sources so their readiness is deterministic.

    Cursor readiness keys off a configured ``CURSOR_API_KEY`` and copilot off a
    GitHub token (the ``cursor:`` / ``copilot:`` config blocks or the
    environment), so point the config home at an empty tmp dir and clear any
    ambient ``CURSOR_API_KEY`` / ``COPILOT_GITHUB_TOKEN`` / ``GH_TOKEN`` /
    ``GITHUB_TOKEN`` — otherwise a developer's real key would flip their verdict
    under these tests. Antigravity similarly accepts ``GEMINI_API_KEY``.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    for var in ("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    # SDK-harness readiness checks ambient credential sources (env API keys,
    # Claude Code's login / managed settings, a Databricks workspace, GCP ADC).
    # Isolate every one of them — a developer's or CI runner's real key,
    # ~/.databrickscfg, or gcloud ADC would otherwise flip these verdicts.
    for var in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ANTIGRAVITY_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CLIENT_ID",
        "DATABRICKS_CLIENT_SECRET",
        "DATABRICKS_CONFIG_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(f"OMNIGENT_{var}", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))  # ~/.databrickscfg, gcloud ADC, …
    import omnigent.onboarding.databricks_config as _dbc
    from omnigent.onboarding import ambient as _ambient

    monkeypatch.setattr(_ambient, "_claude_login_detected", lambda: False)
    monkeypatch.setattr(
        _ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (tmp_path / "managed-absent.json",)
    )
    monkeypatch.setattr(_dbc, "list_databricks_profiles", list)
    # The default-path Databricks check reads ``_DATABRICKSCFG_PATH`` directly
    # (field-aware), and that constant captured the real home at import time —
    # point it at an absent tmp file so a developer's real ~/.databrickscfg
    # can't flip these verdicts.
    monkeypatch.setattr(_dbc, "_DATABRICKSCFG_PATH", tmp_path / "databrickscfg-absent")
    # The provider fallback check merges ambient detections the way runtime
    # resolution does (``effective_config_with_detected``); stub the live
    # detector so a developer's real env keys / CLI logins / local Ollama
    # can't flip these verdicts (tests inject detections explicitly).
    import omnigent.onboarding.detected as _detected

    monkeypatch.setattr(_detected, "detect_providers", list)
    # Copilot also accepts a ``gh auth login`` session as a token, so a developer's
    # real gh login would otherwise flip their verdict here too.
    import omnigent.onboarding.copilot_auth as _ca

    monkeypatch.setattr(_ca, "gh_cli_github_token", lambda host=None: None)
    # Codex readiness resolves the binary via resolve_cli_binary, which honors
    # an OMNIGENT_CODEX_PATH override and probes on-disk global install dirs.
    # Clear the override and stub the fallback dirs so a developer's real codex
    # install can't flip the binary-missing verdict these tests assert.
    import omnigent._platform as platform

    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setattr(platform, "_cli_fallback_dirs", lambda: ())


def _all_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear installed.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    # Follow test_harness_install.py's convention: patch the module's
    # shutil.which (reverted by monkeypatch after the test).
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    # Some harnesses (OpenCode) now validate the CLI's ``--version``. Stub a
    # satisfying version so tests that simply need "binary present" are not
    # tripped up by an unexpected subprocess probe.
    def _stub_run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            # OpenCode's declared range is [1.17.7, 1.18.0); Cursor uses calendar
            # versions and needs a build after 2026-06-01; everything else is
            # fine with a generous semver placeholder.
            if argv[0].endswith("opencode"):
                version = "1.17.7\n"
            elif argv[0].endswith("cursor-agent") or argv[0].endswith("hermes"):
                version = "2026.07.01\n"
            else:
                version = "9.9.9\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=version, stderr="")
        if argv[:3] == ["gh", "auth", "token"]:
            # Copilot readiness falls back to the ``gh`` CLI login when no token
            # is configured. Report "logged out" so these tests stay about CLI
            # presence; the fallback itself is covered in test_copilot_auth.py.
            return subprocess.CompletedProcess(args=argv, returncode=1, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess during readiness tests: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _stub_run)
    # Auth-aware native harnesses (now including Cursor native) check login state
    # in the picker map. Treat them as logged in when the test just needs
    # "binary present".
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)


def _no_clis_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every harness CLI binary appear missing.

    :param monkeypatch: The pytest monkeypatch fixture.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)


# SDK and unknown harnesses are never gated — their credentials resolve at
# runtime from ambient/spec sources the daemon can't enumerate.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-sdk",
        "claude_sdk",
        "openai-agents",
        "openai-agents-sdk",
        "agents_sdk",
        "claude",  # alias → claude-sdk
        "some-future-harness",  # unknown → fail open
    ],
)
def test_sdk_and_unknown_harnesses_are_never_gated(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """SDK / unknown harnesses are configured even with no CLI installed.

    They run in-process (or are unknown to the daemon) and resolve any
    credential at runtime, so the daemon must not block them. A ``False``
    here is a false negative that would break a launch authenticating via
    an env key, a Databricks profile, or the spec's ``executor.auth`` —
    none of which the daemon can see.
    """
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is True


# CLI-wrapping harnesses are gated on their binary being on PATH. Native Cursor
# (``omni cursor``) joins the list: it wraps the ``cursor-agent`` CLI, unlike the
# SDK ``cursor`` harness which gates on a key (covered separately below). Native
# Kiro wraps the standalone ``kiro-cli`` binary.
@pytest.mark.parametrize(
    "harness",
    [
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "pi",
        "cursor-native",
        "native-cursor",
        "kiro-native",
        "native-kiro",
        "antigravity-native",
        "native-antigravity",
        "goose-native",
        "native-goose",
        "hermes",
        # Builtin ACP CLI harnesses gate on their vendor binary; every catalog
        # row (and alias) joins automatically.
        *sorted(
            spelling
            for name, row in ACP_CLI_HARNESSES.items()
            for spelling in (name, *row.aliases)
        ),
    ],
)
def test_cli_harness_configured_only_when_binary_installed(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """A CLI-wrapping harness is configured iff its binary is on PATH.

    These harnesses cannot run without their CLI; the missing binary is
    the one thing the daemon can reliably detect. Installed → True,
    absent → False. A wrong verdict here either blocks the headline
    "I never installed Claude Code/Codex" case (if it stayed True) or
    breaks every native launch (if it stayed False).
    """
    _all_clis_installed(monkeypatch)
    if harness in {"antigravity-native", "native-antigravity"}:
        monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    assert harness_is_configured(harness) is True
    _no_clis_installed(monkeypatch)
    assert harness_is_configured(harness) is False


def test_auth_aware_native_harness_reports_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude-native / opencode-native report ``binary-missing`` when absent.

    These now carry a two-step signal in the picker map (install, then auth),
    mirroring Codex — so a missing binary is ``"binary-missing"``, not a bare
    ``False``.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    assert result["claude-native"] == "binary-missing"
    assert result["opencode-native"] == "binary-missing"


def test_auth_aware_native_harness_needs_auth_when_installed_not_signed_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Installed but not signed in AND no provider → ``needs-auth``.

    Claude is ready via a configured provider OR a CLI login; this pins the
    both-absent case. The autouse fixture points config home at an empty tmp
    dir, so no provider is configured — but stub it explicitly so the verdict
    can't depend on ambient config.
    """
    _all_clis_installed(monkeypatch)
    # claude: no provider configured AND `claude auth status` not-logged-in.
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: False
    )
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda key, **_kw: False)
    # opencode: no stored/env provider.
    import omnigent.onboarding.opencode_auth as oc

    monkeypatch.setattr(
        oc,
        "opencode_auth_summary",
        lambda: oc.OpenCodeAuthSummary(installed=True, stored_providers=(), env_providers=()),
    )
    result = configured_harness_map()
    assert result["claude-native"] == "needs-auth"
    assert result["opencode-native"] == "needs-auth"


def test_claude_ready_via_configured_provider_without_cli_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude with an omnigent provider (API key) but NO CLI login reads ready.

    A user who set an ANTHROPIC API key (a ``key``-kind provider) must go green
    even though ``claude auth status`` — the subscription login — reports
    not-logged-in. Checking the provider first also avoids the status subprocess
    on this common path.
    """
    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )

    def _must_not_probe(key: str, **_kw: object) -> bool:
        if key == "anthropic":
            raise AssertionError("Claude login probed despite a configured provider")
        return False

    monkeypatch.setattr(hi, "harness_cli_logged_in", _must_not_probe)
    assert configured_harness_map()["claude-native"] is True


def test_family_provider_configured_excludes_subscription(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``subscription``-kind default is NOT counted as a provider credential.

    Subscription auth lives in the harness CLI's own login (judged by
    ``harness_cli_logged_in``); counting it here would double-count that path
    and mask a genuine "installed but no key" state. Only non-subscription kinds
    (key/gateway/…) satisfy the provider check.
    """
    import omnigent.onboarding.harness_readiness as hrmod
    from omnigent.onboarding.provider_config import KEY_KIND, SUBSCRIPTION_KIND

    class _Provider:
        def __init__(self, kind: str) -> None:
            self.kind = kind
            # No inline families: nothing to resolve locally, so the entry
            # counts (or not) purely by kind here.
            self.families: dict[str, object] = {}

    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness.default_provider_for_harness",
        lambda _cfg, _h: _Provider(SUBSCRIPTION_KIND),
    )
    assert hrmod._family_provider_configured("claude-native") is False

    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness.default_provider_for_harness",
        lambda _cfg, _h: _Provider(KEY_KIND),
    )
    assert hrmod._family_provider_configured("claude-native") is True

    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness.default_provider_for_harness",
        lambda _cfg, _h: None,
    )
    assert hrmod._family_provider_configured("claude-native") is False


def test_auth_aware_native_harness_launch_gate_stays_binary_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The LAUNCH gate must not gain the auth check — only the picker map does.

    ``harness_is_configured`` drives whether a runner may spawn; gating it on
    login state would wrongly block a launch whose auth resolves at run time.
    So with the binary present it stays ``True`` even when not signed in.
    """
    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda key, **_kw: False)
    assert harness_is_configured("claude-native") is True
    assert harness_is_configured("opencode-native") is True


def test_configured_harness_map_covers_all_spellings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hello-frame map carries every spelling a consumer may hold.

    The server/web UI does a plain dict lookup with whatever harness
    string it has (spec executor types, canonical ids, aliases) — a
    missing key reads as "unknown" and silently disables the warning
    for that agent.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    expected_keys = {
        "claude-sdk",
        "claude-native",
        "native-claude",
        "codex",
        "codex-native",
        "native-codex",
        "openai-agents",
        "openai-agents-sdk",
        "open-responses",
        "claude_sdk",
        "agents_sdk",
        "claude",
        "pi",
        "pi-native",
        "native-pi",
        "cursor",
        # Native Cursor (``omni cursor``) — gates on the cursor-agent CLI.
        "cursor-native",
        "native-cursor",
        # Native Kiro (``omni kiro``) — gates on the kiro-cli binary.
        "kiro-native",
        "native-kiro",
        # Native Devin (``omni devin``) — gates on the devin binary. The bare
        # ``devin`` spelling canonicalizes onto ``devin-native``, so it is
        # covered too; Devin's ACP row is keyed ``devin-acp`` and gates on the
        # same binary through the catalog.
        "devin",
        "devin-native",
        # The retired builtin ACP id still resolves (aliased onto the native
        # wrap), so a consumer holding it must get a real answer, not "unknown".
        "devin-acp",
        "native-devin",
        # Goose — native TUI (``omni goose``) + headless ACP harness; both gate
        # on the goose CLI.
        "goose",
        "goose-native",
        "native-goose",
        # Antigravity SDK harness + its user-facing aliases.
        "antigravity",
        "agy",
        "google-antigravity",
        # Kimi Code CLI + alias.
        "kimi",
        "kimi-code",
        # Native Kimi (``omnigent kimi``) — gates on the kimi CLI.
        "kimi-native",
        "native-kimi",
        # Native Antigravity (agy) CLI-wrapping harness, both spellings and aliases.
        "antigravity-native",
        "native-antigravity",
        "agy-native",
        "native-agy",
        # Native OpenCode harness + its user-facing aliases.
        "opencode-native",
        "native-opencode",
        "opencode",
        # Qwen harnesses — ACP (``qwen`` / ``qwen-code``) + native TUI
        # (``qwen-native`` / ``native-qwen``); all gate on the qwen CLI.
        "qwen",
        "qwen-code",
        "qwen-native",
        "native-qwen",
        # Copilot SDK harness + its user-facing alias.
        "copilot",
        "github-copilot",
        # Hermes — headless subprocess harness (``hermes``) + native TUI
        # (``hermes-native`` / ``native-hermes``); all gate on the hermes CLI.
        "hermes",
        "hermes-native",
        "native-hermes",
        # Generic ACP harness — config-gated (≥1 agent in the acp: block), no CLI
        # binary of its own; the acp:<slug> picks are config-derived, not keyed here.
        "acp",
        # Builtin ACP CLI harnesses: every catalog row + alias, derived so a new
        # row never needs to touch this list.
        *(
            spelling
            for name, row in ACP_CLI_HARNESSES.items()
            for spelling in (name, *row.aliases)
        ),
    }
    assert set(result) == expected_keys


def test_configured_harness_map_gates_only_cli_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no CLI installed, spellings classify onto the right readiness axis.

    SDK spellings (incl. the ``openai-agents-sdk`` workflow spelling and the
    ``claude`` alias) have no binary to miss — with no credential visible
    either they read the credential axis (``needs-auth``), never a
    binary-shaped ``False``. The native + pi spellings flip on the binary. A
    misclassified spelling would warn the wrong agents in the picker.
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    # SDK / alias spellings — no binary axis; with nothing configured they
    # read the credential axis. (An SDK agent authenticating via a visible
    # source — an env key, a Databricks profile — reads True; covered below.)
    for sdk in (
        "claude-sdk",
        "claude_sdk",
        "claude",
        "openai-agents",
        "openai-agents-sdk",
        "agents_sdk",
    ):
        assert result[sdk] == "needs-auth", f"{sdk} should read the credential axis"
    # CLI-wrapping spellings — gated, so False when the binary is absent.
    # (The SDK ``cursor`` harness is excluded: it runs via the ``cursor-sdk``
    # package and gates on a configured ``CURSOR_API_KEY``, not a binary —
    # covered separately. Native Cursor (``cursor-native`` / ``native-cursor``)
    # wraps the ``cursor-agent`` CLI, so it IS gated on the binary.)
    # antigravity-native is also gated (it wraps the ``agy`` CLI); with no
    # binary it reads False before its credential check is even reached.
    for cli in (
        "kimi",
        "kiro-native",
        "native-kiro",
        "antigravity-native",
        "native-antigravity",
        "goose-native",
        "native-goose",
        "qwen",
        "hermes",
        *sorted(ACP_CLI_HARNESSES),
    ):
        assert result[cli] is not True, f"{cli} should be gated on its CLI binary"
    # Auth-aware harnesses (codex, claude, opencode, cursor, pi) carry a
    # two-step signal in the picker map, so a missing binary is the structured
    # ``"binary-missing"`` (step 1 to-do), not a bare ``False``. Cursor joined
    # this group — it is now auth-aware like the other native CLI harnesses, so
    # its missing binary surfaces as ``"binary-missing"`` too. Pi is also here —
    # it reports the credential axis (no CLI login; its credential is a provider).
    for missing in (
        "codex",
        "codex-native",
        "native-codex",
        "claude-native",
        "native-claude",
        "opencode-native",
        "cursor-native",
        "native-cursor",
        "pi",
        "pi-native",
    ):
        assert result[missing] == "binary-missing", f"{missing} should name the missing CLI binary"


def test_copilot_ready_via_gh_cli_login_without_stored_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``gh auth login`` session alone makes copilot ready.

    Without this, a logged-in user is told to paste a token that ``gh`` already
    holds — and on macOS ``gh`` keeps it in the keychain, where the Copilot CLI
    (which only reads ``oauth_token`` out of ``hosts.yml``) can't see it.
    """
    import omnigent.onboarding.copilot_auth as _ca

    _all_clis_installed(monkeypatch)
    assert configured_harness_map()["copilot"] is False

    monkeypatch.setattr(_ca, "gh_cli_github_token", lambda host=None: "gho_from_gh")
    assert configured_harness_map()["copilot"] is True


def test_configured_harness_map_all_true_with_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every spelling reads True once the CLIs are installed and the key/token-
    gated harnesses are satisfied.

    The CLI harnesses pass their binary check, the SDK harnesses are ungated,
    cursor (key-gated) is satisfied by a ``CURSOR_API_KEY``, copilot
    (token-gated) by a ``GH_TOKEN``, antigravity-native (binary + credential
    gated) by a detected Gemini OAuth credential, kimi (binary + credential
    gated) by a detected ``kimi login`` credential or a Kimi API key in
    ``~/.kimi-code/config.toml``, and the generic ACP harness (config-gated) by
    a registered agent — so nothing is reported unconfigured.
    """
    import omnigent.onboarding.gemini_auth as _ga
    import omnigent.onboarding.kimi_auth as _ka

    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main._codex_auth_unavailable_reason",
        lambda: None,
    )
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_ready")
    # antigravity-native also needs a credential (not just the ``agy`` binary).
    monkeypatch.setattr(_ga, "gemini_login_detected", lambda: True)
    # kimi also needs a credential (not just the ``kimi`` binary).
    monkeypatch.setattr(_ka, "kimi_auth_configured", lambda: True)
    monkeypatch.setenv("GH_TOKEN", "gho_ready")
    # claude / pi are auth-aware on the credential axis now: satisfy the provider
    # check deterministically (don't depend on the dev machine's real config).
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )
    # The generic ACP harness is config-gated (≥1 registered agent), not
    # CLI-gated — satisfy it so it isn't the lone unconfigured entry here.
    monkeypatch.setattr("omnigent.onboarding.acp_auth.acp_agents", lambda config=None: [object()])
    # antigravity (SDK) needs a visible Gemini credential (not the openai
    # family the other SDK harnesses share).
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    result = configured_harness_map()
    not_ready = {k: v for k, v in result.items() if v is not True}
    assert not not_ready, f"expected every spelling ready, got {not_ready}"


def test_configured_harness_map_probes_codex_readiness_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex aliases share one potentially expensive readiness probe."""
    calls = 0

    def _codex_reason() -> str:
        nonlocal calls
        calls += 1
        return "needs-auth"

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main._codex_auth_unavailable_reason",
        _codex_reason,
    )

    result = configured_harness_map()

    assert calls == 1
    assert result["codex"] == "needs-auth"
    assert result["codex-native"] == "needs-auth"
    assert result["native-codex"] == "needs-auth"


def test_configured_harness_map_runs_independent_probes_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Independent readiness probes overlap without duplicating aliases."""
    lock = threading.Lock()
    release = threading.Event()
    active = 0
    max_active = 0
    calls: Counter[str] = Counter()

    def _availability(canonical: str) -> bool | str:
        nonlocal active, max_active
        with lock:
            calls[canonical] += 1
            active += 1
            max_active = max(max_active, active)
            if active >= _READINESS_PROBE_MAX_WORKERS:
                release.set()
        if not release.wait(timeout=1.0):
            raise AssertionError("readiness probes did not overlap")
        with lock:
            active -= 1
        if "codex" in canonical:
            return "needs-auth"
        return canonical != "claude-native"

    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._harness_availability",
        _availability,
    )

    result = configured_harness_map()

    assert result
    assert max_active == _READINESS_PROBE_MAX_WORKERS
    assert all(count == 1 for count in calls.values())
    assert result["claude-native"] is False
    assert result["native-claude"] is True
    assert result["codex"] == "needs-auth"
    assert result["codex-native"] == "needs-auth"
    assert result["native-codex"] == "needs-auth"


def test_kimi_readiness_keys_off_binary_and_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kimi is configured iff the ``kimi`` binary is on PATH AND auth exists.

    Kimi authenticates against Moonshot AI's backend via ``kimi login`` (OAuth,
    membership) or a Moonshot API key in ``~/.kimi-code/config.toml``
    (pay-per-use). Like agy, the daemon has no CLI login-status probe, so
    readiness is binary presence PLUS a subprocess-free credential check
    (``kimi_auth_configured``). The alias ``kimi-code`` resolves to the same
    verdict via canonicalization.
    """
    import omnigent.onboarding.kimi_auth as _ka

    # No binary → not configured regardless of credential.
    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(_ka, "kimi_auth_configured", lambda: True)
    assert harness_is_configured("kimi") is False
    assert harness_is_configured("kimi-code") is False

    # Binary present but no auth → still not configured.
    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(_ka, "kimi_auth_configured", lambda: False)
    assert harness_is_configured("kimi") is False
    assert harness_is_configured("kimi-code") is False

    # Binary present and auth detected → configured.
    monkeypatch.setattr(_ka, "kimi_auth_configured", lambda: True)
    assert harness_is_configured("kimi") is True
    assert harness_is_configured("kimi-code") is True


def test_cursor_readiness_keys_off_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cursor is configured iff a ``CURSOR_API_KEY`` is resolvable — not a binary.

    The cursor harness runs via the always-present ``cursor-sdk`` package, so
    its readiness ignores the ``cursor-agent`` binary entirely: no key → not
    configured (even with every CLI installed); an env key or a stored
    ``cursor:`` block → configured (even with no CLI at all). A wrong verdict
    would either warn a key-configured cursor user "needs setup" or greenlight a
    keyless one that fails at the first turn.
    """
    # No key anywhere (autouse isolation), even with all CLIs present → False.
    _all_clis_installed(monkeypatch)
    assert harness_is_configured("cursor") is False

    # An inherited environment key satisfies it, with no CLI installed.
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_from_env")
    assert harness_is_configured("cursor") is True

    # A key stored in the ``cursor:`` config block also satisfies it.
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    monkeypatch.setenv("MY_CURSOR_KEY", "crsr_from_config")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"cursor": {"api_key_ref": "env:MY_CURSOR_KEY"}})
    )
    assert harness_is_configured("cursor") is True


def test_native_cursor_keys_off_binary_not_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native Cursor (``omni cursor``) gates on the cursor-agent CLI, not a key.

    The mirror image of :func:`test_cursor_readiness_keys_off_api_key`: native
    Cursor boots the ``cursor-agent`` TUI, so its readiness is the binary on
    ``PATH`` — a ``CURSOR_API_KEY`` (which configures the SDK ``cursor`` harness)
    does not make it launchable. Conflating the two would tell a native-Cursor
    user with a key set "you're ready" and then die booting a CLI that isn't
    installed.
    """
    # A key set but no binary → not configured (the SDK key doesn't help here).
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("CURSOR_API_KEY", "crsr_from_env")
    assert harness_is_configured("cursor-native") is False
    assert harness_is_configured("native-cursor") is False

    # Binary present → configured, even with no key.
    _all_clis_installed(monkeypatch)
    monkeypatch.delenv("CURSOR_API_KEY", raising=False)
    assert harness_is_configured("cursor-native") is True
    assert harness_is_configured("native-cursor") is True


def test_configured_harness_map_reports_version_too_low_for_outdated_clis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An outdated CLI for major native harnesses is flagged ``version-too-low``.

    This exercises the readiness-layer wiring, which is where the binary is
    on ``PATH`` but does not satisfy the declared ``min_version`` of the spec.
    The core promise of the feature is that users see an upgrade prompt instead
    of being told the binary is missing.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hi, "harness_cli_installed", lambda _key, **_kw: False)
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: True)
    result = configured_harness_map()
    for harness in (
        "claude-native",
        "native-claude",
        "opencode-native",
        "native-opencode",
        "cursor-native",
        "native-cursor",
        "kiro-native",
        "native-kiro",
    ):
        assert result[harness] == HARNESS_VERSION_TOO_LOW, (
            f"{harness} should report version-too-low, not binary-missing"
        )


def test_antigravity_native_requires_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``antigravity-native`` needs both the ``agy`` binary and a credential."""
    import omnigent.onboarding.gemini_auth as _ga

    _all_clis_installed(monkeypatch)
    # Binary installed but no credential → not ready.
    monkeypatch.setattr(_ga, "gemini_login_detected", lambda: False)
    assert harness_is_configured("antigravity-native") is False
    assert harness_is_configured("native-antigravity") is False
    # Stored credential present → ready.
    monkeypatch.setattr(_ga, "gemini_login_detected", lambda: True)
    assert harness_is_configured("antigravity-native") is True
    assert harness_is_configured("native-antigravity") is True


def test_claude_ready_via_managed_gateway_without_provider_or_cli_login(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude reads ready from its own managed-settings gateway alone.

    The enterprise state (`isaac configure claude`): nothing in omnigent's
    config, no subscription login the probe can see, but Claude Code's managed
    settings pin an AI Gateway + apiKeyHelper and the CLI applies them itself.
    This is the exact "Claude Code isn't configured on <host>" dead end — the
    structural check must go green WITHOUT the `claude auth status` subprocess.
    """
    import json

    from omnigent.onboarding import ambient

    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: False
    )
    # The subprocess probe stays broken; readiness must not depend on it.
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda key, **_kw: False)
    settings = tmp_path / "managed-settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://dbc.cloud.databricks.com/ai-gateway/anthropic"
                },
                "apiKeyHelper": "print-token",
            }
        )
    )
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (settings,))

    result = configured_harness_map()
    assert result["claude-native"] is True
    assert result["native-claude"] is True


def test_claude_needs_auth_without_gateway_provider_or_login(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No managed gateway + no provider + no login → still `needs-auth`.

    Guards the structural check from going green on nothing: an absent settings
    file must not credit a credential that isn't there.
    """
    from omnigent.onboarding import ambient

    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: False
    )
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda key, **_kw: False)
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (tmp_path / "absent.json",))

    assert configured_harness_map()["claude-native"] == "needs-auth"


def test_sdk_harness_needs_auth_with_no_visible_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A credential-less host reports the SDK harnesses as ``needs-auth``.

    The headline picker bug: the in-process SDK harnesses were hardcoded
    ready, so a host with no resolvable credential offered them with no
    warning and the launch died at the first turn. The map now reads the
    credential axis — while the launch gate stays ungated (the warning
    informs, it never blocks).
    """
    _no_clis_installed(monkeypatch)
    result = configured_harness_map()
    for sdk in (
        "claude-sdk",
        "claude_sdk",
        "claude",
        "openai-agents",
        "openai-agents-sdk",
        "agents_sdk",
        "antigravity",
    ):
        assert result[sdk] == "needs-auth", f"{sdk} should read needs-auth"
        assert harness_is_configured(sdk) is True, f"{sdk} launch gate must stay ungated"


def test_sdk_harness_ready_via_configured_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured family provider entry alone makes the SDK harnesses ready."""
    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True
    assert result["openai-agents-sdk"] is True


def test_sdk_harness_ready_via_family_env_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient family API key readies only that family's SDK harnesses."""
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] == "needs-auth"

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert result["openai-agents"] is True
    assert result["openai-agents-sdk"] is True


def test_claude_sdk_ready_via_claude_code_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude Code's own login serves claude-sdk (the SDK drives that CLI)."""
    from omnigent.onboarding import ambient

    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(ambient, "_claude_login_detected", lambda: True)
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] == "needs-auth"


def test_claude_sdk_ready_via_managed_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Claude Code's managed-settings gateway alone makes claude-sdk ready."""
    import json

    from omnigent.onboarding import ambient

    _no_clis_installed(monkeypatch)
    settings = tmp_path / "managed-settings.json"
    settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "https://dbc.cloud.databricks.com/ai-gateway/anthropic"
                },
                "apiKeyHelper": "print-token",
            }
        )
    )
    monkeypatch.setattr(ambient, "CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (settings,))
    assert configured_harness_map()["claude-sdk"] is True


def test_sdk_harness_ready_via_databricks_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ambient Databricks *credential* counts as an SDK credential source.

    The SDK executors mint a gateway bearer from ``~/.databrickscfg`` / the
    Databricks env for ``databricks-*`` models even with no provider entry, so
    a host configured with a credentialed profile, ``DATABRICKS_HOST`` plus
    auth material, or a credentialed-profile ``DATABRICKS_CONFIG_FILE`` must
    not read ``needs-auth``.
    """
    import omnigent.onboarding.databricks_config as dbc

    _no_clis_installed(monkeypatch)
    default_cfg = tmp_path / "databrickscfg-default"
    default_cfg.write_text(
        "[DEFAULT]\nhost = https://example.cloud.databricks.com\ntoken = dapi-test\n"
    )
    monkeypatch.setattr(dbc, "_DATABRICKSCFG_PATH", default_cfg)
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True
    monkeypatch.setattr(dbc, "_DATABRICKSCFG_PATH", tmp_path / "databrickscfg-absent")

    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-test-token")
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True

    monkeypatch.delenv("DATABRICKS_HOST")
    monkeypatch.delenv("DATABRICKS_TOKEN")
    config_file = tmp_path / "databrickscfg-override"
    config_file.write_text("[work]\nhost = https://example.cloud.databricks.com\ntoken = t\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True

    # An externally resolved method (its material lives in the databricks
    # CLI's own OAuth token cache, not this file) counts on declaration.
    config_file.write_text(
        "[work]\nhost = https://example.cloud.databricks.com\nauth_type = databricks-cli\n"
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True


def test_databricks_workspace_url_alone_is_not_a_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``DATABRICKS_HOST`` without auth material must read ``needs-auth``.

    A workspace URL names where to authenticate, not a way to authenticate:
    with no token, OAuth pair, profile, or other local source, the executors
    cannot mint a gateway bearer, so readiness must not report the SDK
    harnesses ready off the URL alone.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("DATABRICKS_HOST", "https://example.cloud.databricks.com")
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert result["openai-agents"] == "needs-auth"


def test_sdk_harness_ready_via_global_auth_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user-level global ``auth:`` block counts as an SDK credential source.

    Both SDK builders inherit the global ``config.yaml`` ``auth:`` mapping when
    the agent spec declares no ``executor.auth`` (workflow's
    ``_load_global_auth``), so a host configured only through global API-key
    authentication must not read ``needs-auth`` (which the picker renders as
    an unselectable row).
    """
    _no_clis_installed(monkeypatch)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "api_key", "api_key": "sk-global-test"}})
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    assert result["openai-agents"] is True


@pytest.mark.parametrize("token_var", ["CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN"])
def test_claude_sdk_ready_via_ambient_claude_token_env(
    monkeypatch: pytest.MonkeyPatch,
    token_var: str,
) -> None:
    """Ambient Claude token env credentials count for claude-sdk readiness.

    The host forwards ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token``
    subscription auth) and ``ANTHROPIC_AUTH_TOKEN`` (gateway bearer, paired
    with ``ANTHROPIC_BASE_URL``) to its runners, and Claude Code resolves them
    directly, so a token-only host must not read ``needs-auth``.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv(token_var, "token-test-value")
    if token_var == "ANTHROPIC_AUTH_TOKEN":
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example.com")
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    # The token serves only the anthropic family; openai-agents stays warned.
    assert result["openai-agents"] == "needs-auth"


def test_sdk_harness_ready_via_non_default_family_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured non-default family provider counts as an SDK credential.

    Launch resolution falls back to the first provider entry serving the
    family when no default is configured (``first_available_provider``,
    consumed with ``for_launch=True`` in the runtime), so readiness must not
    report ``needs-auth`` for a host whose only credential is a non-default
    provider entry serving the harness's family — provided its credential
    reference actually resolves locally.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("WORK_ANTHROPIC_KEY", "sk-work-test-key")
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "work-anthropic": {
                        "kind": "key",
                        "anthropic": {
                            "base_url": "https://api.example.com/v1",
                            "api_key_ref": "env:WORK_ANTHROPIC_KEY",
                        },
                    }
                }
            }
        )
    )
    result = configured_harness_map()
    assert result["claude-sdk"] is True
    # No provider serves the openai family here, so openai-agents must still
    # read needs-auth - the fallback is per-family, not a global pass.
    assert result["openai-agents"] == "needs-auth"


@pytest.mark.parametrize(
    ("family", "harness", "other_harness"),
    [
        ("anthropic", "claude-sdk", "openai-agents"),
        ("openai", "openai-agents", "claude-sdk"),
    ],
)
@pytest.mark.parametrize("default", [False, True])
def test_sdk_provider_with_unresolved_credential_reference_is_not_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    harness: str,
    other_harness: str,
    default: bool,
) -> None:
    """An unresolved ``api_key_ref`` must not report the SDK harness ready.

    Launch resolves the selected entry's secret through
    ``ProviderEntry.family()`` / ``resolve_secret`` and fails rather than
    skipping the provider, so an entry pointing at an unset ``env:`` variable
    is a first-turn auth failure, not a credential — readiness must warn
    instead of reporting ready. Covers both SDK harnesses, through both the
    default-provider and first-available fallback selection paths.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.delenv("MISSING_READINESS_TEST_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_MISSING_READINESS_TEST_KEY", raising=False)
    entry: dict[str, object] = {
        "kind": "key",
        family: {
            "base_url": "https://api.example.com/v1",
            "api_key_ref": "env:MISSING_READINESS_TEST_KEY",
        },
    }
    if default:
        entry["default"] = True
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"work": entry}}))
    result = configured_harness_map()
    assert result[harness] == "needs-auth"
    assert result[other_harness] == "needs-auth"


def test_openai_agents_ready_via_detected_local_ollama(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable local Ollama counts as an openai-family credential source.

    Runtime resolution merges ambient detections
    (``effective_config_with_detected``) and can select a reachable, keyless
    local Ollama as its OpenAI-family provider, so an Ollama-only host must
    not warn ``needs-auth`` for openai-agents.
    """
    import omnigent.onboarding.detected as detected_mod
    from omnigent.onboarding.ambient import DetectedProvider

    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(
        detected_mod,
        "detect_providers",
        lambda: [
            DetectedProvider(
                name="ollama",
                kind="local",
                family="openai",
                source="http://localhost:11434",
            )
        ],
    )
    result = configured_harness_map()
    assert result["openai-agents"] is True
    # Ollama serves only the openai family; claude-sdk stays warned.
    assert result["claude-sdk"] == "needs-auth"


def test_codex_cli_config_provider_does_not_ready_sdk_harnesses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A codex ``cli-config`` provider is not an SDK credential source.

    Launch rejects kind ``cli-config`` for anything but the codex CLI harness
    (the spawn-env builder fails loud: the provider table + credential live in
    ``~/.codex/config.toml``, which only that CLI reads), so a host whose only
    openai-family source is a codex config.toml provider must keep warning for
    openai-agents rather than report ready for a source launch would reject.
    """
    import omnigent.onboarding.detected as detected_mod
    from omnigent.onboarding.ambient import DetectedProvider

    _no_clis_installed(monkeypatch)
    monkeypatch.setattr(
        detected_mod,
        "detect_providers",
        lambda: [
            DetectedProvider(
                name="codex-databricks",
                kind="cli-config",
                family="openai",
                source="~/.codex/config.toml",
                model_provider="Databricks",
                display_name="Databricks AI Gateway",
            )
        ],
    )
    result = configured_harness_map()
    assert result["openai-agents"] == "needs-auth"
    assert result["claude-sdk"] == "needs-auth"


def test_claude_native_unresolved_provider_reference_reports_needs_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native-harness path also rejects unresolvable provider references.

    ``_family_provider_configured`` is shared with the auth-aware native
    harnesses: claude-native with a default provider whose ``api_key_ref``
    points at an unset env var (and no CLI login) must read ``needs-auth``,
    not ready.
    """
    _all_clis_installed(monkeypatch)
    monkeypatch.setattr(hi, "harness_cli_logged_in", lambda _key, **_kw: False)
    monkeypatch.delenv("MISSING_READINESS_TEST_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_MISSING_READINESS_TEST_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "work": {
                        "kind": "key",
                        "default": True,
                        "anthropic": {
                            "base_url": "https://api.example.com/v1",
                            "api_key_ref": "env:MISSING_READINESS_TEST_KEY",
                        },
                    }
                }
            }
        )
    )
    assert configured_harness_map()["claude-native"] == "needs-auth"


def test_malformed_databricks_config_contents_never_reach_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Parser errors for Databricks config files must not log file contents.

    ``configparser`` errors embed the offending line, which in a credentials
    file may be a token, so both the default ``~/.databrickscfg`` reader and
    the ``DATABRICKS_CONFIG_FILE`` override parser must log only the exception
    class when a file is malformed.
    """
    import importlib
    import logging

    import omnigent.onboarding.databricks_config as dbc

    _no_clis_installed(monkeypatch)
    secret = "dapi-super-secret-value"
    # A token line before any section header makes configparser raise a
    # MissingSectionHeaderError whose message embeds the line itself.
    malformed = f"token = {secret}\n[DEFAULT]\nhost = https://example\n"
    default_cfg = tmp_path / "databrickscfg-default"
    default_cfg.write_text(malformed)
    override_cfg = tmp_path / "databrickscfg-override"
    override_cfg.write_text(malformed)
    # The autouse fixture stubs list_databricks_profiles; reload to exercise
    # the real parser, then pin its path at this test's malformed file.
    importlib.reload(dbc)
    monkeypatch.setattr(dbc, "_DATABRICKSCFG_PATH", default_cfg)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(override_cfg))
    with caplog.at_level(logging.DEBUG):
        result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert secret not in caplog.text


def test_list_databricks_profiles_malformed_contents_never_reach_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``list_databricks_profiles`` itself logs only the exception class.

    Direct coverage for the function's redaction behavior (the readiness test
    above exercises readiness's own parser): a malformed ``~/.databrickscfg``
    whose offending line embeds a token must produce an empty profile list and
    a log record that never carries the file's contents.
    """
    import importlib
    import logging

    import omnigent.onboarding.databricks_config as dbc

    secret = "dapi-super-secret-value"
    # A token line before any section header makes configparser raise a
    # MissingSectionHeaderError whose message embeds the line itself.
    cfg = tmp_path / "databrickscfg-malformed-direct"
    cfg.write_text(f"token = {secret}\n[DEFAULT]\nhost = https://example\n")
    # The autouse fixture stubs list_databricks_profiles; reload to exercise
    # the real parser, then pin its path at this test's malformed file.
    importlib.reload(dbc)
    monkeypatch.setattr(dbc, "_DATABRICKSCFG_PATH", cfg)
    with caplog.at_level(logging.DEBUG):
        assert dbc.list_databricks_profiles() == []
    assert secret not in caplog.text


def test_databricks_profileless_config_override_is_not_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``DATABRICKS_CONFIG_FILE`` that declares no profile is not a credential.

    Mere existence of the override file (including an empty one) proves no
    authentication source, so readiness must still read ``needs-auth``.
    """
    _no_clis_installed(monkeypatch)
    empty_config = tmp_path / "databrickscfg-empty"
    empty_config.write_text("")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(empty_config))
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert result["openai-agents"] == "needs-auth"


@pytest.mark.parametrize(
    "contents",
    [
        # A bare section supplies neither a workspace nor authentication.
        "[work]\n",
        # A workspace host alone names where to authenticate, not how.
        "[work]\nhost = https://example.cloud.databricks.com\n",
        # Auth material without a workspace cannot mint a gateway bearer.
        "[work]\ntoken = dapi-test\n",
        # An explicit method declaration without that method's material: PAT
        # authentication requires the (missing) token.
        "[work]\nhost = https://example.cloud.databricks.com\nauth_type = pat\n",
        # Same for an OAuth service-principal declaration without its pair.
        "[work]\nhost = https://example.cloud.databricks.com\nauth_type = oauth-m2m\n",
    ],
)
def test_databricks_uncredentialed_profile_is_not_a_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    contents: str,
) -> None:
    """A Databricks profile without host + auth fields is not a credential.

    Readiness checks the locally required configuration fields, not section
    existence: a ``[work]`` section that carries no workspace host plus
    authentication material (token / OAuth pair / auth_type) supplies nothing
    an executor could mint a gateway bearer from, so the SDK harnesses must
    still read ``needs-auth``. Applies to both the ``DATABRICKS_CONFIG_FILE``
    override and the default ``~/.databrickscfg``.
    """
    import omnigent.onboarding.databricks_config as dbc

    _no_clis_installed(monkeypatch)
    config_file = tmp_path / "databrickscfg-uncredentialed"
    config_file.write_text(contents)

    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config_file))
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert result["openai-agents"] == "needs-auth"

    monkeypatch.delenv("DATABRICKS_CONFIG_FILE")
    monkeypatch.setattr(dbc, "_DATABRICKSCFG_PATH", config_file)
    result = configured_harness_map()
    assert result["claude-sdk"] == "needs-auth"
    assert result["openai-agents"] == "needs-auth"


def test_antigravity_sdk_readiness_keys_off_gemini_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """antigravity (SDK) is ready only via a Gemini-native credential.

    Its spawn env resolves a stored ``antigravity:`` key, an ambient
    ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``, or Vertex AI (ADC) — never
    the openai family the other SDK harnesses consume — so an openai key or
    provider entry must not flip it.
    """
    _no_clis_installed(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setattr(
        "omnigent.onboarding.harness_readiness._family_provider_configured", lambda _h: True
    )
    assert configured_harness_map()["antigravity"] == "needs-auth"

    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    assert configured_harness_map()["antigravity"] is True

    monkeypatch.delenv("GEMINI_API_KEY")
    adc = tmp_path / "adc.json"
    # An ADC file with no credential material (``{}``) proves nothing —
    # existence alone must not flip readiness.
    adc.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(adc))
    assert configured_harness_map()["antigravity"] == "needs-auth"

    import json

    adc.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": "test-client-id",
                "client_secret": "test-client-secret",
                "refresh_token": "test-refresh-token",
            }
        )
    )
    assert configured_harness_map()["antigravity"] is True
