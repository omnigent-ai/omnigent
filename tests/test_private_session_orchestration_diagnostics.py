"""Synthetic evidence projection never exports the private inputs it inspects."""

import hashlib
import json

import pytest

from tests._helpers.codex_rollout import CodexRollout, codex_message
from tests.e2e._private_session_orchestration_diagnostics import (
    capture_private_worker_failure,
    collect_worker_observations,
)


def _case(tmp_path, *, native="WORKER_RESULT", stored="WORKER_RESULT"):
    home = tmp_path / "home"
    worker = "private-worker-id"
    thread = "00000000-0000-4000-8000-000000000001"
    turn = "private-turn-id"
    bridge = home / ".omnigent/codex-native" / hashlib.sha256(worker.encode()).hexdigest()[:32]
    bridge.mkdir(parents=True)
    (bridge / "state.json").write_text(json.dumps({"session_id": worker, "thread_id": thread}))
    rollout = CodexRollout(thread)
    rollout.append("event_msg", {"type": "task_started", "turn_id": turn})
    rollout.append("event_msg", {"type": "user_message", "message": "WORKER_TASK"})
    rollout.append("response_item", codex_message("assistant", native))
    rollout.append("event_msg", {"type": "task_complete", "turn_id": turn})
    path = bridge / "codex-home/sessions/2026/11/05" / f"rollout-{thread}.jsonl"
    rollout.write(path)
    items = {
        "data": [
            {
                **codex_message("assistant", stored),
                "stream_message_id": f"codex:{thread}:{turn}:agentMessage:private-item-id",
            }
        ]
    }
    requests = [
        {
            "model": "gpt-4o",
            "prompt_cache_key": thread,
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "WORKER_TASK"}]}
            ],
            "tools": [{"type": "namespace", "name": "mcp__omnigent"}],
        }
    ]
    arguments = {
        "home": home,
        "worker": worker,
        "read_requests": lambda: requests,
        "read_items": lambda: items,
    }
    return arguments, path, rollout, items, requests


def test_observed_native_and_stored_reply_and_owned_request(tmp_path):
    arguments, _, _, _, _ = _case(tmp_path)
    result = collect_worker_observations(**arguments)
    assert result["native"]["turn_status"] == "completed"
    assert result["native"]["view"]["expected_marker_observed"] is True
    assert result["native"]["view"]["messages"][0]["exact_canned_matches"] == ["worker_result"]
    assert result["stored"]["view"]["expected_marker_observed"] is True
    assert result["comparison"] == {
        "exact_text_equal": True,
        "exact_text_blocks_equal": True,
        "redacted_content_hash_equal": True,
    }
    assert result["mock"]["requests"][0]["owner"] == "worker"
    assert result["mock"]["requests"][0]["allowed_tool_names"] == ["mcp__omnigent"]
    assert result["mock"]["actual_queue_consumption"] == "not_observed"
    assert result["mock"]["ledger_completeness"] == "unknown"


@pytest.mark.parametrize("stored", [None, "Mock LLM response", "MANAGER_SENT"])
def test_native_reply_separated_from_stored_response(tmp_path, stored):
    arguments, _, _, items, _ = _case(tmp_path, stored=stored or "unused")
    if stored is None:
        items["data"] = []
    result = collect_worker_observations(**arguments)
    assert result["native"]["view"]["expected_marker_observed"] is True
    assert result["stored"]["view"]["expected_marker_observed"] is False
    assert result["comparison"]["exact_text_equal"] is False


@pytest.mark.parametrize("cache_key,owner", [(None, "unknown"), ("other-private-id", "other")])
def test_request_ownership_is_not_inferred_from_quoted_marker(tmp_path, cache_key, owner):
    arguments, _, _, _, requests = _case(tmp_path)
    requests[0]["prompt_cache_key"] = cache_key
    requests[0]["model"] = "codex-auto-review"
    requests[0]["tools"] = []
    result = collect_worker_observations(**arguments)
    request = result["mock"]["requests"][0]
    assert request["owner"] == owner
    assert request["model_role"] == "auto_review"
    assert request["worker_marker_observed"] is True
    assert request["required_namespace_present"] is False
    assert result["mock"]["worker_owned_count_observed"] == 0


