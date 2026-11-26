"""Supported GitLab activities and approval boundaries, through the public policy."""

from __future__ import annotations

import pytest

from omnigent.policies.builtins._shell import SHELL_TOOLS
from omnigent.policies.builtins.gitlab import gitlab_policy
from omnigent.policies.function import resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyResponse
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from tests.policies.builtins.helpers import tool_call_event

_HANDLER = "omnigent.policies.builtins.gitlab.gitlab_policy"
_PROJECT = "team/sub/app"
_URL = f"https://gitlab.com/{_PROJECT}"
_COMMAND_PREFIXES = ("", "/usr/bin/", "./bin/", "command /usr/bin/", "env ")


def _response(command: str, **configuration: object) -> PolicyResponse | None:
    policy = gitlab_policy(**configuration)  # type: ignore[arg-type]
    return policy(tool_call_event("sys_os_shell", {"command": command}))


def _result(command: str, **configuration: object) -> str:
    response = _response(command, **configuration)
    return response["result"] if response else "ALLOW"


@pytest.mark.parametrize(
    ("action", "write"),
    [
        ("issue list", False),
        ("issue ls", False),
        (
            "issue list -Ac -a example --author example -l bug -m v1 "
            "-p 2 -P 5 -O json --search Fix",
            False,
        ),
        ("issue view 12", False),
        ("issue show 12", False),
        ("issue view 12 -cs -p 2 -P 5 -F json", False),
        ("mr list", False),
        ("mr ls", False),
        ("mr list -AcM -a example --author example -l bug -p 2 -P 5 -F json --search Fix", False),
        ("mr view 12", False),
        ("mr show 12", False),
        ("mr view 12 -cs -p 2 -P 5 -F json", False),
        ("mr diff 12", False),
        ("mr diff 12 --color=never --raw", False),
        ("issue create -t Fix -d Details -y", True),
        ("issue new -t Fix -d Details", True),
        ("issue note 12 -m Details", True),
        ("issue comment 12 -m Details", True),
    ],
)
def test_supported_actions_enforce_project_permissions(action: str, write: bool) -> None:
    configuration = {"read_all": False, "read_repos": [_PROJECT], "write_repos": [_PROJECT]}
    assert _result(f"glab {action} -R {_URL}", **configuration) == "ALLOW"
    assert _result(f"glab {action} -R https://gitlab.com/team/other", **configuration) == "DENY"
    assert _result(f"glab {action} -R https://gitlab.com/team/other") == (
        "DENY" if write else "ALLOW"
    )
    assert _result(f"glab {action}", **configuration) == "ASK"
    assert _result(f"glab {action} -R {_PROJECT}", **configuration) == "ASK"


@pytest.mark.parametrize(
    "selector",
    [_URL, _URL + ".git", "gitlab.com/TEAM/Sub/App", "https://GITLAB.COM/team/sub/app"],
)
def test_explicit_project_selector(selector: str) -> None:
    assert (
        _result(f"glab issue note 12 -m Details -R {selector}", write_repos=[_PROJECT]) == "ALLOW"
    )


@pytest.mark.parametrize(
    "selector",
    [
        "https://gitlab.other.example/team/sub/app",
        "https://gitlab.com.example.net/team/sub/app",
        "https://gitlab.com/team/sub/app/../other",
        "https://gitlab.com/team/sub/app/-/issues/12",
        "https://gitlab.com/team/sub/app?project=other",
        "https://user@gitlab.com/team/sub/app",
        "git@gitlab.com:team/sub/app.git",
        "http://gitlab.com/team/sub/app",
    ],
)
def test_uncertain_project_identity_requires_approval(selector: str) -> None:
    response = _response(f'glab issue list -R "{selector}"')
    assert response is not None and response["result"] == "ASK"
    assert "could not be established" in response["reason"]


