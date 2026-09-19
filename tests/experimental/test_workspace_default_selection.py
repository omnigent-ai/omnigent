"""UI default branches select exact profiles without changing retained workspaces."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import click
import pytest

from omnigent.experimental.workspace_profiles import cli, defaults
from omnigent.experimental.workspace_profiles.launcher import WorkspaceProfileLauncher
from omnigent.onboarding.sandboxes import agent_sandbox_warm_pool as warm
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxLaunchRequest
from omnigent.server import app as server_app
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from tests.experimental.test_workspace_profiles import (
    _BASE,
    _HANDLE,
    _launcher,
    _profile,
    _repos,
    _request,
    _resources,
    _seed,
    _wire_api,
)
from tests.onboarding.sandboxes.test_agent_sandbox_warm_pool import (
    clean_environment as clean_environment,
)
from tests.onboarding.sandboxes.test_agent_sandbox_warm_pool import (
    sdk as sdk,
)


def test_ui_defaults_resolve_for_owner_and_claim_matching_pool(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(_seed(), _seed("web"))
    resolver = MagicMock(return_value={repo.url: repo.branch for repo in profile.manifest.repos})
    launcher = _launcher(profile, default_branch_resolver=resolver)
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    custom = _wire_api(monkeypatch, launcher, _resources(spec))
    request = SandboxLaunchRequest(
        owner="session-owner@example.invalid",
        repos=(
            RepoWorkspace("git@github.com:ACME/web.git", None, "web"),
            RepoWorkspace("https://github.com/acme/api", None, "api"),
        ),
        agent_name="codex-native-ui",
    )

    launcher.prepare_launch_request(request)
    assert launcher.provision("ui-defaults") == _HANDLE.encode()

    resolver.assert_called_once_with(
        request.owner, ("https://github.com/acme/web.git", "https://github.com/acme/api.git")
    )
    claim = custom.create_namespaced_custom_object.call_args.args[4]
    assert claim["spec"]["warmPoolRef"]["name"] == profile.warm_pool
    assert launcher.selected_profile == profile
    assert launcher._resolve_image() == profile.image
    assert launcher._workspace_preparation_stage() == "preparing_workspace"
    assert all(repo.branch is None for repo in request.repos)


@pytest.mark.parametrize("default_branch", ["main", "release/current", "develop"])
def test_current_default_chooses_matching_profile_or_generic_fallback(
    monkeypatch: pytest.MonkeyPatch, default_branch: str
) -> None:
    main = _profile(name="main-v1")
    release = _profile(replace(_seed(), branch="release/current"), name="release-v1")
    resolver = MagicMock(return_value={_seed().url: default_branch})
    launcher = _launcher(main, release, profile_name=main.name, default_branch_resolver=resolver)
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "handle")
    launcher.prepare_launch_request(_request(replace(_repos(main)[0], branch=None)))

    assert launcher.provision("default-branch") == "handle"

    selected = {"main": main, "release/current": release}.get(default_branch)
    assert launcher.selected_profile == selected
    assert launcher.warm_pool == (selected.warm_pool if selected else _BASE["warm_pool"])
    assert launcher._resolve_image() == (selected.image if selected else _BASE["image"])
    resolver.assert_called_once_with("test-owner", (_seed().url,))


@pytest.mark.parametrize(
    "resolved",
    [
        None,
        {},
        {"https://github.com/acme/api.git": "main"},
        {"https://github.com/acme/web.git": "main"},
        {"https://github.com/acme/unrequested.git": "main"},
    ],
    ids=["no-resolver", "missing", "api-only", "web-only", "unrelated"],
)
def test_unresolved_defaults_never_guess_profile_branches(
    monkeypatch: pytest.MonkeyPatch, resolved: dict[str, str] | None
) -> None:
    profile = _profile(_seed(), _seed("web"))
    resolver = MagicMock(return_value=resolved) if resolved is not None else None
    launcher = _launcher(profile, default_branch_resolver=resolver)
    request = _request(*(replace(repo, branch=None) for repo in _repos(profile)))
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "generic")

    launcher.prepare_launch_request(request)
    assert launcher.provision("unresolved") == "generic"

    assert launcher.selected_profile is None
    assert launcher.warm_pool == _BASE["warm_pool"]
    assert launcher._resolve_image() == _BASE["image"]
    assert launcher._workspace_preparation_stage() == "cloning"
    assert all(repo.branch is None for repo in request.repos)


def test_mixed_request_resolves_only_omitted_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile(_seed(), replace(_seed("web"), branch="trunk"))
    resolver = MagicMock(return_value={_seed().url: "main", _seed("web").url: "wrong"})
    launcher = _launcher(profile, default_branch_resolver=resolver)
    request = _request(replace(_repos(profile)[0], branch=None), _repos(profile)[1])
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "handle")

    launcher.prepare_launch_request(request)
    launcher.provision("mixed")

    resolver.assert_called_once_with(request.owner, (_seed().url,))
    assert launcher.selected_profile == profile
    assert request.repos[1].branch == "trunk"


@pytest.mark.parametrize(
    "workspace", ["explicit", "other-repo", "subset", "superset", "directory", "branch"]
)
def test_explicit_or_unmatched_workspaces_skip_default_lookup(
    monkeypatch: pytest.MonkeyPatch, workspace: str
) -> None:
    profile = _profile(_seed(), _seed("web"))
    requested = list(_repos(profile))
    if workspace != "explicit":
        requested = [replace(repo, branch=None) for repo in requested]
    if workspace == "other-repo":
        requested[0] = RepoWorkspace("https://github.com/acme/other.git", None, "other")
    elif workspace == "subset":
        requested.pop()
    elif workspace == "superset":
        requested.append(RepoWorkspace("https://github.com/acme/other.git", None, "other"))
    elif workspace == "directory":
        requested[0] = replace(requested[0], repo_name="different-directory")
    elif workspace == "branch":
        requested[0] = replace(requested[0], branch="different-branch")
    resolver = MagicMock(side_effect=AssertionError("No metadata lookup expected"))
    launcher = _launcher(profile, default_branch_resolver=resolver)
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "handle")

    launcher.prepare_launch_request(_request(*requested))
    launcher.provision("skip-metadata")

    resolver.assert_not_called()
    assert launcher.selected_profile == (profile if workspace == "explicit" else None)


@pytest.mark.parametrize("request_context", [False, True])
def test_wake_binds_omitted_branches_to_allocation_without_current_defaults(
    sdk: Any, monkeypatch: pytest.MonkeyPatch, request_context: bool
) -> None:
    original = _profile(_seed(), replace(_seed("web"), branch="release/current"))
    newer = _profile(_seed(), _seed("web"), name="new-defaults-v2")
    resolver = MagicMock(return_value={repo.url: "main" for repo in newer.manifest.repos})
    launcher = _launcher(original, newer, default_branch_resolver=resolver)
    spec = _launcher(original, profile_name=original.name).template_spec()
    _wire_api(monkeypatch, launcher, _resources(spec))
    repos = tuple(replace(repo, branch=None) for repo in _repos(original))
    if request_context:
        launcher.prepare_launch_request(_request(*repos, agent_name="different-harness"))
    started = MagicMock(return_value="/home/omnigent/workspace")
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "start_host", started)

    assert launcher.start_host(_HANDLE.encode(), repos=repos) == "/home/omnigent/workspace"

    resolver.assert_not_called()
    assert launcher.selected_profile == original
    assert launcher.warm_pool == original.warm_pool
    assert launcher._resolve_image() == original.image
    started.assert_called_once_with(_HANDLE.encode(), repos=_repos(original))
    arguments = {
        "workspace": "/home/omnigent/workspace",
        "server_url": "https://omnigent.example.invalid",
        "host_id": "test-host",
    }
    prepared = json.loads(launcher._workspace_prep_command(repos=repos, **arguments)[5])
    assert prepared["prepare_command"] == warm.AgentSandboxWarmPoolLauncher(
        **_BASE
    )._workspace_prep_command(repos=_repos(original), **arguments)


def test_wake_rejects_explicit_branch_change_before_start(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(_seed(), _seed("web"))
    resolver = MagicMock(return_value={repo.url: "new-default" for repo in profile.manifest.repos})
    launcher = _launcher(profile, default_branch_resolver=resolver)
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    _wire_api(monkeypatch, launcher, _resources(spec))
    repos = (
        replace(_repos(profile)[0], branch="new-default"),
        replace(_repos(profile)[1], branch=None),
    )
    started = MagicMock()
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "start_host", started)

    with pytest.raises(click.ClickException, match="do not exactly match"):
        launcher.start_host(_HANDLE.encode(), repos=repos)

    resolver.assert_not_called()
    started.assert_not_called()


def test_app_wrappers_clone_deployment_and_keep_owner_resolvers_isolated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = _profile(name="main-v1")
    trunk = _profile(replace(_seed(), branch="trunk"), name="trunk-v1")

    def factory() -> WorkspaceProfileLauncher:
        return _launcher(main, trunk)

    agent_config = ManagedSandboxConfig(
        server_url="https://omnigent.example.invalid",
        launcher_factory=factory,
        token_ttl_s=90000,
        provider="agent_sandbox",
    )
    other_config = replace(agent_config, provider="kubernetes")
    deployment = ManagedSandboxDeployment(configs=(agent_config, other_config))
    first = SimpleNamespace(state=SimpleNamespace(github_store=object(), github_client=object()))
    second = SimpleNamespace(state=SimpleNamespace(github_store=object(), github_client=object()))
    original = MagicMock(side_effect=[first, second])
    resolvers = [
        MagicMock(return_value={_seed().url: "main"}),
        MagicMock(return_value={_seed().url: "trunk"}),
    ]
    resolver_class = MagicMock(side_effect=resolvers)
    monkeypatch.setattr(server_app, "create_app", original)
    monkeypatch.setattr(defaults, "GitHubDefaultBranchResolver", resolver_class)
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "handle")

    cli.install_default_branch_resolution()
    build_app = cast(Any, server_app.create_app)
    assert build_app("first-store", sandbox_config=deployment) is first
    assert build_app("second-store", sandbox_config=deployment) is second

    assert resolver_class.call_count == 2
    assert original.call_args_list[0].args == ("first-store",)
    assert original.call_args_list[1].args == ("second-store",)
    first_config = original.call_args_list[0].kwargs["sandbox_config"]
    second_config = original.call_args_list[1].kwargs["sandbox_config"]
    assert first_config is not deployment and second_config is not deployment
    assert first_config is not second_config
    assert deployment.configs == (agent_config, other_config)
    assert agent_config.launcher_factory is factory

    for app, configured, resolver, profile in zip(
        (first, second), (first_config, second_config), resolvers, (main, trunk), strict=True
    ):
        resolver.configure.assert_called_once_with(app.state.github_store, app.state.github_client)
        assert configured.reaper == deployment.reaper
        entry = configured.configs[0]
        assert entry is not agent_config
        assert replace(entry, launcher_factory=factory) == agent_config
        assert configured.configs[1] is other_config
        launcher = entry.launcher_factory()
        assert launcher is not entry.launcher_factory()
        launcher.prepare_launch_request(_request(replace(_repos(main)[0], branch=None)))
        launcher.provision("isolated-app")
        assert launcher.selected_profile == profile
        resolver.assert_called_once_with("test-owner", (_seed().url,))

    unchanged = factory()
    unchanged.prepare_launch_request(_request(replace(_repos(main)[0], branch=None)))
    unchanged.provision("original-factory")
    assert unchanged.selected_profile is None


def test_app_wrapper_preserves_plain_provider_launcher(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = warm.AgentSandboxWarmPoolLauncher(**_BASE)
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://omnigent.example.invalid",
            launcher_factory=lambda: launcher,
            token_ttl_s=90000,
            provider="agent_sandbox",
        )
    )
    app = SimpleNamespace(state=SimpleNamespace(github_store=None, github_client=None))
    original = MagicMock(return_value=app)
    monkeypatch.setattr(server_app, "create_app", original)

    cli.install_default_branch_resolution()
    cast(Any, server_app.create_app)(sandbox_config=deployment)

    wrapped = original.call_args.kwargs["sandbox_config"]
    assert wrapped.default.launcher_factory() is launcher


def test_app_without_sandbox_configuration_still_builds(monkeypatch: pytest.MonkeyPatch) -> None:
    app = SimpleNamespace(state=SimpleNamespace(github_store=None, github_client=None))
    original = MagicMock(return_value=app)
    monkeypatch.setattr(server_app, "create_app", original)

    cli.install_default_branch_resolution()
    assert cast(Any, server_app.create_app)("stores") is app

    original.assert_called_once_with("stores")
