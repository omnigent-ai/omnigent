"""Unit tests for Headroom compression integration."""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runtime.headroom_compression import (
    CompressionMetrics,
    CompressionResult,
    HeadroomCompressor,
)


class TestCompressionResult:
    """Test CompressionResult dataclass."""

    def test_tokens_saved(self):
        """Test tokens_saved property calculation."""
        result = CompressionResult(
            compressed="compressed",
            original_tokens=1000,
            compressed_tokens=300,
            compression_ratio=3.33,
            method="json",
        )
        assert result.tokens_saved == 700

    def test_percent_saved(self):
        """Test percent_saved property calculation."""
        result = CompressionResult(
            compressed="compressed",
            original_tokens=1000,
            compressed_tokens=250,
            compression_ratio=4.0,
            method="json",
        )
        assert result.percent_saved == 75.0

    def test_no_compression(self):
        """Test result when no compression occurred."""
        result = CompressionResult(
            compressed="original",
            original_tokens=100,
            compressed_tokens=100,
            compression_ratio=1.0,
            method="none",
        )
        assert result.tokens_saved == 0
        assert result.percent_saved == 0.0


class TestCompressionMetrics:
    """Test CompressionMetrics tracking."""

    def test_record_compression(self):
        """Test recording compression events."""
        metrics = CompressionMetrics()

        metrics.record_compression("json", 1000, 300)
        metrics.record_compression("code", 500, 400)

        assert metrics.total_compressions == 2
        assert metrics.total_original_tokens == 1500
        assert metrics.total_compressed_tokens == 700
        assert metrics.tokens_saved == 800

    def test_compressions_by_type(self):
        """Test per-method compression tracking."""
        metrics = CompressionMetrics()

        metrics.record_compression("json", 1000, 300)
        metrics.record_compression("json", 2000, 500)
        metrics.record_compression("code", 500, 400)

        assert metrics.compressions_by_type["json"] == 2
        assert metrics.compressions_by_type["code"] == 1
        assert metrics.savings_by_type["json"] == 2200  # (1000-300) + (2000-500) = 700 + 1500
        assert metrics.savings_by_type["code"] == 100  # 500-400

    def test_overall_compression_ratio(self):
        """Test overall compression ratio calculation."""
        metrics = CompressionMetrics()

        metrics.record_compression("json", 1000, 250)  # 4:1
        metrics.record_compression("code", 1000, 800)  # 1.25:1

        # Overall: 2000 original / 1050 compressed = 1.905
        assert abs(metrics.overall_compression_ratio - 1.905) < 0.01

    def test_estimated_cost_savings(self):
        """Test cost savings estimation."""
        metrics = CompressionMetrics()

        # Save 100,000 tokens
        metrics.record_compression("json", 150000, 50000)

        # At $5/1M tokens: 100,000 * (5/1,000,000) = $0.50
        savings = metrics.estimated_cost_savings_usd(cost_per_million_tokens=5.0)
        assert abs(savings - 0.50) < 0.01

    def test_estimated_cost_savings_expensive_model(self):
        """Test cost savings with expensive model pricing."""
        metrics = CompressionMetrics()

        # Save 1,000,000 tokens
        metrics.record_compression("json", 1500000, 500000)

        # At $15/1M tokens (premium tier): 1,000,000 * (15/1,000,000) = $15
        savings = metrics.estimated_cost_savings_usd(cost_per_million_tokens=15.0)
        assert abs(savings - 15.0) < 0.01


