"""Tests for the built-in agent bundle builders in ``omnigent/server/app.py``.

The server seeds Web-UI-launchable agents (claude-native, codex-native, and
the shipped ``debby`` / ``polly`` examples) by materializing each spec into a
gzipped tarball at startup. These builders were previously exercised only
transitively by the agents' e2e suites; a packaging regression (a dropped
``config.yaml``, a spec that no longer materializes) would surface late and
slow. These unit tests build each bundle directly and assert it is a valid,
reproducible tarball containing the expected spec entry.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.harness_plugins import native_provider_for_key
from omnigent.native_coding_agents import NATIVE_CODING_AGENTS as _NATIVE_CODING_AGENTS
from omnigent.server import app
from omnigent.spec import load, materialize_bundle

# Native built-ins are seeded through one registry-driven builder,
# ``_build_native_bundle(provider)`` (PR 1.7). We exercise EVERY native agent
# (not a sample) so each harness's materialize path — and both model-arg shapes
# (codex requires keyword-only model, kiro/opencode default it, the rest take
# just tmpdir) — is covered directly rather than only transitively via e2e.
# Each spec entry is ``<agent_name>.yaml``, proving the right spec materialized.
_NATIVE_BUILDERS = [(agent.key, f"{agent.agent_name}.yaml") for agent in _NATIVE_CODING_AGENTS]

# Shipped-example built-ins keep their hand-written named builders. The bool is
# whether the source is a shipped example a stripped deployment may omit.
_EXAMPLE_BUILDERS = [
    ("_build_debby_bundle", "config.yaml", True),
    ("_build_polly_bundle", "config.yaml", True),
]


def _shipped_example_missing(builder: str) -> bool:
    """Return True when ``builder``'s shipped-example source is not packaged here.

    debby/polly are only seeded when their bundle ships with the wheel; a
    generic deployment legitimately omits them. Skip rather than fail there.
    """
    source = {
        "_build_debby_bundle": app._DEBBY_BUNDLE_SOURCE,
        "_build_polly_bundle": app._POLLY_BUNDLE_SOURCE,
    }[builder]
    return not (source / "config.yaml").is_file()


def _tar_members(blob: bytes) -> list[str]:
    """Return the regular-file member names inside a gzipped tarball."""
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        return [m.name for m in tf.getmembers() if m.isfile()]


def _assert_valid_tarball_with_entry(blob: bytes, spec_entry: str, label: str) -> None:
    """Assert ``blob`` is a valid gzip tarball containing ``spec_entry``."""
    assert blob, f"{label} returned empty bytes."
    # Must be a real gzip stream, not just arbitrary bytes.
    assert gzip.decompress(blob), f"{label} output is not valid gzip."
    members = _tar_members(blob)
    # arcname='.' prefixes every member with './'; match on the trailing path.
    assert any(m.lstrip("./") == spec_entry or m.endswith("/" + spec_entry) for m in members), (
        f"{label} tarball is missing its spec entry {spec_entry!r}; members={members!r}."
    )


@pytest.mark.parametrize(("key", "spec_entry"), _NATIVE_BUILDERS)
def test_native_bundle_builder_produces_valid_tarball(key: str, spec_entry: str) -> None:
    """The registry-driven native builder yields a tarball with the agent's spec."""
    provider = native_provider_for_key(key)
    assert provider is not None, f"no native provider for {key!r}"
    _assert_valid_tarball_with_entry(app._build_native_bundle(provider), spec_entry, key)


@pytest.mark.parametrize(("key", "spec_entry"), _NATIVE_BUILDERS)
def test_native_bundle_builder_is_reproducible(key: str, spec_entry: str) -> None:
    """The native builder is byte-for-byte reproducible (content-addressable seed)."""
    provider = native_provider_for_key(key)
    assert provider is not None, f"no native provider for {key!r}"
    assert app._build_native_bundle(provider) == app._build_native_bundle(provider), (
        f"native builder for {key!r} is not byte-for-byte reproducible."
    )


@pytest.mark.parametrize(("builder", "spec_entry", "shipped_example"), _EXAMPLE_BUILDERS)
def test_bundle_builder_produces_valid_tarball(
    builder: str, spec_entry: str, shipped_example: bool
) -> None:
    """Each shipped-example builder returns a gzip tarball with the agent's spec file."""
    if shipped_example and _shipped_example_missing(builder):
        pytest.skip(f"{builder} source not packaged in this deployment")
    _assert_valid_tarball_with_entry(getattr(app, builder)(), spec_entry, builder)


