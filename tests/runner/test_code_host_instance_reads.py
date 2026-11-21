"""A runner serving a code host instance: its CLI, its environment, and reads only."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


@pytest.fixture(autouse=True)
def _no_process_wide_routing(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Start and end each test as a process with no routing decided yet."""
    monkeypatch.setattr(code_host_instances, "_taken", None)
    monkeypatch.setattr(code_host_instances, "_host_routing", None)
    code_host_instances._runner_routing.cache_clear()
    yield
    code_host_instances._runner_routing.cache_clear()


def _start_runner(environment: dict[str, str]) -> None:
    """Start as a runner whose host set *environment*: it takes its settings first."""
    os.environ.clear()
    os.environ.update(environment)
    code_host_instances.take_runner_settings(os.environ)


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
    monkeypatch.setattr(os, "environ", {})
    _start_runner(
        {
            **SYNTHETIC_ENVIRONMENT,
            CODE_HOST_INSTANCES_FILE_ENV_VAR: str(config),
            CODE_HOST_INSTANCE_HOSTS_ENV_VAR: INSTANCE_HOST,
        }
    )
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


def test_the_cli_gets_only_values_the_host_set_when_the_runner_started(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cli: Path,
    gh_starts: list[dict[str, Any]],
) -> None:
    """A session-forwarded or later value for a name the file lists never reaches the CLI."""
    config = tmp_path / "host.toml"
    config.write_text(
        '[[instance]]\nid = "internal"\nprovider = "github"\ndisplay_name = "Internal"\n'
        f'hosts = ["{INSTANCE_HOST}"]\ncli = ["{cli}"]\nauth = "cli"\n'
        'env = ["HOME", "HTTPS_PROXY"]\n'
    )
    monkeypatch.setattr(os, "environ", {})
    _start_runner(
        {
            **SYNTHETIC_ENVIRONMENT,
            # The host forwarded HTTPS_PROXY; a session forwarded HOME.
            "HTTPS_PROXY": "http://proxy.example.test:3128",
            "HOME": "/session/home",
            "OMNIGENT_RUNNER_SESSION_ENV_NAMES": "HOME",
            "RUNNER_INITIAL_AUTH_TOKEN": "synthetic-runner-token",
            CODE_HOST_INSTANCES_FILE_ENV_VAR: str(config),
            CODE_HOST_INSTANCE_HOSTS_ENV_VAR: INSTANCE_HOST,
        }
    )
    os.environ["HTTPS_PROXY"] = "http://later.example.test:3128"

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        github_resource._gh(["api", "user"], cwd="/workspace")

    [start] = gh_starts
    assert start["env"]["HTTPS_PROXY"] == "http://proxy.example.test:3128"
    assert "HOME" not in start["env"]
    assert "synthetic-runner-token" not in json.dumps(start["env"])


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
    monkeypatch.setattr(os, "environ", {})
    _start_runner(environment)

    assert active_code_host_instances().instances == ()
    assert code_host_instances.instance_for_host(INSTANCE_HOST) is None


def test_the_runner_decides_its_routing_once(serve_instance: Path, tmp_path: Path) -> None:
    first = code_host_instances.code_host_routing()
    (tmp_path / "code_host_instances.toml").write_text("not toml [[")

    assert code_host_instances.code_host_routing() is first
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


