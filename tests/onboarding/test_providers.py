"""Tests for omnigent.onboarding.providers — catalog loading and queries."""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.onboarding import providers as _providers_mod
from omnigent.onboarding.providers import (
    ModelInfo,
    ProviderConfig,
    default_chat_model,
    find_catalog_models,
    get_all_providers,
    get_chat_models,
    get_models,
    get_provider_config,
)

# Minimal catalog fixture that mirrors the real MLflow JSON schema.
# Keyed by provider name; each value is what _fetch_provider_catalog returns
# (the full parsed dict with a "models" key).
_FAKE_CATALOG: dict[str, dict] = {
    "anthropic": {
        "schema_version": "1.0",
        "models": {
            "claude-opus-4-8": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 32000},
                "pricing": {
                    "input_per_million_tokens": 15.0,
                    "output_per_million_tokens": 75.0,
                    "cache_read_per_million_tokens": 1.5,
                    "cache_write_per_million_tokens": 18.75,
                },
            },
            "claude-sonnet-4-6": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 8192},
            },
            "claude-haiku-4-5": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 200000, "max_output": 8192},
            },
            "claude-3-embedding": {
                "mode": "embedding",
                "capabilities": {},
            },
        },
    },
    "openai": {
        "schema_version": "1.0",
        "models": {
            "gpt-5.5-audio-preview-2026-06-01": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-5.5": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-4.1": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "gpt-3.5-turbo": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 16385, "max_output": 4096},
            },
        },
    },
    "openrouter": {
        "schema_version": "1.0",
        "models": {
            "openai/gpt-6": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "moonshotai/kimi-k2.6": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 128000, "max_output": 16384},
            },
            "qwen/qwen3-coder": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 262100, "max_output": 262100},
            },
            "qwen/qwen3-coder-plus": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 997952, "max_output": 65536},
            },
        },
    },
    "gemini": {
        "schema_version": "1.0",
        "models": {
            "gemini/gemini-2.5-flash": {
                "mode": "chat",
                "capabilities": {"function_calling": True},
                "context_window": {"max_input": 1000000, "max_output": 8192},
            },
        },
    },
}

_REAL_FETCH_PROVIDER_CATALOG = _providers_mod._fetch_provider_catalog


