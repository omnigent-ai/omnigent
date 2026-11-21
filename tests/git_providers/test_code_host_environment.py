"""The pinned environment a code host instance's CLI runs in, and its refusal rules."""

from __future__ import annotations

import re

import pytest

from omnigent.git_providers.code_host_environment import (
    DEFAULT_PATH,
    TOKEN_VARIABLES,
    EnvironmentRuleError,
    check_environment,
    pinned_environment,
)

HOST_ENVIRONMENT = {
    "PATH": "/home/someone/bin:/usr/bin",
    "HOME": "/home/someone",
    "LANG": "en_US.UTF-8",
    "SHELL_HISTORY": "private",
    **{name: f"ambient-{name.lower()}" for name in TOKEN_VARIABLES},
}


def test_a_run_gets_only_the_pinned_path_passed_names_and_fixed_values() -> None:
    passed, fixed = check_environment(
        ["HOME", "NOT_SET_ON_HOST"], {"LANG": "C.UTF-8"}, instance_id="internal"
    )

    environment = pinned_environment(passed, fixed, HOST_ENVIRONMENT)

    assert environment == {"PATH": DEFAULT_PATH, "HOME": "/home/someone", "LANG": "C.UTF-8"}


def test_a_fixed_path_replaces_the_default() -> None:
    passed, fixed = check_environment([], {"PATH": "/opt/example/bin"}, instance_id="internal")

    assert pinned_environment(passed, fixed, HOST_ENVIRONMENT) == {"PATH": "/opt/example/bin"}


def test_token_variables_never_reach_a_run_whatever_is_passed_in() -> None:
    environment = pinned_environment(
        list(TOKEN_VARIABLES), dict.fromkeys(TOKEN_VARIABLES, "x"), HOST_ENVIRONMENT
    )

    assert environment == {"PATH": DEFAULT_PATH}


def test_no_config_passes_nothing_but_the_pinned_path() -> None:
    passed, fixed = check_environment(None, None, instance_id="internal")

    assert (passed, fixed) == ((), {})
    assert pinned_environment(passed, fixed, HOST_ENVIRONMENT) == {"PATH": DEFAULT_PATH}


@pytest.mark.parametrize("name", sorted(TOKEN_VARIABLES))
def test_a_token_variable_is_refused_by_its_rule(name: str) -> None:
    with pytest.raises(EnvironmentRuleError) as refused:
        check_environment([name], {}, instance_id="internal")

    assert str(refused.value) == (
        "instance internal env[0] is refused by the token-variable rule: GitHub CLI tokens "
        "never reach an instance's CLI"
    )


@pytest.mark.parametrize(
    ("name", "part"),
    [
        ("SERVICE_API_KEY", "KEY"),
        ("KEYBOARD_LAYOUT", "KEY"),
        ("session_token", "TOKEN"),
        ("CLIENT_SECRET", "SECRET"),
        ("DB_PASSWORD", "PASSWORD"),
    ],
)
def test_a_secret_shaped_name_is_refused_naming_the_rule_so_it_can_be_renamed(
    name: str, part: str
) -> None:
    with pytest.raises(EnvironmentRuleError) as refused:
        check_environment(["HOME", name], {}, instance_id="internal")

    assert str(refused.value) == (
        f"instance internal env[1] is refused by the secret-name rule: names containing "
        f"{part} are treated as secrets. Rename the variable if it doesn't hold one"
    )


def test_a_fixed_value_with_a_secret_shaped_name_is_refused_without_its_value() -> None:
    with pytest.raises(EnvironmentRuleError) as refused:
        check_environment([], {"LANG": "C", "PROJECT_KEY": "value-123"}, instance_id="internal")

    assert "env_set entry 2 is refused by the secret-name rule" in str(refused.value)
    assert "value-123" not in str(refused.value)
    assert "PROJECT_KEY" not in str(refused.value)


@pytest.mark.parametrize(
    ("passed", "fixed", "rule"),
    [
        (["GH_HOST"], {}, "env[0] is refused by the CLI-settings rule"),
        ([], {"GITHUB_API_URL": "x"}, "env_set entry 1 is refused by the CLI-settings rule"),
        (["PATH"], {}, "env[0] is refused by the pinned-PATH rule: set PATH in env_set instead"),
        (["HOME", "HOME"], {}, "env[1] is refused: it is listed twice"),
        (["HOME"], {"HOME": "/x"}, "env_set entry 1 is refused: it is also passed through"),
        (["1BAD"], {}, "env[0] is refused: a variable name must be an identifier"),
        (["A=B"], {}, "env[0] is refused: a variable name must be an identifier"),
        ([], {"LANG": 1}, "env_set entry 1 is refused: its value must be a string"),
        ([], {"LANG": "a\0b"}, "env_set entry 1 is refused: its value must be a string"),
        ("HOME", {}, "env must be a list of names"),
        ([], ["LANG"], "env_set must be a table"),
    ],
)
def test_other_environment_rules(passed: object, fixed: object, rule: str) -> None:
    with pytest.raises(EnvironmentRuleError, match=re.escape(rule)):
        check_environment(passed, fixed, instance_id="internal")
