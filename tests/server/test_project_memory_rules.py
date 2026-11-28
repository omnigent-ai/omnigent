"""Who may write where in a project's memory."""

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.project_memory import ProjectMemoryCaller, require_project_memory_write

PROJECT = "a" * 32


def _caller(*, is_agent: bool, identity_id: str | None = None, may_edit: bool = False):
    return ProjectMemoryCaller(
        PROJECT, is_agent=is_agent, identity_id=identity_id, may_edit=may_edit
    )


@pytest.mark.parametrize(
    ("caller", "path", "code"),
    [
        (_caller(is_agent=True, identity_id="pm"), "/project/stack", None),
        (_caller(is_agent=True, identity_id="pm"), "/identities/pm/plan", None),
        (_caller(is_agent=True, identity_id="pm"), "/identities/pm", None),
        (
            _caller(is_agent=True, identity_id="pm"),
            "/identities/designer/plan",
            ErrorCode.FORBIDDEN,
        ),
        (_caller(is_agent=True, identity_id="pm"), "/identities/pmx/plan", ErrorCode.FORBIDDEN),
        (_caller(is_agent=True), "/project/stack", ErrorCode.FORBIDDEN),
        (_caller(is_agent=False, may_edit=True), "/identities/designer/plan", None),
        # A person who can't edit the project, such as a future viewer role, only reads.
        (_caller(is_agent=False), "/project/stack", ErrorCode.FORBIDDEN),
        (_caller(is_agent=False, may_edit=True), "/user/notes", ErrorCode.INVALID_INPUT),
        (_caller(is_agent=False, may_edit=True), "/identities", ErrorCode.INVALID_INPUT),
        (_caller(is_agent=False, may_edit=True), "/projects/api", ErrorCode.INVALID_INPUT),
    ],
)
def test_write_rules(caller, path, code):
    if code is None:
        require_project_memory_write(caller, path)
        return
    with pytest.raises(OmnigentError) as raised:
        require_project_memory_write(caller, path)
    assert raised.value.code == code


def test_writable_areas():
    assert _caller(is_agent=True, identity_id="pm").writable_areas() == [
        "/project",
        "/identities/pm",
    ]
    assert _caller(is_agent=True).writable_areas() == []
    assert _caller(is_agent=False, may_edit=True).writable_areas() == ["/project", "/identities"]
    assert _caller(is_agent=False).writable_areas() == []
