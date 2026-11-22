"""Repository context on temporary repositories, with real Git and fake ``git`` scripts.

Remote URLs name reserved example hosts, and nothing contacts a network: tracking
refs are written directly. Global configuration lives in a temporary ``HOME``.
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

import pytest

from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
    PUSH_UNRESOLVED_PUSH_DEFAULT,
    PUSH_UNRESOLVED_PUSH_URL,
    PUSH_UNRESOLVED_REFSPEC,
    PUSH_UNRESOLVED_UNSPECIFIED,
    REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED,
    REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE,
    REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY,
    REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING,
    REPOSITORY_CONTEXT_PROBLEM_TIMEOUT,
    REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED,
    REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG,
    ResolveRepositoryContextRequest,
    ResolveRepositoryContextResponse,
)
from omnigent.host.workspace_git import context
from omnigent.host.workspace_git.context import (
    RepositoryIdentity,
    resolve_repository_context,
    sanitize_remote_url,
)
from tests.host.test_workspace_git_runner import _fake_git, _git, _repository

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)

_SECRET_URL = "https://user:secret@example.com/o/r.git?token=q#frag"
_FORBIDDEN_IN_RESPONSES = ("secret", "token=q", "frag", "user:")


@pytest.fixture(autouse=True)
def _isolated_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    return home


async def _resolve(root: Path) -> ResolveRepositoryContextResponse:
    return await resolve_repository_context(root, ResolveRepositoryContextRequest())


def _feature_repository(tmp_path: Path, name: str) -> Path:
    """A repository on branch ``feature`` with one commit."""
    root = (tmp_path / name).resolve()
    _repository(root)
    _git(root, "checkout", "-q", "-b", "feature")
    return root


def _add_remote(root: Path, name: str, url: str) -> None:
    _git(root, "remote", "add", name, url)


def _config(root: Path, *pairs: tuple[str, str]) -> None:
    for key, value in pairs:
        _git(root, "config", "--add", key, value)


def _track(root: Path, reference: str) -> None:
    """Writes a tracking ref as a fetch would, without a network."""
    _git(root, "update-ref", reference, "HEAD")


def _head(root: Path) -> str:
    return _git(root, "rev-parse", "HEAD").strip()


def _push_of(response: ResolveRepositoryContextResponse) -> tuple[str, str, str, str] | None:
    if not response.HasField("push"):
        return None
    push = response.push
    return (
        push.remote_name,
        push.branch,
        f"{push.host}/{push.owner}/{push.repo}",
        push.tracking_sha,
    )


def _serialized(response: ResolveRepositoryContextResponse) -> str:
    return str(response) + response.SerializeToString().decode("latin-1")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/app",
        "https://github.com/acme/app.git",
        "https://github.com/acme/app/",
        "https://GitHub.com/acme/app.git",
        "https://user:token@github.com/acme/app.git",
        "https://github.com:443/acme/app",
        "https://github.com/acme/app?ref=1#x",
        "ssh://git@github.com/acme/app.git",
        "ssh://git@github.com:22/acme/app.git",
        "git@github.com:acme/app.git",
        "github.com:acme/app",
    ],
)
def test_supported_remote_urls_name_host_owner_and_repo(url: str) -> None:
    assert sanitize_remote_url(url) == RepositoryIdentity(
        host="github.com", owner="acme", repo="app"
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://github.com:8443/acme/app",
        "ssh://git@ghe.example.com:7999/acme/app.git",
        "http://github.com/acme/app",
        "git://github.com/acme/app",
        "file:///srv/git/app.git",
        "/srv/git/app.git",
        "../app",
        "C:/repos/app",
        "https://gitlab.com/group/subgroup/app",
        "https://dev.azure.com/org/project/_git/app",
        "https://github.com/acme",
        "https://github.com/../app",
        "https://github.com/acme/app name",
        "https://-bad.example/acme/app",
        "git@github.com:/acme/app",
    ],
)
def test_remote_urls_outside_the_supported_forms_name_no_repository(url: str) -> None:
    assert sanitize_remote_url(url) is None


def test_credentials_query_and_fragment_are_dropped_from_the_identity() -> None:
    identity = sanitize_remote_url(_SECRET_URL)
    assert identity == RepositoryIdentity(host="example.com", owner="o", repo="r")
    assert "secret" not in repr(identity)


async def test_branch_tracking_its_upstream_reports_the_push_target(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app.git")
    _config(
        root, ("branch.feature.remote", "origin"), ("branch.feature.merge", "refs/heads/feature")
    )
    _track(root, "refs/remotes/origin/feature")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert response.branch == "feature"
    assert response.head_sha == _head(root)
    assert response.upstream_ref == "refs/remotes/origin/feature"
    assert _push_of(response) == ("origin", "feature", "github.com/acme/app", _head(root))
    assert [
        (remote.name, remote.host, remote.owner, remote.repo) for remote in response.remotes
    ] == [("origin", "github.com", "acme", "app")]


async def test_triangular_workflow_pushes_to_the_fork(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "upstream", "https://github.com/acme/app.git")
    _add_remote(root, "fork", "git@github.com:me/app.git")
    _config(
        root,
        ("branch.feature.remote", "upstream"),
        ("branch.feature.merge", "refs/heads/main"),
        ("remote.pushDefault", "fork"),
    )
    _track(root, "refs/remotes/fork/feature")

    response = await _resolve(root)

    # The upstream is main, yet the branch pushes to feature on the fork.
    assert response.upstream_ref == "refs/remotes/upstream/main"
    assert _push_of(response) == ("fork", "feature", "github.com/me/app", _head(root))
    assert len(response.remotes) == 2


@pytest.mark.parametrize(
    ("config_pairs", "tracking_ref"),
    [
        pytest.param(
            [("branch.feature.remote", "origin"), ("branch.feature.merge", "refs/heads/main")],
            None,
            id="central-simple-tracking-another-branch",
        ),
        pytest.param([], None, id="central-simple-without-upstream"),
        pytest.param(
            [("branch.feature.merge", "refs/heads/feature")],
            "refs/remotes/origin/feature",
            id="merge-without-remote",
        ),
        pytest.param(
            [
                ("push.default", "upstream"),
                ("branch.feature.remote", "origin"),
                ("branch.feature.merge", "refs/heads/main"),
                ("branch.feature.merge", "refs/heads/other"),
            ],
            "refs/remotes/origin/main",
            id="upstream-with-two-merges",
        ),
        pytest.param(
            [("push.default", "current"), ("push.autoSetupRemote", "true")],
            None,
            id="auto-setup-without-upstream",
        ),
        pytest.param(
            [("remote.origin.push", "refs/heads/main:refs/heads/main")],
            None,
            id="push-refspec-for-another-branch",
        ),
    ],
)
async def test_git_would_refuse_or_not_push_the_branch_gives_no_push_target(
    tmp_path: Path, config_pairs: list[tuple[str, str]], tracking_ref: str | None
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(root, *config_pairs)
    if tracking_ref is not None:
        _track(root, tracking_ref)

    response = await _resolve(root)

    assert not response.HasField("push")
    assert response.push_unresolved == PUSH_UNRESOLVED_UNSPECIFIED


async def test_renamed_fetch_mapping_names_the_remote_branch(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _git(root, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/cache/*")
    _config(root, ("push.default", "current"))
    _track(root, "refs/remotes/origin/cache/feature")

    response = await _resolve(root)

    assert _push_of(response) == ("origin", "feature", "github.com/acme/app", _head(root))


async def test_push_refspec_destination_is_reported_without_a_tracking_commit(
    tmp_path: Path,
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(root, ("remote.origin.push", "refs/heads/feature:refs/heads/review/feature"))

    response = await _resolve(root)

    assert _push_of(response) == ("origin", "review/feature", "github.com/acme/app", "")


@pytest.mark.parametrize(
    "push_refspecs",
    [
        pytest.param(
            ["refs/heads/feature:refs/heads/review-a", "heads/feature:refs/heads/review-b"],
            id="full-and-short-source-for-one-branch",
        ),
        pytest.param(
            ["refs/heads/feature:refs/heads/review-a", "refs/heads/feature:refs/heads/review-b"],
            id="two-full-refspecs",
        ),
        pytest.param(["feature:review/feature"], id="short-source"),
        pytest.param(["^refs/heads/wip"], id="negative-refspec"),
    ],
)
async def test_ambiguous_or_unmodeled_push_refspecs_withhold_evidence(
    tmp_path: Path, push_refspecs: list[str]
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(root, *(("remote.origin.push", refspec) for refspec in push_refspecs))

    response = await _resolve(root)

    assert not response.HasField("push")
    assert response.push_unresolved == PUSH_UNRESOLVED_REFSPEC


async def test_branch_push_remote_can_be_a_remote_with_a_slash(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _add_remote(root, "team/fork", "https://github.com/team/app")
    _config(root, ("push.default", "current"), ("branch.feature.pushRemote", "team/fork"))
    _track(root, "refs/remotes/team/fork/feature")

    push = _push_of(await _resolve(root))

    assert push is not None
    assert (push[0], push[2]) == ("team/fork", "github.com/team/app")


async def test_sole_remote_stands_in_for_origin(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "fork", "https://github.com/me/app")
    _config(root, ("push.default", "current"))

    push = _push_of(await _resolve(root))

    assert push is not None
    assert push[0] == "fork"


@pytest.mark.parametrize(
    ("push_default", "expected_remote"),
    [("fork", "fork"), ("missing", None)],
    ids=["repository-remote", "remote-the-repository-lacks"],
)
async def test_global_remote_push_default_picks_only_a_repository_remote(
    tmp_path: Path, push_default: str, expected_remote: str | None
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _add_remote(root, "fork", "https://github.com/me/app")
    _config(root, ("push.default", "current"))
    _git(root, "config", "--global", "remote.pushDefault", push_default)

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    push = _push_of(response)
    assert (push[0] if push else None) == expected_remote


@pytest.mark.parametrize("key", ["branch.sort", "branch.autoSetupMerge", "remote.pushDefault"])
async def test_global_settings_that_define_no_branch_or_remote_are_supported(
    tmp_path: Path, key: str
) -> None:
    """Common global settings such as ``branch.sort`` don't make every context unsupported."""
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _git(root, "config", "--global", key, "origin" if key == "remote.pushDefault" else "always")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert response.branch == "feature"


