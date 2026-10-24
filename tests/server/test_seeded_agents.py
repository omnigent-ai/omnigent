"""
Tests for the ``OMNIGENT_SEEDED_AGENTS`` allowlist.

Covers the parser, the per-helper seeding gate, and the suppression set
``_ensure_default_agents`` publishes for ``GET /v1/agents``. The route-level
filter that consumes it lives in ``tests/server/routes/test_builtin_agents.py``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import app as server_app
from omnigent.server import seeded_agents
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore


@dataclass
class _SeedStores:
    """
    The three stores the default-agent seeders take.

    :param agent_store: Store the seeder writes the agent row into.
    :param artifact_store: Store the seeder writes the bundle blob into.
    :param agent_cache: Cache the seeder evicts after registering.
    """

    agent_store: SqlAlchemyAgentStore
    artifact_store: LocalArtifactStore
    agent_cache: AgentCache


@pytest.fixture()
def seed_stores(tmp_path: Path, db_uri: str) -> _SeedStores:
    """
    Real stores wired for the default-agent seeders.

    :param tmp_path: Per-test temp dir for the artifact store and cache.
    :param db_uri: SQLite URI for the agent store.
    :returns: The bundled stores as a :class:`_SeedStores`.
    """
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return _SeedStores(
        agent_store=SqlAlchemyAgentStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(
            artifact_store=artifact_store,
            cache_dir=tmp_path / "cache",
        ),
    )


# ── parser ───────────────────────────────────────────────────────────────


def test_allowlist_is_none_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means "seed everything" — distinct from an empty allowlist.

    Collapsing the two would make an unset var seed nothing, emptying the
    picker on every existing deployment.
    """
    monkeypatch.delenv(seeded_agents.SEEDED_AGENTS_ENV, raising=False)
    assert seeded_agents.seeded_agent_allowlist() is None


def test_allowlist_empty_when_set_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicitly empty value keeps no packaged built-ins."""
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "")
    assert seeded_agents.seeded_agent_allowlist() == frozenset()


def test_allowlist_tolerates_whitespace_and_blank_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hand-edited env values carry stray spaces and trailing commas."""
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, " polly , ,debby, ")
    assert seeded_agents.seeded_agent_allowlist() == frozenset({"polly", "debby"})


@pytest.mark.parametrize(
    ("name", "allowlist", "expected"),
    [
        ("polly", None, True),
        ("polly", frozenset({"polly"}), True),
        ("polly", frozenset({"debby"}), False),
        ("polly", frozenset(), False),
    ],
)
def test_seeded_agent_allowed(name: str, allowlist: frozenset[str] | None, expected: bool) -> None:
    """``None`` allows everything; a set allows exactly its members."""
    assert seeded_agents.seeded_agent_allowed(name, allowlist) is expected


# ── seeding gate ─────────────────────────────────────────────────────────


def test_native_seeder_skips_agents_outside_the_allowlist(
    seed_stores: _SeedStores,
) -> None:
    """Only allowlisted native rows are written, and every name is reported.

    The returned set drives suppression, so it must cover the skipped names
    too — otherwise a row an earlier boot seeded would stay visible.
    """
    owned = server_app._ensure_default_native_agents(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
        frozenset({"claude-native-ui"}),
    )

    assert seed_stores.agent_store.get_by_name("claude-native-ui") is not None
    assert seed_stores.agent_store.get_by_name("codex-native-ui") is None
    assert {"claude-native-ui", "codex-native-ui"} <= owned


def test_native_seeder_seeds_everything_when_allowlist_is_none(
    seed_stores: _SeedStores,
) -> None:
    """The default path is unchanged — no env var, no filtering."""
    owned = server_app._ensure_default_native_agents(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
        None,
    )

    for name in owned:
        assert seed_stores.agent_store.get_by_name(name) is not None, name


def test_polly_seeder_skips_when_not_allowlisted(seed_stores: _SeedStores) -> None:
    """polly is gated, and still reports its name so it can be suppressed."""
    owned = server_app._ensure_default_polly_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
        frozenset({"claude-native-ui"}),
    )

    assert owned == frozenset({server_app._POLLY_AGENT_NAME})
    assert seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME) is None


def test_debby_seeder_skips_when_not_allowlisted(seed_stores: _SeedStores) -> None:
    """debby is gated the same way as polly."""
    owned = server_app._ensure_default_debby_agent(
        seed_stores.agent_store,
        seed_stores.artifact_store,
        seed_stores.agent_cache,
        frozenset(),
    )

    assert owned == frozenset({server_app._DEBBY_AGENT_NAME})
    assert seed_stores.agent_store.get_by_name(server_app._DEBBY_AGENT_NAME) is None


# ── end-to-end seeding ───────────────────────────────────────────────────


