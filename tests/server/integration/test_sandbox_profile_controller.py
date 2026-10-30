"""Controller assignments outlive sessions without any Omnigent schema additions."""

import pytest

from omnigent.server.sandbox_profiles import SandboxProfileCatalog
from tests.server.integration._sandbox_profile_support import _OWNER, _PROVIDER_PROFILE, _Env
from tests.server.integration._sandbox_profile_support import env as env

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize("env", ["controller"], indirect=True)]


async def test_controller_recovers_after_last_session_deleted_and_restart(env: _Env):
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    source = env.conversations.get_conversation(source_id)
    host_id = source.host_id
    sandbox_id = env.hosts.get_host(host_id).sandbox_id
    await env.conversations.delete_conversation(source_id)
    assert env.catalog._store.get_host_snapshot(host_id) is None
    env.app.state.sandbox_profiles = SandboxProfileCatalog(
        env.conversations, env.app.state.sandbox_config, env.hosts
    )
    # A catalog update must not change an existing sandbox's assignment.
    env.recording.provider_profile = _PROVIDER_PROFILE.model_copy(update={"revision": "new"})
    response = await env.client.get(f"/v1/hosts/{host_id}?include=sandbox_profile")
    assert response.status_code == 200, response.text
    public = response.json()["sandbox_profile"]
    assert public["id"] == "build" and public["revision"] == env.profile["revision"]
    assert "user_id" not in public and "workspace_id" not in public
    assert any(context.sandbox_id == sandbox_id for context, _ in env.recording.selections)
    target = env.conversations.create_conversation(
        agent_id=env.agent_id, host_id=host_id, workspace="/workspace"
    )
    env.catalog.require_session_host(
        target.id, host_id, user_id=_OWNER, sandbox_provider="company"
    )
    assert env.catalog.get_session_snapshot(target.id) == env.profile
    # Session responses hide internal labels and expose only public profile metadata.
    response = await env.client.get(f"/v1/sessions/{target.id}")
    assert response.status_code == 200, response.text
    assert not any(key.startswith("sandbox_profile:") for key in response.json()["labels"])
    assert response.json()["sandbox_profile"] == public


async def test_controller_denial_is_sanitized_and_does_not_adopt(env: _Env):
    source_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    host_id = env.conversations.get_conversation(source_id).host_id
    await env.conversations.delete_conversation(source_id)
    env.recording.allowed_users.clear()
    response = await env.client.get(f"/v1/hosts/{host_id}?include=sandbox_profile")
    assert response.status_code == 200, response.text
    assert response.json()["sandbox_profile"] is None
    assert response.json()["sandbox_profile_error"] == "Sandbox profile access denied"
    assert "Controller denied" not in response.text


async def test_controller_missing_sandbox_replaces_at_saved_revision(env: _Env):
    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    env.recording.provider_profile = _PROVIDER_PROFILE.model_copy(update={"revision": "new"})
    env.recording.gone = True
    await env.wake(session_id)
    launch = env.app.state.managed_launches.get(session_id)
    assert launch is None or launch.error is None
    assert len(env.recording.allocations) == 2
    assert env.catalog.get_session_snapshot(session_id) == env.profile
    assert list(env.recording.assigned.values()) == [_PROVIDER_PROFILE, _PROVIDER_PROFILE]


async def test_orphan_missing_sandbox_has_no_default_profile(env: _Env):
    session_id = await env.settled(await env.create("json", sandbox_profile_id="build"))
    host_id = env.conversations.get_conversation(session_id).host_id
    await env.conversations.delete_conversation(session_id)
    env.recording.gone = True
    response = await env.client.get(f"/v1/hosts/{host_id}?include=sandbox_profile")
    assert response.status_code == 200, response.text
    assert response.json()["sandbox_profile"] is None
    assert response.json()["sandbox_profile_error"] == "Sandbox no longer exists"
    assert len(env.recording.allocations) == 1
