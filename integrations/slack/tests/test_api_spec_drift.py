"""Guard the Slack client against Omnigent API spec drift.

The Slack integration is deliberately decoupled from the ``omnigent`` server
package (it can't import it), so it can't introspect the live FastAPI app. But
the repo commits the server's generated OpenAPI document at ``openapi.json``
(produced by ``scripts/dump_openapi.py`` and itself drift-guarded server-side by
``tests/server/test_openapi_drift.py``). That artifact is a reliable, in-repo
description of the server contract.

This test reconciles the endpoints the Slack client actually calls
(:data:`OMNIGENT_ENDPOINTS` in ``fakes.py``, kept next to the fake server) with
that document, so:

- If the server RENAMES or REMOVES a documented endpoint/method the bot depends
  on (e.g. ``GET /v1/agents`` → ``GET /v1/available-agents``), a Slack test
  fails with a precise message — instead of the bot 404-ing silently against a
  deployed server.
- If a field the client reads off a response schema disappears
  (``SessionResponse.harness``, ``PaginatedList.data``), a test fails.
- If an endpoint the client treats as INTERNAL suddenly appears in the public
  schema, a test fails — a nudge to reclassify it deliberately.

The catalog is intentionally hand-maintained (it encodes *what the client
needs*, which no tool can infer); this test is what keeps it honest against the
server's own source of truth.

If ``openapi.json`` can't be found (e.g. the Slack package is checked out in
isolation without the parent repo), the reconciliation tests skip rather than
fail — the catalog-consistency test still runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fakes import OMNIGENT_ENDPOINTS, OMNIGENT_RESPONSE_FIELDS

# ``openapi.json`` lives at the monorepo root: integrations/slack/tests/ → ../../../
_OPENAPI_PATH = Path(__file__).resolve().parents[3] / "openapi.json"


def _load_spec() -> dict[str, Any] | None:
    if not _OPENAPI_PATH.is_file():
        return None
    return json.loads(_OPENAPI_PATH.read_text())


_SPEC = _load_spec()
_needs_spec = pytest.mark.skipif(
    _SPEC is None, reason=f"openapi.json not found at {_OPENAPI_PATH}"
)

_DOCUMENTED = [(m, p) for m, p, documented in OMNIGENT_ENDPOINTS if documented]
_INTERNAL = [(m, p) for m, p, documented in OMNIGENT_ENDPOINTS if not documented]


def test_endpoint_catalog_is_well_formed() -> None:
    """The catalog itself is sane — no duplicates, valid methods, absolute paths.

    Runs even without ``openapi.json`` so a malformed catalog is always caught.
    """
    seen: set[tuple[str, str]] = set()
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE"}
    for method, path, _documented in OMNIGENT_ENDPOINTS:
        key = (method, path)
        assert key not in seen, f"duplicate catalog entry: {method} {path}"
        seen.add(key)
        assert method in valid_methods, f"unexpected method {method} for {path}"
        assert path.startswith("/"), f"path must be absolute: {path}"


@_needs_spec
@pytest.mark.parametrize(("method", "path"), _DOCUMENTED, ids=lambda v: v)
def test_documented_endpoint_present_in_openapi(method: str, path: str) -> None:
    """Every endpoint the client depends on (and the server documents) still
    exists in ``openapi.json`` with the expected method."""
    assert _SPEC is not None
    paths = _SPEC["paths"]
    assert path in paths, (
        f"{method} {path}: the Slack client calls this endpoint but it is gone "
        f"from openapi.json — the server renamed/removed it, or the client and "
        f"server drifted. Update OmnigentClient and the fakes.py catalog together."
    )
    methods = {m.upper() for m in paths[path]}
    assert method in methods, (
        f"{path}: client expects {method}, but openapi.json documents {sorted(methods)}."
    )


@_needs_spec
@pytest.mark.parametrize(("method", "path"), _INTERNAL, ids=lambda v: v)
def test_internal_endpoint_absent_from_openapi(method: str, path: str) -> None:
    """Endpoints the client treats as internal stay hidden from the public schema.

    If one starts appearing, that's a deliberate server decision — reclassify it
    in the catalog (``documented=True``) so it's covered by the presence check
    instead."""
    assert _SPEC is not None
    if path not in _SPEC["paths"]:
        return
    methods = {m.upper() for m in _SPEC["paths"][path]}
    assert method not in methods, (
        f"{method} {path} is now in the public OpenAPI schema but the Slack "
        f"catalog marks it internal (documented=False). If this is intended, "
        f"flip it to documented=True in fakes.py's OMNIGENT_ENDPOINTS."
    )


@_needs_spec
@pytest.mark.parametrize("schema_name", sorted(OMNIGENT_RESPONSE_FIELDS))
def test_response_fields_present_in_schema(schema_name: str) -> None:
    """Fields the client reads off documented response schemas still exist.

    A silent rename (e.g. ``harness`` → ``harness_kind``) would make
    ``get_session_info`` return ``None`` forever; pin the field names."""
    assert _SPEC is not None
    schemas = _SPEC.get("components", {}).get("schemas", {})
    assert schema_name in schemas, (
        f"schema {schema_name} vanished from openapi.json — the client parses it; "
        f"update the OMNIGENT_RESPONSE_FIELDS catalog and OmnigentClient together."
    )
    properties = schemas[schema_name].get("properties", {})
    for field in OMNIGENT_RESPONSE_FIELDS[schema_name]:
        assert field in properties, (
            f"{schema_name}.{field} is gone from openapi.json but the Slack client "
            f"reads it. Confirm the server rename and update the client + catalog."
        )
