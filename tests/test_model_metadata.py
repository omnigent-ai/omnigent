"""Validation of runtime-reported model names."""

import pytest

from omnigent.model_metadata import concrete_reported_model


@pytest.mark.parametrize("value", [None, 42, "", " ", "<synthetic>", " <synthetic> "])
def test_non_model_reports_are_ignored(value: object) -> None:
    assert concrete_reported_model(value) is None


@pytest.mark.parametrize("value", ["custom-model", "provider/model[1m]", "opus", "<custom>"])
def test_concrete_model_reports_keep_their_spelling(value: str) -> None:
    assert concrete_reported_model(value) == value
    assert concrete_reported_model(f" {value} ") == value
