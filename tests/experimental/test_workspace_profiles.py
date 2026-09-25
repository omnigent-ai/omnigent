"""Exact seed selection and retained-workspace profile validation."""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import click
import pytest

from omnigent.experimental.workspace_profiles.launcher import (
    MANIFEST_SHA_ANNOTATION,
    WORKSPACE_PROFILE_ANNOTATION,
    WorkspaceProfileLauncher,
)
from omnigent.experimental.workspace_profiles.profiles import (
    SeedManifest,
    SeedRepository,
    WorkspaceProfile,
    canonical_github_url,
    load_profiles,
    manifest_sha,
    parse_manifest,
    parse_profiles,
    select_profile,
)
from omnigent.onboarding.sandboxes import agent_sandbox_warm_pool as warm
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxLaunchRequest
from tests.onboarding.sandboxes.test_agent_sandbox_warm_pool import (
    _pod as _warm_pod,
)
from tests.onboarding.sandboxes.test_agent_sandbox_warm_pool import (
    clean_environment as clean_environment,
)
from tests.onboarding.sandboxes.test_agent_sandbox_warm_pool import (
    sdk as sdk,
)

_NAMESPACE = "workspace-tests"
_CLAIM_UID = "377e70bc-61e5-4a4c-b79e-4913d561f496"
_SANDBOX_UID = "822161cd-35bc-430e-a3fc-7bb8006d80b4"
_HANDLE = warm.WarmPoolHandle(
    _NAMESPACE, "workspace-claim", _CLAIM_UID, "workspace-sandbox", _SANDBOX_UID
)
_BASE = {
    "namespace": _NAMESPACE,
    "warm_pool": "generic-v1",
    "image": "generic-host:test",
    "service_account": "runner",
    "env": (),
    "in_cluster": True,
}


def _seed(
    name: str = "api", *, owner: str = "acme", directory: str | None = None
) -> SeedRepository:
    return SeedRepository(
        url=f"https://github.com/{owner}/{name}.git",
        branch="main",
        directory=directory or name,
        commit="a" * 40,
        bundle=f"{owner}-{name}.bundle",
        bundle_sha256="b" * 64,
    )


def _profile(*repos: SeedRepository, name: str = "workspace-v1") -> WorkspaceProfile:
    return WorkspaceProfile(
        name=name,
        warm_pool=name,
        image=f"registry.example/seed@sha256:{'c' * 64}",
        manifest=SeedManifest(version=1, repos=tuple(repos) or (_seed(),)),
    )


def _request(
    *repos: RepoWorkspace, agent_name: str | None = "claude-native-ui"
) -> SandboxLaunchRequest:
    return SandboxLaunchRequest(owner="test-owner", repos=tuple(repos), agent_name=agent_name)


def _repos(profile: WorkspaceProfile) -> tuple[RepoWorkspace, ...]:
    return tuple(
        RepoWorkspace(repo.url, repo.branch, repo.url.rsplit("/", 1)[1].removesuffix(".git"))
        for repo in profile.manifest.repos
    )


def _launcher(
    *profiles: WorkspaceProfile, profile_name: str | None = None, **kwargs: Any
) -> WorkspaceProfileLauncher:
    return WorkspaceProfileLauncher(
        profiles=profiles or (_profile(),), profile_name=profile_name, **{**_BASE, **kwargs}
    )


def _resources(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        warm.POOLS: {"spec": {"sandboxTemplateRef": {"name": "workspace-v1"}}},
        warm.TEMPLATES: {"spec": copy.deepcopy(spec)},
        warm.CLAIMS: {
            "metadata": {"name": _HANDLE.claim_name, "uid": _CLAIM_UID},
            "status": {"sandbox": {"name": _HANDLE.sandbox_name}},
        },
        warm.SANDBOX_PLURAL: {
            "metadata": {
                "name": _HANDLE.sandbox_name,
                "uid": _SANDBOX_UID,
                "ownerReferences": [{"uid": _CLAIM_UID, "controller": True}],
            },
            "spec": copy.deepcopy(spec),
        },
    }


