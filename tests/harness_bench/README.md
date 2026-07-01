# Harness test bench

A standardized, pluggable conformance suite that probes a harness and
reports a verdict per capability dimension, reconciling observed behavior
against a self-declared profile to surface drift. Design and rationale:
[`docs/harness-bench-design.md`](../../docs/harness-bench-design.md).

## Run it

```bash
# List official harnesses.
python -m tests.harness_bench --list

# Offline (declared) matrix — no turns, no creds.
python -m tests.harness_bench

# Live probe one harness against a gateway profile.
python -m tests.harness_bench --harness codex --profile my-profile

# Live probe every official harness.
python -m tests.harness_bench --profile my-profile
```

Output formats (mutually exclusive):

- default: an aligned, ANSI-colored terminal table (color auto-disables
  when piped or with `--no-color`), followed by a Notes section explaining
  every non-supported cell so a `·` is never opaque.
- `--markdown`: the GitHub-flavored table for docs / PRs.
- `--json`: machine-readable, for diffing runs or regenerating docs.

A non-zero exit means a `DRIFT` cell was found (observed behavior
disagrees with the declared matrix).

## What it reports (P0 dimensions)

`basic_turn`, `streaming`, `tool_calling`, `interrupt`, `policy_deny`,
`model_override`. Verdicts map to the support-matrix glyphs
(`✓ ~ ✗ — ?`), plus `·` skipped and `!! DRIFT`.

## Layout

| File | Role |
| --- | --- |
| `verdict.py` | `Verdict` / `Priority` / `ProbeResult` and the `reconcile` drift check |
| `profile.py` | `BenchProfile` (per-harness self-declaration) + name resolution |
| `manifest.py` | Official profiles, built from `tests/e2e/_harness_probes.py` |
| `driver.py` | `SdkInprocDriver` — spawns a harness wrap, drives turns over SSE |
| `probes/` | One module per dimension; `ALL_PROBES` is the registry |
| `bench.py` | Orchestrator: probes × harnesses → `BenchMatrix` |
| `report.py` | Markdown / JSON renderers |
| `test_bench.py` | Offline conformance (always) + live layer (gated on `--profile`) |

## Add a harness

- **Official:** add a `BenchProfile` to `manifest.py` (base fields come
  from `_harness_probes.HARNESS_PROBES`). No probe or driver edits.
- **Community / out-of-repo:** ship a `BenchProfile` and select it by
  reference: `--harness mypkg.harness:PROFILE`. No bench edits.

## Add a dimension

Add a `CapabilityProbe` subclass under `probes/`, list it in
`probes/__init__.py:ALL_PROBES`, and add its declared verdict to the
profiles. Probes are harness-agnostic — they only call the driver.

## Scope

Phase-1 MVP: the six P0 dimensions above, the `sdk-inproc` transport
driver, and the four official SDK harnesses (claude-sdk, codex, pi,
openai-agents). Phase-2 (per the design doc): native transport drivers
(tmux / app-server / HTTP-SSE), the remaining SDK + native harnesses, and
the P1 dimensions (steering, live-queue, resume/fork, elicitation,
reasoning, images, cost, compaction).
