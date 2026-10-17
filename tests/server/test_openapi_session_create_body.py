"""OpenAPI ``requestBody`` for ``POST /v1/sessions`` (issue #8688).

The route parses its body by hand to dispatch on ``Content-Type``, so FastAPI
inferred no ``requestBody`` and generated clients could not discover either
accepted shape. These tests read the live ``/openapi.json`` and check:

- Both content types are documented and the body is required.
- JSON references both create models; multipart references the metadata
  model, carries a ``bundle`` file part, and sends ``metadata`` as JSON.
- Every ``$ref`` in the document resolves, including nested models such as
  ``initial_items`` -> ``SessionEventInput``.
- The documented fields are exactly the model fields, so the spec cannot
  drift from what the route validates.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from omnigent.server.schemas import (
    ProjectSessionCreateRequest,
    SessionCreateMetadata,
    SessionCreateRequest,
)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PREFIX = "#/components/schemas/"


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:  # type: ignore[explicit-any]
    """The live ``/openapi.json`` of an app built like ``scripts/dump_openapi.py`` does."""
    path = _REPO_ROOT / "scripts" / "dump_openapi.py"
    module_spec = importlib.util.spec_from_file_location("scripts_dump_openapi_body", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["scripts_dump_openapi_body"] = module
    module_spec.loader.exec_module(module)
    app = module._build_app_with_stub_stores()
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()  # type: ignore[explicit-any]
    return body


def _request_body(spec: dict[str, Any]) -> dict[str, Any]:  # type: ignore[explicit-any]
    operation = spec["paths"]["/v1/sessions"]["post"]
    assert "requestBody" in operation, "POST /v1/sessions documents no request body."
    body: dict[str, Any] = operation["requestBody"]  # type: ignore[explicit-any]
    return body


def test_both_content_types_are_documented(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    body = _request_body(spec)
    assert body["required"] is True
    assert set(body["content"]) == {"application/json", "multipart/form-data"}


def test_json_body_references_both_create_models(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    schema = _request_body(spec)["content"]["application/json"]["schema"]
    assert schema == {
        "anyOf": [
            {"$ref": f"{_PREFIX}SessionCreateRequest"},
            {"$ref": f"{_PREFIX}ProjectSessionCreateRequest"},
        ]
    }


def test_multipart_body_has_json_metadata_and_a_bundle_file(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    multipart = _request_body(spec)["content"]["multipart/form-data"]
    schema = multipart["schema"]
    assert set(schema["required"]) == {"metadata", "bundle"}
    assert schema["properties"]["metadata"] == {"$ref": f"{_PREFIX}SessionCreateMetadata"}
    assert schema["properties"]["bundle"]["type"] == "string"
    assert multipart["encoding"]["metadata"]["contentType"] == "application/json"


def test_every_ref_reachable_from_the_body_resolves(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    """A dangling ``$ref`` breaks client generators for the whole document.

    Follows refs transitively from the request body (e.g. ``initial_items``
    -> ``SessionEventInput``). ``ServerStreamEvent`` is added later by
    ``scripts/dump_openapi.py`` and is not reachable from here.
    """
    schemas = spec["components"]["schemas"]
    pending = set(re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(_request_body(spec))))
    seen: set[str] = set()
    missing: list[str] = []
    while pending:
        name = pending.pop()
        seen.add(name)
        if name not in schemas:
            missing.append(name)
            continue
        found = re.findall(r'"#/components/schemas/([^"]+)"', json.dumps(schemas[name]))
        pending |= set(found) - seen
    assert missing == []
    assert {"SessionCreateRequest", "SessionEventInput", "SessionCreateMetadata"} <= seen


def test_initial_items_reference_session_event_input(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    items = spec["components"]["schemas"]["SessionCreateRequest"]["properties"]["initial_items"]
    assert items["items"] == {"$ref": f"{_PREFIX}SessionEventInput"}


@pytest.mark.parametrize(
    "model", [SessionCreateRequest, ProjectSessionCreateRequest, SessionCreateMetadata]
)
def test_documented_fields_match_the_model(
    spec: dict[str, Any],  # type: ignore[explicit-any]
    model: type[BaseModel],
) -> None:
    """Adding a field to a create model must show up in the spec."""
    documented = spec["components"]["schemas"][model.__name__]["properties"]
    assert set(documented) == set(model.model_fields)


def test_metadata_rejects_unknown_fields_in_the_spec_too(spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    """``SessionCreateMetadata`` forbids extra keys; clients should see that."""
    schema = spec["components"]["schemas"]["SessionCreateMetadata"]
    assert schema.get("additionalProperties") is False