def test_ensure_default_agents_publishes_suppressed_names(
    seed_stores: _SeedStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A trimmed roster seeds only the allowlist and suppresses the rest."""
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "claude-native-ui")

    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    assert "claude-native-ui" not in suppressed
    assert server_app._POLLY_AGENT_NAME in suppressed
    assert "codex-native-ui" in suppressed
    assert seed_stores.agent_store.get_by_name("claude-native-ui") is not None
    assert seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME) is None


def test_ensure_default_agents_suppresses_nothing_by_default(
    seed_stores: _SeedStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the var unset, nothing is hidden — the pre-change behavior."""
    monkeypatch.delenv(seeded_agents.SEEDED_AGENTS_ENV, raising=False)

    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    assert suppressed == frozenset()


def test_extra_builtin_agents_bypass_the_allowlist(
    seed_stores: _SeedStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator's own agents seed even when the packaged roster is empty.

    This is the headline use case: trim every packaged built-in, keep only
    the deployment's own agents. Gating the extras too would empty the
    picker entirely.
    """
    custom = tmp_path / "house-agent.yaml"
    custom.write_text(
        "name: house-agent\n"
        "executor:\n"
        "  harness: claude-sdk\n"
        "  model: claude-sonnet-4-20250514\n"
        "prompt: hi\n"
    )
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "")
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(custom))

    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    seeded = seed_stores.agent_store.get_by_name("house-agent")
    assert seeded is not None, "operator extras must survive an empty allowlist"
    assert seeded.name not in suppressed
    assert seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME) is None


def test_suppression_does_not_delete_an_already_seeded_row(
    seed_stores: _SeedStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row seeded before the allowlist landed is hidden, never dropped.

    ``conversations.agent_id`` cascades on delete, so removing the row would
    take its session history. Re-seeding with a narrower allowlist must
    leave the row intact and only report it as suppressed.
    """
    monkeypatch.delenv(seeded_agents.SEEDED_AGENTS_ENV, raising=False)
    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )
    before = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert before is not None, "precondition: polly seeded on the default path"

    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "claude-native-ui")
    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    after = seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME)
    assert after is not None, "suppression must not delete the row"
    assert after.id == before.id
    assert server_app._POLLY_AGENT_NAME in suppressed


def test_os_pathsep_is_not_a_separator(monkeypatch: pytest.MonkeyPatch) -> None:
    """The list is comma-separated — these are names, not paths.

    ``OMNIGENT_BUILTIN_AGENT_DIRS`` next door splits on ``os.pathsep``; a
    reader who assumes the same here should get a clear miss, not a silent
    single-entry allowlist that happens to match nothing.
    """
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, f"polly{os.pathsep}debby")
    parsed = seeded_agents.seeded_agent_allowlist()
    assert parsed is not None
    assert "polly" not in parsed


def test_extra_that_overrides_a_packaged_name_is_not_suppressed(
    seed_stores: _SeedStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator override of a packaged name survives a trimmed allowlist.

    ``OMNIGENT_BUILTIN_AGENT_DIRS`` is documented as the supported way to
    override a built-in by name. With the allowlist dropping that same name,
    the suppressed set must not include it — the override IS the operator's
    roster, and hiding it would empty the picker of exactly what they seeded.
    """
    override = tmp_path / f"{server_app._POLLY_AGENT_NAME}.yaml"
    override.write_text(
        f"name: {server_app._POLLY_AGENT_NAME}\n"
        "executor:\n"
        "  harness: claude-sdk\n"
        "  model: claude-sonnet-4-20250514\n"
        "prompt: house polly\n"
    )
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "")
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(override))
    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )
    assert seed_stores.agent_store.get_by_name(server_app._POLLY_AGENT_NAME) is not None
    assert server_app._POLLY_AGENT_NAME not in suppressed


def test_extras_seeder_reports_the_names_it_seeded(
    seed_stores: _SeedStores, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The extras helper returns its seeded names; nothing when unset."""
    monkeypatch.delenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, raising=False)
    assert (
        server_app._ensure_extra_builtin_agents(
            seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
        )
        == frozenset()
    )
    custom = tmp_path / "house-agent.yaml"
    custom.write_text(
        "name: house-agent\n"
        "executor:\n"
        "  harness: claude-sdk\n"
        "  model: claude-sonnet-4-20250514\n"
        "prompt: hi\n"
    )
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(custom))
    assert server_app._ensure_extra_builtin_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    ) == frozenset({"house-agent"})


def test_unknown_allowlist_name_warns_instead_of_silently_hiding_everything(
    seed_stores: _SeedStores,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo'd name (``claude-native`` for ``claude-native-ui``) is called out.

    The allowlist matches nothing, so every packaged agent is suppressed —
    the log must say the configured name is unknown, not just list what was
    trimmed, or the operator has no clue why the picker went empty.
    """
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "claude-native,Polly")
    with caplog.at_level("WARNING", logger=server_app.__name__):
        server_app._ensure_default_agents(
            seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
        )
    warning = next(r for r in caplog.records if seeded_agents.SEEDED_AGENTS_ENV in r.getMessage())
    message = warning.getMessage()
    # Both unknown names are called out, and the real roster is offered.
    assert "claude-native" in message and "Polly" in message
    assert "claude-native-ui" in message


def test_configured_acp_agents_are_never_trimmed(
    seed_stores: _SeedStores, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``acp:`` entry is the operator's own roster, not a packaged built-in.

    Trimming the packaged set is how a deployment makes room for its own agents,
    so the allowlist must not take those with it — and their slugs, which are
    operator-specific, must not be published as suppressed names either.
    """
    from omnigent.onboarding.acp_auth import AcpAgentEntry

    configured = [AcpAgentEntry(name="Gemini CLI", slug="gemini-cli", command=["gemini", "--acp"])]
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents", lambda *args, **kwargs: configured
    )
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.shadowed_builtin_acp_rows",
        lambda *args, **kwargs: frozenset(),
    )
    # The strictest case: keep nothing packaged at all.
    monkeypatch.setenv(seeded_agents.SEEDED_AGENTS_ENV, "")

    suppressed = server_app._ensure_default_agents(
        seed_stores.agent_store, seed_stores.artifact_store, seed_stores.agent_cache
    )

    names = {agent.name for agent in seed_stores.agent_store.list(limit=200).data}
    assert "gemini-cli" in names, "a configured acp: agent was trimmed with the packaged roster"
    assert "gemini-cli" not in suppressed
    # The packaged rows really were trimmed, so the test is not passing by accident.
    assert "polly" not in names
    # A builtin ACP row is packaged, not the operator's, so it obeys the allowlist.
    assert "grok" not in names
    assert "grok" in suppressed