async def test_push_urls_name_the_repository_pushed_to(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(
        root,
        ("push.default", "current"),
        ("remote.origin.pushurl", "git@github.com:me/app.git"),
    )
    _track(root, "refs/remotes/origin/feature")

    push = _push_of(await _resolve(root))

    # The tracking ref describes the fetch side, another repository, so its commit is withheld.
    assert push == ("origin", "feature", "github.com/me/app", "")


async def test_push_urls_naming_different_repositories_withhold_evidence(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(
        root,
        ("push.default", "current"),
        ("remote.origin.pushurl", "https://github.com/me/app"),
        ("remote.origin.pushurl", "https://github.com/other/app"),
    )

    response = await _resolve(root)

    assert not response.HasField("push")
    assert response.push_unresolved == PUSH_UNRESOLVED_PUSH_URL


async def test_push_default_matching_defers_the_push(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(root, ("push.default", "matching"))

    response = await _resolve(root)

    assert not response.HasField("push")
    assert response.push_unresolved == PUSH_UNRESOLVED_PUSH_DEFAULT


async def test_detached_head_reports_the_commit_without_a_branch(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    sha = _head(root)
    _git(root, "checkout", "-q", "--detach")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert (response.branch, response.head_sha) == ("", sha)
    assert not response.HasField("push")


async def test_repository_without_commits_reports_its_branch_and_no_commit(tmp_path: Path) -> None:
    root = (tmp_path / "empty").resolve()
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert (response.branch, response.head_sha) == ("main", "")


async def test_directory_outside_any_repository_is_not_a_repository(tmp_path: Path) -> None:
    plain = (tmp_path / "plain").resolve()
    plain.mkdir()

    response = await _resolve(plain)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_NOT_A_REPOSITORY


async def test_linked_worktree_reads_the_shared_remotes(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    worktree = (tmp_path / "linked").resolve()
    _git(root, "worktree", "add", "-q", "-b", "linked", str(worktree))
    _config(root, ("push.default", "current"))
    _track(root, "refs/remotes/origin/linked")

    response = await _resolve(worktree)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert response.branch == "linked"
    push = _push_of(response)
    assert len(response.remotes) == 1
    assert push is not None and push[1] == "linked"


async def test_subdirectory_of_a_repository_reads_the_repository_wide_context(
    tmp_path: Path,
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _config(root, ("push.default", "current"))
    _track(root, "refs/remotes/origin/feature")
    subdirectory = root / "packages" / "inner"
    subdirectory.mkdir(parents=True)

    response = await _resolve(subdirectory)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert response.branch == "feature"
    assert _push_of(response) == ("origin", "feature", "github.com/acme/app", _head(root))


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(
            lambda root, home: _config(root, ("url.https://github.com/.insteadOf", "gh:")),
            id="local-url-rewrite",
        ),
        pytest.param(
            lambda root, home: _git(
                root,
                "config",
                "--global",
                "url.git@github.com:.pushInsteadOf",
                "https://github.com/",
            ),
            id="global-push-url-rewrite",
        ),
        pytest.param(
            lambda root, home: _include_remote_file(root),
            id="remote-from-included-file",
        ),
        pytest.param(
            lambda root, home: _git(
                root, "config", "--global", "remote.fork.url", "https://github.com/me/app"
            ),
            id="remote-defined-in-global-config",
        ),
    ],
)
async def test_configuration_defined_outside_the_repository_is_unsupported(
    tmp_path: Path, setup
) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "gh:acme/app")
    setup(root, tmp_path / "home")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSUPPORTED_CONFIG


def _include_remote_file(root: Path) -> None:
    extra = root / ".git" / "extra-remotes"
    extra.write_text('[remote "fork"]\n\turl = https://github.com/me/app\n')
    _git(root, "config", "--add", "include.path", str(extra))


async def test_include_for_unrelated_settings_changes_nothing(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    identity = tmp_path / "work-identity"
    identity.write_text("[user]\n\temail = dev@example.com\n")
    _git(root, "config", "--global", f"includeIf.gitdir:{root}/.path", str(identity))

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert [remote.name for remote in response.remotes] == ["origin"]


def _changing_config_body(config_file: Path, counter: Path, *, changes_once: bool) -> str:
    """Fake git whose remote owner changes after the first read, or on every read."""
    owner_line = (
        'if [ "$n" -le 1 ]; then owner=old; else owner=new; fi' if changes_once else 'owner="v$n"'
    )
    return (
        'case "$*" in\n'
        '*"config -z"*)\n'
        f'  n=$(cat "{counter}" 2>/dev/null || echo 0); n=$((n+1)); echo "$n" > "{counter}"\n'
        f"  {owner_line}\n"
        f"  printf 'file:{config_file}\\0remote.origin.url\\n"
        'https://github.com/%s/app\\0\' "$owner"\n'
        "  exit 0;;\n"
        "*symbolic-ref*) echo refs/heads/feature; exit 0;;\n"
        "*for-each-ref*) printf '\\0\\0'; exit 0;;\n"
        f"*rev-parse*) echo {'a' * 40}; exit 0;;\n"
        "esac\n"
        "exit 2\n"
    )


async def test_repository_changing_between_reads_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "config-reads"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        _changing_config_body(
            (tmp_path / "repo" / ".git" / "config").resolve(), counter, changes_once=True
        ),
    )

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_UNSPECIFIED
    assert [remote.owner for remote in response.remotes] == ["new"]
    assert counter.read_text().strip() == "4"


async def test_repository_that_keeps_changing_is_declined(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    counter = tmp_path / "config-reads"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        _changing_config_body(
            (tmp_path / "repo" / ".git" / "config").resolve(), counter, changes_once=False
        ),
    )

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_REPOSITORY_CHANGING
    assert counter.read_text().strip() == "4"


async def test_remote_urls_and_their_secrets_never_reach_the_response(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", _SECRET_URL)
    _add_remote(root, "mirror", "https://user:secret@example.org:8443/o/r.git?token=q#frag")
    _config(root, ("push.default", "current"))
    _track(root, "refs/remotes/origin/feature")

    response = await _resolve(root)

    assert _push_of(response) is not None
    assert response.unsupported_remote_count == 1
    serialized = _serialized(response)
    for leaked in (*_FORBIDDEN_IN_RESPONSES, "8443"):
        assert leaked not in serialized


async def test_remotes_are_capped_at_eight_and_unsupported_ones_counted(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    _add_remote(root, "alt", "https://github.com:8443/acme/app")
    _add_remote(root, "sub", "https://gitlab.com/group/sub/app")
    _add_remote(root, "ssh-default", "ssh://git@ghe.example.com:22/acme/app.git")
    _add_remote(root, "ssh-custom", "ssh://git@ghe.example.com:7999/acme/app.git")
    for index in range(4):
        _add_remote(root, f"extra{index}", f"https://github.com/o/r{index}")

    response = await _resolve(root)

    assert response.truncated_remotes
    assert response.unsupported_remote_count == 3
    assert [remote.name for remote in response.remotes] == [
        "origin",
        "ssh-default",
        "extra0",
        "extra1",
        "extra2",
    ]


async def test_failed_git_reports_its_category_without_echoing_git_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _fake_git(
        tmp_path,
        monkeypatch,
        f"echo 'fatal: unable to access {_SECRET_URL}' >&2\nexit 128\n",
    )

    with caplog.at_level(logging.DEBUG):
        response = await _resolve(tmp_path / "repo")

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED
    serialized = _serialized(response)
    for leaked in _FORBIDDEN_IN_RESPONSES:
        assert leaked not in serialized
        assert leaked not in caplog.text


async def test_config_snapshot_over_its_cap_is_refused(tmp_path: Path) -> None:
    root = _feature_repository(tmp_path, "repo")
    _add_remote(root, "origin", "https://github.com/acme/app")
    with (root / ".git" / "config").open("a") as config_file:
        config_file.write('[branch "feature"]\n\tdescription = ' + "x" * (300 * 1024) + "\n")

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_GIT_FAILED


async def test_a_slow_git_ends_at_the_shared_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(context, "_RESOLVE_SECONDS", 0.5)
    root = _fake_git(tmp_path, monkeypatch, "sleep 30\n")

    started = time.monotonic()
    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_TIMEOUT
    assert time.monotonic() - started < 5


async def test_missing_git_is_reported_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _feature_repository(tmp_path, "repo")
    empty_path = tmp_path / "no-tools"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    response = await _resolve(root)

    assert response.problem == REPOSITORY_CONTEXT_PROBLEM_GIT_UNAVAILABLE
