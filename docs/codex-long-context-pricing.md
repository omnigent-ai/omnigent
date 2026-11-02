# Codex long-context cost estimates

Codex native sessions and the Codex executor preserve the `last` request breakdown
from `thread/tokenUsage/updated` before coalescing or aggregating usage. Catalog
pricing uses the full request input, including cached input, to select a tier.
Output tokens never select the tier. A qualifying tier prices the whole request,
not merely the tokens above the threshold.

## Metadata and supported models

The shared MLflow catalog's `pricing.long_context` list supplies
`threshold_tokens`, `input_per_million_tokens`, `output_per_million_tokens`, and
optional `cache_read_per_million_tokens` / `cache_write_per_million_tokens`.
Threshold comparisons are strictly greater-than. Missing, incomplete or invalid
tiers are not inferred from a model name. Provider-configured prices still take
precedence and remain flat unless that configuration explicitly gains tier support
in a future change. No release-specific model identifiers are added to runtime code.

The OpenAI catalog retrieved on 2026-10-28 (entries updated 2026-10-27) includes
`gpt-6-astra`, `gpt-6-sol`, and `gpt-6.1-sol`. All three list a 272,000-input-token
threshold, 2× input/cache rates and 1.5× output rates. The deterministic fixtures
cover these exact IDs, including provider-qualified `openai/` lookup. Other
aliases/providers depend on their own unambiguous catalog metadata; they are not
assumed equivalent. The same catalog also lists `gpt-5.6-sol` with this tier.

Sources:

- [MLflow OpenAI catalog](https://github.com/mlflow/mlflow/releases/download/model-catalog%2Flatest/openai.json)
- [GPT-6 Astra](https://developers.openai.com/api/docs/models/gpt-6-astra)
- [GPT-6 Sol](https://developers.openai.com/api/docs/models/gpt-6-sol)
- [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol)

## Precision and compatibility limits

These are catalog estimates, not provider invoices or recovered historical billing.
The catalog's default rates are used. Codex usage notifications do not establish
which service tier was billed; this change does not select `pricing.service_tiers`
or override custom provider prices. Subscription charges, discounts and gateway
pricing need the appropriate provider configuration.

Cumulative totals cannot reconstruct the sizes of earlier requests. Missing or
incomplete request boundaries retain the existing flat estimate for unclassified
tokens. A complete latest request is classified only when it fits the observed
counter growth. Duplicate counters are ignored. On a counter reset, only a complete
latest request is attributable; missing requests cannot be recovered. No historical
backfill or database migration is performed, and native catalog estimates are not
marked as authoritative complete provider costs.

Native forwarding batches request snapshots with retry checkpoints so intermediate
requests survive latest-value coalescing. Persisted checkpoints prevent a retry or
forwarder restart from charging the same snapshot twice. Child inherited totals
are removed before pricing. Executor completions carry request itemization alongside
their turn totals; the server validates that itemized tokens fit those totals before
applying tier adjustments. Older senders without itemization keep flat estimates;
older servers ignore the additional fields and therefore still undercount long requests.

## Local verification

No provider credentials or paid calls are needed:

```sh
uv run --no-sync pytest tests/llms/test_context_window.py tests/llms/test_codex_usage.py \
  tests/llms/test_custom_pricing_integration.py \
  tests/harnesses/codex_native/session/test_usage.py \
  tests/harnesses/codex_native/forwarder/test_usage.py \
  tests/inner/test_codex_executor.py tests/server/routes/test_native_usage_attribution.py \
  --reruns 0 -q
```

The native replay test feeds a 200K-input request followed by a cache-heavy 300K-input
request before flushing, then replays the batch, resumes the forwarder and resets
its counters. The cost must include the long tier once per new request. Two short
requests totaling over 272K must keep base rates, as must a request exactly at 272K.