def test_content_boundaries_and_redaction_are_separate_from_exact_comparison(tmp_path):
    arguments, path, rollout, items, _ = _case(tmp_path, native="ab", stored="ab")
    rollout.records[3]["payload"]["content"] = [
        {"type": "output_text", "text": "a"},
        {"type": "output_text", "text": "b"},
    ]
    rollout.write(path)
    result = collect_worker_observations(**arguments)
    assert result["comparison"] == {
        "exact_text_equal": True,
        "exact_text_blocks_equal": False,
        "redacted_content_hash_equal": False,
    }
    assert (
        result["native"]["view"]["messages"][0]["redacted_content_sha256"]
        != result["stored"]["view"]["messages"][0]["redacted_content_sha256"]
    )
    rollout.records[3]["payload"]["content"] = [
        {"type": "output_text", "text": "password=alpha-secret"}
    ]
    items["data"][0]["content"][0]["text"] = "password=bravo-secret"
    rollout.write(path)
    result = collect_worker_observations(**arguments)
    assert result["comparison"]["exact_text_equal"] is False
    assert (
        result["native"]["view"]["messages"][0]["redacted_text_sha256"]
        == result["stored"]["view"]["messages"][0]["redacted_text_sha256"]
    )


@pytest.mark.parametrize(
    "fault",
    [
        "missing",
        "malformed",
        "partial",
        "oversized",
        "symlink",
        "ambiguous",
        "wrong_identity",
        "unknown_block",
    ],
)
def test_native_evidence_faults_do_not_claim_absence(tmp_path, fault):
    arguments, path, rollout, _, _ = _case(tmp_path)
    if fault == "missing":
        path.unlink()
    elif fault == "malformed":
        path.write_bytes(path.read_bytes() + b"not json\n")
    elif fault == "partial":
        path.write_bytes(path.read_bytes() + b'{"partial":')
    elif fault == "oversized":
        path.write_bytes(b"x" * (4 * 1024 * 1024 + 1))
    elif fault == "symlink":
        other = tmp_path / "outside.jsonl"
        path.rename(other)
        path.symlink_to(other)
    elif fault == "ambiguous":
        path.with_name("duplicate-" + path.name).write_bytes(path.read_bytes())
    elif fault == "wrong_identity":
        rollout.records[0]["payload"]["id"] = "different-private-id"
        rollout.write(path)
    else:
        rollout.records[3]["payload"]["content"] = [
            {"type": "private-unknown-type", "text": "WORKER_RESULT"}
        ]
        rollout.write(path)
    result = collect_worker_observations(**arguments)
    assert result["comparison"]["exact_text_equal"] is None
    view = result["native"]["view"]
    assert view is None or view["complete"] is False
    assert view is None or view["expected_marker_observed"] is not False


def test_unknown_stored_identity_or_pagination_prevents_exact_comparison(tmp_path):
    arguments, _, _, items, _ = _case(tmp_path)
    items["data"][0].pop("stream_message_id")
    result = collect_worker_observations(**arguments)
    assert result["stored"]["view"]["complete"] is False
    assert result["comparison"]["exact_text_equal"] is None
    items["has_more"] = True
    assert (
        collect_worker_observations(**arguments)["stored"]["view"]["expected_marker_observed"]
        is None
    )


