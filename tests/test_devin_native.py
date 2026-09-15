"""Tests for the devin-native launcher, bridge and lifecycle hook."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.devin_native.bridge import (
    DEVIN_HOOK_EVENTS,
    _read_user_config,
    build_devin_launch_args,
    build_devin_mcp_server,
    build_devin_native_spawn_env,
    build_hook_config,
    canonical_devin_permission_mode,
    clear_fork_preamble,
    devin_context_usage,
    devin_input_ready,
    devin_permission_mode,
    devin_queue_pending,
    hooks_size,
    inject_permission_mode,
    inject_slash_command,
    iter_hook_events,
    read_fork_preamble,
    record_hook_event,
    session_config_path,
    wrap_fork_preamble,
    write_devin_agent_rule,
    write_devin_mcp_config,
    write_devin_session_config,
    write_fork_preamble,
)
from omnigent.harnesses.devin_native.hook import _normalize_tool_result
from omnigent.harnesses.devin_native.main import (
    DEVIN_EFFORTS,
    compose_devin_model,
    list_devin_cli_model_options,
    resolve_devin_launch_model,
)

# A real pane capture from devin 3000.10.21 (200x50), trimmed. The composer sits
# between two horizontal rules with the model/context status line below.
_IDLE_PANE = """\
Pro · 99% remaining (resets in 1d 2h)

────────────────────────────────────────
❭ Ask Devin to build features, fix bugs, or work on your code
────────────────────────────────────────
SWE-2 High                       Context: 26k / 262k tokens (9%)
"""
# Mid-turn: the placeholder changes but the composer still accepts input.
_BUSY_PANE = _IDLE_PANE.replace(
    "Ask Devin to build features, fix bugs, or work on your code",
    "Guide Devin while it works",
)
_BOOT_PANE = "Devin CLI\nv3000.10.21\n"
# A narrow pane (e.g. the web sidebar terminal) hard-wraps the idle placeholder
# onto two lines, so a naive single-line substring match never trips and the
# turn fails with "composer did not become ready".
_WRAPPED_IDLE_PANE = """\
Pro · 100% remaining (resets in 11h
21m)
────────────────────────────────
❭ Ask Devin to build features, fix
  bugs, or work on your code
