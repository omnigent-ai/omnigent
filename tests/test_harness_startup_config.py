"""Unit tests for :mod:`omnigent.harness_startup_config`."""

from __future__ import annotations

import pytest

from omnigent.harness_startup_config import (
    resolve_harness_args,
    resolve_harness_command,
    resolve_harness_config,
    resolve_harness_path,
)

# ── resolve_harness_config ───────────────────────────────────────────


def test_scalar_harness_returns_default_no_overrides() -> None:
    assert resolve_harness_config({"harness": "claude-sdk"}) == ("claude-sdk", {})


def test_absent_harness_returns_none_no_overrides() -> None:
    assert resolve_harness_config({}) == (None, {})


def test_mapping_with_default_and_overrides() -> None:
    cfg = {
        "harness": {
            "default": "claude-sdk",
            "claude-sdk": {"command": "/usr/local/bin/claude"},
            "codex": {"args": ["--config", "approval_policy=on-request"]},
        }
    }
    default, overrides = resolve_harness_config(cfg)
    assert default == "claude-sdk"
    assert overrides == {
        "claude-sdk": {"command": "/usr/local/bin/claude"},
        "codex": {"args": ["--config", "approval_policy=on-request"]},
    }


def test_alias_canonicalizes_to_one_override_slot() -> None:
    # ``claude`` is an alias for ``claude-sdk``; both should land in the
    # same slot, with a later entry's fields merging in.
    cfg = {
        "harness": {
            "claude": {"command": "/bin/claude"},
            "claude-sdk": {"args": ["--dangerously-skip-permissions"]},
        }
    }
    _, overrides = resolve_harness_config(cfg)
    assert overrides == {
        "claude-sdk": {
            "command": "/bin/claude",
            "args": ["--dangerously-skip-permissions"],
        }
    }


def test_non_string_default_warns_and_skips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, overrides = resolve_harness_config({"harness": {"default": 123}})
    assert overrides == {}
    assert "harness.default" in capsys.readouterr().err


def test_non_string_override_key_warns_and_skips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    default, overrides = resolve_harness_config({"harness": {1: {"command": "/bin/codex"}}})
    assert default is None
    assert overrides == {}
    assert "keys must be strings" in capsys.readouterr().err


def test_non_mapping_harness_warns_and_returns_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    default, overrides = resolve_harness_config({"harness": ["claude-sdk"]})
    assert default is None
    assert overrides == {}
    assert "harness:" in capsys.readouterr().err


def test_malformed_entry_warns_and_skips(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = {
        "harness": {
            "codex": "not-a-mapping",
            "pi": {"command": "/bin/pi", "args": "not-a-list"},
            "kimi": {"command": ""},
        }
    }
    _, overrides = resolve_harness_config(cfg)
    # codex dropped (not a mapping); pi.command kept, pi.args dropped; kimi.command
    # dropped (empty).
    assert overrides == {"pi": {"command": "/bin/pi"}}
    err = capsys.readouterr().err
    assert "harness.codex" in err
    assert "harness.pi.args" in err
    assert "harness.kimi.command" in err


def test_args_must_be_list_of_strings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cfg = {"harness": {"codex": {"args": [1, 2]}}}
    _, overrides = resolve_harness_config(cfg)
    assert overrides == {}
    assert "harness.codex.args" in capsys.readouterr().err


# ── resolve_harness_command ──────────────────────────────────────────


def test_command_explicit_flag_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "/env/codex")
    cfg = {"harness": {"codex": {"command": "/config/codex"}}}
    assert (
        resolve_harness_command("codex", default="codex", explicit="/explicit/codex", cfg=cfg)
        == "/explicit/codex"
    )


def test_command_env_var_wins_over_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "/env/codex")
    cfg = {"harness": {"codex": {"command": "/config/codex"}}}
    assert (
        resolve_harness_command("codex", default="codex", explicit=None, cfg=cfg) == "/env/codex"
    )


def test_command_config_wins_over_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CODEX_PATH", raising=False)
    cfg = {"harness": {"codex": {"command": "/config/codex"}}}
    assert (
        resolve_harness_command("codex", default="codex", explicit=None, cfg=cfg)
        == "/config/codex"
    )


