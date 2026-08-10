"""No agent-CLI subprocess may inherit unrelated host secrets (#3445).

The pi and codex executors filtered; goose, kimi, qwen, acp and hermes did not,
and hermes's no-HERMES_HOME branch passed ``env=None`` (inherit everything).

This is the parametrized canary the maintainer asked for. The point is not to
re-test each executor's wiring — it is that the *next* harness added to this
repo fails here if it forgets to filter. The fix pattern already existed twice
and still missed five siblings.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.inner.agent_env import (
    BASE_ALLOW_EXACT,
    BASE_ALLOW_PREFIXES,
    clean_agent_env,
    declared_passthrough,
)

# Families that must never reach a vendor CLI unless the spec asks for them.
# One planted value per family, all distinct so a failure names the leak.
CANARY_SECRETS = {
    "AWS_SECRET_ACCESS_KEY": "canary-aws",
    "AWS_SESSION_TOKEN": "canary-aws-session",
    "DATABRICKS_TOKEN": "canary-databricks",
    "DATABRICKS_CONFIG_PROFILE": "canary-databricks-profile",
    "ANTHROPIC_API_KEY": "canary-anthropic",
    "GEMINI_API_KEY": "canary-gemini",
    "GITHUB_TOKEN": "canary-github",
    "SLACK_BOT_TOKEN": "canary-slack",
    "OPENROUTER_API_KEY": "canary-openrouter",
}

# The per-harness families, matching the decision table on #3445.
HARNESS_PREFIXES = {
    "pi": ("PI_", "NODE_"),
    "codex": ("OPENAI_", "REQUESTS_", "CODEX_HOME"),
    "qwen": ("QWEN_", "OPENAI_", "DASHSCOPE_"),
    "goose": ("GOOSE_",),
    "kimi": ("KIMI_", "MOONSHOT_"),
    "acp": (),
    "hermes": ("HERMES_",),
}


@pytest.fixture
def hostile_env():
    """A host environment carrying every canary plus the things a CLI needs."""
    return {
        "HOME": "/home/user",
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
        "HTTPS_PROXY": "http://proxy:8080",
        **CANARY_SECRETS,
    }


# --- the real builders -----------------------------------------------------
#
# The prefix table above is a hand copy of what each executor passes, so on its
# own it only proves clean_agent_env works. These drive each executor's OWN
# spawn-env builder against a planted os.environ, which is what actually makes
# the "next harness fails here" claim true: if qwen reverts to
# os.environ.copy(), or harness number ten never calls the helper, this fails.


def _bare(cls, **attrs):
    """An executor carrying only the attributes its env builder reads.

    The constructors want a live config, an event loop and a spawned CLI. The
    builders do not — they read ``_os_env`` and, for hermes, ``_hermes_home``.
    Bypassing ``__init__`` keeps the canary on the real method without dragging
    in the rest of the executor.
    """
    obj = object.__new__(cls)
    obj._os_env = None
    for name, value in attrs.items():
        setattr(obj, name, value)
    return obj


def _acp_spawn_env():
    from omnigent.inner.acp_executor import AcpExecutor

    return _bare(AcpExecutor)._build_spawn_env()


def _goose_spawn_env():
    from omnigent.inner.goose_executor import GooseExecutor

    # Provider/gateway overrides are deliberate additions layered on top of the
    # filtered base, not leaks; stub them so this asserts the filtering alone.
    return _bare(GooseExecutor, _provider_env=dict)._build_spawn_env()


def _qwen_spawn_env():
    from omnigent.inner.qwen_executor import QwenExecutor

    async def _no_gateway():
        return {}

    return asyncio.run(_bare(QwenExecutor, _resolve_gateway_env=_no_gateway)._build_spawn_env())


def _kimi_spawn_env():
    from omnigent.inner.kimi_executor import KimiExecutor

    return _bare(KimiExecutor)._build_spawn_env()


def _hermes_spawn_env():
    from omnigent.inner.hermes_executor import HermesExecutor

    # The no-HERMES_HOME branch: the one that used to pass env=None and inherit
    # the entire host environment, so it is the branch worth pinning.
    return _bare(HermesExecutor, _hermes_home=None)._build_spawn_env()


def _pi_spawn_env():
    from omnigent.inner.pi_executor import _clean_pi_env

    return _clean_pi_env()


def _codex_spawn_env():
    from omnigent.inner.codex_executor import _clean_codex_env

    return _clean_codex_env()


SPAWN_ENV_BUILDERS = {
    "acp": _acp_spawn_env,
    "codex": _codex_spawn_env,
    "goose": _goose_spawn_env,
    "hermes": _hermes_spawn_env,
    "kimi": _kimi_spawn_env,
    "pi": _pi_spawn_env,
    "qwen": _qwen_spawn_env,
}


def test_every_harness_is_covered_by_a_real_builder():
    """A harness added to the prefix table must also be wired up above."""
    assert set(SPAWN_ENV_BUILDERS) == set(HARNESS_PREFIXES)


@pytest.mark.parametrize("harness", sorted(SPAWN_ENV_BUILDERS))
def test_real_builder_does_not_leak_host_secrets(harness, hostile_env, monkeypatch):
    """Drive the executor's own builder against a planted host environment."""
    monkeypatch.setattr("os.environ", dict(hostile_env))

    env = SPAWN_ENV_BUILDERS[harness]()

    leaked = {k: v for k, v in env.items() if k in CANARY_SECRETS}
    # qwen owns OPENAI_, so an OPENAI_ canary is its own family, not a leak.
    leaked = {k: v for k, v in leaked.items() if not k.startswith(HARNESS_PREFIXES[harness] or ())}
    assert not leaked, f"{harness}'s real spawn env leaked {sorted(leaked)}"


