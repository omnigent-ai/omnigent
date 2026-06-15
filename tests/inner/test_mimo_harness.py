"""Tests for the Mimo harness wrapper."""

from __future__ import annotations

from omnigent.inner import mimo_harness


def test_mimo_harness_builds_executor_from_env(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    seen = {}

    class FakeMimoExecutor:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(mimo_harness, "MimoExecutor", FakeMimoExecutor)
    monkeypatch.setenv("HARNESS_MIMO_MODEL", "anthropic/claude-sonnet-4")
    monkeypatch.setenv("HARNESS_MIMO_PATH", "/bin/mimo")
    monkeypatch.setenv("HARNESS_MIMO_CWD", "/work")
    monkeypatch.setenv("HARNESS_MIMO_AGENT_NAME", "mimo-coder")
    monkeypatch.setenv("HARNESS_MIMO_SKILLS_FILTER", '["alpha"]')

    executor = mimo_harness._build_mimo_executor()

    assert isinstance(executor, FakeMimoExecutor)
    assert seen["model"] == "anthropic/claude-sonnet-4"
    assert seen["mimo_path"] == "/bin/mimo"
    assert seen["cwd"] == "/work"
    assert seen["agent_name"] == "mimo-coder"
    assert seen["skills_filter"] == ["alpha"]
