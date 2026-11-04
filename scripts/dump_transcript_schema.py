"""Generate the published JSON Schema for ``omnigent.transcript/1``.

``docs/session-export.md`` describes the format in prose. Another tool that
wants to read or write one needs it machine-readable, and this repository needs
something CI can check a real export against, so the prose, the models and the
files other people produce cannot drift apart.

The schema is derived from the models in :mod:`omnigent.export.transcript`:

1. The header and entry shapes come from Pydantic.
2. The per-kind field rules, which live in a validator rather than the field
   list, are materialized as a ``oneOf`` branch per kind, so a schema validator
   rejects the same stray fields :meth:`TranscriptEntry._fields_match_kind`
   rejects (a ``tool_output`` on a ``message``, say).

Run it after changing either, and commit the result::

    python scripts/dump_transcript_schema.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from omnigent.export.transcript import (  # noqa: E402 — must follow the path bootstrap
    _COMMON_FIELDS,
    _KIND_FIELDS,
    TRANSCRIPT_SCHEMA,
    TranscriptEntry,
    TranscriptHeader,
)

#: Where the published schema lives, relative to the repository root.
SCHEMA_PATH = Path("schemas/omnigent.transcript.v1.schema.json")
#: The ``$id`` other tools reference.
SCHEMA_ID = "https://omnigent.ai/schemas/omnigent.transcript.v1.schema.json"
#: Fields the writer always sets, so a reader may rely on them.
_REQUIRED_ENTRY_FIELDS = ("turn", "seq", "role", "kind")


def _entry_branch(kind: str, properties: dict[str, Any]) -> dict[str, Any]:
    """Build the entry variant for one kind, naming only the fields it may carry."""
    allowed = _COMMON_FIELDS | _KIND_FIELDS[kind]
    return {
        "title": f"{kind} entry",
        "type": "object",
        "properties": {
            name: schema for name, schema in properties.items() if name in allowed | {"kind"}
        }
        | {"kind": {"const": kind}},
        "required": list(_REQUIRED_ENTRY_FIELDS),
        "additionalProperties": False,
    }


def generate_schema() -> dict[str, Any]:
    """Return the schema for a whole transcript file, header and entries."""
    header = TranscriptHeader.model_json_schema(by_alias=True)
    entry = TranscriptEntry.model_json_schema()
    properties = entry.pop("properties")
    definitions = {**header.pop("$defs", {}), **entry.pop("$defs", {})}
    header["properties"]["schema"]["const"] = TRANSCRIPT_SCHEMA
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": TRANSCRIPT_SCHEMA,
        "description": (
            "A session transcript: one JSON object per line, the header first, "
            "then one entry per item. See docs/session-export.md."
        ),
        "$defs": {
            **definitions,
            "header": header | {"additionalProperties": False},
            "entry": {
                "title": "entry",
                "oneOf": [_entry_branch(kind, properties) for kind in sorted(_KIND_FIELDS)],
            },
        },
        # The file is NDJSON rather than one document, so the schema describes a
        # line: a reader validates the first against the header and the rest
        # against an entry.
        "oneOf": [{"$ref": "#/$defs/header"}, {"$ref": "#/$defs/entry"}],
    }


def main() -> None:
    """Write the schema beside the repository root."""
    path = _REPO_ROOT / SCHEMA_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(generate_schema(), indent=2, sort_keys=True) + "\n")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