class TestHeadroomCompressor:
    """Test HeadroomCompressor functionality."""

    @pytest.fixture
    def compressor(self) -> HeadroomCompressor:
        """Create a compressor instance for testing."""
        return HeadroomCompressor(
            json_threshold=100,
            code_threshold=100,
            prose_threshold=100,
        )

    def test_json_detection(self, compressor: HeadroomCompressor):
        """Test JSON content detection."""
        json_content = '{"key": "value", "numbers": [1, 2, 3]}'
        assert compressor._is_json(json_content) is True

        non_json = "This is not JSON"
        assert compressor._is_json(non_json) is False

    def test_code_detection_by_tool_name(self, compressor: HeadroomCompressor):
        """Test code detection based on tool name."""
        content = "some content"

        # Code tools should trigger code compression
        assert compressor._is_code(content, "read_file") is True
        assert compressor._is_code(content, "Read") is True
        assert compressor._is_code(content, "grep") is True

        # Non-code tools should not
        assert compressor._is_code(content, "api_call") is False

    def test_code_detection_by_patterns(self, compressor: HeadroomCompressor):
        """Test code detection based on content patterns."""
        code_content = """
def function():
    return "hello"

class MyClass:
    pass
"""
        assert compressor._is_code(code_content, "unknown_tool") is True

        prose_content = "This is just regular prose text without code patterns."
        assert compressor._is_code(prose_content, "unknown_tool") is False

    def test_compress_json_tool_result(self, compressor: HeadroomCompressor):
        """Test compressing JSON tool results."""
        large_json = json.dumps({"data": list(range(1000))})

        result = compressor.compress_tool_result(
            content=large_json,
            tool_name="api_call",
        )

        # JSON should be compressed (simulated ~70% reduction in placeholder)
        assert result.method == "json"
        assert result.compression_ratio > 2.0  # >50% savings
        assert result.tokens_saved > 0

    def test_compress_code_tool_result(self, compressor: HeadroomCompressor):
        """Test compressing code file results."""
        code_content = (
            """
def example_function():
    '''Docstring.'''
    x = 1
    y = 2
    return x + y

class ExampleClass:
    def method(self):
        pass
"""
            * 20
        )  # Make it large enough

        result = compressor.compress_tool_result(
            content=code_content,
            tool_name="read_file",
        )

        # Code should be compressed (simulated ~18% reduction in placeholder)
        assert result.method == "code"
        assert result.compression_ratio > 1.1  # >10% savings
        assert result.tokens_saved > 0

    def test_compress_prose_tool_result(self, compressor: HeadroomCompressor):
        """Test compressing prose content."""
        prose = (
            """
This is a long prose text that would benefit from compression.
It contains multiple sentences with various information.
The compressor should detect this as prose and apply appropriate compression.
"""
            * 50
        )  # Make it large enough

        result = compressor.compress_tool_result(
            content=prose,
            tool_name="search",
        )

        # Prose should be compressed (simulated ~25% reduction in placeholder)
        assert result.method == "prose"
        assert result.compression_ratio > 1.2  # >20% savings
        assert result.tokens_saved > 0

    def test_skip_compression_below_threshold(self, compressor: HeadroomCompressor):
        """Test that small content is not compressed."""
        small_json = '{"key": "val"}'

        result = compressor.compress_tool_result(
            content=small_json,
            tool_name="api_call",
            estimated_tokens=10,  # Below json_threshold
        )

        # Should skip compression
        assert result.method == "none"
        assert result.compression_ratio == 1.0
        assert result.tokens_saved == 0

    def test_compress_conversation_history(self, compressor: HeadroomCompressor):
        """Test compressing conversation history."""
        messages = [
            {"role": "user", "content": "Question 1"},
            {"role": "assistant", "content": "Answer 1 " * 100},  # Large
            {"role": "tool", "content": json.dumps({"data": list(range(100))})},  # Large JSON
            {"role": "user", "content": "Question 2"},
            {"role": "assistant", "content": "Answer 2"},  # Recent, protected
        ]

        compressed = compressor.compress_conversation_history(
            messages,
            protect_recent=2,  # Protect last 2 messages
        )

        # First 3 messages should be processed for compression
        # Last 2 should be unchanged (protected)
        assert len(compressed) == len(messages)
        assert compressed[-1] == messages[-1]  # Recent protected
        assert compressed[-2] == messages[-2]  # Recent protected

    def test_metrics_tracking(self):
        """Test that metrics are tracked during compression."""
        metrics = CompressionMetrics()
        compressor = HeadroomCompressor(
            json_threshold=10,
            metrics=metrics,
        )

        large_json = json.dumps({"data": list(range(100))})

        compressor.compress_tool_result(
            content=large_json,
            tool_name="api_call",
        )

        # Metrics should be updated
        assert metrics.total_compressions > 0
        assert metrics.total_original_tokens > 0
        assert metrics.tokens_saved > 0
        assert "json" in metrics.compressions_by_type

    def test_disabled_when_headroom_unavailable(self):
        """Test graceful degradation when Headroom is not available."""
        compressor = HeadroomCompressor()

        # If HEADROOM_AVAILABLE is False, compression should be skipped
        # This is tested via the _no_compression_result fallback
        content = '{"large": "json"}' * 100

        result = compressor.compress_tool_result(
            content=content,
            tool_name="api_call",
        )

        # Result should still be valid even if not compressed
        assert result.compressed == content or result.compression_ratio > 1.0
        assert result.original_tokens >= 0
        assert result.compressed_tokens >= 0


