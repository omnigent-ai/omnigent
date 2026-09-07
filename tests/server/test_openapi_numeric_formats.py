"""Numeric ``format`` guard for the checked-in OpenAPI artifact.

Whereas :mod:`tests.server.test_openapi_drift` guards that
:file:`openapi.json` matches what ``scripts/dump_openapi.py`` emits,
this test guards a *property* of that document: every numeric schema
node under ``components.schemas`` must carry an explicit ``format``
telling typed-language generators how wide the value is.

Without a format, generators guess narrow:

* a formatless ``{"type": "integer"}`` maps to a 32-bit type
  (``Integer`` in Java, ``int?`` in C#, ``int32`` semantics in Go), so
  ``SessionListItem.comments_updated_at`` — documented as Unix epoch
  **microseconds**, ~1.7e15 today — cannot be decoded at all, and the
  epoch-seconds ``created_at``/``updated_at`` fields are a 2038
  overflow;
* a formatless ``{"type": "number"}`` maps to ``float32`` in
  ``oapi-codegen``, so the USD cost fields silently hold a narrower
  value than the server's IEEE-754 binary64 ``float`` computes.

The required stamps are ``format: int64`` on integers (pure widening)
and ``format: double`` on numbers (matches the producer's binary64).
Both are annotation-only in OAS 3.1+ — no wire-format or validation
change. ``default``/``example``/``examples``/``const``/``enum`` values
are data payloads, not schema nodes, so the walk skips them.

The drift test pins ``openapi.json`` byte-for-byte to the live
``generate_spec()`` output, so asserting on the checked-in artifact is
equivalent to asserting on the running server's schema seam.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Repo-root artifact under test (same layout convention as
# tests/server/test_openapi_drift.py).
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_OPENAPI_JSON_PATH: Path = _REPO_ROOT / "openapi.json"

# Keys whose values are data payloads, not schema nodes. A default or
# example that happens to look like a schema must not be walked.
_NON_SCHEMA_KEYS: frozenset[str] = frozenset({"default", "example", "examples", "const", "enum"})

# The one format each numeric type must carry. ``int64`` is a pure
# widening (Integer -> Long); ``double`` matches the server's Python
# float, which is IEEE-754 binary64.
_REQUIRED_FORMATS: dict[str, str] = {
    "integer": "int64",
    "number": "double",
}


def _load_component_schemas() -> dict[str, Any]:
    """
    Load ``components.schemas`` from the checked-in ``openapi.json``.

    :returns: The ``components.schemas`` mapping of the artifact.
    """
    assert _OPENAPI_JSON_PATH.exists(), (
        f"openapi.json not found at {_OPENAPI_JSON_PATH}. The artifact "
        f"must exist at the repo root (regenerate with "
        f"`python scripts/dump_openapi.py`)."
    )
    with _OPENAPI_JSON_PATH.open(encoding="utf-8") as fh:
        spec = json.load(fh)
    schemas = spec.get("components", {}).get("schemas", {})
    assert isinstance(schemas, dict) and schemas, (
        "openapi.json has no components.schemas — the artifact is "
        "malformed or the layout changed; update this test's loader."
    )
    return schemas


def _collect_numeric_format_violations(
    node: Any,
    json_type: str,
    path: str,
    violations: list[str],
    *,
    keys_are_names: bool = False,
) -> None:
    """
    Recursively collect numeric nodes missing the required ``format``.

    Walks dicts and lists, skipping data-payload keys, and records a
    dotted path for every ``type: <json_type>`` schema node whose
    ``format`` is absent or not the required value.

    :param node: Current JSON node.
    :param json_type: ``"integer"`` or ``"number"``.
    :param path: Dotted path to ``node`` for the failure message.
    :param violations: Accumulator for violation descriptions.
    :param keys_are_names: ``node`` is a map of user-chosen names to
        schemas (``schemas``/``properties``), so a property literally
        named ``default`` or ``enum`` is still a schema to check.
    """
    if isinstance(node, dict):
        if not keys_are_names and node.get("type") == json_type:
            required = _REQUIRED_FORMATS[json_type]
            actual = node.get("format")
            if actual != required:
                violations.append(
                    f"{path}: type={json_type} format={actual!r} (expected {required!r})"
                )
        for key, value in node.items():
            if not keys_are_names and key in _NON_SCHEMA_KEYS:
                continue
            _collect_numeric_format_violations(
                value,
                json_type,
                f"{path}.{key}",
                violations,
                keys_are_names=(not keys_are_names and key in ("properties", "patternProperties")),
            )
    elif isinstance(node, list):
        for index, item in enumerate(node):
            _collect_numeric_format_violations(item, json_type, f"{path}[{index}]", violations)


def test_integer_schemas_carry_int64_format() -> None:
    """
    Every ``type: integer`` under ``components.schemas`` has
    ``format: int64``.

    A formatless integer maps to a 32-bit type in generated clients
    (openapi-generator: Java ``Integer``, C# ``int?``; Go semantics
    ``int32``), which cannot decode
    ``SessionListItem.comments_updated_at`` — a Unix epoch
    **microseconds** value, ~817,000x int32 max — and overflows in
    2038 for the epoch-seconds ``created_at``/``updated_at`` fields.
    """
    schemas = _load_component_schemas()
    violations: list[str] = []
    _collect_numeric_format_violations(
        schemas, "integer", "components.schemas", violations, keys_are_names=True
    )
    assert not violations, (
        f"{len(violations)} integer schema node(s) in openapi.json lack "
        f"format: int64, so generated clients type them 32-bit and "
        f"cannot decode epoch-microsecond values like "
        f"SessionListItem.comments_updated_at (~1.7e15 today). "
        f"Stamp numeric formats in scripts/dump_openapi.py and "
        f"regenerate the artifact. First violations:\n  " + "\n  ".join(violations[:20])
    )


def test_number_schemas_carry_double_format() -> None:
    """
    Every ``type: number`` under ``components.schemas`` has
    ``format: double``.

    A formatless number maps to ``float32`` in ``oapi-codegen``
    (all 12 sites in this document are USD cost fields), so a
    generated client holds a narrower value than the server's
    binary64 float computes: 14.036666666666667 re-emits as
    14.036667 after a float32 round-trip.
    """
    schemas = _load_component_schemas()
    violations: list[str] = []
    _collect_numeric_format_violations(
        schemas, "number", "components.schemas", violations, keys_are_names=True
    )
    assert not violations, (
        f"{len(violations)} number schema node(s) in openapi.json lack "
        f"format: double, so generated clients decode the USD cost "
        f"fields (cost_usd, total_cost_usd, ...) as float32 — narrower "
        f"than the binary64 value the server computes. "
        f"Stamp numeric formats in scripts/dump_openapi.py and "
        f"regenerate the artifact. First violations:\n  " + "\n  ".join(violations[:20])
    )


def test_epoch_microseconds_field_is_decodable_as_documented() -> None:
    """
    The single worst site named in the report stays guarded by name:
    ``SessionListItem.comments_updated_at`` (documented as Unix epoch
    **microseconds**) must be ``int64`` so a generated client can hold
    today's ~1.7e15 values.

    Kept separate from the exhaustive walk so a future reshaping of the
    nullable spelling (``anyOf`` today) still points at this exact
    field when it regresses.
    """
    schemas = _load_component_schemas()
    prop = schemas.get("SessionListItem", {}).get("properties", {}).get("comments_updated_at", {})
    assert prop, (
        "SessionListItem.comments_updated_at is missing from "
        "openapi.json — if the field was renamed, update this test."
    )
    # The nullable spelling is anyOf[{integer}, {null}] today; accept a
    # bare integer node too so only the format matters here.
    candidates = prop.get("anyOf", [prop])
    integer_branches = [
        branch
        for branch in candidates
        if isinstance(branch, dict) and branch.get("type") == "integer"
    ]
    assert integer_branches, (
        "SessionListItem.comments_updated_at no longer has an integer "
        "branch — if its type changed, update this test."
    )
    for branch in integer_branches:
        assert branch.get("format") == "int64", (
            f"SessionListItem.comments_updated_at integer branch is "
            f"{json.dumps(branch, sort_keys=True)} — without "
            f"format: int64 a generated client types it 32-bit and "
            f"fails to decode epoch-microsecond values (~1.7e15 "
            f"today, 817,236x int32 max)."
        )
