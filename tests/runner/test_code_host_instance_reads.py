"""A runner serving a code host instance: its CLI, its environment, and reads only."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from omnigent.git_providers import load_facet
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


@pytest.fixture
def cli(tmp_path: Path) -> Path:
    path = tmp_path / "bin" / "example-cli"
    path.parent.mkdir()
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def serve_instance(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: Path) -> Iterator[Path]:
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
    yield cli
    code_host_instances._runner_instances.cache_clear()


@pytest.fixture
def gh_starts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    starts: list[dict[str, Any]] = []

    def run(
        argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
    ) -> tuple[int | None, str, str]:
        starts.append({"argv": argv, "env": env})
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
    assert f"code host instance {expected_id} can't run its CLI" in caplog.text
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
