# Model Hardcoding Plan

## Current Curated Inventory

The current baseline lives in `dev/lint/hardcoded_model_allowlist.txt`. It is
count-based by `path` and `model-id`, so unrelated line movement does not break
the hook while net-new pins still fail.

The remaining pins fall into a few buckets:

- **CI automation:** `.github/actions/*`, `.github/scripts/ci/*`, and
  `.github/workflows/*` pin Databricks gateway models for review bots, release
  helpers, image generation, and integration stress jobs.
- **Harness defaults:** native/executor launch paths such as
  `omnigent/pi_native_credentials.py`, `omnigent/opencode_native_provider.py`,
  `omnigent/inner/*_executor.py`, and `omnigent/codex_native_app_server.py`
  still carry fallback model ids.
- **Static pickers/catalogs:** `omnigent/model_catalog.py`,
  `omnigent/cursor_native.py`, and `omnigent/kiro_native.py` encode static
  model choices for CLIs that do not always expose a live listing API.
- **Policy and sizing logic:** `omnigent/llms/context_window.py`,
  `omnigent/policies/builtins/routing.py`, and `omnigent/tools/builtins/spawn.py`
  mention concrete models when mapping windows, routing examples, or dispatch
  examples.
- **Examples/onboarding:** `examples/kimi_hello.yaml` and onboarding provider
  prompts include concrete defaults to make first-run setup work.

## Prevention

- `dev/lint/lint_no_hardcoded_models.py` scans non-test Python/config/shell
  files for concrete model ids in model-selection contexts.
- When a supported file or the baseline changes, `.pre-commit-config.yaml` runs
  the hook across the full tracked lint surface so both new pins and stale
  allowlist counts fail.
- New hardcoded ids fail unless the allowlist count is intentionally updated,
  while removing a pin requires lowering or deleting its baseline entry.

### Scope and exclusions

The hook scans Python, YAML, JSON, TOML, and shell files under `omnigent`,
`scripts`, `examples`, `.github`, and `dev/lint`. Python uses AST context;
config and shell files use model-looking lines while ignoring comment-only
lines.

Tests, Markdown/prose, top-level files, `web` TypeScript/JavaScript, and
generated/vendor trees are intentionally outside the initial lint surface.
Tests need concrete ids as fixtures, while prose and frontend sources need
syntax-aware handling before they can be added without excessive false
positives.

This is a heuristic ratchet, not a parser-level guarantee. Python detection is
limited to model-named assignments, keyword arguments, and dictionary keys; a
model id in an unrelated positional argument or bare collection can escape the
check. Config and shell detection requires the model context and id on the same
line, so multiline/block-scalar values are also outside the initial coverage.
These gaps should be closed with syntax-aware scanners rather than broader
regexes that would make prose false-positive.

The hook intentionally scans the full tracked surface when a supported file
changes. That bounded cost is what lets it enforce exact global baseline counts
instead of only checking additions in changed files. Like the existing Ruff
hooks, local pre-commit execution assumes the repository `.venv` has been
prepared with `just ensure`; CI is the enforcement backstop.

## Migration Plan

1. **Introduce logical model intents.** Replace fallback ids with stable intents
   currently required by callers: `default`, `fast`, `balanced`, and
   `powerful`. Add purpose-specific intents only when a concrete caller needs
   semantics that explicit capability or context requirements cannot express.
2. **Resolve intents at runtime.** Add one resolver that maps intents to the
   active provider's live catalog, with provider-specific preference rules and
   clear errors when no compatible model exists.
3. **Move CI pins to configuration.** Read CI model choices from repo/org vars
   or workflow inputs, with no concrete default in source-controlled workflow
   code.
4. **Centralize static fallback catalogs.** Keep unavoidable static CLI catalogs
   behind one module with provenance, TTL/refresh notes, and a smaller lint
   exception surface.
5. **Ratchet the baseline down.** Each migration removes the corresponding
   `dev/lint/hardcoded_model_allowlist.txt` entry; the lint rejects both count
   increases and stale allowances.
6. **Document escape hatches.** If a temporary pin is unavoidable, require a
   short rationale near the call site and the smallest allowlist count.

## Resolver Contract

The first migration building block lives in `omnigent/model_metadata.py` and
`omnigent/model_resolver.py`:

- Callers request a stable `ModelIntent` instead of a concrete model id.
- `ModelMetadata` records known capabilities, context window, provider-relative
  cost tier, and supported wire APIs. Capability support is tri-state so a
  provider that reports only ids does not accidentally claim support.
- Resolution follows explicit user/session choice, configured provider default,
  live catalog, then documented static fallback precedence.
- Explicit user/session choices intentionally bypass compatibility requirements.
  When an explicit model is absent from the catalog, its metadata and family
  remain unknown rather than being inferred from its id.
- Capability, context, family, and wire requirements filter discovered models;
  unknown metadata does not satisfy a requirement.
- Intents provide best-effort ranking rather than hard tier guarantees. Callers
  can inspect the selected metadata when they need to report the actual tier.
- Wire metadata distinguishes Anthropic Messages, OpenAI Chat Completions,
  OpenAI Responses, Gemini/Vertex `generateContent`, and Bedrock Converse. It
  does not describe native CLI, ACP, or other harness transport protocols.
- Provider catalog order is the default tie-breaker. Providers with richer
  preference rules can supply a `ModelPreferencePolicy` without changing
  callers or the precedence contract.

This contract is intentionally pure and side-effect free. Runtime callers adapt
provider discovery into candidates at the boundary; later migrations can move
smart routing and remaining policy decisions without coupling them to catalog I/O.

## Runtime Default Migration

The first runtime slice adapts the MLflow provider catalog into normalized
resolver candidates and moves unresolved executor/native defaults behind that
boundary:

- Explicit request, spec, ucode, and provider-configured models still win and do
  not depend on discovery.
- Generic Anthropic/OpenAI providers and Databricks gateway paths resolve the
  appropriate Claude/OpenAI catalog family with the `default` intent.
- Catalog capability, context-window, and pricing data become normalized
  metadata; missing capability facts remain unknown rather than supported.
- A missing live catalog produces a clear configuration/discovery error instead
  of silently selecting a stale release-specific fallback.
- Executor and native-launch tests stub the catalog boundary, while catalog and
  resolver tests exercise metadata normalization and selection independently.

Static picker rows, smart-routing tiers, context/wire heuristics, and CI model
inputs remain separate migration slices because they require different discovery
or configuration sources.

## Smart Routing Migration

Smart routing now treats the runner's live worker catalog as its only candidate
source. Provider-relative cost metadata orders candidates for the stable `fast`,
`balanced`, and `powerful` intents; catalog order remains the tie-breaker when
cost is unknown. If discovery is unavailable, routing skips the override and the
harness keeps the provider-resolved default instead of consulting a stale table.

The remaining exact compatibility exclusions in smart routing are temporary
wire-API workarounds. They stay isolated until catalog wire metadata can express
the affected harness constraints without model-name checks.
