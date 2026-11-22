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


def test_diagnostic_variables_never_reach_a_run_whatever_is_passed_in() -> None:
    names = ["GH_DEBUG", "HTTP_DEBUG", "GIT_TRACE_CURL", "GIT_CURL_VERBOSE", "GODEBUG"]
    host = {"HTTP_DEBUG": "1", "GODEBUG": "netdns=go,http2debug=2"}

    environment = pinned_environment(names, dict.fromkeys(names[:-1], "1"), host)

    assert environment == {"PATH": DEFAULT_PATH}


def test_settings_that_print_nothing_still_reach_a_run() -> None:
    passed, fixed = check_environment(
        ["GODEBUG", "GIT_CURL_FTP_NO_EPSV"], {"BUILD_VERBOSE": "1"}, instance_id="internal"
    )
    host = {"GODEBUG": "netdns=cgo", "GIT_CURL_FTP_NO_EPSV": "1"}

    assert pinned_environment(passed, fixed, host) == {
        "PATH": DEFAULT_PATH,
        "GODEBUG": "netdns=cgo",
        "GIT_CURL_FTP_NO_EPSV": "1",
        "BUILD_VERBOSE": "1",
    }


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
    ("name", "rule"),
    [
        ("GH_DEBUG", "GH_DEBUG"),
        ("DEBUG", "DEBUG"),
        ("debug", "DEBUG"),
        ("DEBUG_HTTP", "DEBUG_HTTP"),
        ("HTTP_DEBUG", "HTTP_DEBUG"),
        ("GIT_CURL_VERBOSE", "GIT_CURL_VERBOSE"),
        ("GIT_TRACE", "a GIT_TRACE setting"),
        ("GIT_TRACE_CURL", "a GIT_TRACE setting"),
        ("GIT_TRACE2_EVENT", "a GIT_TRACE setting"),
        ("git_tracer", "a GIT_TRACE setting"),
    ],
)
@pytest.mark.parametrize("field", ["env", "env_set"])
def test_a_diagnostic_variable_is_refused_naming_the_rule(
    name: str, rule: str, field: str
) -> None:
    passed, fixed = ([name], {}) if field == "env" else ([], {name: "1"})
    where = "env[0]" if field == "env" else "env_set entry 1"

    with pytest.raises(EnvironmentRuleError) as refused:
        check_environment(passed, fixed, instance_id="internal")

    assert str(refused.value) == (
        f"instance internal {where} is refused by the diagnostic-variable rule: {rule} turns "
        "on diagnostic output, which can print credentials"
    )


@pytest.mark.parametrize("value", ["http2debug=1", "netdns=go,HTTP2DEBUG=2"])
def test_a_fixed_godebug_that_logs_http2_is_refused_without_its_value(value: str) -> None:
    with pytest.raises(EnvironmentRuleError) as refused:
        check_environment([], {"GODEBUG": value}, instance_id="internal")

    assert "diagnostic-variable rule: GODEBUG with http2debug turns on" in str(refused.value)
    assert value not in str(refused.value)


@pytest.mark.parametrize(
    "name", ["GIT_CURL_FTP_NO_EPSV", "BUILD_VERBOSE", "GODEBUG", "TRACE_ID", "GIT_TERMINAL_PROMPT"]
)
@pytest.mark.parametrize("field", ["env", "env_set"])
def test_a_variable_outside_the_diagnostic_list_is_accepted(name: str, field: str) -> None:
    passed, fixed = ([name], {}) if field == "env" else ([], {name: "netdns=cgo"})

    assert check_environment(passed, fixed, instance_id="internal") == (tuple(passed), fixed)


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
