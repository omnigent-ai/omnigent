"""Launch + agent-seeding hooks for the echo-native example harness.

STUB SCOPE. A real native harness wraps a live vendor CLI in a tmux/PTY
session, tails its transcript, and mirrors output back into Omnigent (see
``omnigent.pi_native`` / ``omnigent.claude_native`` for the full pattern —
each is 1–2k lines of bridge + forwarder + executor). The example keeps these
hooks deliberately minimal so the *contract* is legible and the plugin is
registry-loadable and unit-testable end to end, without shipping a second real
vendor integration. Points where a real harness does substantive work are
marked ``TODO(real-harness)``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

_AGENT_NAME = "echo-native-ui"


def run_echo_native(
    *,
    server: str | None,
    session_id: str | None,
    extra_args: tuple[str, ...] | None = None,
    resume_picker: bool = False,
    auto_open_conversation: bool = False,
) -> None:
    """CLI + resume launch entry point (``run_native`` hook).

    Signature mirrors the built-in ``run_<x>_native`` launchers so the resume
    dispatch (``resume_dispatch._dispatch_wrapper``) can call it uniformly.

    :param server: Resolved Omnigent server URL, or ``None`` for local.
    :param session_id: Existing session to resume, or ``None`` for a fresh run.
    :param extra_args: Pass-through vendor CLI args.
    :param resume_picker: Whether to show the vendor's resume picker.
    :param auto_open_conversation: Whether to auto-open the conversation.
    """
    # TODO(real-harness): resolve the vendor CLI, spawn it in an Omnigent
    # terminal, and run the transcript forwarder. The stub only documents the
    # entry shape a real launcher implements.
    raise NotImplementedError(
        "echo-native is an example harness stub; run_echo_native has no live vendor CLI. "
        "See omnigent.pi_native.run_pi_native for a real implementation."
    )


def materialize_echo_agent_spec(tmpdir: Path) -> Path:
    """Write the terminal-first wrapper agent spec (``materialize_agent_spec`` hook).

    Produces the ``echo-native-ui`` built-in agent's spec YAML. This IS fully
    functional — it is what the server's seeding loop
    (``_ensure_default_native_agents``) tars into a bundle and registers, so the
    example agent shows up in the picker like any built-in native.

    :param tmpdir: Temporary directory for the generated YAML file.
    :returns: Path to the generated spec.
    """
    yaml_path = tmpdir / "echo-native-ui.yaml"
    raw: dict[str, Any] = {
        "name": _AGENT_NAME,
        "prompt": (
            "Echo is an example native harness. In a real harness the vendor CLI "
            "runs in the session terminal and web messages are forwarded into it."
        ),
        "executor": {"harness": "echo-native"},
        "spawn": True,
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    }
    yaml_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return yaml_path
