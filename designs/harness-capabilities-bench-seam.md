# Seam: harness capabilities → harness bench

**Audience:** whoever wires the harness bench (`tests/harness_bench/`, the
`#1787 → #1790 → #1792` stack) to consume the declarative capability model.
**Status:** capability model is PR #1847 (open, base `main`). This note is the
contract for the follow-up that makes the bench derive from it. No bench code
has been changed yet.

---

## The one-sentence idea

The bench today hand-maintains a "declared support matrix" in
`tests/harness_bench/manifest.py` (`_P0_ALL_SUPPORTED` verdicts + `_STATIC`
columns). That is a *second copy* of "what each harness supports". PR #1847 adds
the *first, canonical* copy — `harness_capabilities()`. **Make the manifest
derive from `harness_capabilities()` and delete the hand-typed dicts**, so there
is one source of truth and the bench's job sharpens from "live probe vs a typed
guess" to "**does the harness actually do what it publicly claims?**".

---

## Two capability layers — do not confuse them

There are now *two* places that describe harness abilities. The bench must read
the **static** one.

| Layer | Where | Nature | Observable |
|---|---|---|---|
| **Static** (use this) | `omnigent.harness_plugins.harness_capabilities()` → `dict[str, HarnessCapabilities]` | *Declared* trait/claim, pre-spawn | Immediately, no subprocess |
| Runtime | `omnigent.inner.executor.Executor.supports_streaming()` / `interrupt_session()` … | *Actual* in-subprocess behavior | Only after spawn |

The manifest declares **expectations**, so it derives from the **static** layer.
The bench's *probes* already measure the runtime behavior live — that is the
verification half, and it stays as-is.

---

## Where the data lives (PR #1847)

- Type: `omnigent/harness_capabilities.py` → `HarnessCapabilities` (frozen
  dataclass) + enums `IntegrationMode`, `Elicitation`, `Resume`, `EffortFamily`,
  `ModelFamily`, `AuthModel`. Import-safe (no onboarding/provider imports), like
  `harness_install_spec.py`.
- Data: per-harness on `HarnessContribution.capabilities`; built-ins in
  `harness_plugins._BUILTIN_CAPABILITIES` (all 23 harnesses).
