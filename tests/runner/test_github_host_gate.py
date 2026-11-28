"""The gate every ``gh`` call passes before it starts: the host it names must be its scope's.

A pull request URL can name any host, and ``gh`` sends a token to the host it's told to, so
a refused call starts no process at all.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef

KNOWN = "ghe-b.example.com"
OTHER_KNOWN = "ghe-a.example.com"
UNKNOWN = "pr-host.example.com"
RUNNER = Path(github_resource.__file__).parent


@pytest.fixture
def started(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the argv of each ``gh`` process ``_gh`` starts; two hosts are set up for GitHub.

    git still runs, so a test's checkout has real remotes.
    """
    starts: list[list[str]] = []
    real_run = github_resource._run

    def run(argv: list[str], **kwargs: object) -> tuple[int | None, str, str]:
        if argv[0] == "git":
            return real_run(argv, **kwargs)  # type: ignore[arg-type]
        starts.append(argv)
        if argv[1:4] == ["auth", "status", "--json"]:
            hosts = {
                host: [{"login": "octo", "state": "success"}] for host in (KNOWN, OTHER_KNOWN)
            }
            return 0, json.dumps({"hosts": hosts}), ""
        if argv[-2:] == ["--jq", ".id"]:
            return 0, "123", ""
        return 0, "{}", ""

    monkeypatch.setattr(github_resource, "_run", run)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", f"{KNOWN},{OTHER_KNOWN}")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda _key: None)
    return starts


@pytest.mark.parametrize(
    ("scope", "argv"),
    [
        # A host named with no scope, or another scope's, never runs.
        (None, ["api", "--hostname", KNOWN, "x"]),
        (None, ["pr", "view", "1", "-R", f"{KNOWN}/o/r"]),
        (OTHER_KNOWN, ["api", "--hostname", KNOWN, "x"]),
        (OTHER_KNOWN, ["repo", "view", f"{KNOWN}/o/r", "--json", "name"]),
        (OTHER_KNOWN, ["pr", "view", f"https://{KNOWN}/o/r/pull/1", "--json", "title"]),
        (
            OTHER_KNOWN,
            ["api", "-X", "POST", f"https://{KNOWN}/api/uploads/user-attachments/assets"],
        ),
        # A scope on a host the user never set up runs nothing, even a call naming no host.
        (UNKNOWN, ["api", "--hostname", UNKNOWN, "x"]),
        (UNKNOWN, ["auth", "status", "--json", "hosts", "-h", UNKNOWN]),
        # With no host, the sign-in listing would check every host with every token.
        (KNOWN, ["auth", "status", "--json", "hosts"]),
        (UNKNOWN, ["auth", "token", "-h", UNKNOWN]),
        # Commands and flags the panel doesn't run.
        (KNOWN, ["pr", "create", "-R", f"{KNOWN}/o/r"]),
        (KNOWN, ["api", "--hostname", KNOWN, "--paginate", "x"]),
        (KNOWN, ["auth", "status", "--show-token"]),
    ],
)
def test_a_refused_call_starts_no_process(
    started: list[list[str]], scope: str | None, argv: list[str]
) -> None:
    with github_resource.gh_target_host_scope(scope):
        rc, out, err = github_resource._gh(argv, cwd="/workspace", token="chosen-token")

    assert started == []
    assert (rc, out) == (1, "")
    assert err.startswith(github_resource.GH_REFUSED)
    assert "chosen-token" not in err


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "--hostname", KNOWN, "x"],
        ["pr", "view", "1", "-R", f"{KNOWN}/o/r"],
        ["api", "-X", "POST", f"https://{KNOWN}/api/uploads/user-attachments/assets"],
        ["auth", "status", "--json", "hosts", "-h", KNOWN],
    ],
)
def test_a_call_naming_its_scopes_known_host_or_none_runs(
    started: list[list[str]], argv: list[str]
) -> None:
    with github_resource.gh_target_host_scope(KNOWN.upper()):
        github_resource._gh(argv, cwd="/workspace")

    assert started == [["gh", *argv]]


def test_github_com_needs_no_set_up_but_still_its_own_scope(started: list[list[str]]) -> None:
    with github_resource.gh_target_host_scope("github.com"):
        github_resource._gh(["api", "--hostname", "github.com", "x"], cwd="/workspace")
        github_resource._gh(["api", "--hostname", KNOWN, "x"], cwd="/workspace")

    assert started == [["gh", "api", "--hostname", "github.com", "x"]]


