"""The Python trigger renderer matches the web form's, byte for byte.

``tests/tools/fixtures/trigger_prompts.json`` is written by the web form's renderer
(``web/src/components/scheduled/triggerTemplates.parity.test.ts``); see that test for
how to regenerate it after a rendering change.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.tools.builtins import trigger_prompts
from omnigent.tools.builtins.trigger_prompts import (
    TRIGGER_SOURCES,
    TriggerConfig,
    decode_trigger_prompt,
    empty_trigger_fields,
    encode_trigger_prompt,
    has_trigger_header,
    instructions_length,
    is_task_session_trigger_prompt,
    parse_github_reviewer,
    render_trigger_config_prompt,
    strip_trigger_header,
    trigger_condition_fields,
    trigger_fields_complete,
    trigger_prompt_source,
    trigger_scope,
    with_trigger_event,
)

_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[1] / "fixtures" / "trigger_prompts.json").read_text(
        encoding="utf-8"
    )
)
_RENDERS = _FIXTURE["renders"]


def _by_name(cases: list[dict[str, Any]]) -> dict[str, Any]:
    return {"argvalues": cases, "ids": [case["name"] for case in cases]}


def _context(case: dict[str, Any]) -> dict[str, Any]:
    context = case["context"]
    return {
        "session_link": context["sessionLink"],
        "polling_interval_minutes": context.get("pollingIntervalMinutes"),
        "task_session_title": context.get("taskSessionTitle"),
    }


@pytest.mark.parametrize("case", **_by_name(_RENDERS))
def test_encode_matches_the_web_form(case: dict[str, Any]) -> None:
    assert encode_trigger_prompt(case["config"], **_context(case)) == case["prompt"]


@pytest.mark.parametrize("case", **_by_name(_RENDERS))
def test_decode_matches_the_web_form(case: dict[str, Any]) -> None:
    decoded = decode_trigger_prompt(case["prompt"])
    assert decoded == case["decoded"]
    # Key order carries into the next save's header.
    assert decoded is not None
    assert list(decoded) == list(case["decoded"])
    assert list(decoded["worker"]) == list(case["decoded"]["worker"])


@pytest.mark.parametrize("case", **_by_name(_RENDERS))
def test_saved_prompt_reads_back(case: dict[str, Any]) -> None:
    prompt = case["prompt"]
    config: TriggerConfig = case["config"]
    assert has_trigger_header(prompt)
    assert trigger_prompt_source(prompt) == config["source"]
    assert is_task_session_trigger_prompt(prompt) is (config.get("taskSessionMode") == "continue")
    # The model receives the rendered prompt without the header line.
    assert strip_trigger_header(prompt) == render_trigger_config_prompt(config, **_context(case))
    assert prompt.index("\n") + 1 == len(prompt) - len(strip_trigger_header(prompt))


@pytest.mark.parametrize("case", **_by_name(_RENDERS + _FIXTURE["completeness"]))
def test_completeness_matches_the_web_form(case: dict[str, Any]) -> None:
    assert trigger_fields_complete(case["config"]) is case["complete"]


@pytest.mark.parametrize("case", _FIXTURE["decodes"], ids=lambda case: case["name"])
def test_decoding_edited_prompts_matches_the_web_form(case: dict[str, Any]) -> None:
    assert decode_trigger_prompt(case["prompt"]) == case["decoded"]


@pytest.mark.parametrize("case", _FIXTURE["reviewers"], ids=lambda case: repr(case["input"]))
def test_reviewer_parsing_matches_the_web_form(case: dict[str, Any]) -> None:
    parsed = parse_github_reviewer(case["input"])
    expected = case["parsed"]
    if expected is None:
        assert parsed is None
        return
    assert parsed is not None
    as_dict = {key: value for key, value in dataclasses.asdict(parsed).items() if value != ""}
    assert as_dict == expected


@pytest.mark.parametrize("case", _FIXTURE["jqlOrderBy"], ids=lambda case: repr(case["input"]))
def test_jql_order_by_split_matches_the_web_form(case: dict[str, Any]) -> None:
    where, order_by = trigger_prompts._split_jql_order_by(case["input"])
    assert {"where": where, "orderBy": order_by} == case["split"]


@pytest.mark.parametrize("case", _FIXTURE["emptyFields"], ids=lambda case: case["source"])
def test_empty_fields_match_the_web_form(case: dict[str, Any]) -> None:
    assert empty_trigger_fields(case["source"]) == case["fields"]


@pytest.mark.parametrize("case", **_by_name(_FIXTURE["eventSwitches"]))
def test_event_switches_match_the_web_form(case: dict[str, Any]) -> None:
    assert with_trigger_event(case["source"], case["fields"], case["event"]) == case["switched"]


def test_fixture_covers_every_source_and_event() -> None:
    covered = {(case["config"]["source"], case["config"]["event"]) for case in _RENDERS}
    for source in TRIGGER_SOURCES:
        for event in trigger_prompts.TRIGGER_TEMPLATES[source].events:
            assert (source, event.id) in covered


def test_fingerprint_is_the_web_form_hash() -> None:
    case = _RENDERS[0]
    header_line, body = case["prompt"].split("\n", 1)
    header = json.loads(header_line.removeprefix("<!-- omnigent-trigger v1 ").removesuffix(" -->"))
    canonical = {key: value for key, value in header.items() if key != "fingerprint"}
    expected = trigger_prompts._cyrb53(f"{trigger_prompts._js_json(canonical)}\n{body}")
    assert header["fingerprint"] == expected
    assert trigger_prompts._trigger_fingerprint(case["config"], body) == expected


def test_decode_rejects_edits_outside_the_form() -> None:
    prompt = _RENDERS[0]["prompt"]
    assert decode_trigger_prompt(prompt) is not None
    assert decode_trigger_prompt(prompt + " ") is None
    assert (
        decode_trigger_prompt(prompt.replace("omnigent-trigger v1 ", "omnigent-trigger v3 "))
        is None
    )
    assert decode_trigger_prompt("x" * 5000) is None
    huge_version = prompt.replace("omnigent-trigger v1 ", f"omnigent-trigger v{'9' * 5000} ")
    assert decode_trigger_prompt(huge_version) is None


def test_worker_permission_mode_defaults_when_absent() -> None:
    config = json.loads(json.dumps(_RENDERS[0]["config"]))
    del config["worker"]["permissionMode"]
    context = _context(_RENDERS[0])
    # An absent mode saves like an empty one: the header leaves it out.
    assert encode_trigger_prompt(config, **context) == _RENDERS[0]["prompt"]


def test_js_trim_uses_javascript_whitespace() -> None:
    # JavaScript keeps U+001C-U+001F and U+0085, which Python's strip() removes, and
    # strips U+FEFF, which Python keeps.
    assert trigger_prompts._js_trim("﻿　 a \u001c") == "a \u001c"
    assert trigger_prompts._js_trim("\u0085a ") == "\u0085a"
    assert trigger_prompts._js_trim_end("a \t\n") == "a"
    assert trigger_prompts._js_trim_start(" a ") == "a "


def test_js_json_escapes_as_json_stringify() -> None:
    assert trigger_prompts._js_json("\ud800 \udfff") == '"\\ud800 \\udfff"'
    assert trigger_prompts._js_json('\u0001\b\f\n\r\t"\\') == '"\\u0001\\b\\f\\n\\r\\t\\"\\\\"'
    assert trigger_prompts._js_json("\u007f /é\U0001f600") == '"\u007f /é\U0001f600"'
    assert trigger_prompts._js_json({"b": [True, None], "a": "x"}) == '{"b":[true,null],"a":"x"}'
    # Two surrogate code points form one character in JavaScript, written as itself.
    surrogate_pair = "".join(chr(unit) for unit in (0xD83D, 0xDE00))
    assert trigger_prompts._js_json(surrogate_pair) == '"\U0001f600"'


def test_instructions_length_counts_utf16_code_units() -> None:
    assert instructions_length("abc") == 3
    assert instructions_length("\U0001f600") == 2
    assert instructions_length("\ud800") == 1


def test_scope_and_fields_follow_the_event() -> None:
    assert trigger_scope("slack", "dm").prefix == "@"
    assert trigger_scope("slack", "group_dm").is_group
    assert trigger_scope("slack", "unknown").prefix == "#"
    assert [field.id for field in trigger_condition_fields("github", "review")] == [
        "label",
        "author",
        "qualifiers",
    ]
    assert "assignee" not in {field.id for field in trigger_condition_fields("jira", "assigned")}