@pytest.mark.parametrize(("builder", "spec_entry", "shipped_example"), _EXAMPLE_BUILDERS)
def test_bundle_builder_is_reproducible(
    builder: str, spec_entry: str, shipped_example: bool
) -> None:
    """Two builds yield byte-identical tarballs (the builders are content-addressable).

    Reproducibility is what lets the startup seeder refresh a row in place only
    when the spec actually changed; a non-deterministic build would re-seed on
    every restart.
    """
    if shipped_example and _shipped_example_missing(builder):
        pytest.skip(f"{builder} source not packaged in this deployment")

    fn = getattr(app, builder)
    assert fn() == fn(), f"{builder} is not byte-for-byte reproducible across builds."


# ── Backwards-compatible loading of the shipped sub-agent examples ──────────
#
# polly and debby are the two shipped examples that ship *sub-agents*, so they
# are the surface for the version-skew regression matei hit: a newer server
# adds a sub-agent whose harness an older client can't validate, and the old
# client must still launch the parent (dropping only the unsupported worker)
# rather than failing every dispatch of the agent. These tests exercise the
# REAL shipped definitions (not synthetic minimal specs — see
# tests/spec/test_load.py for those) so a regression in either the prune logic
# OR the polly/debby structure (e.g. a parent that becomes un-prunable) is
# caught here. See omnigent.spec.load(..., prune_invalid_sub_agents=True).

# (name, bundle source dir, sub-agents the shipped definition declares today)
_SHIPPED_SUB_AGENT_EXAMPLES = [
    (
        "polly",
        app._POLLY_BUNDLE_SOURCE,
        {"claude_code", "codex", "opencode", "cursor", "hermes", "pi"},
    ),
    ("debby", app._DEBBY_BUNDLE_SOURCE, {"claude", "gpt"}),
]


@pytest.mark.parametrize(("name", "source", "expected_sub_agents"), _SHIPPED_SUB_AGENT_EXAMPLES)
def test_shipped_example_loads_with_all_sub_agents(
    name: str, source: Path, expected_sub_agents: set[str]
) -> None:
    """The shipped definition loads and every declared sub-agent survives.

    A baseline guard: today's polly/debby validate cleanly on this version, so
    the version-skew tests below are exercising a genuinely *unsupported* extra
    sub-agent, not masking a pre-existing breakage in the shipped spec.
    """
    if not (source / "config.yaml").is_file():
        pytest.skip(f"{name} source not packaged in this deployment")

    # expand_env=False mirrors the execution-path default (AgentCache /
    # session-scoped runner resolution) and keeps the test independent of the
    # host environment.
    spec = load(source, expand_env=False)
    assert spec.name == name
    sub_names = {sa.name for sa in spec.sub_agents}
    assert expected_sub_agents <= sub_names, (
        f"{name} is missing declared sub-agents: "
        f"{expected_sub_agents - sub_names}; got {sub_names}"
    )
    assert expected_sub_agents <= set(spec.tools.agents)