────────────────────────────────
Claude Opus 5              Context: 25k / 1.0M
Low                        tokens (2%)
"""


class TestComposeModel:
    """Devin has no ``--effort``: effort is a model-variant suffix."""

    def test_composes_family_and_effort(self) -> None:
        assert (
            compose_devin_model("claude-opus-5", "xhigh", known_variants=["claude-opus-5-xhigh"])
            == "claude-opus-5-xhigh"
        )

    def test_no_effort_keeps_family(self) -> None:
        assert compose_devin_model("claude-opus-5", None) == "claude-opus-5"

    def test_no_model_is_none(self) -> None:
        assert compose_devin_model(None, "high") is None

    def test_already_composed_variant_is_left_alone(self) -> None:
        # A full variant id must not gain a second rung suffix.
        assert compose_devin_model("claude-opus-5-xhigh", "max") == "claude-opus-5-xhigh"

    def test_unknown_variant_falls_back_to_family(self) -> None:
        # gemini tops out at `high`, so `max` has no variant: fall back to the
        # family rather than passing Devin an id it would reject.
        assert (
            compose_devin_model(
                "gemini-3.8-flash", "max", known_variants=["gemini-3-8-flash-high"]
            )
            == "gemini-3.8-flash"
        )

    def test_unvalidated_composition_when_catalog_unknown(self) -> None:
        assert compose_devin_model("swe-2", "high", known_variants=None) == "swe-2-high"

    def test_every_declared_effort_composes(self) -> None:
        for effort in DEVIN_EFFORTS:
            variant = f"claude-opus-5-{effort}"
            assert (
                compose_devin_model("claude-opus-5", effort, known_variants=[variant]) == variant
            )


class TestResolveLaunchModel:
    """The shared CLI + runner resolver degrades safely."""

    def test_unreachable_catalog_keeps_the_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> list[str]:
            raise OSError("devin not on PATH")

        monkeypatch.setattr("omnigent.harnesses.devin_native.main.devin_model_variants", _boom)
        # Guessing `claude-opus-5-xhigh` blind could 400 the launch; the family
        # always resolves, so a probe failure costs effort, not the session.
        assert resolve_devin_launch_model("claude-opus-5", "xhigh") == "claude-opus-5"

    def test_no_effort_skips_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail() -> list[str]:  # pragma: no cover - must not be called
            raise AssertionError("catalog probed with no effort to compose")

        monkeypatch.setattr("omnigent.harnesses.devin_native.main.devin_model_variants", _fail)
        assert resolve_devin_launch_model("swe-2", None) == "swe-2"


class TestHookConfig:
    """Devin reads its hooks from the session-scoped config at launch."""

    def test_registers_every_event(self) -> None:
        config = build_hook_config("/tmp/hook.sh")
        assert set(config) == set(DEVIN_HOOK_EVENTS)

    def test_gate_events_get_a_long_timeout(self) -> None:
        config = build_hook_config("/tmp/hook.sh")
        # A gate hook blocks while a human decides, so it must outlive a short
        # timeout; an observational hook must not hold the turn open.
        for event in ("PreToolUse", "UserPromptSubmit", "PermissionRequest"):
            assert config[event][0]["hooks"][0]["timeout"] == 86_400, event
        for event in ("PostToolUse", "Stop", "SessionStart", "SessionEnd", "PostCompaction"):
            assert config[event][0]["hooks"][0]["timeout"] == 30, event

    def test_only_tool_events_carry_a_matcher(self) -> None:
        config = build_hook_config("/tmp/hook.sh")
        for event in ("PreToolUse", "PostToolUse", "PermissionRequest"):
            assert config[event][0]["matcher"] == "", event
        for event in ("UserPromptSubmit", "Stop", "SessionStart"):
            assert "matcher" not in config[event][0], event

    def test_every_event_runs_the_given_command(self) -> None:
        config = build_hook_config("/opt/omnigent/hook.sh")
        for event, entries in config.items():
            hook = entries[0]["hooks"][0]
            assert hook == {
                "type": "command",
                "command": "/opt/omnigent/hook.sh",
                "timeout": hook["timeout"],
            }, event


class TestSessionConfig:
    """``--config`` keeps the user's own settings; the repo is never touched."""

    def test_merges_user_config_and_adds_hooks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        (home / ".config" / "devin").mkdir(parents=True)
        (home / ".config" / "devin" / "config.json").write_text(
            json.dumps({"theme_mode": "dark", "devin": {"org_id": "org-1"}}),
            encoding="utf-8",
        )
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        path = write_devin_session_config(
            bridge,
            hook_command="/tmp/hook.sh",
            model="claude-opus-5-xhigh",
            source_env={"HOME": str(home)},
        )
        written = json.loads(path.read_text(encoding="utf-8"))
        # The user's settings survive...
        assert written["theme_mode"] == "dark"
        assert written["devin"] == {"org_id": "org-1"}
        # ...alongside Omnigent's hooks and the pinned model.
        assert set(written["hooks"]) == set(DEVIN_HOOK_EVENTS)
        assert written["agent"]["model"] == "claude-opus-5-xhigh"
        assert path == session_config_path(bridge)


