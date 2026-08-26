"""Tests for :mod:`omnigent.onboarding.harness_install`."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import omnigent._platform as _platform
from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GEMINI_FAMILY, OPENAI_FAMILY


@pytest.fixture(autouse=True)
def _stub_cli_fallback_dirs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reduce ``resolve_cli_binary`` to a pure ``PATH`` probe under test.

    ``harness_cli_installed`` / ``missing_harness_cli`` resolve via
    ``resolve_cli_binary``, which also probes on-disk global install dirs
    (``~/.local/bin``, nvm, …). Tests here stub ``shutil.which`` to simulate a
    binary's presence/absence; stub the fallback dirs to empty too so a
    developer's real claude/codex install can't flip a ``which``-returns-None
    assertion.

    Also stub ``--version`` probes so tests that simply need "binary present"
    are not tripped up by an unexpected subprocess call once a harness spec
    declares a version floor. Tests that care about the version can override
    the stub explicitly.
    """
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: ())

    def _stub_version_run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="9.9.9\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess in harness_install tests: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _stub_version_run)


@pytest.mark.parametrize(
    "key,binary,package",
    [
        (OPENAI_FAMILY, "codex", "@openai/codex"),
        (hi.PI_KEY, "pi", "@earendil-works/pi-coding-agent"),
        (hi.QWEN_KEY, "qwen", "@qwen-code/qwen-code"),
    ],
)
def test_install_spec_and_command(key: str, binary: str, package: str) -> None:
    """Each npm-installed harness maps to the ucode-matching binary + package.

    A drift in binary/package (e.g. a wrong npm name) would install the wrong
    thing or check the wrong PATH entry — caught here.
    """
    spec = hi.harness_install_spec(key)
    assert spec is not None
    assert spec.binary == binary
    assert spec.package == package
    assert hi.harness_install_command(key) == ["npm", "install", "-g", package]


def test_claude_installs_via_anthropic_native_installer() -> None:
    """Claude ships via Anthropic's installer, not ``npm install -g``.

    ``package`` must stay ``None``: that is the flag :func:`harness_setup_hint`
    and the runner's missing-CLI error branch on to name the vendor installer.
    """
    spec = hi.harness_install_spec(ANTHROPIC_FAMILY)
    assert spec is not None
    assert spec.binary == "claude"
    assert spec.package is None
    assert spec.install_hint == "curl -fsSL https://claude.ai/install.sh | bash"
    assert hi.harness_install_command(ANTHROPIC_FAMILY) == [
        "bash",
        "-c",
        spec.install_hint,
    ]


@pytest.mark.parametrize(
    "key,expected",
    [
        (ANTHROPIC_FAMILY, "curl -fsSL https://claude.ai/install.sh | bash"),
        (OPENAI_FAMILY, "npm install -g @openai/codex"),
    ],
)
def test_install_display_hides_the_bash_c_wrapper(key: str, expected: str) -> None:
    """The command shown to a user is runnable as-is, without the ``bash -c``
    wrapper :func:`harness_install_command` adds for ``subprocess``."""
    assert hi.harness_install_display(key) == expected


def test_claude_setup_hint_names_the_native_installer() -> None:
    """A machine missing the claude CLI is pointed at the working installer."""
    hint = hi.harness_setup_hint("claude-native")
    assert "claude.ai/install.sh" in hint
    assert "npm" not in hint
    assert "claude auth login --claudeai" in hint


def test_kimi_install_spec_is_login_only_no_npm() -> None:
    """Kimi ships via a curl installer (no npm package) and authenticates
    through its own ``kimi login`` (OAuth or Moonshot API key), so it carries
    an ``install_hint`` instead of a ``package`` and intentionally has no
    ``status_args`` (no exit-code "am I logged in?" probe to read). It has no
    ``kimi logout`` subcommand (verified against kimi CLI v0.29.1), so
    ``logout_args`` is ``None`` and ``harness_logout`` is a no-op for it.
    """
    spec = hi.harness_install_spec(hi.KIMI_KEY)
    assert spec is not None
    assert spec.binary == "kimi"
    assert spec.package is None
    assert spec.install_hint is not None and "code.kimi.com" in spec.install_hint
    assert spec.login_args == ("login",)
    assert spec.logout_args is None
    assert spec.status_args is None


def test_kimi_required_cli_returns_install_spec() -> None:
    """The kimi harness is binary-gated: it cannot launch without ``kimi`` on
    PATH, so the sub-agent dispatch preflight must surface the install spec."""
    spec = hi.required_cli_for_harness("kimi")
    assert spec is not None
    assert spec.binary == "kimi"