def test_an_attachment_upload_goes_only_to_its_own_pull_requests_host(
    started: list[list[str]],
) -> None:
    """The upload names its host in a full URL, with no --hostname: the scope still decides."""
    reference = PullRequestRef.from_url(f"https://{KNOWN}/o/r/pull/7")

    with github_resource.gh_target_host_scope(KNOWN), pytest.raises(ValueError):
        # The fake answers the upload with no asset URL, so it fails after it starts.
        github_resource._upload_attachment(
            "/workspace", reference, name="a.png", content_type="image/png", content=b"x"
        )
    uploads = [argv for argv in started if any("/api/uploads/" in part for part in argv)]
    assert len(uploads) == 1
    assert any(part.startswith(f"https://{KNOWN}/api/uploads/") for part in uploads[0])

    started.clear()
    with github_resource.gh_target_host_scope(OTHER_KNOWN), pytest.raises(ValueError):
        github_resource._upload_attachment(
            "/workspace", reference, name="a.png", content_type="image/png", content=b"x"
        )
    assert not any(KNOWN in part for argv in started for part in argv)


def test_token_lookups_take_their_hosts_scope_and_keep_an_outer_one(
    started: list[list[str]],
) -> None:
    assert github_resource._gh_active_token("/workspace", KNOWN) == "{}"
    with github_resource.gh_target_host_scope(OTHER_KNOWN):
        assert github_resource._gh_active_token("/workspace", KNOWN) is None

    assert started == [["gh", "auth", "token", "-h", KNOWN]]


# Calls that start a process from their first argument.
_PROCESS_STARTS = frozenset(
    {
        "run",
        "Popen",
        "call",
        "check_call",
        "check_output",
        "system",
        "spawn",
        "create_subprocess_exec",
        "create_subprocess_shell",
        "_run",
    }
)


def _names_gh(node: ast.AST) -> bool:
    if isinstance(node, (ast.List, ast.Tuple)):
        return bool(node.elts) and _names_gh(node.elts[0])
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and (node.value == "gh" or node.value.startswith("gh "))
    )


def _gh_process_starts(tree: ast.AST) -> list[tuple[int, str]]:
    """Each ``gh`` command line built as a list, or passed to a process start, by line."""
    found: list[tuple[int, str]] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        if isinstance(node, (ast.List, ast.Tuple)) and _names_gh(node):
            found.append((node.lineno, function))
        if isinstance(node, ast.Call) and node.args:
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if (
                name in _PROCESS_STARTS
                and _names_gh(node.args[0])
                and not isinstance(node.args[0], (ast.List, ast.Tuple))
            ):
                found.append((node.lineno, function))
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return found


def test_every_gh_process_in_the_runner_starts_through_the_gate() -> None:
    """No runner code builds a ``gh`` command line outside ``_gh_invocation``."""
    outside = [
        f"{path.relative_to(RUNNER)}:{line} in {function}"
        for path in sorted(RUNNER.rglob("*.py"))
        for line, function in _gh_process_starts(ast.parse(path.read_text(encoding="utf-8")))
        if (path.name, function) != ("github_resource.py", "_gh_invocation")
    ]

    assert outside == []


@pytest.mark.parametrize(
    "source",
    [
        'subprocess.run(["gh", "api", "user"])',
        'subprocess.run("gh api user", shell=True)',
        'os.system("gh auth token")',
        'asyncio.create_subprocess_exec("gh", "api")',
        'argv = ("gh", "pr", "view")',
    ],
)
def test_the_audit_finds_each_way_to_start_gh(source: str) -> None:
    assert _gh_process_starts(ast.parse(source)) != []


