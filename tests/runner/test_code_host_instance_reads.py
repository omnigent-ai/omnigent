"""A runner serving a code host instance: its CLI, its environment, and reads only."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.git_providers import load_facet
from omnigent.git_providers.code_host_instances import load_code_host_instances
from omnigent.runner import code_host_instances, github_resource, pr_resource
from omnigent.runner.code_host_instances import (
    CODE_HOST_INSTANCE_HOSTS_ENV_VAR,
    CODE_HOST_INSTANCES_FILE_ENV_VAR,
    active_code_host_instances,
    provider_display_for,
)
from omnigent.runner.git_providers import offered_capabilities
from omnigent.runner.session_prs import PullRequestRef

INSTANCE_HOST = "code.example.test"
SYNTHETIC_ENVIRONMENT = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/home/example",
    "LANG": "C.UTF-8",
    "EXAMPLE_PRIVATE": "not-for-the-cli",
    "GH_TOKEN": "synthetic-gh-token",
    "GITHUB_TOKEN": "synthetic-github-token",
}


@pytest.fixture(autouse=True)
def _no_process_wide_instances() -> Iterator[None]:
    """Start and end each test with no host-served or cached runner instances."""
    code_host_instances.serve_in_this_process(None)
    code_host_instances._runner_instances.cache_clear()
    yield
    code_host_instances.serve_in_this_process(None)
    code_host_instances._runner_instances.cache_clear()


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "example-cli"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def serve_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: Path) -> Path:
    """Serve one instance, as a host that turned instances on starts its runner."""
    config = tmp_path / "code_host_instances.toml"
    config.write_text(
        "[[instance]]\n"
        'id = "internal"\nprovider = "github"\ndisplay_name = "Internal code host"\n'
        f'hosts = ["{INSTANCE_HOST}"]\n'
        f'cli = ["{cli}", "--fixed"]\n'
        'env = ["HOME"]\nenv_set = { LANG = "en_US.UTF-8" }\nauth = "cli"\n'
        # Writes listed here are still never offered.
        'capabilities = ["line_counts", "review_threads", "comment_posting", "merging"]\n'
    )
    monkeypatch.setattr(os, "environ", {**SYNTHETIC_ENVIRONMENT})
    os.environ[CODE_HOST_INSTANCES_FILE_ENV_VAR] = str(config)
    os.environ[CODE_HOST_INSTANCE_HOSTS_ENV_VAR] = INSTANCE_HOST
    code_host_instances._runner_instances.cache_clear()
    return cli


@pytest.fixture
def gh_starts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    starts: list[dict[str, Any]] = []

    def run(
        argv: list[str],
        *,
        cwd: str,
        timeout: float,
        env: dict[str, str] | None = None,
        log_as: str | None = None,
    ) -> tuple[int | None, str, str]:
        starts.append({"argv": argv, "env": env})
        if "view" in argv:
            return 0, json.dumps({"title": "Instance read", "state": "OPEN"}), ""
        return 0, "{}", ""

    monkeypatch.setattr(github_resource, "_run", run)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)
    return starts


def test_an_instance_host_runs_the_instances_cli_in_its_pinned_environment(
    serve_instance: Path, gh_starts: list[dict[str, Any]]
) -> None:
    with github_resource.gh_target_host_scope("Code.Example.Test"):
        github_resource._gh(["api", "user"], cwd="/workspace", token="chosen-account-token")

    assert gh_starts == [
        {
            "argv": [str(serve_instance), "--fixed", "api", "user"],
            "env": {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "HOME": "/home/example",
                "LANG": "en_US.UTF-8",
                "GH_HOST": INSTANCE_HOST,
            },
        }
    ]


def test_a_host_named_in_the_argv_routes_the_same_way(
    serve_instance: Path, gh_starts: list[dict[str, Any]]
) -> None:
    github_resource._gh(["api", "--hostname", INSTANCE_HOST, "user"], cwd="/workspace")
    github_resource._gh(["pr", "view", "7", "-R", f"{INSTANCE_HOST}/acme/repo"], cwd="/workspace")

    assert [start["argv"][0] for start in gh_starts] == [str(serve_instance)] * 2


def test_other_hosts_still_run_gh_as_before(
    serve_instance: Path, gh_starts: list[dict[str, Any]]
) -> None:
    with github_resource.gh_target_host_scope("github.com"):
        github_resource._gh(["api", "user"], cwd="/workspace")
    github_resource._gh(["api", "user"], cwd="/workspace")

    assert gh_starts == [
        {"argv": ["gh", "api", "user"], "env": None},
        {"argv": ["gh", "api", "user"], "env": None},
    ]


@pytest.mark.parametrize(
    "read_first", [True, False], ids=["after-first-read", "before-first-read"]
)
def test_a_cli_that_disappeared_starts_nothing_and_never_falls_back_to_gh(
    serve_instance: Path,
    gh_starts: list[dict[str, Any]],
    caplog: pytest.LogCaptureFixture,
    read_first: bool,
) -> None:
    if read_first:
        assert code_host_instances.instance_for_host(INSTANCE_HOST) is not None
    serve_instance.unlink()

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        result = github_resource._gh(["api", "user"], cwd="/workspace")

    assert result == (None, "", "code host instance CLI unavailable")
    assert gh_starts == []
    expected_id = "internal" if read_first else "unknown"
    assert f"github code host instance {expected_id} api: CLI unavailable" in caplog.text
    assert str(serve_instance) not in caplog.text


def test_sign_in_on_an_instance_host_asks_who_the_cli_reads_as(
    serve_instance: Path, gh_starts: list[dict[str, Any]]
) -> None:
    assert github_resource._gh_signed_in_to_host("/workspace", INSTANCE_HOST) is True
    assert gh_starts[0]["argv"] == [
        str(serve_instance),
        "--fixed",
        "api",
        "user",
        "--hostname",
        INSTANCE_HOST,
    ]


def test_an_instance_offers_only_the_reads_its_file_lists(serve_instance: Path) -> None:
    facet = load_facet("github", "pull_requests")
    assert facet is not None

    offered = offered_capabilities(facet, INSTANCE_HOST).to_json()

    assert {name for name, on in offered.items() if on} == {"line_counts", "review_threads"}
    assert offered_capabilities(facet, "github.com") is facet.capabilities
    assert offered_capabilities(facet, None) is facet.capabilities


def test_an_instance_is_shown_by_its_own_name(serve_instance: Path) -> None:
    assert provider_display_for("github", INSTANCE_HOST) == {
        "id": "github",
        "display_name": "Internal code host",
        "request_name": "pull request",
        "number_prefix": "#",
    }
    assert (provider_display_for("github", "github.com") or {}).get("display_name") == "GitHub"
    assert (provider_display_for("gitlab", INSTANCE_HOST) or {}).get("display_name") == "GitLab"


def test_a_tracked_instance_pr_refuses_writes_by_the_instances_name(
    serve_instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = PullRequestRef.from_url(f"https://{INSTANCE_HOST}/acme/repo/pull/7")
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda _session, _url: reference)

    with pytest.raises(ValueError, match="Editing Internal code host pull request labels"):
        pr_resource._label_target("session", reference.url)


@pytest.mark.parametrize("value", [None, "", "relative/code_host_instances.toml"])
def test_a_runner_its_host_named_no_file_serves_no_instances(
    monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    environment = dict(SYNTHETIC_ENVIRONMENT)
    if value is not None:
        environment[CODE_HOST_INSTANCES_FILE_ENV_VAR] = value
    monkeypatch.setattr(os, "environ", environment)
    code_host_instances._runner_instances.cache_clear()
    try:
        assert active_code_host_instances().instances == ()
        assert code_host_instances.instance_for_host(INSTANCE_HOST) is None
    finally:
        code_host_instances._runner_instances.cache_clear()


def test_the_runner_reads_its_file_once(
    serve_instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = active_code_host_instances()
    Path(os.environ[CODE_HOST_INSTANCES_FILE_ENV_VAR]).write_text("not toml [[")

    assert active_code_host_instances() is first
    assert code_host_instances.instance_for_host(INSTANCE_HOST) is not None


def test_a_tracked_instance_pr_is_read_with_no_plain_gh_installed(
    serve_instance: Path, gh_starts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The instance's CLI is enough: no ``gh`` on ``PATH`` is needed for its PRs or titles."""
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name, *_a, **_k: None)
    reference = PullRequestRef.from_url(f"https://{INSTANCE_HOST}/acme/repo/pull/7")

    info = github_resource._reference_info("/workspace", reference)

    assert info["gh_available"] is True
    assert info["pr"]["title"] == "Instance read"
    assert gh_starts and {start["argv"][0] for start in gh_starts} == {str(serve_instance)}
    assert github_resource._gh_installed() is True
    assert github_resource._cli_available_for("github.com") is False


