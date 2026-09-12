"""Tests for the devin-native launcher, bridge and lifecycle hook."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.devin_native.bridge import (
    DEVIN_HOOK_EVENTS,
    build_devin_launch_args,
    build_devin_native_spawn_env,
    build_hook_config,
    devin_input_ready,
    hooks_size,
    iter_hook_events,
    record_hook_event,
    session_config_path,
    write_devin_session_config,
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
