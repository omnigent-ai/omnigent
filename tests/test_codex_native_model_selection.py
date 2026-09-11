"""Exact model selections survive storage without guessing from model names."""

from __future__ import annotations

import pytest

from omnigent.harnesses.codex_native.model_selection import ExactCodexModel
from omnigent.server.schemas import (
    BackgroundSessionTitleRequest,
    SessionCreateRequest,
    SessionForkRequest,
    UpdateSessionRequest,
)


@pytest.mark.parametrize(
    "model_id", ["system.ai.gpt-test", "vendor/custom", "bare", "databricks-old"]
)
def test_exact_model_selection_round_trip(model_id: str) -> None:
    request = UpdateSessionRequest(model_override="picker-choice", model_override_id=model_id)
    restored = UpdateSessionRequest.model_validate_json(request.model_dump_json())
    assert restored.model_override == "picker-choice"
    assert restored.model_override_id == model_id


@pytest.mark.parametrize("model", [None, "default", "off", "reset", ""])
def test_exact_id_requires_an_explicit_selection(model: str | None) -> None:
    with pytest.raises(ValueError, match="requires a non-default"):
        UpdateSessionRequest(model_override=model, model_override_id="literal-id")


@pytest.mark.parametrize(
    "request_type,required",
    [
        (SessionCreateRequest, {"agent_id": "agent"}),
        (UpdateSessionRequest, {}),
        (SessionForkRequest, {}),
        (BackgroundSessionTitleRequest, {"prompt": "title"}),
    ],
)
def test_every_selection_request_validates_the_exact_id(
    request_type: type, required: dict[str, str]
) -> None:
    with pytest.raises(ValueError):
        request_type(**required, model_override="picker", model_override_id='unsafe"model')


@pytest.mark.parametrize("model_id", ["", "--flag", 'model"\nauth.command="bad"'])
def test_exact_model_is_still_validated(model_id: str) -> None:
    with pytest.raises(ValueError):
        ExactCodexModel(model_id)
