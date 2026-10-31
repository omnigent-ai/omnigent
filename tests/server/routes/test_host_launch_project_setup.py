"""Which project setup a runner launch sends to its host."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.host.frames import CAP_PROJECT_SETUP
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes import _host_launch
from omnigent.server.routes._host_launch import resolve_launch_project_setup

_CONFIG = {"env_commands": ["conda activate proj"], "setup_commands": ["npm ci"]}


class _Projects:
    """Projects owned by ``owner``; another owner sees none."""

    def __init__(self, owner: str | None) -> None:
        self.owner = owner

    def get(self, project_id: str, *, user_id: str | None) -> Any:
        return SimpleNamespace(config=_CONFIG) if user_id == self.owner else None


def _host(owner: str | None, *, managed: bool = False, capable: bool = True) -> Any:
    capabilities = [CAP_PROJECT_SETUP] if capable else []
    return SimpleNamespace(
        owner=owner,
        hello=SimpleNamespace(capabilities=capabilities),
        registered_with_managed_token=managed,
    )


_SESSION: Any = SimpleNamespace(id="conv_1", project_id="proj_1")


@pytest.mark.parametrize("project_owner", [None, RESERVED_USER_LOCAL])
async def test_single_user_projects_resolve_under_either_local_owner(
    monkeypatch: pytest.MonkeyPatch, project_owner: str | None
) -> None:
    monkeypatch.setattr(_host_launch, "_project_store", _Projects(project_owner))

    setup = await resolve_launch_project_setup(_SESSION, _host(RESERVED_USER_LOCAL))

    assert setup.wire is not None and setup.wire["setup_commands"] == ["npm ci"]


@pytest.mark.parametrize(
    ("host", "unsupported"),
    [
        (_host("alice@example.com", managed=True), False),
        (_host("bob@example.com"), False),
        (_host("alice@example.com", capable=False), True),
    ],
    ids=["managed-sandbox", "other-owners-host", "older-host"],
)
async def test_hosts_that_must_not_run_the_commands_get_none(
    monkeypatch: pytest.MonkeyPatch, host: Any, unsupported: bool
) -> None:
    monkeypatch.setattr(_host_launch, "_project_store", _Projects("alice@example.com"))

    setup = await resolve_launch_project_setup(_SESSION, host)

    assert setup.wire is None
    assert setup.unsupported is unsupported