def test_projects_can_be_pinned_to_a_server() -> None:
    configuration = {
        "hosts": ["gitlab.com", "gitlab.example.com:8443"],
        "write_repos": ["https://gitlab.example.com:8443/team/sub/app"],
    }
    assert (
        _result(
            "glab issue note 12 -m Details -R https://gitlab.example.com:8443/team/sub/app",
            **configuration,
        )
        == "ALLOW"
    )
    assert _result(f"glab issue note 12 -m Details -R {_URL}", **configuration) == "DENY"
    configuration["write_repos"] = [_PROJECT]
    assert _result(f"glab issue note 12 -m Details -R {_URL}", **configuration) == "ALLOW"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ("-t Fix -d Details", "ALLOW"),
        ("--title=Fix --description=Details", "ALLOW"),
        ("-ytFix -dDetails", "ALLOW"),
        ('-t "--help" -d Details', "ALLOW"),
        ('-t "-R https://gitlab.com/team/other" -d Details', "ALLOW"),
        ("-t Fix -d 'Cost is $5; use * literally'", "ASK"),
        ("-t Fix -d Details --confidential=false", "ALLOW"),
        ("-t Fix -d Details --help=false", "ALLOW"),
        ("-t Fix -d Details -- --extra", "ASK"),
        ("-t Fix -d Details --recover", "ASK"),
        ("-t Fix -d Details --group other", "ASK"),
        ("-t Fix -d Details --hostname gitlab.com", "ASK"),
        ("-t Fix -d Details --future-option", "ASK"),
        ("-t Fix -d Details --description-file body.md", "ASK"),
        ("-t Fix -d Details --attach image.png", "ASK"),
        ("-t Fix -d Details --linked-mr 12", "ASK"),
        ("-t Fix -d Details --repo", "ASK"),
        ("-t Fix -d Details -R https://gitlab.com/team/other", "ASK"),
        ("-t Fix -d Details -Rhttps://gitlab.com/team/other", "ASK"),
        ("-t Fix -d Details --confidential=invalid", "ASK"),
        ("-t Fix", "ASK"),
        ("-t Fix -d -", "ASK"),
        ("-t Fix -d", "ASK"),
        ("-t $TITLE -d Details", "ASK"),
        ('-t "$TITLE" -d Details', "ASK"),
        ("-t * -d Details", "ASK"),
        (f"glab issue create -t Fix -d > -R {_URL}", "ASK"),
        (f"glab issue note 12 -m > -R {_URL}", "ASK"),
        (f"glab issue note 12 -m >-R {_URL}", "ASK"),
        (f'"glab" issue note 12 -m > -R {_URL}', "ASK"),
        (f"glab issue note 12 -m hi\r-R\r{_URL}", "ASK"),
        (f"glab issue create -t Fix -d b\r-R\r{_URL}", "ASK"),
        (f"glab issue note 12 -m Details -R {_URL}\r", "ASK"),
        ("-t Fix -d >> -R https://gitlab.com/team/sub/app", "ASK"),
        ("-t Fix -d < -R https://gitlab.com/team/sub/app", "ASK"),
        ("-t Fix -d 2> -R https://gitlab.com/team/sub/app", "ASK"),
        ("-t Fix -d 'literal <input> and > output'", "ASK"),
        ('-t Fix -d "literal <input> and > output"', "ALLOW"),
        (r"-t Fix -d literal\<input\>", "ASK"),
    ],
)
def test_option_values_remain_attached(arguments: str, expected: str) -> None:
    command = (
        f"glab issue create -R {_URL} {arguments}" if arguments.startswith("-") else arguments
    )
    assert _result(command, write_repos=[_PROJECT]) == expected