@pytest.mark.parametrize(
    ("failure", "outcome"),
    [
        (subprocess.TimeoutExpired(["x"], 1.0), "timed out"),
        (OSError("could not start --fixed private-detail"), "could not start"),
    ],
    ids=["timeout", "spawn-error"],
)
def test_an_instance_gone_between_building_and_running_its_call_still_logs_redacted(
    serve_instance: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: BaseException,
    outcome: str,
) -> None:
    """A lookup that changed after the command was built can't bring it into the logs."""
    build = github_resource._gh_invocation

    def build_then_disconnect(*args: Any, **kwargs: Any) -> github_resource.GhInvocation:
        invocation = build(*args, **kwargs)
        monkeypatch.setattr(code_host_instances, "instance_for_host", lambda _host: None)
        return invocation

    def fail(*_args: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(github_resource, "_gh_invocation", build_then_disconnect)
    monkeypatch.setattr(github_resource.subprocess, "run", fail)

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        result = github_resource._gh(["api", "user"], cwd="/private/workspace")

    assert result[0] is None
    assert f"github code host instance internal api: {outcome}" in caplog.text
    for detail in (str(serve_instance), "--fixed", "/private/workspace", "private-detail"):
        assert detail not in caplog.text
        assert detail not in result[2]


def test_a_built_command_that_isnt_plain_gh_is_never_logged_by_its_command(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Backstop: an invocation missing its log name still logs no command."""
    monkeypatch.setattr(
        github_resource,
        "_gh_invocation",
        lambda argv, **_: github_resource.GhInvocation(
            argv=["/opt/example/cli", "--fixed", *argv], env={"PATH": "/usr/bin"}
        ),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["x"], 1.0)

    monkeypatch.setattr(github_resource.subprocess, "run", fail)

    github_resource._gh(["api", "user"], cwd="/private/workspace")

    assert "github code host instance unknown api: timed out" in caplog.text
    assert "/opt/example/cli" not in caplog.text and "--fixed" not in caplog.text


def test_a_plain_gh_failure_still_logs_as_before(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail(*_args: object, **_kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["gh"], 1.0)

    monkeypatch.setattr(github_resource.subprocess, "run", fail)

    github_resource._gh(["api", "user"], cwd="/workspace")

    assert "['gh', 'api', 'user'] in /workspace timed out" in caplog.text


def _instance_file(path: Path, cli: str) -> Path:
    path.write_text(
        '[[instance]]\nid = "internal"\nprovider = "github"\ndisplay_name = "Internal"\n'
        f'hosts = ["{INSTANCE_HOST}"]\ncli = ["{cli}"]\nauth = "cli"\n'
        'capabilities = ["line_counts"]\n'
    )
    return path


@pytest.fixture(params=["host-process", "cli-gone", "session-path"])
def denied_host(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: Path
) -> str:
    """A process where the declared instance host runs nothing, and why."""
    monkeypatch.setattr(os, "environ", {})
    if request.param == "host-process":
        os.environ.update(SYNTHETIC_ENVIRONMENT)
        code_host_instances.deny_in_this_process([INSTANCE_HOST])
        return "code_host_instance_needs_runner"
    environment = {**SYNTHETIC_ENVIRONMENT, CODE_HOST_INSTANCE_HOSTS_ENV_VAR: INSTANCE_HOST}
    if request.param == "cli-gone":
        config = _instance_file(tmp_path / "host.toml", str(tmp_path / "missing-cli"))
    else:
        # A bare name never resolves on a PATH a session forwarded.
        config = _instance_file(tmp_path / "host.toml", cli.name)
        environment["PATH"] = str(cli.parent)
        environment["OMNIGENT_RUNNER_SESSION_ENV_NAMES"] = "PATH"
    environment[CODE_HOST_INSTANCES_FILE_ENV_VAR] = str(config)
    _start_runner(environment)
    return "code_host_instance_cli_unavailable"


def test_a_declared_host_that_runs_nothing_never_reaches_plain_gh(
    denied_host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No read, sign-in, token, account listing or capability falls back to ``gh``."""
    starts: list[list[str]] = []

    def run(argv: list[str], **_kwargs: Any) -> tuple[int | None, str, str]:
        if argv[0] == "git":
            if "--get-regexp" in argv:
                return 0, f"remote.origin.url\nhttps://{INSTANCE_HOST}/owner/repo.git\0", ""
            return 0, "feature\n", ""
        starts.append(argv)
        return 0, "{}", ""

    monkeypatch.setattr(github_resource, "_run", run)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name, *_a, **_k: "/usr/bin/gh")
    monkeypatch.setattr(
        github_resource, "_list_accounts", lambda *_a: pytest.fail("listed accounts")
    )
    monkeypatch.setattr(
        github_resource, "_account_token_for", lambda *_a: pytest.fail("asked for a token")
    )
    reference = PullRequestRef.from_url(f"https://{INSTANCE_HOST}/acme/repo/pull/7")
    facet = load_facet("github", "pull_requests")
    assert facet is not None
    unavailable = {"object": "session.github.info", "available": False, "reason": denied_host}

    with github_resource.gh_target_host_scope(INSTANCE_HOST):
        assert github_resource._gh(["api", "user"], cwd="/workspace")[0] is None
    github_resource._gh(["pr", "view", "7", "-R", f"{INSTANCE_HOST}/acme/repo"], cwd="/w")
    assert github_resource._pr_json("/workspace", reference, "title") is None

    assert github_resource._reference_info("/workspace", reference) == unavailable
    assert github_resource._workspace_github_info("/workspace") == unavailable
    assert github_resource._pr_token("/workspace", reference) is None
    assert github_resource._cli_available_for(INSTANCE_HOST) is False
    assert not any(offered_capabilities(facet, INSTANCE_HOST).to_json().values())
    assert starts == []


def test_the_host_process_decides_its_denial_once(cli: Path) -> None:
    code_host_instances.deny_in_this_process([INSTANCE_HOST.upper()])
    code_host_instances.deny_in_this_process([])

    routing = code_host_instances.code_host_routing()
    assert routing.route_for(INSTANCE_HOST) == code_host_instances.CodeHostRoute(
        None, "needs_runner"
    )
    assert routing.instances.instances == ()
    assert routing.route_for("github.com") is None


def test_a_runners_children_never_see_its_instance_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cli: Path
) -> None:
    """Every process a runner starts, whatever env it passes, inherits neither setting."""
    config = _instance_file(tmp_path / "host.toml", str(cli))
    # A fully synthetic process environment, so nothing real can reach the child.
    for name in list(os.environ):
        monkeypatch.delenv(name)
    for name, value in {
        **SYNTHETIC_ENVIRONMENT,
        CODE_HOST_INSTANCES_FILE_ENV_VAR: str(config),
        CODE_HOST_INSTANCE_HOSTS_ENV_VAR: INSTANCE_HOST,
    }.items():
        monkeypatch.setenv(name, value)

    code_host_instances.take_runner_settings()

    # The child reports presence only, never a value.
    probe = (
        "import json, os, sys\n"
        "names, path = sys.argv[1:3], sys.argv[3]\n"
        "print(json.dumps({'names': [n in os.environ for n in names],"
        " 'path': path in os.environ.values()}))"
    )
    names = [CODE_HOST_INSTANCES_FILE_ENV_VAR, CODE_HOST_INSTANCE_HOSTS_ENV_VAR]
    child = subprocess.run(
        [sys.executable, "-I", "-c", probe, *names, str(config)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout) == {"names": [False, False], "path": False}
    assert code_host_instances.instance_for_host(INSTANCE_HOST) is not None


def test_a_host_process_drops_the_runner_settings_it_inherited() -> None:
    inherited = {
        **SYNTHETIC_ENVIRONMENT,
        CODE_HOST_INSTANCES_FILE_ENV_VAR: "/elsewhere/code_host_instances.toml",
        CODE_HOST_INSTANCE_HOSTS_ENV_VAR: INSTANCE_HOST,
    }

    code_host_instances.discard_runner_settings(inherited)

    assert inherited == SYNTHETIC_ENVIRONMENT
    assert code_host_instances.code_host_routing() is code_host_instances.NO_ROUTING


def test_a_workspace_whose_remote_is_on_an_instance_host_reads_through_its_cli(
    serve_instance: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no tracked PR, the remote's host routes the workspace's reads."""
    starts: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> tuple[int | None, str, str]:
        if argv[0] == "git":
            if "--get-regexp" in argv:
                return 0, f"remote.origin.url\nhttps://{INSTANCE_HOST}/owner/repo.git\0", ""
            return 0, "feature\n", ""
        starts.append({"argv": argv, "env": kwargs.get("env")})
        return 0, json.dumps({"number": 7, "title": "Instance PR", "state": "OPEN"}), ""

    monkeypatch.setattr(github_resource, "_run", run)
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda: False)
    monkeypatch.setattr(
        github_resource, "_account_token_for", lambda *_a: pytest.fail("asked for a token")
    )

    info = github_resource._workspace_github_info("/workspace")

    assert starts and all(start["argv"][0] == str(serve_instance) for start in starts)
    assert all(start["env"]["GH_HOST"] == INSTANCE_HOST for start in starts)
    assert info["pr"]["title"] == "Instance PR"
