"""Validation audit rows identify schema fields without recording rejected values."""

from __future__ import annotations

import json
from importlib import import_module

import httpx
import pytest


async def test_request_validation_audits_types_and_paths_without_input(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_module = import_module("omnigent.server.app")
    original = app_module.add_audit_attrs
    captured: list[dict[str, str]] = []

    def record(**attrs: str) -> None:
        captured.append(attrs)
        original(**attrs)

    monkeypatch.setattr(app_module, "add_audit_attrs", record)
    response = await client.post(
        "/v1/sessions/validation-session/events",
        json={"type": 42, "data": "private-rejected-input"},
    )
    assert response.status_code == 422
    audit = next(row for row in captured if "validation_error_count" in row)
    assert audit["validation_error_count"] == "3"
    assert json.loads(audit["validation_error_types"]) == ["dict_type", "list_type", "string_type"]
    assert json.loads(audit["validation_error_locations"]) == [
        ["body", "field", "type"],
        ["body", "field", "data"],
        ["body", "field"],
    ]
    assert "private-rejected-input" not in json.dumps(captured)
    assert "validation" not in response.json(), response.json()


@pytest.mark.parametrize(
    "location",
    [
        ("body", "data", "private-dictionary-key"),
        ("body", "items", 987654321012345),
        ("body", "private-union-tag", "secret-field"),
    ],
)
def test_validation_summary_redacts_dynamic_location_components(
    location: tuple[object, ...],
) -> None:
    from omnigent.server.validation_audit import validation_audit_attrs

    result = validation_audit_attrs(
        [
            {
                "type": "private-custom-error-type",
                "loc": location,
                "msg": "private-message",
                "input": "private-input",
                "ctx": {"secret": "private-context"},
            }
        ]
    )
    wire = json.dumps(result)
    assert "private" not in wire
    assert "987654321012345" not in wire
    assert json.loads(result["validation_error_types"]) == ["custom"]


def test_validation_summary_bounds_samples_and_path_depth() -> None:
    from omnigent.server.validation_audit import validation_audit_attrs

    result = validation_audit_attrs(
        [{"type": "string_type", "loc": ("body",) * 100, "input": "sensitive"} for _ in range(100)]
    )
    assert result["validation_error_count"] == "100"
    locations = json.loads(result["validation_error_locations"])
    assert len(locations) == 8
    assert all(len(path) == 12 for path in locations)
    assert len(json.dumps(result)) < 1500
