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
        "glab mr accept 12",
        "glab mr rebase 12",
        "glab mr update --target-branch main",
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
        f"git fetch --future-option {_URL}",
        f"git push {_URL} main",
        "git config push.followTags true",
        "git remote set-url origin https://gitlab.com/team/other",
        "git custom-alias",
        "git -c alias.upload=push upload",
    ],
)
def test_unresolved_git_and_configuration_still_require_approval(command: str) -> None:
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
    command = f"{prefix}{spelling} push {_URL} HEAD:refs/heads/feature"
    assert _result(command, write_repos=[_PROJECT]) == "ALLOW"
    assert _result(command) == "DENY"


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
        "export GIT_SSH_COMMAND=example",
        "GIT_NAMESPACE=example git fetch https://gitlab.com/team/sub/app",
        "GIT_AUTHOR_NAME=example git commit -m Fix",
        "export GIT_CONFIG_COUNT",
        "printf -v GIT_CONFIG_COUNT 1",
        "printf -vGIT_CONFIG_COUNT 1",
        "command printf -v GIT_CONFIG_COUNT 1",
        "declare -x GIT_CONFIG_COUNT",
        "declare -n example=GIT_CONFIG_COUNT",
        "unset GIT_SSH_COMMAND",
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


def test_registry_exposes_transfer_settings() -> None:
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
                "write_branches": ["feature"],
                "allow_destructive": False,
                "deny_force_push": True,
                "deny_tag_push": True,
                "shell_tools": ["sys_os_shell"],
                "deny_reason": "Project restricted.",
            },
        )
        is None
    )
    for configuration in (
        {"write_branches": "main"},
        {"allow_destructive": "yes"},
        {"deny_force_push": 1},
        {"deny_tag_push": "false"},
        {"read_all": "yes"},
        {"deny_reason": 123},
    ):
        assert validate_factory_params(_HANDLER, configuration) is not None


@pytest.mark.parametrize(
    "arguments",
    [
        f"clone {_URL}.git",
        f"clone -qvbmain --depth=1 {_URL} checkout",
        f"clone --bare -- {_URL}",
        f"fetch {_URL}",
        f"fetch -qv --depth 1 --no-recurse-submodules {_URL} main",
        f"fetch -- {_URL} refs/heads/main:refs/remotes/example/main",
        f"pull --ff-only --no-recurse-submodules {_URL} main",
        f"pull --no-rebase --no-edit {_URL} refs/heads/main",
    ],
)
def test_explicit_git_downloads_check_read_permissions(arguments: str) -> None:
    assert _result(f"git {arguments}") == "ALLOW"
    assert _result(f"git {arguments}", read_all=False) == "DENY"
    assert _result(f"git {arguments}", read_all=False, read_repos=[_PROJECT]) == "ALLOW"


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (f"{_URL} HEAD:refs/heads/feature", "ALLOW"),
        (f"{_URL} refs/heads/feature", "ALLOW"),
        (f"-qvu --atomic --no-recurse-submodules {_URL} main:refs/heads/feature", "ALLOW"),
        (f"--signed=no {_URL} HEAD:refs/heads/feature", "ALLOW"),
        (f"--repo={_URL} HEAD:refs/heads/feature", "ASK"),
        (f"{_URL} HEAD:refs/heads/main", "DENY"),
        (f"{_URL} HEAD:refs/heads/Feature", "DENY"),
        (f"{_URL} HEAD:refs/heads/heads/feature", "DENY"),
        (f"{_URL} HEAD:refs/heads/feature main:refs/heads/main", "DENY"),
        ("https://gitlab.com/team/other HEAD:refs/heads/feature", "DENY"),
        (f"{_URL} main", "ASK"),
        (f"{_URL} HEAD:feature", "ASK"),
        (f"{_URL} HEAD:heads/feature", "ASK"),
        (f"{_URL}", "ASK"),
        ("origin HEAD:refs/heads/feature", "ASK"),
        (f"{_URL} :", "ASK"),
        (f"{_URL} HEAD:refs/heads/../feature", "ASK"),
        (f"{_URL} HEAD:refs/heads/.hidden", "ASK"),
        (f"{_URL} HEAD:refs/heads/feature.lock", "ASK"),
    ],
)
def test_push_checks_every_exact_destination(arguments: str, expected: str) -> None:
    assert (
        _result(f"git push {arguments}", write_repos=[_PROJECT], write_branches=["feature"])
        == expected
    )