@pytest.fixture(autouse=True)
def mock_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch _fetch_provider_catalog to return fixture data without network calls."""
    monkeypatch.setattr(
        _providers_mod,
        "_fetch_provider_catalog",
        lambda provider: _FAKE_CATALOG.get(provider, {}),
    )


@pytest.fixture
def real_catalog_loader(
    mock_catalog: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Path:
    """Restore the real loader with an isolated persistent cache root."""
    del mock_catalog
    monkeypatch.setattr(_providers_mod, "_fetch_provider_catalog", _REAL_FETCH_PROVIDER_CATALOG)
    monkeypatch.setattr(_providers_mod, "_catalog_cache_root", lambda: tmp_path)
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    _providers_mod._catalog_cache.clear()
    return tmp_path


# ── get_all_providers ──────────────────────────────────────


def test_get_all_providers_returns_nonempty_list() -> None:
    """Catalog must contain at least the major providers."""
    providers = get_all_providers()
    # 68 JSON files in the catalog; after normalization and exclusion
    # there should still be many providers.
    assert len(providers) > 40, (
        f"Expected at least 40 providers from the catalog, "
        f"got {len(providers)}. Catalog files may be missing."
    )


def test_get_all_providers_contains_major_providers() -> None:
    """Major providers must appear in the catalog."""
    providers = get_all_providers()
    for expected in ["anthropic", "openai", "gemini", "groq", "deepseek"]:
        assert expected in providers, (
            f"Expected {expected!r} in providers list. "
            f"Catalog file {expected}.json may be missing."
        )


def test_get_all_providers_popular_first() -> None:
    """Popular providers must appear before the rest."""
    from omnigent.onboarding.providers import COMMON_PROVIDERS

    providers = get_all_providers()
    # The first entries should be the popular providers (in order).
    popular_in_list = [p for p in providers if p in COMMON_PROVIDERS]
    expected_popular = [p for p in COMMON_PROVIDERS if p in set(providers)]
    assert popular_in_list[: len(expected_popular)] == expected_popular, (
        f"Popular providers should appear first in COMMON_PROVIDERS order. "
        f"Got: {popular_in_list[: len(expected_popular)]}, "
        f"expected: {expected_popular}"
    )
    # The rest (after popular) should be alphabetically sorted.
    rest = providers[len(expected_popular) :]
    assert rest == sorted(rest), (
        f"Non-popular providers should be alphabetically sorted, but got: {rest[:10]}..."
    )


def test_get_all_providers_excludes_bedrock_converse() -> None:
    """bedrock_converse is a variant that should be excluded."""
    providers = get_all_providers()
    assert "bedrock_converse" not in providers, (
        "bedrock_converse should be excluded (it's a variant of bedrock)."
    )


def test_get_all_providers_consolidates_vertex_ai() -> None:
    """vertex_ai-* variants should be consolidated into vertex_ai."""
    providers = get_all_providers()
    assert "vertex_ai" in providers, (
        "vertex_ai should appear after consolidating vertex_ai-* variants."
    )
    vertex_variants = [p for p in providers if p.startswith("vertex_ai-")]
    assert vertex_variants == [], (
        f"vertex_ai-* variants should be consolidated, but found: {vertex_variants}"
    )


# ── get_models / get_chat_models ─────────────────────────


def test_get_models_anthropic_returns_models() -> None:
    """Anthropic catalog must contain known models."""
    models = get_models("anthropic")
    assert len(models) > 0, "Expected at least one model from the anthropic catalog."
    # Every model should have the provider field set.
    for m in models:
        assert m.provider == "anthropic", (
            f"Model {m.name!r} has provider={m.provider!r}, expected 'anthropic'."
        )


def test_get_models_preserves_context_and_cache_pricing_metadata() -> None:
    """Shared model metadata includes fields needed by sizing and cost callers."""
    model = next(model for model in get_models("anthropic") if model.name == "claude-opus-4-8")

    assert model.max_input_tokens == 200000
    assert model.max_output_tokens == 32000
    assert model.input_price == 15.0
    assert model.output_price == 75.0
    assert model.cache_read_price == 1.5
    assert model.cache_write_price == 18.75


@pytest.mark.parametrize(
    ("model_id", "provider", "name"),
    [
        ("anthropic/claude-opus-4-8", "anthropic", "claude-opus-4-8"),
        ("ANTHROPIC/CLAUDE-OPUS-4-8", "anthropic", "claude-opus-4-8"),
        ("claude-opus-4-8", "anthropic", "claude-opus-4-8"),
        ("qwen/qwen3-coder", "openrouter", "qwen/qwen3-coder"),
        ("qwen3-coder", "openrouter", "qwen/qwen3-coder"),
        ("databricks-claude-opus-4-8", "anthropic", "claude-opus-4-8"),
        ("databricks/databricks-claude-opus-4-8", "anthropic", "claude-opus-4-8"),
        ("DATABRICKS-CLAUDE-OPUS-4-8", "anthropic", "claude-opus-4-8"),
    ],
)
def test_find_catalog_models_resolves_provider_and_vendor_namespaces(
    model_id: str,
    provider: str,
    name: str,
) -> None:
    """Catalog lookup uses provider metadata without exact release mappings."""
    matches = find_catalog_models(model_id)

    assert [(match.provider, match.name) for match in matches] == [(provider, name)]


def test_find_catalog_models_matches_vendor_namespaced_families() -> None:
    """OpenRouter family fallback preserves the vendor namespace."""
    matches = find_catalog_models("qwen/qwen3-coder-new")

    assert [(match.provider, match.name) for match in matches] == [
        ("openrouter", "qwen/qwen3-coder"),
        ("openrouter", "qwen/qwen3-coder-plus"),
    ]


def test_shared_provider_catalog_caches_success_and_failure(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared TTL cache absorbs repeated lookups, including failures."""
    del real_catalog_loader
    calls: list[str] = []

    def _download(provider: str) -> dict | None:
        calls.append(provider)
        if provider == "anthropic":
            return _FAKE_CATALOG[provider]
        return None

    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", _download)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    assert _REAL_FETCH_PROVIDER_CATALOG("xai") == {}
    assert _REAL_FETCH_PROVIDER_CATALOG("xai") == {}
    assert calls == ["anthropic", "xai"]


