"""Tests for WRONG_REPLICA classification in RunnerRouter._runner_absent_code."""

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.server._runner_ws_tunnel import WrongReplicaWSError, make_tunnel_ws_factory


class MockHostRegistry:
    """Mock host registry for testing."""

    def __init__(self, hosts=None):
        self.hosts = hosts or {}

    def get(self, host_id):
        return self.hosts.get(host_id)


class MockHostStore:
    """Mock host store for testing."""

    def __init__(self, online_hosts=None):
        self.online_hosts = online_hosts or {}

    def is_online(self, host_id):
        return self.online_hosts.get(host_id, False)


class MockTunnelRegistry:
    """Mock tunnel registry."""

    def get(self, runner_id):
        return None


class MockConversationStore:
    """Mock conversation store."""

    def __init__(self, *conversations):
        self.rows = {conv.id: conv for conv in conversations}

    def get_conversation(self, session_id):
        return self.rows.get(session_id)

    def get_conversations(self, session_ids):
        return {sid: self.rows[sid] for sid in session_ids if sid in self.rows}


def test_runner_absent_code_no_host_id_returns_runner_unavailable():
    """When no host_id is provided, classify as RUNNER_UNAVAILABLE."""
    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
    )
    code = router._runner_absent_code(None)
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_no_registries_returns_runner_unavailable():
    """When no registries are wired, classify as RUNNER_UNAVAILABLE."""
    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=None,
        host_store=None,
    )
    code = router._runner_absent_code("host_123")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_host_on_this_replica_returns_runner_unavailable():
    """When host is on this replica, it's genuinely unavailable."""
    host_registry = MockHostRegistry({"host_123": "connection_obj"})
    host_store = MockHostStore()

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_123")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_host_absent_locally_but_online_returns_wrong_replica():
    """When host is absent locally but online elsewhere → WRONG_REPLICA."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    host_store = MockHostStore({"host_456": True})  # Online somewhere

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_456")
    assert code == ErrorCode.WRONG_REPLICA


def test_runner_absent_code_host_absent_everywhere_returns_runner_unavailable():
    """When host is absent locally AND offline everywhere → RUNNER_UNAVAILABLE."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    host_store = MockHostStore({})  # Empty: not online anywhere

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=host_store,
    )
    code = router._runner_absent_code("host_dead")
    assert code == ErrorCode.RUNNER_UNAVAILABLE


def test_runner_absent_code_no_store_registry_only_returns_wrong_replica():
    """When store is absent but registry says host not here → treat as wrong_replica."""
    host_registry = MockHostRegistry({})  # Empty: not on this replica
    # No host_store: should fall back to registry-only check

    router = RunnerRouter(
        registry=MockTunnelRegistry(),
        conversation_store=MockConversationStore(),
        host_registry=host_registry,
        host_store=None,
    )
    code = router._runner_absent_code("host_789")
    # Without store, absence locally is treated as wrong replica (could be elsewhere)
    assert code == ErrorCode.WRONG_REPLICA


@pytest.mark.parametrize("surface", ["resources", "existing", "dispatch", "terminal_attach"])
def test_colocated_child_on_another_replica_is_not_reported_offline(surface):
    parent = Conversation(
        id="parent",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        runner_id="runner_shared",
        host_id="host_parent",
    )
    child = Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        root_conversation_id="parent",
        parent_conversation_id="parent",
        kind="sub_agent",
        runner_id=parent.runner_id,
    )
    registry = MockTunnelRegistry()
    router = RunnerRouter(
        registry=registry,
        conversation_store=MockConversationStore(parent, child),
        host_registry=MockHostRegistry(),
        host_store=MockHostStore({"host_parent": True}),
    )
    if surface == "terminal_attach":
        factory = make_tunnel_ws_factory(router, registry)
        with pytest.raises(WrongReplicaWSError):
            factory("/v1/sessions/child/resources/terminals/terminal_pi_main/attach")
    else:
        with pytest.raises(OmnigentError) as caught:
            if surface == "resources":
                router.client_for_session_resources(child.id, conversation=child)
            elif surface == "existing":
                router.client_for_existing_conversation(child.id)
            else:
                router.client_for_conversation(conversation_id=child.id, harness="pi-native")
        assert caught.value.code == ErrorCode.WRONG_REPLICA
    assert child.host_id is None