@pytest.mark.parametrize("enabled,fail", [(False, True), (True, False)])
def test_inactive_gate_or_success_has_no_diagnostic_io(tmp_path, enabled, fail):
    def forbidden():
        pytest.fail("diagnostic reader called outside failure gate")

    artifact = tmp_path / "diagnostics/runner.log"
    kwargs = {
        "enabled": enabled,
        "home": tmp_path / "absent-home",
        "artifact": artifact,
        "worker": "private-worker-id",
        "read_requests": forbidden,
        "read_items": forbidden,
    }
    if fail:
        with pytest.raises(AssertionError, match="original"):
            with capture_private_worker_failure(**kwargs):
                raise AssertionError("original")
    else:
        with capture_private_worker_failure(**kwargs):
            pass
    assert not artifact.exists()


def test_failure_and_cause_survive_read_and_write_errors(tmp_path):
    def unavailable():
        raise OSError("private credential/path/response must not be exported")

    original = AssertionError("original failure")
    original.__cause__ = ValueError("original cause")
    artifact = tmp_path / "cannot-write"
    artifact.mkdir()
    with pytest.raises(AssertionError) as caught:
        with capture_private_worker_failure(
            enabled=True,
            home=tmp_path / "missing",
            artifact=artifact,
            worker="private-worker-id",
            read_requests=unavailable,
            read_items=unavailable,
        ):
            raise original
    assert caught.value is original
    assert caught.value.__cause__ is original.__cause__


def test_privacy_closed_projection_and_artifact_bound(tmp_path):
    arguments, path, rollout, items, requests = _case(tmp_path)
    sentinel = "PRIVATE_PAYLOAD_SENTINEL"
    requests[0].update(
        {"authorization": sentinel, "metadata": {"path": sentinel}, "model": sentinel}
    )
    requests[0]["input"][0]["content"][0]["text"] += sentinel
    requests[0]["tools"].append({"name": sentinel, "arguments": sentinel})
    items["data"][0]["private"] = sentinel
    rollout.records[0]["payload"]["credentials"] = sentinel
    rollout.write(path)
    artifact = tmp_path / "diagnostics/runner.log"
    with pytest.raises(AssertionError, match="original"):
        with capture_private_worker_failure(enabled=True, artifact=artifact, **arguments):
            raise AssertionError("original")
    raw = artifact.read_text()
    result = json.loads(raw)
    assert len(raw.encode()) <= 8192
    assert sentinel not in raw
    for private in [
        arguments["worker"],
        "private-turn-id",
        "private-item-id",
        "00000000-0000-4000-8000-000000000001",
        str(tmp_path),
    ]:
        assert private not in raw
    assert set(result) == {"schema_version", "kind", "native", "stored", "mock", "comparison"}
    assert result["mock"]["requests"][0]["model_role"] == "other"
    assert result["mock"]["requests"][0]["other_tool_count"] == 1
    assert result["native"]["view"]["messages"][0]["text_utf8_bytes"] == 13


def test_request_and_output_caps_report_partial_instead_of_queue_claims(tmp_path):
    arguments, _, _, _, requests = _case(tmp_path)
    requests.extend(requests[:1] * 256)
    result = collect_worker_observations(**arguments)
    assert result["mock"]["state"] == "partial"
    assert len(result["mock"]["requests"]) == 8
    assert result["mock"]["actual_queue_consumption"] == "not_observed"


@pytest.mark.parametrize(
    "fault", ["state_symlink", "output_symlink", "output_outside", "directory_symlink"]
)
def test_private_paths_cannot_escape_fixture_or_overwrite_existing_files(tmp_path, fault):
    arguments, path, _, _, _ = _case(tmp_path)
    artifact = tmp_path / "diagnostics/runner.log"
    sentinel = tmp_path / "preserve.txt"
    sentinel.write_text("preserve")
    if fault == "state_symlink":
        state = path.parents[5] / "state.json"
        target = tmp_path / "outside-state.json"
        state.rename(target)
        state.symlink_to(target)
        assert collect_worker_observations(**arguments)["native"]["state"] == "unavailable"
    elif fault == "output_symlink":
        artifact.parent.mkdir()
        artifact.symlink_to(sentinel)
    elif fault == "output_outside":
        artifact = tmp_path.parent / "outside-fixture-diagnostic.log"
    else:
        artifact.parent.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AssertionError, match="original"):
        with capture_private_worker_failure(enabled=True, artifact=artifact, **arguments):
            raise AssertionError("original")
    assert sentinel.read_text() == "preserve"
    if fault != "state_symlink":
        assert not artifact.is_file() or artifact.is_symlink()