@pytest.mark.parametrize(
    "harness",
    ["antigravity-native", "native-antigravity", "agy-native", "native-agy"],
)
def test_antigravity_native_aliases_require_agy_cli(
    monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    """Every native agy spelling fails early when its CLI is unavailable."""
    monkeypatch.setattr(hi.shutil, "which", lambda _name: None)
    spec = hi.required_cli_for_harness(harness)
    assert spec is not None
    assert spec.binary == "agy"
    assert hi.missing_harness_cli(harness) == spec


def test_kimi_only_upstream_binary_satisfies_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only ``kimi`` (the upstream MoonshotAI/Kimi-Code binary) counts as
    installed. The legacy pypi ``kimi-cli`` package is intentionally NOT
    accepted — its command-line surface is incompatible with what the
    executor drives, so falsely reading it as configured would crash at
    the first turn."""
    monkeypatch.setattr(
        hi.shutil,
        "which",
        lambda name: "/Users/x/.local/bin/kimi-cli" if name == "kimi-cli" else None,
    )
    assert hi.harness_cli_installed(hi.KIMI_KEY) is False

    monkeypatch.setattr(
        hi.shutil,
        "which",
        lambda name: "/Users/x/.kimi-code/bin/kimi" if name == "kimi" else None,
    )
    assert hi.harness_cli_installed(hi.KIMI_KEY) is True


def test_cli_probe_timeout_defaults_lenient_but_readiness_passes_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The readiness caller can shorten the probe subprocess timeout.

    A wedged harness CLI must not stall the throttled readiness refresh for the
    lenient default (30s); readiness passes ``READINESS_CLI_PROBE_TIMEOUT_S`` so
    the ``auth status`` probe fails fast. Direct callers (setup / launch) keep
    the 30s default.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    recorded: list[float | None] = []

    def _record_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(kwargs.get("timeout"))  # type: ignore[arg-type]
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout='{"loggedIn": true}', stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _record_run)

    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert recorded[-1] == 30.0

    # The positive verdict above is now TTL-cached; drop it so the second
    # call actually probes (this test is about timeout plumbing, and the
    # cache behavior has its own tests below).
    hi._LOGIN_PROBE_CACHE.clear()
    assert (
        hi.harness_cli_logged_in(ANTHROPIC_FAMILY, timeout=hi.READINESS_CLI_PROBE_TIMEOUT_S)
        is True
    )
    assert recorded[-1] == hi.READINESS_CLI_PROBE_TIMEOUT_S == 10.0


def test_login_probe_caches_positive_verdicts_with_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A logged-in verdict is served from cache until its TTL expires.

    Readiness refreshes across every host daemon exec ``auth status``
    per pass; without the cache that compounds into a constant ambient
    subprocess storm on machines with many idle hosts.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    runs: list[list[str]] = []

    def _positive_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout='{"loggedIn": true}', stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _positive_run)

    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert len(runs) == 1, "second call within the TTL must be cache-served"

    # Expire the entry: the next call must probe again.
    for cache_key in hi._LOGIN_PROBE_CACHE:
        hi._LOGIN_PROBE_CACHE[cache_key] = 0.0
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert len(runs) == 2


def test_login_probe_never_caches_negative_verdicts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not-logged-in must re-probe every call.

    The setup wizard confirms a just-completed login via this function; a
    cached negative would report the fresh login as failed for the TTL.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    runs: list[list[str]] = []

    def _negative_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout='{"loggedIn": false}', stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _negative_run)

    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is False
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is False
    assert len(runs) == 2, "negative verdicts must never be cache-served"


def test_logout_invalidates_login_probe_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful logout is confirmed live, not from the cached positive."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/local/bin/{name}")
    logged_in = True

    def _stateful_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal logged_in
        if "logout" in argv:
            logged_in = False
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout="", stderr="")
        body = '{"loggedIn": true}' if logged_in else '{"loggedIn": false}'
        return subprocess.CompletedProcess(
            args=argv, returncode=0 if logged_in else 1, stdout=body, stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _stateful_run)

    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is True
    assert hi.harness_logout(ANTHROPIC_FAMILY) is True, (
        "logout must invalidate the cached positive and confirm live"
    )
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is False


def test_version_probe_caches_by_binary_signature(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``--version`` parses cache against (path, mtime, size); failures don't.

    The output is a pure function of the binary bytes, so a swap (upgrade)
    must re-probe and an unchanged binary must never be probed twice.
    """
    binary = tmp_path / "fake-cli"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    spec = hi.harness_install_spec(ANTHROPIC_FAMILY)
    assert spec is not None
    runs: list[list[str]] = []
    fail_first = True

    def _version_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        runs.append(argv)
        if fail_first:
            raise OSError("scripted probe failure")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout="1.2.3", stderr="")

    monkeypatch.setattr(hi.subprocess, "run", _version_run)

    # A failed probe is not cached — the next call tries again.
    assert hi._harness_cli_version_string(spec, str(binary)) is None
    fail_first = False
    assert hi._harness_cli_version_string(spec, str(binary)) == "1.2.3"
    assert hi._harness_cli_version_string(spec, str(binary)) == "1.2.3"
    assert len(runs) == 2, "unchanged binary must be version-cached after one success"

    # Swapping the binary (new mtime/size) must re-probe.
    binary.write_text("#!/bin/sh\n# upgraded\n", encoding="utf-8")
    os.utime(binary, ns=(1, 1))
    assert hi._harness_cli_version_string(spec, str(binary)) == "1.2.3"
    assert len(runs) == 3


def test_cursor_install_spec_is_login_only_no_npm() -> None:
    """Cursor ships via a curl installer (no npm package) and authenticates
    through its own CLI login, so it carries an ``install_hint`` + status JSON
    key instead of a ``package``.

    Drift here (a package sneaking in, or the wrong status key) would make the
    setup menu offer a bogus ``npm install`` or misread login state.
    """
    spec = hi.harness_install_spec(hi.CURSOR_KEY)
    assert spec is not None
    assert spec.binary == "cursor-agent"
    assert spec.package is None
    assert spec.install_hint is not None and "cursor.com/install" in spec.install_hint
    assert spec.login_args == ("login",)
    assert spec.logout_args == ("logout",)
    assert spec.status_args == ("status", "--format", "json")
    assert spec.login_status_key == "isAuthenticated"


def test_kiro_install_spec_is_manual_installer_no_npm() -> None:
    """Kiro ships as a standalone native installer, not an npm package."""
    spec = hi.harness_install_spec(hi.KIRO_KEY)
    assert spec is not None
    assert spec.display == "Kiro"
    assert spec.binary == "kiro-cli"
    assert spec.package is None
    assert spec.install_hint == "curl -fsSL https://cli.kiro.dev/install | bash"


def test_hermes_install_spec_has_actionable_vendor_installer() -> None:
    """Hermes' trusted vendor installer can be launched from the setup menu."""
    spec = hi.harness_install_spec(hi.HERMES_KEY)
    assert spec is not None
    assert spec.package is None
    assert spec.install_hint is not None
    assert hi.harness_install_command(hi.HERMES_KEY) == [
        "bash",
        "-c",
        spec.install_hint,
    ]


def test_antigravity_install_spec_launches_auth_service_no_npm() -> None:
    """Antigravity (agy) ships via a shell installer (no npm) and signs in by
    launching ``agy`` once. It also exposes a status check (``agy models``), so
    the spec carries empty ``login_args`` plus ``status_args`` + ``install_hint``
    but no ``package`` / ``logout_args``.

    Drift here (a package sneaking in, or losing ``status_args``) would make the
    setup menu offer a bogus ``npm install`` or fall back to a file-only login
    check that can't see server-side revocation.
    """
    spec = hi.harness_install_spec(GEMINI_FAMILY)
    assert spec is not None
    assert spec.binary == "agy"
    assert spec.package is None
    assert spec.install_hint is not None
    assert "antigravity.google/cli/install.sh" in spec.install_hint
    assert spec.status_args == ("models",)
    assert spec.login_args == ()
    assert spec.logout_args is None
    assert spec.login_status_key is None
    assert spec.auth_hint is not None


