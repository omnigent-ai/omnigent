"""Opt-in repository-seeded warm allocation using the existing agent-sandbox provider."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from typing import Any

import click

from omnigent.experimental.workspace_profiles.profiles import (
    WorkspaceProfile,
    canonical_github_url,
    parse_profiles,
    profile_matches,
    select_profile,
)
from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import (
    BOOTSTRAP_CONTAINER,
    PROFILE_ANNOTATION,
    TEMPLATES,
    AgentSandboxWarmPoolLauncher,
    WarmPoolHandle,
)
from omnigent.onboarding.sandboxes.types import (
    RepoWorkspace,
    SandboxLaunchRequest,
    clone_dir_names,
)

WORKSPACE_PROFILE_ANNOTATION = "omnigent.ai/workspace-profile"
MANIFEST_SHA_ANNOTATION = "omnigent.ai/workspace-manifest-sha"
_RUNTIME_MODULE = "omnigent.experimental.workspace_profiles.runtime"
DefaultBranchResolver = Callable[[str, Sequence[str]], Mapping[str, str]]


def _profile_repositories(
    profile: WorkspaceProfile, repos: Sequence[RepoWorkspace]
) -> tuple[RepoWorkspace, ...] | None:
    """Bind omitted branches to a known profile without discovering current defaults."""
    branches = {(repo.url, repo.directory): repo.branch for repo in profile.manifest.repos}
    try:
        bound = tuple(
            replace(
                repo,
                branch=branches[(canonical_github_url(repo.url), directory)]
                if repo.branch is None
                else repo.branch,
            )
            for repo, directory in zip(repos, clone_dir_names(repos), strict=True)
        )
    except (KeyError, ValueError):
        return None
    return bound if profile_matches(profile, bound) else None


class WorkspaceProfileLauncher(AgentSandboxWarmPoolLauncher):
    """Choose exact repository profiles; recover retained allocations by immutable identity."""

    def __init__(
        self,
        *,
        profiles: Sequence[WorkspaceProfile],
        profile_name: str | None = None,
        default_branch_resolver: DefaultBranchResolver | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._profiles = parse_profiles([profile.to_dict() for profile in profiles])
        self._profiles_by_name = {profile.name: profile for profile in self._profiles}
        self._generic_image = self._image_ref
        self._generic_pool = self._warm_pool
        self._request: SandboxLaunchRequest | None = None
        self._selected_profile: WorkspaceProfile | None = None
        self._default_branch_resolver = default_branch_resolver
        if profile_name is not None:
            self._use_profile(self._configured_profile(profile_name))

    @property
    def selected_profile(self) -> WorkspaceProfile | None:
        return self._selected_profile

    @property
    def warm_pool(self) -> str | None:
        return self._warm_pool

    def _configured_profile(self, name: str) -> WorkspaceProfile:
        try:
            return self._profiles_by_name[name]
        except KeyError:
            raise click.ClickException(
                "Workspace profile is unavailable. Restore its original operator configuration "
                "before waking the retained Sandbox; its workspace has not been removed."
            ) from None

    def _use_profile(self, profile: WorkspaceProfile | None) -> None:
        self._selected_profile = profile
        self._image_ref = profile.image if profile else self._generic_image
        self._warm_pool = profile.warm_pool if profile else self._generic_pool

    def prepare_for_launch(self, *, agent_name: str | None = None) -> None:
        super().prepare_for_launch(agent_name=agent_name)
        self._request = None
        self._use_profile(None)

    def prepare_launch_request(self, request: SandboxLaunchRequest) -> None:
        super().prepare_launch_request(request)
        self._request = request

    def configure_default_branches(self, resolver: DefaultBranchResolver) -> None:
        """Bind the containing server's owner-aware GitHub integration."""
        self._default_branch_resolver = resolver

    def _selection_repositories(self, request: SandboxLaunchRequest) -> Sequence[RepoWorkspace]:
        if self._default_branch_resolver is None or all(
            repo.branch is not None for repo in request.repos
        ):
            return request.repos
        if not any(_profile_repositories(profile, request.repos) for profile in self._profiles):
            return request.repos
        urls = tuple(
            dict.fromkeys(
                canonical_github_url(repo.url) for repo in request.repos if repo.branch is None
            )
        )
        defaults = self._default_branch_resolver(request.owner, urls)
        return tuple(
            replace(repo, branch=defaults.get(canonical_github_url(repo.url)))
            if repo.branch is None
            else repo
            for repo in request.repos
        )

    def provision(self, name: str) -> str:
        try:
            selected = (
                select_profile(self._profiles, self._selection_repositories(self._request))
                if self._request
                else None
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        self._use_profile(selected)
        return super().provision(name)

    def _bootstrap_command(self, mode: str) -> list[str]:
        if self._selected_profile is None:
            return super()._bootstrap_command(mode)
        return ["python3", "-I", "-m", "omnigent.host.warm_bootstrap", mode]

    def _workspace_preparation_stage(self) -> str:
        if self._selected_profile is not None:
            return "preparing_workspace"
        return super()._workspace_preparation_stage()

    def template_spec(
        self,
        *,
        agent_name: str | None = None,
        host_config: dict[str, object] | None = None,
        shared: bool = False,
    ) -> dict[str, Any]:
        profile = self._selected_profile
        if profile is None:
            return super().template_spec(
                agent_name=agent_name, host_config=host_config, shared=shared
            )
        spec = super().template_spec(agent_name=None, host_config=host_config, shared=True)
        template = spec["podTemplate"]
        bootstrap = next(
            container
            for container in template["spec"]["containers"]
            if container["name"] == BOOTSTRAP_CONTAINER
        )
        bootstrap["command"] = [
            "python3",
            "-I",
            "-m",
            _RUNTIME_MODULE,
            "prepare",
            "--manifest-sha",
            profile.manifest_sha,
        ]
        annotations = template["metadata"]["annotations"]
        annotations.pop(PROFILE_ANNOTATION)
        annotations[WORKSPACE_PROFILE_ANNOTATION] = profile.name
        annotations[MANIFEST_SHA_ANNOTATION] = profile.manifest_sha
        annotations[PROFILE_ANNOTATION] = hashlib.sha256(
            json.dumps(spec, sort_keys=True).encode()
        ).hexdigest()
        return spec

    def _get(self, group: str, plural: str, namespace: str, name: str) -> dict[str, Any]:
        resource = super()._get(group, plural, namespace, name)
        if self._selected_profile is not None and plural == TEMPLATES:
            # A seeded profile must not fall back to a direct Pod with the seed image.
            self._validate_profile(resource["spec"], agent_name=None, shared=True)
        return resource

    def _allocation(self, handle: WarmPoolHandle) -> dict[str, Any]:
        sandbox = super()._allocation(handle)
        spec = sandbox["spec"]
        metadata = spec["podTemplate"].get("metadata", {})
        annotations = metadata.get("annotations", {})
        name = annotations.get(WORKSPACE_PROFILE_ANNOTATION)
        digest = annotations.get(MANIFEST_SHA_ANNOTATION)
        if name is None and digest is None:
            if any(
                _RUNTIME_MODULE in container.get("command", [])
                for container in spec["podTemplate"]["spec"].get("containers", [])
            ):
                raise click.ClickException("Workspace seed profile metadata is missing.")
            self._use_profile(None)
            return sandbox
        if not isinstance(name, str) or not isinstance(digest, str):
            raise click.ClickException("Workspace seed profile metadata is incomplete.")
        profile = self._configured_profile(name)
        if digest != profile.manifest_sha:
            raise click.ClickException(
                "Workspace seed manifest differs from its retained profile."
            )
        self._use_profile(profile)
        self._validate_profile(spec, agent_name=None, shared=True)
        return sandbox

    def start_host(
        self,
        sandbox_id: str,
        *,
        repos: Sequence[RepoWorkspace] = (),
        **kwargs: Any,
    ) -> str:
        if sandbox_id.startswith("wp1:"):
            try:
                self._allocation(WarmPoolHandle.parse(sandbox_id))
                repos = self._require_repositories(repos)
            finally:
                self._close_clients()
        else:
            self._use_profile(None)
        return super().start_host(sandbox_id, repos=repos, **kwargs)

    def _require_repositories(self, repos: Sequence[RepoWorkspace]) -> Sequence[RepoWorkspace]:
        if self._selected_profile is None:
            return repos
        bound = _profile_repositories(self._selected_profile, repos)
        if bound is None:
            raise click.ClickException(
                "Requested repositories do not exactly match the retained workspace seed. "
                "Create a new session for a different repository set."
            )
        return bound

    def _workspace_prep_command(
        self,
        workspace: str,
        repos: Sequence[RepoWorkspace],
        server_url: str,
        host_id: str,
        host_config: dict[str, object] | None = None,
    ) -> list[str]:
        repos = self._require_repositories(repos)
        command = super()._workspace_prep_command(
            workspace, repos, server_url, host_id, host_config
        )
        if self._selected_profile is None:
            return command
        payload = {
            "manifest_sha": self._selected_profile.manifest_sha,
            "prepare_command": command,
            "server_url": server_url,
        }
        return ["python3", "-I", "-m", _RUNTIME_MODULE, "activate", json.dumps(payload)]