@pytest.mark.parametrize(("name", "source", "expected_sub_agents"), _SHIPPED_SUB_AGENT_EXAMPLES)
def test_shipped_example_survives_unknown_harness_sub_agent(
    name: str, source: Path, expected_sub_agents: set[str], tmp_path: Path
) -> None:
    """A newer-server sub-agent the old client can't validate is dropped, not fatal.

    Reproduces matei's incident on the real bundles: a server bumped polly to a
    definition with an ``opencode`` sub-agent, and older runners/hosts failed to
    launch *any* polly because the then-unknown ``opencode-native`` harness
    failed the whole spec's validation. ``opencode-native`` is a recognized
    harness now, so we inject a deliberately-synthetic harness — the stand-in
    for "whatever the next server adds that this client doesn't know yet" — as a
    worker referenced from the parent's ``tools.agents``, and assert:

    - the strict (authoring/upload) load still fails loud — unchanged behavior;
    - the execution-path load (``prune_invalid_sub_agents=True``) drops ONLY the
      unsupported worker and keeps the parent plus every real sub-agent.
    """
    if not (source / "config.yaml").is_file():
        pytest.skip(f"{name} source not packaged in this deployment")

    # Materialize the real bundle (the server's own path) so we test the
    # shipped sub-agents, then add the worker a newer server would.
    bundle = materialize_bundle(source, tmp_path / name)
    future_worker = bundle / "agents" / "future_worker"
    future_worker.mkdir(parents=True)
    (future_worker / "config.yaml").write_text(
        yaml.dump(
            {
                "spec_version": 1,
                "name": "future_worker",
                "executor": {
                    "type": "omnigent",
                    "config": {"harness": "harness-from-a-newer-server"},
                },
            }
        )
    )
    # Declare it on the parent exactly as a real newer-server spec would — an
    # undeclared agents/ dir would be parsed but never referenced, so the
    # dangling-reference cleanup would go untested.
    parent_cfg = yaml.safe_load((bundle / "config.yaml").read_text())
    parent_cfg.setdefault("tools", {}).setdefault("agents", []).append("future_worker")
    (bundle / "config.yaml").write_text(yaml.dump(parent_cfg))

    # Strict: the whole spec still fails (this is what matei hit, preserved for
    # authoring/upload so real harness typos surface to the author).
    with pytest.raises(OmnigentError, match="invalid agent spec"):
        load(bundle, expand_env=False)

    # Execution path: parent + real workers survive; only the unknown one drops.
    spec = load(bundle, expand_env=False, prune_invalid_sub_agents=True)
    assert spec.name == name
    sub_names = {sa.name for sa in spec.sub_agents}
    assert "future_worker" not in sub_names
    assert expected_sub_agents <= sub_names
    # The dangling reference must be cleaned off the parent too, or the parent
    # itself would have failed validation on the missing-directory check.
    assert "future_worker" not in spec.tools.agents
    assert expected_sub_agents <= set(spec.tools.agents)


# ── Byte-stability of built-in agent ids (PR 1.7 seeding loop) ──────────────
#
# The startup seeder derives each built-in's id from its NAME via
# builtin_agent_id (a pure sha256 of "builtin:<name>"). Persisted
# conversation.agent_id rows point at these ids, so a rename — or a seeding
# loop that seeds a different name — silently orphans every session on the old
# agent after a redeploy. This freezes the expected id for every native
# built-in as it is today; a change here is a loud signal to migrate, not a
# value to blindly update.
_EXPECTED_BUILTIN_AGENT_IDS = {
    "claude-native-ui": "58a1bc5bf0bba6d31ceeb7661f8d751c",
    "codex-native-ui": "16a06503889b0c3034496821afd41b9e",
    "pi-native-ui": "a1b7caa17404f6716180ba69aa37c592",
    "opencode-native-ui": "cf65137fc096a61a6434956c92093549",
    "cursor-native-ui": "a5fac3a24c1961af2dbb1cafe0a81425",
    "kiro-native-ui": "cc8fef6623a8792be9c039cf2673ce93",
    "goose-native-ui": "93dd98c4a79bd26a1dd5dc592ee38afb",
    "antigravity-native-ui": "db097e89797b66fb7e30699813ae09d2",
    "qwen-native-ui": "2cffe181a2fc6cf417a49e1cf8b28d77",
    "kimi-native-ui": "9e7d109e7da66e8b5ed4ec3ecb54cef1",
    "hermes-native-ui": "32113910cf31fcc63dc96bbde428b97c",
}


def test_native_seed_ids_are_byte_stable() -> None:
    """Every native built-in seeds under its frozen, name-derived id.

    Guards the redeploy-safety invariant the seeding loop must preserve: the
    id is a hash of the agent name, so the set of names AND their ids must not
    drift when the 11 hand-written seed helpers collapse into one loop.
    """
    from omnigent.db.utils import builtin_agent_id
    from omnigent.native_coding_agents import NATIVE_CODING_AGENTS

    actual = {a.agent_name: builtin_agent_id(a.agent_name) for a in NATIVE_CODING_AGENTS}
    assert actual == _EXPECTED_BUILTIN_AGENT_IDS, (
        "built-in native agent ids drifted; a redeploy would orphan persisted "
        "conversation.agent_id rows. Migrate deliberately, do not just update this table."
    )


def test_native_seed_loop_covers_every_native_agent() -> None:
    """The seeding loop can resolve a provider for every native coding agent.

    A native agent with no provider row would be silently dropped from the
    seeded set (the loop raises instead — this pins that contract).
    """
    from omnigent.harness_plugins import native_provider_for_key
    from omnigent.native_coding_agents import NATIVE_CODING_AGENTS

    missing = [a.key for a in NATIVE_CODING_AGENTS if native_provider_for_key(a.key) is None]
    assert missing == [], f"native agents without a provider row to seed from: {missing}"