@pytest.mark.parametrize(
    "harness",
    ["antigravity-native", "native-antigravity", "agy-native", "native-agy"],
)
def test_harness_setup_hint_antigravity_surfaces_sign_in(harness: str) -> None:
    """A not-yet-signed-in agy is fixed by launching ``agy`` itself, so the
    launch hint names the installer AND the "run agy to sign in" step —
    otherwise a user who already has agy installed gets a misleading
    install-only hint.
    """
    hint = hi.harness_setup_hint(harness)
    assert "antigravity.google/cli/install.sh" in hint
    assert "agy" in hint
    assert "sign" in hint.lower()


def test_install_command_rejects_non_npm_harness() -> None:
    """A non-npm harness has no npm install command; asking for one is
    a loud error so the caller shows its ``install_hint`` instead."""
    with pytest.raises(ValueError):
        hi.harness_install_command(hi.CURSOR_KEY)
    with pytest.raises(ValueError):
        hi.harness_install_command(hi.KIRO_KEY)


def test_install_harness_cli_noop_for_non_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """``install_harness_cli`` never shells npm for a non-npm CLI.

    It returns ``False`` without spawning anything, so the menu falls back to
    the manual ``install_hint`` rather than running a bogus npm command.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("npm install spawned for a non-npm harness")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.install_harness_cli(hi.CURSOR_KEY) is False
    assert hi.install_harness_cli(hi.KIRO_KEY) is False


def test_unknown_key_has_no_spec_and_is_not_installed() -> None:
    """A family with no dedicated CLI (e.g. a gateway-only family) → None / False,
    never a crash."""
    assert hi.harness_install_spec("gateway") is None
    assert hi.harness_cli_installed("gateway") is False


@pytest.mark.parametrize(
    "harness,binary",
    [
        ("claude-native", "claude"),
        ("codex-native", "codex"),
        ("pi", "pi"),
        # Native Cursor wraps the cursor-agent CLI (distinct from the SDK
        # ``cursor`` harness, which needs no binary — see the test below).
        ("cursor-native", "cursor-agent"),
        ("native-cursor", "cursor-agent"),
        ("kiro-native", "kiro-cli"),
        ("native-kiro", "kiro-cli"),
    ],
)
def test_required_cli_for_cli_backed_harness(harness: str, binary: str) -> None:
    """The CLI-backed harnesses map to the binary their launch needs.

    Drift here (a wrong/missing mapping) would let sub-agent dispatch skip
    the preflight for a harness that actually needs a CLI, reintroducing the
    lazy-boot-failure the guard exists to prevent.
    """
    spec = hi.required_cli_for_harness(harness)
    assert spec is not None
    assert spec.binary == binary


@pytest.mark.parametrize("harness", ["cursor-native", "native-cursor"])
def test_setup_hint_for_native_cursor_points_at_vendor_installer(harness: str) -> None:
    """Native Cursor's "not configured" hint names the curl installer + login,
    never ``omni setup`` — which only configures the SDK ``cursor`` harness
    (``cursor-sdk`` + ``CURSOR_API_KEY``) and never installs ``cursor-agent``.

    A regression to the generic hint sends a native-Cursor user down a dead end
    (the exact bug this fixes).
    """
    hint = hi.harness_setup_hint(harness)
    assert "cursor-agent" in hint
    assert "cursor.com/install" in hint
    assert "cursor-agent login" in hint
    assert "omni setup" not in hint


@pytest.mark.parametrize("harness", ["kiro-native", "native-kiro"])
def test_setup_hint_for_native_kiro_points_at_vendor_installer(harness: str) -> None:
    """Native Kiro's missing-binary hint names Kiro's installer, not setup."""
    hint = hi.harness_setup_hint(harness)
    assert "kiro-cli" in hint
    assert "cli.kiro.dev/install" in hint
    assert "omni setup" not in hint


@pytest.mark.parametrize("harness", ["codex", "pi", "claude-sdk", None])
def test_setup_hint_defaults_to_omnigent_setup(harness: str | None) -> None:
    """Harnesses whose CLI ``omni setup`` installs (npm CLIs) — and the
    SDK / unknown / ``None`` cases — route to the ``omni setup`` hint.

    ``claude-native`` is absent: it names Anthropic's installer instead.
    """
    hint = hi.harness_setup_hint(harness)
    assert "omni setup" in hint


@pytest.mark.parametrize("harness", ["cursor", "claude-sdk", "openai-agents"])
def test_sdk_harnesses_require_no_cli(harness: str) -> None:
    """SDK-based harnesses (incl. ``cursor``, which drives the cursor-sdk Python
    package) require no CLI binary, so the sub-agent dispatch preflight must not
    flag them — otherwise it would block a launch that needs no CLI (and, for
    cursor, print ``npm install -g None`` for its package-less spec)."""
    assert hi.required_cli_for_harness(harness) is None
    assert hi.missing_harness_cli(harness) is None


@pytest.mark.parametrize(
    "harness",
    ["claude-sdk", "codex", "openai-agents-sdk", "unknown"],
)
def test_required_cli_none_for_sdk_or_unknown_harness(harness: str) -> None:
    """SDK-based / unknown harnesses need no CLI binary → ``None``.

    A false positive here would block a perfectly launchable in-process
    harness (e.g. the claude-sdk orchestrator brain) at dispatch.
    """
    assert hi.required_cli_for_harness(harness) is None


def test_missing_harness_cli_present_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary on PATH → no missing-CLI verdict (dispatch proceeds).

    A failure here would mean the guard blocks a worker whose CLI is actually
    installed.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert hi.missing_harness_cli("pi") is None


def test_missing_harness_cli_absent_returns_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Binary absent from PATH → returns the spec so dispatch can fail loud.

    This is exactly the pi-not-installed case the guard catches; a failure
    means the missing CLI would slip through to a lazy boot failure instead.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    spec = hi.missing_harness_cli("pi")
    assert spec is not None
    # The returned spec carries the binary + npm package the dispatch error
    # surfaces to the orchestrator/human.
    assert spec.binary == "pi"
    assert spec.package == "@earendil-works/pi-coding-agent"


def test_missing_harness_cli_none_for_sdk_harness(monkeypatch: pytest.MonkeyPatch) -> None:
    """An SDK harness is never blocked, even when no binary is on PATH.

    ``shutil.which`` returns None for everything here; the guard must still
    pass an SDK harness through because it needs no CLI to boot.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    assert hi.missing_harness_cli("claude-sdk") is None