@pytest.mark.parametrize(
    "entry",
    [
        lambda reference: github_resource._pr_json("/workspace", reference, "title"),
        lambda reference: github_resource._repo_labels("/workspace", reference),
        lambda reference: github_resource._merge_options("/workspace", reference),
        lambda reference: github_resource._pr_commits("/workspace", reference, head_sha="a" * 40),
        lambda reference: github_resource._gh_active_token("/workspace", reference.host),
        lambda reference: github_resource._gh_auth_token("/workspace", "octo", reference.host),
    ],
    ids=["pr-json", "labels", "merge-options", "commits", "active-token", "login-token"],
)
def test_a_pull_request_on_a_host_never_set_up_runs_no_gh_at_all(
    started: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
    entry: Callable[[PullRequestRef], object],
) -> None:
    """With only an environment enterprise token, nothing asks gh about the host or reads a
    token for it: gh counts every host but github.com as signed in, and would send it there."""
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "dummy-enterprise-token")
    reference = PullRequestRef.from_url(f"https://{UNKNOWN}/o/r/pull/7")

    try:
        result = entry(reference)
    except ValueError as exc:
        assert "Sign in to this GitHub host" in str(exc)
    else:
        assert result is None

    assert started == []


def _checkout(path: Path, remotes: dict[str, str], *, config: dict[str, str] | None = None) -> str:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    for name, url in remotes.items():
        subprocess.run(["git", "-C", str(path), "remote", "add", name, url], check=True)
    for key, value in (config or {}).items():
        subprocess.run(["git", "-C", str(path), "config", key, value], check=True)
    return str(path)


@pytest.mark.parametrize(
    "argv",
    [
        ["api", "--hostname", KNOWN, "x"],
        ["api", "x"],
        ["auth", "status", "--json", "hosts", "-h", KNOWN],
        ["auth", "token"],
        ["pr", "view", "--json", "number"],
    ],
)
def test_nothing_reaches_gh_without_a_scope(started: list[list[str]], argv: list[str]) -> None:
    rc, _, err = github_resource._gh(argv, cwd="/workspace")

    assert (rc, started) == (1, [])
    assert "outside a host scope" in err


def test_a_call_naming_no_host_goes_to_gh_host_or_github_com_which_must_be_the_scope(
    started: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    with github_resource.gh_target_host_scope(KNOWN):
        github_resource._gh(["api", "repos/o/r/commits/abc/pulls"], cwd="/workspace")
    with github_resource.gh_target_host_scope("github.com"):
        github_resource._gh(["api", "repos/o/r/commits/abc/pulls"], cwd="/workspace")
    assert started == [["gh", "api", "repos/o/r/commits/abc/pulls"]]

    started.clear()
    monkeypatch.setenv("GH_HOST", KNOWN)
    with github_resource.gh_target_host_scope(KNOWN):
        github_resource._gh(["api", "repos/o/r/commits/abc/pulls"], cwd="/workspace")
    with github_resource.gh_target_host_scope("github.com"):
        github_resource._gh(["api", "repos/o/r/commits/abc/pulls"], cwd="/workspace")
    assert started == [["gh", "api", "repos/o/r/commits/abc/pulls"]]


@pytest.mark.parametrize(
    ("remotes", "config", "host"),
    [
        ({"origin": f"https://{KNOWN}/o/r.git"}, {}, KNOWN),
        ({"origin": f"git@{KNOWN}:o/r.git"}, {}, KNOWN),
        ({"origin": "https://github.com/o/r.git"}, {}, "github.com"),
        # A remote on a host never set up isn't one gh would pick, and counts for nothing.
        ({"origin": f"https://{KNOWN}/o/r.git", "other": f"https://{UNKNOWN}/o/r.git"}, {}, KNOWN),
        ({"origin": f"https://{UNKNOWN}/o/r.git"}, {}, None),
        # Two set-up hosts are ambiguous unless set-default marked one.
        (
            {"origin": "https://github.com/o/r.git", "upstream": f"https://{KNOWN}/o/r.git"},
            {},
            None,
        ),
        (
            {"origin": "https://github.com/o/r.git", "upstream": f"https://{KNOWN}/o/r.git"},
            {"remote.upstream.gh-resolved": "base"},
            KNOWN,
        ),
        # An unknown SSH host may be an SSH-config alias of a known one, so it's ambiguous.
        ({"origin": f"https://{KNOWN}/o/r.git", "other": "corp:o/r.git"}, {}, None),
        ({"origin": f"https://{KNOWN}/o/r.git", "other": "git@corp:o/r.git"}, {}, None),
        ({"origin": f"https://{KNOWN}/o/r.git", "other": "ssh://git@corp/o/r.git"}, {}, None),
        ({"origin": f"https://{KNOWN}/o/r.git", "other": "/srv/git/o/r.git"}, {}, KNOWN),
        ({"origin": f"{KNOWN}:o/r.git"}, {}, KNOWN),
        # insteadOf rewrites the URL gh reads.
        ({"origin": "corp:o/r"}, {f"url.https://{UNKNOWN}/.insteadOf": "corp:"}, None),
        ({"origin": "corp:o/r"}, {f"url.https://{KNOWN}/.insteadOf": "corp:"}, KNOWN),
        ({}, {}, None),
    ],
)
def test_the_checkouts_github_host_is_the_one_known_host_its_remotes_name(
    started: list[list[str]],
    tmp_path: Path,
    remotes: dict[str, str],
    config: dict[str, str],
    host: str | None,
) -> None:
    assert (
        github_resource.checkout_github_host(_checkout(tmp_path, remotes, config=config)) == host
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["pr", "view", "--json", "number"],
        ["pr", "diff", "7"],
        ["repo", "view", "--json", "nameWithOwner,url"],
        ["repo", "set-default", "--view"],
        ["repo", "set-default", "origin"],
        ["repo", "set-default", "o/r"],
        ["api", "repos/{owner}/{repo}/pulls/7/files?per_page=100&page=1"],
    ],
)
def test_a_call_gh_aims_at_the_checkout_runs_only_where_the_checkouts_host_is_the_scope(
    started: list[list[str]], tmp_path: Path, argv: list[str]
) -> None:
    root = _checkout(tmp_path, {"origin": f"https://{KNOWN}/o/r.git"})

    for scope in (github_resource.CHECKOUT_SCOPE, KNOWN, OTHER_KNOWN, "github.com"):
        with github_resource.gh_target_host_scope(scope):
            github_resource._gh(argv, cwd=root)

    assert started == [["gh", *argv]] * 2


