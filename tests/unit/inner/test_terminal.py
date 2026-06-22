"""Unit tests for :mod:`omnigent.inner.terminal`."""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import (
    TerminalInstance,
    create_terminal_instance,
)
from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR


@dataclass
class _SuccessfulProcess:
    """
    Minimal subprocess stand-in for :meth:`TerminalInstance.launch`.

    :param returncode: Process exit status, e.g. ``0`` for success.
    """

    returncode: int = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        """
        Return empty stdout and stderr.

        :returns: ``(stdout, stderr)`` byte strings from the fake process.
        """
        return b"", b""


def contains_subsequence(values: list[str], expected: list[str]) -> bool:
    """
    Return whether *expected* appears contiguously in *values*.

    :param values: Full argv list, e.g. ``["tmux", "set-option"]``.
    :param expected: Expected contiguous argv slice, e.g.
        ``["set-option", "-sq", "extended-keys", "on"]``.
    :returns: ``True`` when the expected slice appears in order.
    """
    if not expected:
        return True
    last_start = len(values) - len(expected)
    return any(
        values[index : index + len(expected)] == expected for index in range(last_start + 1)
    )


def test_threaded_idle_watcher_reports_terminal_exit(tmp_path: Path) -> None:
    """
    The threaded watcher reports tmux disappearance instead of exiting silently.

    :param tmp_path: Temporary directory used for placeholder tmux paths.
    """
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    exited = threading.Event()

    instance._capture_pane_for_idle_or_none = lambda: None  # type: ignore[method-assign]

    instance.start_idle_watcher_thread(
        on_exit=exited.set,
        poll_interval_s=0.01,
    )

    assert exited.wait(timeout=1.0)
    assert instance.running is False


def test_threaded_idle_watcher_keeps_last_pane_text_on_exit(tmp_path: Path) -> None:
    """
    The exit callback can still report the last pane text after tmux disappears.

    :param tmp_path: Temporary directory used for placeholder tmux paths.
    """
    instance = TerminalInstance(
        name="runtime",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    exited = threading.Event()
    snapshots = iter(["\x1b[31mstartup failed\x1b[0m\ntry config", None])

    instance._capture_pane_for_idle_or_none = lambda: next(snapshots)  # type: ignore[method-assign]

    instance.start_idle_watcher_thread(
        on_exit=exited.set,
        poll_interval_s=0.01,
    )

    assert exited.wait(timeout=1.0)
    assert instance.last_pane_text() == "startup failed\ntry config"


@pytest.mark.asyncio
async def test_launch_enables_csi_u_extended_keys_quietly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Managed tmux sessions request CSI-u extended-key forwarding on launch.

    This is the tmux-side half of Shift+Enter support for capable
    native terminals: applications in the pane can receive
    ``\\x1b[13;2u`` after they request Kitty Keyboard Protocol mode.
    The ``-q`` flag is part of the assertion so older tmux versions
    that do not know these options do not fail terminal launch.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the tmux argv and return a successful process.

        :param cmd: Tmux command argv.
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del stdout, stderr, env
        captured.append(list(cmd))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )

    await instance.launch(cwd=tmp_path)

    # ``launch`` should issue one flattened tmux setup command. Zero calls
    # would mean the test never captured launch; more than one call would
    # mean the extended-key assertions below may not cover the full setup argv.
    assert len(captured) == 1
    cmd = captured[0]

    assert contains_subsequence(cmd, ["set-option", "-sq", "extended-keys", "on"])
    assert contains_subsequence(
        cmd,
        ["set-option", "-sq", "extended-keys-format", "csi-u"],
    )


@pytest.mark.asyncio
async def test_launch_does_not_force_terminal_feature_patterns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Extended-key support must be negotiated with the attached terminal.

    Forcing ``terminal-features`` patterns would claim support based on
    a name match rather than on the user's actual terminal capability.
    Leaving that option alone keeps unsupported terminals on their
    legacy key encoding.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the tmux argv and return a successful process.

        :param cmd: Tmux command argv.
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del stdout, stderr, env
        captured.append(list(cmd))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )

    await instance.launch(cwd=tmp_path)

    # ``launch`` should issue one flattened tmux setup command. Zero calls
    # would mean the test never captured launch; more than one call would
    # mean the terminal-feature assertion below may not cover the full setup argv.
    assert len(captured) == 1
    assert "terminal-features" not in captured[0]


