"""Tests for launch-time ``os_env.sandbox.env_template`` resolution."""

from __future__ import annotations

import logging

import pytest

from omnigent.inner.agent_env import clean_agent_env, resolve_agent_env_template
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.env_template import (
    RESOLVED_TEMPLATE_NAMES_ENV_VAR,
    RUNNER_WORKSPACE_TEMPLATE_VAR,
    SESSION_WORKSPACE_TEMPLATE_VAR,
    resolve_env_template,
    validate_env_template,
    workspace_template_context,
)
from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR


def _allow(*names: str):
    allowed = set(names)
    return lambda name: name in allowed


def test_workspace_context_session_falls_back_to_runner() -> None:
    assert workspace_template_context(session_workspace=None, runner_workspace="/repo") == {
        RUNNER_WORKSPACE_TEMPLATE_VAR: "/repo",
        SESSION_WORKSPACE_TEMPLATE_VAR: "/repo",
    }
    assert workspace_template_context(session_workspace="/wt/a", runner_workspace="/repo") == {
        RUNNER_WORKSPACE_TEMPLATE_VAR: "/repo",
        SESSION_WORKSPACE_TEMPLATE_VAR: "/wt/a",
    }
    assert workspace_template_context(session_workspace=None, runner_workspace=None) == {}


def test_resolve_env_template_context_and_allowed_references() -> None:
    template = {
        "EDATA_OUTPUT_DIR": "${OMNIGENT_SESSION_WORKSPACE}/data",
        "CACHE_DIR": "$HOME/.cache/${TOOL_NAME}",
        "PRICE": "$$5",
    }
    resolved = resolve_env_template(
        template,
        source_env={"HOME": "/home/me", "TOOL_NAME": "edata"},
        is_allowed=_allow("HOME", "TOOL_NAME"),
        context=workspace_template_context(session_workspace="/wt/a", runner_workspace="/repo"),
    )
    assert resolved == {
        "EDATA_OUTPUT_DIR": "/wt/a/data",
        "CACHE_DIR": "/home/me/.cache/edata",
        "PRICE": "$5",
    }


@pytest.mark.parametrize(
    ("value", "source_env", "allowed"),
    [
        # Hidden from the sandbox: a template must not reveal it under a new name.
        ("${AWS_SECRET_ACCESS_KEY}", {"AWS_SECRET_ACCESS_KEY": "secret"}, ()),
        # Runner-auth secrets never resolve, even when the filter would allow them.
        (
            f"${{{RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR}}}",
            {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "token"},
            (RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,),
        ),
        # Unset and empty references drop the entry instead of writing "/data".
        ("${UNSET_VAR}/data", {}, ("UNSET_VAR",)),
        ("${EMPTY_VAR}/data", {"EMPTY_VAR": ""}, ("EMPTY_VAR",)),
        ("${OMNIGENT_SESSION_WORKSPACE}/data", {}, ()),
    ],
)
def test_resolve_env_template_drops_unresolvable_entries(
    value: str, source_env: dict[str, str], allowed: tuple[str, ...]
) -> None:
    resolved = resolve_env_template(
        {"LEAK": value, "KEPT": "fixed"},
        source_env=source_env,
        is_allowed=_allow(*allowed),
        context={},
    )
    assert resolved == {"KEPT": "fixed"}


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (["A"], "must be a mapping"),
        ({"1BAD": "x"}, "POSIX"),
        ({"A": 1}, "must be a string"),
        ({"A": "${UNCLOSED"}, "malformed"),
        ({"PATH": "/bin"}, "reserved"),
        ({"OMNIGENT_SESSION_WORKSPACE": "/x"}, "reserved"),
        ({"HARNESS_CODEX_CWD": "/x"}, "reserved"),
    ],
)
def test_validate_env_template_rejects_invalid(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_env_template(raw)


def test_resolve_agent_env_template_uses_cli_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Passthrough-listed names resolve; hidden names do not."""
    os_env = OSEnvSpec(
        sandbox=OSEnvSandboxSpec(
            env_passthrough=["EDATA_ROOT"],
            env_template={
                "EDATA_OUTPUT_DIR": "${OMNIGENT_SESSION_WORKSPACE}/data",
                "EDATA_SHARED": "${EDATA_ROOT}/shared",
                "LEAK": "${AWS_SECRET_ACCESS_KEY}",
            },
        )
    )
    source = {
        RUNNER_WORKSPACE_TEMPLATE_VAR: "/repo",
        "EDATA_ROOT": "/srv/edata",
        "AWS_SECRET_ACCESS_KEY": "secret",
    }

    resolved = resolve_agent_env_template(
        os_env, session_workspace="/wt/a", for_harness_child=True, source=source
    )

    assert resolved == {
        "EDATA_OUTPUT_DIR": "/wt/a/data",
        "EDATA_SHARED": "/srv/edata/shared",
        RESOLVED_TEMPLATE_NAMES_ENV_VAR: "EDATA_OUTPUT_DIR,EDATA_SHARED",
        SESSION_WORKSPACE_TEMPLATE_VAR: "/wt/a",
    }
    assert resolve_agent_env_template(OSEnvSpec(), session_workspace="/wt/a", source=source) == {}


def test_clean_agent_env_admits_only_names_the_runner_resolved() -> None:
    """Resolved templates reach the CLI without ``env_passthrough``; others stay hidden."""
    source = {
        "EDATA_OUTPUT_DIR": "/wt/a/data",
        "UNRESOLVED": "runner-value",
        RESOLVED_TEMPLATE_NAMES_ENV_VAR: "EDATA_OUTPUT_DIR",
    }

    env = clean_agent_env(source=source)

    assert env["EDATA_OUTPUT_DIR"] == "/wt/a/data"
    assert "UNRESOLVED" not in env
    assert RESOLVED_TEMPLATE_NAMES_ENV_VAR not in env


def test_resolve_env_template_warns_once_per_dropped_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    template = {"WARN_ONCE": "${WARN_ONCE_MISSING}"}
    with caplog.at_level(logging.DEBUG, logger="omnigent.inner.env_template"):
        for _ in range(3):
            resolve_env_template(template, source_env={}, is_allowed=_allow(), context={})

    levels = [record.levelno for record in caplog.records if "WARN_ONCE" in record.message]
    assert levels == [logging.WARNING, logging.DEBUG, logging.DEBUG]