def _wire_api(
    monkeypatch: pytest.MonkeyPatch,
    launcher: WorkspaceProfileLauncher,
    resources: dict[str, dict[str, Any]],
) -> MagicMock:
    custom = MagicMock()
    custom.get_namespaced_custom_object.side_effect = lambda _g, _v, _n, plural, _name, **_kw: (
        copy.deepcopy(resources[plural])
    )
    custom.create_namespaced_custom_object.return_value = copy.deepcopy(resources[warm.CLAIMS])
    monkeypatch.setattr(launcher, "_load_custom", lambda: custom)
    monkeypatch.setattr(launcher, "_close_clients", MagicMock())
    monkeypatch.setattr(warm, "_new_pod_name", lambda _name: _HANDLE.claim_name)
    return custom


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/acme/api.git",
        "https://github.com/ACME/API",
        "https://GITHUB.com/acme/api/",
        "git@github.com:acme/api.git",
        "ssh://git@github.com/acme/api.git",
    ],
)
def test_github_url_canonicalization(url: str) -> None:
    assert canonical_github_url(url) == "https://github.com/acme/api.git"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/acme/api",
        "https://gitlab.com/acme/api",
        "https://github.com.evil.example/acme/api",
        "https://token@github.com/acme/api",
        "https://github.com:443/acme/api",
        "https://github.com/acme/api?token=placeholder",
        "https://github.com/acme/api#main",
        "https://github.com/acme/../api",
        "ssh://other@github.com/acme/api",
    ],
)
def test_noncanonical_hosts_or_credential_urls_are_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="credential-free GitHub"):
        canonical_github_url(url)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "git@github.com:acme/api.git"),
        ("branch", None),
        ("branch", "main..other"),
        ("directory", "../workspace"),
        ("directory", ".git"),
        ("bundle", "../api.bundle"),
        ("bundle", "/api.bundle"),
        ("commit", "a" * 39),
        ("bundle_sha256", "G" * 64),
    ],
)
def test_seed_schema_rejects_unsafe_or_implicit_entries(field: str, value: object) -> None:
    data = _profile().manifest.to_dict()
    data["repos"][0][field] = value  # type: ignore[index]
    with pytest.raises(ValueError):
        parse_manifest(data)


def test_manifest_digest_ignores_json_object_key_order() -> None:
    manifest = _profile().manifest
    reordered = {
        "repos": [dict(reversed(list(manifest.repos[0].to_dict().items())))],
        "version": 1,
    }
    assert manifest_sha(parse_manifest(reordered)) == manifest_sha(manifest)
    assert manifest_sha(manifest) != manifest_sha(
        replace(manifest, repos=(replace(manifest.repos[0], commit="d" * 40),))
    )


def test_catalog_round_trip_and_digest_pinning(tmp_path: Path) -> None:
    profile = _profile()
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([profile.to_dict()]))
    assert load_profiles(path) == (profile,)
    with pytest.raises(ValueError, match="pinned"):
        replace(profile, image="registry.example/seed:latest")


def test_catalog_rejects_duplicate_resource_identities_and_unknown_fields() -> None:
    entry = _profile().to_dict()
    with pytest.raises(ValueError, match="unique"):
        parse_profiles([entry, entry])
    with pytest.raises(ValueError, match="fields"):
        parse_profiles([{**entry, "owner": "someone"}])
    with pytest.raises(ValueError, match="unique"):
        parse_manifest({"version": 1, "repos": [_seed().to_dict(), _seed().to_dict()]})


def test_exact_combination_matches_independent_of_request_order() -> None:
    profile = _profile(_seed(), _seed("web"))
    requested = (RepoWorkspace("git@github.com:acme/web.git", "main", "web"), _repos(profile)[0])
    assert select_profile((profile,), requested) == profile