def test_command_legacy_env_wins_over_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deprecated ``HARNESS_*_PATH`` env var still wins over config ``command``.

    Per the shared ``env > config > default`` precedence, the legacy env var
    must not be shadowed by a config override — otherwise a user migrating
    from ``HARNESS_*_PATH`` to the new config form would silently get the
    config value instead of their env var during the deprecation window.
    """
    from omnigent.harness_startup_config import _LEGACY_PATH_WARNED

    _LEGACY_PATH_WARNED.discard("HARNESS_CODEX_PATH")
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PATH", "/legacy/env/codex")
    cfg = {"harness": {"codex": {"command": "/config/codex"}}}
    assert (
        resolve_harness_command("codex", default="codex", explicit=None, cfg=cfg)
        == "/legacy/env/codex"
    )


def test_command_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    assert resolve_harness_command("codex", default="codex", explicit=None, cfg={}) == "codex"


def test_command_canonical_id_for_native_harness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``codex-native`` strips the ``-native`` suffix → ``OMNIGENT_CODEX_PATH``
    # (shared with the headless ``codex`` harness — one var per binary).
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "/env/codex-native")
    assert (
        resolve_harness_command("codex-native", default="codex", explicit=None, cfg={})
        == "/env/codex-native"
    )


def test_command_alias_resolves_to_canonical_env_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``claude`` alias → ``claude-sdk`` which runs the ``claude`` binary, so the
    # env var is ``OMNIGENT_CLAUDE_PATH`` (the binary's var, not the id's var).
    monkeypatch.delenv("OMNIGENT_CLAUDE_SDK_PATH", raising=False)
    monkeypatch.setenv("OMNIGENT_CLAUDE_PATH", "/env/claude")
    assert (
        resolve_harness_command("claude", default="claude", explicit=None, cfg={}) == "/env/claude"
    )


def test_command_empty_explicit_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "/env/codex")
    # An empty --command flag should not shadow the env var.
    assert (
        resolve_harness_command("codex", default="codex", explicit="   ", cfg={}) == "/env/codex"
    )


# ── resolve_harness_path (env deprecation) ──────────────────────────


def test_resolve_harness_path_canonical_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "/canonical/codex")
    monkeypatch.setenv("HARNESS_CODEX_PATH", "/legacy/codex")
    assert resolve_harness_path("codex") == "/canonical/codex"


def test_resolve_harness_path_legacy_env_warns(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """A legacy ``HARNESS_<NAME>_PATH`` value is returned + a deprecation warning."""
    from omnigent.harness_startup_config import _LEGACY_PATH_WARNED

    _LEGACY_PATH_WARNED.discard("HARNESS_CODEX_PATH")  # ensure not pre-warned
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PATH", "/legacy/codex")

    with caplog.at_level("WARNING"):
        assert resolve_harness_path("codex") == "/legacy/codex"

    assert any(
        "HARNESS_CODEX_PATH" in r.message and "deprecated" in r.message and "v0.8.0" in r.message
        for r in caplog.records
    )


def test_resolve_harness_path_legacy_warns_only_once(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The deprecation warning fires once per process per legacy var."""
    from omnigent.harness_startup_config import _LEGACY_PATH_WARNED

    _LEGACY_PATH_WARNED.discard("HARNESS_CODEX_PATH")
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.setenv("HARNESS_CODEX_PATH", "/legacy/codex")

    with caplog.at_level("WARNING"):
        resolve_harness_path("codex")
        resolve_harness_path("codex")
        resolve_harness_path("codex")

    warns = [
        r
        for r in caplog.records
        if "HARNESS_CODEX_PATH" in r.message and "deprecated" in r.message
    ]
    assert len(warns) == 1


def test_resolve_harness_path_neither_set_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    monkeypatch.delenv("HARNESS_CODEX_PATH", raising=False)
    assert resolve_harness_path("codex") is None


