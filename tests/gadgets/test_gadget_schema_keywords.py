"""The web's template tests read the server's JSON Schema keywords from one shared file."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.gadgets.service import _SCHEMA_KEYS

_KEYWORDS_FILE = (
    Path(__file__).resolve().parents[2] / "web/src/gadgets/templates/gadgetSchemaKeywords.json"
)


def test_the_shared_keyword_file_matches_what_the_server_accepts() -> None:
    assert sorted(json.loads(_KEYWORDS_FILE.read_text())) == sorted(_SCHEMA_KEYS)