def test_cli_installed_reflects_which(monkeypatch: pytest.MonkeyPatch) -> None:
    """``harness_cli_installed`` follows ``resolve_cli_binary``.

    On ``PATH`` → True; unresolvable (the autouse fixture stubs the fallback
    dirs empty) → False — the signal the configure ✗ marker and the run gating
    both read.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert hi.harness_cli_installed(ANTHROPIC_FAMILY) is True

    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    assert hi.harness_cli_installed(ANTHROPIC_FAMILY) is False


def test_cli_installed_finds_binary_off_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLI in a global install dir but off ``PATH`` still reads installed.

    This is the reported nvm case: the host daemon's frozen ``PATH`` omits the
    bin dir, so bare ``shutil.which`` misses it, but ``resolve_cli_binary``'s
    fallback ladder finds it on disk. Readiness must not report it missing.
    """
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    claude = fallback_dir / "claude"
    claude.write_text("#!/bin/sh\n")
    claude.chmod(0o755)
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback_dir,))
    assert hi.harness_cli_installed(ANTHROPIC_FAMILY) is True
    assert hi.missing_harness_cli("claude-native") is None


def test_install_harness_cli_requires_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No npm on PATH → install short-circuits to False without shelling out."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("subprocess.run reached despite missing npm")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.install_harness_cli(OPENAI_FAMILY) is False


def test_try_install_harness_cli_missing_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No npm on PATH → ``(False, reason)`` naming the missing installer.

    The UI-driven install shows this reason instead of a bare failure, so the
    user knows the host lacks npm rather than guessing.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        hi.subprocess,
        "run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not shell out")),
    )
    installed, reason = hi.try_install_harness_cli(OPENAI_FAMILY)
    assert installed is False
    assert reason is not None and "npm" in reason
    # Claude's installer is bash-based, so its reason names bash, not npm.
    installed, reason = hi.try_install_harness_cli(ANTHROPIC_FAMILY)
    assert installed is False
    assert reason is not None and "bash" in reason


def test_try_install_harness_cli_manual_only() -> None:
    """A manual-only CLI (no npm package, no install_command) → ``(False, reason)``.

    Cursor installs out-of-band; the reason tells the caller it can't be
    auto-installed so the UI can fall back to showing the install hint.
    """
    installed, reason = hi.try_install_harness_cli(hi.CURSOR_KEY)
    assert installed is False
    assert reason is not None and "automatically" in reason


def test_try_install_harness_cli_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-zero installer exit with the binary still absent → ``(False, reason)``.

    Surfaces the installer's exit code so a failed npm install is actionable.
    """

    def _which(name: str) -> str | None:
        return "/usr/bin/npm" if name == "npm" else None

    monkeypatch.setattr(hi.shutil, "which", _which)
    monkeypatch.setattr(
        hi.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(args=argv, returncode=1),
    )
    installed, reason = hi.try_install_harness_cli(OPENAI_FAMILY)
    assert installed is False
    assert reason is not None and "code 1" in reason


def test_try_install_harness_cli_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful install → ``(True, None)``; the bool wrapper agrees."""
    state = {"installed": False}

    def _which(name: str) -> str | None:
        if name == "npm":
            return "/usr/bin/npm"
        if name == "codex":
            return "/usr/bin/codex" if state["installed"] else None
        return None

    def _run(argv: list[str], **k: object):
        state["installed"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.shutil, "which", _which)
    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.try_install_harness_cli(OPENAI_FAMILY) == (True, None)


def test_try_install_harness_cli_success_when_binary_off_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A binary installed into a global dir but off bare ``PATH`` reads success.

    Regression: the install verdict and the readiness badge must use the SAME
    resolver. On a host whose frozen ``PATH`` omits the npm/nvm/homebrew bin dir,
    npm lands the binary there — off ``PATH`` but on ``resolve_cli_binary``'s
    fallback ladder. Judging install success with bare ``shutil.which`` reported
    a spurious "not found" failure (red toast) while readiness resolved it via
    the ladder (green tick) — the two verdicts disagreeing on one install.
    """
    fallback_dir = tmp_path / "bin"
    fallback_dir.mkdir()
    codex = fallback_dir / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)

    # npm is on PATH; the installed codex binary never is — only the ladder finds it.
    monkeypatch.setattr(hi.shutil, "which", lambda name: "/usr/bin/npm" if name == "npm" else None)
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback_dir,))

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            out = "9.9.9\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)

    # Install verdict agrees with readiness: both see it installed.
    assert hi.try_install_harness_cli(OPENAI_FAMILY) == (True, None)
    assert hi.harness_cli_installed(OPENAI_FAMILY) is True


def test_try_install_prepends_resolved_dir_so_login_can_find_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After install, the resolving dir is on ``PATH`` for the later login step.

    The install verdict resolves via the full ladder, but the setup wizard's
    subsequent ``harness_login`` / ``harness_cli_logged_in`` shell out with the
    bare binary name and only bare ``shutil.which`` (i.e. ``PATH``). If install
    succeeds via a fallback dir (nvm/homebrew/…) without putting that dir on
    ``PATH``, login would fail to find the binary just installed. Assert the
    install prepends the resolving dir so a bare ``PATH`` lookup then succeeds —
    converging install, readiness, and login on the same binary.
    """
    fallback_dir = tmp_path / "nvm" / "bin"
    fallback_dir.mkdir(parents=True)
    codex = fallback_dir / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)

    # A PATH that has npm but NOT the fallback dir; use the REAL shutil.which so
    # the prepend is observable via a genuine PATH lookup (what login does).
    npm_dir = tmp_path / "npmhome"
    npm_dir.mkdir()
    (npm_dir / "npm").write_text("#!/bin/sh\n")
    (npm_dir / "npm").chmod(0o755)
    monkeypatch.setenv("PATH", str(npm_dir))
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (fallback_dir,))
    monkeypatch.setattr(
        hi.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(args=argv, returncode=0),
    )

    # Before: a bare PATH lookup (what harness_login uses) can't find codex.
    assert shutil.which("codex") is None
    assert hi.try_install_harness_cli(OPENAI_FAMILY) == (True, None)
    # After: the resolving dir was prepended, so the login step's bare lookup
    # now resolves the binary that was just installed.
    assert shutil.which("codex") == str(codex)
    assert str(fallback_dir) in os.environ["PATH"].split(os.pathsep)


