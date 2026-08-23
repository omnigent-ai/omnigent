# Headroom Examples

Examples and demonstrations for Headroom integration with Omnigent.

## Files

### demo.py

Interactive demonstration of Headroom compression features:

```bash
PYTHONPATH=. python examples/headroom/demo.py
```

**Demos:**
1. JSON compression (API responses) - 70% reduction
2. Code compression (source files) - 18% reduction  
3. Conversation history compression
4. Cost savings analysis
5. Integration architecture

**Output:**
- Compression ratios by content type
- Token savings calculations
- Cost projections
- Monthly/annual savings estimates

### config_examples.yaml

10 configuration examples for different use cases:

1. Basic agent (default settings)
2. API-heavy workload (aggressive JSON)
3. Code-focused (optimized for code files)
4. Documentation writer (prose compression)
5. Headroom disabled (fallback)
6. Mixed workload (balanced)
7. Conservative compression
8. Cost-optimized (aggressive all)
9. CCR disabled (privacy-focused)
10. Data analysis (JSON-focused)

**Usage:**

```yaml
# Copy relevant section to your agent.yaml
name: my-agent
harness: claude-sdk
model: claude-sonnet-5

compaction:
  headroom_enabled: true
  headroom_json_threshold: 500
  # ... other settings
```

## Quick Start

### Run the Demo

```bash
PYTHONPATH=. python examples/headroom/demo.py
```

### Try Different Configurations

1. Review `config_examples.yaml`
2. Find a configuration matching your workload
3. Copy to your agent YAML file
4. Test and tune thresholds

## Expected Results

The demo shows simulated compression (works without headroom-ai):

- **JSON:** 60-95% reduction (API responses)
- **Code:** 15-20% reduction (files, diffs)
- **Prose:** 20-40% reduction (logs, docs)

### Cost Savings (50 sessions)

```
Tokens saved:        86,250
Compression ratio:     1.47x
Percent saved:          32%
Cost saved:          $0.43
```

### Monthly Projection (1,000 sessions)

```
Tokens saved:     1,725,000
Monthly cost:        $8.62
Annual cost:       $103.50
```

## Configuration Guidelines

### JSON Threshold
- **100-300:** Aggressive (API-heavy workloads)
- **500:** Default (balanced)
- **1000+:** Conservative (only very large responses)

### Code Threshold
- **300-600:** Aggressive (code-heavy workloads)
- **1000:** Default (balanced)
- **2000+:** Conservative (only very large files)

### Prose Threshold
- **500-1000:** Aggressive (document-heavy)
- **2000:** Default (balanced)
- **3000+:** Conservative (only very large documents)

### Recent Window
- **3-5:** Aggressive compression (cost-optimized)
- **5-7:** Default (balanced)
- **10+:** Conservative (preserve more context)

## See Also

- **User Guide:** [../../docs/HEADROOM.md](../../docs/HEADROOM.md)
- **Main README:** [../../README.headroom.md](../../README.headroom.md)
- **Tests:** `pytest tests/unit/test_headroom_compression.py -v`
