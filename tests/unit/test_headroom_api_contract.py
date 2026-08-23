"""Test the headroom-ai API contract.

Verifies that the UniversalCompressor API matches our assumptions.
If this test fails, the headroom-ai package API has changed and our
integration needs updating.
"""

from __future__ import annotations

import pytest

# Skip if headroom-ai is not installed
headroom = pytest.importorskip("headroom")


def test_universal_compressor_api_contract():
    """Verify UniversalCompressor API matches our integration assumptions."""
    from headroom.compression import UniversalCompressor, UniversalCompressorConfig

    # Test that config can be created with our parameters
    config = UniversalCompressorConfig(
        compression_ratio_target=0.5,
        enable_ccr=True,
    )
    assert config is not None

    # Test that compressor can be initialized
    compressor = UniversalCompressor(config=config)
    assert compressor is not None

    # Test compression returns expected attributes
    test_content = '{"data": [1, 2, 3, 4, 5]}'
    result = compressor.compress(test_content)

    # Verify all attributes we use exist
    assert hasattr(result, "compressed"), "Result must have 'compressed' attribute"
    assert hasattr(result, "tokens_before"), "Result must have 'tokens_before' attribute"
    assert hasattr(result, "tokens_after"), "Result must have 'tokens_after' attribute"
    assert hasattr(result, "compression_ratio"), "Result must have 'compression_ratio' attribute"

    # Verify CCR key (may be None if CCR disabled or content too small)
    ccr_key = getattr(result, "ccr_key", None)
    # ccr_key can be None, but the attribute should exist if CCR is enabled
    assert ccr_key is None or isinstance(ccr_key, str), "ccr_key must be None or string"

    # Verify types
    assert isinstance(result.compressed, str)
    assert isinstance(result.tokens_before, int)
    assert isinstance(result.tokens_after, int)
    assert isinstance(result.compression_ratio, (int, float))


def test_headroom_module_structure():
    """Verify headroom module structure matches our imports."""
    from headroom import compression

    # Verify exports we use
    assert hasattr(compression, "UniversalCompressor")
    assert hasattr(compression, "UniversalCompressorConfig")
    assert hasattr(compression, "CompressionResult")