@pytest.mark.parametrize(
    ("arguments", "setting", "permissive"),
    [
        (f"-f {_URL} HEAD:refs/heads/feature", "deny_force_push", "ALLOW"),
        (f"-uf {_URL} HEAD:refs/heads/feature", "deny_force_push", "ALLOW"),
        (
            f"--force-with-lease=refs/heads/feature: {_URL} HEAD:refs/heads/feature",
            "deny_force_push",
            "ALLOW",
        ),
        (f"--force-if-includes {_URL} HEAD:refs/heads/feature", "deny_force_push", "ALLOW"),
        (f"{_URL} +HEAD:refs/heads/feature", "deny_force_push", "ALLOW"),
        ("--force", "deny_force_push", "ASK"),
        ("--force-with-lease", "deny_force_push", "ASK"),
        ("--tags", "deny_tag_push", "ASK"),
        ("--delete", "allow_destructive", "ASK"),
        (f"{_URL} refs/tags/v1", "deny_tag_push", "ALLOW"),
        (f"{_URL} HEAD:refs/tags/v1", "deny_tag_push", "ALLOW"),
        (f"{_URL} tag v1", "deny_tag_push", "ASK"),
        (f"{_URL} HEAD:refs/heads/feature tag v1", "deny_tag_push", "ASK"),
        (f"--tags {_URL}", "deny_tag_push", "ASK"),
        (f"--follow-tags {_URL} HEAD:refs/heads/feature", "deny_tag_push", "ASK"),
        (f"--delete {_URL} refs/heads/feature", "allow_destructive", "ALLOW"),
        (f"{_URL} :refs/heads/feature", "allow_destructive", "ALLOW"),
        (f"--prune {_URL} HEAD:refs/heads/feature", "allow_destructive", "ASK"),
    ],
)
def test_push_guards_require_explicit_opt_in(
    arguments: str, setting: str, permissive: str
) -> None:
    configuration = {"write_repos": [_PROJECT], "write_branches": ["feature"]}
    assert _result(f"git push {arguments}", **configuration) == "DENY"
    configuration[setting] = setting == "allow_destructive"
    assert _result(f"git push {arguments}", **configuration) == permissive


def test_opt_ins_do_not_bypass_project_or_branch_checks() -> None:
    configuration = {
        "deny_force_push": False,
        "deny_tag_push": False,
        "allow_destructive": True,
        "write_repos": [_PROJECT],
        "write_branches": ["feature"],
    }
    assert _result(f"git push -f {_URL} HEAD:refs/heads/main", **configuration) == "DENY"
    assert _result(f"git push --delete {_URL} refs/heads/main", **configuration) == "DENY"
    assert (
        _result("git push https://gitlab.com/team/other refs/tags/v1", **configuration) == "DENY"
    )
    assert _result(f"glab issue create -R {_URL} -t Fix -d Details", **configuration) == "ALLOW"
    assert _result(f"glab mr delete 12 -R {_URL}", **configuration) == "DENY"


@pytest.mark.parametrize("remote", [_URL, "origin", ""])
def test_mirror_respects_force_tag_and_deletion_guards(remote: str) -> None:
    configuration = {
        "write_repos": [_PROJECT],
        "allow_destructive": True,
        "deny_force_push": False,
        "deny_tag_push": False,
    }
    command = f"git push --mirror {remote}"
    assert _result(command, **configuration) == "ASK"
    for guard, value in [
        ("allow_destructive", False),
        ("deny_force_push", True),
        ("deny_tag_push", True),
    ]:
        assert _result(command, **(configuration | {guard: value})) == "DENY"


@pytest.mark.parametrize("arguments", ["--force", "--tags", "--delete", "--mirror"])
def test_push_guards_apply_only_when_https_host_is_not_known_foreign(arguments: str) -> None:
    foreign = _response(
        f"git push {arguments} https://git.example.com/team/app HEAD:refs/heads/feature"
    )
    assert foreign is not None and foreign["result"] == "ASK"
    assert "outside configured GitLab hosts" in foreign["reason"]
    for remote in [_URL, "origin", "", _URL + "/../other"]:
        assert _result(f"git push {arguments} {remote}", write_repos=[_PROJECT]) == "DENY"


@pytest.mark.parametrize("extra", ["--push-option=ci.skip", "--all", "other"])
def test_known_forbidden_branch_stays_denied_with_unresolved_effects(extra: str) -> None:
    response = _response(
        f"git push {_URL} HEAD:refs/heads/main {extra}",
        write_repos=[_PROJECT],
        write_branches=["feature"],
    )
    assert response is not None and response["result"] == "DENY"
    assert "branch is not permitted" in response["reason"]


@pytest.mark.parametrize("name", ["refs/heads/main", "+example"])
def test_tag_shorthand_name_is_not_a_branch_or_force_refspec(name: str) -> None:
    response = _response(
        f"git push {_URL} tag {name}",
        write_repos=[_PROJECT],
        write_branches=["feature"],
        deny_tag_push=False,
    )
    assert response is not None and response["result"] == "ASK"
    assert "additional effects" in response["reason"]


