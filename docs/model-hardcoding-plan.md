# Model Hardcoding Plan

## Current Inventory

Production model selection uses explicit operator configuration, provider or
CLI discovery, and normalized catalog metadata. The count-based hardcode
baseline is gone. The only source-controlled model aliases are the unavoidable
Claude and Codex records in `omnigent/model_fallbacks.py`; each carries an owner,
provenance, and the discovery gap that prevents a live listing.

## Prevention

- `dev/lint/lint_no_hardcoded_models.py` scans every tracked non-test
  Python/config/shell file for concrete model ids.
- When a supported file changes, `.pre-commit-config.yaml` runs the hook across
  the full tracked lint surface so any non-owned hardcode fails.
- Unavoidable static aliases pass only when Python AST analysis proves they are
  confined to complete `StaticModelFallback` records in
  `omnigent/model_fallbacks.py`, including non-empty owner, provenance, and
  discovery-gap metadata.
- There is no count-based escape hatch. New production aliases must either come
  from configuration/discovery or satisfy the central owned-fallback contract.

### Scope and exclusions

The hook scans tracked Python, YAML, JSON, TOML, and shell files across the
repository. Python AST analysis checks every non-docstring string literal;
config and shell files check every non-comment line. Tests, Markdown/prose,
TypeScript/JavaScript, generated OpenAPI, and vendor/build trees are excluded.

Concrete model ids in runtime help, logs, and errors are production literals
and therefore fail lint. Use provider-neutral wording or synthetic identifier
shapes there; docstrings may retain concrete examples when they materially
improve API documentation.

This remains a model-id regex ratchet rather than a parser-level guarantee for
every provider naming scheme. Extend the regex and add a focused test when a
new complete-id shape appears; do not broaden structural exceptions beyond
complete owned fallback records.

The hook intentionally scans the full tracked surface when a supported file
changes. That bounded cost is what lets it enforce global absence instead of
only checking additions in changed files. Like the existing Ruff hooks, local
pre-commit execution assumes the repository `.venv` has been prepared with
`just ensure`; CI is the enforcement backstop.

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
5. **Delete the baseline.** With production call sites migrated, reject every
   non-owned model literal instead of maintaining path/count exceptions.
6. **Constrain the escape hatch.** Unavoidable static aliases must live in the
   central fallback registry with literal ownership, provenance, and discovery
   gap metadata.

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

Smart routing retains the runner's normalized wire metadata when building the
id-only candidate set expected by routing clients. Compatibility post-processing
uses those catalog facts and treats missing metadata from older runners as
unknown instead of inferring protocol support from model names.

## Context Window Migration

Context sizing and pricing now share the onboarding/model-resolver MLflow
catalog cache. Stable family patterns locate a bounded provider catalog, while
provider-qualified and vendor-namespaced ids route directly to their catalog
source. Exact catalog metadata wins; family-prefix matches are accepted only
when every candidate agrees on the requested field.

The stale exact-id Qwen window table and duplicate catalog downloader are gone.
An explicit Anthropic `[1m]` marker remains self-describing metadata, and an
uncatalogued/offline model keeps the conservative 128K fallback rather than a
release-specific guess.

## Pi Wire Routing

Pi gateway configuration consumes the same normalized Unity Catalog model
service metadata. Databricks GPT models use the Responses or Chat surface the
catalog advertises, while generic OpenAI-compatible providers honor their
configured wire. If Databricks discovery is unavailable, an unknown GPT uses
Responses rather than a release-specific completions allowlist.

## Pi Picker Migration

Inner Pi sessions populate their model registry from live Unity Catalog model
services instead of a release-specific Databricks list. MLflow metadata adds
context and output limits when available. If discovery is unavailable, the
resolved run model is still registered so launch does not depend on picker
enumeration; no stale alternatives are offered.

## Kiro Picker Migration

The Kiro Web picker now runs `kiro-cli chat --list-models --format json` on the
bound runner and forwards the CLI's model ids, default, descriptions, context
windows, and credit rates. The server caches the runner response through the
same asynchronous picker path as Codex, so provider changes no longer require an
Omnigent source update and snapshots do not block on the CLI process.

## CI Model Configuration

Credentialed automation reads model roles from repository variables instead of
pinning provider releases in workflow source:

- `OMNIGENT_CI_ANTHROPIC_MODEL`
- `OMNIGENT_CI_FAST_ANTHROPIC_MODEL`
- `OMNIGENT_CI_OPENAI_MODEL`
- `OMNIGENT_CI_E2E_JUDGE_MODEL`
- `OMNIGENT_CI_E2E_MODEL_POOL_GPT`
- `OMNIGENT_CI_IMAGE_MODEL`

Operators can update those values as provider catalogs change without a code
release. Workflows deliberately do not carry source-controlled model defaults;
missing required variables fail or take their existing fail-open path.

## Ad-hoc CLI Defaults

Minimal agent YAMLs that declare neither a harness nor a model now resolve the
Databricks OpenAI-family default from the provider catalog during bundle
materialization. `--model` and `OMNIGENT_MODEL` remain higher-precedence explicit
choices. If discovery is unavailable, the CLI asks for one of those explicit
values instead of silently baking a release-specific model into the bundle.

## Onboarding Defaults

Provider setup derives its suggested default from the live catalog after
excluding specialty modalities. Stable family preferences choose broadly
accessible Anthropic and OpenRouter tiers without naming a release. When the
catalog is unavailable, onboarding accepts an explicit value instead of
prefilling a source-controlled model pin.

The same policy supplies the final runtime fallback for key, gateway, and local
providers. Explicit agent and provider defaults still win; without either,
runtime discovery fails with configuration guidance when no catalog is available.

## Persistent Catalog Resilience

The shared MLflow catalog boundary persists one validated last-known-good file
per provider in the platform user-cache directory. A cache is fresh for one
hour; if live retrieval fails, a validated entry remains usable for up to seven
days and logs its source and age. Atomic replacement prevents concurrent
processes from exposing partial JSON.

Cache files record their own schema version, the upstream catalog schema,
source URL, and fetch time. Corrupt, incompatible, wrong-source, or over-age
entries are ignored. `OMNIGENT_DISABLE_CATALOG_LOOKUP=1` bypasses in-memory,
disk, and network lookup so tests cannot inherit developer-machine state.

## Configuration Help Text

Setup prompts, routing policy schemas, and spawn-tool descriptions explain the
expected provider-configured model value without embedding release-specific ids.
Runtime help may use synthetic examples to show identifier shape; concrete
release ids belong in tests or provider-owned documentation.

## Static Fallback Ownership

The remaining Claude and Codex aliases live only in
`omnigent/model_fallbacks.py`. Each fallback records its adapter owner, catalog
provenance, and the discovery gap that prevents a live listing. `sys_list_models`
surfaces those fields whenever it returns an unverified static catalog.

## Kimi Example Default

The Kimi launcher example declares only the harness. With no explicit
`--model` or session override, Omnigent omits `HARNESS_KIMI_MODEL` and lets the
Kimi CLI use the default from its own provider configuration.