@pytest.mark.parametrize("harness", sorted(SPAWN_ENV_BUILDERS))
def test_real_builder_still_yields_a_usable_environment(harness, hostile_env, monkeypatch):
    monkeypatch.setattr("os.environ", dict(hostile_env))

    env = SPAWN_ENV_BUILDERS[harness]()

    assert env["HOME"] == "/home/user"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["HTTPS_PROXY"] == "http://proxy:8080"


def test_real_builders_pass_node_extra_ca_certs(monkeypatch):
    """A corporate CA must survive filtering, or every Node CLI breaks on TLS."""
    monkeypatch.setattr("os.environ", {"NODE_EXTRA_CA_CERTS": "/etc/corp-ca.pem"})
    for harness, build in sorted(SPAWN_ENV_BUILDERS.items()):
        env = build()
        assert env.get("NODE_EXTRA_CA_CERTS") == "/etc/corp-ca.pem", harness


def test_real_builders_pass_ssh_auth_sock(monkeypatch):
    """ssh-agent must survive filtering, or git-over-SSH breaks in every harness."""
    sock = "/private/tmp/com.apple.launchd.7Qk/Listeners"
    monkeypatch.setattr("os.environ", {"SSH_AUTH_SOCK": sock})
    for harness, build in sorted(SPAWN_ENV_BUILDERS.items()):
        assert build().get("SSH_AUTH_SOCK") == sock, harness


@pytest.mark.parametrize("harness", sorted(HARNESS_PREFIXES))
def test_no_harness_inherits_unrelated_secrets(harness, hostile_env):
    env = clean_agent_env(allow_prefixes=HARNESS_PREFIXES[harness], source=hostile_env)
    leaked = {k: v for k, v in env.items() if k in CANARY_SECRETS}
    assert not leaked, f"{harness} leaked {sorted(leaked)}"
    for value in CANARY_SECRETS.values():
        assert value not in env.values(), f"{harness} leaked the value {value!r}"


@pytest.mark.parametrize("harness", sorted(HARNESS_PREFIXES))
def test_every_harness_still_gets_a_usable_environment(harness, hostile_env):
    """Filtering must not break the CLI: HOME/PATH/locale/proxy still pass."""
    env = clean_agent_env(allow_prefixes=HARNESS_PREFIXES[harness], source=hostile_env)
    assert env["HOME"] == "/home/user"
    assert env["PATH"] == "/usr/bin:/bin"
    assert env["LANG"] == "en_US.UTF-8"
    assert env["HTTPS_PROXY"] == "http://proxy:8080"