def test_install_harness_cli_runs_npm_then_rechecks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Installs via ``npm install -g <package>`` and reports the post-install
    PATH state (True once the binary appears)."""
    calls: list[list[str]] = []
    # npm present; the target binary appears only after the install runs.
    state = {"installed": False}

    def _which(name: str) -> str | None:
        if name == "npm":
            return "/usr/bin/npm"
        if name == "codex":
            return "/usr/bin/codex" if state["installed"] else None
        return None

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["installed"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.shutil, "which", _which)
    monkeypatch.setattr(hi.subprocess, "run", _run)

    assert hi.install_harness_cli(OPENAI_FAMILY) is True
    assert calls == [["npm", "install", "-g", "@openai/codex"]]


def test_install_harness_cli_runs_hermes_installer_then_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Hermes installer is interactive-menu actionable and PATH-verified."""
    calls: list[list[str]] = []
    state = {"installed": False}

    def _which(name: str) -> str | None:
        if name == "bash":
            return "/bin/bash"
        if name == "hermes" and state["installed"]:
            return "/usr/local/bin/hermes"
        return None

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["installed"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.shutil, "which", _which)
    monkeypatch.setattr(hi.subprocess, "run", _run)

    assert hi.install_harness_cli(hi.HERMES_KEY) is True
    spec = hi.harness_install_spec(hi.HERMES_KEY)
    assert spec is not None
    assert spec.install_hint is not None
    assert calls == [["bash", "-c", spec.install_hint]]


def test_install_harness_cli_refreshes_user_local_bin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A vendor install into ~/.local/bin is usable without restarting setup.

    The install resolves the binary via ``resolve_cli_binary`` (whose ladder
    includes ~/.local/bin) and prepends the resolving dir to ``PATH``, so the
    wizard's later bare-``PATH`` login steps find the just-installed CLI.
    """
    user_bin = tmp_path / ".local" / "bin"
    user_bin.mkdir(parents=True)
    hermes = user_bin / "hermes"
    hermes.write_text("#!/bin/sh\n")
    hermes.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    # bash (the hermes installer) is on PATH; hermes is only in the fallback
    # dir, off bare PATH — the real resolver must find it via the ladder.
    bin_dir = tmp_path / "sysbin"
    bin_dir.mkdir()
    (bin_dir / "bash").write_text("#!/bin/sh\n")
    (bin_dir / "bash").chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setattr(_platform, "_cli_fallback_dirs", lambda: (user_bin,))
    monkeypatch.setattr(
        hi.subprocess,
        "run",
        lambda argv, *, check=False, timeout=None: subprocess.CompletedProcess(argv, 0),
    )

    assert hi.install_harness_cli(hi.HERMES_KEY) is True
    # The resolving dir (~/.local/bin) is now first on PATH, so a bare
    # shutil.which — what harness_login uses — finds hermes.
    assert hi.os.environ["PATH"].split(hi.os.pathsep)[0] == str(user_bin)
    assert shutil.which("hermes") == str(hermes)


def test_harness_login_skips_when_already_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """An already-logged-in CLI short-circuits to True without spawning login.

    A failure here means we'd re-run an interactive OAuth flow on a user who is
    already signed in.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in", lambda key: True
    )

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("login subprocess spawned despite already being logged in")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_login(ANTHROPIC_FAMILY) is True


