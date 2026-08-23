"""Headroom-based context compression for token optimization.

Integrates Headroom's content-aware compression to reduce token usage before
LLM calls. Supports JSON, code, prose, and image compression with optional
content-cache-retrieval (CCR) for reversible compression.

Example usage:

    compressor = HeadroomCompressor()
    result = compressor.compress_tool_result(
        content=large_json_response,
        tool_name="api_call",
    )
    if result.compression_ratio > 1.2:  # >20% savings
        use_compressed = result.compressed
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)

# Try to import headroom; gracefully degrade if unavailable
try:
    from headroom.compression import (  # type: ignore[import-not-found]
        UniversalCompressor,
        UniversalCompressorConfig,
    )

    HEADROOM_AVAILABLE = True
except ImportError:
    HEADROOM_AVAILABLE = False
    _logger.info("Headroom not available; simulation mode will be used")


@dataclass
class CompressionResult:
    """Result of compressing content with Headroom.

    :param compressed: The compressed content string.
    :param original_tokens: Estimated tokens in original content.
    :param compressed_tokens: Estimated tokens after compression.
    :param compression_ratio: Ratio of original to compressed tokens (>1.0 = savings).
    :param method: Compression method used ('json', 'code', 'prose', 'image', 'none').
    :param retrieval_key: Optional CCR key for retrieving original content.
    """

    compressed: str
    original_tokens: int
    compressed_tokens: int
    compression_ratio: float
    method: str
    retrieval_key: str | None = None

    @property
    def tokens_saved(self) -> int:
        """Number of tokens saved by compression."""
        return max(0, self.original_tokens - self.compressed_tokens)

    @property
    def percent_saved(self) -> float:
        """Percentage of tokens saved (0-100)."""
        if self.original_tokens == 0:
            return 0.0
        return (self.tokens_saved / self.original_tokens) * 100


@dataclass
class CompressionMetrics:
    """Aggregate compression statistics for monitoring.

    Tracks total compressions, token savings, and per-method breakdowns.
    Used for cost analysis and optimization.
    """

    total_compressions: int = 0
    total_original_tokens: int = 0
    total_compressed_tokens: int = 0

    compressions_by_type: dict[str, int] = field(default_factory=dict)
    savings_by_type: dict[str, int] = field(default_factory=dict)

    def record_compression(
        self,
        method: str,
        original: int,
        compressed: int,
    ) -> None:
        """Record a compression event for metrics tracking.

        :param method: Compression method used (json/code/prose/image/none).
        :param original: Original token count.
        :param compressed: Compressed token count.
        """
        self.total_compressions += 1
        self.total_original_tokens += original
        self.total_compressed_tokens += compressed

        self.compressions_by_type[method] = self.compressions_by_type.get(method, 0) + 1
        self.savings_by_type[method] = self.savings_by_type.get(method, 0) + max(
            0, original - compressed
        )

    @property
    def tokens_saved(self) -> int:
        """Total tokens saved across all compressions."""
        return max(0, self.total_original_tokens - self.total_compressed_tokens)

    @property
    def overall_compression_ratio(self) -> float:
        """Overall compression ratio (>1.0 = savings)."""
        if self.total_compressed_tokens == 0:
            return 1.0
        return self.total_original_tokens / self.total_compressed_tokens

    @property
    def percent_saved(self) -> float:
        """Overall percentage of tokens saved (0-100)."""
        if self.total_original_tokens == 0:
            return 0.0
        return (self.tokens_saved / self.total_original_tokens) * 100

    def estimated_cost_savings_usd(
        self,
        cost_per_million_tokens: float = 5.0,
    ) -> float:
        """Estimate cost savings based on token price.

        :param cost_per_million_tokens: Cost per 1M input tokens in USD.
            Defaults to $5 (mid-tier model average).
        :returns: Estimated savings in USD.
        """
        cost_per_token = cost_per_million_tokens / 1_000_000
        return self.tokens_saved * cost_per_token


class CCRCache:
    """Content-Cache-Retrieval cache for reversible compression.

    Stores original uncompressed content keyed by retrieval IDs, allowing
    agents to recover full details from compressed messages via the
    headroom_retrieve tool.

    Thread-safe for concurrent access. Entries are stored as files in the
    cache directory with content-addressable keys.
    """

    def __init__(self, cache_dir: str | None = None, conversation_id: str | None = None):
        """Initialize CCR cache.

        :param cache_dir: Directory for cache storage. Defaults to
            ~/.headroom/cache if None.
        :param conversation_id: Conversation ID for session isolation.
            If provided, files are stored in a subdirectory per conversation.
        """
        if cache_dir:
            base_dir = Path(cache_dir)
        else:
            base_dir = Path.home() / ".headroom" / "cache"

        # Add conversation subdirectory for session isolation
        if conversation_id:
            self.cache_dir = base_dir / conversation_id
        else:
            self.cache_dir = base_dir

        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _logger.debug("CCR cache initialized at %s", self.cache_dir)

    def _validate_key(self, key: str) -> bool:
        """Validate CCR key to prevent path traversal attacks.

        Only allows alphanumeric, dash, and underscore characters.
        """
        if not key:
            return False
        # Only allow safe characters (alphanumeric + dash + underscore)
        return all(c.isalnum() or c in ('-', '_') for c in key) and len(key) <= 256

    def store(self, key: str, content: str) -> None:
        """Store original content for later retrieval.

        :param key: Retrieval key (from CompressionResult.retrieval_key).
        :param content: Original uncompressed content.
        """
        if not self._validate_key(key):
            _logger.warning("Invalid CCR key rejected: %s", key)
            return

        # Use content hash as filename for content-addressable storage
        cache_file = self.cache_dir / f"{key}.txt"

        # Verify path is within cache_dir to prevent traversal
        try:
            resolved = cache_file.resolve()
            if not resolved.is_relative_to(self.cache_dir.resolve()):
                _logger.error("Path traversal attempt blocked: key=%s", key)
                return
        except (ValueError, OSError) as e:
            _logger.error("Path validation failed for key %s: %s", key, e)
            return

        try:
            cache_file.write_text(content, encoding="utf-8")
            # Set restrictive permissions (owner read/write only)
            cache_file.chmod(0o600)
            _logger.debug("Stored CCR entry: %s (%d bytes)", key, len(content))
        except OSError as e:
            _logger.warning("Failed to cache content for key %s: %s", key, e)

    def retrieve(self, key: str) -> str | None:
        """Retrieve original content by key.

        :param key: Retrieval key from compressed message.
        :returns: Original content if found, None if key doesn't exist.
        """
        if not self._validate_key(key):
            _logger.warning("Invalid CCR key rejected: %s", key)
            return None

        cache_file = self.cache_dir / f"{key}.txt"

        # Verify path is within cache_dir to prevent traversal
        try:
            resolved = cache_file.resolve()
            if not resolved.is_relative_to(self.cache_dir.resolve()):
                _logger.error("Path traversal attempt blocked: key=%s", key)
                return None
        except (ValueError, OSError) as e:
            _logger.error("Path validation failed for key %s: %s", key, e)
            return None

        try:
            if not cache_file.exists():
                _logger.warning("CCR key not found: %s", key)
                return None

            content = cache_file.read_text(encoding="utf-8")
            _logger.debug("Retrieved CCR entry: %s (%d bytes)", key, len(content))
            return content
        except OSError as e:
            _logger.warning("Failed to retrieve content for key %s: %s", key, e)
            return None

    def delete(self, key: str) -> bool:
        """Delete a cached entry.

        :param key: Retrieval key to delete.
        :returns: True if deleted, False if key didn't exist.
        """
        if not self._validate_key(key):
            _logger.warning("Invalid CCR key rejected: %s", key)
            return False

        cache_file = self.cache_dir / f"{key}.txt"

        # Verify path is within cache_dir to prevent traversal
        try:
            resolved = cache_file.resolve()
            if not resolved.is_relative_to(self.cache_dir.resolve()):
                _logger.error("Path traversal attempt blocked: key=%s", key)
                return False
        except (ValueError, OSError) as e:
            _logger.error("Path validation failed for key %s: %s", key, e)
            return False

        try:
            if cache_file.exists():
                cache_file.unlink()
                _logger.debug("Deleted CCR entry: %s", key)
                return True
            return False
        except OSError as e:
            _logger.warning("Failed to delete key %s: %s", key, e)
            return False

    def clear(self) -> int:
        """Clear all cached entries.

        :returns: Number of entries deleted.
        """
        count = 0
        try:
            for cache_file in self.cache_dir.glob("*.txt"):
                cache_file.unlink()
                count += 1
            _logger.info("Cleared %d CCR cache entries", count)
        except OSError as e:
            _logger.warning("Failed to clear cache: %s", e)

        return count


class HeadroomCompressor:
    """Headroom integration for Omnigent content compression.

    Provides content-aware compression for tool results, code files, JSON
    responses, and conversation history. Uses appropriate compression method
    based on content type detection.

    Example:
        >>> compressor = HeadroomCompressor()
        >>> result = compressor.compress_tool_result(
        ...     content='{"data": [1, 2, 3, ...]}',
        ...     tool_name='api_call',
        ... )
        >>> if result.compression_ratio > 1.2:
        ...     print(f"Saved {result.percent_saved:.1f}%")
    """

    def __init__(
        self,
        *,
        json_threshold: int = 500,
        code_threshold: int = 1000,
        prose_threshold: int = 2000,
        enable_ccr: bool = True,
        cache_dir: str | None = None,
        conversation_id: str | None = None,
        metrics: CompressionMetrics | None = None,
    ):
        """Initialize Headroom compressor with configuration.

        :param json_threshold: Minimum tokens before compressing JSON (default: 500).
        :param code_threshold: Minimum tokens before compressing code (default: 1000).
        :param prose_threshold: Minimum tokens before compressing prose (default: 2000).
        :param enable_ccr: Enable content-cache-retrieval for reversible compression.
        :param cache_dir: Directory for CCR cache (None = default ~/.headroom/cache).
        :param conversation_id: Conversation ID for session-isolated cache (required for CCR).
        :param metrics: Optional metrics tracker; creates new if None.
        """
        self.json_threshold = json_threshold
        self.code_threshold = code_threshold
        self.prose_threshold = prose_threshold
        self.enable_ccr = enable_ccr
        self.cache_dir = cache_dir
        self.conversation_id = conversation_id
        self.metrics = metrics or CompressionMetrics()
        self.enabled = HEADROOM_AVAILABLE

        # Initialize CCR cache if enabled (requires conversation_id for session isolation)
        self.ccr_cache: CCRCache | None = None
        if self.enable_ccr:
            self.ccr_cache = CCRCache(cache_dir=cache_dir, conversation_id=conversation_id)

        # Initialize UniversalCompressor if available
        self._compressor = None
        if HEADROOM_AVAILABLE:
            try:
                config = UniversalCompressorConfig(
                    compression_ratio_target=0.5,  # Target 50% compression
                    enable_ccr=self.enable_ccr,
                )
                self._compressor = UniversalCompressor(config=config)
                _logger.debug("HeadroomCompressor initialized with UniversalCompressor")
            except Exception as e:
                _logger.warning("Failed to initialize UniversalCompressor: %s", e)
                self.enabled = False

        if not self.enabled:
            _logger.debug(
                "HeadroomCompressor initialized but headroom-ai not available; "
                "compression will be skipped"
            )

    def compress_tool_result(
        self,
        content: str,
        tool_name: str,
        *,
        estimated_tokens: int | None = None,
    ) -> CompressionResult:
        """Compress a tool result using content-aware method.

        Detects content type and applies optimal compression:
        - JSON responses → SmartCrusher (60-95% reduction)
        - Code files → CodeCompressor (15-20% reduction)
        - Logs/prose → Kompress-v2 (variable reduction)

        :param content: Tool result content to compress.
        :param tool_name: Name of tool that produced the content.
        :param estimated_tokens: Pre-computed token count (optional).
        :returns: Compression result with compressed content and metrics.
        """
        if not self.enabled or not content:
            return self._no_compression_result(content, estimated_tokens or 0)

        # Estimate tokens if not provided
        if estimated_tokens is None:
            estimated_tokens = self._estimate_tokens(content)

        # Detect content type and apply appropriate compression
        if self._is_json(content) and estimated_tokens >= self.json_threshold:
            result = self._compress_with_headroom(content, estimated_tokens, "json")
        elif self._is_code(content, tool_name) and estimated_tokens >= self.code_threshold:
            result = self._compress_with_headroom(content, estimated_tokens, "code")
        elif estimated_tokens >= self.prose_threshold:
            result = self._compress_with_headroom(content, estimated_tokens, "prose")
        else:
            result = self._no_compression_result(content, estimated_tokens)

        # Cache original content if CCR is enabled and we got a retrieval key
        if self.ccr_cache and result.retrieval_key:
            self.ccr_cache.store(result.retrieval_key, content)

        # Track metrics
        self.metrics.record_compression(
            result.method,
            result.original_tokens,
            result.compressed_tokens,
        )

        if result.compression_ratio > 1.1:  # >10% savings worth logging
            _logger.info(
                "Headroom compressed %s result: %d → %d tokens (%.1f%% saved, method=%s)",
                tool_name,
                result.original_tokens,
                result.compressed_tokens,
                result.percent_saved,
                result.method,
            )

        return result

    def compress_conversation_history(
        self,
        messages: list[dict[str, Any]],
        *,
        protect_recent: int = 5,
    ) -> list[dict[str, Any]]:
        """Compress older conversation messages outside recent window.

        Applies selective compression to tool_result and assistant messages
        that fall outside the protected recent window.

        :param messages: List of conversation messages to compress.
        :param protect_recent: Number of recent messages to protect from compression.
        :returns: Messages list with older content compressed.
        """
        if not self.enabled or len(messages) <= protect_recent:
            return messages

        compressed = []
        compress_boundary = len(messages) - protect_recent

        for idx, msg in enumerate(messages):
            if idx < compress_boundary and self._should_compress_message(msg):
                compressed.append(self._compress_message(msg))
            else:
                compressed.append(msg)

        return compressed

    def _is_json(self, content: str) -> bool:
        """Check if content is valid JSON."""
        try:
            json.loads(content)
            return True
        except (ValueError, TypeError):
            return False

    def _is_code(self, content: str, tool_name: str) -> bool:
        """Check if content is code based on tool and content patterns."""
        # Tools that typically return code
        code_tools = {
            "read_file",
            "Read",
            "grep",
            "Grep",
            "git_diff",
            "bash",
            "Bash",
            "list_directory",
            "find_files",
        }

        if tool_name in code_tools:
            return True

        # Check for code patterns
        lines = content.split("\n")
        if not lines:
            return False

        # Heuristics: indentation + code keywords
        has_indentation = any(line.startswith(("    ", "\t")) for line in lines)
        code_keywords = [
            "def ",
            "class ",
            "function ",
            "const ",
            "let ",
            "var ",
            "import ",
            "from ",
            "async ",
            "await ",
            "return ",
        ]
        has_code_keywords = any(kw in content for kw in code_keywords)

        return has_indentation and has_code_keywords

    def _estimate_tokens(self, content: str) -> int:
        """Rough token estimate (4 chars ≈ 1 token)."""
        return len(content) // 4

    def _compress_with_headroom(
        self,
        content: str,
        estimated_tokens: int,
        content_type: str,
    ) -> CompressionResult:
        """Compress content using Headroom UniversalCompressor.

        The UniversalCompressor auto-detects content type and applies appropriate
        compression. Falls back to no-op if headroom unavailable.
        """
        if not self._compressor:
            # Headroom not available - return no-op
            return CompressionResult(
                compressed=content,
                original_tokens=estimated_tokens,
                compressed_tokens=estimated_tokens,
                compression_ratio=1.0,
                method="none",
            )

        try:
            result = self._compressor.compress(content)

            return CompressionResult(
                compressed=result.compressed,
                original_tokens=result.tokens_before,
                compressed_tokens=result.tokens_after,
                compression_ratio=result.compression_ratio,
                method=content_type,
                retrieval_key=getattr(result, "ccr_key", None),
            )
        except Exception as e:
            _logger.warning("Headroom compression failed: %s, returning original", e)
            return CompressionResult(
                compressed=content,
                original_tokens=estimated_tokens,
                compressed_tokens=estimated_tokens,
                compression_ratio=1.0,
                method="none",
            )

    def _no_compression_result(
        self,
        content: str,
        estimated_tokens: int,
    ) -> CompressionResult:
        """Return uncompressed result when compression is skipped."""
        return CompressionResult(
            compressed=content,
            original_tokens=estimated_tokens,
            compressed_tokens=estimated_tokens,
            compression_ratio=1.0,
            method="none",
        )

    def _should_compress_message(self, msg: dict[str, Any]) -> bool:
        """Check if message should be compressed."""
        # Compress tool_result and assistant messages with large content
        role = msg.get("role")
        if role == "tool":
            content = msg.get("content", "")
            return len(content) > self.json_threshold
        if role == "assistant":
            content = msg.get("content", "")
            return len(content) > self.prose_threshold
        return False

    def _compress_message(self, msg: dict[str, Any]) -> dict[str, Any]:
        """Compress a single message's content."""
        content = msg.get("content", "")
        if not content:
            return msg

        estimated_tokens = self._estimate_tokens(content)

        # Detect type and compress
        if self._is_json(content):
            result = self._compress_with_headroom(content, estimated_tokens, "json")
        else:
            result = self._compress_with_headroom(content, estimated_tokens, "prose")

        # Return message with compressed content
        compressed_msg = msg.copy()
        compressed_msg["content"] = result.compressed

        return compressed_msg
