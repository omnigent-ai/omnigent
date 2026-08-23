# Headroom Integration - Remaining Work

This document tracks what needs to be completed before Headroom compression can be activated.

## Current State

**Status:** Integration scaffolding complete, compression **NOT ACTIVE**

All infrastructure is in place but content compression is disabled until reversibility can be guaranteed.

## Blocking Issues (Must Complete Before Activation)

### 1. Tool Registration (HIGH PRIORITY)
**File:** `omnigent/tools/builtins/headroom_retrieve.py` (needs to be created)
**Status:** ❌ Not started

The `headroom_retrieve` tool must be registered as a builtin tool so agents can call it.

**Tasks:**
- [ ] Move `omnigent/runtime/headroom_tool.py` → `omnigent/tools/builtins/headroom_retrieve.py`
- [ ] Create tool class that extends `Tool` base class
- [ ] Add to `omnigent/tools/builtins/__init__.py` imports
- [ ] Add to `BUILTIN_NAMES` and `__all__` in `__init__.py`
- [ ] Add to `get_builtin_tool()` function
- [ ] Update tests to verify tool is callable by agents

**References:**
- See `omnigent/tools/builtins/list_comments.py` for example
- Tool manager: `omnigent/tools/manager.py`

### 2. Out-of-Band Key Storage
**Files:** `omnigent/runtime/compaction.py`, schema changes
**Status:** ❌ Not started

Currently `_headroom_key` would be injected into messages (causing wire leakage). Need proper storage.

**Options:**
1. **Database table** (recommended): Store (conversation_id, message_index, retrieval_key) tuples
2. **Conversation metadata**: Extend conversation schema with retrieval_keys map
3. **Separate cache index**: Map message IDs to retrieval keys

**Tasks:**
- [ ] Design key storage schema
- [ ] Implement storage in compaction layer
- [ ] Implement retrieval in headroom_retrieve tool
- [ ] Add migration if using database
- [ ] Update tests

### 3. Session Isolation for CCR Cache
**File:** `omnigent/runtime/headroom_compression.py` - `CCRCache`
**Status:** ❌ Not started

Currently all sessions share `~/.headroom/cache` - any session can read another's cached content.

**Tasks:**
- [ ] Add conversation_id parameter to CCR cache operations
- [ ] Store files as `~/.headroom/cache/{conversation_id}/{key}.txt`
- [ ] Update `headroom_retrieve` to verify session ownership
- [ ] Add session cleanup on conversation deletion
- [ ] Update tests for isolation

### 4. TTL and Cleanup Lifecycle
**File:** `omnigent/runtime/headroom_compression.py` - `CCRCache`
**Status:** ❌ Not started

Currently cached files persist indefinitely with no cleanup.

**Tasks:**
- [ ] Add timestamp to cached files (metadata or mtime)
- [ ] Implement TTL (default: 7 days?)
- [ ] Add cleanup task (background job or on-access check)
- [ ] Add size limits per conversation
- [ ] Add total cache size limits
- [ ] Update tests

### 5. Uncomment Content Replacement Code
**File:** `omnigent/runtime/compaction.py`
**Status:** ❌ Blocked on items 1-4

Once above items complete, uncomment the compression application code.

**Tasks:**
- [ ] Uncomment `msg["output"] = result.compressed` (4 locations)
- [ ] Implement proper key storage instead of `_headroom_key`
- [ ] Verify compression only happens when retrieval_key is present
- [ ] Update integration tests to verify actual compression

### 6. Uncomment Dependency
**File:** `pyproject.toml`
**Status:** ❌ Blocked on items 1-5

**Tasks:**
- [ ] Uncomment `headroom-ai>=0.36,<1` in `all` extra
- [ ] Regenerate lockfile
- [ ] Verify CI passes with package installed
- [ ] Test end-to-end compression flow

## Non-Blocking Enhancements

### Secure File Permissions
**File:** `omnigent/runtime/headroom_compression.py` - `CCRCache`
**Priority:** Medium

Add restrictive permissions to cache files (0600) and directory (0700).

### Encryption at Rest
**Priority:** Low (future enhancement)

Consider encrypting cached content since it may contain sensitive data.

### Metrics and Monitoring
**Priority:** Low

Expose cache size, hit/miss rates, compression ratios via telemetry.

## Testing Checklist

Before marking as complete:
- [ ] Unit tests for tool registration
- [ ] Unit tests for key storage/retrieval
- [ ] Unit tests for session isolation
- [ ] Integration test: compress → cache → retrieve end-to-end
- [ ] Integration test: cross-session isolation
- [ ] Integration test: TTL expiration
- [ ] Load test: verify performance impact
- [ ] Security test: attempt path traversal
- [ ] Security test: attempt cross-session access

## Estimated Effort

- Tool registration: 2-4 hours
- Key storage: 4-8 hours
- Session isolation: 2-3 hours
- TTL/cleanup: 3-4 hours
- Testing: 4-6 hours
- **Total: 15-25 hours**

## References

- Original PR: #5290
- Code review findings: See PR comments
- Headroom package docs: https://headroom-docs.vercel.app
- Tool registration pattern: `omnigent/tools/builtins/list_comments.py`
