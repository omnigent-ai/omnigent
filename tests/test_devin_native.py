"""Tests for the devin-native launcher, bridge and lifecycle hook."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click
import pytest

from omnigent.harnesses.devin_native import bridge as bridge_module
from omnigent.harnesses.devin_native.bridge import (
    DEVIN_HOOK_EVENTS,
    _read_user_config,
    bridge_dir_for_session_id,
    build_devin_launch_args,
    build_devin_mcp_server,
    build_devin_native_spawn_env,
    build_hook_config,
    canonical_devin_permission_mode,
    clear_agent_instructions_preamble,
    clear_fork_preamble,
    devin_context_usage,
    devin_input_ready,
    devin_permission_mode,
    devin_queue_pending,
    hooks_size,
    inject_permission_mode,
    inject_slash_command,
    iter_hook_events,
    prepare_bridge_dir,
    read_agent_instructions_preamble,
    read_devin_workspace_hint,
    read_fork_preamble,
    record_hook_event,
    remove_devin_agent_rule_if_owned,
    session_config_path,
    wrap_agent_instructions,
    wrap_fork_preamble,
    write_agent_instructions_preamble,
    write_devin_agent_rule,
    write_devin_mcp_config,
    write_devin_session_config,
    write_devin_workspace_hint,
    write_fork_preamble,
)
from omnigent.harnesses.devin_native.hook import _normalize_tool_result
from omnigent.harnesses.devin_native.main import (
    DEVIN_EFFORTS,
    build_fusion_descriptor,
    compose_devin_model,
    family_effort_variants,
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
            compose_devin_model("claude-opus-5", "xhigh", families=[_FAMILY_OPUS])
            == "claude-opus-5-xhigh"
        )

    def test_a_dotted_slug_reaches_its_real_variant(self) -> None:
        """A slug is not the prefix of its variant ids.

        ``gpt-5.6-sol`` spells its rungs ``gpt-5-6-sol-high``, so composing from
        the slug produced an id the catalog never had and effort was dropped for
        every dotted family — half the catalog.
        """
        assert compose_devin_model("gpt-5.6-sol", "high", families=[_FAMILY_SOL]) == (
            "gpt-5-6-sol-high"
        )

    def test_a_reordered_variant_name_still_resolves(self) -> None:
        # `claude-fable-5` spells its variants `claude-5-fable-*`, which no slug
        # rewriting would find; the rung is read off the ids instead.
        assert compose_devin_model("claude-fable-5", "low", families=[_FAMILY_FABLE]) == (
            "claude-5-fable-low"
        )

    def test_an_alias_resolves_to_its_family(self) -> None:
        assert compose_devin_model("opus", "xhigh", families=[_FAMILY_OPUS]) == (
            "claude-opus-5-xhigh"
        )

    def test_a_fast_variant_never_stands_in_for_a_rung(self) -> None:
        # `claude-opus-5-high-fast` also carries `high`, but the plain variant is
        # what the rung means.
        assert compose_devin_model("claude-opus-5", "high", families=[_FAMILY_OPUS]) == (
            "claude-opus-5-high"
        )

    def test_an_unknown_model_falls_back_to_itself(self) -> None:
        assert compose_devin_model("not-a-model", "high", families=[_FAMILY_OPUS]) == "not-a-model"

    def test_no_effort_keeps_family(self) -> None:
        assert compose_devin_model("claude-opus-5", None) == "claude-opus-5"

    def test_no_model_is_none(self) -> None:
        assert compose_devin_model(None, "high") is None

    def test_a_composed_variant_recomposes_to_the_requested_rung(self) -> None:
        # A stored override can already carry a rung (e.g. after a launch persists
        # the composed id). A later effort switch must resolve the NEW rung from
        # the family, not keep the old suffix — that no-op was the mid-session
        # effort bug. It also never double-suffixes.
        assert (
            compose_devin_model("claude-opus-5-xhigh", "max", families=[_FAMILY_OPUS])
            == "claude-opus-5-max"
        )
        assert (
            compose_devin_model("claude-opus-5-xhigh", "low", families=[_FAMILY_OPUS])
            == "claude-opus-5-low"
        )

    def test_offline_recompose_does_not_double_suffix(self) -> None:
        # With no catalog, the existing rung is stripped before the new one is
        # applied, so we never emit `…-xhigh-low`.
        assert compose_devin_model("claude-opus-5-xhigh", "low", families=None) == (
            "claude-opus-5-low"
        )

    def test_a_rung_the_family_lacks_falls_back_to_it(self) -> None:
        # swe-2 has only medium/high/max, and no `swe-2-low` exists anywhere in
        # the catalog: fall back to the family rather than invent an id, which
        # Devin would resolve to some other model entirely.
        assert compose_devin_model("swe-2", "low", families=[_FAMILY_SWE2]) == "swe-2"

    def test_unvalidated_composition_when_catalog_unknown(self) -> None:
        assert compose_devin_model("swe-2", "high", families=None) == "swe-2-high"

    def test_every_declared_effort_resolves(self) -> None:
        for effort in DEVIN_EFFORTS:
            assert compose_devin_model("claude-opus-5", effort, families=[_FAMILY_OPUS]) == (
                f"claude-opus-5-{effort}"
            )

    def test_a_fusion_variant_is_never_recomposed(self) -> None:
        # A fusion id bakes the lead effort in and appends the sidekick; the flat
        # rungs do not apply, so any effort passed alongside it is ignored.
        uid = "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium"
        for effort in (None, "low", "max"):
            assert compose_devin_model(uid, effort, families=_FUSION_CATALOG) == uid

    def test_the_bare_fusion_slug_defers_to_devin_default(self) -> None:
        # "fusion" alone is not a launchable variant; hand Devin the family so it
        # resolves its own default rather than a rung-derived guess.
        assert compose_devin_model("fusion", "high", families=_FUSION_CATALOG) == "fusion"


class TestFamilyEffortVariants:
    """The rung ladder the web picker offers for one model."""

    def test_lists_only_the_rungs_the_family_has(self) -> None:
        assert family_effort_variants(_FAMILY_SWE2) == {
            "medium": "swe-2-medium",
            "high": "swe-2-high",
            "max": "swe-2-max",
        }

    def test_ignores_suffixed_variants(self) -> None:
        # `-fast` / `-priority` / `-1m` rows are not rungs of their own.
        assert family_effort_variants(_FAMILY_OPUS)["max"] == "claude-opus-5-max"

    def test_a_family_without_rungs_offers_none(self) -> None:
        assert (
            family_effort_variants({"slug": "adaptive", "variants": [{"model_uid": "adaptive"}]})
            == {}
        )


#: Family rows in the shape `devin models list --format json` returns, trimmed to
#: the keys the resolver reads. The spellings are the point: dots in slugs, dashes
#: in variant ids, and `claude-fable-5` reordering its own name.
_FAMILY_OPUS: dict[str, object] = {
    "slug": "claude-opus-5",
    "aliases": ["opus"],
    "variants": [
        {"model_uid": f"claude-opus-5-{rung}"}
        for rung in ("medium", "low", "high", "xhigh", "max")
    ]
    + [
        {"model_uid": f"claude-opus-5-{rung}-fast"}
        for rung in ("low", "medium", "high", "xhigh", "max")
    ],
}
_FAMILY_SOL: dict[str, object] = {
    "slug": "gpt-5.6-sol",
    "aliases": [],
    "variants": [{"model_uid": f"gpt-5-6-sol-{rung}"} for rung in ("medium", "low", "high")]
    + [{"model_uid": "gpt-5-6-sol-high-priority"}],
}
_FAMILY_FABLE: dict[str, object] = {
    "slug": "claude-fable-5",
    "aliases": [],
    "variants": [{"model_uid": f"claude-5-fable-{rung}"} for rung in ("low", "medium", "high")],
}
_FAMILY_SWE2: dict[str, object] = {
    "slug": "swe-2",
    "aliases": ["swe"],
    "variants": [{"model_uid": f"swe-2-{rung}"} for rung in ("high", "medium", "max")],
}

#: A trimmed Fusion catalog: the fusion family plus the lead/sidekick families its
#: variant halves resolve against for labels. Exercises a fast lead, a bare-family
#: sidekick (glm-5-2), and a priority sidekick.
_FUSION_FAMILY: dict[str, object] = {
    "slug": "fusion",
    "family_label": "Fusion",
    "variants": [
        {"model_uid": "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium"},
        {"model_uid": "fusion-claude-fable-5-1-high-sidekick-swe-2-high"},
        {"model_uid": "fusion-claude-opus-5-high-fast-sidekick-glm-5-2"},
        {"model_uid": "fusion-claude-opus-5-xhigh-sidekick-gpt-5-6-sol-high-priority"},
    ],
}
_FUSION_CATALOG: list[dict[str, object]] = [
    _FUSION_FAMILY,
    {
        "slug": "claude-fable-5.1",
        "family_label": "Claude Fable 5.1",
        "variants": [{"model_uid": f"claude-fable-5-1-{r}"} for r in ("medium", "high")],
    },
    {
        "slug": "claude-opus-5",
        "family_label": "Claude Opus 5",
        "variants": [
            {"model_uid": "claude-opus-5-high-fast"},
            {"model_uid": "claude-opus-5-xhigh"},
        ],
    },
    {
        "slug": "swe-2",
        "family_label": "SWE-2",
        "variants": [{"model_uid": f"swe-2-{r}"} for r in ("medium", "high")],
    },
    {"slug": "glm-5.2", "family_label": "GLM-5.2", "variants": [{"model_uid": "glm-5-2"}]},
    {
        "slug": "gpt-5.6-sol",
        "family_label": "GPT-5.6 Sol",
        "variants": [
            {"model_uid": "gpt-5-6-sol-high"},
            {"model_uid": "gpt-5-6-sol-high-priority"},
        ],
    },
]


class TestFusionDescriptor:
    """Fusion decomposes ``fusion-<lead>-sidekick-<sidekick>`` into picker facets."""

    def _descriptor(self) -> dict[str, object]:
        desc = build_fusion_descriptor(_FUSION_FAMILY, _FUSION_CATALOG)
        assert desc is not None
        return desc

    def test_default_is_the_first_catalog_combo(self) -> None:
        # Catalog order is preserved, so the first variant is the sensible default.
        assert self._descriptor()["default"] == (
            "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium"
        )

    def test_parses_lead_family_effort_and_sidekick(self) -> None:
        combos = {c["modelUid"]: c for c in self._descriptor()["combos"]}
        c = combos["fusion-claude-fable-5-1-medium-sidekick-swe-2-medium"]
        assert c["lead"] == "claude-fable-5.1"
        assert c["leadLabel"] == "Claude Fable 5.1"
        assert c["effort"] == "medium"
        assert c["fast"] is False
        assert c["sidekick"] == "swe-2-medium"
        assert c["sidekickLabel"] == "SWE-2 Medium"
        assert c["priority"] is False

    def test_parses_a_fast_lead_and_bare_family_sidekick(self) -> None:
        combos = {c["modelUid"]: c for c in self._descriptor()["combos"]}
        c = combos["fusion-claude-opus-5-high-fast-sidekick-glm-5-2"]
        assert c["lead"] == "claude-opus-5"
        assert c["effort"] == "high"
        assert c["fast"] is True
        # A sidekick with no effort rung labels as the bare family.
        assert c["sidekick"] == "glm-5-2"
        assert c["sidekickLabel"] == "GLM-5.2"
        assert c["priority"] is False

    def test_parses_a_priority_sidekick(self) -> None:
        combos = {c["modelUid"]: c for c in self._descriptor()["combos"]}
        c = combos["fusion-claude-opus-5-xhigh-sidekick-gpt-5-6-sol-high-priority"]
        assert c["effort"] == "xhigh"
        # Priority is stripped from the sidekick key and surfaced as a flag.
        assert c["sidekick"] == "gpt-5-6-sol-high"
        assert c["sidekickLabel"] == "GPT-5.6 Sol High"
        assert c["priority"] is True

    def test_every_combo_maps_to_a_real_variant(self) -> None:
        real = {v["model_uid"] for v in _FUSION_FAMILY["variants"]}
        assert {c["modelUid"] for c in self._descriptor()["combos"]} == real

    def test_a_family_with_no_fusion_variants_yields_none(self) -> None:
        assert build_fusion_descriptor(_FAMILY_SWE2, _FUSION_CATALOG) is None


class TestResolveLaunchModel:
    """The shared CLI + runner resolver degrades safely."""

    def test_unreachable_catalog_keeps_the_family(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> list[object]:
            raise OSError("devin not on PATH")

        monkeypatch.setattr("omnigent.harnesses.devin_native.main.devin_model_families", _boom)
        # Guessing `claude-opus-5-xhigh` blind could 400 the launch; the family
        # always resolves, so a probe failure costs effort, not the session.
        assert resolve_devin_launch_model("claude-opus-5", "xhigh") == "claude-opus-5"

    def test_no_effort_skips_the_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _fail() -> list[object]:  # pragma: no cover - must not be called
            raise AssertionError("catalog probed with no effort to compose")

        monkeypatch.setattr("omnigent.harnesses.devin_native.main.devin_model_families", _fail)
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


@pytest.fixture
def _isolated_bridge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the bridge root at *tmp_path* so nothing lands in the real $TMPDIR."""
    root = tmp_path / "bridge-root"
    root.mkdir()
    monkeypatch.setattr(bridge_module, "_BRIDGE_ROOT", root)
    return root


