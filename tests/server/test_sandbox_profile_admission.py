"""The app's shared runner admission hook checks profiles before every launch."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from omnigent.errors import OmnigentError
from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile, SandboxProfileChanged
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher


@pytest.fixture
def admission_app(runtime_init: None, db_uri: str, tmp_path: Path):
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    hosts = HostStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    launcher = FakeSandboxLauncher()
    launcher.manages_profiles = True
    offered = [
        ProviderSandboxProfile(id="build", name="Build", revision="1"),
        ProviderSandboxProfile(id="other", name="Other", revision="1"),
    ]
    launcher.list_profiles = lambda context: offered
    selections = []

    def select(context, profile):
        if profile not in offered:
            raise SandboxProfileChanged("Profile retired")
        selections.append((profile.id, profile.revision))

    launcher.select_profile = select
    app = create_app(
        feature_flags=FeatureFlags(frozenset({Feature.SANDBOX_PROFILES})),
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        host_store=hosts,
        sandbox_config=ManagedSandboxDeployment.single(
            ManagedSandboxConfig(
                provider="company",
                server_url="https://test.example.com",
                token_ttl_s=100,
                launcher_factory=lambda: launcher,
            )
        ),
    )
    host_id = uuid.uuid4().hex
    hosts.upsert_on_connect(host_id, "profile-host", "alice")
    profile = app.state.sandbox_profiles.resolve("build", user_id="alice")
    app.state.profile_test_offered = offered
    app.state.profile_test_selections = selections
    source = conversations.create_conversation(host_id=host_id, workspace="/tmp")
    app.state.sandbox_profiles.bind_session(source.id, profile)
    return app, hosts, conversations, host_id, profile


@pytest.mark.parametrize("reason", ["disabled", "mismatch"])
def test_shared_admission_rejects_unavailable_profiles(admission_app, reason):
    app, _hosts, conversations, host_id, profile = admission_app
    target = conversations.create_conversation(host_id=host_id, workspace="/tmp")
    catalog = app.state.sandbox_profiles
    if reason == "disabled":
        app.state.profile_test_offered.clear()
    else:
        other = catalog.resolve("other", user_id="alice")
        catalog.bind_session(target.id, other)
    with pytest.raises(HTTPException, match="409"):
        app.state.host_registry.launch_authorizer(host_id, target.id, None, None)
    assert conversations.get_conversation(target.id).runner_id is None
    assert catalog.get_session_snapshot(target.id) != profile


def test_failed_host_admission_does_not_adopt_profile(admission_app):
    app, _hosts, conversations, host_id, _profile = admission_app
    target = conversations.create_conversation()
    with pytest.raises(OmnigentError, match="no longer bound"):
        app.state.host_registry.launch_authorizer(host_id, target.id, None, None)
    assert app.state.sandbox_profiles.get_session_snapshot(target.id) is None


@pytest.mark.parametrize("binding", ["existing", "unbound", "transfer"])
def test_profile_binding_waits_for_accepted_host_binding(admission_app, binding):
    app, _hosts, conversations, host_id, profile = admission_app
    old_host_id = uuid.uuid4().hex if binding == "transfer" else None
    target = conversations.create_conversation(
        host_id=host_id if binding == "existing" else old_host_id,
        workspace="/tmp" if binding != "unbound" else None,
    )
    app.state.profile_test_selections.clear()
    app.state.host_registry.launch_authorizer(
        host_id, target.id, None, None, binding != "existing", old_host_id
    )
    snapshot = app.state.sandbox_profiles.get_session_snapshot(target.id)
    assert snapshot == (profile if binding == "existing" else None)
    # Adopting a profile also checks permission at the persistence boundary.
    assert app.state.profile_test_selections == [("build", "1")] * (
        2 if binding == "existing" else 1
    )