def test_a_checkout_with_no_single_github_host_runs_nothing(
    started: list[list[str]], tmp_path: Path
) -> None:
    root = _checkout(tmp_path, {"origin": f"https://{UNKNOWN}/o/r.git"})

    with github_resource.gh_target_host_scope(github_resource.CHECKOUT_SCOPE):
        github_resource._gh(["pr", "view", "--json", "number"], cwd=root)
        github_resource._gh(["auth", "status", "--json", "hosts", "-h", KNOWN], cwd=root)

    assert started == []


TOKENS = {
    "GH_TOKEN": "dummy-gh-token",
    "GITHUB_TOKEN": "dummy-github-token",
    "GH_ENTERPRISE_TOKEN": "dummy-enterprise-token",
    "GITHUB_ENTERPRISE_TOKEN": "dummy-github-enterprise-token",
}
STORED = "stored.example.com"


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Every token variable, GH_REPO and the debug settings set; one host with a stored login."""
    for name, value in {**TOKENS, "GH_REPO": "o/r", "GH_DEBUG": "api", "DEBUG": "1"}.items():
        monkeypatch.setenv(name, value)
    for name in ("GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(name, raising=False)
    config = tmp_path / "gh"
    config.mkdir()
    (config / "hosts.yml").write_text(f"{STORED}:\n    user: octo\n", encoding="utf-8")
    monkeypatch.setenv("GH_CONFIG_DIR", str(config))
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)
    return config


def _passed_tokens(scope: str, argv: list[str], token: str | None = None) -> set[str]:
    invocation = github_resource._gh_invocation(argv, token=token, host=scope, cwd="/workspace")
    env = invocation.env if invocation.env is not None else dict(os.environ)
    assert not {"GH_REPO", "GH_DEBUG", "DEBUG"} & set(env)
    return {name for name in TOKENS if name in env}


@pytest.mark.parametrize(
    ("set_up", "destination", "passed"),
    [
        ({}, "github.com", {"GH_TOKEN", "GITHUB_TOKEN"}),
        # The enterprise token goes to GH_HOST or a configured host, never GH_TOKEN.
        (
            {"OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS": KNOWN},
            KNOWN,
            {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"},
        ),
        ({"GH_HOST": KNOWN}, KNOWN, {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}),
        # A host with only a stored login uses it, not another host's environment token.
        ({}, STORED, set()),
        ({"GH_HOST": KNOWN}, STORED, set()),
        # gh sends GH_TOKEN to GHE.com and github.com's subdomains: only GH_HOST's GHE.com gets it.
        ({"GH_HOST": "tenant.ghe.com"}, "tenant.ghe.com", {"GH_TOKEN", "GITHUB_TOKEN"}),
        ({"OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS": "tenant.ghe.com"}, "tenant.ghe.com", set()),
        ({"OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS": "api.github.com"}, "api.github.com", set()),
        ({"OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS": "x.github.com"}, "x.github.com", set()),
        ({"OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS": "github.localhost"}, "github.localhost", set()),
        ({"GH_HOST": "github.localhost"}, "github.localhost", set()),
    ],
)
def test_each_environment_token_reaches_only_the_host_its_for(
    environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    set_up: dict[str, str],
    destination: str,
    passed: set[str],
) -> None:
    for name, value in set_up.items():
        monkeypatch.setenv(name, value)

    assert _passed_tokens(destination, ["api", "--hostname", destination, "x"]) == passed


@pytest.mark.parametrize(
    ("logins", "passed"),
    [
        # GH_HOST's enterprise token stays off another configured host with its own login.
        ([KNOWN], {OTHER_KNOWN: True, KNOWN: False}),
        # A configured host with no stored login has only the environment's token to use.
        ([], {OTHER_KNOWN: True, KNOWN: True}),
        # GH_HOST keeps the enterprise token, as gh itself ranks it above a stored login.
        ([OTHER_KNOWN, KNOWN], {OTHER_KNOWN: True, KNOWN: False}),
    ],
)
def test_a_configured_host_with_its_own_login_never_gets_the_enterprise_token(
    environment: Path, monkeypatch: pytest.MonkeyPatch, logins: list[str], passed: dict[str, bool]
) -> None:
    (environment / "hosts.yml").write_text(
        "".join(f"{host}:\n    user: octo\n" for host in logins), encoding="utf-8"
    )
    monkeypatch.setenv("GH_HOST", OTHER_KNOWN)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", f"{OTHER_KNOWN},{KNOWN}")
    enterprise = {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}

    for host, gets_it in passed.items():
        tokens = _passed_tokens(host, ["api", "--hostname", host, "x"])
        assert tokens == (enterprise if gets_it else set()), host


def test_a_call_naming_no_host_gets_the_tokens_of_where_it_goes(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _passed_tokens("github.com", ["api", "user"]) == {"GH_TOKEN", "GITHUB_TOKEN"}
    monkeypatch.setenv("GH_HOST", KNOWN)
    enterprise = {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    assert _passed_tokens(KNOWN, ["api", "user"]) == enterprise


def test_the_sign_in_listing_checks_each_host_alone_with_only_its_own_tokens(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no host, gh would check every host with GH_HOST's enterprise token and GH_TOKEN."""
    monkeypatch.setenv("GH_HOST", OTHER_KNOWN)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", KNOWN)
    calls: list[tuple[list[str], set[str]]] = []

    def run(argv: list[str], *, env: dict[str, str] | None = None, **_kwargs: object):
        passed = {name for name in TOKENS if name in (env if env is not None else os.environ)}
        calls.append((argv[1:], passed))
        host = argv[-1]
        # gh answers for more than the host asked about; only that host's entry counts.
        hosts = {
            name: [{"login": f"octo-{name}", "state": "success", "active": True}]
            for name in {host, "github.com"}
        }
        return 0, json.dumps({"hosts": hosts}), ""

    monkeypatch.setattr(github_resource, "_run", run)

    authenticated, accounts = github_resource._list_accounts("/workspace")

    listing = ["auth", "status", "--json", "hosts", "-h"]
    enterprise = {"GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"}
    assert calls == [
        ([*listing, "github.com"], {"GH_TOKEN", "GITHUB_TOKEN"}),
        ([*listing, OTHER_KNOWN], enterprise),
        ([*listing, KNOWN], enterprise),
        ([*listing, STORED], set()),
    ]
    assert authenticated
    assert [(account["host"], account["login"]) for account in accounts] == [
        (host, f"octo-{host}") for host in ("github.com", OTHER_KNOWN, KNOWN, STORED)
    ]


