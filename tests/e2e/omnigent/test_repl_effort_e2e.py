"""E2E: /effort command in the real Omnigent REPL under pexpect (mock LLM).

Migrated to mock LLM: the test only exercises slash commands, no
LLM turn is needed, so mock credentials suffice.
"""

from __future__ import annotations

from pathlib import Path

from tests.e2e.omnigent._pexpect_harness import (
    clean_exit,
    spawn_omnigent_run,
    submit_prompt,
)

_MODEL = "mock-effort-model"
_HARNESS = "openai-agents"
_SPAWN_TIMEOUT = 90.0
_BOOT_TIMEOUT = 60.0
_EXIT_TIMEOUT = 15.0


def _submit_slash_command(child, text: str) -> None:  # type: ignore[no-untyped-def]
    """Submit a slash command under prompt-toolkit/pexpect."""
    submit_prompt(child, text)


def test_repl_effort_command_show_set_reset(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
) -> None:
    """Drive /effort through its show / set / reset state machine."""
    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    env = dict(mock_credentials_env)
    env["PYTHONPATH"] = f"{omnigent_repo_root}:{omnigent_repo_root / 'sdks' / 'python-client'}" + (
        f":{env['PYTHONPATH']}" if env.get("PYTHONPATH") else ""
    )
    child = spawn_omnigent_run(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        model=_MODEL,
        harness=_HARNESS,
        env=env,
        cwd=omnigent_repo_root,
        timeout=_SPAWN_TIMEOUT,
    )
    try:
        child.expect(r"\u276f ", timeout=_BOOT_TIMEOUT)

        _submit_slash_command(child, "/effort")
        child.expect("reasoning effort: default", timeout=10)
        child.expect("options:", timeout=10)

        _submit_slash_command(child, "/effort high")
        child.expect("reasoning effort set to high", timeout=10)

        _submit_slash_command(child, "/effort")
        child.expect("reasoning effort: high", timeout=10)

        child.send("\r")
        child.expect(r"\u276f ", timeout=10)

        _submit_slash_command(child, "/effort default")
        child.expect("reasoning effort reset to agent default", timeout=10)

        clean_exit(child, timeout=_EXIT_TIMEOUT)
        assert child.exitstatus in (0, None)
        assert child.signalstatus is None
    finally:
        if not child.closed:
            child.close(force=True)
