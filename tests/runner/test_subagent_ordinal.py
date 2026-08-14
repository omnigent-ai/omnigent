"""Tests for sub-agent ordinal allocation and cross-runner uniqueness."""

from __future__ import annotations

from omnigent.runner import app as runner_app


def _reset_ordinal_counters() -> None:
    runner_app._subagent_ordinal_counters.clear()


class TestNextSubagentOrdinal:
    def setup_method(self) -> None:
        _reset_ordinal_counters()

    def teardown_method(self) -> None:
        _reset_ordinal_counters()

    def test_first_ordinal_is_one(self) -> None:
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1

    def test_ordinals_increment(self) -> None:
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 2
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 3

    def test_ordinals_independent_per_parent(self) -> None:
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1
        assert runner_app.next_subagent_ordinal("parent_2", "researcher") == 1
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 2

    def test_ordinals_independent_per_agent_type(self) -> None:
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1
        assert runner_app.next_subagent_ordinal("parent_1", "coder") == 1
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 2


class TestRecoverSubagentOrdinals:
    def setup_method(self) -> None:
        _reset_ordinal_counters()

    def teardown_method(self) -> None:
        _reset_ordinal_counters()

    def test_recovery_sets_high_water_mark(self) -> None:
        children = [
            {"session_name": "researcher-1"},
            {"session_name": "researcher-3"},
            {"session_name": "researcher-2"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 4

    def test_recovery_ignores_other_agent_types(self) -> None:
        children = [
            {"session_name": "coder-5"},
            {"session_name": "researcher-2"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 3

    def test_recovery_with_no_matching_children(self) -> None:
        children = [
            {"session_name": "coder-5"},
            {"session_name": "manual-title"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1

    def test_recovery_skips_when_already_initialized(self) -> None:
        runner_app.next_subagent_ordinal("parent_1", "researcher")  # sets to 1
        children = [
            {"session_name": "researcher-10"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        # Should NOT reset — already initialized
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 2

    def test_recovery_handles_empty_children(self) -> None:
        runner_app.recover_subagent_ordinals("parent_1", "researcher", [])
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 1

    def test_recovery_handles_non_string_session_name(self) -> None:
        children = [
            {"session_name": None},
            {"session_name": 42},
            {"session_name": "researcher-2"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 3

    def test_recovery_handles_malformed_ordinals(self) -> None:
        children = [
            {"session_name": "researcher-"},
            {"session_name": "researcher-abc"},
            {"session_name": "researcher-2"},
        ]
        runner_app.recover_subagent_ordinals("parent_1", "researcher", children)
        assert runner_app.next_subagent_ordinal("parent_1", "researcher") == 3
