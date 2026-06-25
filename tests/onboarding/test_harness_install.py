"""Tests for :mod:`omnigent.onboarding.harness_install`."""

from __future__ import annotations

import subprocess

import pytest

from omnigent.onboarding import harness_install as hi
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GEMINI_FAMILY, OPENAI_FAMILY


@pytest.mark.parametrize(
    "key,binary,package",
    [
        (ANTHROPIC_FAMILY, "claude", "@anthropic-ai/claude-code"),
        (OPENAI_FAMILY, "codex", "@openai/codex"),
        (hi.PI_KEY, "pi", "@earendil-works/pi-coding-agent"),
        (hi.QWEN_KEY, "qwen", "@qwen-code/qwen-code"),
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


def test_antigravity_install_spec_status_only_no_npm() -> None:
    """Antigravity (agy) ships via a shell installer (no npm) and has no login
    subcommand — the user signs in by launching ``agy`` once. It DOES expose a
    status check (``agy models``), so the spec carries ``status_args`` +
    ``install_hint`` but no ``package`` / ``login_args`` / ``logout_args``.

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
    assert spec.login_args is None
    assert spec.logout_args is None
    assert spec.login_status_key is None
    assert spec.auth_hint is not None


def test_harness_setup_hint_antigravity_surfaces_sign_in() -> None:
    """A not-yet-signed-in agy can't be fixed by ``agy login`` (no such
    command), so the launch hint names the installer AND the "run agy to sign
    in" step — otherwise a user who already has agy installed gets a misleading
    install-only hint.
    """
    hint = hi.harness_setup_hint("antigravity-native")
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
    never ``omnigent setup`` — which only configures the SDK ``cursor`` harness
    (``cursor-sdk`` + ``CURSOR_API_KEY``) and never installs ``cursor-agent``.

    A regression to the generic hint sends a native-Cursor user down a dead end
    (the exact bug this fixes).
    """
    hint = hi.harness_setup_hint(harness)
    assert "cursor-agent" in hint
    assert "cursor.com/install" in hint
    assert "cursor-agent login" in hint
    assert "omnigent setup" not in hint


@pytest.mark.parametrize("harness", ["kiro-native", "native-kiro"])
def test_setup_hint_for_native_kiro_points_at_vendor_installer(harness: str) -> None:
    """Native Kiro's missing-binary hint names Kiro's installer, not setup."""
    hint = hi.harness_setup_hint(harness)
    assert "kiro-cli" in hint
    assert "cli.kiro.dev/install" in hint
    assert "omnigent setup" not in hint


@pytest.mark.parametrize("harness", ["claude-native", "codex", "pi", "claude-sdk", None])
def test_setup_hint_defaults_to_omnigent_setup(harness: str | None) -> None:
    """Harnesses whose CLI ``omnigent setup`` installs (npm CLIs) — and the
    SDK / unknown / ``None`` cases — route to the ``omnigent setup`` hint."""
    hint = hi.harness_setup_hint(harness)
    assert "omnigent setup" in hint


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
        assert argv == ["agy", "models"]  # the status subcommand
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
