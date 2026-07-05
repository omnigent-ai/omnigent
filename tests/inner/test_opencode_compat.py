"""Tests for the opencode-ai discriminated-union cache shim.

The shim rebinds ``opencode_ai._models._build_discriminated_union_meta`` so
its cache no longer requires setting attributes on ``typing.Union`` objects
(impossible on Python 3.14). These tests verify the replacement computes the
same metadata as the SDK's original, caches without touching the union
object, and steps aside when the SDK already carries the upstream fix.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

import pytest
from opencode_ai import _models
from opencode_ai._utils import PropertyInfo

from omnigent.inner.opencode_compat import patch_discriminator_cache


class _CardVariant(_models.BaseModel):
    type: Literal["card"]
    number: str


class _BankVariant(_models.BaseModel):
    type: Literal["bank"]
    account: str


# typing.Union on purpose (not `X | Y`): the SDK's generated types use it,
# and on Python <= 3.13 the `|` form isn't weakref-able, which would skip
# the cache path these tests cover.
_UNION = Union[_CardVariant, _BankVariant]  # noqa: UP007
_META = (PropertyInfo(discriminator="type"),)


@pytest.fixture
def restore_models():
    """Snapshot and restore the ``_models`` globals the shim mutates."""
    original_fn = _models._build_discriminated_union_meta
    had_cache = hasattr(_models, "DISCRIMINATOR_CACHE")
    original_cache = getattr(_models, "DISCRIMINATOR_CACHE", None)
    yield
    _models._build_discriminated_union_meta = original_fn
    if had_cache:
        _models.DISCRIMINATOR_CACHE = original_cache
    elif hasattr(_models, "DISCRIMINATOR_CACHE"):
        del _models.DISCRIMINATOR_CACHE


def test_patched_meta_matches_original(restore_models) -> None:
    expected = _models._build_discriminated_union_meta(union=_UNION, meta_annotations=_META)
    assert expected is not None

    patch_discriminator_cache()
    details = _models._build_discriminated_union_meta(union=_UNION, meta_annotations=_META)

    assert details is not None
    assert details.mapping == expected.mapping
    assert details.field_name == expected.field_name
    assert details.field_alias_from == expected.field_alias_from


def test_patched_meta_does_not_touch_the_union_object(restore_models) -> None:
    union = Union[_BankVariant, _CardVariant]  # noqa: UP007 — see _UNION

    patch_discriminator_cache()
    details = _models._build_discriminated_union_meta(union=union, meta_annotations=_META)

    assert details is not None
    assert not hasattr(union, "__discriminator__")
    # Second call hits the external cache and returns the same object.
    assert _models._build_discriminated_union_meta(union=union, meta_annotations=_META) is details


def test_patched_construct_type_picks_discriminated_variant(restore_models) -> None:
    patch_discriminator_cache()

    annotated = Annotated[_UNION, PropertyInfo(discriminator="type")]
    value = _models.construct_type(value={"type": "bank", "account": "42"}, type_=annotated)

    assert isinstance(value, _BankVariant)
    assert value.account == "42"


def test_patch_skips_when_upstream_fix_present(restore_models) -> None:
    original_fn = _models._build_discriminated_union_meta
    _models.DISCRIMINATOR_CACHE = {}

    patch_discriminator_cache()

    assert _models._build_discriminated_union_meta is original_fn


def test_patch_is_idempotent(restore_models) -> None:
    patch_discriminator_cache()
    patched_fn = _models._build_discriminated_union_meta
    patched_cache = _models.DISCRIMINATOR_CACHE

    patch_discriminator_cache()

    assert _models._build_discriminated_union_meta is patched_fn
    assert _models.DISCRIMINATOR_CACHE is patched_cache


def test_patch_skips_when_internals_missing(restore_models, monkeypatch) -> None:
    original_fn = _models._build_discriminated_union_meta
    monkeypatch.delattr(_models, "strip_annotated_type")

    patch_discriminator_cache()

    assert _models._build_discriminated_union_meta is original_fn
    assert not hasattr(_models, "DISCRIMINATOR_CACHE")