def test_provider_catalog_persists_validated_live_result(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful live fetch writes source and schema metadata atomically."""
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: 1_000.0)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == _FAKE_CATALOG["anthropic"]

    value = json.loads((real_catalog_loader / "anthropic.json").read_text())
    assert value["cache_schema_version"] == 1
    assert value["catalog_schema_version"] == "1.0"
    assert value["source_url"].endswith("/anthropic.json")
    assert value["fetched_at"] == 1_000.0
    assert value["catalog"] == _FAKE_CATALOG["anthropic"]


def test_provider_catalog_reuses_fresh_disk_without_network(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh validated disk entry wins after the process cache is cleared."""
    now = 2_000.0
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: now)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )
    _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    _providers_mod._catalog_cache.clear()

    def _unexpected_download(_provider: str) -> None:
        raise AssertionError("fresh disk cache must avoid network discovery")

    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", _unexpected_download)
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: now + 60)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == _FAKE_CATALOG["anthropic"]
    assert (real_catalog_loader / "anthropic.json").is_file()


def test_provider_catalog_uses_stale_disk_only_after_live_failure(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale-but-bounded entry is used with observable provenance."""
    fetched_at = 3_000.0
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: fetched_at)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )
    _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    _providers_mod._catalog_cache.clear()
    monkeypatch.setattr(
        _providers_mod,
        "_catalog_now",
        lambda: fetched_at + _providers_mod._CATALOG_TTL_SECONDS + 1,
    )
    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", lambda _provider: None)

    with caplog.at_level(logging.WARNING, logger=_providers_mod.__name__):
        result = _REAL_FETCH_PROVIDER_CATALOG("anthropic")

    assert result == _FAKE_CATALOG["anthropic"]
    assert "Using stale model catalog cache" in caplog.text
    assert "provider=anthropic" in caplog.text
    assert "source=https://github.com/" in caplog.text
    assert (real_catalog_loader / "anthropic.json").is_file()


def test_provider_catalog_rejects_over_age_cache(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An entry beyond the stale-if-error window never becomes a default."""
    fetched_at = 4_000.0
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: fetched_at)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )
    _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    _providers_mod._catalog_cache.clear()
    monkeypatch.setattr(
        _providers_mod,
        "_catalog_now",
        lambda: fetched_at + _providers_mod._CATALOG_STALE_IF_ERROR_SECONDS + 1,
    )
    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", lambda _provider: None)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == {}
    assert (real_catalog_loader / "anthropic.json").is_file()


def test_provider_catalog_repairs_corrupt_cache_from_live_fetch(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unreadable JSON is ignored and replaced by the next valid download."""
    cache_path = real_catalog_loader / "anthropic.json"
    cache_path.write_text("{not-json", encoding="utf-8")
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: 5_000.0)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == _FAKE_CATALOG["anthropic"]
    assert json.loads(cache_path.read_text())["catalog"] == _FAKE_CATALOG["anthropic"]


@pytest.mark.parametrize(
    "metadata_override",
    [
        {"cache_schema_version": 999},
        {"catalog_schema_version": "2.0"},
        {"source_url": "https://catalog.invalid/anthropic.json"},
    ],
    ids=("cache-schema", "catalog-schema", "source-url"),
)
def test_provider_catalog_rejects_incompatible_cache_metadata(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
    metadata_override: dict[str, object],
) -> None:
    """Incompatible wrapper provenance is not used after a live failure."""
    source_url = _providers_mod._catalog_source_url("anthropic")
    value = {
        "cache_schema_version": 1,
        "catalog_schema_version": "1.0",
        "source_url": source_url,
        "fetched_at": 6_000.0,
        "catalog": _FAKE_CATALOG["anthropic"],
    }
    value.update(metadata_override)
    (real_catalog_loader / "anthropic.json").write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: 6_001.0)
    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", lambda _provider: None)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == {}


@pytest.mark.parametrize("schema_version", ["1", "1.0", "1.1", 1])
def test_provider_catalog_accepts_compatible_schema_versions(schema_version: object) -> None:
    """Compatible major versions tolerate upstream minor and representation changes."""
    catalog = {"schema_version": schema_version, "models": {"model": {}}}

    assert _providers_mod._valid_catalog_payload(catalog)


@pytest.mark.parametrize("schema_version", ["2.0", 2, True, "invalid", "².0", None])
def test_provider_catalog_rejects_incompatible_schema_versions(schema_version: object) -> None:
    """Malformed and unsupported major versions cannot enter the cache."""
    catalog = {"schema_version": schema_version, "models": {"model": {}}}

    assert not _providers_mod._valid_catalog_payload(catalog)


def test_provider_catalog_rejects_empty_model_map() -> None:
    """An empty upstream result cannot replace useful last-known-good data."""
    assert not _providers_mod._valid_catalog_payload({"schema_version": "1.0", "models": {}})


def test_provider_catalog_rejects_incompatible_live_schema(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unsupported upstream schema is neither returned nor persisted."""
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: {"schema_version": "2.0", "models": {"model": {}}},
    )

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == {}
    assert list(real_catalog_loader.iterdir()) == []


