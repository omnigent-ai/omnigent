"""End-to-end repro: ``omni claude -p <prompt> --<flag> <value>`` mangles arg
forwarding — the prompt is passed as the flag's path argument.

Reported user journey::

    omni claude -p "hello" --model haiku --mcp-config mcp.json

fails. ``omni claude`` collects ``--model haiku --mcp-config mcp.json`` as the
pass-through ``claude_args`` and appends the ``-p`` prompt as the **last**
positional after them. Claude Code's ``--mcp-config <configs...>`` is a
*variadic* option, so ``claude`` receives the prompt string as an extra MCP
config path and exits ``1`` with::

    Error: Invalid MCP configuration:
    MCP config file not found: <cwd>/hello

The omnigent side then surfaces only a generic 60s terminal-creation timeout
(``The runner did not create the Claude terminal ... within 60s.``), so the
real cause is invisible on the user's TTY (it lands only in the runner log).
The reported workaround is the equals form ``--mcp-config=mcp.json`` (which
keeps the path glued to the flag) with the ``-p`` value first.

What this test drives
---------------------
It reproduces the failure through the **real** product code and the **real**
``claude`` binary, deterministically and hermetically:

1. Invoke the actual ``omni claude`` Click command (real argv parsing + the
   real ``claude()`` command body → ``run_claude_native``) with the exact
   reported command line, capturing the ``claude`` args it forwards. Only the
   daemon/server bring-up (``_ensure_backend``) and the terminal-launch sink
   are stubbed — that infrastructure is what is non-hermetic in CI, not the
   arg-forwarding logic under test.
2. Run those forwarded args through the runner-side merge
   (``_build_claude_native_base_args``), the second place args are assembled
   on the daemon launch path, so both merge points are exercised.
3. Execute the **real** ``claude`` binary with the resulting argv and assert
   it does not reject the ``-p`` prompt as an MCP config path.

Why not drive the full ``omni claude`` PTY journey here? It was reproduced
live that way (a 60s ``did not create the Claude terminal`` timeout, with the
runner log showing ``MCP config file not found: <cwd>/hello``), but that path
spawns a detached daemon/server/runner whose log location and
timeout-vs-broken-terminal outcome are non-deterministic under this shared-CI
harness. The failure this guards — ``claude`` rejecting the prompt as a config
path — is fully deterministic, so the test targets it directly through the
same product code.

No Claude login is required: ``claude`` exits at argv-parse time (before any
auth) on the buggy form. When forwarding is fixed, ``claude`` accepts the
config and instead fails fast with an auth error (``Not logged in``), so the
test stays green post-fix. Gated only on the ``claude`` binary (installed by
claude-sdk in CI); tmux/PTY are not needed by this approach.

Usage::

    python -m pytest \
        tests/e2e/test_claude_native_prompt_flag_forwarding_e2e.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

pytestmark = pytest.mark.skipif(
    shutil.which("claude") is None or sys.platform == "win32",
    reason=(
        "needs the real `claude` binary (no login required: the buggy launch "
        "dies at claude's argv parse, before auth). Not applicable on Windows."
    ),
)

# A unique, whitespace-free prompt so the bug's signature — claude reporting
# it as a missing MCP config path — is unambiguous in claude's output.
_PROMPT_SENTINEL = "initial-prompt-sentinel-not-a-config-path"


def _forwarded_claude_args(use_native_config: bool, sentinel: str) -> tuple[str, ...]:
    """
    Return the ``claude`` args the real ``omni claude`` command forwards.

    Invokes the genuine ``omni claude`` Click command with the reported
    command line and captures what it hands the launch path. Only the daemon
    bring-up and the launch sink are stubbed; the arg-forwarding logic under
    test runs for real.

    :param use_native_config: When ``True``, pass ``--use-native-config`` (the
        report says plain and native-config forms are both affected).
    :param sentinel: The ``-p`` prompt value to forward.
    :returns: The forwarded ``claude`` args, prompt included.
    """
    import omnigent.cli as _cli
    import omnigent.harnesses.claude_native.main as claude_native

    captured: dict[str, Any] = {}

    def _fake_remote(base_url: str, spec_path: Path, **kwargs: Any) -> None:
        captured.update(kwargs)

    # Stub only infrastructure: never spawn a daemon/server, never launch a
    # terminal, never resolve provider/Databricks auth. The command's
    # arg-forwarding (the code under test) runs unmodified.
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(claude_native, "_run_with_remote_server", _fake_remote)
        monkey.setattr(claude_native, "_run_with_local_server", _fake_remote)
        monkey.setattr(claude_native.shutil, "which", lambda command: f"/usr/bin/{command}")
        monkey.setattr(claude_native, "resolve_native_claude_config", lambda spec: None)
        monkey.setattr(_cli, "_ensure_backend", lambda server: "https://example.test")
        monkey.setattr(_cli, "_load_effective_config", dict)
        monkey.setattr(_cli, "_resolve_auto_open_conversation_from_config", lambda cfg: False)

        argv = ["claude"]
        if use_native_config:
            argv.append("--use-native-config")
        # The reported failing form: -p prompt, then space-separated value
        # flags that omni does not itself parse (they pass through to claude).
        argv += ["-p", sentinel, "--model", "haiku", "--mcp-config", "mcp.json"]

        result = CliRunner().invoke(_cli.cli, argv, catch_exceptions=False)
    finally:
        monkey.undo()

    assert result.exit_code == 0, (
        f"`omni claude` invocation errored before forwarding args: {result.output}"
    )
    forwarded = captured.get("claude_args")
    assert forwarded is not None, "the command never reached the terminal-launch path"
    return tuple(forwarded)


@pytest.mark.parametrize("use_native_config", [False, True])
def test_prompt_is_not_forwarded_as_a_value_flags_argument(use_native_config: bool) -> None:
    """
    ``omni claude -p <prompt> --mcp-config mcp.json`` must not let claude
    swallow the prompt as an MCP config path.

    Forwards args via the real ``omni claude`` command, merges them the way
    the runner does, runs the real ``claude`` binary, and fails iff claude
    rejects the ``-p`` prompt as a missing MCP config file — the exact
    user-visible failure.

    :param use_native_config: Whether to pass ``--use-native-config`` (both
        forms are reported affected).
    """
    from omnigent.runner.native.orchestration import _build_claude_native_base_args

    forwarded = _forwarded_claude_args(use_native_config, _PROMPT_SENTINEL)

    # Sanity: the journey really forwarded the prompt (guards a vacuous pass).
    assert _PROMPT_SENTINEL in forwarded, (
        f"the prompt was not forwarded to claude at all: {forwarded!r}"
    )

    # Second merge point: the daemon-launch runner path re-assembles these as
    # base args (an explicit --model wins over the override, so this is a
    # no-op reposition here — included so a fix that only touches one merge
    # point can't slip past).
    merged = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override="haiku",
        terminal_launch_args=list(forwarded),
        resume_external_session_id=None,
    )
    assert _PROMPT_SENTINEL in merged

    workdir = Path(tempfile.mkdtemp(prefix="prompt-forwarding-"))
    try:
        # A VALID config file: the bug is that claude never gets to use it,
        # because the prompt is wedged in as a second --mcp-config path.
        (workdir / "mcp.json").write_text('{"mcpServers": {}}\n')

        # Run the real claude binary with a clean, login-free env so MCP
        # config validation happens (and fails fast) before any auth: strip
        # credentials and point HOME at an empty dir (no ~/.claude login).
        env = {
            "PATH": __import__("os").environ.get("PATH", ""),
            "HOME": str(workdir),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
        proc = subprocess.run(
            ["claude", *merged],
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    output = f"{proc.stdout}\n{proc.stderr}"

    # Guard against a vacuous pass: claude must actually have run and reported
    # something (exit + output), not been skipped.
    assert output.strip(), (
        f"claude produced no output; the binary may not have run (exit={proc.returncode})"
    )

    # The bug's signature: claude rejected the -p prompt as a missing MCP
    # config path. When forwarding is correct, claude accepts the real config
    # and fails elsewhere (e.g. `Not logged in`), so this signature is absent.
    swallowed = "MCP config file not found" in output and _PROMPT_SENTINEL in output
    assert not swallowed, (
        "claude received the -p prompt as the --mcp-config value and exited "
        f"(exit={proc.returncode}); the prompt must be forwarded so a "
        "preceding variadic value flag cannot swallow it. "
        f"Forwarded args: {merged!r}\nclaude output:\n{output.strip()}"
    )