@pytest.mark.parametrize(
    "arguments",
    [
        f"clone -c core.filemode=false {_URL}",
        f"clone --config core.filemode=false {_URL}",
        f"clone --recurse-submodules {_URL}",
        f"clone --reference /tmp/example {_URL}",
        f"fetch --multiple {_URL} other",
        f"fetch --all {_URL}",
        f"fetch --submodule-prefix example {_URL}",
        f"pull --recurse-submodules {_URL} main",
        f"push --recurse-submodules=on-demand {_URL} HEAD:refs/heads/feature",
        f"push --recurse-submodules=check {_URL} HEAD:refs/heads/feature",
        f"push --push-option=ci.skip {_URL} HEAD:refs/heads/feature",
        f"push -oci.skip {_URL} HEAD:refs/heads/feature",
        f"push --receive-pack=example {_URL} HEAD:refs/heads/feature",
        f"push --all {_URL}",
        f"push --future-option value {_URL} HEAD:refs/heads/feature",
        f"-c push.followTags=true push {_URL} HEAD:refs/heads/feature",
        f"-C /tmp/example push {_URL} HEAD:refs/heads/feature",
        f"push --repo {_URL} --repo https://gitlab.com/team/other HEAD:refs/heads/feature",
        "clone git@gitlab.com:team/sub/app.git",
        "fetch https://gitlab.com.example.net/team/sub/app main",
        "pull /tmp/example main",
    ],
)
def test_unmodelled_transfer_effects_ask(arguments: str) -> None:
    assert _result(f"git {arguments}", write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize(
    "arguments",
    [
        f"push -o --force {_URL} HEAD:refs/heads/feature",
        f"push --push-option --delete {_URL} HEAD:refs/heads/feature",
        f"push --receive-pack --tags {_URL} HEAD:refs/heads/feature",
        f"push --recurse-submodules --force {_URL} HEAD:refs/heads/feature",
        "push --push-option --force",
        "push --receive-pack --tags",
        "push --recurse-submodules --force",
        f"push --future-option --force {_URL} HEAD:refs/heads/feature",
    ],
)
def test_git_option_values_do_not_become_push_guards(arguments: str) -> None:
    response = _response(f"git {arguments}", write_repos=[_PROJECT])
    assert response is not None and response["result"] == "ASK"
    assert "Force pushes" not in response["reason"]
    assert "deletion is not permitted" not in response["reason"]
    assert "Tag pushes" not in response["reason"]


@pytest.mark.parametrize("branch", ["", "bad..name", "bad.lock", "bad/", "-bad", "bad name"])
def test_invalid_branch_configuration_fails(branch: str) -> None:
    with pytest.raises(ValueError, match="write_branches"):
        gitlab_policy(write_branches=[branch])


def test_git_transfer_shell_composition_and_host_pinning() -> None:
    configuration = {
        "hosts": ["gitlab.com", "gitlab.example.com:8443"],
        "write_repos": ["https://gitlab.example.com:8443/team/sub/app"],
    }
    command = "git push https://gitlab.example.com:8443/team/sub/app HEAD:refs/heads/feature"
    assert _result(f'bash -c "{command}"', **configuration) == "ALLOW"
    assert _result(f"git push {_URL} HEAD:refs/heads/feature", **configuration) == "DENY"
    assert _result(f"git fetch origin && {command}", **configuration) == "ASK"
    assert (
        _result(f"git fetch origin && git push {_URL} HEAD:refs/heads/feature", **configuration)
        == "DENY"
    )


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://user:example@gitlab.com/team/sub/app.git", "ASK"),
        (_URL + ".wiki.git", "DENY"),
        (_URL + "/snippets/12.git", "DENY"),
        (_URL + "/", "ASK"),
    ],
)
def test_git_project_identity_is_not_reduced_to_parent(remote: str, expected: str) -> None:
    assert _result(f"git fetch {remote}", read_all=False, read_repos=[_PROJECT]) == expected


@pytest.mark.parametrize(
    "prefix",
    [
        "printf -v GIT_CONFIG_COUNT 1; export GIT_CONFIG_COUNT;",
        "printf -vGIT_CONFIG_COUNT 1;",
        "printf -v GIT_CONFIG_COUNT 1; "
        "printf -v GIT_CONFIG_KEY_0 url.https://git.example.com/team/.insteadOf; "
        "printf -v GIT_CONFIG_VALUE_0 https://gitlab.com/; "
        "export GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0;",
        "env -u GIT_CONFIG_COUNT",
    ],
)
def test_protected_environment_names_keep_otherwise_permitted_transfers_pending(
    prefix: str,
) -> None:
    response = _response(
        f"{prefix} git push {_URL} HEAD:refs/heads/feature", write_repos=[_PROJECT]
    )
    assert response is not None and response["result"] == "ASK"
    assert "CLI environment" in response["reason"]