@pytest.mark.parametrize(
    "command",
    [
        "glab issue note 12",
        "glab issue note 12 -m -",
        "glab issue note https://gitlab.com/team/other/-/issues/12 -m Details",
        "glab issue view https://gitlab.com/team/other/-/issues/12",
        "glab mr view feature",
        "glab mr view",
        "glab mr diff 12 extra",
        "glab issue list --group team/other",
        "glab issue list --future-option value",
        "glab issue create unexpected -t Fix -d Details",
        "glab issue note 12 note delete -m Details",
    ],
)
def test_unmodelled_operands_and_interactive_writes_ask(command: str) -> None:
    assert _result(f"{command} -R {_URL}", write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize(
    ("command", "reason"),
    [
        (f'glab issue create -R {_URL} -t Fix -d ""', "explicit issue text"),
        (f'glab issue note 12 -R {_URL} -m ""', "explicit issue text"),
        (
            f'bash -c -- "glab issue create -R {_URL} -t Fix -d Details"',
            "after the command-string flag",
        ),
        (
            f'bash -c -e "glab issue create -R {_URL} -t Fix -d Details"',
            "after the command-string flag",
        ),
        (f'sh -c +x "glab mr delete 12 -R {_URL}"', "after the command-string flag"),
        (f'bash -c -o errexit "glab mr delete 12 -R {_URL}"', "after the command-string flag"),
        (f'bash -lc -- "glab mr delete 12 -R {_URL}"', "after the command-string flag"),
    ],
)
def test_approval_reason_identifies_the_unresolved_boundary(command: str, reason: str) -> None:
    response = _response(command, write_repos=[_PROJECT])
    assert response is not None and response["result"] == "ASK"
    assert reason in response["reason"]


@pytest.mark.parametrize(
    "command",
    [
        "glab mr create -b main -t Fix -d Details",
        "glab mr merge 12 --remove-source-branch",
        "glab mr accept 12",
        "glab mr rebase 12",
        "glab mr update 12 --target-branch main",
        "glab repo fork",
        "glab ci run",
        "glab variable set KEY value",
        "glab api projects/123/issues",
        "glab api -X POST projects/123/issues -f title=Fix",
        "glab api -X DELETE projects/123/repository/branches/release -f branch=main",
        "glab config set api_host gitlab.other.example",
        'glab alias set write "api -X POST"',
        "glab auth login",
        "glab unknown run",
        "glab --repo https://gitlab.com/team/sub/app issue list",
    ],
)
def test_deferred_glab_operations_still_require_approval(command: str) -> None:
    assert _result(f"{command} -R {_URL}", write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize(
    "command",
    [
        f"git clone {_URL}",
        f"git fetch --future-option {_URL}",
        f"git push {_URL} main",
        "git config push.followTags true",
        "git remote set-url origin https://gitlab.com/team/other",
        "git custom-alias",
        "git -c alias.upload=push upload",
    ],
)
def test_git_remote_and_configuration_handling_is_deferred(command: str) -> None:
    assert _result(command, write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize("action", ["issue delete", "mr del", "repo rm", "project delete"])
def test_known_deletions_are_denied(action: str) -> None:
    assert _result(f"glab {action} 12 -R {_URL}", write_repos=[_PROJECT]) == "DENY"


@pytest.mark.parametrize(
    ("prefix", "spelling"),
    [("", name) for name in ("glab", "GLAB", "GlAb", "glab.exe", "GLAB.EXE", "GlAb.ExE")]
    + [(prefix, "glab") for prefix in _COMMAND_PREFIXES[1:]]
    + [("command /usr/bin/", "GlAb.ExE")],
)
def test_executable_paths_and_wrappers(prefix: str, spelling: str) -> None:
    assert _result(f"{prefix}{spelling} issue view 12 -R {_URL}") == "ALLOW"
    assert _result(f"{prefix}{spelling} issue create -R {_URL} -t Fix -d Details") == "DENY"
    assert _result(f"{prefix}{spelling} mr delete 12 -R {_URL}") == "DENY"


@pytest.mark.parametrize(
    ("prefix", "spelling"),
    [("", name) for name in ("git", "GIT", "git.exe", "GIT.EXE")]
    + [(prefix, "git") for prefix in _COMMAND_PREFIXES[1:]]
    + [("command /usr/bin/", "GIT.EXE")],
)
def test_git_executable_paths_and_wrappers(prefix: str, spelling: str) -> None:
    assert _result(f"{prefix}{spelling} push {_URL} main") == "ASK"


@pytest.mark.parametrize(
    "executable",
    [
        r"C:\tools\glab.exe",
        r"c:\tools\GLAB.EXE",
        r'"C:\Program Files\glab.exe"',
        r"'C:\tools\glab.exe'",
        r"\\server\tools\glab.exe",
        r"\tools\glab.exe",
        r".\tools\glab.exe",
        r"tools\glab.exe",
        r"env C:\tools\glab.exe",
        r"cmd /c C:\tools\glab.exe",
        r"bash -c 'C:\tools\glab.exe issue list'",
    ],
)
def test_backslash_paths_require_approval_before_posix_parsing(executable: str) -> None:
    assert _result(f"{executable} issue create -R {_URL} -t Fix -d Details") == "ASK"
    assert _result(f"{executable} issue delete 12 -R {_URL}") == "ASK"


@pytest.mark.parametrize(
    "command",
    [
        "glab config set api_host gitlab.other.example",
        "export GITLAB_HOST=gitlab.other.example",
        "GLAB_CONFIG_DIR=/tmp/config",
        "export GIT_CONFIG_COUNT=1",
        'alias glab="another-command"',
        "GITLAB_HOST=gitlab.other.example glab issue list",
        "python -m glab",
        "python -m GLAB",
        "xargs glab issue list",
        "custom-wrapper glab issue list",
    ],
)
def test_cli_redirection_and_unresolved_invocations_ask(command: str) -> None:
    assert _result(f"{command} && glab issue list -R {_URL}") == "ASK"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f'bash -c "glab issue list -R {_URL}"', "ALLOW"),
        (f'env -S "glab issue list -R {_URL}"', "ALLOW"),
        (f"! glab issue create -R {_URL} -t Fix -d Details", "DENY"),
        (f"! env glab issue list -R {_URL}", "ALLOW"),
        (f'! bash -c "glab mr delete 12 -R {_URL}"', "DENY"),
        (f"! env glab mr del 12 -R {_URL}", "DENY"),
        (f"! ! glab repo rm 12 -R {_URL}", "DENY"),
        (f"gl^ab issue create -R {_URL} -t Fix -d Details", "ASK"),
        (f"gl%EXAMPLE%ab issue list -R {_URL}", "ASK"),
        (f"gl!EXAMPLE!ab issue list -R {_URL}", "ASK"),
        (f"glab issue note 12 -R {_URL} -m 'text & glab issue delete 12'", "ASK"),
        (f"glab issue note 12 -R {_URL} -m 'text -R https://gitlab.com/team/other'", "ASK"),
        (f"glab issue note 12 -R {_URL} -m ‘text’", "ASK"),
        (f"glab issue note 12 -R {_URL} -m “text”", "ASK"),
        (f"bash -c 'glab issue list -R {_URL}' > output.txt", "ASK"),
        (f"bash -c 'glab issue note 12 -m > -R {_URL}'", "ASK"),
        (f"echo ready && glab mr del 12 -R {_URL}", "DENY"),
        (f"glab api projects/123 && glab mr del 12 -R {_URL}", "DENY"),
        (f"echo $(date) && glab mr del 12 -R {_URL}", "DENY"),
        (f"echo $(date) && glab issue list -R {_URL}", "ASK"),
        (f"eval eval eval eval eval glab issue list -R {_URL}", "ASK"),
        ("sh script.sh", "ASK"),
        ("git add . && git commit -m Fix && git status", "ALLOW"),
        ("echo ready", "ALLOW"),
        ("echo ready > output.txt", "ASK"),
        ('echo "$COMMAND"', "ASK"),
        (f"glab mr delete 12 -R {_URL}; echo '\r'", "DENY"),
        (rf"C:\tools\glab.exe issue list && glab mr delete 12 -R {_URL}", "DENY"),
        (f"glab issue\tlist -R {_URL}", "ALLOW"),
    ],
)
def test_shell_boundaries(command: str, expected: str) -> None:
    assert _result(command) == expected