@pytest.mark.parametrize("change", ["subset", "superset", "default_branch", "branch", "directory"])
def test_partial_or_different_workspaces_never_match(change: str) -> None:
    profile = _profile(_seed(), _seed("web"))
    requested = list(_repos(profile))
    if change == "subset":
        requested.pop()
    elif change == "superset":
        requested.append(RepoWorkspace("https://github.com/acme/extra.git", "main", "extra"))
    elif change == "default_branch":
        requested[0] = replace(requested[0], branch=None)
    elif change == "branch":
        requested[0] = replace(requested[0], branch="feature")
    else:
        requested[0] = replace(requested[0], repo_name="another-directory")
    assert select_profile((profile,), requested) is None


def test_directory_collisions_use_existing_clone_mapping() -> None:
    profile = _profile(
        _seed(owner="first", directory="first__api"),
        _seed(owner="second", directory="second__api"),
    )
    assert select_profile((profile,), _repos(profile)) == profile
    assert select_profile((profile,), tuple(reversed(_repos(profile)))) == profile


def test_ambiguous_profiles_fail_instead_of_picking_one() -> None:
    first = _profile()
    second = replace(first, name="workspace-v2", warm_pool="workspace-v2")
    with pytest.raises(ValueError, match="Multiple workspace profiles"):
        select_profile((first, second), _repos(first))


def test_request_selection_pins_image_and_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = _profile()
    launcher = _launcher(profile)
    observed = []

    def provision(instance: Any, _name: str) -> str:
        observed.append((instance._resolve_image(), instance._warm_pool, instance._agent_name))
        return _HANDLE.encode()

    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", provision)
    request = _request(*_repos(profile))
    launcher.prepare_launch_request(request)
    assert launcher._request == request
    assert launcher.provision("example") == _HANDLE.encode()
    assert observed == [(profile.image, profile.warm_pool, request.agent_name)]
    assert len(_HANDLE.encode()) <= 256


@pytest.mark.parametrize("legacy", [False, True])
def test_unmatched_and_legacy_requests_restore_generic_behavior(
    monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    profile = _profile()
    launcher = _launcher(profile, profile_name=profile.name)
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "provision", lambda *_args: "generic")
    if legacy:
        launcher.prepare_for_launch(agent_name="other-agent")
    else:
        launcher.prepare_launch_request(_request())
    assert launcher.provision("example") == "generic"
    assert launcher.selected_profile is None
    assert launcher._resolve_image() == _BASE["image"]
    assert launcher.warm_pool == _BASE["warm_pool"]
    for shared in (False, True):
        assert launcher.template_spec(shared=shared) == warm.AgentSandboxWarmPoolLauncher(
            **_BASE
        ).template_spec(shared=shared)


def test_selected_template_pins_isolated_runtime_and_profile_fingerprint() -> None:
    profile = _profile()
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    template = spec["podTemplate"]
    annotations = template["metadata"]["annotations"]
    assert annotations[WORKSPACE_PROFILE_ANNOTATION] == profile.name
    assert annotations[MANIFEST_SHA_ANNOTATION] == profile.manifest_sha
    assert annotations[warm.SHARED_POOL_ANNOTATION] == "true"
    assert "omnigent.ai/agent" not in template["metadata"]["labels"]
    bootstrap, host = template["spec"]["containers"]
    assert bootstrap["command"] == [
        "python3",
        "-I",
        "-m",
        "omnigent.experimental.workspace_profiles.runtime",
        "prepare",
        "--manifest-sha",
        profile.manifest_sha,
    ]
    assert host["command"] == ["python3", "-I", "-m", "omnigent.host.warm_bootstrap", "host"]
    assert all(c["readinessProbe"]["exec"]["command"][1] == "-I" for c in (bootstrap, host))
    fingerprint = annotations.pop(warm.PROFILE_ANNOTATION)
    assert fingerprint == hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()


