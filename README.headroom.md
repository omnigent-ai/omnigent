# Headroom Integration Scaffolding

Integration scaffolding for Headroom AI context compression. Infrastructure is ready for when the `headroom-ai` package becomes publicly available.

## Requirements

This integration requires:
1. `headroom-ai` package (available on PyPI, but dependency commented until CCR implemented)
2. Feature flag enabled: `OMNIGENT_FEATURES=headroom_compression`

Currently disabled: Package is available but dependency kept commented until reversible compression (CCR) is implemented to prevent irreversible data loss.

## Quick Start

```bash
# Package is available on PyPI but dependency commented until CCR implemented
# pip install headroom-ai  # Available at version 0.36.4+

# To enable once CCR is implemented:
# 1. Uncomment dependency in pyproject.toml [project.optional-dependencies] all
# 2. Set feature flag
export OMNIGENT_FEATURES=headroom_compression

# Run tests (currently verify graceful degradation when package unavailable)
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

**Integration Scaffolding** (ready when CCR reversibility is implemented)
- Implementation: Integration layer complete, tested with graceful degradation
- Tests: 21/21 passing (verify no-op behavior when package unavailable)
- Package: `headroom-ai` available on PyPI (v0.36.4+), dependency commented in `all` extra
- Blocker: CCR (reversible compression) not yet implemented - dependency kept commented to prevent irreversible data loss when enabled
- Feature flag: `OMNIGENT_FEATURES=headroom_compression` for future activation

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

### Installation (when CCR is implemented)

```bash
# 1. Uncomment headroom-ai dependency in pyproject.toml [project.optional-dependencies] all
# 2. Install with the all extra
pip install -e ".[all]"

# 3. Enable feature flag
export OMNIGENT_FEATURES=headroom_compression

# 4. Enable in agent config (optional, enabled by default)
compaction:
  headroom_enabled: true
```

### Current behavior (package unavailable/commented)

With `headroom-ai` dependency commented:
- Layer 0 compression returns unchanged content (honest no-op)
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
