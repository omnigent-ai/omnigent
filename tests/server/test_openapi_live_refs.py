"""The live ``/openapi.json`` must define every schema it references.

Covers the document the server serves (and ``/docs`` renders): no dangling
``$ref``, and the stream route's ``ServerStreamEvent`` union matches the one
in the dumped ``openapi.json`` artifact.
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

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_REF = re.compile(r'"#/components/schemas/([^"]+)"')


@pytest.fixture(scope="module")
def dump_module() -> Any:  # type: ignore[explicit-any]
    """``scripts/dump_openapi.py``, loaded once for the module."""
    path = _REPO_ROOT / "scripts" / "dump_openapi.py"
    module_spec = importlib.util.spec_from_file_location("scripts_dump_openapi_refs", path)
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    sys.modules["scripts_dump_openapi_refs"] = module
    module_spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def live_spec(dump_module: Any) -> dict[str, Any]:  # type: ignore[explicit-any]
    """The document the server serves at ``/openapi.json``."""
    app = dump_module._build_app_with_stub_stores()
    response = TestClient(app).get("/openapi.json")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()  # type: ignore[explicit-any]
    return body


def test_live_spec_has_no_dangling_refs(live_spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    """Every ``$ref`` must name a schema the document defines."""
    referenced = set(_REF.findall(json.dumps(live_spec)))
    missing = sorted(referenced - set(live_spec["components"]["schemas"]))
    assert missing == [], f"Undefined schemas referenced by /openapi.json: {missing}"


def test_stream_route_union_and_variants_are_defined(live_spec: dict[str, Any]) -> None:  # type: ignore[explicit-any]
    schemas = live_spec["components"]["schemas"]
    stream = live_spec["paths"]["/v1/sessions/{session_id}/stream"]["get"]
    content = stream["responses"]["200"]["content"]["text/event-stream"]
    assert content["schema"] == {"$ref": "#/components/schemas/ServerStreamEvent"}
    union = schemas["ServerStreamEvent"]
    variants = set(_REF.findall(json.dumps(union)))
    assert len(variants) > 1, "ServerStreamEvent should be a union of event schemas."
    assert variants <= set(schemas)


def test_live_and_dumped_specs_share_the_union(
    live_spec: dict[str, Any],  # type: ignore[explicit-any]
    dump_module: Any,  # type: ignore[explicit-any]
) -> None:
    """One definition: the committed artifact and the live document agree."""
    dumped = dump_module.generate_spec()
    assert (
        dumped["components"]["schemas"]["ServerStreamEvent"]
        == live_spec["components"]["schemas"]["ServerStreamEvent"]
    )