@pytest.mark.parametrize(
    "command",
    [
        f"glab issue create -R {_URL} -t HOME -d Details",
        "git commit -m HOME",
        'git commit -m "Document GIT_DIR"',
        'echo "GIT"_DIR',
        "git add git_helpers.py",
    ],
)
def test_protected_environment_names_in_literal_text_conservatively_ask(command: str) -> None:
    response = _response(command, write_repos=[_PROJECT])
    assert response is not None and response["result"] == "ASK"
    assert "CLI environment" in response["reason"]


def test_protected_environment_names_do_not_weaken_a_known_denial() -> None:
    assert (
        _result(
            f"export GIT_CONFIG_COUNT; git push {_URL} HEAD:refs/heads/main",
            write_repos=[_PROJECT],
            write_branches=["feature"],
        )
        == "DENY"
    )


def test_protected_environment_name_matching_respects_identifier_boundaries() -> None:
    assert _result("git add test_git_helpers.py prefix_GIT_DIR.txt HOMELAND.txt") == "ALLOW"


@pytest.mark.parametrize(
    "name",
    [
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "https_proxy",
        "HTTPS_PROXY",
        "CURL_CA_BUNDLE",
        "SSL_CERT_FILE",
        "SSH_ASKPASS",
    ],
)
def test_execution_and_transport_environment_names_require_approval(name: str) -> None:
    response = _response(
        f"export {name}; git push {_URL} HEAD:refs/heads/feature", write_repos=[_PROJECT]
    )
    assert response is not None and response["result"] == "ASK"
    assert "CLI environment" in response["reason"]


@pytest.mark.parametrize("name", ["Git_Config_Count", "gIt_CoNfIg_KeY_0", "Path", "ssl_cert_file"])
def test_protected_environment_names_ignore_case_across_hosts(name: str) -> None:
    response = _response(
        f"set {name}=example; git push {_URL} HEAD:refs/heads/feature", write_repos=[_PROJECT]
    )
    assert response is not None and response["result"] == "ASK"
    assert "CLI environment" in response["reason"]


_MR_CREATE_FIELDS = (
    f"--head {_URL} -s feature -b main -t Fix -d Details "
    "--remove-source-branch=false --allow-collaboration=false"
)


@pytest.mark.parametrize(
    "action",
    [
        f"create {_MR_CREATE_FIELDS}",
        f"new {_MR_CREATE_FIELDS} --draft -y -l bug",
        "update 12 -t Revised -d Details",
        'update 12 -t /merge -d "Mention /rebase inline"',
        "update 12 --target-branch main",
        "update 12 -r -l bug -u stale -y",
        "update 12 --wip --draft=false --remove-source-branch=false",
    ],
)
def test_supported_mr_metadata_writes_check_project_not_branch_content(action: str) -> None:
    command = f"glab mr {action} -R {_URL}"
    assert _result(command, write_repos=[_PROJECT], write_branches=["unrelated"]) == "ALLOW"
    assert _result(command, read_all=True) == "DENY"
    assert (
        _result(
            command.replace(f"-R {_URL}", "-R https://gitlab.com/team/other"),
            write_repos=[_PROJECT],
        )
        == "DENY"
    )


@pytest.mark.parametrize(
    "omitted",
    [
        f"--head {_URL}",
        "-s feature",
        "-b main",
        "-t Fix",
        "-d Details",
        "--remove-source-branch=false",
        "--allow-collaboration=false",
    ],
)
def test_mr_creation_requires_explicit_inputs_and_false_overrides(omitted: str) -> None:
    fields = _MR_CREATE_FIELDS.replace(omitted, "")
    assert _result(f"glab mr create -R {_URL} {fields}", write_repos=[_PROJECT]) == "ASK"
    assert _result(f"glab mr create -R {_URL} {fields}") == "DENY"


@pytest.mark.parametrize(
    ("action", "expected", "opted_in"),
    [
        ("create --remove-source-branch", "DENY", "ASK"),
        ("new --remove-source-branch", "DENY", "ASK"),
        ("merge 12 -d", "DENY", "ASK"),
        ("merge 12 -yd", "DENY", "ASK"),
        ("merge 12 -d --when-pipeline-succeeds", "DENY", "ASK"),
        ("accept 12 --remove-source-branch=true", "DENY", "ASK"),
        ("merge --remove-source-branch", "DENY", "ASK"),
        ("merge 12 -d --remove-source-branch=false", "ASK", "ASK"),
        ("update 12 --remove-source-branch", "ASK", "ASK"),
    ],
)
def test_mr_source_removal_is_guarded_without_guessing_toggle_state(
    action: str, expected: str, opted_in: str
) -> None:
    command = f"glab mr {action} -R {_URL}"
    assert _result(command, write_repos=[_PROJECT]) == expected
    assert _result(command, write_repos=[_PROJECT], allow_destructive=True) == opted_in


