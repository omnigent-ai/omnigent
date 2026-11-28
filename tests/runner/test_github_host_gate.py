"""The gate every ``gh`` call passes before it starts: the host it names must be its scope's.

A pull request URL can name any host, and ``gh`` sends a token to the host it's told to, so
a refused call starts no process at all.
"""

from __future__ import annotations

import ast
import json
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
        (UNKNOWN, ["auth", "status", "--json", "hosts"]),
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
        ["auth", "status", "--json", "hosts"],
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
        ["auth", "status", "--json", "hosts"],
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


def test_a_checkout_with_no_single_github_host_runs_nothing_but_the_sign_in_listing(
    started: list[list[str]], tmp_path: Path
) -> None:
    root = _checkout(tmp_path, {"origin": f"https://{UNKNOWN}/o/r.git"})

    with github_resource.gh_target_host_scope(github_resource.CHECKOUT_SCOPE):
        github_resource._gh(["pr", "view", "--json", "number"], cwd=root)
        github_resource._gh(["auth", "status", "--json", "hosts"], cwd=root)

    assert started == [["gh", "auth", "status", "--json", "hosts"]]
