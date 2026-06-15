"""Tests for :mod:`omnigent.onboarding.harness_install`."""

from __future__ import annotations

import subprocess

import pytest

from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY


@pytest.mark.parametrize(
    "key,binary,package",
    [
        (ANTHROPIC_FAMILY, "claude", "@anthropic-ai/claude-code"),
        (OPENAI_FAMILY, "codex", "@openai/codex"),
        (hi.PI_KEY, "pi", "@earendil-works/pi-coding-agent"),
    ],
)
def test_install_spec_and_command(key: str, binary: str, package: str) -> None:
    """Each known harness maps to the ucode-matching binary + npm package.

    A drift in binary/package (e.g. a wrong npm name) would install the wrong
    thing or check the wrong PATH entry — caught here.
    """
    spec = hi.harness_install_spec(key)
    assert spec is not None
    assert spec.binary == binary
    assert spec.package == package
    assert hi.harness_install_command(key) == ["npm", "install", "-g", package]


def test_mimo_install_spec_is_binary_only() -> None:
    """Mimo is CLI-backed, but Omnigent does not know a supported npm package."""
    spec = hi.harness_install_spec(hi.MIMO_KEY)
    assert spec is not None
    assert spec.display == "Mimo"
    assert spec.binary == "mimo"
    assert spec.package is None
    with pytest.raises(KeyError):
        hi.harness_install_command(hi.MIMO_KEY)


def test_cursor_install_spec_is_binary_only() -> None:
    """Cursor is CLI-backed, but Omnigent does not know a supported npm package."""
    spec = hi.harness_install_spec(hi.CURSOR_KEY)
    assert spec is not None
    assert spec.display == "Cursor"
    assert spec.binary == "cursor-agent"
    assert spec.package is None
    with pytest.raises(KeyError):
        hi.harness_install_command(hi.CURSOR_KEY)


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
        ("cursor", "cursor-agent"),
        ("mimo", "mimo"),
    ],
)
def test_required_cli_for_cli_backed_harness(harness: str, binary: str) -> None:
    """CLI-backed harnesses map to the binary their launch needs.

    Drift here (a wrong/missing mapping) would let sub-agent dispatch skip
    the preflight for a harness that actually needs a CLI, reintroducing the
    lazy-boot-failure the guard exists to prevent.
    """
    spec = hi.required_cli_for_harness(harness)
    assert spec is not None
    assert spec.binary == binary


@pytest.mark.parametrize(
    "harness",
    ["claude-sdk", "codex", "openai-agents-sdk", "databricks_supervisor", "unknown"],
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
    """``harness_cli_installed`` is exactly ``shutil.which(binary) is not None``.

    Present → True; absent → False — the signal the configure ✗ marker and the
    run gating both read.
    """
    monkeypatch.setattr(hi.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert hi.harness_cli_installed(ANTHROPIC_FAMILY) is True

    monkeypatch.setattr(hi.shutil, "which", lambda name: None)
    assert hi.harness_cli_installed(ANTHROPIC_FAMILY) is False


def test_install_harness_cli_requires_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    """No npm on PATH → install short-circuits to False without shelling out."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: None)

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("subprocess.run reached despite missing npm")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.install_harness_cli(ANTHROPIC_FAMILY) is False


def test_install_harness_cli_without_package_returns_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A binary-only harness does not attempt a guessed npm install."""
    monkeypatch.setattr(hi.shutil, "which", lambda name: "/usr/bin/npm")

    def _explode(*a: object, **k: object) -> None:
        raise AssertionError("subprocess.run reached despite missing install package")

    monkeypatch.setattr(hi.subprocess, "run", _explode)
    assert hi.install_harness_cli(hi.MIMO_KEY) is False


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
    calls: list[list[str]] = []
    state = {"logged_in": False}
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.harness_cli_logged_in",
        lambda k: state["logged_in"],
    )

    def _run(argv: list[str], *, check: bool = False, timeout: float | None = None):
        calls.append(argv)
        state["logged_in"] = True  # the user completed the interactive login
        return subprocess.CompletedProcess(args=argv, returncode=0)

    monkeypatch.setattr(hi.subprocess, "run", _run)
    assert hi.harness_login(key) is True
    assert calls == [expected_argv]


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