@pytest.mark.parametrize("tool", sorted(SHELL_TOOLS))
def test_all_harness_shell_tools_are_checked(tool: str) -> None:
    policy = gitlab_policy()
    response = policy(
        tool_call_event(tool, {"command": f"glab issue create -R {_URL} -t Fix -d Details"})
    )
    assert response is not None and response["result"] == "DENY"


def test_event_selection_and_custom_denial_reason() -> None:
    policy = gitlab_policy(shell_tools=["custom_shell"], deny_reason="Project restricted.")
    arguments = {"command": f"glab issue create -R {_URL} -t Fix -d Details"}
    assert policy(tool_call_event("sys_os_shell", arguments)) is None
    response: PolicyResponse | None = policy(tool_call_event("custom_shell", arguments))
    assert response is not None and response["reason"].startswith("Project restricted.")
    assert policy(tool_call_event("gitlab_mcp_write", arguments)) is None
    assert policy({"type": "tool_result", "data": {}}) is None
    assert policy(tool_call_event("custom_shell", {"command": None})) is None


@pytest.mark.parametrize(
    "configuration",
    [
        {"hosts": []},
        {"hosts": ["http://gitlab.example.com"]},
        {"hosts": ["gitlab.com", "invalid host"]},
        {"write_repos": ["https://gitlab.other.example/team/app"]},
        {"read_repos": ["team/../app"]},
        {"read_repos": ["https://gitlab.com/team%2Fsub%2Fapp"]},
    ],
)
def test_invalid_configuration_fails(configuration: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        gitlab_policy(**configuration)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_policy_resolution_preserves_allow_deny_and_ask() -> None:
    policy = resolve_function_policy(
        FunctionPolicySpec(
            name="gitlab",
            on=None,
            function=FunctionRef(path=_HANDLER, arguments={"write_repos": [_PROJECT]}),
        )
    )
    for command, expected in [
        (f"glab issue create -R {_URL} -t Fix -d Details", PolicyAction.ALLOW),
        (
            "glab issue create -R https://gitlab.com/team/other -t Fix -d Details",
            PolicyAction.DENY,
        ),
        (f"git push {_URL} main", PolicyAction.ASK),
    ]:
        result = await policy.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name="sys_os_shell",
                content={"name": "sys_os_shell", "arguments": {"command": command}},
            ),
            {},
        )
        assert result.action == expected


def test_registry_exposes_only_initial_scope_settings() -> None:
    load_registry()
    assert any(entry.handler == _HANDLER for entry in get_registry())
    assert (
        validate_factory_params(
            _HANDLER,
            {
                "hosts": ["gitlab.example.com"],
                "read_all": False,
                "read_repos": [_PROJECT],
                "write_repos": [_PROJECT],
                "shell_tools": ["sys_os_shell"],
                "deny_reason": "Project restricted.",
            },
        )
        is None
    )
    for configuration in (
        {"write_branches": ["main"]},
        {"allow_destructive": True},
        {"read_all": "yes"},
        {"deny_reason": 123},
    ):
        assert validate_factory_params(_HANDLER, configuration) is not None
