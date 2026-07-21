"""Contract tests for a session-scoped, non-human runner principal."""

from __future__ import annotations

import importlib
import importlib.util
from dataclasses import FrozenInstanceError
from types import ModuleType

import pytest

from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_BINDING_TOKEN = "runner-capability-token"
_OTHER_TOKEN = "another-runner-capability-token"


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """Return a relational conversation store for capability resolution."""
    return SqlAlchemyConversationStore(db_uri)


def _capabilities() -> ModuleType:
    """Load the capability API, producing the intended initial red failure."""
    module_name = "omnigent.server.runner_capabilities"
    assert importlib.util.find_spec(module_name) is not None, (
        f"{module_name} is not implemented; add it from "
        "specs/003-shared-external-host-access/contracts/runner-capability.md"
    )
    return importlib.import_module(module_name)


def _bound_conversation(
    store: SqlAlchemyConversationStore,
    token: str = _BINDING_TOKEN,
) -> str:
    """Create a conversation bound to the token-derived runner."""
    conversation = store.create_conversation()
    store.replace_runner_id(conversation.id, token_bound_runner_id(token))
    return conversation.id


def test_matching_token_authenticates_non_human_principal(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A matching token resolves a principal without a human identity."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)

    principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )

    assert principal is not None
    assert principal.runner_id == token_bound_runner_id(_BINDING_TOKEN)
    assert principal.conversation_id == conversation_id
    assert not hasattr(principal, "user_id")
    assert not hasattr(principal, "permission_level")
    assert not hasattr(principal, "token")


def test_runner_principal_is_immutable(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Authorization context cannot be rebound after authentication."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)
    principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )
    assert principal is not None

    with pytest.raises(FrozenInstanceError):
        principal.conversation_id = "conv_other"


@pytest.mark.parametrize("token", [None, "", "   ", _OTHER_TOKEN])
def test_missing_blank_or_wrong_token_fails_closed(
    conversation_store: SqlAlchemyConversationStore,
    token: str | None,
) -> None:
    """Invalid proof produces no runner principal."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)

    assert capabilities.authenticate_runner(token, conversation_id, conversation_store) is None


def test_token_is_scoped_to_one_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A valid token for one session grants no principal on another."""
    capabilities = _capabilities()
    _bound_conversation(conversation_store)
    other_conversation_id = _bound_conversation(conversation_store, _OTHER_TOKEN)

    assert (
        capabilities.authenticate_runner(
            _BINDING_TOKEN,
            other_conversation_id,
            conversation_store,
        )
        is None
    )


def test_relaunch_invalidates_previous_token(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Replacing the current runner implicitly revokes the old token."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)

    old_principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )
    assert old_principal is not None

    conversation_store.replace_runner_id(
        conversation_id,
        token_bound_runner_id(_OTHER_TOKEN),
    )

    assert (
        capabilities.authenticate_runner(
            _BINDING_TOKEN,
            conversation_id,
            conversation_store,
        )
        is None
    )
    assert (
        capabilities.authenticate_runner(
            _OTHER_TOKEN,
            conversation_id,
            conversation_store,
        )
        is not None
    )


@pytest.mark.parametrize(
    "action_name",
    [
        "READ_SESSION",
        "READ_SESSION_SPEC",
        "APPEND_EVENT",
        "REPORT_USAGE",
        "EVALUATE_POLICY",
        "PROXY_MCP",
    ],
)
def test_runner_action_allow_list(
    conversation_store: SqlAlchemyConversationStore,
    action_name: str,
) -> None:
    """The bound principal may perform each declared runner callback."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)
    principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )
    assert principal is not None

    action = getattr(capabilities.RunnerAction, action_name)
    assert capabilities.runner_allows(principal, conversation_id, action)


@pytest.mark.parametrize(
    "action",
    [
        "send_user_message",
        "write_comment",
        "manage_permissions",
        "delete_session",
        "fork_session",
        "operate_host",
        "unknown_future_action",
    ],
)
def test_human_and_unknown_actions_are_denied(
    conversation_store: SqlAlchemyConversationStore,
    action: str,
) -> None:
    """Possessing a runner token does not confer general edit authority."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)
    principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )
    assert principal is not None

    assert not capabilities.runner_allows(principal, conversation_id, action)


def test_principal_cannot_authorize_another_conversation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Authorization rechecks scope even after successful authentication."""
    capabilities = _capabilities()
    conversation_id = _bound_conversation(conversation_store)
    other_conversation_id = _bound_conversation(conversation_store, _OTHER_TOKEN)
    principal = capabilities.authenticate_runner(
        _BINDING_TOKEN,
        conversation_id,
        conversation_store,
    )
    assert principal is not None

    assert not capabilities.runner_allows(
        principal,
        other_conversation_id,
        capabilities.RunnerAction.READ_SESSION,
    )
