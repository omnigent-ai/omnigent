"""Input correlation stays isolated and cannot expose message content."""

import asyncio
import copy
import logging

import pytest

from omnigent.native import input_receipt
from omnigent.native.input_diagnostics import (
    current_input_attributes,
    input_attributes,
    input_delivery_scope,
    log_input_event,
)
from omnigent.native.input_receipt import (
    build_native_input_receipt,
    current_native_input_receipt,
    native_input_receipt_scope,
    validate_native_input_receipt,
)


def test_input_attributes_reject_content_and_malformed_identifiers() -> None:
    assert input_attributes(
        {
            "input_stable_id": "a" * 32,
            "pending_id": "pending_" + "b" * 32,
            "delivery_attempt_id": "private prompt",
            "input_enqueued_at_ms": True,
            "content": "private prompt",
            "created_by": "person@example.com",
            "response_id": "untrusted response",
        }
    ) == {"input_stable_id": "a" * 32, "pending_id": "pending_" + "b" * 32}
    assert input_attributes({"input_stable_id": {"text": "private prompt"}}) == {}


@pytest.mark.asyncio
async def test_concurrent_inputs_and_workers_keep_their_own_identity() -> None:
    async def observe(letter: str) -> dict[str, object]:
        with input_delivery_scope({"input_stable_id": letter * 32}, response_id="resp_" + letter):
            await asyncio.sleep(0)
            observed = await asyncio.to_thread(current_input_attributes)
            with input_delivery_scope(None):
                assert current_input_attributes() == {}
            assert current_input_attributes() == observed
            return observed

    assert await asyncio.gather(observe("a"), observe("b")) == [
        {"input_stable_id": "a" * 32, "response_id": "resp_a"},
        {"input_stable_id": "b" * 32, "response_id": "resp_b"},
    ]
    assert current_input_attributes() == {}


@pytest.mark.parametrize("debug_fails", [False, True])
def test_logging_failure_cannot_interrupt_delivery(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, debug_fails: bool
) -> None:
    logger = logging.getLogger(__name__)
    caplog.set_level(logging.DEBUG, logger=__name__)

    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("private sink details")

    monkeypatch.setattr(logger, "info", fail)
    if debug_fails:
        monkeypatch.setattr(logger, "debug", fail)
    with input_delivery_scope({"input_stable_id": "a" * 32}):
        log_input_event(logger, "native_input_execution_finished", outcome="executor_returned")
    assert current_input_attributes() == {}
    assert "private sink details" not in caplog.text
    if not debug_fails:
        assert "Native input diagnostic could not be emitted (RuntimeError)" in caplog.text


def test_diagnostic_emit_filters_extra_content_and_malformed_fields(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger=__name__)
    log_input_event(
        logging.getLogger(__name__),
        "native_input_settled",
        attributes={"input_stable_id": "a" * 32, "content": "private prompt"},
        response_id="resp_saved",
        outcome="rpc_accepted",
        message="private prompt",
        item_id="private prompt",
        native_turn_id="x" * 257,
        lifecycle_response_id="resp_native",
        cause_class="accepted_missing_native_record",
        reason_code="completed_without_native_record",
        exception_type="x" * 65,
        pending_age_ms=True,
        rpc_error_code=-32600,
    )
    [record] = caplog.records
    assert record.attributes == {
        "input_stable_id": "a" * 32,
        "response_id": "resp_saved",
        "lifecycle_response_id": "resp_native",
        "outcome": "rpc_accepted",
        "cause_class": "accepted_missing_native_record",
        "reason_code": "completed_without_native_record",
        "rpc_error_code": -32600,
    }


@pytest.mark.parametrize(
    "fault",
    [
        "version",
        "boolean_version",
        "extra",
        "attempt",
        "pending",
        "block",
        "nan",
        "surrogate",
        "size",
    ],
)
def test_receipt_rejects_partial_or_unknown_contract(
    fault: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = build_native_input_receipt(
        [{"type": "input_text", "text": "original\r\n"}],
        {"pending_id": "pending_" + "a" * 32, "delivery_attempt_id": "b" * 32},
    )
    assert value is not None
    if fault == "version":
        value["version"] = 2
    elif fault == "boolean_version":
        value["version"] = True
    elif fault == "extra":
        value["unrecognized"] = True
    elif fault == "attempt":
        value["identity"].pop("delivery_attempt_id")
    elif fault == "pending":
        value["identity"].pop("pending_id")
    elif fault == "size":
        monkeypatch.setattr(input_receipt, "MAX_SESSION_EVENT_REQUEST_BYTES", 32)
    elif fault == "block":
        value["content"] = ["flattened"]
    else:
        value["content"][0]["text"] = float("nan") if fault == "nan" else "\ud800"
    assert validate_native_input_receipt(value) is None


@pytest.mark.asyncio
async def test_receipts_copy_original_blocks_and_isolate_concurrent_workers() -> None:
    async def observe(letter: str) -> dict:
        original = [
            {"type": "input_text", "text": letter + "\r\n"},
            {"type": "input_file", "file_id": "file_original"},
        ]
        receipt = build_native_input_receipt(
            original,
            {
                "pending_id": "pending_" + letter * 32,
                "delivery_attempt_id": letter * 32,
            },
        )
        assert receipt is not None
        expected = copy.deepcopy(receipt)
        with native_input_receipt_scope(receipt):
            original[0]["text"] = "changed"
            receipt["content"].clear()
            observed = await asyncio.to_thread(current_native_input_receipt)
            assert observed == expected
            observed["content"].clear()
            assert current_native_input_receipt() == expected
            assert current_input_attributes() == {}
            with pytest.raises(RuntimeError, match="nested failure"):
                with native_input_receipt_scope(None):
                    assert current_native_input_receipt() is None
                    raise RuntimeError("nested failure")
            assert current_native_input_receipt() == expected
        return expected

    first, second = await asyncio.gather(observe("a"), observe("b"))
    assert first != second
    assert current_native_input_receipt() is None