@pytest.mark.asyncio
async def test_launch_disables_tmux_pane_and_window_creation_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Managed tmux sessions remove the user-facing creation controls.

    The launcher should not leave tmux's default prefix table or
    right-click menus available, because those let an attached user
    create extra panes, windows, or sessions outside Omnigent' terminal
    registry.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the tmux argv and return a successful process.

        :param cmd: Tmux command argv.
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del stdout, stderr, env
        captured.append(list(cmd))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )

    await instance.launch(cwd=tmp_path)

    # ``launch`` should issue one flattened tmux setup command. Zero calls
    # would mean the test never captured launch; more than one call would
    # mean the lock-down assertions below may not cover the full setup argv.
    assert len(captured) == 1
    cmd = captured[0]

    assert contains_subsequence(cmd, ["set-option", "-g", "prefix", "None"])
    assert contains_subsequence(cmd, ["set-option", "-g", "prefix2", "None"])
    assert contains_subsequence(cmd, ["unbind-key", "-a", "-T", "prefix"])
    assert contains_subsequence(cmd, ["unbind-key", "-q", "-T", "root", "MouseDown3Pane"])
    assert contains_subsequence(cmd, ["unbind-key", "-q", "-T", "root", "M-MouseDown3Pane"])
    assert contains_subsequence(cmd, ["unbind-key", "-q", "-T", "root", "MouseDown3Status"])
    assert contains_subsequence(cmd, ["unbind-key", "-q", "-T", "root", "M-MouseDown3Status"])
    assert contains_subsequence(
        cmd,
        ["unbind-key", "-q", "-T", "root", "MouseDown3StatusLeft"],
    )
    assert contains_subsequence(
        cmd,
        ["unbind-key", "-q", "-T", "root", "M-MouseDown3StatusLeft"],
    )


@pytest.mark.asyncio
async def test_launch_strips_env_unset_keys_from_inherited_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``env_unset`` removes ambient parent env vars from the tmux child.

    A terminal that lists a key in ``env_unset`` must not pass that key
    through to the spawned tmux process, even when it is present in the
    parent process's environment. This is the mechanism the runner uses
    to keep ambient Databricks-SDK profile selection
    (``DATABRICKS_CONFIG_PROFILE``) out of the Claude terminal: MCP
    servers spawned by Claude inherit the tmux env, and the Databricks
    SDK's auth resolver will pick up an ambient profile in preference
    to an explicit token, sending requests with a bearer for the wrong
    workspace.

    A direct unit on ``env_unset`` is the right layer for this
    invariant: a workflow integration test would only fail when the
    downstream auth failure surfaces, while this test fails the
    moment the strip step regresses.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured_envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the env passed to tmux and return a successful process.

        :param cmd: Tmux command argv (unused by this assertion).
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del cmd, stdout, stderr
        captured_envs.append(dict(env))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    # Force the unwanted var into the parent env so the test would
    # also catch a regression where ``env_unset`` is silently dropped.
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient-host-profile")
    # A benign ambient var that is NOT in env_unset — proves the strip
    # is surgical rather than a wholesale wipe. (We can't use
    # ``OMNIGENT_TMUX_SOCK`` for this any more: the sandbox hardening
    # stopped ``launch`` from advertising the control-socket path to the pane.)
    monkeypatch.setenv("OMNIGENT_BENIGN_SENTINEL", "keep-me")
    # Seed an inherited OMNIGENT_TMUX_SOCK so the negative assertion
    # below exercises ``launch``'s explicit ``env.pop`` of any ambient
    # value — not merely the fact that launch stopped *setting* it
    # (``launch`` strips both the self-set and any inherited value).
    monkeypatch.setenv("OMNIGENT_TMUX_SOCK", "/leaked/from/parent.sock")

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        env_unset=["DATABRICKS_CONFIG_PROFILE"],
    )

    await instance.launch(cwd=tmp_path)

    # 1 = the single launch invocation. Zero would mean tmux was never
    # spawned (test setup broken); >1 would mean an extra spawn slipped
    # past env stripping and the assertion below may miss it.
    assert len(captured_envs) == 1, (
        f"Expected exactly one tmux spawn during launch(), "
        f"got {len(captured_envs)}. If 0, the fake "
        f"create_subprocess_exec was not hooked. If >1, the strip "
        f"logic may not apply uniformly across spawns."
    )
    spawned_env = captured_envs[0]

    # The core invariant: the var listed in ``env_unset`` must be
    # absent from the spawned env even though it was set on the
    # parent. A failure here means the leak path the runner relies on
    # for Claude MCP isolation is open again.
    assert "DATABRICKS_CONFIG_PROFILE" not in spawned_env, (
        "env_unset failed to strip DATABRICKS_CONFIG_PROFILE from "
        "the tmux child environment. The runner's Claude terminal "
        "relies on this strip to keep ambient profile selection out "
        "of MCP-server auth resolution; if this regresses, Claude's "
        "Databricks-backed MCPs (slack, github, etc.) will start "
        "auth-failing again whenever the parent shell sets the var."
    )

    # Sanity check that ordinary env still flows through — the strip
    # must be surgical, not a wholesale wipe. The benign ambient var
    # set above must survive since it is not in ``env_unset``.
    assert spawned_env.get("OMNIGENT_BENIGN_SENTINEL") == "keep-me", (
        "benign ambient var missing from tmux env — env_unset "
        "must remove only the listed keys, not the entire env."
    )
    # And the control-socket path must NOT be advertised to the pane
    # the tmux server is unsandboxed, so a pane that knows
    # the socket path could ``tmux -S <sock> run-shell`` out of the box.
    assert "OMNIGENT_TMUX_SOCK" not in spawned_env, (
        "OMNIGENT_TMUX_SOCK leaked into the tmux child env — the pane "
        "must not be told the unsandboxed control socket's path."
    )