- Accessor: `harness_plugins.harness_capabilities() -> dict[str, HarnessCapabilities]`
  (merged across contributions, so community plugins' capabilities flow in too).
- Serialized: `HarnessCapabilities.as_dict()` and each `harness_catalog()` row's
  `"capabilities"` key (already on `GET /v1/harnesses`).

Fields: `integration_mode`, `elicitation`, `resume`, `effort`, `model_family`,
`auth`, `subagents`, `interrupt`, `streaming`.

---

## Axis mapping (this is the non-obvious part)

The bench's axes are not 1:1 with capabilities: probes measure **behaviors**,
capabilities describe **traits**. Three groups:

### A. Descriptive columns → derive directly from capabilities
Replaces the hand-typed `manifest._STATIC`:

| `manifest._STATIC` column | Capability field | Note |
|---|---|---|
| `implementation` | `integration_mode` | e.g. `SDK_IN_PROCESS` → "SDK in-process". Map enum→prose in one helper. |
| `auth` | `auth` | `OMNIGENT_CREDENTIAL` / `OWN_AUTH` / `SESSION_SCOPED_CONFIG`. The old free-text ("Anthropic key / Databricks gateway") is richer prose; keep a small enum→string map if you want the exact wording, or simplify. |
| *(new columns available for free)* | `model_family`, `effort`, `resume`, `elicitation`, `subagents` | Pure metadata the report can now show without new plumbing. |

### B. Declared verdicts → derive where a capability backs the probe
Replaces `manifest._P0_ALL_SUPPORTED`:

| Bench probe | Backing capability | Declared verdict rule |
|---|---|---|
| `interrupt` | `interrupt: bool` | `True` → `SUPPORTED`, else `PARTIAL`/`UNSUPPORTED` |
| `streaming` | `streaming: bool` | `True` → `SUPPORTED` (deltas), else `PARTIAL` (complete-only) |
| `model_override` | `SDK_MODEL_OVERRIDE_HARNESSES` (already in the registry via `model_env_keys()`) or `native` metadata | already derivable from #1756; no new field |

### C. Probe-only — no capability backing; leave hand-declared
These are behaviors with no single trait to key off. Keep them in the manifest
as-is (or a small explicit table):

- `basic_turn` — every harness is expected to complete a turn; not a
  differentiating capability.
- `tool_calling` — not modeled as a capability axis (all P0 harnesses support
  it; would need a new axis if that changes).
- `policy_deny` — related to `elicitation` but *not* identical (policy DENY is
  enforcement, elicitation is the ASK surface). Do **not** derive `policy_deny`
  from `elicitation`; keep it explicit unless you add a dedicated axis.

**Rule of thumb:** derive A and B; leave C. If you find yourself forcing a
probe-only behavior onto a trait, add a new capability axis instead (see below).

---

## Semantic shift after wiring

`verdict.reconcile()` compares declared vs live-probed. Today "declared" is a
typed guess. After this seam, "declared" = the harness's **published capability**.
So a DRIFT now means **"a harness's capability declaration is false"** — which
makes the capability table self-enforcing (you can't lie in `_BUILTIN_CAPABILITIES`
without the bench catching it on the next live run). Say this in the reconcile
output so the signal is legible.

---

## Confidence caveat (important for correctness)

Only the **four P0 SDK harnesses** — `claude-sdk`, `codex`, `pi`,
`openai-agents` — have `interrupt`/`streaming` **verified live** by the bench
today (declared `True/True`; a test in `test_harness_capabilities.py` pins this).
The other 19 harnesses' `interrupt`/`streaming` values are **declared
best-effort by integration mode**, not yet probe-verified. That is fine and
intended — it is exactly the declare-then-reconcile workflow — but the bench
wiring must not treat those 19 as ground truth. As transport drivers land for
phase-2 harnesses, their live verdicts either confirm the declaration or raise
DRIFT (which then corrects the declaration). Do not silently assume the
best-effort values are right.

---

## Adding a new axis (if a probe-only behavior needs backing)

1. Add the field to `HarnessCapabilities` (+ `as_dict()`), in
   `omnigent/harness_capabilities.py`.
2. Fill it for all 23 in `_BUILTIN_CAPABILITIES`.
3. If derivable from an existing constant, add a guard test in
   `tests/test_harness_capabilities.py` asserting the declaration matches its
   source (see `test_model_family_matches_model_override_sets`).
Keep the model small — only add an axis when a real consumer (a probe) needs it.

---

## Suggested sequence

1. #1847 lands (capability model + `interrupt`/`streaming` axes).
2. Follow-up bench PR:
   - a `manifest.py` helper `_declared_from_capabilities(harness) -> dict[dimension, Verdict]` for group B, and enum→prose helpers for group A;
   - delete `_P0_ALL_SUPPORTED` and the derivable parts of `_STATIC`;
   - keep group-C dimensions explicit;
   - update `reconcile()` phrasing to "declared capability vs observed".
3. Phase-2 harness rollout then gets its metadata for free (all 23 already
   declared) — only transport drivers remain bench-side work.

---

## Gotchas checklist

- [ ] Read the **static** `harness_capabilities()`, not `Executor.supports_*`.
- [ ] Derive groups A + B only; leave `basic_turn` / `tool_calling` /
      `policy_deny` explicit.
- [ ] Don't equate `policy_deny` with `elicitation`.
- [ ] Treat non-P0 `interrupt`/`streaming` as best-effort until probed.
- [ ] Community-plugin harnesses flow through `harness_capabilities()` too —
      the manifest should tolerate harnesses with no declared capabilities
      (sparse dict), not `KeyError`.
