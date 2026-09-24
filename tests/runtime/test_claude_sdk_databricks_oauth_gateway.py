"""claude-sdk must resolve Databricks gateway credentials from an OAuth U2M profile.

When a ``claude-sdk`` agent is configured with::

    executor:
      harness: claude-sdk
      model: databricks-claude-haiku-4-5
      auth:
        type: databricks
        profile: <profile>

…and no Omnigent ucode state is cached for the workspace (a fresh install or
any machine that has never run ``omnigent setup`` against this Databricks
workspace), the spawn env carries ``HARNESS_CLAUDE_SDK_GATEWAY=true`` and the
profile name but no gateway host/base-URL overrides, so the executor must
derive the gateway transport from the profile itself.

OAuth U2M profiles (the common Azure Databricks credential) have a ``host``
and ``auth_type = databricks-cli`` but NO static ``token`` field — the
Databricks CLI mints bearer tokens on demand. Credential resolution must
therefore need only the profile's ``host`` (always present) and delegate the
bearer to the generated ``databricks auth token --profile …`` command, the
same way the codex gateway path does. A resolution path that requires a
static token fails with::

    OSError: ClaudeSDKExecutor(gateway=True) requires gateway credentials
    from the gateway base URL / auth command or a valid ~/.databrickscfg
    profile.

…making the documented ``type: databricks`` credential unusable for Claude,
while the identical pattern works for ``openai-agents`` (which defers all
credential resolution to request time).
"""

from __future__ import annotations

import configparser
from pathlib import Path

import pytest

from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)

WORKSPACE_HOST = "https://adb-12345.azuredatabricks.net"

# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate OMNIGENT_CONFIG_HOME so the developer's real config is not read.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(
        "omnigent.runtime.workflow._resolve_catalog_default_model",
        lambda provider_name, family, *, context: f"catalog-{provider_name}-{family}-default",
    )


