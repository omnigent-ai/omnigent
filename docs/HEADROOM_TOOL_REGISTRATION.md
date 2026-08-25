# Headroom Retrieve Tool Registration

## Current Status

The `headroom_retrieve` tool is registered as a **framework-owned** builtin (similar to `list_comments` and `update_comment`). 

## Important: Tool Must Be Enabled

**When using Headroom compression, agents MUST include `headroom_retrieve` in their tool list** to recover compressed content.

### Example Agent Configuration

```yaml
name: my-agent
harness: claude-sdk
model: claude-sonnet-5

# Enable Headroom compression
compaction:
  headroom_enabled: true
  headroom_enable_ccr: true

# IMPORTANT: Include headroom_retrieve tool
tools:
  builtins:
    - headroom_retrieve  # Required for CCR
    - web_search
    # ... other tools

instructions: |
  Your agent instructions here.
  When you see compressed content with a retrieval key, use:
  headroom_retrieve(key="...") to get the full original.
```

## Why This Matters

When Layer 0 compression is active, large tool outputs are replaced with compressed versions like:

```
{"summary": "...compressed..."}

[Compressed. Retrieve: headroom_retrieve(key="abc123")]
```

Without the `headroom_retrieve` tool enabled, the agent:
- Cannot recover the original content
- Will try to call a tool it doesn't have
- May produce incorrect responses due to missing detail

## Future Enhancement

A future improvement could auto-register `headroom_retrieve` when `headroom_enabled: true` is set, similar to how `list_comments` is auto-registered. For now, explicit opt-in is required.
