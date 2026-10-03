"""Launch config tests for Codex session."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib
import yaml

from omnigent.harnesses.codex_native import app_server as codex_native_app_server
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.spec import load

# The default-stance auto-review override normalize_codex_permission_launch_args
# adds when no explicit approval/sandbox/reviewer/profile choice is present.
_AUTO_REVIEW_ARGS = ["-c", 'approvals_reviewer="auto_review"']


@pytest.mark.parametrize(
    ("launch", "expected"),
    [
        (
            codex_native_app_server.NativeCodexLaunch([], None, None),
            False,
        ),
        (
            codex_native_app_server.NativeCodexLaunch([], None, "profile"),
            True,
        ),
        (
            codex_native_app_server.NativeCodexLaunch(['model_provider="openai"'], None, None),
            True,
        ),
    ],
)
def test_native_codex_launch_pins_model_provider(
    launch: codex_native_app_server.NativeCodexLaunch, expected: bool
) -> None:
    """Provider-pin detection is owned beside the override parser."""
    assert codex_native_app_server.native_codex_launch_pins_model_provider(launch) is expected


def test_materialize_codex_agent_spec_uses_codex_native_harness(
    tmp_path: Path, monkeypatch
) -> None:
    """
    The generated wrapper spec is self-contained and selects the
    isolated ``codex-native`` harness rather than the existing
    non-TUI ``codex`` harness.
    """
    # Pin the host shells so the declared terminals are deterministic
    # ($SHELL=bash → the default/first terminal is ``bash``).
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setenv("SHELL", "/bin/bash")
    spec_path = codex_native._materialize_codex_agent_spec(
        tmp_path,
        model="gpt-test",
    )

    raw = yaml.safe_load(spec_path.read_text(encoding="utf-8"))

    assert raw["name"] == "codex-native-ui"
    # Exact executor block: the spec must NOT carry a profile key —
    # the --profile CLI flag was removed, so routing is resolved at
    # launch time (provider config / global auth / ambient detection).
    assert raw["executor"] == {
        "harness": "codex-native",
        "model": "gpt-test",
    }


def test_materialized_codex_agent_spec_loads_as_valid_omnigent_yaml(
    tmp_path: Path,
) -> None:
    """
    The generated wrapper spec passes Omnigent YAML validation.

    This guards the session-create path, which registers the generated
    spec bundle and fails before Codex starts if ``codex-native`` is not
    accepted by the spec adapter.
    """
    spec_path = codex_native._materialize_codex_agent_spec(
        tmp_path,
        model="gpt-test",
    )

    spec = load(spec_path)

    assert spec.executor.config["harness"] == "codex-native"
    # The relay derives its tool set from this spec; a dropped spawn flag
    # silently removes sys_session_create/send/close from the native CLI.
    assert spec.spawn is True
    # A non-empty terminals block makes the relay advertise sys_terminal_*
    # tools to the native CLI, with one terminal per installed shell.
    assert spec.terminals is not None
    assert spec.terminals["bash"].command == "bash"


@pytest.mark.parametrize(
    ("codex_args", "thread_id", "remote_url", "expected"),
    [
        # Fresh thread over a Unix socket (local ``omnigent codex``
        # cold start): no ``resume``/thread id, transport passed verbatim.
        (
            (),
            None,
            "unix:///tmp/app-server.sock",
            [*_AUTO_REVIEW_ARGS, "--remote", "unix:///tmp/app-server.sock"],
        ),
        # Resume an existing thread over a Unix socket (local reattach).
        (
            (),
            "thread_local",
            "unix:///tmp/app-server.sock",
            [
                "resume",
                "--remote",
                "unix:///tmp/app-server.sock",
                "thread_local",
            ],
        ),
        # Fresh thread over a loopback ws endpoint.
        (
            (),
            None,
            "ws://127.0.0.1:9876",
            [*_AUTO_REVIEW_ARGS, "--remote", "ws://127.0.0.1:9876"],
        ),
        # Resume over the host-spawned runner's ws:// endpoint; hardcoding
        # unix:// would prevent the terminal from attaching to its server.
        (
            (),
            "thread_host",
            "ws://127.0.0.1:9876",
            ["resume", "--remote", "ws://127.0.0.1:9876", "thread_host"],
        ),
        # Leading codex args are preserved ahead of the attach flags.
        (
            ("--model", "gpt-5.4-mini"),
            "thread_x",
            "ws://127.0.0.1:9876",
            [
                "--model",
                "gpt-5.4-mini",
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_x",
            ],
        ),
        # A fresh thread still submits the positional launch prompt once.
        (
            ("--model", "gpt-5.4-mini", "Reply with INITIAL_ONCE"),
            None,
            "ws://127.0.0.1:9876",
            [
                "--model",
                "gpt-5.4-mini",
                "Reply with INITIAL_ONCE",
                *_AUTO_REVIEW_ARGS,
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
    ],
)
def test_build_codex_remote_args_passes_transport_verbatim(
    codex_args: tuple[str, ...],
    thread_id: str | None,
    remote_url: str,
    expected: list[str],
) -> None:
    """
    ``build_codex_remote_args`` emits the TUI ``--remote`` attach argv
    for both transports and both thread states.

    The transport URL is passed through verbatim so one builder serves
    both the local Unix-socket path and the host-spawned ``ws://`` path,
    and ``resume <thread_id>`` is appended iff a thread id is supplied.
    If this regressed to a hardcoded ``unix://`` prefix, the ws cases
    would fail and the host-spawned Codex terminal could not reach its
    TCP app-server (no terminal would render in the web UI).
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=codex_args,
            thread_id=thread_id,
            remote_url=remote_url,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("thread_id", "expected"),
    [
        # Fresh thread: -c overrides precede the bare --remote attach.
        (
            None,
            [
                "-c",
                'model="catalog-databricks-openai-default"',
                "-c",
                'model_provider="omnigent_databricks"',
                *_AUTO_REVIEW_ARGS,
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Resume: -c overrides are global flags and MUST precede the
        # ``resume`` subcommand (codex rejects globals placed after it).
        (
            "thread_host",
            [
                "-c",
                'model="catalog-databricks-openai-default"',
                "-c",
                'model_provider="omnigent_databricks"',
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_host",
            ],
        ),
    ],
)
def test_build_codex_remote_args_emits_config_overrides_before_subcommand(
    thread_id: str | None,
    expected: list[str],
) -> None:
    """
    ``build_codex_remote_args`` emits each ``config_overrides`` entry as a
    ``-c <value>`` global flag ahead of the attach flags.

    The ``--remote`` TUI is a separate process that does not inherit the
    app-server's ``-c`` flags; without these the TUI falls back to the
    OpenAI built-in provider (``requires_openai_auth = true``), renders
    the first-run login onboarding screen, and never creates a thread —
    so a host-spawned session hangs in ``running`` with no response.
    Asserting the exact argv (not just membership) guards two things at
    once: that the overrides are forwarded at all, and that they land
    *before* the ``resume`` subcommand — codex treats ``-c`` as a global
    option and rejects it when placed after a subcommand, which would
    abort TUI startup and reintroduce the hang.
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=(),
            thread_id=thread_id,
            remote_url="ws://127.0.0.1:9876",
            config_overrides=(
                'model="catalog-databricks-openai-default"',
                'model_provider="omnigent_databricks"',
            ),
        )
        == expected
    )


# Pre- and post-0.154 remote resume keep or strip permission flags differently;
# the positional launch prompt must be dropped on both paths.
@pytest.mark.parametrize("codex_cli_version", [None, (0, 150, 0), (0, 154, 0), (0, 159, 2)])
@pytest.mark.parametrize("bypass_sandbox", [False, True])
def test_build_codex_remote_args_resume_omits_positional_launch_prompt(
    codex_cli_version: tuple[int, int, int] | None,
    bypass_sandbox: bool,
) -> None:
    """Resuming forwards the launch configuration but not the persisted positional prompt."""
    launch_prompt = "Reply with INITIAL_ONCE"
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=("--model", "gpt-5.4-mini", launch_prompt, "--sandbox", "read-only"),
        thread_id="thread_existing",
        remote_url="ws://127.0.0.1:9876",
        config_overrides=('model_provider="omnigent"',),
        codex_cli_version=codex_cli_version,
        bypass_sandbox=bypass_sandbox,
    )
    assert args[-4:] == ["resume", "--remote", "ws://127.0.0.1:9876", "thread_existing"], args
    assert launch_prompt not in args, (
        "cold resume resubmits the original launch prompt instead of only the "
        f"new input (argv={args})"
    )
    assert args[args.index("--model") + 1] == "gpt-5.4-mini", args
    assert args[args.index("-c") + 1] == 'model_provider="omnigent"', args


@pytest.mark.parametrize(
    ("codex_args", "expected"),
    [
        # ``--flag value`` pair: both dropped.
        (("--sandbox", "read-only"), []),
        (("--ask-for-approval", "on-request"), []),
        # Option-adjacent: the next token is ANOTHER flag, not this flag's
        # value, so it must survive (the over-match bug dropped --model).
        (("--sandbox", "--model", "gpt"), ["--model", "gpt"]),
        # ``--flag=value`` single token: dropped whole, consumes nothing after.
        (("--ask-for-approval=on-failure",), []),
        (("--sandbox=read-only", "--model", "gpt"), ["--model", "gpt"]),
        # Strip short aliases -a/-s like their long forms, handling both
        # space-separated and =value spellings; -a also aborts Codex startup.
        (("-a", "never"), []),
        (("-a=never",), []),
        (("-s", "read-only"), []),
        (("-s=read-only", "--model", "gpt"), ["--model", "gpt"]),
        # Short alias option-adjacent to another flag: the next flag survives.
        (("-a", "--model", "gpt"), ["--model", "gpt"]),
        # Trailing flag at end-of-list: dropped cleanly, no value to consume.
        (("--model", "gpt", "--sandbox"), ["--model", "gpt"]),
        # Unrelated arg next to a stripped pair is preserved.
        (
            ("--model", "gpt", "--sandbox", "read-only", "--cwd", "/x"),
            ["--model", "gpt", "--cwd", "/x"],
        ),
        # A pre-existing bypass flag is de-duped (the caller re-adds one copy).
        (("--dangerously-bypass-approvals-and-sandbox", "--model", "gpt"), ["--model", "gpt"]),
        # No conflicting flags: everything passes through untouched.
        (("--model", "gpt-5.4-mini"), ["--model", "gpt-5.4-mini"]),
    ],
)
def test_strip_approval_sandbox_flags_only_consumes_real_values(
    codex_args: tuple[str, ...],
    expected: list[str],
) -> None:
    """
    ``_strip_approval_sandbox_flags`` drops the conflicting flags without
    over-matching the token that follows them.

    A ``--sandbox`` / ``--ask-for-approval`` flag consumes the next token as
    its value ONLY when that token is a real value (does not start with
    ``-``); a following flag or end-of-list consumes nothing, so unrelated
    args like ``--model gpt`` are never swallowed. The ``--flag=value``
    single-token spelling is dropped whole.
    """
    assert codex_native_app_server._strip_approval_sandbox_flags(codex_args) == expected


def test_build_codex_remote_args_default_keeps_approval_flags_no_bypass() -> None:
    """
    Default (``bypass_sandbox=False``) emits NO bypass flag and preserves the
    approval/sandbox flags the approval-mode presets pass through.

    The web "Full access" / "Read only" presets are sent as
    ``--sandbox`` / ``--ask-for-approval`` pairs inside ``codex_args``. With
    bypass off those must reach the TUI verbatim and the dangerous bypass
    flag must never appear — a regression here would either drop a user's
    chosen approval preset or silently escalate to full bypass.
    """
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=("--sandbox", "read-only", "--ask-for-approval", "on-request"),
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
    )

    assert "--dangerously-bypass-approvals-and-sandbox" not in args
    assert args == [
        "--sandbox",
        "read-only",
        "--ask-for-approval",
        "on-request",
        "--remote",
        "ws://127.0.0.1:9876",
    ]


@pytest.mark.parametrize(
    ("codex_args", "thread_id", "expected"),
    [
        # Fresh thread, no conflicting flags: a single bypass flag is prepended.
        (
            (),
            None,
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Codex aborts when approval-preset flags accompany the bypass flag.
        # Strip each flag/value pair; preserve unrelated args and add bypass once.
        (
            ("--sandbox", "danger-full-access", "--ask-for-approval", "never", "--model", "gpt"),
            None,
            [
                "--dangerously-bypass-approvals-and-sandbox",
                "--model",
                "gpt",
                "--remote",
                "ws://127.0.0.1:9876",
            ],
        ),
        # Resume inherits the bypass policy from the app-server.
        (
            ("--dangerously-bypass-approvals-and-sandbox", "--sandbox", "read-only"),
            "thread_x",
            [
                "resume",
                "--remote",
                "ws://127.0.0.1:9876",
                "thread_x",
            ],
        ),
    ],
)
def test_build_codex_remote_args_bypass_emits_flag_and_strips_conflicts(
    codex_args: tuple[str, ...],
    thread_id: str | None,
    expected: list[str],
) -> None:
    """
    Fresh sessions get one bypass flag without conflicting approval flags.
    Resumed terminals inherit that policy from the app-server.
    """
    assert (
        codex_native_app_server.build_codex_remote_args(
            codex_args=codex_args,
            thread_id=thread_id,
            remote_url="ws://127.0.0.1:9876",
            bypass_sandbox=True,
        )
        == expected
    )


def test_build_codex_remote_args_bypass_hook_trust_prepends_flag() -> None:
    """``bypass_hook_trust=True`` prepends ``--dangerously-bypass-hook-trust``.

    Runner-owned headless sessions pass this flag so the TUI skips the
    interactive "Hooks need review" prompt that can never be answered without
    a live terminal user.
    """
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=(),
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
        bypass_hook_trust=True,
    )
    assert args[0] == "--dangerously-bypass-hook-trust"
    assert "--remote" in args
    assert "ws://127.0.0.1:9876" in args


def test_build_codex_remote_args_bypass_hook_trust_with_resume() -> None:
    """``bypass_hook_trust=True`` flag precedes the ``resume`` subcommand."""
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=(),
        thread_id="thread-abc",
        remote_url="ws://127.0.0.1:9876",
        bypass_hook_trust=True,
    )
    assert args[0] == "--dangerously-bypass-hook-trust"
    assert "resume" in args
    assert args.index("--dangerously-bypass-hook-trust") < args.index("resume")


def test_build_codex_remote_args_bypass_hook_trust_default_false() -> None:
    """``bypass_hook_trust`` defaults to ``False``; flag is absent."""
    args = codex_native_app_server.build_codex_remote_args(
        codex_args=(),
        thread_id=None,
        remote_url="ws://127.0.0.1:9876",
    )
    assert "--dangerously-bypass-hook-trust" not in args


def test_trust_codex_project_updates_private_config_only(tmp_path: Path) -> None:
    """Headless startup trusts the workspace without changing shared config."""
    source_config = tmp_path / "shared-config.toml"
    source_text = '[projects."/existing"]\ntrust_level = "untrusted"\n'
    source_config.write_text(source_text, encoding="utf-8")
    codex_home = tmp_path / "private-home"
    codex_home.mkdir()
    private_config = codex_home / "config.toml"
    private_config.write_text(source_text, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    codex_native_app_server._trust_codex_project(codex_home, workspace)

    parsed = tomllib.loads(private_config.read_text(encoding="utf-8"))
    assert parsed["projects"][str(workspace.resolve())]["trust_level"] == "trusted"
    assert parsed["projects"]["/existing"]["trust_level"] == "untrusted"
    assert source_config.read_text(encoding="utf-8") == source_text


def test_build_codex_native_server_does_not_trust_project_by_default(
    tmp_path: Path,
) -> None:
    """Interactive Codex launches retain the normal project trust prompt."""
    app_server = codex_native_app_server.build_codex_native_server(
        socket_path=tmp_path / "app.sock",
        codex_home=tmp_path / "codex-home",
        cwd=tmp_path / "workspace",
        model=None,
        profile=None,
        bridge_dir=tmp_path / "bridge",
        codex_path="/opt/codex/bin/codex",
    )

    assert app_server.trust_project is False


# --- #2745: routing summary surfaced in the startup-timeout error ---


def test_native_codex_launch_summary_defaults_empty() -> None:
    """The new summary field defaults to empty so existing call sites stay valid."""
    launch = codex_native_app_server.NativeCodexLaunch(
        config_overrides=[], model=None, profile=None
    )
    assert launch.summary == ""


def test_resolve_native_codex_launch_no_provider_sets_login_fallback_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured provider -> summary names the login fallback (#2745)."""
    from omnigent.onboarding import ambient, detected, provider_config
    from omnigent.runtime import workflow

    monkeypatch.setattr(provider_config, "load_config", dict)
    monkeypatch.setattr(ambient, "codex_config_detection", lambda: None)
    monkeypatch.setattr(detected, "dismissed_detection_names", lambda cfg: frozenset())
    monkeypatch.setattr(detected, "effective_config_with_detected", lambda cfg: {})
    monkeypatch.setattr(provider_config, "default_provider_for_harness", lambda cfg, harness: None)
    monkeypatch.setattr(workflow, "_load_global_auth", lambda: None)

    launch = codex_native_app_server.resolve_native_codex_launch(model=None)

    assert launch.profile is None
    assert "no provider configured" in launch.summary
    assert "sign-in" in launch.summary


def test_resolve_native_codex_launch_databricks_provider_sets_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Databricks provider default -> summary names the ucode profile (#2745)."""
    from omnigent.onboarding import ambient, detected, provider_config

    entry = SimpleNamespace(kind=provider_config.DATABRICKS_KIND, profile="my-profile")
    monkeypatch.setattr(provider_config, "load_config", dict)
    monkeypatch.setattr(ambient, "codex_config_detection", lambda: None)
    monkeypatch.setattr(detected, "dismissed_detection_names", lambda cfg: frozenset())
    monkeypatch.setattr(
        provider_config, "default_provider_for_harness", lambda cfg, harness: entry
    )

    launch = codex_native_app_server.resolve_native_codex_launch(model="gpt-5.5")

    assert launch.profile == "my-profile"
    assert launch.summary == "Databricks ucode profile 'my-profile'"


def test_resolve_native_codex_launch_connect_broker_managed_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No configured provider, but a managed connect host (host-only [omnigent]
    profile + broker sidecar) routes Codex through the gateway with broker auth."""
    from omnigent.inner import databricks_executor
    from omnigent.onboarding import ambient, detected, provider_config
    from omnigent.runtime import workflow

    # Everything unconfigured, so resolution reaches the last-resort branch.
    monkeypatch.setattr(provider_config, "load_config", dict)
    monkeypatch.setattr(ambient, "codex_config_detection", lambda: None)
    monkeypatch.setattr(detected, "dismissed_detection_names", lambda cfg: frozenset())
    monkeypatch.setattr(detected, "effective_config_with_detected", lambda cfg: {})
    monkeypatch.setattr(provider_config, "default_provider_for_harness", lambda cfg, harness: None)
    monkeypatch.setattr(workflow, "_load_global_auth", lambda: None)
    # Managed connect signal: [omnigent] profile host + broker sidecar present.
    monkeypatch.setattr(
        databricks_executor, "_read_databrickscfg_host", lambda profile: "https://ws.example"
    )
    monkeypatch.setattr(
        "omnigent.host.databricks_credential.broker_token_command",
        lambda host, *a, **k: "python3 -m omnigent.host.databricks_credential token --coords /x",
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_resolve_databricks_codex_model",
        lambda host, profile, model: "system.ai.gpt-6-astra",
    )

    launch = codex_native_app_server.resolve_native_codex_launch(model=None)

    assert launch.profile is None
    assert launch.model == "system.ai.gpt-6-astra"  # ucode-served model
    assert launch.config_overrides  # gateway provider table (base_url + broker auth)
    assert "managed connect host" in launch.summary


def test_resolve_native_codex_launch_no_broker_sidecar_falls_back_to_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No broker sidecar (e.g. a laptop) → connect-broker branch is skipped and
    Codex falls back to CLI login, so non-sandbox auth is untouched."""
    from omnigent.inner import databricks_executor
    from omnigent.onboarding import ambient, detected, provider_config
    from omnigent.runtime import workflow

    monkeypatch.setattr(provider_config, "load_config", dict)
    monkeypatch.setattr(ambient, "codex_config_detection", lambda: None)
    monkeypatch.setattr(detected, "dismissed_detection_names", lambda cfg: frozenset())
    monkeypatch.setattr(detected, "effective_config_with_detected", lambda cfg: {})
    monkeypatch.setattr(provider_config, "default_provider_for_harness", lambda cfg, harness: None)
    monkeypatch.setattr(workflow, "_load_global_auth", lambda: None)
    monkeypatch.setattr(
        databricks_executor, "_read_databrickscfg_host", lambda profile: "https://ws.example"
    )
    monkeypatch.setattr(
        "omnigent.host.databricks_credential.broker_token_command", lambda host, *a, **k: None
    )

    launch = codex_native_app_server.resolve_native_codex_launch(model=None)

    assert "no provider configured" in launch.summary  # CLI-login fallback, not the gateway
