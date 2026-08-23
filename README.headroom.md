# Headroom Integration Scaffolding

Integration scaffolding for Headroom AI context compression. Infrastructure is ready for when the `headroom-ai` package becomes publicly available.

## Requirements

This integration requires:
1. `headroom-ai` package installed (`pip install headroom-ai`)
2. Feature flag enabled: `OMNIGENT_FEATURES=headroom_compression`

Without these, Layer 0 compression is disabled and no token reduction occurs.

## Quick Start

```bash
# Install headroom-ai (when available)
pip install headroom-ai

# Enable feature flag
export OMNIGENT_FEATURES=headroom_compression

# Run tests
pytest tests/unit/test_headroom_compression.py -v

# Enable in agent config
compaction:
  headroom_enabled: true
```

## What It Does (When Installed)

Adds Layer 0 compression before existing compaction layers:

- **JSON:** 60-95% reduction (API responses)
- **Code:** 15-20% reduction (files, diffs)
- **Prose:** 20-40% reduction (logs, docs)

## Documentation

- **User Guide:** [`docs/HEADROOM.md`](docs/HEADROOM.md)
- **Demo:** [`examples/headroom/demo.py`](examples/headroom/demo.py)
- **Examples:** See `docs/HEADROOM.md` for configuration examples

## Status

**Integration Scaffolding** (ready for when `headroom-ai` becomes available)
- Implementation: Integration layer complete, tested with graceful degradation
- Tests: 21/21 passing (unit tests verify no-op behavior when package unavailable)
- Package: `headroom-ai` not yet publicly available (optional extra prepared)
- Feature flag: `OMNIGENT_FEATURES=headroom_compression` for future activation
- CCR (reversible compression): Placeholder - not yet implemented

## Files

**Implementation (4):**
- `omnigent/runtime/headroom_compression.py` - Core module
- `omnigent/runtime/compaction.py` - Layer 0 integration
- `omnigent/spec/types.py` - Configuration
- `pyproject.toml` - Dependency (ready to enable)

**Tests (2):**
- `tests/unit/test_headroom_compression.py`
- `tests/integration/test_headroom_integration.py`

**Examples (1):**
- `examples/headroom/demo.py`

**Documentation (1):**
- `docs/HEADROOM.md`

## Deployment

### Installation

```bash
# 1. Install headroom-ai package
pip install headroom-ai

# 2. Enable feature flag
export OMNIGENT_FEATURES=headroom_compression

# 3. Enable in agent config (optional, enabled by default)
compaction:
  headroom_enabled: true
```

### Without headroom-ai

If `headroom-ai` is not installed:
- Layer 0 compression is skipped
- No token reduction occurs
- No fake metrics are reported
- Compaction falls back to existing Layers 1-3

## Expected Impact (When Package Becomes Available)

**Projected for 50-developer team once headroom-ai is installed:**
- Token reduction: 15-95% (content-type dependent: JSON 60-95%, code 15-20%, prose 20-40%)
- Cost savings: Variable, depends on content mix and model pricing
- Quality: Compression is lossy as CCR retrieval not yet implemented

**Current behavior:** Returns unchanged content with no compression when package unavailable.

See `docs/HEADROOM.md` for full documentation.