def test_resolve_harness_path_ignores_non_registry_legacy_var(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A speculative ``HARNESS_*_PATH`` for a harness that never had one is ignored.

    Only the 6 headless harnesses (codex/pi/kimi/goose/qwen/hermes) historically
    documented a ``HARNESS_*_PATH``. Other harnesses (e.g. cursor) never did —
    honoring ``HARNESS_CURSOR_PATH`` would invent a new knob under a deprecated
    name, so it's ignored (only the canonical ``OMNIGENT_CURSOR_PATH`` works).
    """
    monkeypatch.delenv("OMNIGENT_CURSOR_PATH", raising=False)
    monkeypatch.setenv("HARNESS_CURSOR_PATH", "/speculative/cursor")
    assert resolve_harness_path("cursor") is None


def test_resolve_harness_path_strips_native_suffix() -> None:
    """pi-native and pi share OMNIGENT_PI_PATH."""
    import omnigent.harness_startup_config as m

    assert m._harness_path_env_var("pi-native") == "OMNIGENT_PI_PATH"
    assert m._harness_path_env_var("pi") == "OMNIGENT_PI_PATH"


# ── resolve_harness_args ──────────────────────────────────────────────


def test_args_cli_only_when_no_config() -> None:
    assert resolve_harness_args("codex", ("--verbose",), cfg={}) == ["--verbose"]


def test_args_config_base_then_cli() -> None:
    cfg = {"harness": {"codex": {"args": ["--config", "k=v"]}}}
    assert resolve_harness_args("codex", ("--dangerously-skip-permissions",), cfg=cfg) == [
        "--config",
        "k=v",
        "--dangerously-skip-permissions",
    ]


def test_args_config_base_with_empty_cli() -> None:
    cfg = {"harness": {"codex": {"args": ["--config", "k=v"]}}}
    assert resolve_harness_args("codex", (), cfg=cfg) == ["--config", "k=v"]


def test_args_alias_canonicalized() -> None:
    # ``claude`` alias → ``claude-sdk`` override slot.
    cfg = {"harness": {"claude-sdk": {"args": ["--base"]}}}
    assert resolve_harness_args("claude", ("--cli",), cfg=cfg) == ["--base", "--cli"]


def test_args_no_config_layer() -> None:
    assert resolve_harness_args("codex", ("--verbose",), cfg=None) == ["--verbose"]


# ── isaac wrapper shape (the runner auto-create's contract) ──────────
# The runner's native terminal auto-create resolves command+args through the
# resolvers above; these pin the exact `isaac -- <args>` / `isaac codex --
# <args>` shape a downstream integration configures. The config `args` base
# prepends before Omnigent's already-augmented args, so the `--` stays first.
def test_isaac_wrap_shape_claude_native() -> None:
    cfg = {"harness": {"claude-native": {"command": "isaac", "args": ["--"]}}}
    assert resolve_harness_command("claude-native", default="claude", cfg=cfg) == "isaac"
    built = ("--model", "x", "--mcp-config", "{}")
    assert resolve_harness_args("claude-native", built, cfg=cfg) == ["--", *built]


def test_isaac_wrap_shape_codex_native() -> None:
    cfg = {"harness": {"codex-native": {"command": "isaac", "args": ["codex", "--"]}}}
    assert resolve_harness_command("codex-native", default="codex", cfg=cfg) == "isaac"
    built = ("--remote", "ws://127.0.0.1:9876")
    assert resolve_harness_args("codex-native", built, cfg=cfg) == ["codex", "--", *built]


# ── CLI persists RAW args; the runner is the single merge point ──────


@pytest.mark.parametrize("harness", ["codex", "claude"])
def test_native_cli_persists_raw_args_not_config_merged(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The native ``codex``/``claude`` CLI must hand *raw* pass-through args to
    ``run_*_native``. The daemon runner is the single ``harness.<id>.args`` merge
    point (``_auto_create_*_terminal``), and every CLI launch reaches it; if the
    CLI also pre-merged, a managed session wrapped twice (``isaac codex -- codex
    -- …``), which broke the launch. Regression for that double-apply.
    """
    import click
    from click.testing import CliRunner

    import omnigent.claude_native as claude_native
    import omnigent.cli as _cli
    import omnigent.cli_native as cli_native
    import omnigent.codex_native as codex_native

    cfg = {
        "harness": {
            "claude-native": {"command": "isaac", "args": ["--"]},
            "codex-native": {"command": "isaac", "args": ["codex", "--"]},
        }
    }
    # Both are late-bound in cli_native to the omnigent.cli module (see _late_bound).
    monkeypatch.setattr(_cli, "_load_effective_config", lambda: dict(cfg))
    monkeypatch.setattr(_cli, "_ensure_backend", lambda server: "https://example/api/2.0/omnigent")

    captured: dict[str, tuple[str, ...]] = {}
    monkeypatch.setattr(
        codex_native, "run_codex_native", lambda **kw: captured.update(args=kw["extra_args"])
    )
    monkeypatch.setattr(
        claude_native, "run_claude_native", lambda **kw: captured.update(args=kw["extra_args"])
    )

    group = click.Group()
    cli_native.register_native_commands(group)
    result = CliRunner().invoke(group, [harness, "userarg"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    # Raw pass-through only — NOT the config-merged ("codex", "--", "userarg")
    # or ("--", "userarg") that a second merge on the runner would double.
    assert captured["args"] == ("userarg",)
