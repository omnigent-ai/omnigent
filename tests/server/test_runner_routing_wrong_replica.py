"""Tests for WRONG_REPLICA classification in RunnerRouter._runner_absent_code."""

import pytest

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter, routing_host_id
from omnigent.server._runner_ws_tunnel import WrongReplicaWSError, make_tunnel_ws_factory
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY


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
@pytest.mark.parametrize("ancestry", ["direct", "nested", "nested_hostless_root"])
@pytest.mark.parametrize("side_chat", [False, True])
def test_colocated_child_on_another_replica_is_not_reported_offline(surface, ancestry, side_chat):
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
        root_conversation_id="child" if side_chat else "parent",
        parent_conversation_id=None if side_chat else "parent",
        kind="default" if side_chat else "sub_agent",
        runner_id=parent.runner_id,
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: parent.id}
        if side_chat
        else {},
    )
    conversations = [parent, child]
    if ancestry != "direct":
        root = Conversation(
            id="root",
            created_at=1,
            updated_at=1,
            root_conversation_id="root",
            host_id="host_root" if ancestry == "nested" else None,
        )
        intermediate = Conversation(
            id="intermediate",
            created_at=1,
            updated_at=1,
            root_conversation_id="intermediate" if side_chat else root.id,
            parent_conversation_id=None if side_chat else parent.id,
            kind="default" if side_chat else "sub_agent",
            runner_id=parent.runner_id,
            labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: parent.id}
            if side_chat
            else {},
        )
        parent.parent_conversation_id = root.id
        parent.root_conversation_id = root.id
        parent.kind = "sub_agent"
        if side_chat:
            child.labels[SIDE_CHAT_SOURCE_LABEL_KEY] = intermediate.id
        else:
            child.parent_conversation_id = intermediate.id
            child.root_conversation_id = root.id
        conversations.extend([root, intermediate])
    registry = MockTunnelRegistry()
    router = RunnerRouter(
        registry=registry,
        conversation_store=MockConversationStore(*conversations),
        host_registry=MockHostRegistry({"host_root": "local_connection"}),
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


@pytest.mark.parametrize("runner_id", [None, "runner_shared", "runner_different"])
@pytest.mark.parametrize("side_chat", [False, True])
def test_fork_source_only_routes_side_chats_sharing_the_source_runner(runner_id, side_chat):
    source = Conversation(
        id="source",
        created_at=1,
        updated_at=1,
        root_conversation_id="source",
        runner_id="runner_shared",
        host_id="host_source",
    )
    fork = Conversation(
        id="fork",
        created_at=1,
        updated_at=1,
        root_conversation_id="fork",
        runner_id=runner_id,
        labels={
            SIDE_CHAT_SOURCE_LABEL_KEY: source.id,
            SIDE_CHAT_LABEL_KEY: "1" if side_chat else "0",
        },
    )

    host_id = routing_host_id(fork, MockConversationStore(source, fork))

    assert host_id == ("host_source" if side_chat and runner_id != "runner_different" else None)
    assert fork.host_id is None
    assert fork.parent_conversation_id is None


@pytest.mark.parametrize("depth", [0, 17])
@pytest.mark.parametrize("max_reads", [0, 1, 2, 16])
def test_side_chat_routing_respects_read_budget_and_subagent_root_fallback(max_reads, depth):
    root = Conversation(
        id="root",
        created_at=1,
        updated_at=1,
        root_conversation_id="root",
        runner_id="runner_shared",
        host_id="host_root",
    )
    source = Conversation(
        id="source",
        created_at=1,
        updated_at=1,
        kind="sub_agent",
        root_conversation_id=root.id,
        runner_id="runner_shared",
    )
    side_chat = Conversation(
        id="side",
        created_at=1,
        updated_at=1,
        root_conversation_id="side",
        runner_id=source.runner_id,
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: source.id},
    )

    ancestors = [
        Conversation(
            id=f"ancestor_{index}",
            created_at=1,
            updated_at=1,
            kind="sub_agent",
            parent_conversation_id=f"ancestor_{index + 1}" if index + 1 < depth else root.id,
            root_conversation_id=root.id,
            runner_id=root.runner_id,
        )
        for index in range(depth)
    ]
    source.parent_conversation_id = ancestors[0].id if ancestors else None
    store = MockConversationStore(root, source, side_chat, *ancestors)
    reads = []

    def read(session_id):
        reads.append(session_id)
        return store.rows.get(session_id)

    store.get_conversation = read
    assert routing_host_id(side_chat, store, max_ancestor_reads=max_reads) == (
        "host_root" if max_reads >= 2 else None
    )
    assert len(reads) <= max_reads
    if max_reads == 16:
        router = RunnerRouter(
            registry=MockTunnelRegistry(),
            conversation_store=store,
            host_registry=MockHostRegistry(),
            host_store=MockHostStore({root.host_id: True}),
        )
        for child in (side_chat, source):
            reads.clear()
            with pytest.raises(OmnigentError) as caught:
                router.client_for_session_resources(child.id, conversation=child)
            assert caught.value.code == ErrorCode.WRONG_REPLICA
            assert len(reads) <= 16


def test_side_chat_routing_handles_cyclic_source_links():
    side_chat = Conversation(
        id="side",
        created_at=1,
        updated_at=1,
        root_conversation_id="side",
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "source"},
    )
    source = Conversation(
        id="source",
        created_at=1,
        updated_at=1,
        root_conversation_id="source",
        labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: side_chat.id},
    )

    assert routing_host_id(side_chat, MockConversationStore(side_chat, source)) is None


def test_client_writable_fork_provenance_is_not_routing_authority():
    child = Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        root_conversation_id="child",
        labels={SIDE_CHAT_LABEL_KEY: "1", "omnigent.fork.source_id": "private_source"},
    )
    store = MockConversationStore(child)

    def reject_read(_session_id):
        raise AssertionError("Client-supplied ancestry must not be dereferenced")

    store.get_conversation = reject_read
    assert routing_host_id(child, store) is None


@pytest.mark.parametrize("depth", [16, 17])
def test_default_routing_read_budget_bounds_acyclic_side_chat_chains(depth):
    chain = [
        Conversation(
            id=str(index),
            created_at=1,
            updated_at=1,
            root_conversation_id=str(index),
            labels={SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: str(index + 1)},
            host_id="host_root" if index == depth else None,
        )
        for index in range(depth + 1)
    ]
    store = MockConversationStore(*chain)
    reads = []

    def read(session_id):
        reads.append(session_id)
        return store.rows.get(session_id)

    store.get_conversation = read
    assert routing_host_id(chain[0], store) == ("host_root" if depth == 16 else None)
    assert len(reads) == 16


@pytest.mark.parametrize("root_host", [None, "host_root"])
@pytest.mark.parametrize("broken_parent", ["missing", "cycle"])
def test_routing_host_handles_broken_ancestry(root_host, broken_parent):
    root = Conversation(
        id="root", created_at=1, updated_at=1, root_conversation_id="root", host_id=root_host
    )
    child = Conversation(
        id="child",
        created_at=1,
        updated_at=1,
        kind="sub_agent",
        parent_conversation_id="parent",
        root_conversation_id=root.id,
    )
    conversations = [root, child]
    if broken_parent == "cycle":
        conversations.append(
            Conversation(
                id="parent",
                created_at=1,
                updated_at=1,
                kind="sub_agent",
                parent_conversation_id=child.id,
                root_conversation_id=root.id,
            )
        )

    assert routing_host_id(child, MockConversationStore(*conversations)) == root_host