def test_an_older_gh_answers_the_sign_in_listing_from_each_hosts_status(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **_kwargs: object):
        calls.append(argv[1:])
        if "--json" in argv:
            return 1, "", "unknown flag: --json"
        return (0 if argv[-1] == STORED else 1), "", ""

    monkeypatch.setattr(github_resource, "_run", run)

    assert github_resource._list_accounts("/workspace") == (True, [])
    assert calls[1::2] == [
        ["auth", "status", "-h", "github.com"],
        ["auth", "status", "-h", STORED],
    ]


def test_a_sandbox_passes_no_environment_token_and_a_chosen_one_replaces_them(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    argv = ["api", "--hostname", STORED, "x"]
    invocation = github_resource._gh_invocation(argv, token="chosen", host=STORED)
    assert invocation.env is not None
    assert {name: invocation.env.get(name) for name in TOKENS} == {
        "GH_TOKEN": "chosen",
        "GITHUB_TOKEN": None,
        "GH_ENTERPRISE_TOKEN": "chosen",
        "GITHUB_ENTERPRISE_TOKEN": None,
    }

    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: True)
    assert _passed_tokens("github.com", ["api", "--hostname", "github.com", "x"]) == set()


def test_a_stored_logins_token_is_read_without_the_environments_enterprise_token(
    environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Otherwise gh would answer with the environment's token for another host's login."""
    envs: list[dict[str, str] | None] = []

    def run(argv: list[str], *, env: dict[str, str] | None = None, **_kwargs: object):
        envs.append(env)
        return 0, "stored-token", ""

    monkeypatch.setattr(github_resource, "_run", run)

    assert github_resource._gh_active_token("/workspace", STORED) == "stored-token"
    assert envs[0] is not None
    assert not set(TOKENS) & set(envs[0])


def test_an_environment_with_nothing_to_remove_is_inherited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (*TOKENS, "GH_REPO", "GH_DEBUG", "DEBUG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)

    invocation = github_resource._gh_invocation(
        ["api", "--hostname", "github.com", "x"], token=None, host="github.com"
    )

    assert invocation.env is None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("GH_CONFIG_DIR", "{root}/explicit"),
        ("XDG_CONFIG_HOME", "{root}/xdg"),
    ],
)
def test_a_stored_login_is_read_from_where_gh_keeps_hosts_yml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, variable: str, value: str
) -> None:
    """GH_CONFIG_DIR, else XDG_CONFIG_HOME/gh, else ~/.config/gh, as gh itself reads it."""
    for name in (
        "GH_CONFIG_DIR",
        "XDG_CONFIG_HOME",
        "GH_HOST",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    directory = Path(value.format(root=tmp_path))
    hosts = (
        directory / "hosts.yml" if variable == "GH_CONFIG_DIR" else directory / "gh" / "hosts.yml"
    )
    hosts.parent.mkdir(parents=True)
    hosts.write_text(f"{STORED}:\n    user: octo\n", encoding="utf-8")
    # The home directory's hosts.yml names a different host, and loses.
    home = tmp_path / "home" / ".config" / "gh"
    home.mkdir(parents=True)
    (home / "hosts.yml").write_text(f"{OTHER_KNOWN}:\n    user: octo\n", encoding="utf-8")

    assert not github_resource._known_github_host(STORED)
    assert github_resource._known_github_host(OTHER_KNOWN)
    monkeypatch.setenv(variable, str(directory))
    assert github_resource._known_github_host(STORED)
    assert not github_resource._known_github_host(OTHER_KNOWN)


@pytest.mark.parametrize(
    "configured",
    [
        f"{KNOWN}:8443",
        "[::1]",
        f"{KNOWN}.",
        f"https://{KNOWN}",
        f"xn--{KNOWN}",
        f"*.{KNOWN}",
        f".{KNOWN}",
    ],
)
@pytest.mark.parametrize("side", ["GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "hosts.yml"])
def test_a_set_up_host_matches_only_exactly_after_lower_casing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, side: str, configured: str
) -> None:
    """No port, bracket, trailing-dot, scheme, IDNA or wildcard handling: each fails closed."""
    for name in ("GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))

    def set_up(value: str) -> None:
        if side == "hosts.yml":
            (tmp_path / "hosts.yml").write_text(f"{value}:\n    user: octo\n", encoding="utf-8")
        else:
            monkeypatch.setenv(side, value)

    set_up(configured)
    assert not github_resource._known_github_host(KNOWN)
    assert not github_resource._known_github_host("ghe-b.example.com.example.com")
    set_up(KNOWN.upper())
    assert github_resource._known_github_host(KNOWN)
