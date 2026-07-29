"""No agent-CLI subprocess may inherit unrelated host secrets (#3445).

The pi and codex executors filtered; goose, kimi, qwen, acp and hermes did not,
and hermes's no-HERMES_HOME branch passed ``env=None`` (inherit everything).

This is the parametrized canary the maintainer asked for. The point is not to
re-test each executor's wiring — it is that the *next* harness added to this
repo fails here if it forgets to filter. The fix pattern already existed twice
and still missed five siblings.
"""

from __future__ import annotations

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