@pytest.mark.parametrize(
    "action",
    [
        "merge 12 -y",
        "accept 12 -sy --auto-merge=false",
        "merge 12 --remove-source-branch=false",
        "merge 12 -r -m Details",
        "rebase 12",
        "rebase 12 --skip-ci",
    ],
)
def test_mr_branch_operations_require_authoritative_metadata(action: str) -> None:
    response = _response(
        f"glab mr {action} -R {_URL}", write_repos=[_PROJECT], allow_destructive=True
    )
    assert response is not None and response["result"] == "ASK"
    reason = "source project and branch" if action.startswith("rebase") else "target branch"
    assert reason in response["reason"]
    assert _result(f"glab mr {action} -R {_URL}") == (
        "ASK" if action.startswith("rebase") else "DENY"
    )


@pytest.mark.parametrize(
    "action",
    [
        f"create {_MR_CREATE_FIELDS} --push",
        f"create {_MR_CREATE_FIELDS} --fill",
        f"create {_MR_CREATE_FIELDS} --auto-merge",
        f"create {_MR_CREATE_FIELDS} --create-source-branch",
        f"create {_MR_CREATE_FIELDS} --target-project team/other",
        "create " + _MR_CREATE_FIELDS.replace(_URL, "https://gitlab.com/team/other"),
        f"create {_MR_CREATE_FIELDS} --head https://gitlab.com/team/other",
        f"create {_MR_CREATE_FIELDS} --allow-collaboration=true",
        f"create {_MR_CREATE_FIELDS} --source-branch=-invalid",
        f"create {_MR_CREATE_FIELDS} -d /merge",
        f'new {_MR_CREATE_FIELDS} -d "Details\n  /rebase"',
        "update 12 -b main",
        "update branch -t Fix",
        "update -t Fix",
        "update 12",
        "update 12 --fill",
        "update 12 --attach example.png",
        "update 12 -d -",
        'update 12 -t ""',
        'update 12 -d ""',
        "update 12 -d /merge",
        'update 12 -d "Details\n/rebase"',
        'update 12 -d "  /remove_source_branch"',
        'update 12 -d "Details\n\t/unknown_action"',
        "update 12 --description-file example.txt",
        "update 12 --target-branch=../main",
        "edit 12 -t Fix",
        "merge feature -y",
        f"merge {_URL}/-/merge_requests/12 -y",
    ],
)
def test_unresolved_mr_arguments_and_secondary_effects_ask(action: str) -> None:
    assert _result(f"glab mr {action} -R {_URL}", write_repos=[_PROJECT]) == "ASK"


def test_mr_quick_actions_are_checked_after_project_permissions() -> None:
    command = f'glab mr update 12 -R {_URL} -d "Details\n/rebase"'
    response = _response(command, write_repos=[_PROJECT])
    assert response is not None and response["result"] == "ASK"
    assert "quick actions" in response["reason"]
    assert _result(command) == "DENY"


def test_mr_options_keep_their_values_and_last_boolean_wins() -> None:
    fields = _MR_CREATE_FIELDS.replace("-t Fix", '-t "--remove-source-branch"')
    assert _result(f"glab mr create -R {_URL} {fields}", write_repos=[_PROJECT]) == "ALLOW"
    fields = _MR_CREATE_FIELDS + " --remove-source-branch=true --remove-source-branch=false"
    assert _result(f"glab mr create -R {_URL} {fields}", write_repos=[_PROJECT]) == "ALLOW"
    fields = _MR_CREATE_FIELDS + ' -d /merge -d "Mention /merge inline"'
    assert _result(f"glab mr create -R {_URL} {fields}", write_repos=[_PROJECT]) == "ALLOW"
    response = _response(
        f'glab mr merge 12 -R {_URL} -m "--remove-source-branch"', write_repos=[_PROJECT]
    )
    assert response is not None and response["result"] == "ASK"
    assert "target branch" in response["reason"]


def test_mr_shell_composition_preserves_known_denials() -> None:
    assert (
        _result(
            f"glab mr rebase 12 -R {_URL} && glab mr merge 12 -R {_URL} -d",
            write_repos=[_PROJECT],
        )
        == "DENY"
    )


_API_SELECTOR = f"--hostname gitlab.com --placeholder project={_PROJECT}"
_API_MR_FIELDS = (
    "-f source_branch=feature -f target_branch=main -f title=Fix -f description=Details "
    "-F remove_source_branch=false -F allow_collaboration=false"
)


