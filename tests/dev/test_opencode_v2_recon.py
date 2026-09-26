"""Tests for the pure (network-free) helpers in dev/opencode_v2_recon.py."""

from __future__ import annotations

from pathlib import Path

from dev.opencode_v2_recon import (
    ASK_ALL_PERMISSIONS,
    build_arg_parser,
    build_recon_opencode_config,
    redact_secrets,
)


def test_arg_parser_requires_model() -> None:
    parser = build_arg_parser()
    with __import__("pytest").raises(SystemExit):
        parser.parse_args([])


def test_arg_parser_defaults() -> None:
    parser = build_arg_parser()
    args = parser.parse_args(["--model", "anthropic/claude-sonnet-4-5"])
    assert args.model == "anthropic/claude-sonnet-4-5"
    assert args.opencode_path is None
    assert args.keep_workdir is False
    assert (
        args.out_dir == Path("tests/fixtures/opencode_v2").resolve()
        or args.out_dir.name == "opencode_v2"
    )


def test_redact_secrets_replaces_every_occurrence() -> None:
    text = "password=hunter2 path=/tmp/hunter2/foo secret=hunter2"
    redacted = redact_secrets(text, ["hunter2"])
    assert "hunter2" not in redacted
    assert redacted.count("<redacted>") == 3


def test_redact_secrets_prefers_longer_matches_first() -> None:
    text = "/tmp/opencode-v2-recon-abc123/nested"
    redacted = redact_secrets(text, ["/tmp/opencode-v2-recon-abc123", "abc123"])
    assert redacted == "<redacted>/nested"


def test_redact_secrets_ignores_blank_entries() -> None:
    assert redact_secrets("hello world", ["", None]) == "hello world"  # type: ignore[list-item]


def test_build_recon_opencode_config_shape(tmp_path: Path) -> None:
    instructions_path = tmp_path / "instructions.txt"
    plugin_path = tmp_path / "plugin.js"
    config = build_recon_opencode_config(
        instructions_path=instructions_path,
        mcp_server_command=["python3", "server.py"],
        plugin_path=plugin_path,
    )
    assert config["permissions"] == ASK_ALL_PERMISSIONS
    assert config["instructions"] == [str(instructions_path)]
    assert config["mcp"]["servers"]["recon-echo"] == {
        "type": "local",
        "command": ["python3", "server.py"],
        "codemode": False,
    }
    assert config["plugins"] == [str(plugin_path)]