def test_profile_provision_uses_claim_and_rejects_classifier_fallback(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    launcher = _launcher(profile)
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    resources = _resources(spec)
    custom = _wire_api(monkeypatch, launcher, resources)
    launcher.prepare_launch_request(_request(*_repos(profile)))
    assert launcher.provision("example") == _HANDLE.encode()
    body = custom.create_namespaced_custom_object.call_args.args[4]
    assert body["spec"]["warmPoolRef"]["name"] == profile.warm_pool

    resources[warm.TEMPLATES]["spec"]["podTemplate"]["metadata"]["labels"]["omnigent.ai/agent"] = (
        "other"
    )
    custom.create_namespaced_custom_object.reset_mock()
    with pytest.raises(click.ClickException, match="shared warm-pool"):
        launcher.provision("another")
    custom.create_namespaced_custom_object.assert_not_called()


def test_wake_recovers_original_profile_without_rematching_current_catalog(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _profile()
    newer = replace(
        original, name="workspace-v2", warm_pool="workspace-v2", image=f"other@sha256:{'d' * 64}"
    )
    spec = _launcher(original, profile_name=original.name).template_spec()
    launcher = _launcher(
        original, newer, warm_pool="changed-default", image="changed-default:latest"
    )
    _wire_api(monkeypatch, launcher, _resources(spec))
    launcher.prepare_launch_request(_request(*_repos(original), agent_name="another-harness"))
    started = MagicMock(return_value="/home/omnigent/workspace/api")
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "start_host", started)
    assert (
        launcher.start_host(_HANDLE.encode(), repos=_repos(original))
        == "/home/omnigent/workspace/api"
    )
    assert launcher.selected_profile == original
    assert launcher._resolve_image() == original.image
    assert launcher.warm_pool == original.warm_pool
    started.assert_called_once()


@pytest.mark.parametrize("context", [True, False])
def test_retained_profile_recovers_without_request_context(
    sdk: Any, monkeypatch: pytest.MonkeyPatch, context: bool
) -> None:
    profile = _profile()
    launcher = _launcher(profile)
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    _wire_api(monkeypatch, launcher, _resources(spec))
    if context:
        launcher.prepare_launch_request(_request())
    monkeypatch.setattr(launcher, "_pod", lambda *_args: None)
    launcher.resume(_HANDLE.encode())
    assert launcher.selected_profile == profile


def test_missing_historical_profile_preserves_allocated_resources(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _profile()
    spec = _launcher(original, profile_name=original.name).template_spec()
    launcher = _launcher(_profile(name="new-v2"))
    custom = _wire_api(monkeypatch, launcher, _resources(spec))
    pod = MagicMock()
    monkeypatch.setattr(launcher, "_pod", pod)
    with pytest.raises(click.ClickException, match="Restore its original") as failure:
        launcher.resume(_HANDLE.encode())
    assert not isinstance(failure.value, SandboxGoneError)
    pod.assert_not_called()
    custom.delete_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize(
    "tamper", ["digest", "image", "command", "fingerprint", "missing", "partial"]
)
def test_tampered_retained_profiles_fail_before_pod_deletion(
    sdk: Any, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    profile = _profile()
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    annotations = spec["podTemplate"]["metadata"]["annotations"]
    if tamper == "digest":
        annotations[MANIFEST_SHA_ANNOTATION] = "0" * 64
    elif tamper == "image":
        spec["podTemplate"]["spec"]["containers"][0]["image"] = "other:latest"
    elif tamper == "command":
        spec["podTemplate"]["spec"]["containers"][0]["command"] = ["sleep", "infinity"]
    elif tamper == "fingerprint":
        annotations[warm.PROFILE_ANNOTATION] = "0" * 64
    elif tamper == "missing":
        annotations.pop(WORKSPACE_PROFILE_ANNOTATION)
        annotations.pop(MANIFEST_SHA_ANNOTATION)
    else:
        annotations.pop(MANIFEST_SHA_ANNOTATION)
    launcher = _launcher(profile)
    custom = _wire_api(monkeypatch, launcher, _resources(spec))
    pod = MagicMock()
    monkeypatch.setattr(launcher, "_pod", pod)
    with pytest.raises(click.ClickException):
        launcher.resume(_HANDLE.encode())
    pod.assert_not_called()
    custom.delete_namespaced_custom_object.assert_not_called()


def test_recovery_verifies_uid_before_reading_profile(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    resources = _resources(_launcher(profile, profile_name=profile.name).template_spec())
    resources[warm.SANDBOX_PLURAL]["metadata"]["uid"] = "changed-uid"
    launcher = _launcher(profile)
    _wire_api(monkeypatch, launcher, resources)
    with pytest.raises(SandboxGoneError):
        launcher._allocation(_HANDLE)
    assert launcher.selected_profile is None


def test_generic_allocation_does_not_adopt_a_current_matching_seed(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    launcher = _launcher(profile)
    generic = warm.AgentSandboxWarmPoolLauncher(**_BASE).template_spec(shared=True)
    _wire_api(monkeypatch, launcher, _resources(generic))
    launcher.prepare_launch_request(_request(*_repos(profile)))
    assert launcher._allocation(_HANDLE)["spec"] == generic
    assert launcher.selected_profile is None
    assert launcher.template_spec(shared=True) == generic


def test_activation_wraps_normal_preparation_and_rejects_repo_changes() -> None:
    profile = _profile()
    launcher = _launcher(profile, profile_name=profile.name)
    arguments = {
        "workspace": "/home/omnigent/workspace",
        "repos": _repos(profile),
        "server_url": "https://omnigent.example",
        "host_id": "test-host",
        "host_config": {"host": {"public": True}},
    }
    command = launcher._workspace_prep_command(**arguments)
    assert command[:5] == [
        "python3",
        "-I",
        "-m",
        "omnigent.experimental.workspace_profiles.runtime",
        "activate",
    ]
    payload = json.loads(command[5])
    assert payload == {
        "manifest_sha": profile.manifest_sha,
        "server_url": arguments["server_url"],
        "prepare_command": warm.AgentSandboxWarmPoolLauncher(**_BASE)._workspace_prep_command(
            **arguments
        ),
    }
    with pytest.raises(click.ClickException, match="exactly match"):
        launcher._workspace_prep_command(**{**arguments, "repos": ()})


def test_historical_start_rejects_subset_before_starting_host(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile(_seed(), _seed("web"))
    launcher = _launcher(profile)
    _wire_api(
        monkeypatch,
        launcher,
        _resources(_launcher(profile, profile_name=profile.name).template_spec()),
    )
    start = MagicMock()
    monkeypatch.setattr(warm.AgentSandboxWarmPoolLauncher, "start_host", start)
    with pytest.raises(click.ClickException, match="exactly match"):
        launcher.start_host(_HANDLE.encode(), repos=_repos(profile)[:1])
    start.assert_not_called()


def test_inherited_activation_delivers_profile_runtime_command(
    sdk: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile()
    spec = _launcher(profile, profile_name=profile.name).template_spec()
    launcher = _launcher(profile)
    _wire_api(monkeypatch, launcher, _resources(spec))
    pod = _warm_pod(spec)
    monkeypatch.setattr(launcher, "_pod", lambda *_args: pod)
    token = "unit-test-launch-token"
    generation = hashlib.sha256(token.encode()).hexdigest()[:32]
    monkeypatch.setattr(
        launcher,
        "_status",
        MagicMock(
            side_effect=[
                {"stage": "waiting", "generation": None},
                {"stage": "prepared", "generation": generation},
            ]
        ),
    )
    execute = MagicMock(return_value="")
    monkeypatch.setattr(launcher, "_exec", execute)
    workspace = launcher.start_host(
        _HANDLE.encode(),
        repos=_repos(profile),
        token=token,
        host_id="test-host",
        host_name="test-host-name",
        server_url="https://omnigent.example",
        agent_name="codex-native-ui",
    )
    assert workspace == "/home/omnigent/workspace/api"
    activation = execute.call_args.args[3]
    assert activation["token"] == token
    command = activation["prepare_command"]
    assert command[:5] == [
        "python3",
        "-I",
        "-m",
        "omnigent.experimental.workspace_profiles.runtime",
        "activate",
    ]
    assert json.loads(command[5])["manifest_sha"] == profile.manifest_sha