@pytest.mark.parametrize(
    ("api_request", "write"),
    [
        ("projects/:project", False),
        ("/projects/:project/issues -i --output ndjson", False),
        ("projects/:project/issues/12", False),
        ("projects/:project/issues/12/notes", False),
        ("projects/:project/issues/12/notes/34", False),
        ("projects/:project/merge_requests", False),
        ("projects/:project/merge_requests/12", False),
        ("projects/:project/merge_requests/12/notes", False),
        ("projects/:project/merge_requests/12/changes", False),
        ("projects/:project/merge_requests/12/diffs", False),
        ("projects/:project/merge_requests/12/raw_diffs", False),
        ("projects/:project/repository/branches", False),
        ("projects/:project/repository/branches/feature", False),
        ("projects/:project/repository/branches/:ref --placeholder ref=feature/topic", False),
        ("projects/:project/issues -X GET -f page=2 -f per_page=10 -f state=opened", False),
        ("projects/:project/issues -f title=Fix -f description=Details", True),
        ("projects/:project/issues -iXPOST -ftitle=Fix -fdescription=Details", True),
        (
            "projects/:project/issues -f title=Fix -f description=@example.txt "
            "-F confidential=true",
            True,
        ),
        ('projects/:project/issues/12/notes -f "body=Mention /merge inline"', True),
        ("projects/:project/merge_requests/12/notes -f body=Details", True),
        (f"projects/:project/merge_requests {_API_MR_FIELDS}", True),
        ("projects/:project/merge_requests/12 -X PUT -f target_branch=main", True),
        (
            "projects/:project/merge_requests/12 -X PUT -f title=/merge "
            '-f "description=Mention /rebase inline"',
            True,
        ),
        (
            "projects/:project/merge_requests/12 -X PUT -f title=Fix "
            "-f add_labels=bug -f remove_labels=stale -F remove_source_branch=false",
            True,
        ),
    ],
)
def test_supported_api_routes_enforce_project_permissions(api_request: str, write: bool) -> None:
    command = f"glab api {api_request} {_API_SELECTOR}"
    configuration = {
        "read_all": False,
        "read_repos": [_PROJECT],
        "write_repos": [_PROJECT],
        "write_branches": ["unrelated"],
    }
    assert _result(command, **configuration) == "ALLOW"
    foreign = command.replace(f"project={_PROJECT}", "project=team/other")
    assert _result(foreign, **configuration) == "DENY"
    assert _result(command) == ("DENY" if write else "ALLOW")