class TestIntegrationScenarios:
    """Test real-world integration scenarios."""

    def test_large_api_response_compression(self):
        """Simulate compressing a large API response."""
        compressor = HeadroomCompressor()

        # Simulate a large API response with nested data
        api_response = json.dumps(
            {
                "status": "success",
                "data": [
                    {"id": i, "name": f"Item {i}", "details": {"field": f"value {i}"}}
                    for i in range(1000)
                ],
                "meta": {"total": 1000, "page": 1},
            }
        )

        result = compressor.compress_tool_result(
            content=api_response,
            tool_name="api_call",
        )

        # Should achieve significant compression for JSON
        assert result.method == "json"
        assert result.compression_ratio > 2.0
        print(
            f"API Response: {result.original_tokens} → {result.compressed_tokens} tokens "
            f"({result.percent_saved:.1f}% saved)"
        )

    def test_code_file_compression(self):
        """Simulate compressing a large code file."""
        compressor = HeadroomCompressor()

        # Simulate a Python file
        code_file = (
            """
import os
import sys
from typing import List, Dict, Any

class DataProcessor:
    '''Process data from various sources.'''

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.results = []

    def process(self, items: List[Any]) -> List[Dict[str, Any]]:
        '''Process items and return results.'''
        processed = []
        for item in items:
            result = self._process_single(item)
            processed.append(result)
        return processed

    def _process_single(self, item: Any) -> Dict[str, Any]:
        '''Process a single item.'''
        return {
            'id': item.get('id'),
            'value': item.get('value', 0) * 2,
            'status': 'processed',
        }
"""
            * 10
        )  # Simulate larger file

        result = compressor.compress_tool_result(
            content=code_file,
            tool_name="read_file",
        )

        # Should compress code with AST-aware compression
        assert result.method == "code"
        assert result.compression_ratio > 1.1
        print(
            f"Code File: {result.original_tokens} → {result.compressed_tokens} tokens "
            f"({result.percent_saved:.1f}% saved)"
        )

    def test_monthly_cost_savings_projection(self):
        """Project monthly cost savings from compression."""
        metrics = CompressionMetrics()
        compressor = HeadroomCompressor(metrics=metrics)

        # Simulate 100 sessions with various content types
        for _ in range(100):
            # Large JSON API responses
            api_response = json.dumps({"data": list(range(500))})
            compressor.compress_tool_result(api_response, "api_call")

            # Code files
            code = "def function():\n    pass\n" * 100
            compressor.compress_tool_result(code, "read_file")

            # Documentation/logs
            prose = "Log entry with details. " * 200
            compressor.compress_tool_result(prose, "search")

        # Calculate savings
        total_savings = metrics.tokens_saved
        cost_savings = metrics.estimated_cost_savings_usd(cost_per_million_tokens=5.0)

        print(f"\nProjected Monthly Savings (100 sessions):")
        print(f"  Tokens saved: {total_savings:,}")
        print(f"  Cost saved: ${cost_savings:.2f}")
        print(f"  Compression ratio: {metrics.overall_compression_ratio:.2f}x")
        print(f"  Percent saved: {metrics.percent_saved:.1f}%")
        print(f"\nBreakdown by type:")
        for method, count in metrics.compressions_by_type.items():
            saved = metrics.savings_by_type.get(method, 0)
            print(f"  {method}: {count} compressions, {saved:,} tokens saved")

        # Assertions
        assert total_savings > 0
        assert cost_savings > 0
        assert metrics.overall_compression_ratio > 1.0
