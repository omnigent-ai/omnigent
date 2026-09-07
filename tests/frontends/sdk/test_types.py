"""Tests for Python SDK response dataclasses."""

from __future__ import annotations

import pytest
from omnigent_client._types import File, PaginatedList, Response


def test_response_from_dict_parses_integer_compatible_json_scalars() -> None:
    response = Response.from_dict(
        {
            "id": "resp_1",
            "status": "completed",
            "model": "agent",
            "output": [{"type": "message"}],
            "created_at": "10",
            "completed_at": 12.0,
            "usage": {
                "input_tokens": "3",
                "output_tokens": 4.0,
                "total_tokens": 7,
            },
        }
    )

    assert response.created_at == 10
    assert response.completed_at == 12
    assert response.output == [{"type": "message"}]
    assert response.usage is not None
    assert response.usage.total_tokens == 7


def test_file_from_dict_rejects_non_scalar_integer_fields() -> None:
    with pytest.raises(TypeError, match="expected a JSON number or numeric string"):
        File.from_dict(
            {
                "id": "file_1",
                "filename": "report.txt",
                "bytes": {},
                "created_at": 10,
            }
        )


def test_collection_fields_fall_back_when_payload_is_not_a_list() -> None:
    response = Response.from_dict(
        {
            "id": "resp_1",
            "status": "completed",
            "model": "agent",
            "output": {"type": "message"},
        }
    )
    page = PaginatedList.from_dict({"data": {"id": "file_1"}})

    assert response.output == []
    assert page.data == []
