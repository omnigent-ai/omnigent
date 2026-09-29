"""Explicit account generations retain revocation checks under external auth."""

from contextlib import ExitStack

import pytest
from sqlalchemy.orm import Session

from omnigent.db.account_authority import (
    account_authority_scope,
    account_checks_scope,
    require_active_account,
    target_account_scope,
)
from omnigent.db.db_models import SqlUser, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import ErrorCode, OmnigentError


@pytest.mark.parametrize("source", ["actor", "target", "related", "argument"])
@pytest.mark.parametrize("state", ["active", "deleted", "replaced", "missing"])
def test_external_auth_still_validates_saved_generations(db_uri, source, state):
    engine = get_or_create_engine(db_uri)
    generation = "a" * 32
    with Session(engine) as session:
        if state != "missing":
            session.add(
                SqlUser(
                    workspace_id=0,
                    id="alice",
                    account_generation="b" * 32 if state == "replaced" else generation,
                    deleted_at=1 if state == "deleted" else None,
                )
            )
            session.commit()
        with ExitStack() as scopes:
            scopes.enter_context(account_checks_scope(False))
            kwargs = {}
            if source == "actor":
                scopes.enter_context(account_authority_scope("alice", generation))
            elif source == "target":
                scopes.enter_context(target_account_scope("alice", generation))
            elif source == "related":
                kwargs["related_accounts"] = {"alice": generation}
            else:
                kwargs["generation"] = generation
            if state == "active":
                assert require_active_account(session, "alice", **kwargs) == generation
            else:
                with pytest.raises(OmnigentError) as error:
                    require_active_account(session, "alice", **kwargs)
                expected = ErrorCode.CONFLICT if source == "target" else ErrorCode.UNAUTHORIZED
                assert error.value.code == expected


def test_generation_checks_respect_workspace(db_uri):
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        session.add(SqlUser(workspace_id=1, id="alice", account_generation="a" * 32))
        session.add(SqlUser(workspace_id=2, id="alice", account_generation="b" * 32))
        session.commit()
        with account_checks_scope(False), workspace_scope(2):
            with pytest.raises(OmnigentError) as error:
                require_active_account(session, "alice", generation="a" * 32)
            assert error.value.code == ErrorCode.UNAUTHORIZED