def test_empty_live_catalog_preserves_stale_last_known_good(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient empty response falls back without overwriting the disk cache."""
    fetched_at = 6_500.0
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: fetched_at)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )
    _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    cache_path = real_catalog_loader / "anthropic.json"
    original_cache = cache_path.read_text()
    _providers_mod._catalog_cache.clear()
    monkeypatch.setattr(
        _providers_mod,
        "_catalog_now",
        lambda: fetched_at + _providers_mod._CATALOG_TTL_SECONDS + 1,
    )
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: {"schema_version": "1.0", "models": {}},
    )

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == _FAKE_CATALOG["anthropic"]
    assert cache_path.read_text() == original_cache


def test_catalog_source_url_quotes_provider_path_characters() -> None:
    """Provider input cannot inject release-path or query delimiters."""
    url = _providers_mod._catalog_source_url("anthropic/../../asset?download=1")

    assert url.endswith("/anthropic%2F..%2F..%2Fasset%3Fdownload%3D1.json")


def test_disabled_catalog_lookup_bypasses_memory_disk_and_network(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The test isolation switch cannot inherit developer cache state."""
    monkeypatch.setattr(_providers_mod, "_catalog_now", lambda: 7_000.0)
    monkeypatch.setattr(
        _providers_mod,
        "_download_provider_catalog",
        lambda _provider: _FAKE_CATALOG["anthropic"],
    )
    _REAL_FETCH_PROVIDER_CATALOG("anthropic")
    assert (real_catalog_loader / "anthropic.json").is_file()
    monkeypatch.setenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", "1")

    def _unexpected(*_args: object) -> None:
        raise AssertionError("disabled lookup must bypass every catalog cache tier")

    monkeypatch.setattr(_providers_mod, "_read_disk_catalog", _unexpected)
    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", _unexpected)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == {}


def test_concurrent_catalog_writes_leave_one_complete_entry(
    real_catalog_loader: Path,
) -> None:
    """Concurrent processes can race without exposing partial JSON."""
    source_url = _providers_mod._catalog_source_url("anthropic")

    def _write(index: int) -> None:
        catalog = {
            "schema_version": "1.0",
            "models": {f"model-{index}": {"mode": "chat"}},
        }
        _providers_mod._write_disk_catalog(
            _providers_mod._DiskCatalogEntry(
                catalog=catalog,
                source_url=source_url,
                fetched_at=float(index),
            ),
            "anthropic",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_write, range(32)))

    entry = _providers_mod._read_disk_catalog("anthropic", source_url)
    assert entry is not None
    assert len(entry.catalog["models"]) == 1
    assert list(real_catalog_loader.iterdir()) == [real_catalog_loader / "anthropic.json"]