@pytest.mark.parametrize(
    "key,expected_argv",
    [
        (ANTHROPIC_FAMILY, ["claude", "auth", "login", "--claudeai"]),
        (OPENAI_FAMILY, ["codex", "login"]),
        (GEMINI_FAMILY, ["/usr/bin/agy"]),
    ],
)
def test_harness_login_runs_cli_login_then_verifies(
    monkeypatch: pytest.MonkeyPatch, key: str, expected_argv: list[str]
) -> None:
    """Not logged in → runs the harness's first-class login argv, then verifies.

    Asserts the exact argv so a drift away from ``claude auth login --claudeai``
    / ``codex login`` (e.g. back to a TUI hack) is caught, and that the result
    reflects the post-login verdict.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    # Pin stdin to a TTY so this test stays focused on argv and never touches a
    # real /dev/tty — the non-TTY branch is exercised separately below.
    monkeypatch.setattr(hi.sys.stdin, "isatty", lambda: True)
    calls: list[list[str]] = []
    state = {"logged_in": False}
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in",
        lambda k: state["logged_in"],
    )

    def _run(argv: list[str], **kwargs: object):
        calls.append(argv)
        state["logged_in"] = True  # the user completed the interactive login
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_login(key) is True
    assert calls == [expected_argv]


def test_harness_login_resolves_agy_outside_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Antigravity sign-in launches agy's resolved fallback install path."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    monkeypatch.setattr(hi, "resolve_cli_binary", lambda name: "/home/user/.local/bin/agy")
    monkeypatch.setattr(hi.sys.stdin, "isatty", lambda: True)
    state = {"logged_in": False}
    monkeypatch.setattr(
        hi,
        "harness_cli_logged_in",
        lambda key: state["logged_in"],
    )
    calls: list[list[str]] = []

    def _run(argv: list[str], **kwargs: object):
        calls.append(argv)
        state["logged_in"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)

    assert hi.harness_login(GEMINI_FAMILY) is True
    assert calls == [["/home/user/.local/bin/agy"]]


def test_harness_login_wires_dev_tty_when_stdin_not_a_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No TTY on stdin → open /dev/tty, pass it as the child's std* fds, then close it.

    When the parent's stdio is piped (e.g. launched via ``uv tool run``) the
    harness CLI sees ``isatty() == False`` and refuses to open the browser,
    stranding the login. The fix opens ``/dev/tty`` and hands it to the child as
    stdin/stdout/stderr so it sees a real terminal. Asserts that wiring happens
    and that the fd is released even on the success path (``finally``).
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hi.sys.stdin, "isatty", lambda: False)
    state = {"logged_in": False}
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in",
        lambda k: state["logged_in"],
    )

    sentinel_fd = 4242
    monkeypatch.setattr(hi.os, "open", lambda path, flags: sentinel_fd)
    closed: list[int] = []
    monkeypatch.setattr(hi.os, "close", lambda fd: closed.append(fd))

    seen: dict = {}

    def _run(argv: list[str], **kwargs: object):
        seen["kwargs"] = kwargs
        state["logged_in"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_login(ANTHROPIC_FAMILY) is True
    assert seen["kwargs"]["stdin"] == sentinel_fd
    assert seen["kwargs"]["stdout"] == sentinel_fd
    assert seen["kwargs"]["stderr"] == sentinel_fd
    assert closed == [sentinel_fd]  # fd released after the login returns


def test_harness_login_falls_back_when_dev_tty_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No controlling terminal → swallow the OSError and inherit parent stdio.

    Headless / CI runs have no ``/dev/tty``; the login must still proceed with
    the parent's inherited stdio rather than crash, and must not pass any
    std* fds to the child.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(hi.sys.stdin, "isatty", lambda: False)
    state = {"logged_in": False}
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in",
        lambda k: state["logged_in"],
    )

    def _no_tty(path: str, flags: int) -> int:
        raise OSError("no controlling terminal")

    monkeypatch.setattr(hi.os, "open", _no_tty)

    seen: dict = {}

    def _run(argv: list[str], **kwargs: object):
        seen["kwargs"] = kwargs
        state["logged_in"] = True
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_login(ANTHROPIC_FAMILY) is True
    assert "stdin" not in seen["kwargs"]
    assert "stdout" not in seen["kwargs"]
    assert "stderr" not in seen["kwargs"]


def test_harness_login_false_when_login_not_completed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Login ran but the CLI still reports no login → False.

    This is what stops the caller from recording a phantom subscription when the
    user bails out of (or fails) the OAuth flow.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in", lambda k: False
    )
    monkeypatch.setattr(
        hi.subprocess,
        "run",
        lambda argv, **k: subprocess.CompletedProcess(args=argv, returncode=1),
    )
    assert hi.harness_login(OPENAI_FAMILY) is False


def test_harness_login_false_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI binary on PATH → False without spawning anything."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("login spawned despite missing binary")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_login(ANTHROPIC_FAMILY) is False


def test_harness_login_false_for_harness_without_login(monkeypatch: pytest.MonkeyPatch) -> None:
    """A harness with no login command (Pi) → False without spawning anything."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("login spawned for a harness with no login_args")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_login(hi.PI_KEY) is False


@pytest.mark.parametrize(
    "key,expected_argv",
    [
        (ANTHROPIC_FAMILY, ["claude", "auth", "logout"]),
        (OPENAI_FAMILY, ["codex", "logout"]),
    ],
)
def test_harness_logout_runs_cli_logout_then_verifies(
    monkeypatch: pytest.MonkeyPatch, key: str, expected_argv: list[str]
) -> None:
    """Runs the harness's own logout argv and reports the logged-out verdict."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls: list[list[str]] = []
    state = {"logged_in": True}
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in",
        lambda k: state["logged_in"],
    )

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["logged_in"] = False
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_logout(key) is True
    assert calls == [expected_argv]


@pytest.mark.parametrize(
    "stdout,returncode,expected",
    [
        # Claude prints JSON; loggedIn is the verdict regardless of exit code.
        ('{"loggedIn": true, "authMethod": "claude.ai"}', 0, True),
        ('{"loggedIn": false}', 1, False),
        # Exit 0 but loggedIn false → the structured verdict still wins.
        ('{"loggedIn": false}', 0, False),
    ],
)
def test_harness_cli_logged_in_uses_claude_json_verdict(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: bool
) -> None:
    """Claude's `auth status` JSON `loggedIn` field is the login verdict.

    This is the macOS fix: Claude stores creds in the Keychain (no
    `~/.claude/.credentials.json`), so a file check falsely reports "not logged
    in" right after a successful login. Asking `claude auth status` reads the
    real state. Failure here means we'd regress to the file-based check.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object):
        assert argv == ["claude", "auth", "status"]  # the status subcommand
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is expected


@pytest.mark.parametrize(
    "stdout,returncode,expected",
    [
        ("Logged in using an API key - sk-***", 0, True),  # non-JSON, exit 0
        ("Not logged in", 1, False),  # non-JSON, exit 1
    ],
)
def test_harness_cli_logged_in_codex_uses_exit_code(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: bool
) -> None:
    """Codex's `login status` is non-JSON, so the exit code is the verdict.

    Codex exits 0 only when logged in; failure means the non-JSON fallback
    branch misread the status.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object):
        assert argv == ["codex", "login", "status"]  # the status subcommand
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_logged_in(OPENAI_FAMILY) is expected


@pytest.mark.parametrize(
    "stdout,returncode,expected",
    [
        # Cursor prints JSON with ``isAuthenticated``; the field is the verdict
        # regardless of exit code.
        ('{"isAuthenticated": true, "status": "authenticated"}', 0, True),
        ('{"isAuthenticated": false}', 1, False),
        ('{"isAuthenticated": false}', 0, False),
    ],
)
def test_harness_cli_logged_in_uses_cursor_json_verdict(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: bool
) -> None:
    """Cursor's ``status --format json`` reports ``isAuthenticated``.

    Unlike Claude (``loggedIn``) it uses a different key, so the spec's
    ``login_status_key`` selects it. A regression would misread cursor login
    state in the setup menu's ✓/✗ marker.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object):
        assert argv == ["cursor-agent", "status", "--format", "json"]
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_logged_in(hi.CURSOR_KEY) is expected