@pytest.mark.asyncio
async def test_launch_default_env_unset_leaks_databricks_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without ``env_unset``, the parent env still leaks into the tmux child.

    The companion to ``test_launch_strips_env_unset_keys_from_inherited_environment``:
    proves that the strip is opt-in via the field, not a hidden global
    behavior. If this test ever fails, an unrelated change has started
    stripping ``DATABRICKS_CONFIG_PROFILE`` from every terminal — that
    is a wider behavior change than the original fix intended and
    deserves a deliberate decision, not a silent regression.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured_envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the env passed to tmux and return a successful process.

        :param cmd: Tmux command argv (unused).
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del cmd, stdout, stderr
        captured_envs.append(dict(env))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "ambient-host-profile")

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        # No ``env_unset`` — the default behavior is to inherit the
        # parent env untouched.
    )

    await instance.launch(cwd=tmp_path)

    assert len(captured_envs) == 1
    spawned_env = captured_envs[0]
    # The same value the parent set must reach the child, proving
    # the strip is gated on ``env_unset`` rather than always on.
    assert spawned_env.get("DATABRICKS_CONFIG_PROFILE") == "ambient-host-profile", (
        "Expected default launch to inherit DATABRICKS_CONFIG_PROFILE "
        "from the parent env. If this fails, some other code path "
        "is unconditionally stripping the var — the runner's "
        "explicit env_unset is no longer the single source of truth "
        "for which terminals see the profile."
    )