@pytest.fixture()
def oauth_databrickscfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a ``~/.databrickscfg`` with an OAuth U2M profile (no ``token``).

    OAuth U2M profiles have ``host`` and ``auth_type = databricks-cli`` but
    NO static ``token`` field (the Databricks CLI mints tokens on demand).

    :param tmp_path: Temporary directory for the fake config file.
    :param monkeypatch: Pytest monkeypatch fixture to redirect the path.
    :returns: Path to the fake ``~/.databrickscfg``.
    """
    cfg_path = tmp_path / ".databrickscfg"
    cfg = configparser.ConfigParser()
    cfg["my-profile"] = {
        "host": WORKSPACE_HOST,
        "auth_type": "databricks-cli",
        # deliberately NO ``token`` field — OAuth U2M profile
    }
    with cfg_path.open("w") as fh:
        cfg.write(fh)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    # Stub out ucode state so we test the no-ucode path (fresh install).
    monkeypatch.setattr(
        "omnigent.runtime.workflow.get_workspace_url_for_profile",
        lambda profile: WORKSPACE_HOST,
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.read_ucode_state",
        lambda workspace_url: None,
    )
    return cfg_path


def _make_claude_databricks_spec(profile: str = "my-profile") -> AgentSpec:
    """Build a minimal claude-sdk spec with Databricks auth.

    This is exactly the documented ``executor.auth: {type: databricks, …}``
    pattern from the Omnigent Models & Credentials docs.

    :param profile: ``~/.databrickscfg`` profile name.
    :returns: A populated :class:`AgentSpec`.
    """
    return AgentSpec(
        spec_version=1,
        name="test-claude-databricks",
        instructions="You are a test agent.",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "claude-sdk"},
            model="databricks-claude-haiku-4-5",
            auth=DatabricksAuth(profile=profile),
        ),
        llm=LLMConfig(model="databricks-claude-haiku-4-5"),
    )


def _resolve_gateway_env_from_spawn_env(env: dict[str, str]) -> dict[str, str]:
    """Resolve gateway credentials exactly as the claude-sdk harness does.

    Mirrors ``_build_claude_sdk_executor`` in
    ``omnigent/inner/claude_sdk_harness.py``: the spawn env's gateway
    values (any of which may be absent) are threaded into
    :func:`~omnigent.inner.claude_sdk_executor._resolve_gateway_env`.

    :param env: The spawn-env dict from :func:`_build_claude_sdk_spawn_env`.
    :returns: The resolved gateway env (``{}`` means "no credentials").
    """
    from omnigent.inner.claude_sdk_executor import _resolve_gateway_env

    return _resolve_gateway_env(
        env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"),
        host_override=env.get("HARNESS_CLAUDE_SDK_GATEWAY_HOST"),
        base_url_override=env.get("HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"),
        auth_command_override=env.get("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"),
    )


# ── Tests ─────────────────────────────────────────────────────────────────


def test_oauth_profile_resolves_gateway_credentials(
    oauth_databrickscfg: Path,
) -> None:
    """The full spawn-env → executor credential chain works for OAuth profiles.

    Reproduces the reported journey at the unit level: build the spawn env
    for a ``type: databricks`` claude-sdk spec with no cached ucode state,
    then resolve gateway credentials from it exactly as the harness does.

    Without the fix, resolution requires a static ``token`` field in
    ``~/.databrickscfg`` (absent on OAuth U2M profiles), returns ``{}``, and
    the executor raises "requires gateway credentials" on every launch.
    """
    spec = _make_claude_databricks_spec()
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true", (
        "Expected HARNESS_CLAUDE_SDK_GATEWAY=true for DatabricksAuth spec"
    )
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "my-profile", (
        "The profile must be threaded to the executor for host + token resolution"
    )

    gateway_env = _resolve_gateway_env_from_spawn_env(env)
    assert gateway_env, (
        "Gateway credential resolution returned {} for an OAuth U2M profile "
        "(host present, no static token) — the executor will raise "
        "'ClaudeSDKExecutor(gateway=True) requires gateway credentials'. "
        "The workspace host must be derivable from the profile alone."
    )
    assert gateway_env["ANTHROPIC_BASE_URL"] == f"{WORKSPACE_HOST}/ai-gateway/anthropic", (
        "ANTHROPIC_BASE_URL must route the Claude CLI to the profile "
        "workspace's Databricks AI Gateway"
    )


def test_oauth_profile_auth_command_mints_token_at_request_time(
    oauth_databrickscfg: Path,
) -> None:
    """The resolved auth command must mint the bearer via the profile, not a static token.

    OAuth U2M profiles have no static token, so the gateway env must carry a
    ``databricks auth token --profile …`` helper command (run by the Claude
    CLI at request time) rather than depending on a token snapshot.
    """
    spec = _make_claude_databricks_spec()
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    gateway_env = _resolve_gateway_env_from_spawn_env(env)
    assert gateway_env, "gateway credentials must resolve for an OAuth profile"
    helper = gateway_env.get("OMNIGENT_CLAUDE_API_KEY_HELPER", "")
    assert 'databricks auth token --profile "my-profile"' in helper, (
        "The bearer must be minted per-request via the profile-pinned "
        "Databricks CLI command; a static token cannot exist for OAuth U2M "
        f"profiles. Helper was: {helper!r}"
    )


def test_executor_constructs_with_oauth_profile(
    oauth_databrickscfg: Path,
) -> None:
    """ClaudeSDKExecutor(gateway=True) must construct from an OAuth profile.

    This is the exact failure surface from the bug journey: the executor's
    constructor eagerly resolves the gateway transport and raised::

        OSError: ClaudeSDKExecutor(gateway=True) requires gateway credentials …

    for every launch with an OAuth U2M profile. After the fix it constructs
    and carries a usable ``ANTHROPIC_BASE_URL`` in its spawn extra-env.
    """
    from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor

    spec = _make_claude_databricks_spec()
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Mirrors _build_claude_sdk_executor's env → constructor mapping.
    executor = ClaudeSDKExecutor(
        gateway=env.get("HARNESS_CLAUDE_SDK_GATEWAY", "").lower() in ("1", "true", "yes"),
        databricks_profile=env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"),
        gateway_host=env.get("HARNESS_CLAUDE_SDK_GATEWAY_HOST") or None,
        base_url_override=env.get("HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL") or None,
        gateway_auth_command=env.get("HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND") or None,
    )
    assert executor._extra_env["ANTHROPIC_BASE_URL"] == (
        f"{WORKSPACE_HOST}/ai-gateway/anthropic"
    ), "The executor must route to the profile workspace's AI Gateway"


def test_openai_agents_databricks_auth_works_for_contrast(
    oauth_databrickscfg: Path,
) -> None:
    """Confirm openai-agents handles the same OAuth profile without error (contrast).

    The identical credential pattern has always worked for ``openai-agents``:
    it defers all credential resolution to request time (via the Databricks
    SDK's full OAuth support), so it never fails at spawn time. This test
    preserves that invariant so a fix cannot accidentally break it.

    :param oauth_databrickscfg: Fixture providing the OAuth U2M config.
    """
    from omnigent.runtime.workflow import _build_openai_agents_sdk_spawn_env

    spec = AgentSpec(
        spec_version=1,
        name="test-openai-databricks",
        instructions="You are a test agent.",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "openai-agents"},
            model="gpt-5-6-luna",
            auth=DatabricksAuth(profile="my-profile"),
        ),
        llm=LLMConfig(model="gpt-5-6-luna"),
    )
    # Must not raise — openai-agents handles OAuth profiles correctly.
    env = _build_openai_agents_sdk_spawn_env(spec)
    assert env.get("HARNESS_OPENAI_AGENTS_DATABRICKS_PROFILE") == "my-profile", (
        "openai-agents must still thread the profile env var for token refresh"
    )