@pytest.mark.parametrize(
    "stdout,returncode,expected",
    [
        # ``agy models`` lists models (exit 0) only when signed in.
        ("Gemini 3.5 Flash (Medium)\nGemini 3.1 Pro (High)\n", 0, True),
        ("Error: Please sign in to view available models.", 1, False),
        # Exit code is authoritative for agy (no ``login_status_key``): stdout
        # that happens to be a JSON object with ``loggedIn`` must NOT override
        # it, so an exit-0 run still reads as signed in.
        ('{"loggedIn": false}', 0, True),
        # Empty stdout (e.g. the list went to stderr) → exit code decides.
        ("", 0, True),
        ("", 1, False),
    ],
)
def test_harness_cli_logged_in_agy_uses_exit_code(
    monkeypatch: pytest.MonkeyPatch, stdout: str, returncode: int, expected: bool
) -> None:
    """Antigravity's ``agy models`` is non-JSON, so the exit code is the verdict.

    ``agy`` has no ``login status`` subcommand; ``agy models`` exits 0 only when
    signed in (else exits non-zero with "Please sign in"). A regression would
    misread agy login state in the setup menu's ✓/✗ marker.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object):
        assert argv == ["/usr/bin/agy", "models"]  # the status subcommand
        return subprocess.CompletedProcess(
            args=argv, returncode=returncode, stdout=stdout, stderr=""
        )

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_logged_in(GEMINI_FAMILY) is expected


def test_harness_cli_logged_in_false_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No CLI binary on PATH → False without spawning a status check."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("status spawned despite missing binary")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_cli_logged_in(ANTHROPIC_FAMILY) is False


def test_harness_cli_logged_in_false_for_harness_without_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A harness with no status command (Pi) → False without spawning anything."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("status spawned for a harness with no status_args")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_cli_logged_in(hi.PI_KEY) is False


# ── UI setup-step descriptor ─────────────────────────────


def test_ui_install_key_resolves_bare_and_native_spellings() -> None:
    """The UI may pass either the bare id or the native executor spelling."""
    assert hi.ui_install_key("codex") == OPENAI_FAMILY
    assert hi.ui_install_key("codex-native") == OPENAI_FAMILY
    assert hi.ui_install_key("qwen-native") == hi.QWEN_KEY
    assert hi.ui_install_key("claude-native") == ANTHROPIC_FAMILY
    # Non-installable (curl/OAuth/SDK) harnesses resolve to None.
    assert hi.ui_install_key("cursor") is None
    assert hi.ui_install_key("cursor-native") is None
    assert hi.ui_install_key("claude-sdk") is None


def test_ui_installable_harnesses_includes_native_spellings() -> None:
    installable = hi.ui_installable_harnesses()
    assert {"claude", "codex", "pi", "opencode", "qwen"} <= installable
    assert {"codex-native", "qwen-native", "opencode-native"} <= installable
    assert "cursor" not in installable
    assert "claude-sdk" not in installable


def test_ui_setup_steps_install_then_ui_auth_for_codex() -> None:
    """Codex: one-click install, then a UI-authable auth step. The step opens
    the credential form (action ``"auth"``) whose options include the ``codex
    login`` subscription; it stays status-tracked (``"authed"``)."""
    steps = hi.ui_setup_steps("codex")
    assert [s.kind for s in steps] == ["install", "auth"]
    install, auth = steps
    assert install.action == "install"
    assert install.status_key == "installed"
    assert install.command is None
    assert auth.action == "auth"
    assert auth.title == "Set up authentication"
    assert auth.command == "codex login"
    assert auth.status_key == "authed"


def test_ui_setup_steps_native_spelling_matches_bare() -> None:
    """The native spelling yields the same steps as the bare id."""
    assert [s.as_dict() for s in hi.ui_setup_steps("codex-native")] == [
        s.as_dict() for s in hi.ui_setup_steps("codex")
    ]


def test_ui_setup_steps_pi_auth_is_ui_authable_and_tracked() -> None:
    """Pi is UI-authable: its auth step opens the credential form (action
    ``"auth"``), carries no CLI login command (no subscription), and is
    status-tracked (``"authed"``) so the dialog can't drop it as "unknown"
    and wrongly read "ready"."""
    steps = hi.ui_setup_steps("pi")
    assert [s.kind for s in steps] == ["install", "auth"]
    assert steps[1].action == "auth"
    assert steps[1].command is None
    assert steps[1].status_key == "authed"


def test_ui_setup_steps_qwen_auth_stays_untracked_setup_fallback() -> None:
    """Qwen is env-auth (not UI-authable), so its auth step stays an untracked
    ``omni setup`` signpost — the case that must NOT gain the form."""
    steps = hi.ui_setup_steps("qwen")
    assert [s.kind for s in steps] == ["install", "auth"]
    assert steps[1].action == "setup"
    assert steps[1].command == "omni setup"
    assert steps[1].status_key is None


def test_ui_setup_steps_generic_for_non_installable() -> None:
    """A non-installable harness (cursor) gets a single generic setup step."""
    for harness in ("cursor", "claude-sdk"):
        steps = hi.ui_setup_steps(harness)
        assert len(steps) == 1
        assert steps[0].action == "setup"
        assert steps[0].command == "omni setup"
        assert steps[0].status_key is None


# ── Version-aware installed check ────────────────────────


@pytest.mark.parametrize(
    "key,min_version,max_version_exclusive",
    [
        (hi.OPENCODE_KEY, "1.17.7", "1.19.0"),
        (hi.CURSOR_KEY, "2026.06.02", None),
        (hi.KIMI_KEY, "0.7.0", None),
        (ANTHROPIC_FAMILY, "2.1.161", None),
        (OPENAI_FAMILY, "0.137.0", None),
        (hi.PI_KEY, "0.84.2", None),
        (hi.QWEN_KEY, "0.18.1", None),
        (hi.GOOSE_KEY, "1.38.0", None),
        (hi.HERMES_KEY, "0.17.0", None),
        (hi.KIRO_KEY, "2.10.0", None),
    ],
)
def test_versioned_specs_declare_bounds(
    key: str, min_version: str, max_version_exclusive: str | None
) -> None:
    """Version-bounded harness specs expose the same floors setup enforces."""
    spec = hi.harness_install_spec(key)
    assert spec is not None
    assert spec.min_version == min_version
    assert spec.max_version_exclusive == max_version_exclusive


@pytest.mark.parametrize(
    "version,expected",
    [
        ("1.17.6", False),  # below min
        ("1.19.0", False),  # at max exclusive
        ("2.0.0", False),  # above max
        ("1.17.8", True),  # inside range
        ("1.18.16", True),  # inside range (1.18.x)
    ],
)
def test_harness_cli_installed_checks_version_for_versioned_specs(
    monkeypatch: pytest.MonkeyPatch, version: str, expected: bool
) -> None:
    """A present CLI whose ``--version`` is outside the declared range reads as
    not installed, so setup prompts for an upgrade before the runtime gate."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            # OpenCode's supported range is [1.17.7, 1.19.0).
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{version}\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(hi.OPENCODE_KEY) is expected