@pytest.mark.parametrize(
    "api_request",
    [
        "projects/:project/repository/commits",
        "projects/:project/issues/12/close -X POST",
        "projects/:project/issues -X HEAD",
        "projects/:project/issues -X get",
        "projects/:project/issues -X PATCH",
        "projects/:project/issues -X POST -X GET",
        "projects/:project/issues --paginate",
        "projects/:project/issues --input example.json",
        "projects/:project/issues --form title=Fix",
        'projects/:project/issues -H "X-HTTP-Method-Override: DELETE"',
        'projects/:project/issues -H "Sudo: 12"',
        '"projects/:project/issues?page=2"',
        "projects/:project/issues/../merge_requests",
        "projects/:project//issues",
        "https://gitlab.com/api/v4/projects/:project/issues",
        "projects/123/issues",
        "projects/:id/issues",
        "projects/:project-extra/issues",
        "projects/:project/issues --placeholder ref=feature",
        f"projects/:project/issues --placeholder project={_PROJECT}",
        "projects/:project/issues --placeholder branch=feature",
        "projects/:project/repository/branches/:ref-extra --placeholder ref=feature",
        "projects/:project/repository/branches/:ref --placeholder ref=feature/:repo",
        "projects/:project/issues -X GET -f sudo=12",
        "projects/:project/issues -X GET -f id=123",
        'projects/:project/issues -f "title=Fix" -f "title=Revised" -f description=Details',
        "projects/:project/issues -F title=Fix -f description=Details",
        "projects/:project/issues -F title=@example.txt -f description=Details",
        "projects/:project/issues -F title=:branch -f description=Details",
        "projects/:project/issues -F title=123 -f description=Details",
        "projects/:project/issues -F title=null -f description=Details",
        'projects/:project/issues -F "title=[true]" -f description=Details',
        'projects/:project/issues -F "title={}" -f description=Details',
        'projects/:project/issues -f "title[]=Fix" -f description=Details',
        'projects/:project/issues -f title=Fix -f "description="',
        "projects/:project/issues -f title=Fix -f description=Details -f _method=DELETE",
        f"projects/:project/merge_requests {_API_MR_FIELDS} -f source_project_id=123",
        f"projects/:project/merge_requests {_API_MR_FIELDS} -F should_remove_source_branch=true",
        "projects/:project/merge_requests/12 -X PUT -f title=Fix "
        "-F should_remove_source_branch=true",
        f"projects/:project/merge_requests {_API_MR_FIELDS} -f source_branch=other",
        "projects/:project/merge_requests "
        + _API_MR_FIELDS.replace("allow_collaboration=false", "allow_collaboration=true"),
        "projects/:project/merge_requests "
        + _API_MR_FIELDS.replace("source_branch=feature", "source_branch=-invalid"),
        "projects/:project/merge_requests/12 -X PUT",
        "projects/:project/merge_requests/12 -X PUT -F remove_source_branch=false",
        'projects/:project/merge_requests/12 -X PUT -f "title="',
        'projects/:project/merge_requests/12 -X PUT -f "description="',
        "projects/:project/merge_requests/12 -X PUT -f target_branch=../main",
        "projects/:project/merge_requests/12 -X PUT -F squash=true",
        "projects/:project/merge_requests/12 -X PUT -f id=123 -f title=Fix",
    ],
)
def test_unmodelled_api_inputs_require_approval(api_request: str) -> None:
    assert _result(f"glab api {api_request} {_API_SELECTOR}", write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize(
    "omitted",
    [
        "-f source_branch=feature",
        "-f target_branch=main",
        "-f title=Fix",
        "-f description=Details",
        "-F remove_source_branch=false",
        "-F allow_collaboration=false",
    ],
)
def test_api_mr_creation_requires_literal_fields_and_false_overrides(omitted: str) -> None:
    fields = _API_MR_FIELDS.replace(omitted, "")
    assert (
        _result(
            f"glab api projects/:project/merge_requests {fields} {_API_SELECTOR}",
            write_repos=[_PROJECT],
        )
        == "ASK"
    )


@pytest.mark.parametrize(
    "selector",
    [
        "",
        "--hostname gitlab.com",
        f"--placeholder project={_PROJECT}",
        "--hostname https://gitlab.com --placeholder project=team/sub/app",
        "--hostname gitlab.example.com:8443 --placeholder project=team/sub/app",
        "--hostname gitlab.other.example --placeholder project=team/sub/app",
        "--hostname gitlab.com --hostname gitlab.example.com --placeholder project=team/sub/app",
        "--hostname gitlab.com --placeholder project=team/sub/app.git",
        "--hostname gitlab.com --placeholder project=team/../app",
        "--hostname gitlab.com --placeholder project=https://gitlab.com/team/app",
    ],
)
def test_api_authority_and_project_must_be_explicit_literal_inputs(selector: str) -> None:
    assert _result(f"glab api projects/:project/issues {selector}") == "ASK"


@pytest.mark.parametrize(
    "project", [":project", "123", ":id", ":namespace/:repo", ":group/:namespace/:repo"]
)
@pytest.mark.parametrize("suffix", ["", "/issues/12", "/merge_requests/12"])
def test_api_object_deletion_is_denied_before_project_resolution(
    project: str, suffix: str
) -> None:
    command = f"glab api projects/{project}{suffix} -X DELETE --hostname gitlab.com"
    assert _result(command) == "DENY"
    assert _result(command, allow_destructive=True) == "DENY"


@pytest.mark.parametrize(
    ("branch_endpoint", "placeholder", "branch"),
    [
        ("feature", "", "feature"),
        (":ref", "--placeholder ref=feature/topic", "feature/topic"),
    ],
    ids=["literal", "placeholder"],
)
def test_api_branch_deletion_requires_opt_in_and_exact_project_and_branch_permissions(
    branch_endpoint: str, placeholder: str, branch: str
) -> None:
    command = (
        f"glab api projects/:project/repository/branches/{branch_endpoint} "
        f"-X DELETE {_API_SELECTOR} {placeholder}"
    )
    configuration = {"write_repos": [_PROJECT], "write_branches": [branch]}
    assert _result(command, **configuration) == "DENY"
    assert _result(command, allow_destructive=True, **configuration) == "ALLOW"
    assert _result(command, allow_destructive=True) == "DENY"
    assert (
        _result(
            command,
            allow_destructive=True,
            write_repos=[_PROJECT],
            write_branches=["main"],
        )
        == "DENY"
    )
    assert (
        _result(
            command,
            allow_destructive=True,
            write_repos=[_PROJECT],
            write_branches=[branch.upper()],
        )
        == "DENY"
    )
    for extra in (" -f branch=main", " --input example.json"):
        assert _result(command + extra, allow_destructive=True, **configuration) == "ASK"
    assert (
        _result(
            command.replace(branch, "feature.json"),
            allow_destructive=True,
            **configuration,
        )
        == "ASK"
    )
    assert _result(command.replace("gitlab.com", "gitlab.other.example")) == "ASK"


def test_api_bulk_and_unresolved_branch_deletions_stay_conservative() -> None:
    for endpoint in (
        "projects/:project/repository/merged_branches",
        "projects/123/repository/branches/feature",
        "projects/:namespace/:repo/repository/branches/:branch",
    ):
        command = f"glab api {endpoint} -X DELETE {_API_SELECTOR}"
        assert _result(command, write_repos=[_PROJECT]) == "DENY"
        assert _result(command, write_repos=[_PROJECT], allow_destructive=True) == "ASK"


@pytest.mark.parametrize(
    ("api_request", "expected"),
    [
        ("merge_requests -f source_branch=feature -F remove_source_branch=true", "DENY"),
        ("merge_requests/12 -X PUT -f title=Fix -F remove_source_branch=true", "DENY"),
        ("merge_requests/12/merge -X PUT -F should_remove_source_branch=true", "DENY"),
        ("merge_requests/12/merge -X PUT -f should_remove_source_branch=true", "DENY"),
        ("merge_requests/12/merge -X PUT -F remove_source_branch=true", "ASK"),
        ("merge_requests/12/merge -X PUT -F should_remove_source_branch=false", "ASK"),
        ("merge_requests/12 -X PUT -f title=Fix -f remove_source_branch=false", "ASK"),
        ("merge_requests/12/rebase -X PUT -F skip_ci=true", "ASK"),
    ],
)
def test_api_removal_assignments_and_unresolved_mr_branch_effects(
    api_request: str, expected: str
) -> None:
    command = f"glab api projects/:project/{api_request} {_API_SELECTOR}"
    assert _result(command, write_repos=[_PROJECT]) == expected
    assert _result(command, write_repos=[_PROJECT], allow_destructive=True) == "ASK"
    if "/rebase" in api_request:
        assert _result(command) == "ASK"
    if api_request.startswith("merge_requests/12/merge "):
        assert _result(command) == "DENY"
        assert _result(command, allow_destructive=True) == "DENY"
        assert (
            _result(
                command.replace(f"project={_PROJECT}", "project=team/other"),
                write_repos=[_PROJECT],
                allow_destructive=True,
            )
            == "DENY"
        )


def test_api_destination_override_does_not_misidentify_source_project_as_write_target() -> None:
    command = (
        f"glab api projects/:project/merge_requests {_API_SELECTOR} {_API_MR_FIELDS} "
        "-f target_project_id=123"
    )
    assert _result(command) == "ASK"
    assert _result(command, write_repos=[_PROJECT]) == "ASK"


@pytest.mark.parametrize(
    "api_request",
    [
        'issues -f title=Fix -f "description=Details\n/close"',
        'issues/12/notes -f "body=Details\n  /move team/other"',
        "merge_requests "
        + _API_MR_FIELDS.replace("-f description=Details", '-f "description=/merge"'),
        'merge_requests/12 -X PUT -f "description=Details\n/rebase"',
        'merge_requests/12/notes -f "body=/merge"',
    ],
)
def test_api_text_quick_actions_require_approval_after_project_checks(api_request: str) -> None:
    command = f"glab api projects/:project/{api_request} {_API_SELECTOR}"
    response = _response(command, write_repos=[_PROJECT])
    assert response is not None and response["result"] == "ASK"
    assert "quick actions" in response["reason"]
    assert _result(command) == "DENY"


def test_api_option_values_and_method_selection_are_not_reinterpreted() -> None:
    command = f"glab api projects/:project/issues {_API_SELECTOR}"
    assert (
        _result(command + " -f title=--method -f description=Details", write_repos=[_PROJECT])
        == "ALLOW"
    )
    assert _result(command + " -f page=2") == "DENY"
    assert _result(command + " -X GET -f page=2") == "ALLOW"
    assert _result(command + " --input --method GET") == "ASK"
    assert _result(command + " --help") == "ALLOW"
    assert _result(command + " --paginate=false --silent --include --output=json") == "ALLOW"
    assert (
        _result(command.replace("projects/:project/issues", "projects/team%2Fsub%2Fapp/issues"))
        == "ASK"
    )
    assert (
        _result(
            command
            + " -f title=Fix -f description=Details -F confidential=false -f confidential=false",
            write_repos=[_PROJECT],
        )
        == "ASK"
    )
    denied = "glab api projects/123/repository/branches/feature -X DELETE --hostname gitlab.com"
    assert _result(command + " --input example.json && " + denied) == "DENY"


def test_api_projects_can_be_pinned_to_a_specific_authority() -> None:
    command = (
        "glab api projects/:project/issues -f title=Fix -f description=Details " + _API_SELECTOR
    )
    configuration = {
        "hosts": ["gitlab.com", "gitlab.example.com"],
        "write_repos": ["https://gitlab.example.com/team/sub/app"],
    }
    assert _result(command, **configuration) == "DENY"
    assert (
        _result(
            command.replace("--hostname gitlab.com", "--hostname gitlab.example.com"),
            **configuration,
        )
        == "ALLOW"
    )
    assert (
        _result(
            "glab api projects/:project --hostname gitlab.example.com:8443 "
            "--placeholder project=team/sub/app",
            hosts=["gitlab.example.com:8443"],
        )
        == "ASK"
    )