def test_no_instance_and_no_gh_still_reads_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name, *_a, **_k: None)
    reference = PullRequestRef.from_url("https://github.com/acme/repo/pull/7")

    assert github_resource._reference_info("/workspace", reference)["gh_available"] is False
    assert github_resource._gh_installed() is False


def test_an_instance_host_is_never_asked_for_a_token(
    serve_instance: Path, gh_starts: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even with a stale account preference, the instance's CLI keeps its token."""
    monkeypatch.setattr(
        github_resource._config, "github_account_preference", lambda _repo: "someone"
    )
    reference = PullRequestRef.from_url(f"https://{INSTANCE_HOST}/acme/repo/pull/7")

    assert github_resource._pr_token("/workspace", reference) is None
    assert github_resource._gh_auth_token("/workspace", "someone", INSTANCE_HOST) is None
    github_resource._reference_info("/workspace", reference)

    assert not [start for start in gh_starts if "token" in start["argv"]]
    assert not [start for start in gh_starts if start["argv"][-3:-1] == ["auth", "status"]]


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (subprocess.TimeoutExpired(["x"], 1.0), "timed out"),
        (OSError("could not start --fixed private-detail"), "could not start"),
    ],
    ids=["timeout", "spawn-error"],
)
def test_an_instance_cli_failure_logs_only_the_instance_and_operation(
    serve_instance: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
    outcome: str,
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(github_resource.subprocess, "run", fail)

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        result = github_resource._gh(
            ["pr", "view", "7", "--json", "title"], cwd="/private/workspace"
        )

    assert result[0] is None
    assert f"github code host instance internal pr view: {outcome}" in caplog.text
    for detail in (str(serve_instance), "--fixed", "/private/workspace", "private-detail"):
        assert detail not in caplog.text
        assert detail not in result[2]


def test_a_plain_gh_failure_still_logs_as_before(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["gh"], 1.0)

    monkeypatch.setattr(github_resource.subprocess, "run", fail)

    github_resource._gh(["api", "user"], cwd="/workspace")

    assert "['gh', 'api', 'user'] in /workspace timed out" in caplog.text


def test_a_host_that_served_instances_leaves_none_behind_for_a_runner(
    serve_instance: Path, gh_starts: list[dict[str, Any]], tmp_path: Path
) -> None:
    """After a host stops serving in process, a runner reads with its own instances."""
    other_cli = tmp_path / "other-cli"
    other_cli.write_text("#!/bin/sh\nexit 0\n")
    other_cli.chmod(0o755)
    other_file = tmp_path / "other.toml"
    other_file.write_text(
        '[[instance]]\nid = "other"\nprovider = "github"\ndisplay_name = "Other"\n'
        f'hosts = ["{INSTANCE_HOST}"]\ncli = ["{other_cli}"]\nauth = "cli"\n'
    )
    host_set = load_code_host_instances(
        other_file, known_capabilities=code_host_instances.CAPABILITY_NAMES, host_environment={}
    )
    code_host_instances.serve_in_this_process(host_set)
    code_host_instances.serve_in_this_process(None)

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        github_resource._gh(["api", "user"], cwd="/workspace")

    assert gh_starts[0]["argv"][0] == str(serve_instance)