def test_reader_errors_are_finite_and_never_replace_original_failure(tmp_path):
    def unavailable():
        raise ValueError("private-token-and-url")

    arguments, _, _, _, _ = _case(tmp_path)
    arguments.update(read_requests=unavailable, read_items=unavailable)
    artifact = tmp_path / "diagnostics/runner.log"
    original = AssertionError("original")
    with pytest.raises(AssertionError) as caught:
        with capture_private_worker_failure(enabled=True, artifact=artifact, **arguments):
            raise original
    result = json.loads(artifact.read_text())
    assert caught.value is original
    assert "private-token-and-url" not in artifact.read_text()
    assert result["mock"]["state"] == "unavailable"
    assert result["stored"]["state"] == "unavailable"
    assert result["comparison"]["exact_text_equal"] is None


def test_large_projection_uses_closed_output_limit_record(tmp_path):
    arguments, path, rollout, items, _ = _case(tmp_path)
    content = [{"type": "output_text", "text": "tiny"}] * 64
    rollout.records[3]["payload"]["content"] = content
    for _ in range(8):
        rollout.records.insert(
            4,
            {
                "type": "response_item",
                "payload": {"type": "message", "role": "assistant", "content": content},
            },
        )
    rollout.write(path)
    items["data"][0]["content"] = content
    items["data"] *= 9
    artifact = tmp_path / "diagnostics/runner.log"
    with pytest.raises(AssertionError):
        with capture_private_worker_failure(enabled=True, artifact=artifact, **arguments):
            raise AssertionError("original")
    assert len(artifact.read_bytes()) <= 8192
    assert json.loads(artifact.read_text()) == {
        "schema_version": 1,
        "capture": "output_limit",
        "truncated": True,
    }


def test_unknown_input_and_tool_shapes_do_not_imply_marker_or_tool_absence(tmp_path):
    arguments, _, _, _, requests = _case(tmp_path)
    requests[0]["input"] = [
        {"role": "user", "content": [{"type": "future-input", "payload": "WORKER_TASK"}]}
    ]
    requests[0]["tools"] = [None]
    result = collect_worker_observations(**arguments)
    request = result["mock"]["requests"][0]
    assert request["worker_marker_observed"] is None
    assert request["required_namespace_present"] is None


def test_closed_schema_contains_only_enums_digests_and_bounded_scalars(tmp_path):
    import re

    arguments, _, _, _, _ = _case(tmp_path)
    result = collect_worker_observations(**arguments)
    assert set(result["native"]) == {"state", "view", "turn_status"}
    assert set(result["stored"]) == {"state", "view"}
    assert set(result["mock"]) == {
        "state",
        "ledger_completeness",
        "actual_queue_consumption",
        "request_count_observed",
        "worker_owned_count_observed",
        "requests",
    }
    assert set(result["comparison"]) == {
        "exact_text_equal",
        "exact_text_blocks_equal",
        "redacted_content_hash_equal",
    }
    for source in ("native", "stored"):
        assert set(result[source]["view"]) == {
            "assistant_count_observed",
            "complete",
            "expected_marker_observed",
            "messages",
        }
        for message in result[source]["view"]["messages"]:
            assert set(message) == {
                "content_complete",
                "block_types",
                "other_block_count",
                "text_utf8_bytes",
                "redacted_text_sha256",
                "redacted_content_sha256",
                "expected_marker_observed",
                "exact_canned_matches",
            }
            assert re.fullmatch("[0-9a-f]{64}", message["redacted_text_sha256"])
            assert re.fullmatch("[0-9a-f]{64}", message["redacted_content_sha256"])
    assert set(result["mock"]["requests"][0]) == {
        "ordinal",
        "owner",
        "model_role",
        "worker_marker_observed",
        "required_namespace_present",
        "allowed_tool_names",
        "other_tool_count",
    }


