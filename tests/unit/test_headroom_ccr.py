"""Tests for Headroom Content-Cache-Retrieval (CCR) functionality."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from omnigent.runtime.headroom_compression import CCRCache, HeadroomCompressor
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.headroom_retrieve import HeadroomRetrieveTool


class TestCCRCache:
    """Test CCR cache storage and retrieval."""

    def test_store_and_retrieve(self):
        """Test basic store and retrieve operations."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CCRCache(cache_dir=tmpdir)

            # Store content
            key = "test_key_123"
            content = "This is the original uncompressed content."
            cache.store(key, content)

            # Retrieve content
            retrieved = cache.retrieve(key)
            assert retrieved == content

    def test_retrieve_nonexistent_key(self):
        """Test retrieving a key that doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CCRCache(cache_dir=tmpdir)

            result = cache.retrieve("nonexistent_key")
            assert result is None

    def test_delete(self):
        """Test deleting cached entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CCRCache(cache_dir=tmpdir)

            key = "delete_me"
            cache.store(key, "content")

            # Verify it exists
            assert cache.retrieve(key) is not None

            # Delete it
            deleted = cache.delete(key)
            assert deleted is True

            # Verify it's gone
            assert cache.retrieve(key) is None

            # Try to delete again
            deleted_again = cache.delete(key)
            assert deleted_again is False

    def test_clear(self):
        """Test clearing all cached entries."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CCRCache(cache_dir=tmpdir)

            # Store multiple entries
            cache.store("key1", "content1")
            cache.store("key2", "content2")
            cache.store("key3", "content3")

            # Clear all
            count = cache.clear()
            assert count == 3

            # Verify all are gone
            assert cache.retrieve("key1") is None
            assert cache.retrieve("key2") is None
            assert cache.retrieve("key3") is None

    def test_default_cache_dir(self):
        """Test that default cache directory is created."""
        cache = CCRCache()
        assert cache.cache_dir.exists()
        assert cache.cache_dir == Path.home() / ".headroom" / "cache"

    def test_store_empty_key(self):
        """Test that storing with empty key is a no-op."""
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CCRCache(cache_dir=tmpdir)

            # Store with empty key should not error
            cache.store("", "content")
            cache.store(None, "content")  # type: ignore[arg-type]

            # Verify nothing was stored
            files = list(Path(tmpdir).glob("*.txt"))
            assert len(files) == 0


class TestHeadroomCCRIntegration:
    """Test CCR integration with HeadroomCompressor."""

    def test_ccr_enabled_caches_content(self):
        """Test that compression with CCR caches original content."""
        with tempfile.TemporaryDirectory() as tmpdir:
            compressor = HeadroomCompressor(
                enable_ccr=True,
                cache_dir=tmpdir,
            )

            # Without headroom-ai, compression is a no-op, so no cache should be created
            # This test verifies the plumbing is in place
            assert compressor.ccr_cache is not None
            assert compressor.ccr_cache.cache_dir == Path(tmpdir)

    def test_ccr_disabled_no_cache(self):
        """Test that CCR disabled means no cache is created."""
        compressor = HeadroomCompressor(enable_ccr=False)
        assert compressor.ccr_cache is None


class TestHeadroomRetrieveTool:
    """Test the headroom_retrieve builtin tool."""

    def test_retrieve_existing_content(self):
        """Test retrieving content that exists in cache."""
        # Use default cache dir so tool and cache use the same location
        conversation_id = "test_conv_retrieve"
        cache = CCRCache(conversation_id=conversation_id)
        key = "test_key_retrieve"
        original_content = "Original uncompressed content here."
        cache.store(key, original_content)

        try:
            # Retrieve via tool
            tool = HeadroomRetrieveTool()
            ctx = ToolContext(task_id="test_task", agent_id="test_agent", conversation_id=conversation_id)
            result_str = tool.invoke(json.dumps({"key": key}), ctx)
            result = json.loads(result_str)

            assert "content" in result
            assert result["content"] == original_content
            assert "error" not in result
        finally:
            # Cleanup
            cache.delete(key)

    def test_retrieve_nonexistent_key(self):
        """Test retrieving a key that doesn't exist."""
        tool = HeadroomRetrieveTool()
        ctx = ToolContext(task_id="test_task", agent_id="test_agent", conversation_id="test_conv_nokey")
        result_str = tool.invoke(json.dumps({"key": "nonexistent_xyz"}), ctx)
        result = json.loads(result_str)

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert "content" not in result

    def test_retrieve_empty_key(self):
        """Test retrieving with no key provided."""
        tool = HeadroomRetrieveTool()
        ctx = ToolContext(task_id="test_task", agent_id="test_agent", conversation_id="test_conv_empty")
        result_str = tool.invoke(json.dumps({"key": ""}), ctx)
        result = json.loads(result_str)

        assert "error" in result
        assert "no retrieval key" in result["error"].lower()

    def test_retrieve_with_unicode_content(self):
        """Test retrieving content with unicode characters."""
        conversation_id = "test_conv_unicode"
        cache = CCRCache(conversation_id=conversation_id)
        key = "unicode_test_key"
        unicode_content = "Hello 世界! 🚀 Émojis and spëcial çhars."
        cache.store(key, unicode_content)

        try:
            tool = HeadroomRetrieveTool()
            ctx = ToolContext(task_id="test_task", agent_id="test_agent", conversation_id=conversation_id)
            result_str = tool.invoke(json.dumps({"key": key}), ctx)
            result = json.loads(result_str)

            assert result["content"] == unicode_content
        finally:
            cache.delete(key)

    def test_retrieve_large_content(self):
        """Test retrieving large content."""
        conversation_id = "test_conv_large"
        cache = CCRCache(conversation_id=conversation_id)
        key = "large_content_key"
        large_content = "x" * 1_000_000  # 1MB of content
        cache.store(key, large_content)

        try:
            tool = HeadroomRetrieveTool()
            ctx = ToolContext(task_id="test_task", agent_id="test_agent", conversation_id=conversation_id)
            result_str = tool.invoke(json.dumps({"key": key}), ctx)
            result = json.loads(result_str)

            assert result["content"] == large_content
            assert len(result["content"]) == 1_000_000
        finally:
            cache.delete(key)