def test_a_harness_still_sees_its_own_family(hostile_env):
    src = {**hostile_env, "QWEN_CODE_MODEL": "q", "GOOSE_PROVIDER": "g"}
    qwen = clean_agent_env(allow_prefixes=HARNESS_PREFIXES["qwen"], source=src)
    assert qwen["QWEN_CODE_MODEL"] == "q"
    assert "GOOSE_PROVIDER" not in qwen, "qwen must not see goose's family either"


def test_kimi_keeps_its_documented_ambient_auth(hostile_env):
    """kimi's docstring deliberately wants ambient KIMI_*; scoping must preserve it."""
    src = {**hostile_env, "KIMI_API_KEY": "kimi-real", "MOONSHOT_API_KEY": "ms-real"}
    env = clean_agent_env(allow_prefixes=HARNESS_PREFIXES["kimi"], source=src)
    assert env["KIMI_API_KEY"] == "kimi-real"
    assert env["MOONSHOT_API_KEY"] == "ms-real"
    assert "ANTHROPIC_API_KEY" not in env


def test_env_passthrough_is_the_documented_escape_hatch(hostile_env):
    """The migration for an env-authenticated generic ACP agent (e.g. gemini)."""
    without = clean_agent_env(source=hostile_env)
    assert "GEMINI_API_KEY" not in without
    with_pt = clean_agent_env(extra_allowed=("GEMINI_API_KEY",), source=hostile_env)
    assert with_pt["GEMINI_API_KEY"] == "canary-gemini"


def test_deny_exact_beats_a_matching_prefix(hostile_env):
    """codex strips OPENAI_API_KEY despite allowing the OPENAI_ prefix."""
    src = {**hostile_env, "OPENAI_API_KEY": "sk-dev", "OPENAI_BASE_URL": "https://x"}
    env = clean_agent_env(allow_prefixes=("OPENAI_",), deny_exact=("OPENAI_API_KEY",), source=src)
    assert "OPENAI_API_KEY" not in env
    assert env["OPENAI_BASE_URL"] == "https://x"


def test_source_is_never_mutated(hostile_env):
    before = dict(hostile_env)
    clean_agent_env(allow_prefixes=("QWEN_",), source=hostile_env)
    assert hostile_env == before


def test_base_does_not_include_identity_vars():
    """USER/LOGNAME/SHELL/TZ stay per-harness: pi passes them, codex does not,
    and the shared base must not silently widen codex's set."""
    for name in ("USER", "LOGNAME", "SHELL", "TZ"):
        assert name not in BASE_ALLOW_EXACT
    assert not any(p in BASE_ALLOW_PREFIXES for p in ("AWS_", "DATABRICKS_", "OPENAI_"))


def test_declared_passthrough_tolerates_a_missing_chain():
    class _NoSandbox:
        sandbox = None

    assert declared_passthrough(None) == ()
    assert declared_passthrough(_NoSandbox()) == ()


def test_acp_agent_declaration_passes_only_what_it_names(hostile_env, monkeypatch):
    """A generic-ACP agent's own ``env_passthrough`` is an allowlist, not a bypass.

    The executor cannot infer which family an arbitrary agent authenticates
    with, so the agent names its variables. Everything it does not name stays
    withheld — a declaration must not reopen the whole environment.
    """
    from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor

    monkeypatch.setattr("os.environ", {**hostile_env, "XAI_API_KEY": "declared-and-wanted"})
    ex = AcpExecutor(
        AcpAgentConfig(command="agent stdio", name="Grok", env_passthrough=("XAI_API_KEY",))
    )
    env = ex._build_spawn_env()

    assert env.get("XAI_API_KEY") == "declared-and-wanted"
    for name in CANARY_SECRETS:
        assert name not in env, name