class TestAgentRule:
    """A custom agent's instructions reach Devin as an always-on Windsurf rule."""

    def test_writes_always_on_frontmatter(self, tmp_path: Path) -> None:
        write_devin_agent_rule(tmp_path, "Always write TypeScript, never JavaScript.")
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        text = rule.read_text(encoding="utf-8")
        # The frontmatter is what makes Devin load the rule into every turn;
        # without `trigger: always_on` Devin treats the file as manual (unloaded).
        assert text.startswith("---\ntrigger: always_on\n---\n")
        assert "Always write TypeScript, never JavaScript." in text

    def test_none_removes_a_stale_rule(self, tmp_path: Path) -> None:
        write_devin_agent_rule(tmp_path, "old instructions")
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        assert rule.exists()
        # A later plain-Devin launch (no instructions) must not leave the previous
        # agent's rule behind in the workspace.
        write_devin_agent_rule(tmp_path, None)
        assert not rule.exists()

    def test_blank_instructions_write_nothing(self, tmp_path: Path) -> None:
        write_devin_agent_rule(tmp_path, "   ")
        assert not (tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md").exists()

    def test_absent_user_config_still_yields_hooks(self, tmp_path: Path) -> None:
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        path = write_devin_session_config(
            bridge, hook_command="/tmp/hook.sh", source_env={"HOME": str(tmp_path / "nope")}
        )
        assert set(json.loads(path.read_text())["hooks"]) == set(DEVIN_HOOK_EVENTS)

    def test_commented_user_config_does_not_break_launch(self, tmp_path: Path) -> None:
        # Devin accepts JSON with comments; json.loads does not. An unparseable
        # user config must not stop the session getting its hooks.
        home = tmp_path / "home"
        (home / ".config" / "devin").mkdir(parents=True)
        (home / ".config" / "devin" / "config.json").write_text(
            '{\n  // a comment devin allows\n  "theme_mode": "dark"\n}', encoding="utf-8"
        )
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        written = json.loads(
            write_devin_session_config(
                bridge, hook_command="/tmp/hook.sh", source_env={"HOME": str(home)}
            ).read_text()
        )
        assert set(written["hooks"]) == set(DEVIN_HOOK_EVENTS)

    def test_preserves_other_agent_keys(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        (home / ".config" / "devin").mkdir(parents=True)
        (home / ".config" / "devin" / "config.json").write_text(
            json.dumps({"agent": {"show_history_on_continue": True}}), encoding="utf-8"
        )
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        written = json.loads(
            write_devin_session_config(
                bridge,
                hook_command="/tmp/hook.sh",
                model="swe-2",
                source_env={"HOME": str(home)},
            ).read_text()
        )
        assert written["agent"] == {"show_history_on_continue": True, "model": "swe-2"}


class TestLaunchArgs:
    """Omnigent owns resume, workspace trust and the transcript export."""

    def _args(self, **kwargs: object) -> list[str]:
        return build_devin_launch_args(
            kwargs.pop("passthrough", []),  # type: ignore[arg-type]
            config_path=Path("/b/devin_config.json"),
            export_path_value=Path("/b/transcript.atif.json"),
            **kwargs,  # type: ignore[arg-type]
        )

    def test_always_passes_config_and_export(self) -> None:
        args = self._args()
        assert args[:4] == [
            "--config",
            "/b/devin_config.json",
            "--export",
            "/b/transcript.atif.json",
        ]

    def test_disables_workspace_trust_prompt(self) -> None:
        # An un-dismissable trust prompt would wedge the runner-owned pane.
        args = self._args()
        assert "--respect-workspace-trust" in args
        assert args[args.index("--respect-workspace-trust") + 1] == "false"

    def test_resume_and_model_and_mode(self) -> None:
        args = self._args(resume_id="fancy-spring", model="swe-2-high", permission_mode="smart")
        assert args[args.index("--resume") + 1] == "fancy-spring"
        assert args[args.index("--model") + 1] == "swe-2-high"
        assert args[args.index("--permission-mode") + 1] == "smart"

    def test_sandbox_flag_is_opt_in(self) -> None:
        assert "--sandbox" not in self._args()
        assert "--sandbox" in self._args(sandbox=True)

    def test_passthrough_args_come_last(self) -> None:
        args = self._args(passthrough=["--foo", "bar"], model="swe-2")
        assert args[-2:] == ["--foo", "bar"]


class TestHookEventLog:
    """The forwarder resumes from a byte offset, never re-posting an item."""

    def test_round_trip(self, tmp_path: Path) -> None:
        record_hook_event(tmp_path, {"hook_event_name": "Stop", "session_id": "s1"})
        events = list(iter_hook_events(tmp_path))
        assert [payload["hook_event_name"] for _o, payload in events] == ["Stop"]

    def test_offset_resumes_without_replay(self, tmp_path: Path) -> None:
        record_hook_event(tmp_path, {"hook_event_name": "SessionStart"})
        record_hook_event(tmp_path, {"hook_event_name": "UserPromptSubmit"})
        first = list(iter_hook_events(tmp_path))
        assert len(first) == 2
        offset = first[0][0]
        resumed = list(iter_hook_events(tmp_path, start_offset=offset))
        assert [p["hook_event_name"] for _o, p in resumed] == ["UserPromptSubmit"]
        # Reading from the end yields nothing — the cold-resume "skip history" path.
        assert list(iter_hook_events(tmp_path, start_offset=hooks_size(tmp_path))) == []

    def test_partial_trailing_line_is_left_for_the_next_poll(self, tmp_path: Path) -> None:
        record_hook_event(tmp_path, {"hook_event_name": "SessionStart"})
        # A hook mid-write leaves a newline-less tail; consuming it would both
        # drop the event and corrupt the offset.
        with (tmp_path / "hooks.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"payload": {"hook_event_name": "Stop"')
        events = list(iter_hook_events(tmp_path))
        assert [p["hook_event_name"] for _o, p in events] == ["SessionStart"]

    def test_missing_log_is_empty_not_an_error(self, tmp_path: Path) -> None:
        assert list(iter_hook_events(tmp_path / "absent")) == []
        assert hooks_size(tmp_path / "absent") == 0


class TestPaneReadiness:
    """Composer detection drives when a web turn may be injected."""

    def test_idle_pane_is_ready(self) -> None:
        assert devin_input_ready(_IDLE_PANE) is True

    def test_busy_pane_is_also_ready(self) -> None:
        # Devin accepts steering mid-turn, so a running turn is still injectable.
        assert devin_input_ready(_BUSY_PANE) is True

    def test_booting_pane_is_not_ready(self) -> None:
        assert devin_input_ready(_BOOT_PANE) is False

    def test_wrapped_placeholder_is_still_ready(self) -> None:
        # A narrow pane hard-wraps the placeholder across lines; readiness must
        # collapse whitespace before matching or the turn hangs to timeout.
        assert devin_input_ready(_WRAPPED_IDLE_PANE) is True


class TestSpawnEnv:
    """The executor finds its bridge dir through the spawn env."""

    def test_carries_bridge_dir_and_session(self) -> None:
        env = build_devin_native_spawn_env("conv_abc123")
        assert env["HARNESS_DEVIN_NATIVE_REQUEST_SESSION_ID"] == "conv_abc123"
        assert Path(env["HARNESS_DEVIN_NATIVE_BRIDGE_DIR"]).is_dir()


class TestNormalizeToolResult:
    """Devin sends ``tool_response``; the shared policy seam reads ``tool_output``."""

    def test_output_is_promoted(self) -> None:
        payload = _normalize_tool_result(
            {"tool_response": {"success": True, "output": "hi", "error": None}}
        )
        assert payload["tool_output"] == "hi"

    def test_error_is_used_when_there_is_no_output(self) -> None:
        payload = _normalize_tool_result(
            {"tool_response": {"success": False, "output": "", "error": "boom"}}
        )
        assert payload["tool_output"] == "boom"

    def test_existing_tool_output_wins(self) -> None:
        payload = _normalize_tool_result({"tool_output": "kept", "tool_response": {"output": "x"}})
        assert payload["tool_output"] == "kept"

    def test_absent_response_is_untouched(self) -> None:
        assert _normalize_tool_result({"tool_name": "exec"}) == {"tool_name": "exec"}


class TestModelOptions:
    """The picker lists families; effort rungs come off the variant ids."""

    def test_parses_families_and_effort_rungs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "default_model": "swe-2",
            "families": [
                {
                    "slug": "claude-opus-5",
                    "family_label": "Claude Opus 5",
                    "aliases": ["opus"],
                    "variants": [
                        {
                            "model_uid": "claude-opus-5-low",
                            "max_context_tokens": 1_000_000,
                            "cost_summary": "$5 / 1M Input",
                        },
                        {"model_uid": "claude-opus-5-xhigh"},
                        {"model_uid": "claude-opus-5-xhigh-fast"},
                    ],
                },
                {
                    "slug": "swe-2",
                    "family_label": "SWE-2",
                    "variants": [{"model_uid": "swe-2-high"}],
                },
            ],
        }
        monkeypatch.setattr(
            "omnigent.harnesses.devin_native.main._run_devin_models_list", lambda **_kw: payload
        )
        options = list_devin_cli_model_options()
        by_id = {option["id"]: option for option in options}
        assert set(by_id) == {"claude-opus-5", "swe-2"}
        opus = by_id["claude-opus-5"]
        assert opus["displayName"] == "Claude Opus 5"
        assert opus["aliases"] == ["opus"]
        assert opus["contextWindow"] == 1_000_000
        assert opus["description"] == "$5 / 1M Input"
        # Only rungs that really exist as a variant are offered; the `-fast`
        # serving modifier is not an effort.
        assert opus["efforts"] == ["low", "xhigh"]
        assert by_id["swe-2"]["isDefault"] is True
        assert by_id["claude-opus-5"]["isDefault"] is False

    def test_rejects_a_payload_with_no_families(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "omnigent.harnesses.devin_native.main._run_devin_models_list",
            lambda **_kw: {"families": []},
        )
        with pytest.raises(ValueError, match="did not contain any valid families"):
            list_devin_cli_model_options()


# Devin's own queue strip, verbatim from a pane where a message was submitted
# while a turn was running (devin 3000.10.21).
_QUEUED_PANE = """\
Pro · 100% remaining (resets in 11h 21m)
────────────────────────────────────────
❭ Ask Devin to build features, fix bugs, or work on your code
────────────────────────────────────────
── 1 queued ─────────────────────────── ↑ edit · ↵ send now ──
○ Tell me the best one
"""


class TestQueuedPane:
    """A steered message must not sit in Devin's own queue."""

    def test_queue_strip_is_detected(self) -> None:
        assert devin_queue_pending(_QUEUED_PANE) is True

    def test_idle_pane_has_nothing_queued(self) -> None:
        assert devin_queue_pending(_IDLE_PANE) is False

    def test_busy_pane_alone_is_not_a_queue(self) -> None:
        # Mid-turn without the queue strip: the composer is writable, nothing parked.
        assert devin_queue_pending(_BUSY_PANE) is False

    def test_wrapped_queue_strip_is_still_detected(self) -> None:
        # Narrow panes wrap the strip, so matching cannot depend on one line.
        assert devin_queue_pending("── 2 queued ──\n↑ edit ·\n↵ send now ──\n○ hi\n") is True

    def test_the_word_queued_alone_is_not_a_queue(self) -> None:
        # A turn that merely talks about queues must not trip the flush.
        assert devin_queue_pending("I queued the job for you.\n") is False


class TestMcpConfig:
    """Omnigent's MCP relay is registered where Devin actually reads servers."""

    def test_writes_the_project_local_mcp_file(self, tmp_path: Path) -> None:
        # Devin's `--config` user config carries no MCP servers; `devin mcp add`
        # writes this project-local file, so the relay has to land there.
        path = write_devin_mcp_config(tmp_path / "ws", tmp_path / "bridge")
        assert path == tmp_path / "ws" / ".devin" / "mcp_config.local.json"

    def test_relay_entry_is_stdio_serve_mcp_for_this_bridge(self, tmp_path: Path) -> None:
        bridge = tmp_path / "bridge"
        path = write_devin_mcp_config(tmp_path / "ws", bridge, python_executable="/py")
        entry = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]["omnigent"]
        assert entry["command"] == "/py"
        assert entry["transport"] == "stdio"
        assert entry["args"][:2] == ["-I", "-m"]
        assert "serve-mcp" in entry["args"]
        assert str(bridge) in entry["args"]

    def test_user_servers_survive(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        (workspace / ".devin").mkdir(parents=True)
        (workspace / ".devin" / "mcp_config.local.json").write_text(
            json.dumps({"mcpServers": {"glean": {"command": "/bin/glean"}}}), encoding="utf-8"
        )
        path = write_devin_mcp_config(workspace, tmp_path / "bridge")
        servers = json.loads(path.read_text(encoding="utf-8"))["mcpServers"]
        assert sorted(servers) == ["glean", "omnigent"]

    def test_a_malformed_file_does_not_break_the_launch(self, tmp_path: Path) -> None:
        workspace = tmp_path / "ws"
        (workspace / ".devin").mkdir(parents=True)
        (workspace / ".devin" / "mcp_config.local.json").write_text("not json", encoding="utf-8")
        path = write_devin_mcp_config(workspace, tmp_path / "bridge")
        assert "omnigent" in json.loads(path.read_text(encoding="utf-8"))["mcpServers"]

    def test_seeds_a_stable_relay_token(self, tmp_path: Path) -> None:
        bridge = tmp_path / "bridge"
        write_devin_mcp_config(tmp_path / "ws", bridge)
        token = json.loads((bridge / "bridge.json").read_text(encoding="utf-8"))["token"]
        assert token
        # A relaunch must not rotate a token the relay already booted with.
        write_devin_mcp_config(tmp_path / "ws", bridge)
        assert json.loads((bridge / "bridge.json").read_text(encoding="utf-8"))["token"] == token

    def test_no_workspace_key_so_no_os_tools_are_served(self, tmp_path: Path) -> None:
        # Devin owns its own filesystem tools; a token-only bridge.json keeps
        # serve-mcp to the relay (same choice as opencode/cursor).
        bridge = tmp_path / "bridge"
        write_devin_mcp_config(tmp_path / "ws", bridge)
        assert set(json.loads((bridge / "bridge.json").read_text(encoding="utf-8"))) == {"token"}

    def test_entry_shape_matches_devins_own_writer(self, tmp_path: Path) -> None:
        # `devin mcp add` (3000.10.21) emits exactly these keys.
        entry = build_devin_mcp_server(tmp_path / "bridge", python_executable="/py")
        assert set(entry) == {"command", "args", "transport", "env"}


# Devin marks a non-default permission mode on the composer's top rule; the
# default shows none. Verbatim from cycling Shift+Tab on devin 3000.10.21.
def _mode_pane(marker: str) -> str:
    rule = "─" * 40
    return (
        f"{rule} {marker} ─\n"
        "❭ Ask Devin to build features, fix bugs, or work on your code\n"
        f"{rule}\nGLM-5.2 High\n"
    )


class TestPermissionMode:
    """Mid-session switching cycles Shift+Tab, so the pane is the source of truth."""

    def test_reads_each_marker(self) -> None:
        assert devin_permission_mode(_mode_pane("(bypass permissions on)")) == "dangerous"
        assert devin_permission_mode(_mode_pane("(accept edits on)")) == "accept-edits"
        assert devin_permission_mode(_mode_pane("(smart mode on)")) == "smart"

    def test_no_marker_is_the_default_mode(self) -> None:
        # Devin marks only the non-default modes, so a bare rule means `normal`.
        assert devin_permission_mode(_IDLE_PANE) == "normal"

    def test_marker_survives_a_narrow_pane_wrap(self) -> None:
        assert devin_permission_mode("──\n(accept edits\non) ─\n❭ Ask Devin\n") == "accept-edits"

    def test_aliases_normalize_to_devins_canonical_names(self) -> None:
        assert canonical_devin_permission_mode("auto") == "normal"
        assert canonical_devin_permission_mode("bypass") == "dangerous"
        assert canonical_devin_permission_mode("yolo") == "dangerous"
        assert canonical_devin_permission_mode(" Accept-Edits ") == "accept-edits"

    def test_an_unknown_mode_is_refused_before_touching_the_pane(self, tmp_path: Path) -> None:
        # Guarding first matters: cycling blind could overshoot onto `dangerous`,
        # which auto-approves every tool.
        with pytest.raises(RuntimeError, match="does not expose permission mode"):
            inject_permission_mode(tmp_path, mode="not-a-mode")


class TestContextUsage:
    """The web context ring is fed from Devin's own footer."""

    def test_reads_the_abbreviated_pair(self) -> None:
        assert devin_context_usage("SWE-2 High   Context: 26k / 262k tokens (9%)") == (
            26_000,
            262_000,
        )

    def test_reads_a_wrapped_footer(self) -> None:
        # A narrow pane splits the footer across lines, so matching cannot be
        # line-based (this is the shape the sidebar terminal produces).
        pane = "Claude Opus 5 Context: 25k / 1.0M\nLow           tokens (2%)\n"
        assert devin_context_usage(pane) == (25_000, 1_000_000)

    def test_reads_unabbreviated_counts(self) -> None:
        assert devin_context_usage("Context: 1234 / 200000 tokens (1%)") == (1234, 200_000)

    def test_the_real_idle_pane_parses(self) -> None:
        # The captured fixture carries the footer, so the ring fills from an
        # ordinary idle pane without waiting for anything.
        assert devin_context_usage(_IDLE_PANE) == (26_000, 262_000)

    def test_no_footer_yields_no_pair(self) -> None:
        # Better a hidden ring than one drawn from a half-parsed footer.
        assert devin_context_usage("GLM-5.2 High") == (None, None)
        assert devin_context_usage(_BOOT_PANE) == (None, None)

    def test_a_zero_window_is_refused(self) -> None:
        assert devin_context_usage("Context: 10 / 0 tokens") == (None, None)


class TestForkPreamble:
    """A forked clone replays its prior conversation on the first message."""

    def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        write_fork_preamble(tmp_path, "You: hi\n\nAssistant: hello")
        assert read_fork_preamble(tmp_path) == "You: hi\n\nAssistant: hello"
        clear_fork_preamble(tmp_path)
        assert read_fork_preamble(tmp_path) is None

    def test_blank_preamble_writes_nothing(self, tmp_path: Path) -> None:
        write_fork_preamble(tmp_path, "   \n")
        assert read_fork_preamble(tmp_path) is None

    def test_clear_is_idempotent(self, tmp_path: Path) -> None:
        clear_fork_preamble(tmp_path)  # nothing staged; must not raise

    def test_wrap_frames_the_history_before_the_user_text(self) -> None:
        wrapped = wrap_fork_preamble("You: hi", "now do the thing")
        assert wrapped.startswith("<omnigent_fork_history>")
        assert "You: hi" in wrapped
        # The user's own message sits after the close tag, so the forwarder's
        # non-greedy strip leaves it intact.
        assert wrapped.endswith("now do the thing")
        assert wrapped.index("</omnigent_fork_history>") < wrapped.index("now do the thing")

    def test_embedded_sentinels_are_defanged(self) -> None:
        # Exactly one real open/close pair, so the strip can never be ambiguous.
        wrapped = wrap_fork_preamble("You: <omnigent_fork_history> sneaky", "go")
        assert wrapped.count("<omnigent_fork_history>") == 1
        assert "[omnigent_fork_history]" in wrapped


class TestSlashCommandSafety:
    """`send-keys -l` types literally, so a control byte would be a keystroke."""

    def test_rejects_a_control_byte(self, tmp_path: Path) -> None:
        # A CR would submit whatever follows as a second command.
        for payload in ("/model swe-2\rrm -rf x", "/model a\x1b[A", "/model x\ny"):
            with pytest.raises(RuntimeError, match="control bytes"):
                inject_slash_command(tmp_path, command=payload, timeout_s=0.01)

    def test_still_requires_a_leading_slash(self, tmp_path: Path) -> None:
        with pytest.raises(RuntimeError, match="must start with"):
            inject_slash_command(tmp_path, command="model swe-2", timeout_s=0.01)


class TestReservedLaunchArgs:
    """Passthrough args must not override the flags Omnigent owns."""

    def _args(self, passthrough: list[str]) -> list[str]:
        return build_devin_launch_args(
            passthrough, config_path=Path("/c.json"), export_path_value=Path("/e.json")
        )

    def test_rejects_a_session_hijack(self) -> None:
        # Omnigent only passes --resume when it means to; an injected one would
        # attach a different Devin session to this conversation.
        for flag in ("--resume", "-r", "--continue", "-c"):
            with pytest.raises(RuntimeError, match="Omnigent-owned"):
                self._args([flag, "someone-elses-session"])

    def test_rejects_overriding_the_policy_gate_and_transcript(self) -> None:
        # --config carries the Omnigent hooks block: replacing it would disable
        # the PreToolUse policy gate. --export is where the forwarder reads.
        for flag in ("--config", "--export", "--respect-workspace-trust"):
            with pytest.raises(RuntimeError, match="Omnigent-owned"):
                self._args([flag, "/tmp/other"])

    def test_rejects_the_equals_form(self) -> None:
        with pytest.raises(RuntimeError, match="Omnigent-owned"):
            self._args(["--config=/tmp/evil.json"])

    def test_allows_the_permission_mode_omnigent_itself_passes(self) -> None:
        # The web create flow delivers the user's picked mode through these very
        # args, so reserving it would break permission modes entirely.
        assert "dangerous" in self._args(["--permission-mode", "dangerous"])

    def test_allows_an_ordinary_arg(self) -> None:
        assert self._args(["--verbose"])[-1] == "--verbose"


class TestUserConfigJsonc:
    """Devin accepts JSONC, so a commented config must not be discarded."""

    def _write(self, tmp_path: Path, body: str) -> Path:
        cfg = tmp_path / "config.json"
        cfg.write_text(body, encoding="utf-8")
        return cfg

    def test_line_and_block_comments_survive(self, tmp_path: Path) -> None:
        cfg = self._write(
            tmp_path,
            '{\n  // note\n  "theme_mode": "dark",\n  /* block */\n'
            '  "devin": {"org_id": "org-42"}\n}',
        )
        parsed = _read_user_config(cfg)
        # Dropping these would silently lose the user's org and permissions.
        assert parsed["theme_mode"] == "dark"
        assert parsed["devin"] == {"org_id": "org-42"}

    def test_comment_markers_inside_a_string_are_kept(self, tmp_path: Path) -> None:
        cfg = self._write(tmp_path, '{"note": "keep // this and /* this */ inside"}')
        assert _read_user_config(cfg)["note"] == "keep // this and /* this */ inside"

    def test_truly_malformed_config_still_degrades_to_empty(self, tmp_path: Path) -> None:
        assert _read_user_config(self._write(tmp_path, "{not json at all")) == {}