@pytest.mark.usefixtures("_isolated_bridge_root")
class TestAgentRule:
    """A custom agent's instructions reach Devin as an always-on Windsurf rule."""

    def test_writes_always_on_frontmatter(self, tmp_path: Path) -> None:
        assert write_devin_agent_rule(
            tmp_path, "Always write TypeScript, never JavaScript.", session_id="sess-a"
        )
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        text = rule.read_text(encoding="utf-8")
        # The frontmatter is what makes Devin load the rule into every turn;
        # without `trigger: always_on` Devin treats the file as manual (unloaded).
        assert text.startswith("---\ntrigger: always_on\n")
        assert "omnigent_session: sess-a" in text
        assert "Always write TypeScript, never JavaScript." in text

    def test_none_removes_a_stale_rule(self, tmp_path: Path) -> None:
        write_devin_agent_rule(tmp_path, "old instructions", session_id="sess-a")
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        assert rule.exists()
        # A later plain-Devin launch (no instructions) must not leave the previous
        # agent's rule behind in the workspace.
        assert not write_devin_agent_rule(tmp_path, None, session_id="sess-a")
        assert not rule.exists()

    def test_blank_instructions_write_nothing(self, tmp_path: Path) -> None:
        assert not write_devin_agent_rule(tmp_path, "   ", session_id="sess-a")
        assert not (tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md").exists()

    def test_a_live_other_session_keeps_its_rule(self, tmp_path: Path) -> None:
        """Devin loads every always-on rule in the dir, so the file is shared.

        Two agents in one workspace cannot both own it; the launch that arrives
        second must leave the running session's brief alone and take the preamble.
        """
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        write_devin_agent_rule(tmp_path, "agent A instructions", session_id="live-sess")
        prepare_bridge_dir("live-sess")  # what marks that session as still running

        assert not write_devin_agent_rule(tmp_path, "agent B instructions", session_id="sess-b")
        assert "agent A instructions" in rule.read_text(encoding="utf-8")

    def test_a_finished_session_rule_is_taken_over(self, tmp_path: Path) -> None:
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        write_devin_agent_rule(tmp_path, "agent A instructions", session_id="gone-sess")
        shutil.rmtree(bridge_dir_for_session_id("gone-sess"), ignore_errors=True)

        assert write_devin_agent_rule(tmp_path, "agent B instructions", session_id="sess-b")
        assert "agent B instructions" in rule.read_text(encoding="utf-8")

    def test_a_plain_launch_keeps_another_sessions_rule(self, tmp_path: Path) -> None:
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        write_devin_agent_rule(tmp_path, "agent A instructions", session_id="live-sess")
        prepare_bridge_dir("live-sess")

        write_devin_agent_rule(tmp_path, None, session_id="sess-b")
        assert rule.exists(), "a plain launch must not delete a live agent's rule"

    def test_a_home_workspace_refuses_the_rule(self, tmp_path: Path, monkeypatch) -> None:
        """A rule under $HOME loads for every Devin run on the machine."""
        monkeypatch.setenv("HOME", str(tmp_path))
        assert not write_devin_agent_rule(tmp_path, "leaky instructions", session_id="sess-a")
        assert not (tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md").exists()

    def test_identical_instructions_reuse_the_rule(self, tmp_path: Path) -> None:
        # Same agent, second session: the rule already says the right thing, so it
        # is honoured rather than fought over.
        write_devin_agent_rule(tmp_path, "shared instructions", session_id="live-sess")
        prepare_bridge_dir("live-sess")
        assert write_devin_agent_rule(tmp_path, "shared instructions", session_id="live-sess")

    def test_teardown_removes_this_sessions_rule(self, tmp_path: Path) -> None:
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        write_devin_agent_rule(tmp_path, "agent instructions", session_id="sess-a")
        assert rule.exists()
        # At session end the rule must go, or it loads into a later Devin run.
        assert remove_devin_agent_rule_if_owned(tmp_path, "sess-a") is True
        assert not rule.exists()

    def test_teardown_leaves_another_sessions_rule(self, tmp_path: Path) -> None:
        rule = tmp_path / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        write_devin_agent_rule(tmp_path, "agent A instructions", session_id="sess-a")
        # A different session ending must not delete the rule sess-a owns.
        assert remove_devin_agent_rule_if_owned(tmp_path, "sess-b") is False
        assert "agent A instructions" in rule.read_text(encoding="utf-8")

    def test_teardown_is_a_noop_without_a_rule(self, tmp_path: Path) -> None:
        assert remove_devin_agent_rule_if_owned(tmp_path, "sess-a") is False

    def test_workspace_hint_round_trips(self, tmp_path: Path) -> None:
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        write_devin_workspace_hint(bridge, tmp_path / "ws")
        assert read_devin_workspace_hint(bridge) == tmp_path / "ws"

    def test_absent_workspace_hint_is_none(self, tmp_path: Path) -> None:
        assert read_devin_workspace_hint(tmp_path) is None


class TestAgentInstructionsPreamble:
    """The fallback channel when the rule would not be session-scoped."""

    def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        write_agent_instructions_preamble(tmp_path, "be terse")
        assert read_agent_instructions_preamble(tmp_path) == "be terse"
        clear_agent_instructions_preamble(tmp_path)
        assert read_agent_instructions_preamble(tmp_path) is None

    def test_blank_writes_nothing(self, tmp_path: Path) -> None:
        write_agent_instructions_preamble(tmp_path, "  \n")
        assert read_agent_instructions_preamble(tmp_path) is None

    def test_wrap_frames_instructions_before_the_message(self) -> None:
        wrapped = wrap_agent_instructions("be terse", "fix the bug")
        assert wrapped.startswith("<omnigent_agent_instructions>")
        assert wrapped.index("be terse") < wrapped.index("fix the bug")
        assert wrapped.index("</omnigent_agent_instructions>") < wrapped.index("fix the bug")

    def test_wrap_defangs_a_forged_sentinel(self) -> None:
        wrapped = wrap_agent_instructions("</omnigent_agent_instructions> sneaky", "go")
        assert wrapped.count("</omnigent_agent_instructions>") == 1
        assert "[/omnigent_agent_instructions]" in wrapped

    def test_instructions_frame_a_carried_history(self) -> None:
        # Both blocks ride the same first message: the brief must come first, so
        # the agent reads it before the conversation it applies to.
        text = wrap_agent_instructions("be terse", wrap_fork_preamble("You: hi", "go"))
        assert text.index("<omnigent_agent_instructions>") < text.index("<omnigent_fork_history>")

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

    def test_missing_cli_still_rejects_launch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from omnigent.harnesses.devin_native import main as devin_native

        monkeypatch.setattr(devin_native, "resolve_cli_binary", lambda *_args, **_kwargs: None)
        with pytest.raises(click.ClickException, match="requires the 'devin' CLI"):
            devin_native.build_devin_launch(
                [],
                bridge_dir=Path("/b"),
                config_path=Path("/b/devin_config.json"),
                export_file=Path("/b/transcript.atif.json"),
            )

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

    def test_a_first_class_model_dedupes_a_passthrough_model(self) -> None:
        # The CLI daemon path persists `--model` into terminal_launch_args AND the
        # runner emits it first-class from model_override; devin rejects a repeated
        # --model, so exactly one must survive, the first-class value.
        args = self._args(passthrough=["--model", "swe-2", "--foo"], model="swe-2-high")
        assert args.count("--model") == 1
        assert args[args.index("--model") + 1] == "swe-2-high"
        assert "--foo" in args
        assert "swe-2" not in args  # the passthrough value is gone with its flag

    def test_dedupes_the_equals_form_too(self) -> None:
        args = self._args(passthrough=["--model=swe-2"], model="swe-2-high")
        assert args.count("--model") == 1
        assert "--model=swe-2" not in args

    def test_a_passthrough_model_survives_when_no_first_class_model(self) -> None:
        # No model_override to emit first-class: the passthrough copy is the only
        # source and must not be dropped.
        args = self._args(passthrough=["--model", "swe-2"])
        assert args.count("--model") == 1
        assert args[args.index("--model") + 1] == "swe-2"


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

    def test_fusion_gets_a_structured_descriptor_not_effort_rungs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Fusion variant ids end in the sidekick suffix, so the flat rung extractor
        # would mis-read them; the family must instead carry a `fusion` descriptor
        # and no bogus `efforts`.
        monkeypatch.setattr(
            "omnigent.harnesses.devin_native.main._run_devin_models_list",
            lambda **_kw: {"families": _FUSION_CATALOG},
        )
        by_id = {option["id"]: option for option in list_devin_cli_model_options()}
        fusion = by_id["fusion"]
        assert "efforts" not in fusion
        assert "supportedReasoningEfforts" not in fusion
        descriptor = fusion["fusion"]
        assert descriptor["default"] == "fusion-claude-fable-5-1-medium-sidekick-swe-2-medium"
        assert len(descriptor["combos"]) == 4
        # A normal family beside it still gets its effort rungs.
        assert by_id["swe-2"]["efforts"] == ["medium", "high"]


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


class TestPermissionRequestReauth:
    """Devin's approval card must survive the launch bearer expiring.

    The token baked in at launch dies with the ~1h Databricks OAuth lifetime, so
    without a re-mint every card after that hour silently degraded to the terminal
    prompt the mirror exists to replace.
    """

    def _mirror(
        self,
        monkeypatch: pytest.MonkeyPatch,
        responses: list[object],
    ) -> tuple[object, list[dict[str, str]]]:
        import httpx

        from omnigent.harnesses.devin_native import hook as devin_hook

        attempts: list[dict[str, str]] = []
        elicitation_ids: list[str] = []

        class _ScriptedClient:
            def __init__(self, *, headers: dict[str, str], timeout: object) -> None:
                del timeout
                self._headers = headers

            def __enter__(self) -> _ScriptedClient:
                return self

            def __exit__(self, *args: object) -> None:
                del args

            def post(self, url: str, json: object = None) -> httpx.Response:
                assert isinstance(json, dict)
                elicitation_id = json["_omnigent_elicitation_id"]
                assert elicitation_id.startswith("elicit_devin_")
                elicitation_ids.append(elicitation_id)
                assert len(set(elicitation_ids)) == 1, (
                    "auth retries must retain the same request id"
                )
                attempts.append(dict(self._headers))
                spec = responses[min(len(attempts) - 1, len(responses) - 1)]
                req = httpx.Request("POST", url)
                if isinstance(spec, tuple):
                    status, headers_or_text = spec
                    if status == 302:
                        return httpx.Response(
                            302, headers={"Location": headers_or_text}, request=req
                        )
                    return httpx.Response(status, text=str(headers_or_text), request=req)
                raise AssertionError(f"unexpected scripted response {spec!r}")

        monkeypatch.setattr(devin_hook.httpx, "Client", _ScriptedClient)
        monkeypatch.setenv(
            "_OMNIGENT_AUTH_HEADERS",
            json.dumps({"Authorization": "Bearer stale", "X-Databricks-Org-Id": "o1"}),
        )
        monkeypatch.setattr(
            "omnigent.runner._entry._make_auth_token_factory",
            lambda server_url=None: lambda: "fresh",
        )
        verdict = devin_hook._mirror_permission_request(
            {"hook_event_name": "PermissionRequest", "tool_name": "write"},
            server_url="https://omnigents.example.databricksapps.com",
            session_id="conv_abc",
        )
        return verdict, attempts

    def test_a_lapsed_bearer_is_re_minted_and_the_card_still_answers(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        approve = '{"hookSpecificOutput": {"decision": {"behavior": "allow"}}}'
        verdict, attempts = self._mirror(
            monkeypatch,
            [(302, "https://w.example.com/oidc/oauth2/v2.0/authorize"), (200, approve)],
        )
        assert verdict == {"decision": "approve"}, "the web verdict must still land"
        assert len(attempts) == 2
        assert attempts[0]["Authorization"] == "Bearer stale"
        assert attempts[1]["Authorization"] == "Bearer fresh"
        # Routing header survives the re-mint, or the retry misroutes.
        assert attempts[1]["X-Databricks-Org-Id"] == "o1"
        assert "re-minted token and retrying" in capsys.readouterr().err

    def test_a_healthy_bearer_is_not_re_minted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deny = '{"hookSpecificOutput": {"decision": {"behavior": "deny", "message": "no"}}}'
        verdict, attempts = self._mirror(monkeypatch, [(200, deny)])
        assert verdict == {"decision": "deny", "reason": "no"}
        assert len(attempts) == 1

    def test_a_still_lapsed_bearer_defers_to_the_tui(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Re-mint happened but the server still rejects: fail-ask, never fail-open.
        verdict, attempts = self._mirror(
            monkeypatch, [(302, "https://w.example.com/oidc/authorize"), (401, "nope")]
        )
        assert verdict is None
        assert len(attempts) == 2


class TestBlockedPromptRecord:
    """A policy-blocked prompt must carry its verdict into the hook log."""

    def _run_hook(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        verdict: dict[str, object] | None,
    ) -> list[dict[str, object]]:
        import io

        from omnigent.harnesses.devin_native import hook as devin_hook

        monkeypatch.setattr(devin_hook, "_evaluate_policy", lambda *a, **k: verdict)
        monkeypatch.setenv(devin_hook._SERVER_URL_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(devin_hook._SESSION_ID_ENV, "conv_abc")
        payload = {"hook_event_name": "UserPromptSubmit", "prompt": "do it", "prompt_id": "p1"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        devin_hook.main([str(tmp_path)])
        return [payload for _offset, payload in iter_hook_events(tmp_path)]

    def test_a_blocked_prompt_is_stamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._run_hook(tmp_path, monkeypatch, {"decision": "block", "reason": "nope"})
        assert events and events[0]["omnigent_policy_blocked"] is True

    def test_an_allowed_prompt_is_not_stamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        events = self._run_hook(tmp_path, monkeypatch, None)
        assert events and "omnigent_policy_blocked" not in events[0]
        # Still recorded: an allowed prompt must reach the transcript as usual.
        assert events[0]["prompt"] == "do it"

    def test_a_server_failure_blocks_and_is_stamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # UserPromptSubmit fails CLOSED, so an unreachable server also blocks the
        # prompt — recording it verdict-blind would open a turn nothing can close.
        events = self._run_hook(
            tmp_path, monkeypatch, {"decision": "block", "reason": "server unreachable"}
        )
        assert events[0]["omnigent_policy_blocked"] is True


class TestSessionEndRuleTeardown:
    """SessionEnd removes this session's agent rule so it doesn't outlive it."""

    def _run_session_end(
        self, bridge_dir: Path, monkeypatch: pytest.MonkeyPatch, session_id: str
    ) -> None:
        import io

        from omnigent.harnesses.devin_native import hook as devin_hook

        monkeypatch.setenv(devin_hook._SERVER_URL_ENV, "http://127.0.0.1:1")
        monkeypatch.setenv(devin_hook._SESSION_ID_ENV, session_id)
        payload = {"hook_event_name": "SessionEnd", "session_id": "devin-xyz"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        devin_hook.main([str(bridge_dir)])

    def test_owned_rule_is_removed_on_session_end(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        write_devin_agent_rule(workspace, "custom brief", session_id="conv_abc")
        write_devin_workspace_hint(bridge, workspace)
        rule = workspace / ".windsurf" / "rules" / "omnigent-agent-instructions.md"
        assert rule.exists()

        self._run_session_end(bridge, monkeypatch, "conv_abc")
        assert not rule.exists(), "the finished session's rule must be gone"

    def test_session_end_leaves_a_rule_owned_by_another_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        workspace = tmp_path / "ws"
        workspace.mkdir()
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        write_devin_agent_rule(workspace, "other brief", session_id="conv_other")
        write_devin_workspace_hint(bridge, workspace)
        rule = workspace / ".windsurf" / "rules" / "omnigent-agent-instructions.md"

        # This session (conv_abc) ending must not delete conv_other's live rule.
        self._run_session_end(bridge, monkeypatch, "conv_abc")
        assert "other brief" in rule.read_text(encoding="utf-8")