@pytest.mark.parametrize(
    "key",
    [
        hi.CURSOR_KEY,
        hi.KIMI_KEY,
        ANTHROPIC_FAMILY,
        OPENAI_FAMILY,
        hi.PI_KEY,
        hi.QWEN_KEY,
        hi.GOOSE_KEY,
        hi.HERMES_KEY,
        hi.KIRO_KEY,
    ],
)
def test_harness_cli_installed_checks_minimum_for_other_versioned_specs(
    monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    """Version-bounded harnesses treat a CLI older than their declared floor as
    not installed, so setup prompts for an upgrade."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            out = "0.0.1\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(key) is False


def test_the_codex_launch_floor_accepts_the_ci_pinned_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.139.0 must read as installed, not ``version-too-low``.

    A too-low codex makes ``harness_is_configured`` false, and the host then
    refuses EVERY codex launch — plain sessions included — with a misleading
    "run omni setup". Smart Routing's spawn hook wants 0.145.0, but that is
    enforced where the hook is registered, so an older CLI loses only the
    spawn gate.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="codex-cli 0.139.0\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(OPENAI_FAMILY) is True


def test_the_kimi_floor_accepts_the_cli_this_spec_installs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A current ``kimi-code`` build must read as installed, not too-low.

    The floor tracks Moonshot's ``kimi-code`` CLI (a 0.x series, the binary
    this spec's installer puts on PATH), not the separately numbered
    ``kimi-cli`` project. Pinning it to a 1.x version made every shipping
    ``kimi`` fail the range, so ``harness_is_configured`` stayed false and the
    host refused every kimi-native launch.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="0.34.0\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(hi.KIMI_KEY) is True


def test_the_hermes_floor_accepts_the_shipping_version_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hermes' semver ``--version`` line must satisfy the floor.

    Hermes prints ``Hermes Agent v0.19.1 (2026.7.30)`` — a semver with the
    build date beside it — so the parser reads ``0.19.1``. A date-shaped floor
    could never be met by that string, which left hermes-native unlaunchable.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout="Hermes Agent v0.19.1 (2026.7.30)\n",
                stderr="",
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(hi.HERMES_KEY) is True


def test_harness_cli_installed_true_when_version_in_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present CLI with a satisfying version reads as installed."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            out = "1.17.8\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(hi.OPENCODE_KEY) is True


def test_harness_cli_installed_ignores_upper_bound_for_unversioned_specs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Harnesses without a version declaration are not probed with ``--version``."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("version probe spawned for an unversioned harness")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_cli_installed(GEMINI_FAMILY) is True


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("cursor-agent 2026.07.01-777f564", "2026.07.01"),
        ("2026.06.19-20-24-33-653a7fb", "2026.06.19"),
        ("2026.05.24.1.dda726e", "2026.05.24"),
        ("kimi version 1.47.0", "1.47.0"),
        ("1.17.7-rc1", "1.17.7-rc1"),
    ],
)
def test_parse_harness_cli_version_normalizes_date_versions(raw: str, expected: str) -> None:
    """Date-shaped Cursor versions are stripped to ``YYYY.MM.DD`` so PEP 440 can
    compare them; normal semver versions stay unchanged."""
    assert hi._parse_harness_cli_version(raw) == expected


@pytest.mark.parametrize(
    "key,outdated,satisfying",
    [
        (hi.CURSOR_KEY, "2026.05.24", "2026.06.22"),
        (hi.KIMI_KEY, "0.6.0", "0.34.0"),
        (hi.HERMES_KEY, "0.16.9", "0.19.1"),
    ],
)
def test_harness_cli_installed_enforces_default_post_2026_06_01_floors(
    monkeypatch: pytest.MonkeyPatch,
    key: str,
    outdated: str,
    satisfying: str,
) -> None:
    """Cursor and Kimi default to the first release after 2026-06-01."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{outdated}\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(key) is False

    def _run_ok(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=f"{satisfying}\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run_ok)
    assert hi.harness_cli_installed(key) is True


def test_harness_cli_installed_false_when_version_unparseable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A present CLI whose ``--version`` output contains no parseable version is
    treated as not installed, so setup prompts for an upgrade."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout="dev-SNAPSHOT\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_cli_installed(hi.OPENCODE_KEY) is False


def test_harness_cli_version_satisfies_short_circuits_when_binary_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``harness_cli_version_satisfies`` returns False when the binary is absent
    without shelling out to a missing executable."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("version probe spawned despite missing binary")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.harness_cli_version_satisfies(hi.OPENCODE_KEY) is False


def test_missing_harness_cli_flags_outdated_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CLI present but outside its declared version range is treated as
    missing by the dispatch preflight, so the runner fails loud before launch."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")

    def _run(argv: list[str], **k: object) -> subprocess.CompletedProcess[str]:
        if len(argv) >= 2 and argv[1] == "--version":
            out = "1.16.0\n"
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=out, stderr="")
        raise AssertionError(f"unexpected subprocess: {argv!r}")

    monkeypatch.setattr(hi.subprocess, "run", _run)
    spec = hi.missing_harness_cli("opencode-native")
    assert spec is not None
    assert spec.binary == "opencode"
