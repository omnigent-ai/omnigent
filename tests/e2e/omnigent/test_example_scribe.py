"""Structural test for the Scribe documentation bundle (examples/scribe).

Scribe is the docs counterpart to Polly: it authors prose itself and delegates
read-only code investigation to a ``researcher`` sub-agent (claude-sdk), with an
optional cross-vendor fact-check by a ``reviewer`` sub-agent (codex). Pure
spec-load — no LLM, no credentials — modeled on ``test_example_debby.py``.

What breaks if this fails:
- a sub-agent is dropped or renamed (Scribe loses investigation or the
  fact-check),
- the reviewer collapses onto claude-sdk (the cross-model fact-check stops being
  independent),
- a sub-agent silently pins a model (re-coupling it to one provider — a
  Databricks-only id would 404 on a plain Anthropic / OpenAI key),
- a doc skill (changelog / migration-guide / api-docs) is dropped or renamed,
- the ``os_env`` block disappears (Scribe loses the file/shell tools it reads
  context and writes docs with).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

# tests/e2e/omnigent/test_example_scribe.py -> repo root is 3 parents up.
_SCRIBE_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "scribe"


@pytest.fixture(scope="module")
def scribe_spec() -> AgentSpec:
    """Load and validate the scribe bundle once for the module."""
    return load(_SCRIBE_BUNDLE)


def test_scribe_has_researcher_and_cross_vendor_reviewer(scribe_spec: AgentSpec) -> None:
    """
    Scribe has exactly two sub-agents: a ``researcher`` on claude-sdk and a
    ``reviewer`` on codex.

    The reviewer running a different vendor than Scribe's claude-sdk brain is
    the whole point of the fact-check — it catches claims the author's own model
    would wave through. If the reviewer lands on claude-sdk too, the cross-model
    check is no longer independent.
    """
    assert scribe_spec.name == "scribe"
    fam = {a.name: a.executor.config.get("harness") for a in scribe_spec.sub_agents}
    assert sorted(scribe_spec.tools.agents) == ["researcher", "reviewer"]
    assert fam["researcher"] == "claude-sdk"
    assert fam["reviewer"] == "codex"
    # Reviewer is a different vendor than the brain → the fact-check is independent.
    assert fam["researcher"] != fam["reviewer"]


def test_scribe_sub_agents_are_unpinned(scribe_spec: AgentSpec) -> None:
    """
    Neither sub-agent pins a model: each inherits whatever Claude / OpenAI
    provider the user configured.

    Un-pinning is load-bearing for OSS — a Databricks-specific model id would
    404 on a plain Anthropic / OpenAI key. Re-introducing a pin re-couples a
    sub-agent to one provider, so fail here if a model reappears.
    """
    by_name = {a.name: a for a in scribe_spec.sub_agents}
    for name in ("researcher", "reviewer"):
        assert by_name[name].executor.model is None, name
        assert by_name[name].executor.profile is None, name


def test_scribe_doc_skills_present(scribe_spec: AgentSpec) -> None:
    """The three doc skills are discovered from skills/<name>/SKILL.md."""
    assert sorted(s.name for s in scribe_spec.skills) == [
        "api-docs",
        "changelog",
        "migration-guide",
    ]


def test_scribe_has_os_env(scribe_spec: AgentSpec) -> None:
    """
    Scribe carries an ``os_env`` block so the bridged ``sys_os_*`` tools register
    — it reads change context (git/gh) and writes docs through them. The shipped
    sandbox is ``type: none`` so the bundle loads on macOS too. Dropping
    ``os_env`` would leave Scribe with no file/shell tools at all.
    """
    assert scribe_spec.os_env is not None
    assert scribe_spec.os_env.type == "caller_process"
    assert scribe_spec.os_env.sandbox is not None
    assert scribe_spec.os_env.sandbox.type == "none"
