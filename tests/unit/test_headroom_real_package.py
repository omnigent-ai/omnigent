"""Tests with real headroom-ai package (when available).

These tests verify the integration works with the actual headroom-ai
library, not just the simulation/graceful-degradation path.
"""

from __future__ import annotations

import pytest

# Skip all tests in this module if headroom-ai is not installed
pytest.importorskip("headroom")

from omnigent.runtime.headroom_compression import (
    CompressionMetrics,
    HeadroomCompressor,
)


class TestRealHeadroomIntegration:
    """Test actual headroom-ai package integration."""

    def test_real_compression_with_json(self):
        """Test JSON compression with real headroom-ai package."""
        compressor = HeadroomCompressor(
            json_threshold=100,
            enable_ccr=False,  # Disable CCR for this test
            conversation_id="test_real",
        )

        # Large JSON that should trigger compression
        json_content = '{"data": ' + str([{"id": i, "value": f"item_{i}"} for i in range(100)]) + "}"

        result = compressor.compress_tool_result(
            content=json_content,
            tool_name="api_call",
        )

        # Verify compression actually happened
        assert result.compression_ratio > 1.0, "Real package should compress JSON"
        assert result.compressed != json_content, "Compressed should differ from original"
        assert len(result.compressed) < len(json_content), "Compressed should be smaller"
        assert result.original_tokens > 0
        assert result.compressed_tokens > 0
        assert result.compressed_tokens < result.original_tokens

    def test_real_compression_with_ccr(self):
        """Test CCR with real headroom-ai package."""
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            compressor = HeadroomCompressor(
                json_threshold=100,
                enable_ccr=True,
                cache_dir=tmpdir,
                conversation_id="test_ccr_real",
            )

            json_content = '{"items": ' + str(list(range(200))) + "}"

            result = compressor.compress_tool_result(
                content=json_content,
                tool_name="list_items",
            )

            # Verify CCR key was generated
            assert result.retrieval_key is not None, "CCR should generate a retrieval key"
            assert isinstance(result.retrieval_key, str)
            assert len(result.retrieval_key) > 0

            # Verify we can retrieve the original
            assert compressor.ccr_cache is not None
            retrieved = compressor.ccr_cache.retrieve(result.retrieval_key)
            assert retrieved == json_content, "Retrieved content should match original"

    def test_universal_compressor_initialization(self):
        """Test that UniversalCompressor initializes correctly."""
        from headroom.compression import UniversalCompressor, UniversalCompressorConfig

        config = UniversalCompressorConfig(
            compression_ratio_target=0.5,
            enable_ccr=True,
        )
        compressor = UniversalCompressor(config=config)
        assert compressor is not None

    def test_compression_metrics_tracking(self):
        """Test metrics are tracked with real compression."""
        metrics = CompressionMetrics()
        compressor = HeadroomCompressor(
            json_threshold=50,
            enable_ccr=False,
            conversation_id="test_metrics",
            metrics=metrics,
        )

        json_data = '{"numbers": ' + str(list(range(100))) + "}"
        compressor.compress_tool_result(content=json_data, tool_name="test")

        # Verify metrics were updated
        assert metrics.total_compressions > 0
        assert metrics.tokens_saved > 0
        assert metrics.total_original_tokens > 0
        assert metrics.total_compressed_tokens > 0
