"""Unit tests for the ``Uuid16`` column type and its id-normalisation helpers.

Pins the contract the binary-id migration relies on: which legacy forms bind,
which malformed forms fail loud, the forgiving Python-side normaliser, and the
driver-variance guards on result decoding.
"""

from __future__ import annotations

import pytest

from omnigent.db.db_models import (
    InvalidUuidError,
    Uuid16,
    normalize_uuid,
    uuid_to_bytes,
)

_HEX = "a1b2c3d4e5f67890abcdef1234567890"


class TestUuidToBytes:
    def test_bare_hex(self) -> None:
        assert uuid_to_bytes(_HEX) == bytes.fromhex(_HEX)

    def test_dashed_canonical(self) -> None:
        dashed = f"{_HEX[:8]}-{_HEX[8:12]}-{_HEX[12:16]}-{_HEX[16:20]}-{_HEX[20:]}"
        assert uuid_to_bytes(dashed) == bytes.fromhex(_HEX)

    @pytest.mark.parametrize(
        "prefix",
        [
            "conv_",
            "ag_",
            "host_",
            "pol_",
            "file_",
            "cmt_",
            "msg_",
            "fc_",
            "fco_",
            "err_",
            "rs_",
            "cmp_",
            "nt_",
            "rse_",
            "sc_",
            "tc_",
            "rd_",
            "agy_conv_",
        ],
    )
    def test_known_legacy_prefixes_strip(self, prefix: str) -> None:
        assert uuid_to_bytes(prefix + _HEX) == bytes.fromhex(_HEX)

    @pytest.mark.parametrize(
        "bad",
        [
            # unknown prefixes must fail loud, not silently store the hex tail
            "resp_" + _HEX,
            "runner_" + _HEX,
            "runner_token_" + _HEX,
            "junk_" + _HEX,
            # malformed shapes
            "conv_short",
            _HEX[:30],
            "z" * 32,
            "",
        ],
    )
    def test_rejects_unknown_or_malformed(self, bad: str) -> None:
        with pytest.raises(InvalidUuidError):
            uuid_to_bytes(bad)


class TestNormalizeUuid:
    def test_legacy_and_bare_collapse(self) -> None:
        assert normalize_uuid("conv_" + _HEX) == _HEX
        assert normalize_uuid(_HEX) == _HEX

    def test_malformed_passes_through_for_mismatch_semantics(self) -> None:
        # A store scope-compare against bare hex must simply not match.
        assert normalize_uuid("nonexistent-id") == "nonexistent-id"

    def test_none_passes_through(self) -> None:
        assert normalize_uuid(None) is None


class TestUuid16ResultDecoding:
    """Driver-variance guards, mirroring ``CompressedText``'s decode()."""

    def test_bytes(self) -> None:
        assert Uuid16().process_result_value(bytes.fromhex(_HEX), None) == _HEX

    def test_memoryview(self) -> None:
        value = memoryview(bytes.fromhex(_HEX))
        assert Uuid16().process_result_value(value, None) == _HEX

    def test_str_passthrough(self) -> None:
        assert Uuid16().process_result_value(_HEX, None) == _HEX

    def test_none(self) -> None:
        assert Uuid16().process_result_value(None, None) is None
