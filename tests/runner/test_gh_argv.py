"""Which host each ``gh`` command line the panel runs names, and which ones are refused."""

from __future__ import annotations

import pytest

from omnigent.runner.gh_argv import GhArgvRefused, GhTarget, classify

HOST = "ghe.example.com"
OTHER = "other.example.com"


@pytest.mark.parametrize(
    ("argv", "host", "from_checkout"),
    [
        (["api", "--hostname", HOST, "repos/o/r/pulls/1"], HOST, False),
        (["api", f"--hostname={HOST}", "repos/o/r/pulls/1"], HOST, False),
        (["api", "repos/o/r/commits/abc/pulls"], None, False),
        (["api", "repos/{owner}/{repo}/pulls/1/files?per_page=100&page=1"], None, True),
        (["api", "graphql", "--hostname", HOST, "-f", "query={ viewer { login } }"], HOST, False),
        # Field values are free text: a URL in a label or a query isn't a host.
        (
            ["api", "--hostname", HOST, "repos/o/r/labels", "-f", f"labels[]=https://{OTHER}/x"],
            HOST,
            False,
        ),
        (["api", "--hostname", HOST, "graphql", "-F", f"body=see https://{OTHER}"], HOST, False),
        (
            ["api", "--hostname", HOST, "-X", "PATCH", "repos/o/r/pulls/1", "--input", "/tmp/x"],
            HOST,
            False,
        ),
        (
            ["api", "--hostname", HOST, "-H", "Accept: application/vnd.github.diff", "x"],
            HOST,
            False,
        ),
        (["api", "--hostname", HOST, "graphql", "--cache", "30s", "--jq", ".data"], HOST, False),
        # The attachment upload names its host in the URL; github.com's is uploads.github.com.
        (
            [
                "api",
                "-X",
                "POST",
                f"https://{HOST}/api/uploads/user-attachments/assets?name=a",
                "-H",
                "Content-Type: application/octet-stream",
                "--input",
                "/tmp/a",
            ],
            HOST,
            False,
        ),
        (
            [
                "api",
                "-X",
                "POST",
                "https://uploads.github.com/user-attachments/assets?name=a",
                "--input",
                "/tmp/a",
            ],
            "github.com",
            False,
        ),
        (["auth", "status"], None, False),
        (["auth", "status", "--json", "hosts"], None, False),
        (["auth", "status", "-h", HOST], HOST, False),
        (["auth", "token", "-h", HOST], HOST, False),
        (["auth", "token", "--user", "octo", "-h", HOST], HOST, False),
        (["repo", "view", f"{HOST}/o/r", "--json", "defaultBranchRef"], HOST, False),
        (["repo", "view", f"https://{HOST}/o/r", "--json", "name"], HOST, False),
        (["repo", "view", "o/r", "--json", "name"], None, False),
        (["repo", "view", "--json", "nameWithOwner,url"], None, True),
        (["repo", "set-default", "--view"], None, True),
        (["repo", "set-default", "origin"], None, True),
        (["repo", "set-default", "o/r"], None, False),
        (["pr", "view", "7", "-R", f"{HOST}/o/r", "--json", "number"], HOST, False),
        (["pr", "view", "7", f"-R{HOST}/o/r"], HOST, False),
        (["pr", "view", "7", f"--repo={HOST}/o/r"], HOST, False),
        (["pr", "view", "7", "-R", "o/r"], None, False),
        (["pr", "view", f"https://{HOST}/o/r/pull/7", "--json", "title"], HOST, False),
        (["pr", "view", "--json", "number"], None, True),
        (["pr", "diff", "7"], None, True),
        (["pr", "diff", "7", "-R", f"{HOST}/o/r"], HOST, False),
        (
            ["pr", "merge", "7", "-R", f"{HOST}/o/r", "--squash", "--match-head-commit", "abc"],
            HOST,
            False,
        ),
        (["pr", "merge", "7", "-R", f"{HOST}/o/r", "--disable-auto"], HOST, False),
        (
            [
                "pr",
                "list",
                "-R",
                f"{HOST}/o/r",
                "--head",
                "b",
                "--state",
                "all",
                "--limit",
                "5",
                "--json",
                "number",
            ],
            HOST,
            False,
        ),
        (["pr", "close", "7", "-R", f"{HOST}/o/r"], HOST, False),
        (["pr", "reopen", "7", "-R", f"{HOST}/o/r"], HOST, False),
        (["pr", "ready", "7", "-R", f"{HOST}/o/r", "--undo"], HOST, False),
        (
            [
                "label",
                "list",
                "-R",
                f"{HOST}/o/r",
                "--search=-x",
                "--json",
                "name",
                "--limit",
                "3",
            ],
            HOST,
            False,
        ),
        # Hosts are compared lower-cased, as the scope is.
        (["api", "--hostname", "GHE.Example.com", "x"], HOST, False),
    ],
)
def test_each_command_the_panel_runs_names_the_host_gh_sends_to(
    argv: list[str], host: str | None, from_checkout: bool
) -> None:
    target = classify(argv)

    assert target == GhTarget(command=target.command, host=host, from_checkout=from_checkout)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["pr", "create", "--fill"],
        ["pr", "checkout", "7"],
        ["issue", "view", f"https://{HOST}/o/r/issues/1"],
        ["repo", "clone", "o/r"],
        ["browse"],
        ["extension", "exec", "x"],
        ["auth", "login", "-h", HOST],
        ["auth", "status", "--show-token"],
        ["auth", "status", "-t"],
        ["auth", "status", "extra"],
        # gh's pagination follows Link URLs to any host it holds a token for.
        ["api", "--hostname", HOST, "--paginate", "repos/o/r/pulls"],
        ["api", "--hostname", HOST, "--slurp", "repos/o/r/pulls"],
        ["api", "--hostname", HOST, "--verbose", "x"],
        ["api", "--hostname", HOST, "-i", "x"],
        ["api", "--hostname", HOST, "x", "y"],
        ["api", "--hostname"],
        ["api", "--hostname=", "x"],
        ["api", "--hostname", "[::1]", "x"],
        ["api", "--hostname", f"{HOST}.", "x"],
        ["api", "--hostname", f"{HOST}:8443", "x"],
        ["api", "--hostname", "localhost", "x"],
        # A full-URL endpoint must be https, with no userinfo or port, on the scope's host.
        ["api", f"http://{HOST}/api/v3/x"],
        ["api", f"HTTPS://{HOST}/api/v3/x"],
        ["api", f"https://user@{HOST}/x"],
        ["api", f"https://{HOST}:8443/x"],
        ["api", "ssh://h.example.com/x"],
        ["api", "--hostname", HOST, f"https://{OTHER}/api/v3/x"],
        ["pr", "view", f"https://{OTHER}/o/r/pull/1", "-R", f"{HOST}/o/r"],
        ["pr", "view", "feature-branch"],
        ["pr", "view", f"http://{HOST}/o/r/pull/1"],
        ["pr", "view", "7", "8"],
        ["pr", "view", "7", "-R"],
        ["pr", "view", "--", "7"],
        ["pr", "view", "7", "-R", f"{HOST}/o/r/extra"],
        ["pr", "view", "7", "-R", f"git@{HOST}:o/r"],
        ["pr", "view", "7", "-R", "o//r"],
        ["pr", "merge", "7", "-R", f"{HOST}/o/r", "--admin"],
        ["pr", "merge", "7", "-R", f"{HOST}/o/r", "--auto"],
        ["pr", "list", "extra"],
        ["repo", "view", f"{HOST}/o/r/x"],
        ["repo", "view", f"{HOST}:o/r"],
        ["repo", "set-default", "my.remote"],
        ["label", "list", "-R", f"{HOST}/o/r", "--web"],
    ],
)
def test_anything_the_panel_doesnt_run_or_cant_classify_is_refused(argv: list[str]) -> None:
    with pytest.raises(GhArgvRefused):
        classify(argv)


@pytest.mark.parametrize(
    "argv",
    [
        # gh uses the last of a repeated flag; even the same value twice is refused.
        ["api", "--hostname", HOST, "--hostname", OTHER, "x"],
        ["api", "--hostname", HOST, f"--hostname={HOST}", "x"],
        ["auth", "token", "-h", HOST, "--hostname", HOST],
        ["pr", "view", "7", "-R", f"{HOST}/o/r", "-R", f"{OTHER}/o/r"],
        ["pr", "view", "7", "-R", "o/r", f"--repo={HOST}/o/r"],
        ["label", "list", f"-R{HOST}/o/r", "--repo", f"{HOST}/o/r"],
    ],
)
def test_a_repeated_host_or_repository_flag_is_refused(argv: list[str]) -> None:
    with pytest.raises(GhArgvRefused):
        classify(argv)