def test_provider_catalog_without_live_or_cached_data_fails_empty(
    real_catalog_loader: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh install still fails clearly instead of inventing a model."""
    monkeypatch.setattr(_providers_mod, "_download_provider_catalog", lambda _provider: None)

    assert _REAL_FETCH_PROVIDER_CATALOG("anthropic") == {}
    assert list(real_catalog_loader.iterdir()) == []


def test_get_chat_models_filters_to_chat_mode() -> None:
    """get_chat_models must only return mode='chat' models."""
    chat_models = get_chat_models("anthropic")
    assert len(chat_models) > 0, "Expected at least one chat model from anthropic catalog."
    for m in chat_models:
        assert m.mode == "chat", (
            f"get_chat_models returned model {m.name!r} with mode={m.mode!r}, expected 'chat'."
        )


def test_get_chat_models_sorted_newest_first() -> None:
    """Chat models must be sorted with newer versions before older ones."""
    chat_models = get_chat_models("openai")
    # gpt-5.x models should appear before gpt-4.x models, which
    # should appear before gpt-3.5 models. Check that the first
    # model has a higher version than the last.
    from omnigent.onboarding.providers import _extract_model_version

    first_version = _extract_model_version(chat_models[0].name)
    last_version = _extract_model_version(chat_models[-1].name)
    assert first_version >= last_version, (
        f"Models not sorted newest-first: first model "
        f"{chat_models[0].name!r} (v{first_version}) should have "
        f"version >= last model {chat_models[-1].name!r} (v{last_version})."
    )


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("provider-gpt-5-6", 5.6),
        ("provider-claude-opus-4-8", 4.8),
        ("provider/llama-3.1-instruct", 3.1),
        ("o3", 3.0),
        ("provider-gpt-audio-2025-12-15", 0.0),
        ("provider-gpt-oss-120b", 0.0),
        ("provider-qwen35-122b", 0.0),
    ],
)
def test_extract_model_version_ignores_dates_sizes_and_unknown_families(
    name: str, expected: float
) -> None:
    """Only vendor version tokens influence newest-first ordering."""
    from omnigent.onboarding.providers import _extract_model_version

    assert _extract_model_version(name) == expected


# ── default_chat_model ─────────────────────────────────────


def test_default_chat_model_anthropic_prefers_accessible_tier() -> None:
    """Anthropic selects the newest broadly accessible catalog tier."""
    assert default_chat_model("anthropic") == "claude-sonnet-4-6"


def test_default_chat_model_openai_uses_newest_general_model() -> None:
    """OpenAI selects the newest non-specialty catalog model."""
    default = default_chat_model("openai")
    assert default == "gpt-5.5"
    assert default.startswith("gpt-")
    for token in ("audio", "realtime", "search", "transcribe", "tts", "image"):
        assert token not in default.lower()


def test_default_chat_model_openrouter_prefers_kimi_family() -> None:
    """OpenRouter prefers its broadly-served Kimi family over newer entries."""
    assert default_chat_model("openrouter") == "moonshotai/kimi-k2.6"


def test_default_chat_model_openrouter_requires_kimi_family() -> None:
    """OpenRouter does not silently fall back to a proprietary model."""
    assert default_chat_model("openrouter", allowed_models={"openai/gpt-6"}) is None


def test_default_chat_model_without_catalog_is_none() -> None:
    """Offline onboarding asks for a model instead of using a source pin."""
    assert default_chat_model("xai") is None


def test_default_chat_model_dynamic_skips_specialty_variants() -> None:
    """The catalog rule drops specialty modalities."""
    from omnigent.onboarding.providers import _SPECIALTY_MODEL_TOKENS

    general = [
        m.name
        for m in get_chat_models("openai")
        if not any(tok in m.name.lower() for tok in _SPECIALTY_MODEL_TOKENS)
    ]
    assert general, "openai catalog should have a general-purpose model"
    # The catalog's raw newest-first top entry IS a specialty model, so the
    # dynamic rule's exclusion genuinely changes the result.
    assert general[0] != get_chat_models("openai")[0].name
    assert general[0].startswith("gpt-")


def test_default_chat_model_limits_dynamic_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callers can constrain dynamic defaults before policy selection."""
    models = [
        ModelInfo(name="gpt-audio-new", provider="vendor", mode="chat"),
        ModelInfo(name="gpt-new", provider="vendor", mode="chat"),
        ModelInfo(name="gpt-compatible", provider="vendor", mode="chat"),
    ]
    monkeypatch.setattr(_providers_mod, "get_chat_models", lambda _provider: models)

    assert (
        default_chat_model(
            "vendor",
            allowed_models={"gpt-audio-new", "gpt-compatible"},
        )
        == "gpt-compatible"
    )


def test_default_chat_model_unknown_provider_is_none() -> None:
    """An unknown provider yields None (the runtime then fails loud)."""
    assert default_chat_model("nonexistent_provider_xyz") is None


def test_get_models_returns_model_info_dataclass() -> None:
    """Models must be ModelInfo instances with required fields."""
    models = get_models("openai")
    model = models[0]
    assert isinstance(model, ModelInfo), (
        f"Expected ModelInfo instance, got {type(model).__name__}."
    )
    assert model.name, "Model name must not be empty."
    assert model.provider == "openai"


def test_get_models_unknown_provider_returns_empty() -> None:
    """Unknown provider should return an empty list, not raise."""
    models = get_models("nonexistent_provider_xyz")
    assert models == [], f"Expected empty list for unknown provider, got {len(models)} models."


def test_get_models_strips_provider_prefix() -> None:
    """Models with provider/ prefix in their name should be stripped."""
    models = get_models("gemini")
    for m in models:
        assert not m.name.startswith("gemini/"), (
            f"Model name {m.name!r} still has provider prefix — should be stripped."
        )


def test_get_models_excludes_finetuned() -> None:
    """Fine-tuned model variants (ft:*) should be excluded."""
    # Check openai which is most likely to have ft: entries.
    models = get_models("openai")
    ft_models = [m for m in models if m.name.startswith("ft:")]
    assert ft_models == [], (
        f"Fine-tuned models should be excluded, but found: {[m.name for m in ft_models]}"
    )


# ── get_provider_config ──────────────────────────────────


def test_simple_provider_returns_api_key_mode() -> None:
    """Simple providers (openai, anthropic) have a single api_key auth mode."""
    config = get_provider_config("anthropic")
    assert isinstance(config, ProviderConfig)
    assert config.default_mode == "api_key"
    assert len(config.auth_modes) == 1
    mode = config.auth_modes[0]
    assert mode.mode_id == "api_key"
    assert mode.is_default is True
    # Must have at least an api_key field.
    field_names = [f.name for f in mode.fields]
    assert "api_key" in field_names, f"Expected 'api_key' field in auth mode, got {field_names}."


def test_bedrock_has_multiple_auth_modes() -> None:
    """Bedrock should have api_key and access_keys auth modes."""
    config = get_provider_config("bedrock")
    mode_ids = [m.mode_id for m in config.auth_modes]
    assert "api_key" in mode_ids, f"Expected 'api_key' mode for bedrock, got {mode_ids}."
    assert "access_keys" in mode_ids, f"Expected 'access_keys' mode for bedrock, got {mode_ids}."
    assert config.default_mode == "api_key"


def test_azure_requires_api_base_and_version() -> None:
    """Azure auth mode must require api_base and api_version fields."""
    config = get_provider_config("azure")
    mode = config.auth_modes[0]
    field_names = [f.name for f in mode.fields]
    assert "api_key" in field_names
    assert "api_base" in field_names, (
        f"Azure auth must require api_base, got fields: {field_names}."
    )
    assert "api_version" in field_names, (
        f"Azure auth must require api_version, got fields: {field_names}."
    )


def test_auth_fields_have_descriptions() -> None:
    """Every auth field must have a non-empty description."""
    for provider_name in ["anthropic", "bedrock", "azure", "databricks"]:
        config = get_provider_config(provider_name)
        for mode in config.auth_modes:
            for field in mode.fields:
                assert field.description, (
                    f"Auth field {field.name!r} in {provider_name}/{mode.mode_id} "
                    f"has empty description."
                )


def test_unknown_provider_gets_default_api_key_mode() -> None:
    """Providers not in _PROVIDER_AUTH_MODES get a default api_key mode."""
    config = get_provider_config("some_unknown_provider")
    assert config.default_mode == "api_key"
    assert len(config.auth_modes) == 1
    assert config.auth_modes[0].fields[0].name == "api_key"