@pytest.mark.asyncio
async def test_launch_strips_runner_binding_token_from_tmux_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The runner tunnel binding token never reaches the tmux child env.

    Host-spawned ``claude-native`` / ``codex-native`` agents
    run their shell inside this tmux pane, so a binding token in the
    pane's env lets the agent payload impersonate the runner against the
    control-plane tunnel. The token is seeded into BOTH the parent env
    and the per-terminal ``env`` overrides, proving the strip runs after
    ``env.update(self.env)`` and so cannot be re-admitted by a spec
    author. A benign override proves the strip is surgical, not a
    wholesale wipe.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Seeds the binding token into the parent env.
    """
    captured_envs: list[dict[str, str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
        env: dict[str, str],
    ) -> _SuccessfulProcess:
        """
        Capture the env passed to tmux and return a successful process.

        :param cmd: Tmux command argv (unused).
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :param env: Environment passed to the subprocess.
        :returns: A successful fake process.
        """
        del cmd, stdout, stderr
        captured_envs.append(dict(env))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
        ),
    )

    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, "from-parent-env")

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        # A spec author re-admitting the token via per-terminal env must
        # not win: the strip runs after ``env.update(self.env)``.
        env={
            RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "from-spec-env",
            "BENIGN_TERMINAL_MARKER": "marker-value",
        },
    )

    await instance.launch(cwd=tmp_path)

    assert len(captured_envs) == 1, (
        f"Expected exactly one tmux spawn during launch(), got "
        f"{len(captured_envs)}. If 0, the fake create_subprocess_exec "
        f"was not hooked; if >1, the strip may not apply to every spawn."
    )
    spawned_env = captured_envs[0]

    # Core invariant: token absent despite being set on both the parent
    # env and the per-terminal override. Presence here means the agent's
    # in-pane shell could read the runner's control-plane credential.
    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in spawned_env, (
        "binding token leaked into the tmux child env"
    )
    assert "from-parent-env" not in spawned_env.values()
    assert "from-spec-env" not in spawned_env.values()
    # Benign override survived — the strip is targeted at the secret.
    assert spawned_env.get("BENIGN_TERMINAL_MARKER") == "marker-value", (
        "per-terminal env override was dropped — the strip must remove "
        "only the runner-auth secret, not the whole env."
    )
    # The control-socket path must not be advertised to the
    # pane — the unsandboxed tmux server's run-shell would otherwise be
    # one ``tmux -S <sock>`` away for the agent payload in the pane.
    assert "OMNIGENT_TMUX_SOCK" not in spawned_env


@pytest.mark.asyncio
async def test_send_chunks_long_literal_text_under_tmux_command_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Long literal text is split across multiple ``send-keys -l`` calls.

    tmux rejects any single client command over its 16KB imsg cap with
    "command too long", so an unchunked 20KB literal would be rejected
    and the text silently lost. Fails if ``send`` regresses to one
    oversized invocation, or if chunk boundaries drop, duplicate, or
    reorder characters.

    :param tmp_path: Temporary directory used for the fake tmux socket.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    captured: list[list[str]] = []

    async def fake_create_subprocess_exec(
        *cmd: str,
        stdout: object,
        stderr: object,
    ) -> _SuccessfulProcess:
        """
        Capture the tmux argv and return a successful process.

        :param cmd: Tmux command argv.
        :param stdout: Captured stdout redirection.
        :param stderr: Captured stderr redirection.
        :returns: A successful fake process.
        """
        del stdout, stderr
        captured.append(list(cmd))
        return _SuccessfulProcess()

    monkeypatch.setattr(
        terminal_mod,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=fake_create_subprocess_exec,
            subprocess=terminal_mod.asyncio.subprocess,
            # ``send`` awaits a real 50ms settle between text and keys.
            sleep=terminal_mod.asyncio.sleep,
        ),
    )

    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
    )
    instance.running = True

    # 20,000 chars exceeds tmux's 16KB per-command cap outright — before
    # chunking this exact call failed with "command too long".
    text = "a" * 20_000
    result = await instance.send(text=text)

    assert result == {"status": "sent"}
    # 20 literal chunks (19 x 1,024 + 544) plus the trailing Enter. One
    # literal call means chunking regressed to a single rejected
    # invocation; a different count means the chunk size drifted.
    assert len(captured) == 21, (
        f"Expected 20 chunked send-keys -l calls + 1 Enter, got {len(captured)}."
    )
    literal_calls, enter_call = captured[:-1], captured[-1]
    chunks: list[str] = []
    for call in literal_calls:
        # Every chunk repeats the full literal-mode flag prefix — a chunk
        # missing ``-l`` would be interpreted as tmux key names instead
        # of literal text.
        assert contains_subsequence(call, ["send-keys", "-l", "-t", "main"])
        chunk = call[-1]
        # 1,024 chars pack to at most ~4KB on tmux's wire protocol —
        # the margin under the 16KB cap this chunking exists to respect.
        assert len(chunk) <= 1_024, (
            f"send-keys -l call carries {len(chunk)} chars; oversized chunks "
            f"risk tmux's 16KB per-command cap ('command too long')."
        )
        chunks.append(chunk)
    # Character-exact reassembly across chunk boundaries: the pane must
    # receive the same stream a single invocation would have carried.
    assert "".join(chunks) == text
    assert contains_subsequence(enter_call, ["send-keys", "-t", "main", "Enter"])


def test_idle_detector_honors_short_threshold_override() -> None:
    """A per-watcher ``idle_threshold_s`` override fires idle sooner.

    The claude-native status watcher passes a short threshold so the
    session flips to ``idle`` promptly after Claude stops redrawing. This
    proves the parameter is actually honored by the detector — not
    silently ignored in favour of the long module default (which would
    leave the status stuck "running", reintroducing the very lag the PTY
    approach removes).

    Both detectors get the same two identical snapshots back-to-back, so
    the only variable is the threshold. With a ~0s override, the second
    (unchanged) tick crosses the idle edge immediately because any
    elapsed monotonic time clears it. With the long module default the
    same two ticks stay below the threshold, so no idle fires.
    """
    snapshot = "claude idle prompt\n"

    # ``0.0`` override: the first tick primes the baseline, the second
    # (identical) tick clears the near-zero threshold → idle edge.
    fast = terminal_mod._IdleDetector(idle_threshold_s=0.0)
    assert fast.tick(snapshot) is False  # primes _last_snapshot baseline
    assert fast.tick(snapshot) is True  # unchanged + 0s threshold → idle

    # No override → module default (10s). Two rapid identical ticks are
    # nowhere near 10s apart, so the idle edge must NOT fire. If this
    # returned True, the threshold parameter would be doing nothing.
    default = terminal_mod._IdleDetector()
    assert default.tick(snapshot) is False  # primes baseline
    assert default.tick(snapshot) is False  # <10s elapsed → not idle


def test_idle_detector_suppress_activity_discounts_client_driven_repaint() -> None:
    """A change flagged ``suppress_activity`` is not counted as activity.

    The watcher sets ``suppress_activity`` when a web client interacted
    with the terminal within the recent window (attach/detach reflow,
    focus, mouse, keystroke). The detector must re-baseline such a change
    WITHOUT flagging ``changed_this_tick`` — otherwise a client attaching,
    detaching, focusing, clicking, or typing would flip the session to
    "running" — while a change with the flag clear still registers as
    agent activity.
    """
    detector = terminal_mod._IdleDetector()

    # Baseline.
    assert detector.tick("screen-A") is False
    assert detector.changed_this_tick is False

    # A client-driven repaint (suppress_activity=True): re-baselined, not
    # activity. If this flipped changed_this_tick True, attach/detach/
    # focus/typing would mark the session running.
    assert detector.tick("screen-B reflowed", suppress_activity=True) is False
    assert detector.changed_this_tick is False, (
        "a change within the client-interaction window must not register as PTY activity"
    )

    # A subsequent change with no recent interaction (flag clear) is real
    # agent output and DOES register — suppression must not be sticky.
    assert detector.tick("screen-C agent output") is False
    assert detector.changed_this_tick is True, (
        "agent output outside the client-interaction window must register as "
        "activity; suppression must apply only to the flagged tick"
    )


def _write_instance_dir(root: Path, name: str, owner_pid: int | None) -> Path:
    """
    Create a fake terminal instance dir under the sweep root.

    :param root: Fake temp root the sweep scans.
    :param name: Directory name, e.g. ``"omnigent-terminal-dead1"``.
    :param owner_pid: Owner pid to record, or ``None`` for no marker
        (an unrelated / pre-marker dir the sweep must not touch).
    :returns: The created directory path.
    """
    instance_dir = root / name
    instance_dir.mkdir()
    if owner_pid is not None:
        (instance_dir / "owner.pid").write_text(str(owner_pid), encoding="utf-8")
    return instance_dir


def _dead_pid() -> int:
    """
    Return the pid of a real process that has already exited.

    Spawning and reaping a child guarantees ``os.kill(pid, 0)`` raises
    ``ProcessLookupError`` for it (modulo astronomically unlikely
    immediate pid reuse), which is the reaper's definition of a dead
    owner.

    :returns: A pid with no live process behind it.
    """
    import subprocess
    import sys

    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


def test_reap_orphaned_terminals_reaps_only_dead_owner_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The orphan sweep removes dead-owner dirs and nothing else.

    Three instance dirs: a dead owner (must be reaped — this is the
    leak the sweep exists for: detached tmux outliving a SIGKILL'd
    runner), a live owner (another runner's terminal — must survive),
    and no marker (unknown provenance — must survive). None has a tmux
    socket, so ``kill-server`` must never be invoked; the subprocess
    stub raises if it is.

    :param tmp_path: Fake temp root the sweep scans.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    import os

    def _raise_if_called(*args: object, **kwargs: object) -> None:
        """Fail the test if kill-server runs with no socket present."""
        raise AssertionError(f"kill-server must not run without a socket: {args} {kwargs}")

    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(
        terminal_mod,
        "subprocess",
        SimpleNamespace(run=_raise_if_called, TimeoutExpired=TimeoutError),
    )
    dead_dir = _write_instance_dir(tmp_path, "omnigent-terminal-dead1", _dead_pid())
    live_dir = _write_instance_dir(tmp_path, "omnigent-terminal-live1", os.getpid())
    unmarked_dir = _write_instance_dir(tmp_path, "omnigent-terminal-old1", None)

    reaped = terminal_mod.reap_orphaned_terminals()

    # Exactly the dead-owner dir is reaped. 0 means the dead-owner
    # detection regressed (the CI leak returns); >1 means a live or
    # unknown terminal was destroyed.
    assert reaped == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()


def test_reap_orphaned_terminals_kills_server_for_dead_owner_socket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A dead-owner instance with a socket gets ``tmux kill-server``.

    Removing the dir alone leaves the detached tmux server running on
    the unlinked socket — the actual resource leak — so the sweep must
    issue ``kill-server`` against that socket before deleting.

    :param tmp_path: Fake temp root the sweep scans.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    kill_calls: list[list[str]] = []

    def _record_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        """Record the kill-server argv and report success."""
        kill_calls.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: tmp_path)
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)
    monkeypatch.setattr(
        terminal_mod,
        "subprocess",
        SimpleNamespace(run=_record_run, TimeoutExpired=TimeoutError),
    )
    dead_dir = _write_instance_dir(tmp_path, "omnigent-terminal-dead2", _dead_pid())
    socket_path = dead_dir / "tmux.sock"
    socket_path.touch()

    reaped = terminal_mod.reap_orphaned_terminals()

    assert reaped == 1
    assert not dead_dir.exists()
    # kill-server targeted exactly this instance's socket; a missing
    # call means the tmux server (the real leak) survives dir removal.
    assert kill_calls == [["tmux", "-S", str(socket_path), "kill-server"]]


@pytest.mark.skipif(
    sys.platform not in ("linux", "darwin"),
    reason="sandbox backends only resolve on Linux (bwrap) or macOS (seatbelt)",
)
def test_create_terminal_instance_denies_control_socket_but_keeps_private_dir_writable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A sandboxed terminal keeps its ``private_dir`` writable yet denies
    the pane access to the tmux control socket inside it.

    The socket must stay at ``private_dir/tmux.sock`` so the
    orphan-reaper (which kills ``<instance-dir>/tmux.sock``) still
    works, and ``private_dir`` must remain a write root so a forked
    workspace is usable. The escape is closed instead by adding the
    socket to ``deny_unix_socket_paths`` — bwrap masks it with
    /dev/null and seatbelt emits a unix-socket deny. We assert all
    three facts on the resolved policy at once because they are
    co-dependent: dropping any one re-opens the escape or breaks
    usability.
    """
    import shutil

    # create_terminal_instance only guards on tmux availability (it does
    # not launch tmux during construction), so faking the predicate lets
    # this run in CI without tmux installed — same trick the reaper tests
    # use — instead of an invisible-coverage-loss skip.
    monkeypatch.setattr(terminal_mod, "_tmux_available", lambda: True)

    backend_type = "linux_bwrap" if sys.platform == "linux" else "darwin_seatbelt"
    spec = TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type=backend_type),
        ),
    )

    result = create_terminal_instance(name="bash", session_key="s1", spec=spec)
    instance = result.instance
    try:
        assert instance.socket_path.parent == instance.private_dir, (
            "socket must live inside private_dir so reap_orphaned_terminals "
            "(which kills <instance-dir>/tmux.sock) can still reach it"
        )
        policy = instance.sandbox_policy
        assert policy is not None and policy.active, "expected an active sandbox policy"

        resolved_sock = instance.socket_path.resolve(strict=False)
        resolved_private = instance.private_dir.resolve(strict=False)

        assert policy.deny_unix_socket_paths is not None
        assert resolved_sock in policy.deny_unix_socket_paths, (
            "tmux control socket was not added to the sandbox deny list — "
            "the pane could connect to the unsandboxed server and run-shell out"
        )
        assert resolved_private in policy.write_roots, (
            "private_dir dropped from write roots — a forked workspace would "
            "become read-only inside the pane"
        )
    finally:
        shutil.rmtree(instance.private_dir, ignore_errors=True)