@pytest.mark.parametrize(
    "tool",
    [
        {},
        {"name": None},
        {"name": 7},
        {"name": ""},
        {"function": None},
        {"function": []},
        {"function": {"name": None}},
    ],
)
def test_malformed_dictionary_tool_does_not_prove_namespace_absence(tmp_path, tool):
    arguments, _, _, _, requests = _case(tmp_path)
    requests[0]["tools"] = [tool]
    result = collect_worker_observations(**arguments)
    assert result["mock"]["requests"][0]["required_namespace_present"] is None


@pytest.mark.parametrize(
    "item",
    [
        None,
        [],
        {},
        {"type": "message"},
        {"type": "message", "role": None},
        {"type": "message", "role": "future-private-role"},
    ],
)
def test_unsupported_stored_item_makes_comparison_unknown(tmp_path, item):
    arguments, _, _, items, _ = _case(tmp_path)
    items["data"].append(item)
    result = collect_worker_observations(**arguments)
    assert result["stored"]["state"] == "partial"
    assert result["stored"]["view"]["complete"] is False
    assert result["comparison"]["exact_text_equal"] is None


@pytest.mark.parametrize("role", [None, 7, "future-private-role"])
def test_unsupported_native_message_role_makes_comparison_unknown(tmp_path, role):
    arguments, path, rollout, _, _ = _case(tmp_path)
    payload = rollout.records[3]["payload"]
    if role is None:
        payload.pop("role")
    else:
        payload["role"] = role
    rollout.write(path)
    result = collect_worker_observations(**arguments)
    assert result["native"]["state"] == "partial"
    assert result["native"]["view"]["complete"] is False
    assert result["native"]["view"]["expected_marker_observed"] is None
    assert result["comparison"]["exact_text_equal"] is None


@pytest.mark.parametrize("role", ["user", "developer", "system"])
def test_known_native_nonassistant_roles_preserve_complete_observation(tmp_path, role):
    arguments, path, rollout, _, _ = _case(tmp_path)
    rollout.records.insert(
        3, {"type": "response_item", "payload": codex_message(role, "private context")}
    )
    rollout.write(path)
    result = collect_worker_observations(**arguments)
    assert result["native"]["view"]["complete"] is True
    assert result["comparison"]["exact_text_equal"] is True


def test_known_stored_user_and_resource_event_preserve_complete_observation(tmp_path):
    arguments, _, _, items, _ = _case(tmp_path)
    items["data"].extend(
        [
            codex_message("user", "private context"),
            {
                "type": "resource_event",
                "event_type": "session.resource.created",
                "resource_id": "private-resource-id",
                "resource_type": "terminal",
                "resource": None,
            },
        ]
    )
    result = collect_worker_observations(**arguments)
    assert result["stored"]["view"]["complete"] is True
    assert result["comparison"]["exact_text_equal"] is True


@pytest.mark.parametrize("fault", ["tool", "stored", "native"])
def test_unknown_shape_diagnostics_preserve_the_original_failure_and_cause(tmp_path, fault):
    arguments, path, rollout, items, requests = _case(tmp_path)
    if fault == "tool":
        requests[0]["tools"] = [{"function": None}]
    elif fault == "stored":
        items["data"].append(None)
    else:
        rollout.records[3]["payload"].pop("role")
        rollout.write(path)
    artifact = tmp_path / "diagnostics/runner.log"
    original = AssertionError("original")
    cause = ValueError("original cause")
    original.__cause__ = cause
    with pytest.raises(AssertionError) as caught:
        with capture_private_worker_failure(enabled=True, artifact=artifact, **arguments):
            raise original
    assert caught.value is original
    assert caught.value.__cause__ is cause
    assert len(artifact.read_bytes()) <= 8192
