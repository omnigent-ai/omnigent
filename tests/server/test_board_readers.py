"""Board readers: the owner's list of other projects whose Lead may read the board."""

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.board_readers import board_readers, may_read_board, validate_board_readers

SELF, SISTER, OTHER = "a" * 32, "b" * 32, "c" * 32


def _project(config):
    return SimpleNamespace(id=SELF, config=config)


def test_board_readers_keeps_only_listed_project_ids():
    assert board_readers(_project(None)) == []
    assert board_readers(_project({"board_readers": "x"})) == []
    project = _project({"board_readers": [SISTER, "junk", SISTER, 3, OTHER]})
    assert board_readers(project) == [SISTER, OTHER]
    assert may_read_board(project, SISTER)
    assert not may_read_board(project, SELF)
    assert not may_read_board(_project({}), SISTER)


@pytest.mark.parametrize(
    "listed", [[SELF], [SISTER, SISTER], ["B" * 32], "x", [f"{index:032x}" for index in range(21)]]
)
def test_validate_refuses_malformed_lists(listed):
    with pytest.raises(OmnigentError):
        validate_board_readers({"board_readers": listed}, SELF)


def test_validate_accepts_a_missing_or_valid_list():
    validate_board_readers(None, SELF)
    validate_board_readers({"icon": "x"}, SELF)
    validate_board_readers({"board_readers": [SISTER, OTHER]}, SELF)
    validate_board_readers({"board_readers": []}, SELF)
