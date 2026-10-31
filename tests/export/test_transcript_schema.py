"""The published JSON Schema matches the models and accepts a real export.

``schemas/omnigent.transcript.v1.schema.json`` is what another tool reads to
produce or consume ``omnigent.transcript/1``. These tests keep it honest: that
it is what the generator writes today, and that a transcript this repository
produces validates against it.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from omnigent.export import iter_transcript_lines

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "schemas" / "omnigent.transcript.v1.schema.json"


def _generator() -> Any:
    """Import the dump script without installing it."""
    spec = importlib.util.spec_from_file_location(
        "dump_transcript_schema", _REPO_ROOT / "scripts" / "dump_transcript_schema.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def schema() -> dict[str, Any]:
    """The schema as published."""
    return json.loads(_SCHEMA_PATH.read_text())


def _lines(**kwargs: Any) -> list[dict[str, Any]]:
    """A transcript covering every entry kind the writer can produce."""
    session = {
        "id": "conv_abc123",
        "title": "Churn",
        "created_at": 1_700_000_000,
        "harness": "claude-native",
    }
    items: list[dict[str, Any]] = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "churn by region"}],
            "created_at": 1_700_000_000,
        },
        {
            "id": "rsn_1",
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "group by region"}],
            "created_at": 1_700_000_001,
        },
        {
            "id": "fc_1",
            "type": "function_call",
            "name": "sql",
            "call_id": "call_1",
            "arguments": json.dumps({"query": "select 1"}),
            "created_at": 1_700_000_002,
        },
        {
            "id": "fco_1",
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "west,12",
            "created_at": 1_700_000_003,
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "the west"}],
            "created_at": 1_700_000_004,
        },
    ]
    return [json.loads(line) for line in iter_transcript_lines(session, items, **kwargs)]


def test_the_published_schema_is_what_the_generator_writes(schema: dict[str, Any]) -> None:
    """A model change that nobody regenerated for would leave the two disagreeing,
    and other tools read the file rather than the models."""
    assert schema == _generator().generate_schema()


def test_the_schema_itself_is_valid() -> None:
    """A schema other tools load must be a legal 2020-12 document."""
    Draft202012Validator.check_schema(json.loads(_SCHEMA_PATH.read_text()))


def test_a_real_transcript_validates(schema: dict[str, Any]) -> None:
    """Every line this repository writes is a line the published schema accepts."""
    validator = Draft202012Validator(schema)

    for line in _lines():
        assert not list(validator.iter_errors(line)), line


def test_a_redacted_transcript_validates(schema: dict[str, Any]) -> None:
    """Dropping content must not produce lines the schema rejects."""
    validator = Draft202012Validator(schema)

    for line in _lines(redact=True):
        assert not list(validator.iter_errors(line)), line


@pytest.mark.parametrize(
    "line,why",
    [
        ({"turn": 1, "seq": 1, "role": "user", "kind": "nonsense"}, "unknown kind"),
        ({"turn": 1, "seq": 1, "role": "user"}, "no kind"),
        ({"seq": 1, "role": "user", "kind": "message"}, "no turn"),
        (
            {"turn": 1, "seq": 1, "role": "user", "kind": "message", "tool_output": "x"},
            "a field belonging to another kind",
        ),
        ({"schema": "omnigent.transcript/2", "session": "conv_1"}, "an unsupported version"),
    ],
)
def test_the_schema_rejects_what_the_reader_rejects(
    schema: dict[str, Any], line: dict[str, Any], why: str
) -> None:
    """The per-kind rules live in a validator on the model; the schema has to carry
    them too, or it would accept files the reader refuses."""
    assert list(Draft202012Validator(schema).iter_errors(line)), why
